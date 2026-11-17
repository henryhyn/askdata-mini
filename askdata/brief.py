"""把数据字典写成给模型看的文字。

分两级：检索结果只给表级摘要 (表名、描述)，选中之后才给字段。几百张表的字段一次全塞给模型，
token 会爆，模型也会被不相关的字段干扰。
"""

from __future__ import annotations

from askdata.catalog import Catalog

KIND = {'number': '数值', 'text': '文本', 'date': '日期 YYYY-MM-DD', 'datetime': '时间 YYYY-MM-DD HH:MM:SS'}


def columns_brief(catalog: Catalog, tables: list[str], probe: bool = False) -> str:
    """表的字段说明，外加这些表之间可以怎么连接。probe=True 时标出哪些列可以用 probe_column_values 查取值 (M3)。"""
    out = []
    for name in tables:
        t = catalog.tables[name]
        out.append(f'## {t.name} ({t.row_count} 行)\n{t.description}')
        if t.partition:
            out.append(f'按 {t.partition} 分区：查询必须带 {t.partition} 的过滤条件')
        for c in t.columns.values():
            mark = ''
            if c.name in t.sensitive:
                mark = ' (敏感字段，不能查询)'
            elif probe and (c.values is not None or f'{name}.{c.name}' in catalog.enums):
                mark = ' (过滤前用 probe_column_values 确认取值)'
            out.append(f'- {c.name} [{KIND[c.kind]}]：{c.description}{mark}')
        joins = [f'{t.name}.{j.column} = {j.table}.{j.target}' for j in t.joins if j.table in tables]
        if joins:
            out.append('可连接：' + '；'.join(joins))
        out.append('')
    return '\n'.join(out)


def metrics_brief(catalog: Catalog) -> str:
    return '\n'.join(f'- {m["name"]}：{m["definition"]}' for m in catalog.metrics)
