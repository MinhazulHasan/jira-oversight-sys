from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from oversight.cli import EXIT_CONFIG, EXIT_OK, main
from oversight.config import ProcessConfig
from oversight.derive.run import DeriveResult, _category_lookup, _status_categories, run_derive
from oversight.errors import ConfigError
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import UTC, to_iso

from tests.fixtures.derive import (
    CASES,
    LABELS,
    NOW,
    STATUSES,
    assert_intervals,
    assert_metrics,
    load_into,
    with_ops,
)

PROJECTS = ["AIL", "OPS"]
NO_WARNINGS = {
    "inconsistent_transitions": 0,
    "negative_intervals": 0,
    "unmapped_statuses": 0,
    "current_status_mismatch": 0,
    "missing_created_at": 0,
}


def fixed_now() -> datetime:
    return NOW


def _derive(store: Store, process: ProcessConfig, projects: list[str] = PROJECTS) -> DeriveResult:
    return run_derive(store, with_ops(process), projects=projects, now=fixed_now)


def _intervals(store: Store, key: str) -> list[dict]:
    rows = store.query(
        "SELECT status, stage, entered_at, left_at, hours FROM status_intervals "
        "WHERE issue_key = ? ORDER BY seq",
        (key,),
    )
    return [dict(row) for row in rows]


def _metrics_row(store: Store, key: str) -> dict | None:
    row = store.query_one("SELECT * FROM issue_metrics WHERE issue_key = ?", (key,))
    if row is None:
        return None
    metrics = dict(row)
    metrics["skipped_stages"] = json.loads(metrics["skipped_stages"])
    return metrics


def _snapshot(store: Store) -> tuple[list[tuple], list[tuple]]:
    intervals = store.query("SELECT * FROM status_intervals ORDER BY issue_key, seq")
    metrics = store.query("SELECT * FROM issue_metrics ORDER BY issue_key")
    return [tuple(r) for r in intervals], [tuple(r) for r in metrics]


def _add_issue(store: Store, key: str, created_at: str | None, status: str = "To Do", **extra: object) -> None:
    row = {
        "issue_key": key,
        "issue_id": f"id-{key}",
        "project_key": key.split("-")[0],
        "status": status,
        "created_at": created_at,
    }
    store.upsert("issues", row | extra)


def test_fixture_issues_match_the_labels(store: Store, process: ProcessConfig) -> None:
    load_into(store)
    result = _derive(store, process)

    expected = LABELS["run"]
    assert result.issues == expected["issues"]
    assert result.intervals == expected["intervals"] == store.count("status_intervals")
    assert result.warnings == expected["warnings"]
    assert store.count("issue_metrics") == len(CASES)
    for key, label in LABELS["issues"].items():
        assert_intervals(_intervals(store, key), label["intervals"])
        metrics = _metrics_row(store, key)
        assert metrics["computed_at"] == LABELS["now"]
        assert_metrics(metrics, label["metrics"])


def test_seq_keeps_the_timeline_order_when_entered_at_cannot(store: Store, process: ProcessConfig) -> None:
    load_into(store)
    _derive(store, process)
    rows = store.query(
        "SELECT seq, status, entered_at FROM status_intervals WHERE issue_key = 'AIL-113' ORDER BY seq"
    )
    assert [(r["seq"], r["status"]) for r in rows] == [(0, "To Do"), (1, "In Progress"), (2, "Done")]
    by_time = sorted(rows, key=lambda r: r["entered_at"])
    assert [r["status"] for r in by_time] == ["In Progress", "To Do", "Done"]
    gaps = store.query_one(
        "SELECT COUNT(*) FROM (SELECT issue_key, COUNT(*) AS n, MIN(seq) AS lo, MAX(seq) AS hi "
        "FROM status_intervals GROUP BY issue_key) WHERE lo != 0 OR hi != n - 1"
    )[0]
    assert gaps == 0


