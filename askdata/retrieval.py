"""找表：jieba 分词 + SQLite FTS5 全文检索。

表只有十几到几百张时，词法检索就够用，不必上向量库。做法和书里 C2 第三节“设计 3：检索先用词法”一致：
- 写入侧：把每张表的表名、描述、字段名、字段描述、枚举含义用 jieba 分好词，以空格连接后建全文索引；
- 查询侧：模型给出的关键词同样过 jieba，第一个关键词作必含词，其余作扩展词；
- 领域词典：表名、字段名、枚举含义、指标名灌进 jieba，避免“华东区”“退款金额”被切碎。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import warnings
from dataclasses import dataclass

with warnings.catch_warnings():  # jieba 的老代码在 Python 3.12 下有转义序列警告，和本项目无关
    warnings.simplefilter('ignore', SyntaxWarning)
    import jieba

from askdata.catalog import Catalog

jieba.setLogLevel(logging.WARNING)

_WORD = re.compile(r'[\w一-鿿]+')  # 只留汉字、字母、数字、下划线：FTS5 的语法字符一律去掉


@dataclass
class Hit:
    table: str
    description: str
    score: float  # 越大越相关


def tokenize(text: str) -> list[str]:
    tokens = []
    for piece in _WORD.findall(text.lower()):
        tokens += [t for t in jieba.lcut(piece) if t.strip()]
    return tokens


class TableIndex:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        for word in self._domain_words():
            jieba.add_word(word)
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.execute('CREATE VIRTUAL TABLE docs USING fts5(name UNINDEXED, body)')
        for t in catalog.tables.values():
            self.db.execute('INSERT INTO docs VALUES (?, ?)', (t.name, ' '.join(tokenize(self._text(t)))))

    def _domain_words(self) -> set[str]:
        words = {m['name'].split(' ')[0] for m in self.catalog.metrics}
        for labels in self.catalog.enums.values():
            words |= {v for v in labels.values() if len(v) <= 8}
        words |= {'退款金额', '实付金额', '订单状态', '注册渠道', '日活', '活跃用户', '客服工单', '广告花费'}
        return {w for w in words if len(w) >= 2}

    def _text(self, t) -> str:
        # 表描述重复两次，相当于给它加权：表级的说法比字段描述更能代表这张表是干什么的
        parts = [t.name, t.description, t.description]
        for c in t.columns.values():
            parts += [c.name, c.description]
            labels = self.catalog.enums.get(f'{t.name}.{c.name}', {})
            if len(labels) <= 20:  # 枚举含义也进索引，“已取消”“好友邀请”这类说法才能找到表；城市这种高基数的不进
                parts += labels.values()
        for m in self.catalog.metrics:  # 指标名挂到口径里提到的表上：用户说“GMV”，要能找到 orders
            if re.search(rf'\b{t.name}\b', m['definition']):
                parts += [m['name']] * 2
        return ' '.join(parts)

    def search(self, keywords: list[str], limit: int = 8) -> tuple[list[Hit], bool]:
        """返回 (命中列表, 是否放宽了必含词)。

        第一个关键词的所有分词都必须命中；全部关键词的分词合起来做 OR 查询，用 BM25 排序。
        必含词一个都对不上时退回纯 OR 查询，并告诉调用方结果是放宽过的。
        """
        keywords = [k for k in keywords if k and k.strip()]
        if not keywords:
            return [], False
        must = tokenize(keywords[0])
        every = list(dict.fromkeys(tok for k in keywords for tok in tokenize(k)))
        if not every:
            return [], False
        any_query = ' OR '.join(_quote(t) for t in every)
        ranked = self._query(any_query)
        if must:
            allowed = {name for name, _ in self._query(' AND '.join(_quote(t) for t in must))}
            strict = [(n, s) for n, s in ranked if n in allowed]
            if strict:
                return self._hits(strict, limit), False
        return self._hits(ranked, limit), True

    def _query(self, q: str) -> list[tuple[str, float]]:
        # bm25() 越小越相关，取负数让分数越大越相关
        return [(n, -s) for n, s in self.db.execute('SELECT name, bm25(docs) FROM docs WHERE docs MATCH ? ORDER BY bm25(docs)', (q,))]

    def _hits(self, ranked: list[tuple[str, float]], limit: int) -> list[Hit]:
        return [Hit(n, self.catalog.tables[n].description, round(s, 3)) for n, s in ranked[:limit]]


def _quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'
