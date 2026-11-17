"""数据字典：表和字段“有什么、是什么意思”。

模型看不到数据库，它对数据的全部了解都来自这里。字典的来源有两个：
- data/dictionary.json：人写的表描述、字段描述、枚举含义、指标口径、可连接关系；
- 数据库本身：字段类型 (从样例值推断日期 / 时间)、行数。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from askdata import config

_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_DATETIME = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')


@dataclass
class Column:
    name: str
    description: str
    sql_type: str  # 数据库声明的类型：INTEGER / REAL / TEXT
    kind: str  # number / text / date / datetime：日期和时间在 SQLite 里都是 TEXT，靠样例值区分


@dataclass
class Join:
    column: str
    table: str
    target: str


@dataclass
class Table:
    name: str
    description: str
    columns: dict[str, Column]
    row_count: int
    joins: list[Join] = field(default_factory=list)
    row_scope: str | None = None  # 行级权限按哪一列控制 (M4)
    sensitive: list[str] = field(default_factory=list)  # 敏感字段 (M4)
    partition: str | None = None  # 分区列 (M4)


@dataclass
class Catalog:
    tables: dict[str, Table]
    enums: dict[str, dict[str, str]]  # "表.列" → {取值: 含义}
    metrics: list[dict[str, str]]

    def table(self, name: str) -> Table | None:
        return self.tables.get(name)

    def neighbors(self, name: str) -> set[str]:
        """一跳可连接的表 (双向)。"""
        out = {j.table for j in self.tables[name].joins}
        out |= {t.name for t in self.tables.values() for j in t.joins if j.table == name}
        return out & set(self.tables)


def _kind(sql_type: str, sample: object) -> str:
    if sql_type in ('INTEGER', 'REAL'):
        return 'number'
    if isinstance(sample, str) and _DATETIME.match(sample):
        return 'datetime'
    if isinstance(sample, str) and _DATE.match(sample):
        return 'date'
    return 'text'


def _load_enums(raw: dict, db: sqlite3.Connection) -> dict[str, dict[str, str]]:
    enums = {}
    for key, spec in raw.items():
        if isinstance(spec, str):  # "@cities.city_id:city_name"：取值和含义在另一张维表里
            m = re.match(r'^@(\w+)\.(\w+):(\w+)$', spec)
            if not m:
                raise ValueError(f'枚举 {key} 的引用格式不对：{spec}')
            t, k, v = m.groups()
            enums[key] = {str(a): str(b) for a, b in db.execute(f'SELECT {k}, {v} FROM {t}')}
        else:
            enums[key] = {str(a): str(b) for a, b in spec.items()}
    return enums


def load(db_path: Path | None = None, dict_path: Path | None = None) -> Catalog:
    db_path = db_path or config.DB_PATH
    dict_path = dict_path or config.DICT_PATH
    if not db_path.exists():
        raise FileNotFoundError(f'找不到数据库 {db_path}，先运行 uv run python data/build_shop.py')
    raw = json.loads(dict_path.read_text(encoding='utf-8'))
    db = sqlite3.connect(db_path)
    try:
        tables = {}
        for t in raw['tables']:
            name = t['name']
            info = {r[1]: r[2].upper() for r in db.execute(f'PRAGMA table_info({name})')}
            if set(info) != set(t['columns']):
                raise ValueError(f'表 {name} 的字典和数据库字段对不上：{sorted(set(info) ^ set(t["columns"]))}')
            sample = db.execute(f'SELECT * FROM {name} LIMIT 1').fetchone() or [None] * len(info)
            columns = {
                col: Column(col, t['columns'][col], typ, _kind(typ, val)) for (col, typ), val in zip(info.items(), sample, strict=True)
            }
            tables[name] = Table(
                name=name,
                description=t['description'],
                columns=columns,
                row_count=db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0],
                joins=[Join(**j) for j in t.get('joins', [])],
                row_scope=t.get('row_scope'),
                sensitive=t.get('sensitive', []),
                partition=t.get('partition'),
            )
        return Catalog(tables=tables, enums=_load_enums(raw.get('enums', {}), db), metrics=raw.get('metrics', []))
    finally:
        db.close()
