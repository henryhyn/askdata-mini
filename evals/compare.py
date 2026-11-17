"""判分：比较执行结果，不比较 SQL 文本。

同一个结果有很多种写法 (连表还是子查询、列的顺序、多输出几列)，比 SQL 文本会把对的判成错的。
规则：
- 行数必须相同，行的顺序不管 (排名类问题靠 LIMIT 限定了是哪几行，顺序不影响对错)；
- 标准答案的每一列，都要在预测结果里找到一列对应 (列名不管，允许预测多出几列)；
- 数值保留两位小数比较，容忍浮点误差。
"""

from __future__ import annotations

from collections import Counter
from itertools import product


def norm(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return round(float(v) + 0.0, 2)
    if isinstance(v, str):
        s = v.strip()
        try:
            return round(float(s), 2)  # "6.0" 和 6.0 算同一个值
        except ValueError:
            return s
    return v


def same_result(gold: list[list], pred: list[list]) -> bool:
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    g = [[norm(v) for v in r] for r in gold]
    p = [[norm(v) for v in r] for r in pred]
    g_cols = list(zip(*g, strict=True))
    p_cols = list(zip(*p, strict=True))
    # 每个标准列可能对应哪些预测列：值的多重集合相同
    options = []
    for gc in g_cols:
        cands = [k for k, pc in enumerate(p_cols) if Counter(pc) == Counter(gc)]
        if not cands:
            return False
        options.append(cands)
    target = Counter(map(tuple, g))
    for pick in product(*options):
        if len(set(pick)) < len(pick):
            continue
        if Counter(tuple(r[k] for k in pick) for r in p) == target:
            return True
    return False
