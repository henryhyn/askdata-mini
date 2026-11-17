"""执行查询：校验 IR → 编译 → 用只读连接执行。

流水线和 Agent 共用这一份，护栏只写一次。任何一步失败都返回 QueryResult(error=...)，
不抛异常：错误要交回给模型改，而不是让整次提问崩掉。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from askdata import config
from askdata.catalog import Catalog
from askdata.compiler import IRError, Policy, compile_ir
from askdata.ir import QueryIR

TIMEOUT_SEC = 10


@dataclass
class QueryResult:
    ir: dict | None = None
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def validation_message(e: ValidationError) -> str:
    """Pydantic 的校验错误 → 一行带字段路径的说明。没有路径的 “Field required” 模型不知道该改哪里。"""
    lines = []
    for err in e.errors()[:3]:
        loc = '.'.join(str(x) for x in err['loc'])
        lines.append(f'{loc}: {err["msg"]}' if loc else err['msg'])
    return 'IR 结构不合法：' + '；'.join(lines)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    # mode=ro：数据库层面只读。IR 本来就写不出修改语句，这是第二道保险
    return sqlite3.connect(f'file:{db_path or config.DB_PATH}?mode=ro', uri=True, check_same_thread=False)


def run_ir(raw: dict | QueryIR, catalog: Catalog, policy: Policy | None = None, db_path: Path | None = None) -> QueryResult:
    raw_dict = raw.model_dump(by_alias=True, exclude_defaults=True) if isinstance(raw, QueryIR) else raw
    try:
        ir = raw if isinstance(raw, QueryIR) else QueryIR.model_validate(raw)
    except ValidationError as e:
        return QueryResult(ir=raw_dict, error=validation_message(e))
    try:
        compiled = compile_ir(ir, catalog, policy)
    except IRError as e:
        return QueryResult(ir=raw_dict, error=str(e))

    result = QueryResult(ir=raw_dict, sql=render_sql(compiled.sql, compiled.params), columns=compiled.labels, tables=compiled.tables)
    conn = connect(db_path)
    deadline = time.monotonic() + TIMEOUT_SEC
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)  # 返回非 0 即中断查询
    try:
        rows = conn.execute(compiled.sql, compiled.params).fetchmany(compiled.limit)
        result.rows = [list(r) for r in rows]
    except sqlite3.OperationalError as e:
        result.error = f'查询超过 {TIMEOUT_SEC} 秒被中断，加过滤条件缩小范围' if 'interrupted' in str(e) else f'执行出错：{e}'
    finally:
        conn.close()
    return result


def render_sql(sql: str, params: list) -> str:
    """把参数代入 SQL，只用于展示给人看。"""
    out = sql
    for p in params:
        out = out.replace('?', repr(p) if isinstance(p, str) else str(p), 1)
    return out
