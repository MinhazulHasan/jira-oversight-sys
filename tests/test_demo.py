"""The demo project: deterministic, quick, consistent with a real sync, and its
story holds in the derived data (derive sees no anomaly at all).

The default demo (1,150 issues) is seeded and derived once per module; the
story tests only read it. Everything is offline and runs from a fixed `now`.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import time
import zlib
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

import pytest

from oversight.cli import EXIT_CONFIG, EXIT_OK, main
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import (
    ACCOUNT_TIMEZONE,
    ASSIGNEE_NAMES,
    EPIC_NAMES,
    MEMBER_NAMES,
    MEMBERS,
    PICTURED,
    POINTS_FIELD,
    SPRINT_FIELD,
    WORKLOG_AUTHOR,
    DemoStats,
    picture,
    seed_demo,
)
from oversight.derive.run import DeriveResult, run_derive
from oversight.errors import ConfigError
from oversight.jira.mapping import map_issue, map_issue_links
from oversight.settings import Settings
from oversight.stats import percentile
from oversight.store.db import Store
from oversight.timeutil import UTC, to_iso

from tests.conftest import CONFIG_FIXTURES, VIEWER_HEADERS

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
PROJECT = "DEMO"
ISSUES = 1150
# Every table the demo writes, with the order that makes a row dump comparable.
DEMO_TABLES: dict[str, str] = {
    "statuses": "project_key, status_id",
    "issues": "issue_key",
    "status_changes": "issue_key, changelog_id",
    "field_changes": "issue_key, changelog_id, field",
    "worklogs": "worklog_id",
    "links": "issue_key, kind, target",
    "board_members": "account_id",
    "sync_state": "scope",
}
ACCOUNT_ID = re.compile(r"^acc-\d{2}$")
# Two capitalised words in a row read like a person's name; only the workflow's status names and the
# accounts' fictional display names (ASSIGNEE_NAMES, MEMBER_NAMES) look like that.
NAME_LIKE = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")
NOT_NAMES = {"To Do", "In Progress"}


def fixed_now() -> datetime:
    return NOW


def load_process() -> ProcessConfig:
    return ProcessConfig.load(CONFIG_FIXTURES / "process.yaml")


def dump(store: Store, project: str = PROJECT) -> dict[str, list[tuple]]:
    """Every demo table's rows for the project, in a fixed order; sync_state without its run id."""
    out: dict[str, list[tuple]] = {}
    for table, order in DEMO_TABLES.items():
        if table == "sync_state":
            rows = store.query("SELECT scope, last_synced_at, account_timezone FROM sync_state WHERE scope = ?", (project,))
        elif table in ("issues", "statuses", "board_members"):
            rows = store.query(f"SELECT * FROM {table} WHERE project_key = ? ORDER BY {order}", (project,))
        else:
            rows = store.query(
                f"SELECT * FROM {table} WHERE issue_key IN (SELECT issue_key FROM issues WHERE project_key = ?) "
                f"ORDER BY {order}",
                (project,),
            )
        out[table] = [tuple(row) for row in rows]
    return out


def raw_issues(store: Store) -> dict[str, dict]:
    return {row["issue_key"]: json.loads(row["raw_json"]) for row in store.query("SELECT issue_key, raw_json FROM issues")}


def sprint_objects(store: Store) -> dict[int, dict]:
    """The sprint objects in raw_json, de-duplicated by id (as the rules build reads them)."""
    sprints: dict[int, dict] = {}
    for raw in raw_issues(store).values():
        for sprint in raw["fields"].get(SPRINT_FIELD) or []:
            sprints.setdefault(sprint["id"], sprint)
    return sprints


def share(part: int, whole: int) -> float:
    assert whole > 0
    return part / whole


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Store, DemoStats, DeriveResult]]:
    """The default demo, seeded once and derived once; the story tests only read it."""
    store = Store.open(tmp_path_factory.mktemp("demo") / "oversight.db")
    stats = seed_demo(store, now=NOW)
    result = run_derive(store, load_process(), projects=[PROJECT], now=fixed_now)
    yield store, stats, result
    store.close()


# --- deterministic, quick, idempotent ---------------------------------------------------------------


def test_the_same_now_and_seed_give_byte_identical_tables(tmp_path: Path) -> None:
    with Store.open(tmp_path / "a.db") as first, Store.open(tmp_path / "b.db") as second:
        seed_demo(first, now=NOW)
        seed_demo(second, now=NOW)
        assert dump(first) == dump(second)
        assert dump(first)["issues"]  # the comparison was not between two empty dumps

        # A different seed tells a different story in the same shape.
        with Store.open(tmp_path / "c.db") as other:
            seed_demo(other, now=NOW, seed=7)
            assert other.count("issues") == first.count("issues")
            assert dump(other)["issues"] != dump(first)["issues"]


def test_seeding_again_without_reset_changes_nothing(store: Store) -> None:
    first = seed_demo(store, now=NOW, issues=120)
    before = dump(store)
    again = seed_demo(store, now=NOW, issues=120)
    assert again == first
    assert dump(store) == before


