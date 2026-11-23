from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from oversight.config import EstimateConfig, ProcessConfig
from oversight.derive.metrics import (
    compute_metrics,
    effort_ratio,
    estimate_set_at,
    has_estimate_value,
    reopen_count,
    skipped_stages,
)
from oversight.derive.timeline import Interval, build_intervals
from oversight.errors import ConfigError
from oversight.store.db import Store
from oversight.timeutil import UTC

from tests.fixtures.derive import CASES, LABELS, NOW, assert_metrics, category_of_for, with_ops

POINTS = EstimateConfig(basis="points", field="story_points", hours_per_point=8)
HOURS = EstimateConfig(basis="hours", field="original_estimate")
CATEGORIES = {"to do": "new", "in progress": "indeterminate", "done": "done", "closed": "done"}


def category_of(name: str | None) -> str | None:
    return CATEGORIES.get(name.casefold()) if name else None


def _change(
    changelog_id: str, from_status: str | None, to_status: str | None, at: str = "2026-10-01T10:00:00.000Z"
) -> dict:
    return {"changelog_id": changelog_id, "changed_at": at, "from_status": from_status, "to_status": to_status}


def _field_change(
    changelog_id: str, at: str, from_value: str | None, to_value: str | None, field: str = "story_points"
) -> dict:
    return {
        "changelog_id": changelog_id,
        "field": field,
        "changed_at": at,
        "from_value": from_value,
        "to_value": to_value,
    }


def _metrics(key: str, process: ProcessConfig, now: datetime = NOW) -> dict:
    process = with_ops(process)
    case = CASES[key]
    issue = case["issue"]
    changes = case.get("status_changes", [])
    intervals = build_intervals(
        issue["created_at"], issue["status"], changes, process.project(issue["project_key"]).stage_of
    )
    return compute_metrics(
        issue, intervals, changes, case.get("field_changes", []), process, category_of_for(issue, process), now
    )


# --- fixtures against the hand-computed labels ---------------------------------


@pytest.mark.parametrize("key", sorted(CASES))
def test_metrics_match_the_hand_computed_labels(key: str, process: ProcessConfig) -> None:
    metrics = _metrics(key, process)
    assert metrics["issue_key"] == key
    assert metrics["computed_at"] == LABELS["now"]
    assert_metrics(metrics, LABELS["issues"][key]["metrics"])


def test_metrics_rows_have_exactly_the_issue_metrics_columns(store: Store, process: ProcessConfig) -> None:
    columns = [row["name"] for row in store.query("PRAGMA table_info(issue_metrics)")]
    assert list(_metrics("AIL-101", process)) == columns


# --- has_estimate_value --------------------------------------------------------


@pytest.mark.parametrize("value", [1, 0.5, 3.0, "0.5", "3", " 2 ", "1e1", -1])
def test_numbers_other_than_zero_are_estimates(value: object) -> None:
    # The contract only rules out zero; a negative estimate still counts as one.
    assert has_estimate_value(value)


@pytest.mark.parametrize(
    "value", [None, "", " ", "0", "0.0", 0, 0.0, "abc", "4400, 4427", True, False, float("nan"), "inf", [1]]
)
def test_empty_zero_and_non_numbers_are_no_estimate(value: object) -> None:
    assert not has_estimate_value(value)


# --- skipped_stages --------------------------------------------------------------


def test_todo_to_done_skips_development_in_ail(process: ProcessConfig) -> None:
    skips = skipped_stages("AIL", [_change("5", "To Do", "Done")], process)
    assert skips == [{"changelog_id": "5", "from": "To Do", "to": "Done", "skipped": ["development"]}]


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("To Do", "In Progress"),  # one step forward
        ("In Progress", "Done"),
        ("Done", "To Do"),  # backwards
        ("Done", "In Progress"),
        ("In Progress", "In Progress"),  # no move
        ("To Do", "In Review"),  # unmapped target
        ("In Review", "Done"),  # unmapped source
        (None, "Done"),
    ],
)
def test_steps_backwards_and_unmapped_moves_are_not_skips(
    process: ProcessConfig, from_status: str | None, to_status: str | None
) -> None:
    assert skipped_stages("AIL", [_change("5", from_status, to_status)], process) == []


def test_skips_are_judged_on_the_change_as_recorded(process: ProcessConfig) -> None:
    # Change 2 says To Do -> Done although the issue was In Progress. It counts,
    # as JQL `status CHANGED FROM "To Do" TO "Done"` would count it.
    changes = [_change("1", "To Do", "In Progress"), _change("2", "To Do", "Done", "2026-10-02T10:00:00.000Z")]
    assert [skip["changelog_id"] for skip in skipped_stages("AIL", changes, process)] == ["2"]