def test_rerunning_replaces_rather_than_duplicates(store: Store, process: ProcessConfig) -> None:
    load_into(store)
    first = _derive(store, process)
    before = _snapshot(store)
    second = _derive(store, process)
    assert _snapshot(store) == before
    assert (second.issues, second.intervals, second.warnings) == (first.issues, first.intervals, first.warnings)
    assert second.run_id != first.run_id


def test_rerunning_picks_up_new_history(store: Store, process: ProcessConfig) -> None:
    load_into(store)
    _derive(store, process)
    store.upsert(
        "status_changes",
        {
            "issue_key": "AIL-110",
            "changelog_id": "1102",
            "changed_at": "2026-11-04T10:00:00.000Z",
            "from_status": "In Progress",
            "to_status": "Done",
        },
    )
    store.upsert("issues", CASES["AIL-110"]["issue"] | {"status": "Done", "status_category": "done"})
    result = _derive(store, process)

    assert result.intervals == LABELS["run"]["intervals"] + 1
    assert [row["status"] for row in _intervals(store, "AIL-110")] == ["To Do", "In Progress", "Done"]
    metrics = _metrics_row(store, "AIL-110")
    assert metrics["done_at"] == "2026-11-04T10:00:00.000Z"
    assert metrics["cycle_time_h"] == pytest.approx(26.0)  # 11-03 08:00 -> 11-04 10:00
    assert metrics["status_age_h"] == pytest.approx(2.0)  # 10:00 -> now, 12:00


def test_orphan_rows_are_removed(store: Store, process: ProcessConfig) -> None:
    load_into(store)
    _derive(store, process)
    store.delete_rows("issues", {"issue_key": "AIL-101"})
    store.upsert("issue_metrics", {"issue_key": "GONE-1", "reopen_count": 3})
    store.conn.execute(
        "INSERT INTO status_intervals (issue_key, seq, status, entered_at) VALUES (?, 0, ?, ?)",
        ("GONE-1", "To Do", "2026-10-01T09:00:00.000Z"),
    )

    _derive(store, process)

    for key in ("AIL-101", "GONE-1"):
        assert store.count("status_intervals", {"issue_key": key}) == 0
        assert store.count("issue_metrics", {"issue_key": key}) == 0
    assert store.count("issue_metrics") == len(CASES) - 1
    stats = json.loads(store.last_run("derive")["stats_json"])
    assert stats["orphans_deleted"] == 3 + 1 + 1 + 1  # AIL-101's 3 intervals and metrics; GONE-1's two rows


def test_other_projects_are_left_alone(store: Store, process: ProcessConfig) -> None:
    load_into(store)
    _derive(store, process)
    ops_before = [_intervals(store, key) for key in ("OPS-1", "OPS-2", "OPS-3")]
    store.delete_rows("status_changes", {"issue_key": "OPS-3"})  # would shorten OPS-3's timeline

    result = _derive(store, process, ["AIL"])

    assert result.issues == {"AIL": 18}
    assert [_intervals(store, key) for key in ("OPS-1", "OPS-2", "OPS-3")] == ops_before


def test_an_empty_project_succeeds(store: Store, process: ProcessConfig) -> None:
    result = _derive(store, process, ["AIL"])
    assert (result.issues, result.intervals, result.warnings) == ({"AIL": 0}, 0, NO_WARNINGS)


def test_a_successful_run_is_recorded(store: Store, process: ProcessConfig) -> None:
    load_into(store)
    result = _derive(store, process)
    run = store.last_run("derive")
    assert run["run_id"] == result.run_id
    assert (run["status"], run["started_at"], run["finished_at"]) == ("succeeded", LABELS["now"], LABELS["now"])
    stats = json.loads(run["stats_json"])
    assert stats["issues"] == LABELS["run"]["issues"]
    assert stats["intervals"] == LABELS["run"]["intervals"]
    assert stats["warnings"] == LABELS["run"]["warnings"]
    assert stats["elapsed_s"] == result.elapsed_s >= 0


