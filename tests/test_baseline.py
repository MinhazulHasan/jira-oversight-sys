"""The baseline report"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from oversight.api.routes.baseline import latest_report
from oversight.baseline import BaselineExpected, BaselineResult, ComparisonRow, load_expected, run_baseline
from oversight.cli import EXIT_CONFIG, EXIT_OK, main
from oversight.config import ProcessConfig
from oversight.derive.run import run_derive
from oversight.errors import ConfigError
from oversight.settings import PROJECT_ROOT, Settings
from oversight.store.db import Store
from oversight.timeutil import UTC

from tests.conftest import FIXTURES

SNAPSHOT: dict[str, Any] = yaml.safe_load((FIXTURES / "baseline" / "ail_snapshot.yaml").read_text(encoding="utf-8"))
DERIVED_AT = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
GENERATED_AT = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
AS_OF = date(2026, 11, 2)
WINDOW_DAYS = 90
ZONE = "America/Chicago"
FILES = ["baseline_AIL_2026-11-02.json", "baseline_AIL_2026-11-02.md"]

EXPECTED_FIGURES: dict[str, Any] = {
    "snapshot": {
        "last_synced_at": "2026-11-04T11:00:00.000Z",  # AIL's cursor, not OPS's
        "total_issues": 11,  # AIL-1 to AIL-11; OPS-1 is another project
        "created_before_window_end": 10,  # all but AIL-9, created exactly at 11-02 06:00
        "by_type": {"Task": 8, "Bug": 1, "Epic": 1, "Subtask": 1},  # Task: AIL-1, 2, 4, 5, 6, 8, 9, 11
        "by_status": {"Done": 9, "In Progress": 1, "To Do": 1},  # In Progress: AIL-8; To Do: AIL-9
        "by_status_category": {"done": 9, "indeterminate": 1, "new": 1},
    },
    "resolved": {
        "in_window": 6,
        "with_original_estimate": {"count": 1, "share": 1 / 6},  # AIL-6 (7,200 s); AIL-4's 0 is no estimate
        "with_story_points": {"count": 4, "share": 4 / 6},  # AIL-1, 3, 5, 7; AIL-6 has 0, AIL-4 none
        "with_logged_time": {
            "timespent": {"count": 4, "share": 4 / 6},  # AIL-1, 3, 5, 7
            "aggregate": {"count": 5, "share": 5 / 6},  # the same four and AIL-4 (subtask time)
            "worklog_rows": {"count": 3, "share": 3 / 6},  # AIL-1, 5, 6; AIL-3 and AIL-7 have none
        },
        "straight_todo_to_done": {"count": 1, "share": 1 / 6, "from_statuses": ["To Do"], "to_statuses": ["Done"]},
        "created_in_done": {"count": 1, "share": 1 / 6},  # AIL-4
        "done_entered_in_window": 7,
    },
    "estimates": {
        "hours_per_point": {"n": 4, "median": 7.0, "p25": 5.5, "p75": 8.5},
        "within_25pct": {"hours_per_point": 8.0, "count": 3, "share": 3 / 4},
        "estimate_set_after_start": {"count": 1, "of": 2, "share": 1 / 2},
    },
    "time_in_status": {
        "To Do": {"stage": "backlog", "n": 8, "median": 1.5, "p75": 2.0, "p90": 8.6},
        "In Progress": {"stage": "development", "n": 4, "median": 24.0, "p75": 30.0, "p90": 40.8},
        "Done": {"stage": "done", "n": 1, "median": 24.0, "p75": 24.0, "p90": 24.0},  # AIL-5, before its reopen
        "In Review": {"stage": None, "n": 1, "median": 24.0, "p75": 24.0, "p90": 24.0},  # AIL-7
    },
    "cycle_time": {"n": 3, "median": 48.0, "p75": 54.0, "p90": 57.6},
    "reopens": {"count": 1, "share": 1 / 6},  # AIL-5
    "worklogs": {
        "count": 6,
        "total_hours": 46.5,
        "authors": 3,
        "top_author_share": {"seconds": 32 / 93, "count": 3 / 6},
        "tempo_share": {"seconds": 32 / 93, "count": 3 / 6},
    },
    "evidence": {
        "not_measurable": ["code_review", "dev_signoff", "testing", "test_review", "test_signoff"],
        "configured": [],
    },
    "issue_links": {
        "issues_with_issue_links": 3,  # AIL-1, AIL-2, AIL-5 (OPS-1's link isn't AIL's)
        "issue_links": 4,
        "issue_links_by_title": {"blocks": 2, "relates to": 2, "is blocked by": 1},
        "remote_links": 4,
        "remote_links_by_host": {"github.com": 2, "(no host)": 1, "example.atlassian.net": 1},
    },
}

MIXED = {
    "total_issues": 11,
    "resolved_in_window": 6,
    "with_original_estimate": 0,
    "with_story_points": 4,
    "with_logged_time": 5,
    "straight_todo_to_done": 1,
}
MIXED_ROWS = [
    ComparisonRow(
        metric="total_issues",
        expected=11,
        actual=10,
        match=False,
        note="the reference audit counted during 2 Nov; this window ends at midnight before it",
    ),
    ComparisonRow(metric="resolved_in_window", expected=6, actual=6, match=True, note=""),
    ComparisonRow(
        metric="with_original_estimate",
        expected=0,
        actual=1,
        match=False,
        note="original estimates set or cleared since the reference audit, or a different set of resolved issues",
    ),
    ComparisonRow(metric="with_story_points", expected=4, actual=4, match=True, note=""),
    ComparisonRow(
        metric="with_logged_time",
        expected=5,
        actual=4,
        match=False,
        note="the reference audit may have counted logged time another way; other variants: aggregate 5, "
        "worklog_rows 3",
    ),
    ComparisonRow(metric="straight_todo_to_done", expected=1, actual=1, match=True, note=""),
]


def load_snapshot(store: Store) -> None:
    """Insert every fixture row with Store.upsert, as a sync would."""
    with store.transaction():
        store.upsert_many("statuses", SNAPSHOT["statuses"])
        for case in SNAPSHOT["cases"]:
            store.upsert("issues", case["issue"])
            for table in ("status_changes", "field_changes", "worklogs", "links"):
                store.upsert_many(table, case.get(table, []))
        for cursor in SNAPSHOT["sync_state"]:
            store.set_cursor(**cursor)


def derive(store: Store, process: ProcessConfig) -> None:
    run_derive(store, process, projects=["AIL"], now=lambda: DERIVED_AT)


def report(store: Store, process: ProcessConfig, out_dir: Path, **overrides: Any) -> BaselineResult:
    options: dict[str, Any] = {
        "project": "AIL",
        "as_of": AS_OF,
        "window_days": WINDOW_DAYS,
        "tz_name": ZONE,
        "out_dir": out_dir,
        "now": lambda: GENERATED_AT,
    }
    return run_baseline(store, process, **(options | overrides))


def expected(**overrides: Any) -> BaselineExpected:
    data: dict[str, Any] = {
        "project": "AIL",
        "as_of": AS_OF,
        "window_days": WINDOW_DAYS,
        "timezone": ZONE,
        "tempo_account_id": "app-tempo",
        "expected": MIXED,
    }
    return BaselineExpected.model_validate(data | overrides)


def add_issue(store: Store, key: str = "AIL-1", **columns: Any) -> None:
    """One AIL issue, resolved inside the window unless the columns say otherwise."""
    row = {
        "issue_key": key,
        "issue_id": f"id-{key}",
        "project_key": "AIL",
        "status": "Done",
        "status_category": "done",
        "created_at": "2026-09-01T00:00:00.000Z",
        "resolved_at": "2026-09-02T00:00:00.000Z",
    }
    store.upsert("issues", row | columns)


def rounded(value: Any) -> Any:
    """Floats rounded to 9 places, all the way down, so fractions written by hand compare exactly."""
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, dict):
        return {key: rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rounded(item) for item in value]
    return value


def read_json(result: BaselineResult) -> dict[str, Any]:
    assert result.json_path is not None
    return json.loads(result.json_path.read_text(encoding="utf-8"))


def read_markdown(result: BaselineResult) -> str:
    assert result.markdown_path is not None
    return result.markdown_path.read_text(encoding="utf-8")


@pytest.fixture
def snapshot_store(store: Store, process: ProcessConfig) -> Store:
    load_snapshot(store)
    derive(store, process)
    return store


def test_every_figure_matches_the_hand_computed_values(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    result = report(snapshot_store, process, tmp_path, expected=expected())

    assert rounded(result.figures) == rounded(EXPECTED_FIGURES)
    assert rounded(read_json(result)["figures"]) == rounded(EXPECTED_FIGURES)
    assert list(result.figures["snapshot"]["by_type"]) == ["Task", "Bug", "Epic", "Subtask"]
    assert list(result.figures["time_in_status"]) == ["To Do", "In Progress", "Done", "In Review"]
    assert list(result.figures["issue_links"]["remote_links_by_host"]) == [
        "github.com",
        "(no host)",
        "example.atlassian.net",
    ]


@pytest.mark.parametrize(
    ("resolved_at", "inside"),
    [
        ("2026-11-02T05:59:00.000Z", True),
        ("2026-11-02T06:00:00.000Z", False),  # exactly at the end
        ("2026-11-02T05:00:00.000Z", True),  # out, if the end wrongly used summer time (UTC-5)
        ("2026-08-04T05:00:00.000Z", True),  # exactly at the start: 4 Aug 00:00 CDT is 05:00 UTC
        ("2026-08-04T04:59:59.999Z", False),  # a millisecond before the start
        ("2026-08-04T04:00:00.000Z", False),  # in, if the start wrongly used winter time (UTC-6)
    ],
)
def test_the_window_runs_between_local_midnights(
    store: Store, process: ProcessConfig, tmp_path: Path, resolved_at: str, inside: bool
) -> None:
    add_issue(store, created_at="2026-07-01T00:00:00.000Z", resolved_at=resolved_at)
    derive(store, process)
    assert report(store, process, tmp_path).figures["resolved"]["in_window"] == int(inside)


def test_straight_to_done_ignores_the_case_of_status_names(
    store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    add_issue(store)
    store.upsert(
        "status_changes",
        {
            "issue_key": "AIL-1",
            "changelog_id": "1",
            "changed_at": "2026-09-01T10:00:00.000Z",
            "from_status": "to do",
            "to_status": "DONE",
        },
    )
    derive(store, process)
    assert report(store, process, tmp_path).figures["resolved"]["straight_todo_to_done"]["count"] == 1


def test_straight_to_done_uses_every_status_of_both_stages(
    store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    data = process.model_dump()
    data["projects"]["AIL"]["status_to_stage"] |= {"Backlog": "backlog", "Closed": "done"}
    wider = ProcessConfig.model_validate(data)
    add_issue(store, "AIL-1", status="Closed")
    add_issue(store, "AIL-2", status="Closed")
    store.upsert_many(
        "status_changes",
        [
            {"issue_key": "AIL-1", "changelog_id": "1", "changed_at": "2026-09-01T10:00:00.000Z",
             "from_status": "Backlog", "to_status": "Closed"},
            {"issue_key": "AIL-2", "changelog_id": "2", "changed_at": "2026-09-01T10:00:00.000Z",
             "from_status": "Backlog", "to_status": "In Progress"},
            {"issue_key": "AIL-2", "changelog_id": "3", "changed_at": "2026-09-01T11:00:00.000Z",
             "from_status": "In Progress", "to_status": "Closed"},
        ],
    )
    derive(store, wider)
    result = report(store, wider, tmp_path)

    assert result.figures["resolved"]["straight_todo_to_done"] == {
        "count": 1,
        "share": 0.5,
        "from_statuses": ["To Do", "Backlog"],
        "to_statuses": ["Done", "Closed"],
    }
    assert "| Straight from To Do or Backlog to Done or Closed | 1 | 50.0% |" in read_markdown(result).splitlines()


def test_hours_per_point_needs_both_points_and_time_spent(
    store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    add_issue(store, "AIL-1", story_points=2.0, time_spent_s=None)
    add_issue(store, "AIL-2", story_points=2.0, time_spent_s=0)
    add_issue(store, "AIL-3", story_points=None, time_spent_s=7200)
    add_issue(store, "AIL-4", story_points=0.0, time_spent_s=7200)
    add_issue(store, "AIL-5", story_points=0.5, time_spent_s=1800)  # 0.5 h / 0.5 points = 1 h per point
    derive(store, process)
    estimates = report(store, process, tmp_path).figures["estimates"]
    assert estimates["hours_per_point"] == {"n": 1, "median": 1.0, "p25": 1.0, "p75": 1.0}
    assert estimates["within_25pct"] == {"hours_per_point": 8.0, "count": 0, "share": 0.0}


@pytest.mark.parametrize(
    ("points_set_at", "late"),
    [
        (None, 0),  # points from creation, no history: set at created_at, when work started
        ("2026-09-01T00:00:00.000Z", 0),  # set at the very moment work started: not after it
        ("2026-09-01T00:00:00.001Z", 1),  # a millisecond later
    ],
)
def test_an_estimate_is_late_only_when_set_after_work_started(
    store: Store, process: ProcessConfig, tmp_path: Path, points_set_at: str | None, late: int
) -> None:
    add_issue(store, created_at="2026-09-01T00:00:00.000Z", story_points=2.0)
    store.upsert(
        "status_changes",
        {
            "issue_key": "AIL-1",
            "changelog_id": "2",
            "changed_at": "2026-09-01T08:00:00.000Z",
            "from_status": "In Progress",
            "to_status": "Done",
        },
    )
    if points_set_at:
        store.upsert(
            "field_changes",
            {
                "issue_key": "AIL-1",
                "changelog_id": "1",
                "field": "story_points",
                "changed_at": points_set_at,
                "from_value": None,
                "to_value": "2",
            },
        )
    derive(store, process)
    figures = report(store, process, tmp_path).figures["estimates"]["estimate_set_after_start"]
    assert figures == {"count": late, "of": 1, "share": float(late)}


def test_worklog_shares_skip_missing_authors_and_break_ties_by_seconds(
    store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    add_issue(store)
    store.upsert_many(
        "worklogs",
        [
            {"worklog_id": "1", "issue_key": "AIL-1", "seconds": 100, "author_id": "acc-a"},
            {"worklog_id": "2", "issue_key": "AIL-1", "seconds": 100, "author_id": "acc-a"},
            {"worklog_id": "3", "issue_key": "AIL-1", "seconds": 150, "author_id": "acc-b"},
            {"worklog_id": "4", "issue_key": "AIL-1", "seconds": 150, "author_id": "acc-b"},
            {"worklog_id": "5", "issue_key": "AIL-1", "seconds": 500, "author_id": None},
            {"worklog_id": "6", "issue_key": "AIL-1", "seconds": None, "author_id": "acc-c"},
        ],
    )
    derive(store, process)
    result = report(store, process, tmp_path, expected=expected(tempo_account_id="acc-nobody"))

    assert rounded(result.figures["worklogs"]) == rounded(
        {
            "count": 6,
            "total_hours": 1000 / 3600,
            "authors": 3,
            "top_author_share": {"seconds": 0.3, "count": 2 / 6},
            "tempo_share": {"seconds": 0.0, "count": 0.0},
        }
    )


def test_tempo_share_needs_the_expected_tempo_account(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    plain = report(snapshot_store, process, tmp_path / "plain")
    assert "tempo_share" not in plain.figures["worklogs"]
    assert "Tempo app account" not in read_markdown(plain)
    unnamed = report(snapshot_store, process, tmp_path / "unnamed", expected=expected(tempo_account_id=None))
    assert "tempo_share" not in unnamed.figures["worklogs"]


def test_an_empty_project_gives_an_empty_report(store: Store, process: ProcessConfig, tmp_path: Path) -> None:
    result = report(store, process, tmp_path)

    no_share = {"count": 0, "share": None}
    assert result.figures == {
        "snapshot": {
            "last_synced_at": None,
            "total_issues": 0,
            "created_before_window_end": 0,
            "by_type": {},
            "by_status": {},
            "by_status_category": {},
        },
        "resolved": {
            "in_window": 0,
            "with_original_estimate": no_share,
            "with_story_points": no_share,
            "with_logged_time": {"timespent": no_share, "aggregate": no_share, "worklog_rows": no_share},
            "straight_todo_to_done": no_share | {"from_statuses": ["To Do"], "to_statuses": ["Done"]},
            "created_in_done": no_share,
            "done_entered_in_window": 0,
        },
        "estimates": {
            "hours_per_point": {"n": 0, "median": None, "p25": None, "p75": None},
            "within_25pct": {"hours_per_point": 8.0, "count": 0, "share": None},
            "estimate_set_after_start": {"count": 0, "of": 0, "share": None},
        },
        "time_in_status": {},
        "cycle_time": {"n": 0, "median": None, "p75": None, "p90": None},
        "reopens": no_share,
        "worklogs": {
            "count": 0,
            "total_hours": 0.0,
            "authors": 0,
            "top_author_share": {"seconds": None, "count": None},
        },
        "evidence": EXPECTED_FIGURES["evidence"],
        "issue_links": {
            "issues_with_issue_links": 0,
            "issue_links": 0,
            "issue_links_by_title": {},
            "remote_links": 0,
            "remote_links_by_host": {},
        },
    }
    markdown = read_markdown(result)
    assert "- The snapshot holds no AIL issues: run `python -m oversight sync --full` first." in markdown
    assert "No status interval ended in the window." in markdown
    assert "| 0 | n/a | n/a | n/a |" in markdown.splitlines()


def test_the_comparison_has_one_row_per_expected_figure(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    result = report(snapshot_store, process, tmp_path, expected=expected())

    assert result.comparison == MIXED_ROWS
    assert read_json(result)["comparison"] == [asdict(row) for row in MIXED_ROWS]


def test_matching_figures_carry_no_note_except_the_logged_time_variants(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    exact = {
        "total_issues": 10,
        "resolved_in_window": 6,
        "with_original_estimate": 1,
        "with_story_points": 4,
        "with_logged_time": 4,
        "straight_todo_to_done": 1,
    }
    result = report(snapshot_store, process, tmp_path, expected=expected(expected=exact))

    assert all(row.match for row in result.comparison)
    notes = [row.note for row in result.comparison]
    assert notes == ["", "", "", "", "other variants: aggregate 5, worklog_rows 3", ""]
    assert "6 of 6 figures match." in read_markdown(result)


def test_an_unknown_expected_metric_gets_a_row_without_a_value(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    hand_built = expected(expected={"reopened": 3, "resolved_in_window": 6})
    result = report(snapshot_store, process, tmp_path, expected=hand_built)
    assert result.comparison == [
        ComparisonRow(metric="reopened", expected=3, actual=None, match=False, note="not a baseline figure"),
        ComparisonRow(metric="resolved_in_window", expected=6, actual=6, match=True, note=""),
    ]
    assert "| reopened |  | 3 | n/a | no | not a baseline figure |" in read_markdown(result).splitlines()


@pytest.mark.parametrize(
    ("overrides", "note"),
    [
        (
            {"as_of": date(2026, 11, 3)},
            "the expected figures are for AIL as of 2026-11-03 over 90 days, "
            "and this report is for AIL as of 2026-11-02 over 90 days",
        ),
        (
            {"window_days": 30},
            "the expected figures are for AIL as of 2026-11-02 over 30 days, "
            "and this report is for AIL as of 2026-11-02 over 90 days",
        ),
        (
            {"project": "OPS"},
            "the expected figures are for OPS as of 2026-11-02 over 90 days, "
            "and this report is for AIL as of 2026-11-02 over 90 days",
        ),
    ],
)
def test_the_comparison_is_skipped_for_another_run(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path, overrides: dict[str, Any], note: str
) -> None:
    result = report(snapshot_store, process, tmp_path, expected=expected(**overrides))

    assert result.comparison == []
    payload = read_json(result)
    assert payload["comparison"] == []
    assert (payload["metadata"]["comparison_status"], payload["metadata"]["comparison_note"]) == ("skipped", note)
    markdown = read_markdown(result)
    assert f"Skipped: {note}." in markdown.splitlines()
    assert "| Metric |" not in markdown
    assert "tempo_share" in result.figures["worklogs"]


def test_another_zone_is_compared_with_a_note(snapshot_store: Store, process: ProcessConfig, tmp_path: Path) -> None:
    result = report(snapshot_store, process, tmp_path, expected=expected(), tz_name="UTC")

    resolved = result.figures["resolved"]
    assert (resolved["in_window"], resolved["with_story_points"]["count"]) == (6, 3)
    assert resolved["straight_todo_to_done"]["count"] == 2
    assert [row.match for row in result.comparison] == [False, True, False, False, False, False]
    note = "the reference audit used America/Chicago and this report uses UTC, so the window edges differ"
    assert read_json(result)["metadata"]["comparison_note"] == note
    assert f"Note: {note}." in read_markdown(result).splitlines()


def test_reports_are_written_and_the_run_is_recorded(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    out_dir = tmp_path / "not" / "there" / "yet"
    result = report(snapshot_store, process, out_dir)

    assert result.markdown_path == out_dir / "baseline_AIL_2026-11-02.md"
    assert result.json_path == out_dir / "baseline_AIL_2026-11-02.json"
    assert sorted(path.name for path in out_dir.iterdir()) == FILES
    assert b"\r\n" not in result.markdown_path.read_bytes()
    assert b"\r\n" not in result.json_path.read_bytes()

    run = snapshot_store.last_run("baseline")
    assert (run["status"], run["started_at"], run["finished_at"]) == (
        "succeeded",
        "2026-11-04T13:00:00.000Z",
        "2026-11-04T13:00:00.000Z",
    )
    assert json.loads(run["stats_json"]) == {
        "project": "AIL",
        "as_of": "2026-11-02",
        "window_days": 90,
        "zone": ZONE,
        "outputs": {"markdown": str(result.markdown_path), "json": str(result.json_path)},
        "comparison": {"status": "not requested", "rows": 0, "matched": 0},
    }
    assert "Not requested: `python -m oversight baseline --compare`" in read_markdown(result)


def test_the_json_metadata_describes_the_run(snapshot_store: Store, process: ProcessConfig, tmp_path: Path) -> None:
    result = report(snapshot_store, process, tmp_path, expected=expected())
    payload = read_json(result)

    assert list(payload) == ["metadata", "figures", "comparison"]
    assert payload["metadata"] == {
        "project": "AIL",
        "as_of": "2026-11-02",
        "window_days": 90,
        "zone": ZONE,
        "window": {
            "start": "2026-08-04T05:00:00.000Z",
            "end": "2026-11-02T06:00:00.000Z",
            "start_local": "2026-08-04T00:00:00-05:00",
            "end_local": "2026-11-02T00:00:00-06:00",
        },
        "generated_at": "2026-11-04T13:00:00.000Z",
        "process_version": "0.1.0-test",
        "last_synced_at": "2026-11-04T11:00:00.000Z",
        "metrics_computed_at": "2026-11-04T12:00:00.000Z",
        "metrics_stale": False,
        "issues_without_metrics": 0,
        "comparison_status": "compared",
        "comparison_note": "",
        "run_id": snapshot_store.last_run("baseline")["run_id"],
    }


def test_the_markdown_report_renders_every_section_and_the_comparison_table(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    lines = read_markdown(report(snapshot_store, process, tmp_path, expected=expected())).splitlines()

    assert [line for line in lines if line.startswith("#")] == [
        "# Baseline report: AIL, as of 2026-11-02",
        "## Comparison with the reference audit",
        "## Snapshot",
        "## Resolved in the window",
        "## Estimates",
        "## Time in status",
        "## Cycle time",
        "## Reopens",
        "## Worklogs",
        "## Evidence",
        "## Issue links",
    ]
    for line in (
        "| Window | 2026-08-04 00:00 to 2026-11-02 00:00 America/Chicago (90 days; the end is excluded) |",
        "| Window in UTC | 2026-08-04T05:00:00.000Z to 2026-11-02T06:00:00.000Z |",
        "| Metric | What is counted | Reference audit | Regenerated | Match | Note |",
        "|---|---|---:|---:|---|---|",
        "| total_issues | issues created before the window end | 11 | 10 | no "
        "| the reference audit counted during 2 Nov; this window ends at midnight before it |",
        "| resolved_in_window | issues resolved in the window | 6 | 6 | yes |  |",
        "| with_original_estimate | resolved, with an original estimate | 0 | 1 | no "
        "| original estimates set or cleared since the reference audit, or a different set of resolved issues |",
        "| with_story_points | resolved, with story points | 4 | 4 | yes |  |",
        "| with_logged_time | resolved, with time spent | 5 | 4 | no "
        "| the reference audit may have counted logged time another way; other variants: aggregate 5, "
        "worklog_rows 3 |",
        "| straight_todo_to_done | resolved, straight from the first stage to done | 1 | 1 | yes |  |",
        "3 of 6 figures match. Each difference has a note; where the data and the reference audit disagree, the "
        "data wins.",
        "| Created before the window end | 10 |",
        "| Task | 8 |",
        "| With logged time: time spent including subtasks | 5 | 83.3% |",
        "| Straight from To Do to Done | 1 | 16.7% |",
        "Issues that entered a done status in the window, from the status history "
        "(an alternative to the resolution date): 7.",
        "| Hours per point, median | 7.0 |",
        "| Within ±25% of 8 hours per point | 3 of 4 (75.0%) |",
        "| Estimate first set after work started | 1 of 2 (50.0%) |",
        "| To Do | backlog | 8 | 1.5 | 2.0 | 8.6 |",
        "| In Review | (unmapped) | 1 | 24.0 | 24.0 | 24.0 |",
        "| 3 | 48.0 | 54.0 | 57.6 |",
        "Issues resolved in the window and reopened at least once: 1 (16.7%).",
        "| Hours logged | 46.5 |",
        "| Most common account: share of hours | 34.4% |",
        "| Tempo app account: share of worklogs | 50.0% |",
        "Not measurable: process.yaml has no evidence source for code_review, dev_signoff, testing, "
        'test_review and test_signoff, so the rules that need them will report "not measurable".',
        "| blocks | 2 |",
        "| github.com | 2 |",
    ):
        assert line in lines, line


def test_no_account_id_name_or_link_text_reaches_any_output(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    accounts = {"Developer One"}
    for case in SNAPSHOT["cases"]:
        for row in [case["issue"], *case.get("status_changes", []), *case.get("worklogs", [])]:
            accounts |= {row[key] for key in ("assignee_id", "author_id") if row.get(key)}
    assert accounts == {"acc-dev-1", "acc-dev-2", "app-tempo", "Developer One"}
    link_text = {"PR #1", "PR #2", "leaked secret", "hunter2", "token=abc", "example/repo", "pageId"}

    result = report(snapshot_store, process, tmp_path, expected=expected())
    outputs = {
        "markdown": read_markdown(result),
        "json": result.json_path.read_text(encoding="utf-8"),
        "console": result.render(),
        "run record": snapshot_store.last_run("baseline")["stats_json"],
    }
    for where, text in outputs.items():
        for secret in accounts | link_text:
            assert secret not in text, f"{secret!r} in the {where} output"


def test_jira_data_cannot_break_the_markdown(store: Store, process: ProcessConfig, tmp_path: Path) -> None:
    add_issue(store, status="Waiting | on <b>them</b>", status_category="indeterminate", resolved_at=None)
    store.upsert("links", {"issue_key": "AIL-1", "kind": "remote_link", "target": "https://<script>/x", "title": "x"})
    derive(store, process)
    result = report(store, process, tmp_path)

    lines = read_markdown(result).splitlines()
    assert "| Waiting \\| on &lt;b&gt;them&lt;/b&gt; | 1 |" in lines
    assert "| &lt;script&gt; | 1 |" in lines
    assert not any("<b>" in line or "<script>" in line for line in lines)
    figures = read_json(result)["figures"]
    assert figures["snapshot"]["by_status"] == {"Waiting | on <b>them</b>": 1}
    assert figures["issue_links"]["remote_links_by_host"] == {"<script>": 1}


def test_render_summarises_the_report(snapshot_store: Store, process: ProcessConfig, tmp_path: Path) -> None:
    result = report(snapshot_store, process, tmp_path, expected=expected())
    lines = result.render().splitlines()

    assert lines[:4] == [
        "baseline AIL as of 2026-11-02: 90 days in America/Chicago",
        "  window: 2026-08-04T05:00:00.000Z to 2026-11-02T06:00:00.000Z (UTC, end excluded)",
        "  issues: 11 in the snapshot, 10 created before the window end",
        "  resolved in the window: 6 (story points 4 (66.7%), original estimate 1 (16.7%), "
        "logged time 4 (66.7%), straight to done 1 (16.7%))",
    ]
    assert "  comparison with the reference audit: 3 of 6 match" in lines
    assert (
        "    total_issues            expected      11  actual      10  DIFFERS  "
        "(the reference audit counted during 2 Nov; this window ends at midnight before it)"
    ) in lines
    assert "    resolved_in_window      expected       6  actual       6  match" in lines
    assert lines[-2:] == [f"  report: {result.markdown_path}", f"  data: {result.json_path}"]


def test_render_copes_with_a_result_built_by_hand() -> None:
    text = BaselineResult(project="AIL", as_of=AS_OF, window_days=1, timezone="UTC", figures={}).render()
    assert text.splitlines() == [
        "baseline AIL as of 2026-11-02: 1 day in UTC",
        "  window: 2026-11-01T00:00:00.000Z to 2026-11-02T00:00:00.000Z (UTC, end excluded)",
        "  issues: n/a in the snapshot, n/a created before the window end",
        "  resolved in the window: n/a (story points n/a, original estimate n/a, logged time n/a, "
        "straight to done n/a)",
    ]


def test_a_project_without_derived_metrics_is_a_config_error(
    store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    load_snapshot(store)  # synced, never derived
    out_dir = tmp_path / "reports"

    with pytest.raises(ConfigError, match=re.escape("no derived metrics for AIL: run `python -m oversight derive`")):
        report(store, process, out_dir)

    failed = store.last_run("baseline", status="failed")
    assert failed is not None and failed["finished_at"] == "2026-11-04T13:00:00.000Z"
    assert json.loads(failed["stats_json"])["error"].startswith("ConfigError: no derived metrics for AIL")
    assert store.last_run("baseline") is None
    assert not out_dir.exists()


def test_missing_and_stale_derived_data_are_flagged(store: Store, process: ProcessConfig, tmp_path: Path) -> None:
    add_issue(store, "AIL-1")
    add_issue(store, "AIL-2", created_at=None)  # derive skips an issue it can't start a timeline for
    derive(store, process)  # at 12:00
    store.set_cursor("AIL", last_synced_at="2026-11-04T12:30:00.000Z", run_id="sync-2", account_timezone=ZONE)

    result = report(store, process, tmp_path)

    metadata = read_json(result)["metadata"]
    assert (metadata["issues_without_metrics"], metadata["metrics_stale"]) == (1, True)
    lines = read_markdown(result).splitlines()
    assert (
        "- 1 of 2 issues have no derived metrics, so the figures built on derived data leave them out." in lines
    )
    assert (
        "- The last sync is newer than the derived metrics: run `python -m oversight derive`, "
        "then regenerate this report."
    ) in lines
    assert result.figures["resolved"]["in_window"] == 2
    assert result.figures["resolved"]["done_entered_in_window"] == 1


def test_the_oldest_metrics_row_dates_the_derived_data(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    snapshot_store.upsert("issue_metrics", {"issue_key": "AIL-9", "computed_at": "2026-11-04T09:00:00.000Z"})

    metadata = read_json(report(snapshot_store, process, tmp_path))["metadata"]

    assert (metadata["metrics_computed_at"], metadata["metrics_stale"]) == ("2026-11-04T09:00:00.000Z", True)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"project": "XYZ"}, "project 'XYZ' is not configured in process.yaml"),
        ({"project": "../AIL"}, "invalid Jira project keys: ../AIL"),
        ({"tz_name": "Mars/Olympus_Mons"}, "unknown time zone: 'Mars/Olympus_Mons'"),
        ({"window_days": 0}, "window_days must be at least 1"),
    ],
)
def test_invalid_arguments_are_config_errors(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    out_dir = tmp_path / "reports"
    with pytest.raises(ConfigError, match=re.escape(message)):
        report(snapshot_store, process, out_dir, **overrides)
    failed = snapshot_store.last_run("baseline", status="failed")
    assert json.loads(failed["stats_json"])["error"].startswith("ConfigError")
    assert not out_dir.exists()


def test_load_expected_reads_the_shipped_config() -> None:
    loaded = load_expected(PROJECT_ROOT / "config" / "baseline_expected.yaml")

    assert (loaded.project, loaded.as_of, loaded.window_days, loaded.timezone) == (
        "AIL",
        date(2026, 11, 2),
        90,
        "America/Chicago",
    )
    assert loaded.tempo_account_id and loaded.tempo_account_id.startswith("557058:")
    assert loaded.expected == {
        "total_issues": 1134,
        "resolved_in_window": 291,
        "with_original_estimate": 0,
        "with_story_points": 192,
        "with_logged_time": 248,
        "straight_todo_to_done": 46,
    }


def _valid_expected() -> dict[str, Any]:
    return {
        "project": "AIL",
        "as_of": "2026-11-02",
        "window_days": 90,
        "timezone": "America/Chicago",
        "expected": {"total_issues": 1134},
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update({"surprise": 1}), "surprise"),
        (lambda d: d.pop("expected"), "expected"),
        (lambda d: d.update({"expected": {"total_issues": "lots"}}), "total_issues"),
        (lambda d: d.update({"expected": {"with_storypoints": 3}}), "unknown metrics with_storypoints"),
        (lambda d: d.update({"as_of": "2 Nov"}), "as_of"),
        (lambda d: d.update({"window_days": 0}), "window_days must be at least 1"),
        (lambda d: d.update({"timezone": "Mars/Olympus_Mons"}), "unknown time zone"),
        (lambda d: d.update({"timezone": " "}), "timezone is empty"),
        (lambda d: d.update({"project": "ail"}), "not an upper-case Jira project key"),
        (lambda d: d.update({"project": "A-B"}), "invalid Jira project keys"),
    ],
)
def test_load_expected_rejects_an_invalid_file(tmp_path: Path, mutate: Any, message: str) -> None:
    data = _valid_expected()
    mutate(data)
    path = tmp_path / "baseline_expected.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_expected(path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("project: [unclosed", "not valid YAML"),
        ("", "baseline_expected.yaml is invalid"),
        ("- AIL\n- 2026-11-02\n", "baseline_expected.yaml is invalid"),
    ],
)
def test_load_expected_rejects_a_broken_file(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "baseline_expected.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_expected(path)


def test_load_expected_needs_the_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_expected(tmp_path / "missing.yaml")


def test_load_expected_reports_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "baseline_expected.yaml"
    path.write_text((FIXTURES / "config" / "baseline_expected.yaml").read_text(encoding="utf-8"), encoding="utf-16")
    with pytest.raises(ConfigError, match="not UTF-8"):
        load_expected(path)


def test_cli_baseline_compare_writes_both_reports(
    settings: Settings, process: ProcessConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(settings.db_file) as store:
        load_snapshot(store)
        derive(store, process)

    assert main(["baseline", "--compare"], settings=settings) == EXIT_OK

    out = capsys.readouterr().out
    assert "baseline AIL as of 2026-11-02: 90 days in America/Chicago" in out
    assert "comparison with the reference audit: 0 of 6 match" in out
    assert sorted(path.name for path in settings.reports_path.iterdir()) == FILES
    payload = json.loads((settings.reports_path / FILES[0]).read_text(encoding="utf-8"))
    assert payload["metadata"]["comparison_status"] == "compared"
    assert rounded(payload["figures"]["worklogs"]["tempo_share"]) == rounded({"seconds": 32 / 93, "count": 0.5})


def test_cli_baseline_asks_for_derive_first(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    with Store.open(settings.db_file) as store:
        load_snapshot(store)
    code = main(["baseline", "--as-of", "2026-11-02", "--tz", "America/Chicago"], settings=settings)
    assert code == EXIT_CONFIG
    assert "run `python -m oversight derive` first" in capsys.readouterr().err


def test_cli_baseline_missing_writes_one_for_each_synced_board_that_has_none(
    settings: Settings, process: ProcessConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(settings.db_file) as store:
        load_snapshot(store)  # AIL and OPS have synced
        derive(store, process)  # AIL's metrics only
    boards = settings.model_copy(update={"jira_projects": ["AIL", "NEW"]})  # NEW has never synced: nothing to report yet

    assert main(["baseline", "--missing", "--as-of", "2026-11-02", "--tz", ZONE], settings=boards) == EXIT_OK
    assert "baseline AIL as of 2026-11-02" in capsys.readouterr().out
    assert sorted(path.name for path in settings.reports_path.iterdir()) == FILES
    written = (settings.reports_path / FILES[0]).stat().st_mtime_ns

    for argv in (["baseline", "--missing"], ["baseline", "--missing", "--project", "NEW"]):
        assert main(argv, settings=boards) == EXIT_OK
        assert capsys.readouterr().out == ""
    assert sorted(path.name for path in settings.reports_path.iterdir()) == FILES
    assert (settings.reports_path / FILES[0]).stat().st_mtime_ns == written


def test_cli_baseline_missing_writes_the_other_boards_when_one_fails(
    settings: Settings, process: ProcessConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(settings.db_file) as store:
        load_snapshot(store)
        derive(store, process)  # OPS has synced but has no metrics: its report cannot be written yet
    boards = settings.model_copy(update={"jira_projects": ["OPS", "AIL"]})

    code = main(["baseline", "--missing", "--as-of", "2026-11-02", "--tz", ZONE], settings=boards)

    assert code == EXIT_CONFIG  # the failure is still reported, after AIL's report is written
    assert sorted(path.name for path in settings.reports_path.iterdir()) == FILES
    assert "run `python -m oversight derive` first" in capsys.readouterr().err


def test_cli_baseline_compare_and_missing_do_not_go_together(settings: Settings) -> None:
    with pytest.raises(SystemExit):
        main(["baseline", "--compare", "--missing"], settings=settings)


def test_no_output_names_a_ticket_number(snapshot_store: Store, process: ProcessConfig, tmp_path: Path) -> None:
    for zone_name in (ZONE, "UTC"):
        result = report(snapshot_store, process, tmp_path / zone_name.replace("/", "-"), expected=expected(),
                        tz_name=zone_name)
        outputs = {"markdown": read_markdown(result), "json": result.json_path.read_text(encoding="utf-8"),
                   "console": result.render()}
        for where, text in outputs.items():
            assert "ticket 4" not in text.lower(), (zone_name, where)


def test_the_api_words_the_notes_of_a_report_written_before_the_go_live_as_this_version_does(
    snapshot_store: Store, process: ProcessConfig, tmp_path: Path
) -> None:
    result = report(snapshot_store, process, tmp_path, expected=expected(), tz_name="UTC")
    assert result.json_path is not None
    payload = read_json(result)
    current = [row["note"] for row in payload["comparison"]]
    payload["metadata"]["comparison_note"] = (
        "ticket 4 used America/Chicago and this report uses UTC, so the window edges differ")
    old = {
        "total_issues": "ticket 4 counted during 2 Nov; the window end is midnight before it",
        "with_original_estimate": "original estimates set or cleared since ticket 4, or a different set of resolved "
                                  "issues",
        "with_logged_time": "ticket 4 may have counted logged time another way; other variants: aggregate 5, "
                            "worklog_rows 3",
    }
    for row in payload["comparison"]:
        row["note"] = old.get(row["metric"], row["note"])
    payload["comparison"].append({"metric": "made_up", "expected": 1, "actual": None, "match": False,
                                  "note": "not a baseline figure"})
    result.json_path.write_text(json.dumps(payload), encoding="utf-8")

    served = latest_report(tmp_path, "AIL", process.stages)

    assert served is not None
    assert [row["note"] for row in served["comparison"]] == [*current, "not a baseline figure"]
    assert served["metadata"]["comparison_note"] == (
        "the reference audit used America/Chicago and this report uses UTC, so the window edges differ")
    assert "ticket 4" not in json.dumps(served).lower()
