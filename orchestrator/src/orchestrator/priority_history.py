"""历史快题先验（run-35236 + run-35434 平台事件表的 median 首次 start→得分）。

只用于调度排序，不含题解/flag；官方题集固定时 `unique_code` 稳定，
因此按 code 的历史耗时排序比平台 difficulty 标签更接近真实成本。
未出现过的 code 用类别先验（e1/e2/e3/d 先跑，a/c 后跑）。
"""

from __future__ import annotations

# median seconds from first start to score across the two runs
HISTORY_SECONDS: dict[str, int] = {
    "e3-03": 30,
    "d-02": 31,
    "d-01": 37,
    "e1-01": 39,
    "e1-05": 40,
    "e1-02": 45,
    "a-11": 49,
    "f1-04": 51,
    "e2-01": 51,
    "e3-01": 54,
    "a-10": 67,
    "a-02": 73,
    "a-01": 78,
    "a-04": 108,
    "a-09": 110,
    "a-08": 114,
    "f1-03": 119,
    "b-03": 120,
    "a-12": 125,
    "a-06": 131,
    "a-14": 136,
    "f1-02": 154,
    "c-07": 189,
    "c-08": 208,
    "d-06": 232,
    "f1-01": 245,
    "c-05": 268,
    "f2-08": 297,
    "c-04": 346,
    "c-06": 803,
    "a-17": 1105,
    "c-09": 1128,
    "a-07": 1376,
    "d-04": 1392,
    "b-01": 1462,
    "c-03": 2806,
    "a-05": 4896,
}

# 未在历史表里的 code：按类别给一个保守先验（越小越先跑）
CATEGORY_PRIOR: dict[str, int] = {
    "e1": 50,
    "e2": 60,
    "e3": 50,
    "d": 60,
    "f1": 150,
    "f2": 300,
    "b": 300,
    "a": 400,
    "c": 800,
}
DEFAULT_PRIOR = 1000


def expected_seconds(code: str) -> int:
    known = HISTORY_SECONDS.get(code)
    if known is not None:
        return known
    prefix = code.split("-", 1)[0].lower()
    return CATEGORY_PRIOR.get(prefix, DEFAULT_PRIOR)
