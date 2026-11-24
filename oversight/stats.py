"""Small, dependency-free statistics used by metrics, baselines and thresholds."""
from __future__ import annotations

import math
from collections.abc import Iterable


def percentile(values: Iterable[float], pct: float) -> float | None:
    """Linear-interpolated percentile (numpy's default method)."""
    data = sorted(v for v in values if v is not None and not math.isnan(v))
    if not data:
        return None
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    rank = (len(data) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(data[low])
    return data[low] + (data[high] - data[low]) * (rank - low)


def median(values: Iterable[float]) -> float | None:
    return percentile(values, 50)


def share(part: int, whole: int) -> float | None:
    """part / whole as a fraction, or None when whole is 0."""
    return part / whole if whole else None
