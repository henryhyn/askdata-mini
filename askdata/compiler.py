"""把 IR 编译成 SQLite 的 SQL，护栏都在这里。

编译失败抛 IRError，错误信息是写给模型看的：说清楚哪里错了、可以怎么改。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from askdata.catalog import Catalog, Column
from askdata.ir import Filter, QueryIR

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000  # 给了也不能超过：防止被诱导拉全表
MAX_JOINS = 3

_DATE_ONLY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class IRError(Exception):
    pass


@dataclass
class Policy:
    """本次查询的访问控制。敏感字段、分区约束是数据字典里的规则，对所有人生效；这里放因人、因轮而异的部分。"""

    candidates: set[str] | None = None  # 只能查这些表；None 表示不限制
    regions: list[str] | None = None  # 行级权限：只能看这些大区的数据；None 表示不限制


@dataclass
class Compiled:
    sql: str
    params: list = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    limit: int = DEFAULT_LIMIT


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _next_day(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


class _Compiler:
    def __init__(self, ir: QueryIR, catalog: Catalog, policy: Policy):
        self.ir, self.catalog, self.policy = ir, catalog, policy
        self.params: list = []
        self.aliases: dict[str, str] = {}

    # ── 表和列 ──────────────────────────────────────────────

    def add_table(self, table: str, alias: str) -> None:
        if table not in self.catalog.tables:
            raise IRError(f'表 {table} 不存在。先用检索找到表，再看它的字段')
        if self.policy.candidates is not None and table not in self.policy.candidates:
            raise IRError(f'表 {table} 不在可查询的范围内，只能查看过字段的表：{sorted(self.policy.candidates)}')
        if alias in self.aliases:
            raise IRError(f'别名 {alias} 重复了，每张表要用不同的别名')
        self.aliases[alias] = table

    def resolve(self, ref: str) -> tuple[str, str, Column]:
        if '.' in ref:
            alias, col = ref.split('.', 1)
            if alias not in self.aliases:
                raise IRError(f'列引用 {ref} 里的别名 {alias} 不存在，可用的别名：{sorted(self.aliases)}')
            owners = [alias]
        else:
            col = ref
            owners = [a for a, t in self.aliases.items() if col in self.catalog.tables[t].columns]
            if len(owners) > 1:
                raise IRError(f'列 {col} 在多张表里都有，写成“别名.列名”：' + '、'.join(f'{a}.{col}' for a in owners))
            if not owners:
                hint = '；'.join(f'{t} 的列：{", ".join(self.catalog.tables[t].columns)}' for t in self.aliases.values())
                raise IRError(f'列 {col} 不存在。{hint}')
        alias = owners[0]
        table = self.catalog.tables[self.aliases[alias]]
        if col not in table.columns:
            raise IRError(f'表 {table.name} 没有列 {col}，它的列：{", ".join(table.columns)}')
        if col in table.sensitive:
            raise IRError(f'{table.name}.{col} 是敏感字段，不能查询。如实告诉用户没有权限，不要换个方式去取')
        return alias, col, table.columns[col]

    def col_sql(self, ref: str) -> tuple[str, Column]:
        alias, col, column = self.resolve(ref)
        return f'{alias}.{_q(col)}', column

    # ── 各子句 ──────────────────────────────────────────────

    def compile(self) -> Compiled:
        ir = self.ir
        self.add_table(ir.table, ir.alias)
        if len(ir.joins) > MAX_JOINS:
            raise IRError(f'最多连接 {MAX_JOINS} 张表')
        joins = []
        for j in ir.joins:
            self.add_table(j.table, j.alias)
            left, _ = self.col_sql(j.left)
            right, _ = self.col_sql(j.right)
            joins.append(f'{"LEFT JOIN" if j.kind == "left" else "JOIN"} {_q(j.table)} {j.alias} ON {left} = {right}')

        select, labels, group_exprs, plain_cols = [], [], [], set()
        has_agg = any(s.agg for s in ir.select)
        for s in ir.select:
            expr, label = self.select_item(s)
            if s.bucket:
                group_exprs.append(expr)
            elif not s.agg:
                plain_cols.add(self.resolve(s.column)[:2])
            if label in labels:
                raise IRError(f'输出列名 {label} 重复了，用 as 起不同的名字')
            labels.append(label)
            select.append(f'{expr} AS {_q(label)}')

        grouped = set()
        for g in ir.group_by:
            expr, _ = self.col_sql(g)
            group_exprs.append(expr)
            grouped.add(self.resolve(g)[:2])
        if has_agg and plain_cols - grouped:
            # SQLite 允许这样写，但会随便挑一行的值，结果是错的
            missing = ', '.join(f'{a}.{c}' for a, c in sorted(plain_cols - grouped))
            raise IRError(f'有聚合时，select 里的普通列必须写进 group_by：{missing}')

        where = [self.filter(f) for f in ir.where]
        self.check_partitions(ir)
        where += self.row_scope()
        having = []
        for h in ir.having:
            expr = self.agg_expr(h.agg, h.column)
            having.append(f'{expr} {h.op} ?')
            self.params.append(h.value)

        order = []
        for o in ir.order_by:
            if o.ref in labels:
                expr = _q(o.ref)
            else:
                expr, _ = self.col_sql(o.ref)
            order.append(expr + (' DESC' if o.desc else ''))

        limit = DEFAULT_LIMIT if ir.limit is None else min(ir.limit, MAX_LIMIT)
        sql = f'SELECT {", ".join(select)}\nFROM {_q(ir.table)} {ir.alias}'
        sql += ''.join(f'\n{j}' for j in joins)
        if where:
            sql += '\nWHERE ' + '\n  AND '.join(where)
        if group_exprs:
            sql += '\nGROUP BY ' + ', '.join(group_exprs)
        if having:
            sql += '\nHAVING ' + ' AND '.join(having)
        if order:
            sql += '\nORDER BY ' + ', '.join(order)
        sql += f'\nLIMIT {limit}'
        return Compiled(sql=sql, params=self.params, labels=labels, tables=ir.tables(), limit=limit)

    def check_partitions(self, ir: QueryIR) -> None:
        """分区大表必须按分区列过滤，防止一次查询扫遍全部历史数据。"""
        filtered = {self.resolve(f.column)[:2] for f in ir.where}
        for alias, name in self.aliases.items():
            p = self.catalog.tables[name].partition
            if p and (alias, p) not in filtered:
                raise IRError(f'{name} 是按 {p} 分区的大表，where 里必须有 {alias}.{p} 的过滤条件 (如某一天或某个日期范围)')

    def row_scope(self) -> list[str]:
        """行级权限：按 Policy.regions 给每张受控的表注入过滤条件。条件由代码加，不出现在 IR 里，模型改不了。

        注意：只有带大区字段的表受控。order_items 这类不带大区的表单独查时不受限制，
        真实系统里行级权限应该在数据库层 (视图、行级安全策略、按人分配的只读账号) 强制，编译器里注入只是演示。"""
        regions = self.policy.regions
        if regions is None:
            return []
        if not regions:
            raise IRError('当前用户没有任何大区的数据权限')
        out = []
        for alias, name in self.aliases.items():
            col = self.catalog.tables[name].row_scope
            if col:
                self.params += regions
                out.append(f'{alias}.{_q(col)} IN ({", ".join("?" * len(regions))})')
        return out

    def agg_expr(self, agg: str, column: str | None) -> str:
        if column is None:
            if agg != 'count':
                raise IRError(f'{agg} 需要填 column')
            return 'COUNT(*)'
        expr, col = self.col_sql(column)
        if agg in ('sum', 'avg') and col.kind != 'number':
            raise IRError(f'{agg} 只能用在数值列上，{column} 不是数值列')
        if agg == 'count_distinct':
            return f'COUNT(DISTINCT {expr})'
        return f'{agg.upper()}({expr})'

    def select_item(self, s) -> tuple[str, str]:
        if s.bucket:
            if s.agg or not s.column:
                raise IRError('日期分桶要填 column，且不能和 agg 同时用')
            expr, col = self.col_sql(s.column)
            if col.kind not in ('date', 'datetime'):
                raise IRError(f'{s.column} 不是日期或时间列，不能分桶')
            expr = {'day': f'date({expr})', 'week': f"date({expr}, 'weekday 0', '-6 days')", 'month': f"strftime('%Y-%m-01', {expr})"}[s.bucket]
            return expr, s.as_ or f'{s.column}_{s.bucket}'
        if s.agg:
            expr = self.agg_expr(s.agg, s.column)
            return expr, s.as_ or (f'{s.agg}({s.column})' if s.column else 'count(*)')
        if not s.column:
            raise IRError('select 的每一项要么填 column，要么填 agg')
        expr, _ = self.col_sql(s.column)
        return expr, s.as_ or s.column.split('.')[-1]

    def filter(self, f: Filter) -> str:
        expr, col = self.col_sql(f.column)
        v = f.value
        if f.op in ('is_null', 'is_not_null'):
            return f'{expr} IS {"NOT " if f.op == "is_not_null" else ""}NULL'
        if f.op in ('in', 'not_in'):
            if not isinstance(v, list) or not v:
                raise IRError(f'{f.column} 的 {f.op} 要给非空数组')
        elif f.op == 'between':
            if not isinstance(v, list) or len(v) != 2:
                raise IRError(f'{f.column} 的 between 要给 [起, 止] 两个元素')
        elif isinstance(v, list) or v is None:
            raise IRError(f'{f.column} 的 {f.op} 要给单个值')

        # 时间列 (YYYY-MM-DD HH:MM:SS) 拿日期 (YYYY-MM-DD) 比较时，上界要换成“小于下一天”，
        # 否则 order_time <= '2026-06-28' 会漏掉 28 号当天 0 点以后的所有订单。这是问数里很常见的静默错误。
        if col.kind == 'datetime':
            return self.datetime_filter(expr, f.op, v)
        return self.plain_filter(expr, f.op, v)

    def plain_filter(self, expr: str, op: str, v) -> str:
        if op in ('in', 'not_in'):
            self.params += v
            return f'{expr} {"NOT IN" if op == "not_in" else "IN"} ({", ".join("?" * len(v))})'
        if op == 'between':
            self.params += v
            return f'{expr} BETWEEN ? AND ?'
        self.params.append(v)
        return f'{expr} {"LIKE" if op == "like" else op} ?'

    def datetime_filter(self, expr: str, op: str, v) -> str:
        def day(x) -> bool:
            return isinstance(x, str) and bool(_DATE_ONLY.match(x))

        if op == 'between' and day(v[1]):
            self.params += [v[0], _next_day(v[1])]
            return f'{expr} >= ? AND {expr} < ?'
        if op in ('<=', '>') and day(v):
            self.params.append(_next_day(v))
            return f'{expr} {"<" if op == "<=" else ">="} ?'
        if op in ('=', '!=') and day(v):
            self.params.append(v)
            return f'date({expr}) {op} ?'
        if op in ('in', 'not_in') and all(day(x) for x in v):
            return self.plain_filter(f'date({expr})', op, v)
        return self.plain_filter(expr, op, v)


def compile_ir(ir: QueryIR, catalog: Catalog, policy: Policy | None = None) -> Compiled:
    return _Compiler(ir, catalog, policy or Policy()).compile()