def test_a_failure_is_recorded_and_reraised(store: Store, process: ProcessConfig) -> None:
    load_into(store)
    _derive(store, process)
    before = _snapshot(store)
    store.upsert(
        "status_changes",
        {
            "issue_key": "AIL-105",
            "changelog_id": "1053",
            "changed_at": "not a timestamp",
            "from_status": "Done",
            "to_status": "To Do",
        },
    )

    with pytest.raises(ValueError, match="cannot derive AIL-105"):
        _derive(store, process)

    failed = store.last_run("derive", status="failed")
    assert failed is not None and failed["finished_at"] == LABELS["now"]
    assert "AIL-105" in json.loads(failed["stats_json"])["error"]
    assert _snapshot(store) == before  # the project's transaction rolled back


def test_an_unknown_project_is_a_config_error_and_changes_nothing(store: Store, process: ProcessConfig) -> None:
    load_into(store)
    with pytest.raises(ConfigError, match="XYZ"):
        _derive(store, process, ["AIL", "XYZ"])
    assert store.count("status_intervals") == store.count("issue_metrics") == 0
    failed = store.last_run("derive", status="failed")
    assert "ConfigError" in json.loads(failed["stats_json"])["error"]
    assert store.last_run("derive") is None  # nothing succeeded


def test_an_issue_without_created_at_is_skipped_with_a_warning(store: Store, process: ProcessConfig) -> None:
    _add_issue(store, "AIL-1", None)
    _add_issue(store, "AIL-2", "2026-11-04T08:00:00.000Z")
    store.upsert("issue_metrics", {"issue_key": "AIL-1", "reopen_count": 1})

    result = _derive(store, process, ["AIL"])

    assert result.issues == {"AIL": 1}
    assert result.warnings == NO_WARNINGS | {"missing_created_at": 1}
    assert store.count("issue_metrics", {"issue_key": "AIL-1"}) == 0
    assert store.count("status_intervals", {"issue_key": "AIL-1"}) == 0
    assert _metrics_row(store, "AIL-2")["status_age_h"] == pytest.approx(4.0)


def test_history_ending_away_from_the_current_status_is_a_warning(store: Store, process: ProcessConfig) -> None:
    _add_issue(store, "AIL-1", "2026-11-01T08:00:00.000Z", status="To Do")
    store.upsert(
        "status_changes",
        {
            "issue_key": "AIL-1",
            "changelog_id": "1",
            "changed_at": "2026-11-02T08:00:00.000Z",
            "from_status": "To Do",
            "to_status": "Done",
        },
    )
    result = _derive(store, process, ["AIL"])
    assert result.warnings == NO_WARNINGS | {"current_status_mismatch": 1}


def test_unmapped_statuses_are_counted_once_per_name(store: Store, process: ProcessConfig) -> None:
    _add_issue(store, "AIL-1", "2026-11-01T08:00:00.000Z", status="in review")
    _add_issue(store, "AIL-2", "2026-11-01T08:00:00.000Z", status="In Review")
    _add_issue(store, "AIL-3", "2026-11-01T08:00:00.000Z", status="Blocked")
    result = _derive(store, process, ["AIL"])
    assert result.warnings["unmapped_statuses"] == 2


def test_category_lookup_prefers_the_statuses_table(process: ProcessConfig) -> None:
    categories = _status_categories(STATUSES + [{"name": "Parked", "category": None}])
    issue = {"status": "Closed", "status_category": "done"}
    category_of = _category_lookup(categories, process.project("AIL"), process.done_stage, issue)

    assert category_of("in progress") == "indeterminate"  # table, ignoring case
    assert category_of("DONE") == "done"
    assert category_of("closed") == "done"  # not in the table: the issue's current status
    assert category_of("Parked") is None  # a blank category is no category
    assert category_of("Elsewhere") is None
    assert category_of(None) is None


