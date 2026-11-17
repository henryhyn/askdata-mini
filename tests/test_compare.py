from __future__ import annotations

from evals.compare import same_result


def test_order_and_extra_columns_do_not_matter():
    gold = [['ios', 3], ['web', 1]]
    pred = [[1, 'web', 'x'], [3, 'ios', 'y']]
    assert same_result(gold, pred)


def test_float_tolerance():
    assert same_result([[7386298.52]], [[7386298.5200000005]])


def test_row_count_must_match():
    assert not same_result([[1]], [[1], [2]])


def test_columns_must_line_up_row_by_row():
    gold = [['a', 1], ['b', 2]]
    pred = [['a', 2], ['b', 1]]
    assert not same_result(gold, pred)
