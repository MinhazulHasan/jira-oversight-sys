from __future__ import annotations

from dataclasses import asdict

import pytest

from oversight.config import ProcessConfig
from oversight.derive.timeline import (
    UNKNOWN_STATUS,
    Interval,
    TimelineWarnings,
    build_intervals,
    sort_changes,
)
from oversight.timeutil import iso

from tests.fixtures.derive import CASES, LABELS, assert_intervals, with_ops

NO_WARNINGS = {"inconsistent_transitions": 0, "negative_intervals": 0}


def _change(changelog_id: str, changed_at: str, from_status: str | None, to_status: str | None) -> dict:
    return {
        "changelog_id": changelog_id,
        "changed_at": changed_at,
        "from_status": from_status,
        "to_status": to_status,
    }


def _timeline(key: str, process: ProcessConfig, warnings: TimelineWarnings | None = None) -> list[Interval]:
    case = CASES[key]
    issue = case["issue"]
    stage_of = with_ops(process).project(issue["project_key"]).stage_of
    return build_intervals(
        issue["created_at"], issue["status"], case.get("status_changes", []), stage_of, warnings
    )


def test_every_fixture_has_a_label() -> None:
    assert sorted(CASES) == sorted(LABELS["issues"])


@pytest.mark.parametrize("key", sorted(CASES))
def test_intervals_match_the_hand_computed_labels(key: str, process: ProcessConfig) -> None:
    warnings = TimelineWarnings()
    intervals = _timeline(key, process, warnings)
    label = LABELS["issues"][key]
    assert_intervals([asdict(i) for i in intervals], label["intervals"])
    assert asdict(warnings) == label.get("warnings", NO_WARNINGS)


@pytest.mark.parametrize("key", sorted(CASES))
def test_timelines_are_unbroken_and_end_open(key: str, process: ProcessConfig) -> None:
    intervals = _timeline(key, process)
    assert intervals[0].entered_at == iso(CASES[key]["issue"]["created_at"])
    for before, after in zip(intervals, intervals[1:]):
        assert before.left_at == after.entered_at
        assert before.hours is not None and before.hours >= 0
    assert intervals[-1].left_at is None and intervals[-1].hours is None


def test_input_order_does_not_change_the_timeline(process: ProcessConfig) -> None:
    case = CASES["AIL-104"]
    stage_of = process.project("AIL").stage_of
    issue = case["issue"]
    forwards = build_intervals(issue["created_at"], issue["status"], case["status_changes"], stage_of)
    backwards = build_intervals(issue["created_at"], issue["status"], case["status_changes"][::-1], stage_of)
    assert forwards == backwards


def test_sort_changes_orders_by_time_then_by_numeric_id() -> None:
    noon, later, earlier = "2026-10-01T12:00:00.000Z", "2026-10-01T13:00:00.000Z", "2026-10-01T11:00:00.000Z"
    changes = [
        _change("10", noon, "a", "b"),
        _change("2", later, "a", "b"),
        _change("9", noon, "a", "b"),
        _change("100", earlier, "a", "b"),
    ]
    assert [c["changelog_id"] for c in sort_changes(changes)] == ["100", "9", "10", "2"]


def test_sort_changes_puts_non_numeric_ids_after_numeric_ones() -> None:
    at = "2026-10-01T12:00:00.000Z"
    changes = [_change("b", at, "x", "y"), _change("7", at, "x", "y"), _change("a", at, "x", "y")]
    changes.append({"changelog_id": None, "changed_at": at})
    assert [c["changelog_id"] for c in sort_changes(changes)] == ["7", None, "a", "b"]


def test_sort_changes_compares_instants_not_text() -> None:
    changes = [
        _change("1", "2026-11-03T22:54:38.662-0600", "a", "b"),
        _change("2", "2026-11-04T01:00:00.000Z", "a", "b"),
    ]
    assert [c["changelog_id"] for c in sort_changes(changes)] == ["2", "1"]


def test_sort_changes_needs_a_timestamp() -> None:
    with pytest.raises(ValueError, match="changed_at of change 77 is missing"):
        sort_changes([_change("77", None, "a", "b")])  # type: ignore[arg-type]


