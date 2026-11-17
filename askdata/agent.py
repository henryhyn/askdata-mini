"""M2：工具 Agent。

    把问题交给模型
    循环，直到模型调用 final_answer 或请求次数用完:
        模型决定调用哪些工具 (找表、看字段、执行查询)
        执行工具，结果加入对话

和 M1 的流水线比，控制流从代码交给了模型：检索不对可以换词再搜，看了字段发现不对可以换表，
查询报错可以改。代价是调用次数不固定、延迟波动变大，同一个问题每次走的路可能不一样。

结束时以执行记录为准 (finalize)：模型要交付哪次查询的结果，必须用 result_id 指明，而且这次查询确实执行成功过；
模型说“取到了”却拿不出这样一次查询，就如实报告失败。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import ValidationError

from askdata import config
from askdata.answer import Answer
from askdata.brief import metrics_brief
from askdata.catalog import Catalog
from askdata.llm import ChatModel
from askdata.retrieval import TableIndex
from askdata.tools import FinalAnswer, RunState, Toolbox

MAX_REQUESTS = 16

INSTRUCTIONS = """你是公司内部的数据分析助手，帮不会写 SQL 的同事查数据。今天是 {today}。数据是只读的。

工作方式：
1. 闲聊、和数据无关、要求查看个人敏感信息或修改数据的请求：不调用其他工具，直接调用 final_answer(kind="reply") 回复。
2. 查数据：find_tables 找表 → get_table_columns 看字段 → run_query 执行。只能查看过字段的表。
3. 检索结果里没有合适的表，就换个说法再搜，不要在不相关的表上硬凑。
4. run_query 失败时，根据错误修改 IR 再试。
5. 取到数后调用 final_answer(kind="result", result_id=...)，result_id 是直接回答问题的那次查询。用户只会看到这一次查询的结果，summary 里不要引用其他查询的数。
6. 库里确实没有需要的数据，调用 final_answer(kind="reply") 说明缺什么，不要编造。
每次回复都必须以调用 final_answer 结束。

指标口径：
{metrics}"""

Listener = Callable[[str, dict], None]


class Agent:
    def __init__(self, catalog: Catalog, model: ChatModel, index: TableIndex | None = None, listeners: list[Listener] | None = None):
        self.catalog = catalog
        self.model = model
        self.index = index or TableIndex(catalog)
        self.listeners = listeners or []
        self.instructions = INSTRUCTIONS.format(today=config.TODAY, metrics=metrics_brief(catalog))

    def emit(self, kind: str, data: dict) -> None:
        for listener in self.listeners:
            listener(kind, data)

    def ask(self, question: str) -> Answer:
        state = RunState()
        tools = Toolbox(self.catalog, self.index, state)
        specs = tools.specs()  # 工具定义和系统提示词在会话内不变，构成稳定的前缀，便于命中模型侧的前缀缓存
        messages: list[dict] = [{'role': 'system', 'content': self.instructions}, {'role': 'user', 'content': question}]
        self.emit('run_start', {'question': question})

        final: FinalAnswer | None = None
        for step in range(1, MAX_REQUESTS + 1):
            reply = self.model.chat(messages, specs)
            self.emit('llm', {'step': step, 'content': reply.content, 'tool_calls': reply.tool_calls, 'finish_reason': reply.finish_reason})
            if not reply.tool_calls:
                # 没调工具、直接说话：当作文字回复收尾
                final = FinalAnswer(kind='reply', summary=reply.content or '(模型没有回复内容)')
                break
            messages.append(
                {
                    'role': 'assistant',
                    'content': reply.content or None,
                    'tool_calls': [
                        {'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in reply.tool_calls
                    ],
                }
            )
            # 每个工具调用都要有结果，否则下一次请求会被接口拒绝；final_answer 也回一个结果
            for call in reply.tool_calls:
                if call['name'] == 'final_answer':
                    final, result = self._final(call['arguments'])
                else:
                    result = tools.call(call['name'], call['arguments'])
                self.emit('tool', {'step': step, 'name': call['name'], 'arguments': call['arguments'], 'result': result})
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result, ensure_ascii=False, default=str)})
            if final:
                break

        answer = finalize(final, state)
        self.emit('run_end', {'kind': answer.kind, 'text': answer.text, 'attempts': answer.attempts})
        return answer

    def _final(self, arguments: str) -> tuple[FinalAnswer | None, dict]:
        try:
            return FinalAnswer.model_validate(json.loads(arguments or '{}')), {'ok': True}
        except (json.JSONDecodeError, ValidationError) as e:
            return None, {'error': f'final_answer 参数不合法：{e}'}


def finalize(final: FinalAnswer | None, state: RunState) -> Answer:
    """以执行记录为准，不信模型的自我评价。"""
    base = {'attempts': state.attempts, 'errors': state.errors}
    if final is None:
        last = f'，最后一次错误：{state.errors[-1]}' if state.errors else ''
        return Answer('error', f'请求次数用完了还没有结束{last}', **base)
    if final.kind == 'reply':
        return Answer('reply', final.summary, **base)
    if not state.results:
        last = f'：{state.errors[-1]}' if state.errors else '，一次查询都没有执行'
        return Answer('error', f'模型说取到了数，但本轮没有一次查询执行成功{last}', **base)
    if final.result_id is None or not 1 <= final.result_id <= len(state.results):
        return Answer('error', f'模型说取到了数，但给出的 result_id ({final.result_id}) 不是本轮成功执行过的查询', **base)
    r = state.results[final.result_id - 1]
    return Answer('result', final.summary, ir=r.ir, sql=r.sql, columns=r.columns, rows=r.rows, tables=r.tables, **base)
