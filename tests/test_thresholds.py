from __future__ import annotations

import pytest

from oversight.config import ProcessConfig
from oversight.rules.thresholds import Threshold, stage_threshold, status_age_threshold
from oversight.stats import percentile
from oversight.store.db import Store
from tests.rules_helpers import NOW, add_history, add_issue, derive


def _status(store: Store, status: str = "In Progress", **kwargs: object) -> Threshold | None:
    options: dict[str, object] = {"pct": 90, "min_history": 4, "history_days": 180} | kwargs
    return status_age_threshold(store, "AIL", status, now=NOW, **options)  # type: ignore[arg-type]


def test_fewer_than_min_history_gives_none(store: Store, process: ProcessConfig) -> None:
    add_history(store, "In Progress", [10, 20, 30])
    derive(store, process)

    assert _status(store) is None


def test_exactly_min_history_gives_the_percentile_of_the_hours(store: Store, process: ProcessConfig) -> None:
    hours = [40.0, 10.0, 30.0, 20.0]
    add_history(store, "In Progress", hours)
    derive(store, process)

    found = _status(store)

    assert found is not None
    assert found.hours == pytest.approx(37.0)
    assert found.hours == pytest.approx(percentile(hours, 90))
    assert (found.n, found.pct) == (4, 90)


def test_intervals_older_than_history_days_are_ignored(store: Store, process: ProcessConfig) -> None:
    add_history(store, "In Progress", [10, 20, 30])
    add_history(store, "In Progress", [500], left=-31 * 24)
    derive(store, process)

    assert _status(store, history_days=30) is None
    widened = _status(store, history_days=40)
    assert widened is not None and widened.n == 4
    assert widened.hours == pytest.approx(359.0)


def test_old_intervals_do_not_move_the_percentile(store: Store, process: ProcessConfig) -> None:
    add_history(store, "In Progress", [10, 20, 30, 40])
    add_history(store, "In Progress", [5000], left=-200 * 24)
    derive(store, process)

    found = _status(store)

    assert found is not None
    assert (found.n, found.hours) == (4, pytest.approx(37.0))


def test_status_is_matched_case_insensitively(store: Store, process: ProcessConfig) -> None:
    add_history(store, "In Progress", [10, 20, 30, 40])
    derive(store, process)

    for spelling in ("in progress", "IN PROGRESS", "In Progress"):
        found = _status(store, spelling)
        assert found is not None and found.n == 4, spelling


def test_open_intervals_other_statuses_and_other_projects_do_not_count(
    store: Store, process: ProcessConfig
) -> None:
    add_history(store, "In Progress", [10, 20, 30])
    add_history(store, "To Do", [15, 25])
    add_history(store, "In Progress", [12, 22], project="DEMO")
    add_issue(store, "AIL-1", status="In Progress", created=-100, changes=[(-90, "To Do", "In Progress")])
    derive(store, process, ("AIL", "DEMO"))

    assert _status(store) is None
    found = _status(store, min_history=3)
    assert found is not None and found.n == 3
    to_do = _status(store, "To Do", min_history=3)
    assert to_do is not None and to_do.n == 3


def test_stage_threshold_uses_the_stage_column(store: Store, process: ProcessConfig) -> None:
    add_history(store, "In Progress", [10, 20, 30, 40])
    derive(store, process)

    found = stage_threshold(store, "AIL", "Development", pct=75, min_history=4, history_days=180, now=NOW)

    assert found is not None
    assert (found.hours, found.n, found.pct) == (pytest.approx(32.5), 4, 75)
    assert stage_threshold(store, "AIL", "development", pct=75, min_history=5, history_days=180, now=NOW) is None
    assert stage_threshold(store, "AIL", "backlog", pct=75, min_history=1, history_days=180, now=NOW) is None
