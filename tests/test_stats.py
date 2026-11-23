from __future__ import annotations

import pytest

from oversight.stats import median, percentile, share


def test_percentile_interpolates_like_numpy() -> None:
    data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(data, 50) == 5.5
    assert percentile(data, 90) == pytest.approx(9.1)
    assert percentile(data, 0) == 1 and percentile(data, 100) == 10


def test_percentile_edge_cases() -> None:
    assert percentile([], 50) is None
    assert percentile([None, float("nan")], 50) is None
    assert median([7]) == 7
    with pytest.raises(ValueError):
        percentile([1], 101)


def test_share() -> None:
    assert share(1, 4) == 0.25
    assert share(1, 0) is None
