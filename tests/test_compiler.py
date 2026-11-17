from __future__ import annotations

import pytest

from askdata.compiler import MAX_LIMIT, IRError, Policy, compile_ir
from askdata.executor import run_ir
from askdata.ir import QueryIR


def compile_raw(raw, catalog, policy=None):
    return compile_ir(QueryIR.model_validate(raw), catalog, policy)


def test_datetime_upper_bound_includes_whole_day(catalog):
    c = compile_raw({'table': 'orders', 'select': [{'agg': 'count'}], 'where': [{'column': 'order_time', 'op': 'between', 'value': ['2026-06-22', '2026-06-28']}]}, catalog)
    assert '>= ? AND t."order_time" < ?' in c.sql
    assert c.params == ['2026-06-22', '2026-06-29']


def test_datetime_le_date_means_end_of_day(catalog):
    c = compile_raw({'table': 'orders', 'select': [{'agg': 'count'}], 'where': [{'column': 'order_time', 'op': '<=', 'value': '2026-06-28'}]}, catalog)
    assert c.params == ['2026-06-29'] and '< ?' in c.sql


def test_plain_column_must_be_grouped(catalog):
    with pytest.raises(IRError, match='group_by'):
        compile_raw({'table': 'orders', 'select': [{'column': 'platform'}, {'agg': 'count'}]}, catalog)


def test_unknown_column_lists_real_columns(catalog):
    with pytest.raises(IRError, match='pay_amount'):
        compile_raw({'table': 'orders', 'select': [{'agg': 'sum', 'column': 'amount'}]}, catalog)


def test_ambiguous_column_needs_alias(catalog):
    raw = {
        'table': 'orders',
        'alias': 'o',
        'joins': [{'table': 'users', 'alias': 'u', 'left': 'o.user_id', 'right': 'u.user_id'}],
        'select': [{'column': 'region_code'}],
    }
    with pytest.raises(IRError, match='o.region_code'):
        compile_raw(raw, catalog)


def test_limit_is_capped(catalog):
    c = compile_raw({'table': 'orders', 'select': [{'column': 'order_id'}], 'limit': 100000}, catalog)
    assert c.limit == MAX_LIMIT and c.sql.endswith(f'LIMIT {MAX_LIMIT}')


def test_candidate_tables_are_enforced(catalog):
    with pytest.raises(IRError, match='不在可查询的范围内'):
        compile_raw({'table': 'users', 'select': [{'agg': 'count'}]}, catalog, Policy(candidates={'orders'}))


def test_misspelled_key_is_rejected_not_ignored(catalog):
    r = run_ir({'table': 'orders', 'select': [{'agg': 'count'}], 'filters': [{'column': 'status', 'op': '=', 'value': 5}]}, catalog)
    assert not r.ok and 'filters' in r.error


def test_sum_needs_number(catalog):
    with pytest.raises(IRError, match='数值列'):
        compile_raw({'table': 'orders', 'select': [{'agg': 'sum', 'column': 'platform'}]}, catalog)


def test_week_bucket_starts_on_monday(catalog):
    r = run_ir(
        {
            'table': 'orders',
            'select': [{'column': 'order_time', 'bucket': 'week', 'as': '周'}, {'agg': 'count', 'as': 'n'}],
            'where': [{'column': 'order_time', 'op': 'between', 'value': ['2026-06-01', '2026-06-14']}],
            'order_by': [{'ref': '周'}],
        },
        catalog,
    )
    assert r.ok and [row[0] for row in r.rows] == ['2026-06-01', '2026-06-08']


def test_join_and_group(catalog):
    r = run_ir(
        {
            'table': 'inventory',
            'alias': 'i',
            'joins': [{'table': 'warehouses', 'alias': 'w', 'left': 'i.warehouse_id', 'right': 'w.warehouse_id'}],
            'select': [{'column': 'w.warehouse_name'}, {'agg': 'sum', 'column': 'i.stock', 'as': '库存'}],
            'group_by': ['w.warehouse_name'],
        },
        catalog,
    )
    assert r.ok and len(r.rows) == 5
