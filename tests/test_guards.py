from __future__ import annotations

import pytest

from askdata.agent import Agent
from askdata.compiler import IRError, Policy
from askdata.executor import run_ir
from tests.test_compiler import compile_raw


def test_sensitive_column_is_blocked_everywhere(catalog):
    for raw in (
        {'table': 'users', 'select': [{'column': 'phone'}]},
        {'table': 'users', 'select': [{'agg': 'count'}], 'where': [{'column': 'phone', 'op': 'like', 'value': '138%'}]},
        {'table': 'users', 'select': [{'agg': 'count_distinct', 'column': 'real_name'}]},
    ):
        with pytest.raises(IRError, match='敏感字段'):
            compile_raw(raw, catalog)


def test_partitioned_table_needs_partition_filter(catalog):
    with pytest.raises(IRError, match='分区'):
        compile_raw({'table': 'user_daily_active', 'select': [{'agg': 'count'}]}, catalog)
    compile_raw({'table': 'user_daily_active', 'select': [{'agg': 'count'}], 'where': [{'column': 'dt', 'op': '=', 'value': '2026-06-30'}]}, catalog)


def test_row_scope_is_injected_on_every_scoped_table(catalog):
    raw = {
        'table': 'orders',
        'alias': 'o',
        'joins': [{'table': 'users', 'alias': 'u', 'left': 'o.user_id', 'right': 'u.user_id'}],
        'select': [{'agg': 'count'}],
    }
    c = compile_raw(raw, catalog, Policy(regions=['east']))
    assert 'o."region_code" IN (?)' in c.sql and 'u."region_code" IN (?)' in c.sql
    full = run_ir({'table': 'users', 'select': [{'agg': 'count'}]}, catalog)
    east = run_ir({'table': 'users', 'select': [{'agg': 'count'}]}, catalog, Policy(regions=['east']))
    assert east.rows[0][0] < full.rows[0][0]


def test_row_scope_cannot_be_widened_by_ir(catalog):
    # 模型自己写了 region_code = 'north'，权限条件照样注入，结果是两者的交集：空
    r = run_ir({'table': 'users', 'select': [{'agg': 'count'}], 'where': [{'column': 'region_code', 'op': '=', 'value': 'north'}]}, catalog, Policy(regions=['east']))
    assert r.rows == [[0]]


def test_no_region_means_no_data(catalog):
    with pytest.raises(IRError, match='没有任何大区'):
        compile_raw({'table': 'users', 'select': [{'agg': 'count'}]}, catalog, Policy(regions=[]))


def test_follow_up_sees_history_and_reuses_tables(catalog, index, fake):
    first = {'table': 'users', 'select': [{'agg': 'count', 'as': '用户数'}], 'where': [{'column': 'region_code', 'op': '=', 'value': 'east'}]}
    second = {**first, 'where': first['where'] + [{'column': 'is_vip', 'op': '=', 'value': 1}]}
    model = fake(
        [
            {'name': 'get_table_columns', 'args': {'tables': ['users']}},
            {'name': 'run_query', 'args': {'ir': first}},
            {'name': 'final_answer', 'args': {'kind': 'result', 'summary': '华东区用户数', 'result_id': 1}},
            # 第二轮：不再看字段，直接在上一轮的表上查询
            {'name': 'run_query', 'args': {'ir': second}},
            {'name': 'final_answer', 'args': {'kind': 'result', 'summary': '其中会员数', 'result_id': 1}},
        ]
    )
    agent = Agent(catalog, model, index)
    agent.ask('华东区有多少用户？')
    answer = agent.ask('其中付费会员有多少？')
    assert answer.kind == 'result' and answer.attempts == 1
    second_turn = model.requests[3]
    assert [m['role'] for m in second_turn] == ['system', 'user', 'assistant', 'user']
    assert '"is_vip"' not in second_turn[2]['content'] and '"region_code"' in second_turn[2]['content']
