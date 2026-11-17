"""M1：固定流水线。

    理解问题 (要不要取数、搜什么词) → 检索表 → 选表 → 生成 IR → 执行，失败带着错误重新生成 (最多 2 次)

每一步调用一次模型，控制流写死在代码里：可预测、好测试，调用次数固定在 3–5 次。
它的弱点也写死了：检索漏掉了正确的表，后面的重试只会在同一批错表上打转。M2 的 Agent 就是为了解决这个问题。
"""

from __future__ import annotations

import json

from askdata import config
from askdata.answer import Answer
from askdata.brief import columns_brief, metrics_brief
from askdata.catalog import Catalog
from askdata.compiler import Policy
from askdata.executor import run_ir
from askdata.ir import QueryIR
from askdata.llm import ChatModel, parse_json
from askdata.retrieval import TableIndex

MAX_REPAIR = 2

ROLE = f"""你是公司内部的数据分析助手，帮不会写 SQL 的同事查数据。今天是 {config.TODAY}。
数据是只读的；只回答能用数据库里的数据回答的问题，库里没有的数据如实说没有，不要编造。"""

UNDERSTAND = """判断用户的问题要不要查数据，并给出检索数据表用的关键词。只输出 JSON：
{"intent": "query" 或 "reply", "reply": "intent 为 reply 时直接回复用户的话", "keywords": ["最关键的词放第一个", "再给几个同义词、相关词"]}
闲聊、和数据无关、要求查看手机号姓名等个人敏感信息的请求，intent 填 reply。"""

CHOOSE = """下面是检索到的候选表。选出回答问题需要的表 (可以多张，要连表时都选上)。只输出 JSON：
{"tables": ["表名"], "reply": "一张都用不上时，告诉用户为什么"}"""

GENERATE = """根据字段说明，为用户的问题写一个查询 IR。只输出 IR 的 JSON，不要解释。
IR 的 JSON Schema：
{schema}

指标口径：
{metrics}

可用的表和字段：
{columns}"""


class Pipeline:
    def __init__(self, catalog: Catalog, model: ChatModel, index: TableIndex | None = None):
        self.catalog = catalog
        self.model = model
        self.index = index or TableIndex(catalog)

    def ask(self, question: str) -> Answer:
        # 1. 理解问题
        try:
            plan = self._json(UNDERSTAND, question)
        except ValueError as e:
            return Answer('error', f'模型没有按格式回复：{e}')
        if plan.get('intent') == 'reply':
            return Answer('reply', plan.get('reply') or '这个问题我回答不了。')

        # 2. 检索
        hits, _ = self.index.search(plan.get('keywords') or [question])
        if not hits:
            return Answer('reply', '没有找到和这个问题相关的数据表，换个说法试试？')

        # 3. 选表
        listing = '\n'.join(f'- {h.table}：{h.description}' for h in hits)
        try:
            chosen = self._json(f'{CHOOSE}\n\n候选表：\n{listing}', question)
        except ValueError as e:
            return Answer('error', f'模型没有按格式回复：{e}')
        tables = [t for t in chosen.get('tables') or [] if t in self.catalog.tables]
        if not tables:
            return Answer('reply', chosen.get('reply') or '库里没有能回答这个问题的数据。')

        # 4. 生成 IR 并执行；字段说明只给选中的表和它们一跳能连接的表
        scope = list(dict.fromkeys(tables + sorted(set().union(*(self.catalog.neighbors(t) for t in tables)))))
        system = GENERATE.format(
            schema=json.dumps(QueryIR.model_json_schema(), ensure_ascii=False),
            metrics=metrics_brief(self.catalog),
            columns=columns_brief(self.catalog, scope),
        )
        messages = [{'role': 'system', 'content': f'{ROLE}\n\n{system}'}, {'role': 'user', 'content': question}]
        answer = Answer('error')
        for _ in range(1 + MAX_REPAIR):
            reply = self.model.chat(messages)
            messages.append({'role': 'assistant', 'content': reply.content})
            try:
                raw = parse_json(reply.content)
            except ValueError as e:
                error = f'回复不是合法的 JSON：{e}'
            else:
                result = run_ir(raw, self.catalog, Policy(candidates=set(scope)))
                answer.attempts += 1
                if result.ok:
                    return Answer(
                        'result',
                        f'查询返回 {len(result.rows)} 行。',
                        ir=result.ir,
                        sql=result.sql,
                        columns=result.columns,
                        rows=result.rows,
                        tables=result.tables,
                        attempts=answer.attempts,
                        errors=answer.errors,
                    )
                error = result.error
            answer.errors.append(error)
            messages.append({'role': 'user', 'content': f'这个 IR 有问题：{error}\n请修改后重新输出完整的 IR JSON。'})
        answer.text = f'尝试 {1 + MAX_REPAIR} 次都没有成功，最后一次的错误：{answer.errors[-1]}'
        return answer

    def _json(self, instruction: str, question: str) -> dict:
        messages = [{'role': 'system', 'content': f'{ROLE}\n\n{instruction}'}, {'role': 'user', 'content': question}]
        return parse_json(self.model.chat(messages).content)
