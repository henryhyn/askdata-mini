"""Agent 的工具。

工具是写给模型用的 API：模型只能看到名字、描述和参数定义，看不到实现，出了错只能读返回的那段文字。
所以描述写在参数模型的 docstring 和 Field(description) 里 (会进 JSON Schema)，错误信息要说清楚下一步怎么做。

一次提问的可变状态放在 RunState 里：看过哪些表 (候选集)、最后一次成功的查询结果、失败了几次。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from askdata.brief import columns_brief
from askdata.catalog import Catalog
from askdata.compiler import Policy
from askdata.executor import QueryResult, run_ir, validation_message
from askdata.ir import QueryIR
from askdata.retrieval import TableIndex

PROBE_LIMIT = 50  # probe 最多返回的取值个数
SAMPLE_ROWS = 5  # 只把几行样例回给模型；完整结果绕过模型直接交给调用方，模型不需要也不应该复述大段数据


@dataclass
class RunState:
    candidates: set[str] = field(default_factory=set)  # 看过字段的表，run_query 只能查这些
    results: list[QueryResult] = field(default_factory=list)  # 成功的查询，下标 + 1 就是 result_id
    attempts: int = 0
    errors: list[str] = field(default_factory=list)


class FindTables(BaseModel):
    """检索可能包含所需数据的表，返回表名和描述 (不含字段)。
    换个说法再搜是正常的：结果里没有合适的表时，用同义词、上位词或字段层面的说法再搜一次。"""

    model_config = ConfigDict(extra='forbid')
    keywords: list[str] = Field(min_length=1, description='最关键的词放第一个 (必须命中)，后面是同义词、相关词 (命中更好)')


class GetTableColumns(BaseModel):
    """查看表的字段说明，连同这些表一跳能连接的表。只有看过字段的表才能在 run_query 里使用。"""

    model_config = ConfigDict(extra='forbid')
    tables: list[str] = Field(min_length=1, description='表名，来自 find_tables 的结果')


class ProbeColumnValues(BaseModel):
    """查某一列的真实取值，写过滤条件之前用它确认取值，不要猜。
    编码字段 (如状态、渠道) 会同时返回编码和含义，过滤条件里填编码；取值很多的编码字段 (如城市) 要给 keyword 按含义搜。
    返回的 closed 为 false 时，值集来自抽样、可能不完整：没查到不等于不存在，不要据此写取反或穷举式的过滤，也不要告诉用户“没有这个取值”。"""

    model_config = ConfigDict(extra='forbid')
    table: str
    column: str
    keyword: str = Field(default='', description='按取值或含义筛选，如“已取消”“杭州”；不给就返回全部取值')


class RunQuery(BaseModel):
    """执行查询。成功时返回 result_id、列名、行数和前几行样例；失败时返回错误，根据错误修改 IR 再试。
    列名只用 get_table_columns 返回过的，不要臆造。尽量用一次查询回答问题：按月、按类别的统计用分组，不要拆成多次查询。"""

    model_config = ConfigDict(extra='forbid')
    ir: QueryIR


class FinalAnswer(BaseModel):
    """结束本轮对话。每次回复都必须以调用它结束。"""

    model_config = ConfigDict(extra='forbid')
    kind: Literal['result', 'reply'] = Field(
        description='result：已经用 run_query 成功取到了回答问题的数，没有成功执行过不能填 result；'
        'reply：不取数的文字回复，包括闲聊、答疑、澄清、拒绝、库里没有所需数据'
    )
    summary: str = Field(description='result 时用一两句话说明取了什么数、口径是什么 (时间范围、筛选条件)，不要罗列数据；reply 时写给用户的回复')
    result_id: int | None = Field(default=None, description='kind 为 result 时必填：交给用户的那次 run_query 的 result_id')


def _inline_refs(schema: dict) -> dict:
    """把 $defs / $ref 展开成内联的 schema：不是所有 OpenAI 兼容接口都支持 $ref。"""
    defs = schema.pop('$defs', {})

    def walk(node):
        if isinstance(node, dict):
            if '$ref' in node:
                return walk(dict(defs[node['$ref'].split('/')[-1]]))
            return {k: walk(v) for k, v in node.items() if k != 'title'}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def tool_spec(name: str, model: type[BaseModel]) -> dict:
    schema = _inline_refs(model.model_json_schema())
    description = schema.pop('description', '')
    return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': schema}}


class Toolbox:
    def __init__(self, catalog: Catalog, index: TableIndex, state: RunState, policy: Callable[[set[str]], Policy] | None = None):
        self.catalog, self.index, self.state = catalog, index, state
        self.policy = policy or (lambda candidates: Policy(candidates=candidates))
        self.handlers: dict[str, tuple[type[BaseModel], Callable]] = {
            'find_tables': (FindTables, self.find_tables),
            'get_table_columns': (GetTableColumns, self.get_table_columns),
            'probe_column_values': (ProbeColumnValues, self.probe_column_values),
            'run_query': (RunQuery, self.run_query),
        }

    def specs(self) -> list[dict]:
        return [tool_spec(name, model) for name, (model, _) in self.handlers.items()] + [tool_spec('final_answer', FinalAnswer)]

    def call(self, name: str, arguments: str) -> dict:
        """解析参数 → 校验 → 执行。任何一步出错都把错误作为结果返回给模型，而不是抛异常。"""
        if name not in self.handlers:
            return {'error': f'没有工具 {name}，可用的工具：{", ".join(self.handlers)}、final_answer'}
        model, handler = self.handlers[name]
        try:
            args = model.model_validate(json.loads(arguments or '{}'))
        except json.JSONDecodeError as e:
            return {'error': f'参数不是合法的 JSON：{e}'}
        except ValidationError as e:
            return {'error': validation_message(e).replace('IR 结构不合法', '参数不合法')}
        return handler(args)

    def find_tables(self, args: FindTables) -> dict:
        hits, relaxed = self.index.search(args.keywords)
        out: dict = {'tables': [{'name': h.table, 'description': h.description, 'relevance': h.score} for h in hits]}
        if relaxed and hits:
            out['note'] = f'第一个关键词“{args.keywords[0]}”没有命中任何表，下面是只按其余关键词检索的结果，相关性可能不高'
        if not hits:
            out['note'] = '没有找到相关的表，换个说法再搜，或者告诉用户库里可能没有这类数据'
        return out

    def get_table_columns(self, args: GetTableColumns) -> dict:
        unknown = [t for t in args.tables if t not in self.catalog.tables]
        known = [t for t in args.tables if t in self.catalog.tables]
        if not known:
            return {'error': f'这些表不存在：{unknown}，先用 find_tables 检索'}
        scope = list(dict.fromkeys(known + sorted(set().union(*(self.catalog.neighbors(t) for t in known)))))
        self.state.candidates |= set(scope)
        out: dict = {'columns': columns_brief(self.catalog, scope, probe=True)}
        if unknown:
            out['note'] = f'这些表不存在，已忽略：{unknown}'
        return out

    def probe_column_values(self, args: ProbeColumnValues) -> dict:
        table = self.catalog.table(args.table)
        if table is None:
            return {'error': f'表 {args.table} 不存在'}
        column = table.columns.get(args.column)
        if column is None:
            return {'error': f'表 {args.table} 没有列 {args.column}，它的列：{", ".join(table.columns)}'}
        labels = self.catalog.enums.get(f'{table.name}.{column.name}', {})
        kw = args.keyword.strip()

        def hit(value) -> bool:
            label = labels.get(str(value), '')
            # 用户说“小程序”，含义是“微信小程序”；用户说“已取消的”，含义是“已取消”：两个方向都算命中
            return not kw or kw in str(value) or (label and (kw in label or label in kw))

        if column.values is not None:
            values = [v for v in column.values if hit(v)][:PROBE_LIMIT]
            out: dict = {'enumerable': True, 'closed': column.closed, 'values': values}
            if labels:
                out['labels'] = {str(v): labels[str(v)] for v in values if str(v) in labels}  # 编码 → 含义，旁路给出，values 能原样填进过滤条件
            if not column.closed:
                out['note'] = '值集来自最新数据的抽样，可能不完整'
            if kw and not values:
                out['note'] = f'没有找到和“{kw}”匹配的取值' + ('' if column.closed else '；值集不完整，这个取值可能只是没被抽到')
            return out
        if labels:  # 取值太多的编码字段：值集给不全，但可以按含义在字典里搜
            if not kw:
                return {'enumerable': False, 'note': f'取值有 {len(labels)} 个，给 keyword 按含义搜'}
            matches = {k: v for k, v in labels.items() if kw in v or v in kw}
            return {'enumerable': False, 'matches': dict(list(matches.items())[:PROBE_LIMIT]) or None, 'note': '' if matches else f'字典里没有“{kw}”'}
        return {'enumerable': False, 'note': '这一列取值很多，不是枚举，按范围或模糊匹配过滤'}

    def run_query(self, args: RunQuery) -> dict:
        self.state.attempts += 1
        result = run_ir(args.ir, self.catalog, self.policy(self.state.candidates))
        if not result.ok:
            self.state.errors.append(result.error)
            return {'ok': False, 'error': result.error}
        self.state.results.append(result)
        return {'ok': True, 'result_id': len(self.state.results), 'columns': result.columns, 'row_count': len(result.rows), 'sample_rows': result.rows[:SAMPLE_ROWS], 'sql': result.sql}
