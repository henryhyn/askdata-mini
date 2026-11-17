from __future__ import annotations

import json

import pytest

from askdata import catalog as catalog_mod
from askdata.llm import Reply, Usage
from askdata.retrieval import TableIndex


@pytest.fixture(scope='session')
def catalog():
    return catalog_mod.load()


@pytest.fixture(scope='session')
def index(catalog):
    return TableIndex(catalog)


class FakeModel:
    """按顺序吐出预先写好的回复，记下每次收到的消息。字符串是文本回复，dict 是一次工具调用。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.requests: list[list[dict]] = []
        self.usage = Usage()

    def chat(self, messages, tools=None):
        self.requests.append([dict(m) for m in messages])
        self.usage.calls += 1
        item = self.script.pop(0)
        if isinstance(item, str):
            return Reply(content=item, finish_reason='stop')
        calls = item if isinstance(item, list) else [item]
        return Reply(
            content='',
            tool_calls=[{'id': f'call_{self.usage.calls}_{i}', 'name': c['name'], 'arguments': json.dumps(c['args'], ensure_ascii=False)} for i, c in enumerate(calls)],
            finish_reason='tool_calls',
        )


@pytest.fixture
def fake():
    return FakeModel
