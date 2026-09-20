"""观测口径的公共实现。

分位数只有一处实现：阶段耗时、模型调用耗时、将来的任何延迟指标都用它——
两份实现分叉的话，"P95"在两个面板上会是两个意思。
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（与 numpy.percentile 的默认口径一致）。

    样本很少时 P95 会接近最大值，这是**如实**的：三个样本本来就估不出 P95，
    与其假装精确，不如让读者看到它不稳。
    """
    if not values:
        return 0.0
    if not 0 <= q <= 1:
        raise ValueError("q 必须在 [0, 1] 之间")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[int(position)], 3)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def summarize(values: Sequence[float]) -> dict[str, float]:
    """一组耗时样本的常用摘要。"""
    if not values:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "max": round(max(float(value) for value in values), 3),
    }
