from __future__ import annotations

from askdata.retrieval import tokenize


def test_metric_name_finds_its_table(index):
    hits, relaxed = index.search(['GMV'])
    assert hits[0].table == 'orders' and not relaxed


def test_enum_label_finds_table(index):
    hits, _ = index.search(['好友邀请', '注册'])
    assert hits[0].table == 'users'


def test_relaxes_when_must_term_misses(index):
    hits, relaxed = index.search(['不存在的词', '退款'])
    assert relaxed and hits[0].table == 'refunds'


def test_fts_syntax_is_stripped():
    assert tokenize('"订单" AND (退款) OR NEAR*') == ['订单', 'and', '退款', 'or', 'near']


def test_no_keywords(index):
    assert index.search([]) == ([], False)
