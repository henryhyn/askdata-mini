"""一次提问的结果。流水线和 Agent 都返回它，命令行和评测只认它。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Answer:
    kind: str  # result：取到了数；reply：没有取数的文字回复 (答疑、澄清、拒绝、没有数据)；error：失败
    text: str = ''
    ir: dict | None = None
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    attempts: int = 0  # 执行了几次查询 (含失败)
    errors: list[str] = field(default_factory=list)  # 每次失败的原因，排查用
