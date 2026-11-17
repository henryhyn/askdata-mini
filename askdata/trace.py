"""Trace：把一次提问里模型每一步看到了什么、做了什么记下来。

优化之前，先要能看见：它搜了什么词？看了哪几张表？查询为什么失败？失败后怎么改的？

    traces/<日期>/<时间>-<编号>.jsonl     一行一个事件
    uv run python -m askdata.trace <文件>   按步骤查看
"""

from __future__ import annotations

import itertools
import json
import sys
import threading
import time
from pathlib import Path

from askdata import config

_seq = itertools.count(1)
_lock = threading.Lock()


class TraceWriter:
    def __init__(self, root: Path | None = None):
        root = root or config.ROOT / 'traces'
        with _lock:
            n = next(_seq)
        self.path = root / time.strftime('%Y-%m-%d') / f'{time.strftime("%H%M%S")}-{n:03d}.jsonl'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.start = time.monotonic()

    def __call__(self, kind: str, data: dict) -> None:
        event = {'t': round(time.monotonic() - self.start, 2), 'kind': kind, **data}
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')

    def on_llm_call(self, messages: list[dict], reply, usage) -> None:
        """挂在 OpenAIChat.on_call 上：记录每次模型调用的 token 和耗时。"""
        self(
            'llm_usage',
            {
                'messages': len(messages),
                'prompt_tokens': usage.prompt_tokens,
                'cached_tokens': usage.cached_tokens,
                'completion_tokens': usage.completion_tokens,
                'seconds': round(usage.seconds, 2),
            },
        )


def short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + '…'


def view(path: Path) -> str:
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        e = json.loads(line)
        t = f'{e["t"]:>6.1f}s'
        match e['kind']:
            case 'run_start':
                out.append(f'{t}  ▶ {e["question"]}')
            case 'llm_usage':
                out.append(f'{t}    模型调用：输入 {e["prompt_tokens"]} (缓存 {e["cached_tokens"]}) / 输出 {e["completion_tokens"]} token，{e["seconds"]} 秒')
            case 'llm':
                if e.get('content'):
                    out.append(f'{t}    💬 {short(e["content"])}')
            case 'tool':
                out.append(f'{t}  [{e["step"]}] {e["name"]}({short(e["arguments"], 300)})')
                out.append(f'{" " * 9}⎿ {short(e["result"], 300)}')
            case 'run_end':
                out.append(f'{t}  ■ {e["kind"]}：{short(e["text"])} (执行查询 {e["attempts"]} 次)')
    return '\n'.join(out)


if __name__ == '__main__':
    print(view(Path(sys.argv[1])))
