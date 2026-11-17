"""把数据字典写成给模型看的文字。

分两级：检索结果只给表级摘要 (表名、描述)，选中之后才给字段。几百张表的字段一次全塞给模型，
token 会爆，模型也会被不相关的字段干扰。
"""

from __future__ import annotations

from askdata.catalog import Catalog

KIND = {'number': '数值', 'text': '文本', 'date': '日期 YYYY-MM-DD', 'datetime': '时间 YYYY-MM-DD HH:MM:SS'}


def columns_brief(catalog: Catalog, tables: list[str]) -> str:
    """表的字段说明，外加这些表之间可以怎么连接。"""
    out = []
    for name in tables:
        t = catalog.tables[name]
        out.append(f'## {t.name} ({t.row_count} 行)\n{t.description}')
        for c in t.columns.values():
            out.append(f'- {c.name} [{KIND[c.kind]}]：{c.description}')
        joins = [f'{t.name}.{j.column} = {j.table}.{j.target}' for j in t.joins if j.table in tables]
        if joins:
            out.append('可连接：' + '；'.join(joins))
        out.append('')
    return '\n'.join(out)


def metrics_brief(catalog: Catalog) -> str:
    return '\n'.join(f'- {m["name"]}：{m["definition"]}' for m in catalog.metrics)
