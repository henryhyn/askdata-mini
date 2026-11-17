from __future__ import annotations

from askdata.agent import Agent

GOOD_IR = {'table': 'orders', 'select': [{'agg': 'count', 'as': 'n'}], 'where': [{'column': 'status', 'op': '=', 'value': 5}]}


def run(catalog, index, fake, script):
    events = []
    agent = Agent(catalog, fake(script), index, listeners=[lambda k, d: events.append((k, d))])
    return agent.ask('已取消的订单有多少笔？'), events, agent.model


def test_happy_path(catalog, index, fake):
    answer, events, model = run(
        catalog,
        index,
        fake,
        [
            {'name': 'find_tables', 'args': {'keywords': ['订单', '已取消']}},
            {'name': 'get_table_columns', 'args': {'tables': ['orders']}},
            {'name': 'run_query', 'args': {'ir': GOOD_IR}},
            {'name': 'final_answer', 'args': {'kind': 'result', 'summary': '已取消订单数', 'result_id': 1}},
        ],
    )
    assert answer.kind == 'result' and answer.rows == [[2392]]
    assert [d['name'] for k, d in events if k == 'tool'] == ['find_tables', 'get_table_columns', 'run_query', 'final_answer']
    # 每个工具调用都有对应的结果消息
    last = model.requests[-1]
    assert sum(m['role'] == 'tool' for m in last) == 3


def test_query_requires_seen_columns(catalog, index, fake):
    answer, events, _ = run(
        catalog,
        index,
        fake,
        [
            {'name': 'run_query', 'args': {'ir': GOOD_IR}},
            {'name': 'final_answer', 'args': {'kind': 'reply', 'summary': '查不了'}},
        ],
    )
    result = next(d['result'] for k, d in events if k == 'tool' and d['name'] == 'run_query')
    assert '不在可查询的范围内' in result['error']
    assert answer.kind == 'reply' and answer.attempts == 1


def test_claimed_result_without_success_is_error(catalog, index, fake):
    answer, _, _ = run(catalog, index, fake, [{'name': 'final_answer', 'args': {'kind': 'result', 'summary': '共 100 笔', 'result_id': 1}}])
    assert answer.kind == 'error' and '没有执行' in answer.text


def test_result_id_picks_the_delivered_query(catalog, index, fake):
    explore = {'table': 'orders', 'select': [{'column': 'status'}, {'agg': 'count'}], 'group_by': ['status']}
    script = [
        {'name': 'get_table_columns', 'args': {'tables': ['orders']}},
        {'name': 'run_query', 'args': {'ir': explore}},
        {'name': 'run_query', 'args': {'ir': GOOD_IR}},
        {'name': 'final_answer', 'args': {'kind': 'result', 'summary': '已取消订单数', 'result_id': 2}},
    ]
    answer, _, _ = run(catalog, index, fake, script)
    assert answer.rows == [[2392]]
    script[-1] = {'name': 'final_answer', 'args': {'kind': 'result', 'summary': 'x', 'result_id': 3}}
    answer, _, _ = run(catalog, index, fake, script)
    assert answer.kind == 'error' and 'result_id' in answer.text


def test_bad_arguments_are_returned_to_model(catalog, index, fake):
    answer, events, _ = run(
        catalog,
        index,
        fake,
        [
            {'name': 'find_tables', 'args': {'query': '订单'}},
            {'name': 'final_answer', 'args': {'kind': 'reply', 'summary': 'ok'}},
        ],
    )
    result = next(d['result'] for k, d in events if k == 'tool' and d['name'] == 'find_tables')
    assert 'keywords' in result['error'] and answer.kind == 'reply'


def test_plain_text_reply(catalog, index, fake):
    answer, _, _ = run(catalog, index, fake, ['你好，我是数据助手'])
    assert answer.kind == 'reply' and answer.text == '你好，我是数据助手'


def test_request_limit(catalog, index, fake):
    answer, _, model = run(catalog, index, fake, [{'name': 'find_tables', 'args': {'keywords': ['订单']}}] * 16)
    assert answer.kind == 'error' and model.usage.calls == 16


def test_tool_schema_has_no_refs(catalog, index):
    import json

    from askdata.tools import RunState, Toolbox

    specs = Toolbox(catalog, index, RunState()).specs()
    text = json.dumps(specs, ensure_ascii=False)
    assert '$ref' not in text and '$defs' not in text
    run_query = next(s for s in specs if s['function']['name'] == 'run_query')
    # IR 的字段规则随工具定义发给模型
    assert '编码字段填编码本身' in json.dumps(run_query, ensure_ascii=False)


def probe(catalog, index, table, column, keyword=''):
    import json

    from askdata.tools import RunState, Toolbox

    return Toolbox(catalog, index, RunState()).call('probe_column_values', json.dumps({'table': table, 'column': column, 'keyword': keyword}))


def test_probe_maps_label_to_code(catalog, index):
    assert probe(catalog, index, 'orders', 'status', '已取消') == {'enumerable': True, 'closed': True, 'values': [5], 'labels': {'5': '已取消'}}
    assert probe(catalog, index, 'orders', 'platform', '小程序')['values'] == ['mini_program']


def test_probe_high_cardinality_needs_keyword(catalog, index):
    assert 'keyword' in probe(catalog, index, 'users', 'city_id')['note']
    assert probe(catalog, index, 'users', 'city_id', '杭州')['matches'] == {'102': '杭州'}


def test_probe_sampled_values_are_not_closed(catalog, index):
    out = probe(catalog, index, 'user_daily_active', 'app_version', '6.0')
    # 6.0 只出现在 1–2 月，最新数据的抽样里没有；值集要标成不完整，不能让模型以为不存在
    assert out['closed'] is False and out['values'] == [] and '可能只是没被抽到' in out['note']


def test_profile_rules(catalog):
    orders = catalog.tables['orders']
    assert orders.columns['status'].closed and orders.columns['status'].values == [1, 2, 3, 4, 5]
    assert orders.columns['pay_amount'].values is None  # 金额不当枚举
    assert orders.columns['order_time'].values is None  # 日期不当枚举
    assert catalog.tables['refunds'].columns['reason_code'].values == [1, 2, 3, 4, 5, 9]  # 小表全表核实，老口径 9 也在
