"""命令行入口。

    askdata "上周的 GMV 是多少"        问一个问题
    askdata                           交互模式，输入 exit 退出
    askdata --mode pipeline "..."     用 M1 的固定流水线回答
    askdata --regions east,north      以只能看华东、华北数据的身份提问 (行级权限)
"""

from __future__ import annotations

import argparse
import sys

from askdata import catalog as catalog_mod
from askdata import config
from askdata.answer import Answer
from askdata.engine import MODES, make_engine
from askdata.llm import OpenAIChat
from askdata.trace import TraceWriter, short

MAX_SHOW = 20


def show_table(columns: list[str], rows: list[list]) -> str:
    def cell(v) -> str:
        if isinstance(v, float):
            return f'{v:,.2f}'
        return '' if v is None else str(v)

    body = [[cell(v) for v in r] for r in rows[:MAX_SHOW]]

    def width(s: str) -> int:  # 中文按两格算
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)

    widths = [max([width(c)] + [width(r[i]) for r in body]) for i, c in enumerate(columns)]

    def line(cells: list[str]) -> str:
        return '  '.join(c + ' ' * (w - width(c)) for c, w in zip(cells, widths, strict=True))

    out = [line(columns), line(['-' * w for w in widths])] + [line(r) for r in body]
    if len(rows) > MAX_SHOW:
        out.append(f'... 共 {len(rows)} 行，只显示前 {MAX_SHOW} 行')
    return '\n'.join(out)


def show(answer: Answer) -> str:
    out = []
    if answer.kind == 'result':
        out.append(show_table(answer.columns, answer.rows))
        out.append(f'\n{answer.text}')
        out.append(f'\n口径 (用到的表：{", ".join(answer.tables)})：\n{answer.sql}')
    else:
        out.append(answer.text)
    return '\n'.join(out)


def progress(kind: str, data: dict) -> None:
    """把 Agent 的每一步打印出来。"""
    if kind == 'tool' and data['name'] != 'final_answer':
        print(f'⏺ {data["name"]}({short(data["arguments"], 120)})')
        result = data['result']
        if 'error' in result:
            print(f'  ⎿ ✗ {short(result["error"], 200)}')
        elif data['name'] == 'find_tables':
            print(f'  ⎿ {", ".join(t["name"] for t in result["tables"]) or "没有命中"}')
        elif data['name'] == 'run_query':
            print(f'  ⎿ {result["row_count"]} 行')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='askdata', description='用自然语言查询演示数据库')
    parser.add_argument('question', nargs='?', help='要问的问题；不给就进入交互模式')
    parser.add_argument('--mode', choices=MODES, default=MODES[-1], help=f'默认 {MODES[-1]}')
    parser.add_argument('--regions', help='行级权限：只能看这些大区的数据，逗号分隔，如 east,north')
    args = parser.parse_args(argv)

    catalog = catalog_mod.load()
    model = OpenAIChat()
    trace = TraceWriter()
    model.on_call = trace.on_llm_call
    regions = [r.strip() for r in args.regions.split(',') if r.strip()] if args.regions else None
    engine = make_engine(args.mode, catalog, model, listeners=[trace, progress], regions=regions)

    def ask(q: str) -> None:
        before = (model.usage.calls, model.usage.prompt_tokens, model.usage.completion_tokens, model.usage.seconds)
        answer = engine.ask(q)
        print(show(answer))
        u = model.usage
        print(
            f'\n── {answer.kind} · 模型调用 {u.calls - before[0]} 次 · 输入 {u.prompt_tokens - before[1]} / 输出 {u.completion_tokens - before[2]} token'
            f' · {u.seconds - before[3]:.1f} 秒 · Trace：{trace.path.relative_to(config.ROOT)}'
        )

    if args.question:
        ask(args.question)
        return 0
    print('输入问题，可以接着上一个问题追问；exit 退出')
    while True:
        try:
            q = input('\n> ').strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if q in ('exit', 'quit'):
            return 0
        if q:
            ask(q)


if __name__ == '__main__':
    sys.exit(main())