def test_skips_use_the_projects_own_stage_order(process: ProcessConfig) -> None:
    ops = with_ops(process)
    changes = [
        _change("3", "Code Review", "Closed", "2026-10-03T10:00:00.000Z"),
        _change("1", "open", "TESTING", "2026-10-01T10:00:00.000Z"),  # names match ignoring case
        _change("2", "Testing", "In Development", "2026-10-02T10:00:00.000Z"),
    ]
    assert skipped_stages("OPS", changes, ops) == [
        {"changelog_id": "1", "from": "open", "to": "TESTING", "skipped": ["development", "code_review"]},
        {"changelog_id": "3", "from": "Code Review", "to": "Closed", "skipped": ["testing"]},
    ]


def test_skipped_stages_needs_a_configured_project(process: ProcessConfig) -> None:
    with pytest.raises(ConfigError, match="XYZ"):
        skipped_stages("XYZ", [], process)


# --- reopen_count ---------------------------------------------------------------------


def test_reopens_count_moves_out_of_done() -> None:
    changes = [
        _change("1", "To Do", "Done"),
        _change("2", "Done", "In Progress"),  # reopen
        _change("3", "In Progress", "Done"),
        _change("4", "Done", "Closed"),  # done -> done: not a reopen
        _change("5", "Closed", "To Do"),  # reopen
        _change("6", "To Do", "In Review"),  # unknown category, but not from done
    ]
    assert reopen_count(changes, category_of) == 2


def test_a_move_from_done_to_an_unknown_status_is_a_reopen() -> None:
    assert reopen_count([_change("1", "Done", "Mystery")], category_of) == 1
    assert reopen_count([_change("1", "Mystery", "To Do")], category_of) == 0
    assert reopen_count([], category_of) == 0


# --- estimate_set_at --------------------------------------------------------------------


ISSUE = {"issue_key": "AIL-1", "project_key": "AIL", "created_at": "2026-10-01T09:00:00.000Z", "story_points": 3.0}


def test_estimate_set_at_ignores_other_fields() -> None:
    changes = [
        _field_change("1", "2026-10-01T09:10:00.000Z", None, "4427", field="sprint"),
        _field_change("2", "2026-10-01T09:20:00.000Z", None, "3600", field="original_estimate"),
        _field_change("3", "2026-10-01T09:30:00.000Z", None, "3"),
    ]
    assert estimate_set_at(ISSUE, changes, POINTS) == "2026-10-01T09:30:00.000Z"
    assert estimate_set_at(ISSUE | {"original_estimate_s": 3600}, changes, HOURS) == "2026-10-01T09:20:00.000Z"


def test_estimate_set_at_orders_changes_by_time_then_numeric_id() -> None:
    at = "2026-10-01T09:30:00.000Z"
    changes = [
        _field_change("10", at, "2", "3"),  # an estimate already existed by now...
        _field_change("9", at, None, "2"),  # ...because 9 came first and set it
        _field_change("8", "2026-10-01T09:45:00.000Z", None, "1"),  # later, even with a lower id
    ]
    assert estimate_set_at(ISSUE, changes, POINTS) == at


def test_estimate_set_at_without_a_current_estimate_still_reports_the_first_setting() -> None:
    changes = [
        _field_change("1", "2026-10-01T10:00:00.000Z", None, "3"),
        _field_change("2", "2026-10-02T10:00:00.000Z", "3", None),  # removed again
    ]
    assert estimate_set_at(ISSUE | {"story_points": None}, changes, POINTS) == "2026-10-01T10:00:00.000Z"


def test_estimate_set_at_is_none_without_any_estimate() -> None:
    changes = [_field_change("1", "2026-10-01T10:00:00.000Z", None, "0")]
    assert estimate_set_at(ISSUE | {"story_points": None}, changes, POINTS) is None
    assert estimate_set_at(ISSUE | {"story_points": 0.0}, [], POINTS) is None


def test_estimate_set_at_returns_the_storage_format() -> None:
    changes = [_field_change("1", "2026-10-01T04:30:00.000-0500", None, "3")]
    assert estimate_set_at(ISSUE, changes, POINTS) == "2026-10-01T09:30:00.000Z"


# --- effort_ratio ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("issue", "estimate", "expected"),
    [
        ({"time_spent_s": 72000, "story_points": 2.0}, POINTS, 1.25),  # 20 h / (2 x 8 h)
        ({"time_spent_s": None, "story_points": 2.0}, POINTS, 0.0),  # nothing logged yet
        ({"time_spent_s": 3600, "story_points": None}, POINTS, None),
        ({"time_spent_s": 3600, "story_points": 0.0}, POINTS, None),
        ({"time_spent_s": 3600, "story_points": -1.0}, POINTS, None),
        ({"time_spent_s": 45000, "original_estimate_s": 36000}, HOURS, 1.25),  # 12.5 h / 10 h
        ({"time_spent_s": 45000, "original_estimate_s": 0}, HOURS, None),
        ({"time_spent_s": 45000, "original_estimate_s": None, "story_points": 5.0}, HOURS, None),
        ({"time_spent_s": 45000, "original_estimate_s": 36000, "story_points": None}, POINTS, None),
    ],
)
def test_effort_ratio(issue: dict, estimate: EstimateConfig, expected: float | None) -> None:
    assert effort_ratio(issue, estimate) == expected