def test_category_lookup_without_a_statuses_table(process: ProcessConfig) -> None:
    issue = {"status": "In Progress", "status_category": "indeterminate"}
    category_of = _category_lookup({}, process.project("AIL"), process.done_stage, issue)
    assert category_of("Done") == "done"  # maps to the done stage
    assert category_of("In Progress") == "indeterminate"  # the current status
    assert category_of("To Do") is None


def test_render_summarises_the_run() -> None:
    result = DeriveResult(
        run_id="abc123",
        issues={"AIL": 1134, "OPS": 3},
        intervals=4321,
        elapsed_s=1.234,
        warnings=NO_WARNINGS | {"unmapped_statuses": 2},
    )
    text = result.render()
    assert "abc123" in text and "4,321 status intervals" in text and "1.23 s" in text
    assert "AIL: 1,134 issues" in text and "OPS: 3 issues" in text
    assert "warnings: unmapped_statuses 2 (details in the log)" in text
    quiet = DeriveResult(run_id="r", issues={}, intervals=0, elapsed_s=0.0, warnings=NO_WARNINGS)
    assert "warnings: none" in quiet.render()


def test_cli_derive_rebuilds_the_snapshot(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    with Store.open(settings.db_file) as store:
        load_into(store)
    assert main(["derive"], settings=settings) == EXIT_OK
    assert "AIL: 18 issues" in capsys.readouterr().out
    with Store.open(settings.db_file) as store:
        assert store.count("issue_metrics") == 18  # OPS isn't in this process.yaml, so not derived
        assert store.last_run("derive")["status"] == "succeeded"


def test_cli_derive_reports_an_unknown_project(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["derive", "--project", "XYZ"], settings=settings) == EXIT_CONFIG
    assert "project 'XYZ' is not configured" in capsys.readouterr().err


PERF_ISSUES = 2_000
PERF_CYCLE = [
    ("To Do", "In Progress"),
    ("In Progress", "Done"),
    ("Done", "In Progress"),
    ("In Progress", "Done"),
    ("Done", "To Do"),
    ("To Do", "Done"),
]


@pytest.mark.slow
def test_a_whole_project_rebuilds_well_within_a_minute(
    store: Store, process: ProcessConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with store.transaction():
        store.upsert_many("statuses", [row for row in STATUSES if row["project_key"] == "AIL"])
        for n in range(PERF_ISSUES):
            key = f"AIL-{n + 1}"
            created = base + timedelta(hours=n)
            store.upsert(
                "issues",
                {
                    "issue_key": key,
                    "issue_id": str(100_000 + n),
                    "project_key": "AIL",
                    "status": "Done",
                    "status_category": "done",
                    "created_at": to_iso(created),
                    "story_points": 3.0,
                    "time_spent_s": 36_000,
                },
            )
            store.upsert_many(
                "status_changes",
                [
                    {
                        "issue_key": key,
                        "changelog_id": str(n * 10 + step),
                        "changed_at": to_iso(created + timedelta(hours=step + 1)),
                        "from_status": from_status,
                        "to_status": to_status,
                    }
                    for step, (from_status, to_status) in enumerate(PERF_CYCLE)
                ],
            )
            store.upsert(
                "field_changes",
                {
                    "issue_key": key,
                    "changelog_id": str(n * 10 + 9),
                    "field": "story_points",
                    "changed_at": to_iso(created + timedelta(minutes=30)),
                    "from_value": None,
                    "to_value": "3",
                },
            )

    started = time.perf_counter()
    result = run_derive(store, process, projects=["AIL"], now=fixed_now)
    elapsed = time.perf_counter() - started

    with capsys.disabled():
        print(f"\nrun_derive: {PERF_ISSUES:,} issues, {result.intervals:,} intervals in {elapsed:.2f} s")
    assert result.issues == {"AIL": PERF_ISSUES}
    assert result.intervals == PERF_ISSUES * (len(PERF_CYCLE) + 1)
    sample = _metrics_row(store, "AIL-1")
    assert (sample["reopen_count"], len(sample["skipped_stages"])) == (2, 1)
    assert elapsed < 30
