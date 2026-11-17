"""查询中间表示 (IR)：模型写 IR，代码把它编译成 SQL。

为什么不让模型直接写 SQL：护栏要在结构上做，而不是在字符串上做。IR 里只能表达
“从哪张表、取哪些列、怎么过滤、怎么分组”，删表、改数据、子查询里藏东西这类操作根本写不出来；
行数上限、候选表限制、行级权限都由编译器强制 (见 compiler.py)。

两条约定：
1. 所有模型都 extra='forbid'。默认的 extra='ignore' 会把拼错的键 (如把 where 写成 filters) 静默丢掉，
   过滤条件整条消失，查询照样能跑，结果却错了。宁可报错让模型改。
2. 字段的填写规则写在 Field(description=...) 和类的 docstring 里，它们会进 JSON Schema，每次请求都发给模型；
   写在 # 注释里的东西模型永远看不到。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra='forbid')
_STRICT_ALIAS = ConfigDict(extra='forbid', populate_by_name=True)

Scalar = str | int | float | bool | None
Agg = Literal['sum', 'avg', 'count', 'count_distinct', 'min', 'max']
FilterOp = Literal['=', '!=', '>', '>=', '<', '<=', 'in', 'not_in', 'between', 'like', 'is_null', 'is_not_null']

COLUMN_REF = '列引用：只有一张表时直接写列名；有连接时写成“别名.列名”，如 o.pay_amount。'


class Filter(BaseModel):
    """一个过滤条件。多个条件之间是 AND。"""

    model_config = _STRICT

    column: str = Field(description=COLUMN_REF)
    op: FilterOp
    value: Scalar | list[Scalar] = Field(
        default=None,
        description='按算子给形状：in / not_in 给数组；between 给 [起, 止] 两个元素 (含两端)；is_null / is_not_null 不给；其余给单个值。'
        '编码字段填编码本身 (如 status 填 5)，不要填中文含义。'
        '时间字段 (datetime) 可以直接用日期比较，如 between ["2026-06-01", "2026-06-30"] 表示 6 月 1 日 0 点到 6 月 30 日 24 点。',
    )


class SelectItem(BaseModel):
    """一个输出列，三选一：
    - 普通列：只填 column；
    - 聚合：填 agg，除 count 外都要填 column (count 不填 column 表示 count(*))；
    - 日期分桶：填 column 和 bucket，把日期截到天 / 周 (周一) / 月 (1 日)，作为分组维度，不能和 agg 同时用。"""

    model_config = _STRICT_ALIAS

    column: str | None = Field(default=None, description=COLUMN_REF)
    agg: Agg | None = None
    bucket: Literal['day', 'week', 'month'] | None = None
    as_: str | None = Field(default=None, alias='as', description='输出列名，建议用中文，如“订单数”。order_by 用它引用这一列。')


class Join(BaseModel):
    """连接一张表。on 的两边都写“别名.列名”。"""

    model_config = _STRICT

    table: str
    alias: str = Field(pattern=r'^[A-Za-z_]\w*$', description='这张表的别名，如 u')
    left: str = Field(description='已有表的列，如 o.user_id')
    right: str = Field(description='本表的列，如 u.user_id')
    kind: Literal['inner', 'left'] = 'inner'


class Having(BaseModel):
    """对分组后的聚合值过滤。"""

    model_config = _STRICT

    agg: Agg
    column: str | None = Field(default=None, description='count 可不填')
    op: Literal['=', '!=', '>', '>=', '<', '<=']
    value: int | float


class OrderBy(BaseModel):
    model_config = _STRICT

    ref: str = Field(description='select 里某一列的 as 名，或分组列的列引用')
    desc: bool = False


class QueryIR(BaseModel):
    """一次查询。只能读一张主表加若干连接表，不支持子查询、窗口函数、UNION。
    有聚合时，select 里的普通列都必须出现在 group_by 里 (日期分桶列自动参与分组，不用写进 group_by)。"""

    model_config = _STRICT

    table: str = Field(description='主表名')
    alias: str = Field(default='t', pattern=r'^[A-Za-z_]\w*$', description='主表别名，有连接时用来写“别名.列名”')
    joins: list[Join] = Field(default_factory=list)
    select: list[SelectItem] = Field(min_length=1)
    where: list[Filter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list, description='分组的普通列 (列引用)')
    having: list[Having] = Field(default_factory=list)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, description='返回行数，不填默认 200，最多 1000')

    def tables(self) -> list[str]:
        return [self.table] + [j.table for j in self.joins]
