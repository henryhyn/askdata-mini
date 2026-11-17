"""按模式创建问答引擎。命令行和评测都通过这里拿引擎，保证两边跑的是同一套东西。"""

from __future__ import annotations

from askdata.agent import Agent, Listener
from askdata.catalog import Catalog
from askdata.llm import ChatModel
from askdata.pipeline import Pipeline
from askdata.retrieval import TableIndex

MODES = ('pipeline', 'agent')


def make_engine(mode: str, catalog: Catalog, model: ChatModel, index: TableIndex | None = None, listeners: list[Listener] | None = None):
    if mode == 'pipeline':
        return Pipeline(catalog, model, index)
    if mode == 'agent':
        return Agent(catalog, model, index, listeners)
    raise ValueError(f'未知模式 {mode}，可选：{", ".join(MODES)}')