def test_seeding_the_default_demo_takes_under_ten_seconds(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    started = time.perf_counter()
    stats = seed_demo(store, now=NOW)
    elapsed = time.perf_counter() - started
    with capsys.disabled():
        print(f"\nseed_demo: {stats.issues:,} issues in {elapsed:.2f} s")
    assert stats.issues == ISSUES
    assert elapsed < 10


# --- reset --------------------------------------------------------------------------------------------


def _unrelated_project(store: Store) -> None:
    """An AIL issue with child, derived and findings rows: a reset of DEMO must not touch it."""
    with store.transaction():
        store.upsert(
            "issues",
            {"issue_key": "AIL-1", "issue_id": "1", "project_key": "AIL", "status": "Done", "created_at": "2026-10-01T09:00:00.000Z"},
        )
        store.upsert("worklogs", {"worklog_id": "w-ail", "issue_key": "AIL-1", "seconds": 3600})
        store.upsert("statuses", {"project_key": "AIL", "status_id": "1", "name": "Done", "category": "done"})
        store.set_cursor("AIL", last_synced_at="2026-11-01T00:00:00.000Z", run_id="r-ail", account_timezone="UTC")
        store.conn.execute("INSERT INTO status_intervals VALUES ('AIL-1', 0, 'Done', 'done', '2026-10-01T09:00:00.000Z', NULL, NULL)")
        store.upsert("issue_metrics", {"issue_key": "AIL-1", "reopen_count": 0})
        store.upsert("board_members", {"project_key": "AIL", "account_id": "acc-ail", "seen_at": "2026-11-01T00:00:00.000Z"})


def _finding(store: Store, finding_id: str, project: str, issue_key: str) -> None:
    store.upsert(
        "findings",
        {
            "finding_id": finding_id,
            "rule_id": "R-FLOW-02",
            "rule_version": "0.1.0",
            "issue_key": issue_key,
            "project_key": project,
            "kind": "rule",
            "severity": "breach",
            "evidence_key": "skip-1",
        },
    )


def test_reset_removes_the_previous_rows_and_nothing_else(store: Store) -> None:
    seed_demo(store, now=NOW, issues=80)
    run_derive(store, load_process(), projects=[PROJECT], now=fixed_now)
    _unrelated_project(store)
    _finding(store, "f-demo", PROJECT, "DEMO-3")
    _finding(store, "f-ail", "AIL", "AIL-1")
    with store.transaction():
        store.upsert("rule_status", {"run_id": "r1", "project_key": PROJECT, "rule_id": "R-FLOW-02", "rule_version": "0.1.0", "measurable": 1})
        store.upsert("rule_status", {"run_id": "r1", "project_key": "AIL", "rule_id": "R-FLOW-02", "rule_version": "0.1.0", "measurable": 1})
        store.upsert("warning_links", {"warning_finding_id": "f-demo", "breach_finding_id": "f-demo", "linked_at": "2026-11-01T00:00:00.000Z"})
        store.upsert("warning_links", {"warning_finding_id": "f-ail", "breach_finding_id": "f-ail", "linked_at": "2026-11-01T00:00:00.000Z"})
        store.upsert("digests", {"digest_id": "d-demo", "project_key": PROJECT, "channel": "file", "sent_at": "2026-11-01T00:00:00.000Z"})
        store.upsert("insights", {"insight_id": "i-demo", "project_key": PROJECT, "week_of": "2026-11-02", "generated_at": "2026-11-01T00:00:00.000Z", "verified": 1})
    ail_before = dump(store, "AIL")
    assert ail_before["issues"] and ail_before["worklogs"] and ail_before["sync_state"] and ail_before["board_members"]

    stats = seed_demo(store, now=NOW, seed=7, issues=50, reset=True)

    assert stats.issues == 50
    assert store.count("issues", {"project_key": PROJECT}) == 50
    keys = store.issue_keys(PROJECT)
    assert keys == {f"DEMO-{n}" for n in range(1, 51)}
    for table in ("status_changes", "field_changes", "worklogs", "links", "status_intervals", "issue_metrics"):
        stray = store.query(f"SELECT DISTINCT issue_key FROM {table} WHERE issue_key LIKE 'DEMO-%'")
        assert {row["issue_key"] for row in stray} <= keys, table
    # The previous DEMO's derived rows went with it; derive has not run on the new one yet.
    assert store.query("SELECT COUNT(*) FROM status_intervals WHERE issue_key LIKE 'DEMO-%'")[0][0] == 0
    assert store.count("findings", {"project_key": PROJECT}) == 0
    assert store.count("rule_status", {"project_key": PROJECT}) == 0
    assert store.count("warning_links", {"warning_finding_id": "f-demo"}) == 0
    assert store.count("digests", {"project_key": PROJECT}) == 0
    assert store.count("insights", {"project_key": PROJECT}) == 0
    # The members are DEMO's again, as the new seeding stored them; AIL's are not touched.
    assert store.count("board_members", {"project_key": PROJECT}) == len(ASSIGNEE_NAMES) + len(MEMBERS)
    # AIL is exactly as it was, findings and all.
    assert dump(store, "AIL") == ail_before
    assert store.count("findings", {"project_key": "AIL"}) == 1
    assert store.count("rule_status", {"project_key": "AIL"}) == 1
    assert store.count("warning_links", {"warning_finding_id": "f-ail"}) == 1
    assert store.query_one("SELECT * FROM status_intervals WHERE issue_key = 'AIL-1'") is not None
    assert store.count("issue_metrics", {"issue_key": "AIL-1"}) == 1
    assert store.get_cursor("AIL").last_run_id == "r-ail"


def test_without_reset_a_smaller_demo_leaves_the_extra_issues(store: Store) -> None:
    seed_demo(store, now=NOW, issues=80)
    seed_demo(store, now=NOW, issues=50)
    assert store.count("issues", {"project_key": PROJECT}) == 80


# --- the story, in the derived data ------------------------------------------------------------------


def test_counts_and_render_match_the_tables(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    store, stats, _ = seeded
    assert stats.project == PROJECT
    assert stats.issues == store.count("issues") == ISSUES
    assert stats.status_changes == store.count("status_changes")
    assert stats.field_changes == store.count("field_changes")
    assert stats.worklogs == store.count("worklogs")
    assert stats.links == store.count("links")
    assert stats.sprints == sorted(stats.sprints) and len(set(stats.sprints)) == len(stats.sprints)
    assert set(sprint_objects(store)) <= set(stats.sprints)
    text = stats.render()
    assert text.startswith(f"demo project DEMO: {ISSUES:,} issues, {stats.status_changes:,} status changes")
    assert f"{stats.worklogs:,} worklogs, {stats.links:,} links" in text
    assert f"sprints ({len(stats.sprints)}): {stats.sprints[0]}, " in text


def test_derive_sees_a_clean_history(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    store, _, result = seeded
    assert result.issues == {PROJECT: ISSUES}
    assert all(count == 0 for count in result.warnings.values()), result.warnings
    assert store.count("issue_metrics") == ISSUES
    floor = to_iso(NOW - timedelta(days=183))
    now = to_iso(NOW)
    assert store.query_one("SELECT COUNT(*) FROM issues WHERE created_at < ? OR created_at > ?", (floor, now))[0] == 0
    assert store.query_one("SELECT COUNT(*) FROM issues WHERE updated_at < created_at OR updated_at > ?", (now,))[0] == 0
    assert store.query_one(
        "SELECT COUNT(*) FROM status_changes sc JOIN issues i USING (issue_key) "
        "WHERE sc.changed_at <= i.created_at OR sc.changed_at > ?",
        (now,),
    )[0] == 0
    assert store.query_one(
        "SELECT COUNT(*) FROM worklogs w JOIN issues i USING (issue_key) "
        "WHERE w.started_at < i.created_at OR w.started_at > ? OR w.updated_at < w.started_at OR w.updated_at > ?",
        (now, now),
    )[0] == 0
    # resolved_at is the moment of the last move into Done, and only Done issues have one.
    assert store.query_one("SELECT COUNT(*) FROM issues WHERE (status = 'Done') != (resolved_at IS NOT NULL)")[0] == 0
    assert store.query_one(
        "SELECT COUNT(*) FROM issues i WHERE status = 'Done' AND resolved_at != "
        "(SELECT MAX(changed_at) FROM status_changes sc WHERE sc.issue_key = i.issue_key AND sc.to_status = 'Done')"
    )[0] == 0
    ids = [row[0] for row in store.query("SELECT changelog_id FROM status_changes UNION ALL SELECT changelog_id FROM field_changes")]
    assert len(ids) == len(set(ids)) and all(i.isdigit() for i in ids)


def test_types_epics_and_hierarchy(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    store, _, _ = seeded
    by_type = {row[0]: row[1] for row in store.query("SELECT issue_type, COUNT(*) FROM issues GROUP BY issue_type")}
    assert by_type["Epic"] == 14
    assert 0.55 <= share(by_type["Task"], ISSUES) <= 0.70
    assert 0.15 <= share(by_type["Bug"], ISSUES) <= 0.25
    assert 0.08 <= share(by_type["Subtask"], ISSUES) <= 0.16
    assert 0.02 <= share(by_type["Test"], ISSUES) <= 0.08

    epics = store.query("SELECT issue_key, summary FROM issues WHERE issue_type = 'Epic'")
    assert {row["summary"] for row in epics} == set(EPIC_NAMES)
    for epic in epics:
        children = store.query("SELECT issue_type FROM issues WHERE parent_key = ? AND epic_key = ?", (epic["issue_key"], epic["issue_key"]))
        assert len(children) >= 5, epic["summary"]
        assert {row["issue_type"] for row in children} <= {"Task", "Bug", "Test"}
    work = store.query_one("SELECT COUNT(*) FROM issues WHERE issue_type IN ('Task', 'Bug', 'Test')")[0]
    under_epic = store.query_one("SELECT COUNT(*) FROM issues WHERE issue_type IN ('Task', 'Bug', 'Test') AND epic_key IS NOT NULL")[0]
    assert 0.75 <= share(under_epic, work) <= 0.95
    # A subtask hangs under a task, which keeps the epic link; the subtask itself has none.
    subtasks = store.query(
        "SELECT s.epic_key, p.issue_type AS parent_type, p.created_at < s.created_at AS parent_first "
        "FROM issues s LEFT JOIN issues p ON p.issue_key = s.parent_key WHERE s.issue_type = 'Subtask'"
    )
    assert subtasks
    assert all(row["parent_type"] == "Task" and row["epic_key"] is None and row["parent_first"] for row in subtasks)
    assert store.query_one("SELECT COUNT(*) FROM issues WHERE issue_type = 'Epic' AND parent_key IS NOT NULL")[0] == 0


def test_estimates_are_points_on_about_two_thirds_and_hours_on_none(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    store, _, _ = seeded
    with_points = store.query_one("SELECT COUNT(*) FROM issues WHERE story_points IS NOT NULL")[0]
    assert 0.58 <= share(with_points, ISSUES) <= 0.72
    assert store.query_one("SELECT COUNT(*) FROM issues WHERE original_estimate_s IS NOT NULL OR remaining_estimate_s IS NOT NULL")[0] == 0
    values = {row[0] for row in store.query("SELECT DISTINCT story_points FROM issues WHERE story_points IS NOT NULL")}
    assert values <= {0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0} and len(values) >= 5
    assert store.query_one("SELECT COUNT(*) FROM issues WHERE issue_type = 'Epic' AND story_points IS NOT NULL")[0] == 0
    basis = store.query(
        "SELECT i.story_points IS NOT NULL AS pointed, m.estimate_basis FROM issues i JOIN issue_metrics m USING (issue_key)"
    )
    assert all((row["estimate_basis"] == "points") == bool(row["pointed"]) for row in basis)
    # The points history: most set before the start, some after it (R-EST-02's material), a few revised.
    fields = {row[0]: row[1] for row in store.query("SELECT field, COUNT(*) FROM field_changes GROUP BY field")}
    assert set(fields) == {"story_points", "assignee"} and fields["story_points"] > 500 and fields["assignee"] > 500
    assert all(
        row[0] is None or re.fullmatch(r"\d+(\.5)?", row[0])
        for row in store.query("SELECT to_value FROM field_changes WHERE field = 'story_points'")
    )
    late = store.query_one("SELECT COUNT(*) FROM issue_metrics WHERE estimate_set_at > started_at")[0]
    assert 20 <= late <= 120
    revised = store.query_one("SELECT COUNT(*) FROM field_changes WHERE field = 'story_points' AND from_value IS NOT NULL")[0]
    assert 10 <= revised <= 100


def test_logged_time_is_tempo_worklogs_on_most_done_work(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    store, _, _ = seeded
    done = store.query_one("SELECT COUNT(*) FROM issues WHERE status = 'Done'")[0]
    logged = store.query_one("SELECT COUNT(*) FROM issues WHERE status = 'Done' AND time_spent_s > 0")[0]
    assert 0.78 <= share(logged, done) <= 0.92
    assert {row[0] for row in store.query("SELECT DISTINCT author_id FROM worklogs")} == {WORKLOG_AUTHOR}
    assert store.query_one("SELECT COUNT(*) FROM worklogs WHERE seconds <= 0 OR seconds % 900 != 0")[0] == 0
    # The issue's time columns agree with its worklogs; a parent's aggregate includes its subtasks.
    assert store.query_one(
        "SELECT COUNT(*) FROM issues i WHERE COALESCE(time_spent_s, 0) != "
        "(SELECT COALESCE(SUM(seconds), 0) FROM worklogs w WHERE w.issue_key = i.issue_key)"
    )[0] == 0
    assert store.query_one(
        "SELECT COUNT(*) FROM issues i WHERE COALESCE(aggregate_time_spent_s, 0) != COALESCE(time_spent_s, 0) + "
        "(SELECT COALESCE(SUM(s.time_spent_s), 0) FROM issues s WHERE s.parent_key = i.issue_key AND s.issue_type = 'Subtask')"
    )[0] == 0
    assert store.query_one("SELECT COUNT(*) FROM issues WHERE status = 'To Do' AND time_spent_s IS NOT NULL")[0] == 0


def test_skipped_stages_fell_after_the_process_change(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    store, _, _ = seeded
    skipped = (
        "EXISTS (SELECT 1 FROM status_changes sc WHERE sc.issue_key = i.issue_key "
        "AND sc.from_status = 'To Do' AND sc.to_status = 'Done')"
    )
    done = store.query_one("SELECT COUNT(*) FROM issues i WHERE status = 'Done'")[0]
    skips = store.query_one(f"SELECT COUNT(*) FROM issues i WHERE status = 'Done' AND {skipped}")[0]
    assert 0.11 <= share(skips, done) <= 0.21
    change = to_iso(NOW - timedelta(days=35))
    before = store.query_one(f"SELECT COUNT(*), SUM({skipped}) FROM issues i WHERE status = 'Done' AND resolved_at < ?", (change,))
    after = store.query_one(f"SELECT COUNT(*), SUM({skipped}) FROM issues i WHERE status = 'Done' AND resolved_at >= ?", (change,))
    assert share(before[1], before[0]) >= 2 * share(after[1], after[0])
    # derive records each skip against the stage it jumped over.
    metrics = store.query_one(
        "SELECT COUNT(*) FROM issue_metrics m JOIN issues i USING (issue_key) "
        "WHERE i.status = 'Done' AND m.skipped_stages LIKE '%\"development\"%'"
    )[0]
    assert metrics == skips


def test_effort_ratios_spread_around_one_and_two_epics_drift(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    store, _, _ = seeded
    rows = store.query(
        "SELECT i.epic_key, i.resolved_at, m.effort_ratio FROM issue_metrics m JOIN issues i USING (issue_key) "
        "WHERE i.status = 'Done' AND i.story_points IS NOT NULL AND i.time_spent_s > 0"
    )
    ratios = [row["effort_ratio"] for row in rows]
    assert len(ratios) >= 300 and all(ratio is not None for ratio in ratios)
    assert 0.8 <= median(ratios) <= 1.3
    assert sum(ratio > 2.0 for ratio in ratios) >= 5
    assert sum(ratio >= 1.5 for ratio in ratios) >= 30  # R-EFF-01 has work to do
    assert max(ratios) <= 6.0  # a long tail, not nonsense
    # Two epics drift upwards in the last five weeks: their recent median is well above the rest.
    change = to_iso(NOW - timedelta(days=35))
    recent: dict[str, list[float]] = {}
    for row in rows:
        if row["epic_key"] and row["resolved_at"] >= change:
            recent.setdefault(row["epic_key"], []).append(row["effort_ratio"])
    drifting = [epic for epic, values in recent.items() if len(values) >= 5 and median(values) >= 1.4]
    assert len(drifting) >= 2
    steady = [median(values) for epic, values in recent.items() if epic not in drifting and len(values) >= 3]
    assert steady and median(steady) < 1.3


def test_exactly_one_active_sprint_ending_in_two_days_with_a_third_not_started(
    seeded: tuple[Store, DemoStats, DeriveResult],
) -> None:
    store, stats, _ = seeded
    sprints = sprint_objects(store)
    assert {sprint["state"] for sprint in sprints.values()} == {"closed", "active", "future"}
    active = [sprint for sprint in sprints.values() if sprint["state"] == "active"]
    assert len(active) == 1
    sprint = active[0]
    assert to_iso(NOW + timedelta(days=1)) <= sprint["endDate"] <= to_iso(NOW + timedelta(days=3))
    assert sprint["startDate"] < to_iso(NOW) and sprint["name"] and "completeDate" not in sprint
    assert all("completeDate" in s for s in sprints.values() if s["state"] == "closed")
    assert all(s["startDate"] > to_iso(NOW) for s in sprints.values() if s["state"] == "future")
    members = store.query(
        "SELECT i.issue_key, i.status_category, i.story_points, i.sprint_ids, m.started_at "
        "FROM issues i JOIN issue_metrics m USING (issue_key)"
    )
    members = [row for row in members if sprint["id"] in json.loads(row["sprint_ids"] or "[]")]
    assert len(members) >= 100
    not_started = [row for row in members if row["status_category"] != "done" and row["started_at"] is None]
    assert 0.30 <= share(len(not_started), len(members)) <= 0.50
    assert any(row["story_points"] is None for row in members if row["status_category"] != "done")  # W-EST-01's material
    # The sprint_ids column and the sprint objects agree, and every closed sprint has its history.
    for key, raw in raw_issues(store).items():
        ids = sorted(s["id"] for s in raw["fields"].get(SPRINT_FIELD) or [])
        assert json.loads(store.query_one("SELECT sprint_ids FROM issues WHERE issue_key = ?", (key,))[0]) == ids
    assert sprint["id"] in stats.sprints


def test_hygiene_reopens_and_unassigned_work(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    store, _, _ = seeded
    unassigned = store.query_one(
        "SELECT COUNT(*) FROM issues WHERE status = 'In Progress' AND assignee_id IS NULL AND issue_type != 'Epic'"
    )[0]
    assert 3 <= unassigned <= 30
    reopened = store.query_one("SELECT COUNT(*), SUM(reopen_count) FROM issue_metrics WHERE reopen_count > 0")
    assert 5 <= reopened[0] <= 60 and reopened[1] >= reopened[0]
    assert store.query_one(
        "SELECT COUNT(*) FROM status_changes WHERE from_status = 'Done' AND to_status = 'In Progress'"
    )[0] == reopened[1]
    # Some of the reopened work is open again right now.
    assert store.query_one(
        "SELECT COUNT(*) FROM issues i WHERE status = 'In Progress' AND EXISTS "
        "(SELECT 1 FROM status_changes sc WHERE sc.issue_key = i.issue_key AND sc.from_status = 'Done')"
    )[0] >= 1
    # Early warnings have something to find too: effort burning fast on open work.
    assert store.query_one(
        "SELECT COUNT(*) FROM issue_metrics m JOIN issues i USING (issue_key) "
        "WHERE i.status = 'In Progress' AND m.effort_ratio >= 0.8"
    )[0] >= 5


def test_about_a_tenth_of_open_issues_sit_beyond_the_p90_of_their_status(
    seeded: tuple[Store, DemoStats, DeriveResult],
) -> None:
    store, _, _ = seeded
    since = to_iso(NOW - timedelta(days=180))
    thresholds: dict[str, float] = {}
    for status in ("To Do", "In Progress"):
        hours = [
            row[0]
            for row in store.query(
                "SELECT hours FROM status_intervals WHERE status = ? AND left_at IS NOT NULL AND left_at >= ?",
                (status, since),
            )
        ]
        assert len(hours) >= 20, status  # enough history for W-AGE-01 to set a threshold
        thresholds[status] = percentile(hours, 90)
    open_issues = store.query(
        "SELECT i.status, m.status_age_h FROM issues i JOIN issue_metrics m USING (issue_key) "
        "WHERE i.status_category != 'done' AND i.issue_type != 'Epic'"
    )
    beyond = sum(1 for row in open_issues if row["status_age_h"] > thresholds[row["status"]])
    assert 0.05 <= share(beyond, len(open_issues)) <= 0.25


def test_accounts_carry_only_their_fictional_names(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    """People are shown by Jira display name, so the demo's accounts have made-up ones (the brief's,
    in this order), and nothing else of a person: no email address, no avatar, no real name."""
    store, _, _ = seeded
    assert list(ASSIGNEE_NAMES.items()) == [
        ("acc-01", "Ada Brooks"), ("acc-02", "Ben Okafor"), ("acc-03", "Carmen Ruiz"), ("acc-04", "Dev Malhotra"),
        ("acc-05", "Elif Aydin"), ("acc-06", "Finn Gallagher"), ("acc-07", "Grace Lindqvist"), ("acc-08", "Hana Mori"),
    ]
    assignees = {row[0] for row in store.query("SELECT DISTINCT assignee_id FROM issues WHERE assignee_id IS NOT NULL")}
    assert assignees == set(ASSIGNEE_NAMES)  # every account has work at 1,150 issues
    authors = {row[0] for row in store.query("SELECT DISTINCT author_id FROM status_changes")}
    assert authors and all(ACCOUNT_ID.match(account) for account in authors)
    assert {row[0] for row in store.query("SELECT DISTINCT to_value FROM field_changes WHERE field = 'assignee'")} <= assignees
    for raw in raw_issues(store).values():
        text = json.dumps(raw)
        assert "emailAddress" not in text and "avatarUrls" not in text
        assignee = raw["fields"]["assignee"]
        assert assignee is None or assignee["displayName"] == ASSIGNEE_NAMES[assignee["accountId"]]
    # The people table holds each assignee and member as a sync would: the display name, active, seen at the
    # seeding, and no picture URL (two have a picture drawn in code: the test after this one).
    people = {row["account_id"]: tuple(row)[1:] for row in store.query(
        "SELECT account_id, display_name, active, updated_at, avatar_url FROM people")}
    assert people == {account: (name, 1, to_iso(NOW), None) for account, name in {**ASSIGNEE_NAMES, **MEMBER_NAMES}.items()}
    names = NOT_NAMES | set(ASSIGNEE_NAMES.values()) | set(MEMBER_NAMES.values())
    for table in (*DEMO_TABLES, "people"):
        for row in store.query(f"SELECT * FROM {table}"):
            for value in row:
                if isinstance(value, str):
                    assert not (set(NAME_LIKE.findall(value)) - names), (table, value[:120])


def test_the_board_has_three_members_with_no_tickets_and_no_app_account(
    seeded: tuple[Store, DemoStats, DeriveResult],
) -> None:
    """DEMO's members, as a sync's member fetch stores them: every assignee and three fictional people who hold none
    of its tickets (no pictures), seen at the seeding. The worklogs' app account is in the list the demo maps, and
    the mapping keeps it out, as every app account."""
    store, _, _ = seeded
    assert list(MEMBER_NAMES.items()) == [("acc-09", "Noor Haddad"), ("acc-10", "Tomás Ferreira"), ("acc-11", "Yuki Tanabe")]
    members = {row[0]: row[1] for row in store.query(
        "SELECT account_id, seen_at FROM board_members WHERE project_key = ?", (PROJECT,))}
    assert members == {account: to_iso(NOW) for account in (*ASSIGNEE_NAMES, *MEMBERS)}
    assignees = {row[0] for row in store.query("SELECT DISTINCT assignee_id FROM issues WHERE assignee_id IS NOT NULL")}
    assert assignees.isdisjoint(MEMBERS)  # no tickets, on any board
    assert WORKLOG_AUTHOR not in members and store.count("people", {"account_id": WORKLOG_AUTHOR}) == 0
    for account in MEMBERS:
        row = store.query_one("SELECT avatar, avatar_hash, avatar_url FROM people WHERE account_id = ?", (account,))
        assert tuple(row) == (None, None, None)  # initials


def decode_png(data: bytes) -> tuple[int, int, list[tuple[int, ...]]]:
    """A PNG's size and pixels, checked chunk by chunk: the signature, every CRC, 8-bit RGB, rows unfiltered."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    at, chunks = 8, {}
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        kind, body = data[at + 4 : at + 8], data[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", data[at + 8 + length : at + 12 + length])
        assert crc == zlib.crc32(kind + body), kind
        chunks[kind] = chunks.get(kind, b"") + body
        at += 12 + length
    width, height, depth, colour, _compression, _filter, interlace = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, colour, interlace) == (8, 2, 0) and chunks.get(b"IEND") == b""
    raw = zlib.decompress(chunks[b"IDAT"])
    stride = 1 + width * 3
    assert len(raw) == height * stride and all(raw[y * stride] == 0 for y in range(height))
    pixels = [tuple(raw[y * stride + 1 + 3 * x : y * stride + 4 + 3 * x]) for y in range(height) for x in range(width)]
    return width, height, pixels


def test_two_accounts_have_a_picture_drawn_in_code(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    """Two of the eight have a picture, so the dashboard shows both a picture and initials: an abstract pattern
    made in code as valid PNG bytes, stored as a sync stores a fetched one, but with no URL: nothing was fetched."""
    store, _, _ = seeded
    rows = {row["account_id"]: row for row in store.query("SELECT * FROM people")}
    assert {account for account, row in rows.items() if row["avatar"] is not None} == set(PICTURED) == {"acc-01", "acc-05"}
    for account in PICTURED:
        row = rows[account]
        assert row["avatar_type"] == "image/png" and row["avatar_hash"] == hashlib.sha256(row["avatar"]).hexdigest()[:12]
        assert (row["avatar_url"], row["avatar_source"], row["avatar_fetched_at"]) == (None, None, to_iso(NOW))
        width, height, pixels = decode_png(row["avatar"])
        assert (width, height) == (48, 48)
        assert len(set(pixels)) == 2  # one colour on a pale ground: a pattern, not a photo
        assert pixels[0] == pixels[-1] and pixels[47] == pixels[48 * 47]  # the ground frames it
        assert all(pixels[y * 48 + x] == pixels[y * 48 + 47 - x] for y in range(48) for x in range(24))  # mirrored
    for account in set(ASSIGNEE_NAMES) - set(PICTURED):  # the other six get initials
        assert [rows[account][column] for column in ("avatar", "avatar_type", "avatar_hash", "avatar_fetched_at")] == [
            None] * 4
    assert picture("acc-01") == picture("acc-01") != picture("acc-05")  # the same every time, and one each


def test_rows_match_their_raw_payload_through_the_real_mapping(
    seeded: tuple[Store, DemoStats, DeriveResult], fields: FieldsConfig
) -> None:
    store, _, _ = seeded
    assert (fields.id_of("story_points"), fields.id_of("sprint")) == (POINTS_FIELD, SPRINT_FIELD)
    links_from_raw = []
    for row in store.query("SELECT * FROM issues"):
        raw = json.loads(row["raw_json"])
        stored = dict(row)
        stored["sprint_ids"] = json.loads(stored["sprint_ids"]) if stored["sprint_ids"] is not None else None
        mapped = map_issue(raw, fields)
        assert mapped.pop("raw_json") == raw
        assert {key: stored[key] for key in mapped} == mapped, row["issue_key"]
        links_from_raw += map_issue_links(raw)
    stored_links = [dict(row) for row in store.query("SELECT * FROM links ORDER BY issue_key, kind, target")]
    assert sorted(links_from_raw, key=lambda r: (r["issue_key"], r["kind"], r["target"])) == stored_links
    assert {row["title"] for row in stored_links} == {"blocks", "is blocked by", "relates to"}
    assert 20 <= len(stored_links) <= 120
    # The raw timestamps are Jira's form in the account's zone, and the sprint field matches fields.yaml.
    sample = json.loads(store.query_one("SELECT raw_json FROM issues WHERE sprint_ids != '[]' AND sprint_ids IS NOT NULL")[0])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4}", sample["fields"]["created"])
    assert {"id", "name", "state", "startDate", "endDate", "boardId", "goal"} <= set(sample["fields"][SPRINT_FIELD][0])
    assert sample["fields"]["timeoriginalestimate"] is None


def test_statuses_sync_state_and_the_run_record(seeded: tuple[Store, DemoStats, DeriveResult]) -> None:
    store, stats, _ = seeded
    statuses = {row["name"]: row["category"] for row in store.query("SELECT name, category FROM statuses WHERE project_key = ?", (PROJECT,))}
    assert statuses == {"To Do": "new", "In Progress": "indeterminate", "Done": "done"}
    assert {row[0] for row in store.query("SELECT DISTINCT status FROM issues")} == set(statuses)
    cursor = store.get_cursor(PROJECT)
    assert cursor is not None
    assert cursor.last_synced_at == to_iso(NOW) and cursor.account_timezone == ACCOUNT_TIMEZONE
    run = store.query_one("SELECT * FROM runs WHERE run_id = ?", (cursor.last_run_id,))
    assert run is not None and run["kind"] == "demo" and run["status"] == "succeeded"
    assert run["started_at"] == to_iso(NOW)
    recorded = json.loads(run["stats_json"])
    assert recorded["issues"] == ISSUES and recorded["seed"] == 20261104 and recorded["sprints"] == stats.sprints


# --- edges, errors, other sizes and keys -------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 5, 30])
def test_small_demos_seed_and_derive(store: Store, count: int) -> None:
    stats = seed_demo(store, now=NOW, issues=count)
    assert stats.issues == store.count("issues") == count
    assert store.count("issues", {"issue_type": "Epic"}) == (1 if count >= 20 else 0)
    assert store.count("statuses", {"project_key": PROJECT}) == 3
    result = run_derive(store, load_process(), projects=[PROJECT], now=fixed_now)
    assert result.issues == {PROJECT: count} and all(v == 0 for v in result.warnings.values())


def test_bad_inputs_are_refused_before_anything_is_written(store: Store) -> None:
    with pytest.raises(ConfigError, match="--issues"):
        seed_demo(store, now=NOW, issues=0)
    with pytest.raises(ValueError, match="aware"):
        seed_demo(store, now=datetime(2026, 11, 4, 12, 0))
    assert store.count("issues") == 0 and store.count("runs") == 0


def test_a_failed_seeding_leaves_a_failed_run_and_no_rows(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(Store, "replace_rows", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        seed_demo(store, now=NOW, issues=20)
    assert store.count("issues") == 0 and store.get_cursor(PROJECT) is None
    run = store.query_one("SELECT status, stats_json FROM runs WHERE kind = 'demo'")
    assert run["status"] == "failed" and "disk full" in run["stats_json"]


def test_two_demo_projects_share_a_database(store: Store) -> None:
    seed_demo(store, now=NOW, issues=60)
    other = seed_demo(store, now=NOW, issues=60, project="TRIAL")
    assert other.project == "TRIAL" and store.issue_keys("TRIAL") == {f"TRIAL-{n}" for n in range(1, 61)}
    assert store.count("issues") == 120
    ids = [row[0] for row in store.query("SELECT issue_id FROM issues")]
    assert len(set(ids)) == 120
    assert {row[0] for row in store.query("SELECT DISTINCT scope FROM sync_state")} == {PROJECT, "TRIAL"}
    seed_demo(store, now=NOW, issues=10, project="TRIAL", reset=True)
    assert store.count("issues", {"project_key": "TRIAL"}) == 10
    assert store.count("issues", {"project_key": PROJECT}) == 60


def test_the_cli_seeds_and_points_at_the_next_commands(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["demo", "--issues", "40", "--seed", "5", "--project", "demo"], settings=settings)
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "demo project DEMO: 40 issues" in out
    assert "Next: python -m oversight derive --project DEMO && python -m oversight rules --project DEMO" in out
    with Store.open(settings.db_file) as store:
        assert store.count("issues", {"project_key": PROJECT}) == 40
        assert json.loads(store.query_one("SELECT stats_json FROM runs WHERE kind = 'demo'")[0])["seed"] == 5

    code = main(["demo", "--issues", "0"], settings=settings)
    captured = capsys.readouterr()
    assert code == EXIT_CONFIG and "--issues must be at least 1" in captured.err


@pytest.mark.parametrize("seed", [20261104, 20261105])
def test_the_people_have_habits_so_the_scorecards_show_every_state(tmp_path: Path, seed: int) -> None:
    """JO-11: over the scorecards' default 90 days the demo shows each state a scorecard can be in: closer to the
    process than the board, further from it, level with it, too few tickets to compare (Hana Mori, part time of
    late), measures DEMO cannot show (no stage to skip, no code review in Jira) and members who hold no ticket."""
    from fastapi.testclient import TestClient

    from oversight.api.app import create_app
    from oversight.api.scorecard import COMPARABLE
    from oversight.llm.scripted import ScriptedProvider
    from oversight.rules.catalog import RulesConfig
    from oversight.rules.engine import run_rules

    settings = Settings(_env_file=None, jira_projects=PROJECT, api_service_key="k", db_path=tmp_path / "oversight.db",
                        config_dir=CONFIG_FIXTURES, reports_dir=tmp_path / "reports")
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=NOW, seed=seed)
        run_derive(store, load_process(), projects=[PROJECT], now=fixed_now)
        run_rules(store, load_process(), RulesConfig.load(settings.rules_file), FieldsConfig.load(settings.fields_file),
                  projects=[PROJECT], now=fixed_now)
    with TestClient(create_app(settings, provider=ScriptedProvider(), now=fixed_now), headers={"X-Service-Key": "k", **VIEWER_HEADERS}) as api:
        body = api.get("/api/v1/people/scorecard", params={"level": "people"}).json()
        members = [entry for entry in api.get("/api/v1/people/directory").json()["people"] if entry["tickets"] == 0]
    shown = [m["key"] for m in body["measures"] if m["measurable"]]
    # One truth per board (JO-12 G11): the rules measure R-FLOW-02 on DEMO, so the scorecard measures skipped stages.
    assert [m["key"] for m in body["measures"] if not m["measurable"]] == ["code_review"]
    values = [row["values"][key] for row in body["people"] for key in shown]
    assert {value["vs_board"] for value in values} == {"closer", "further", "level", None}
    assert any(value["enough"] for value in values) and any(not value["enough"] for value in values)
    hana = next(row for row in body["people"] if row["person"]["name"] == "Hana Mori")
    assert hana["tickets"] > 0 and not any(hana["values"][key]["enough"] for key in shown if key in COMPARABLE)
    ada = next(row for row in body["people"] if row["person"]["name"] == "Ada Brooks")
    assert ada["values"]["estimated_before_start"]["vs_board"] == "closer"
    finn = next(row for row in body["people"] if row["person"]["name"] == "Finn Gallagher")
    assert finn["values"]["time_logged"]["vs_board"] == "further"
    assert [entry["person"]["name"] for entry in members] == list(MEMBER_NAMES.values())