def test_no_changes_gives_one_open_interval_in_the_current_status(process: ProcessConfig) -> None:
    intervals = build_intervals("2026-10-01T09:00:00.000Z", "In Progress", [], process.project("AIL").stage_of)
    assert intervals == [Interval("In Progress", "development", "2026-10-01T09:00:00.000Z", None, None)]


def test_from_status_is_compared_ignoring_case(process: ProcessConfig) -> None:
    warnings = TimelineWarnings()
    build_intervals(
        "2026-10-01T09:00:00.000Z",
        "Done",
        [
            _change("1", "2026-10-01T10:00:00.000Z", "To Do", "In Progress"),
            _change("2", "2026-10-01T11:00:00.000Z", "IN PROGRESS", "Done"),
        ],
        process.project("AIL").stage_of,
        warnings,
    )
    assert asdict(warnings) == NO_WARNINGS


def test_every_inconsistent_change_is_counted(process: ProcessConfig) -> None:
    warnings = TimelineWarnings()
    intervals = build_intervals(
        "2026-10-01T09:00:00.000Z",
        "Done",
        [
            _change("1", "2026-10-01T10:00:00.000Z", "To Do", "In Progress"),
            _change("2", "2026-10-01T11:00:00.000Z", "Done", "To Do"),
            _change("3", "2026-10-01T12:00:00.000Z", "In Progress", "Done"),
        ],
        process.project("AIL").stage_of,
        warnings,
    )
    assert warnings.inconsistent_transitions == 2
    assert [i.status for i in intervals] == ["To Do", "In Progress", "To Do", "Done"]


def test_warnings_are_optional(process: ProcessConfig) -> None:
    intervals = build_intervals(
        "2026-10-01T12:00:00.000Z",
        "Done",
        [_change("1", "2026-10-01T11:00:00.000Z", "In Progress", "Done")],
        process.project("AIL").stage_of,
    )
    assert intervals[0].hours == 0.0


def test_only_the_negative_interval_is_clamped(process: ProcessConfig) -> None:
    warnings = TimelineWarnings()
    intervals = build_intervals(
        "2026-10-01T12:00:00.000Z",
        "Done",
        [
            _change("1", "2026-10-01T10:00:00.000Z", "To Do", "In Progress"),
            _change("2", "2026-10-01T11:00:00.000Z", "In Progress", "Done"),
        ],
        process.project("AIL").stage_of,
        warnings,
    )
    assert [i.hours for i in intervals] == [0.0, 1.0, None]
    assert warnings.negative_intervals == 1


def test_any_jira_timestamp_form_is_stored_in_the_storage_format(process: ProcessConfig) -> None:
    intervals = build_intervals(
        "2026-11-02T23:08:12.832-0600",
        "Done",
        [
            _change("334350", "2026-11-02T23:09:56.971-0600", "To Do", "In Progress"),
            _change("335082", 1793768078662, "In Progress", "Done"),  # type: ignore[arg-type]
        ],
        process.project("AIL").stage_of,
    )
    assert_intervals([asdict(i) for i in intervals], LABELS["issues"]["AIL-1140"]["intervals"])


def test_missing_status_names_become_unknown(process: ProcessConfig) -> None:
    warnings = TimelineWarnings()
    intervals = build_intervals(
        "2026-10-01T09:00:00.000Z",
        "Done",
        [
            _change("1", "2026-10-01T10:00:00.000Z", None, "In Progress"),
            _change("2", "2026-10-01T11:00:00.000Z", "In Progress", " "),
            _change("3", "2026-10-01T12:00:00.000Z", "", "Done"),
        ],
        process.project("AIL").stage_of,
        warnings,
    )
    assert [(i.status, i.stage) for i in intervals] == [
        (UNKNOWN_STATUS, None),
        ("In Progress", "development"),
        (UNKNOWN_STATUS, None),
        ("Done", "done"),
    ]
    assert asdict(warnings) == NO_WARNINGS

    stage_of = process.project("AIL").stage_of
    only = build_intervals("2026-10-01T09:00:00.000Z", None, [], stage_of)  # type: ignore[arg-type]
    assert only[0].status == UNKNOWN_STATUS


def test_created_at_is_required(process: ProcessConfig) -> None:
    with pytest.raises(ValueError, match="created_at is missing"):
        build_intervals(None, "To Do", [], process.project("AIL").stage_of)  # type: ignore[arg-type]