def test_effort_ratio_uses_the_configured_hours_per_point() -> None:
    six_hour_points = EstimateConfig(basis="points", field="story_points", hours_per_point=6)
    assert effort_ratio({"time_spent_s": 43200, "story_points": 1.0}, six_hour_points) == 2.0  # 12 h / 6 h


# --- compute_metrics edge cases ---------------------------------------------------------


def _interval(status: str, stage: str | None, entered_at: str, left_at: str | None = None) -> Interval:
    return Interval(status, stage, entered_at, left_at, None if left_at is None else 1.0)


def test_status_age_is_never_negative(process: ProcessConfig) -> None:
    # Jira's clock a little ahead of ours: the last change is "after" now.
    issue = ISSUE | {"status": "In Progress"}
    intervals = [_interval("In Progress", "development", "2026-10-01T12:00:00.500Z")]
    early = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert compute_metrics(issue, intervals, [], [], process, category_of, early)["status_age_h"] == 0.0
    later = early + timedelta(hours=2, minutes=30, milliseconds=500)
    assert compute_metrics(issue, intervals, [], [], process, category_of, later)["status_age_h"] == 2.5


def test_cycle_time_is_none_when_done_comes_before_the_start(process: ProcessConfig) -> None:
    # Created In Progress at 12:00, moved to Done by a change dated 11:00.
    intervals = [
        _interval("In Progress", "development", "2026-10-01T12:00:00.000Z", "2026-10-01T11:00:00.000Z"),
        _interval("Done", "done", "2026-10-01T11:00:00.000Z"),
    ]
    metrics = compute_metrics(ISSUE, intervals, [], [], process, category_of, NOW)
    assert (metrics["started_at"], metrics["done_at"], metrics["cycle_time_h"]) == (
        "2026-10-01T12:00:00.000Z",
        "2026-10-01T11:00:00.000Z",
        None,
    )


def test_an_unmapped_last_status_is_done_only_by_category(process: ProcessConfig) -> None:
    intervals = [
        _interval("In Progress", "development", "2026-10-01T10:00:00.000Z", "2026-10-01T11:00:00.000Z"),
        _interval("Parked", None, "2026-10-01T11:00:00.000Z"),
    ]
    assert compute_metrics(ISSUE, intervals, [], [], process, category_of, NOW)["done_at"] is None

    def parked_is_done(name: str | None) -> str | None:
        return "done" if name == "Parked" else category_of(name)

    metrics = compute_metrics(ISSUE, intervals, [], [], process, parked_is_done, NOW)
    assert (metrics["done_at"], metrics["cycle_time_h"]) == ("2026-10-01T11:00:00.000Z", 1.0)


def test_later_work_stages_count_as_started(process: ProcessConfig) -> None:
    ops = with_ops(process)
    intervals = [
        _interval("Open", "backlog", "2026-10-01T09:00:00.000Z", "2026-10-01T10:00:00.000Z"),
        _interval("Testing", "testing", "2026-10-01T10:00:00.000Z", "2026-10-01T12:00:00.000Z"),
        _interval("Closed", "done", "2026-10-01T12:00:00.000Z"),
    ]
    issue = {"issue_key": "OPS-9", "project_key": "OPS", "created_at": "2026-10-01T09:00:00.000Z"}
    metrics = compute_metrics(issue, intervals, [], [], ops, category_of, NOW)
    assert (metrics["started_at"], metrics["cycle_time_h"]) == ("2026-10-01T10:00:00.000Z", 2.0)


def test_no_intervals_gives_empty_metrics(process: ProcessConfig) -> None:
    metrics = compute_metrics(ISSUE, [], [], [], process, category_of, NOW)
    assert metrics["started_at"] is metrics["done_at"] is metrics["status_age_h"] is None


def test_compute_metrics_needs_an_aware_now(process: ProcessConfig) -> None:
    with pytest.raises(ValueError, match="naive"):
        compute_metrics(ISSUE, [], [], [], process, category_of, datetime(2026, 11, 4, 12, 0))


def test_compute_metrics_needs_a_configured_project(process: ProcessConfig) -> None:
    with pytest.raises(ConfigError, match="XYZ"):
        compute_metrics(ISSUE | {"project_key": "XYZ"}, [], [], [], process, category_of, NOW)
