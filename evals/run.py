"""评测运行器。

    uv run python -m evals.run                     # 全部用例跑一遍
    uv run python -m evals.run --layer 枚举 -n 3     # 只跑一层，每题跑 3 次
    uv run python -m evals.run --verify            # 只检查标准答案本身：每条标准 SQL 都能执行、结果非空、没有并列

每题判分规则见 compare.py。结果写到 evals/results/<时间>-<模式>.json，失败用例的 IR、SQL、错误都在里面。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from askdata import catalog as catalog_mod
from askdata import config
from askdata.engine import MODES, make_engine
from askdata.llm import OpenAIChat
from askdata.retrieval import TableIndex
from askdata.trace import TraceWriter
from evals.compare import same_result

HERE = Path(__file__).parent


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def gold_rows(sql: str) -> list[list]:
    with sqlite3.connect(config.DB_PATH) as db:
        return [list(r) for r in db.execute(sql)]


def gold_tables(sql: str) -> set[str]:
    return set(re.findall(r'\b(?:FROM|JOIN)\s+(\w+)', sql, re.I))


def judge(case: dict, answer) -> tuple[bool, str]:
    forbidden = case.get('forbid_columns') or []
    if answer.sql and any(re.search(rf'"{c}"', answer.sql) for c in forbidden):
        return False, '查询了禁止的字段'
    if case.get('expect') == 'reply':
        return (answer.kind == 'reply', '' if answer.kind == 'reply' else f'应该文字回复，实际是 {answer.kind}')
    if answer.kind != 'result':
        return False, f'没有取到数 ({answer.kind})'
    if same_result(gold_rows(case['gold_sql']), answer.rows):
        return True, ''
    return False, '结果和标准答案不一致'


def run_one(case: dict, mode: str, catalog, index) -> dict:
    model = OpenAIChat()
    trace = TraceWriter()
    model.on_call = trace.on_llm_call
    engine = make_engine(mode, catalog, model, index, listeners=[trace])
    start = time.monotonic()
    answer = None
    try:
        for q in case['turns']:
            answer = engine.ask(q)
        ok, reason = judge(case, answer)
    except Exception as e:  # 网络错误等：记为失败，不中断整轮评测
        ok, reason = False, f'异常：{type(e).__name__}: {e}'
    seconds = time.monotonic() - start
    record = {
        'id': case['id'],
        'layer': case['layer'],
        'pass': ok,
        'reason': reason,
        'seconds': round(seconds, 1),
        'llm_calls': model.usage.calls,
        'prompt_tokens': model.usage.prompt_tokens,
        'completion_tokens': model.usage.completion_tokens,
        'trace': str(trace.path.relative_to(config.ROOT)),
    }
    if answer is not None:
        a = asdict(answer)
        a['rows'] = a['rows'][:10]
        record['answer'] = a
        if 'gold_sql' in case:
            record['tables_ok'] = gold_tables(case['gold_sql']) - {'cities'} <= set(answer.tables) | {'cities'} if answer.tables else False
            record['zero_rows'] = answer.kind == 'result' and not answer.rows
    return record


def report(records: list[dict], mode: str) -> str:
    lines = [f'模式：{mode} · 模型：{config.model_name()} · {len(records)} 次运行', '']
    layers = list(dict.fromkeys(r['layer'] for r in records))
    lines.append(f'{"分层":<6}{"通过":>8}')
    for layer in layers:
        rs = [r for r in records if r['layer'] == layer]
        lines.append(f'{layer:<6}{sum(r["pass"] for r in rs):>4}/{len(rs):<3}')
    total = sum(r['pass'] for r in records)
    lines.append(f'{"合计":<6}{total:>4}/{len(records):<3} ({total / len(records):.0%})')
    result_cases = [r for r in records if 'tables_ok' in r]
    secs = sorted(r['seconds'] for r in records)
    lines += [
        '',
        f'选表正确率      {sum(r["tables_ok"] for r in result_cases) / max(len(result_cases), 1):.0%}',
        f'0 行结果        {sum(r["zero_rows"] for r in result_cases)} 次',
        f'平均模型调用    {statistics.mean(r["llm_calls"] for r in records):.1f} 次',
        f'平均输入 token  {statistics.mean(r["prompt_tokens"] for r in records):.0f}',
        f'耗时 P50 / P95  {statistics.median(secs):.1f} / {secs[min(len(secs) - 1, int(len(secs) * 0.95))]:.1f} 秒',
    ]
    failed: dict[str, list[str]] = {}
    for r in records:
        if not r['pass']:
            failed.setdefault(r['id'], []).append(r['reason'])
    if failed:
        lines += ['', '失败用例 (失败次数：原因)：']
        lines += [f'  {k} ×{len(v)}：{"；".join(dict.fromkeys(v))}' for k, v in failed.items()]
    return '\n'.join(lines)


def verify(cases: list[dict]) -> int:
    bad = 0
    for c in cases:
        if 'gold_sql' not in c:
            continue
        rows = gold_rows(c['gold_sql'])
        problem = '' if rows else '结果为空'
        m = re.search(r'LIMIT (\d+)\s*$', c['gold_sql'])
        if m and not problem:  # 排名类题目：第 N 名和第 N+1 名并列时，标准答案不唯一
            more = gold_rows(re.sub(r'LIMIT \d+\s*$', f'LIMIT {int(m.group(1)) + 1}', c['gold_sql']))
            nums = [i for i, v in enumerate(rows[-1]) if isinstance(v, (int, float))]  # 排序依据是某个数值列
            if len(more) > len(rows) and any(more[-1][i] == rows[-1][i] for i in nums):
                problem = f'第 {m.group(1)} 名有并列'
        bad += bool(problem)
        print(f'{"✗" if problem else "✓"} {c["id"]:<32}{len(rows):>4} 行  {problem}')
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=MODES, default=MODES[-1])
    parser.add_argument('--cases', type=Path, default=HERE / 'cases.jsonl')
    parser.add_argument('--layer', help='只跑某一层')
    parser.add_argument('--id', help='只跑某一题')
    parser.add_argument('-n', '--repeat', type=int, default=1, help='每题跑几次')
    parser.add_argument('-j', '--workers', type=int, default=4)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()

    cases = load_cases(args.cases)
    cases = [c for c in cases if (not args.layer or c['layer'] == args.layer) and (not args.id or c['id'] == args.id)]
    if args.verify:
        return verify(cases)

    catalog = catalog_mod.load()
    index = TableIndex(catalog)
    jobs = [c for c in cases for _ in range(args.repeat)]
    with ThreadPoolExecutor(args.workers) as pool:
        records = list(pool.map(lambda c: run_one(c, args.mode, catalog, index), jobs))

    text = report(records, args.mode)
    print(text)
    out = HERE / 'results' / f'{time.strftime("%Y%m%d-%H%M%S")}-{args.mode}.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({'mode': args.mode, 'model': config.model_name(), 'records': records}, ensure_ascii=False, indent=1, default=str))
    print(f'\n详细结果：{out.relative_to(HERE.parent)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
