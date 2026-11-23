"""Hand-built derive fixtures and their hand-computed labels, shared by the derive tests.

- ail_issues.yaml: AIL issues as `sync` stores them (issues, status_changes,
  field_changes and statuses rows).
- ops_issues.yaml: OPS, a made-up hours-basis project; OPS_PROJECT is its
  process.yaml entry, which with_ops() adds to the AIL ProcessConfig.
- tests/labels/metrics.yaml: the expected intervals and metrics, by hand.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from oversight.config import ProcessConfig
from oversight.derive.run import _category_lookup, _status_categories
from oversight.store.db import Store

HERE = Path(__file__).parent
LABELS_FILE = HERE.parent.parent / "labels" / "metrics.yaml"

# OPS: estimates in hours, and a workflow that maps five of James's stages.
OPS_PROJECT: dict[str, Any] = {
    "status_to_stage": {
        "Open": "backlog",
        "In Development": "development",
        "Code Review": "code_review",
        "Testing": "testing",
        "Closed": "done",
    },
    "estimate": {"basis": "hours", "field": "original_estimate"},
}

INTERVAL_KEYS = ("status", "stage", "entered_at", "left_at")
FLOAT_METRICS = ("cycle_time_h", "effort_ratio", "status_age_h")
# Calendar hours to within 3.6 ms: tighter than any hand-computed label needs.
HOURS_TOLERANCE = 1e-6


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


_AIL = _load(HERE / "ail_issues.yaml")
_OPS = _load(HERE / "ops_issues.yaml")

LABELS: dict[str, Any] = _load(LABELS_FILE)
NOW: datetime = datetime.fromisoformat(LABELS["now"])
STATUSES: list[dict[str, Any]] = _AIL.get("statuses", []) + _OPS.get("statuses", [])
CASES: dict[str, dict[str, Any]] = {
    case["issue"]["issue_key"]: case for case in _AIL["cases"] + _OPS["cases"]
}


def with_ops(process: ProcessConfig) -> ProcessConfig:
    """The AIL process plus the OPS project."""
    data = process.model_dump()
    data["projects"]["OPS"] = OPS_PROJECT
    return ProcessConfig.model_validate(data)


def category_of_for(issue: Mapping[str, Any], process: ProcessConfig) -> Callable[[str | None], str | None]:
    """The category lookup run_derive builds for this issue from the fixture `statuses` rows."""
    project = issue["project_key"]
    rows = [row for row in STATUSES if row["project_key"] == project]
    return _category_lookup(_status_categories(rows), process.project(project), process.done_stage, issue)


def load_into(store: Store) -> None:
    """Insert every fixture row with Store.upsert, as a sync would."""
    with store.transaction():
        store.upsert_many("statuses", STATUSES)
        for case in CASES.values():
            store.upsert("issues", case["issue"])
            store.upsert_many("status_changes", case.get("status_changes", []))
            store.upsert_many("field_changes", case.get("field_changes", []))


def assert_intervals(actual: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]) -> None:
    """Interval rows equal the labels: text exactly, hours to HOURS_TOLERANCE."""
    assert [{k: row[k] for k in INTERVAL_KEYS} for row in actual] == [
        {k: row[k] for k in INTERVAL_KEYS} for row in expected
    ]
    for got, want in zip(actual, expected, strict=True):
        if want["hours"] is None:
            assert got["hours"] is None, got
        else:
            assert got["hours"] == pytest.approx(want["hours"], abs=HOURS_TOLERANCE), got


def assert_metrics(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """A metrics row equals its label: floats to HOURS_TOLERANCE, everything else exactly."""
    exact = {name: value for name, value in expected.items() if name not in FLOAT_METRICS}
    assert {name: actual[name] for name in exact} == exact
    for name in FLOAT_METRICS:
        want = expected[name]
        if want is None:
            assert actual[name] is None, name
        else:
            assert actual[name] == pytest.approx(want, abs=HOURS_TOLERANCE), name
