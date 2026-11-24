from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oversight.api.aggregates import Finding, first_sightings, load_findings, weeks
from oversight.api.app import create_app
from oversight.api.routes.overview import hot_spots
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.insights.weekly import run_insights
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC, parse_ts, to_iso
from tests.conftest import CONFIG_FIXTURES, VIEWER_HEADERS

KEY = "test-key"
NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
FIRST_RUN = NOW - timedelta(days=35)  # a window edge: findings first seen here sit on both sides of it
SECOND_RUN = NOW - timedelta(days=10, hours=3)

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,19}-[1-9]\d{0,8}$")
RULE_ID = re.compile(r"^[RW]-[A-Z]+-\d{2}$")
FINDING_ID = re.compile(r"^[0-9a-f]{32}$")
STAGES = ("backlog", "estimate", "development", "code_review", "dev_signoff", "testing", "test_review",
          "test_signoff", "done")
UNITS = ("count", "hours", "ratio", "share", "points", "days")
METRICS = ("median_cycle_time_h", "median_effort_ratio", "skipped_stage_share", "reopen_rate", "open_breaches",
           "open_warnings")
DONE_METRICS = METRICS[:4]
TOTALS = ("open_breaches", "open_warnings", "new_7d", "resolved_7d", "issues_in_scope", "rules_not_measurable")
COVERAGE = ("estimate", "story_points", "time_logged", "code_review", "test_evidence")
TOLERANCE = 1e-4


def is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 1


def is_timestamp(value: Any) -> bool:
    return isinstance(value, str) and ISO_Z.fullmatch(value) is not None and parse_ts(value) is not None


def is_day(value: Any) -> bool:
    return isinstance(value, str) and DAY.fullmatch(value) is not None and date.fromisoformat(value) is not None


def assert_meta(meta: dict[str, Any], now: datetime = NOW) -> None:
    assert set(meta) == {"data_mode", "last_synced_at", "projects", "rules_version", "process_version",
                         "generated_at", "report_timezone"}
    assert meta["data_mode"] == "live"
    assert is_timestamp(meta["last_synced_at"])
    assert meta["projects"] == ["DEMO"]
    assert is_text(meta["rules_version"]) and is_text(meta["process_version"])
    assert meta["generated_at"] == to_iso(now)
    assert is_text(meta["report_timezone"])


def assert_points(points: list[dict[str, Any]], count: int, now: datetime = NOW) -> None:
    """Trend points: {t: Monday, value: number | null}, consecutive weeks, ending with the current one."""
    assert len(points) == count
    for point in points:
        assert set(point) == {"t", "value"}
        assert is_day(point["t"]) and date.fromisoformat(point["t"]).weekday() == 0
        assert point["value"] is None or is_number(point["value"])
    days = [date.fromisoformat(point["t"]) for point in points]
    assert all(later - earlier == timedelta(days=7) for earlier, later in zip(days, days[1:]))
    today = now.astimezone(UTC).date()
    assert days[-1] == today - timedelta(days=today.weekday())


def assert_window_total(total: dict[str, Any], now: datetime = NOW) -> None:
    assert set(total) == {"current", "previous", "series"}
    series = total["series"]
    assert 2 <= len(series) <= 12
    for point in series:
        assert set(point) == {"end_at", "value"}
        assert is_timestamp(point["end_at"]) and is_count(point["value"])
    assert total["current"] == series[-1]["value"] and total["previous"] == series[-2]["value"]
    ends = [parse_ts(point["end_at"]) for point in series]
    assert ends[-1] == now
    assert all(later - earlier == timedelta(days=7) for earlier, later in zip(ends, ends[1:]))


def assert_overview_shape(body: dict[str, Any], now: datetime = NOW) -> None:
    assert set(body) == {"meta", "totals", "hot_spots", "by_rule", "coverage", "trend_cards", "status_mix",
                         "latest_insight", "tracking_since"}
    assert body["tracking_since"] is None or ISO_Z.match(body["tracking_since"])
    assert_meta(body["meta"], now)
    assert tuple(body["totals"]) == TOTALS
    for total in body["totals"].values():
        assert_window_total(total, now)
    assert len(body["hot_spots"]) <= 5
    for spot in body["hot_spots"]:
        assert set(spot) == {"epic_key", "epic_title", "open_breaches", "open_warnings", "issues", "series"}
        assert spot["epic_key"] is None or ISSUE_KEY.fullmatch(spot["epic_key"])
        assert is_text(spot["epic_title"])
        assert is_count(spot["open_breaches"]) and is_count(spot["open_warnings"]) and is_count(spot["issues"])
        assert_points(spot["series"], 12, now)
        assert spot["series"][-1]["value"] == spot["open_breaches"] + spot["open_warnings"]
    for rule in body["by_rule"]:
        assert set(rule) == {"rule_id", "title", "severity", "kind", "open", "measurable", "missing_evidence"}
        assert RULE_ID.fullmatch(rule["rule_id"]) and is_text(rule["title"])
        assert rule["severity"] in ("breach", "warning") and rule["kind"] in ("rule", "warning")
        assert is_count(rule["open"]) and isinstance(rule["measurable"], bool)
        assert isinstance(rule["missing_evidence"], list) and all(is_text(n) for n in rule["missing_evidence"])
        assert rule["measurable"] == (rule["missing_evidence"] == [])
    assert tuple(body["coverage"]) == COVERAGE
    for entry in body["coverage"].values():
        assert set(entry) == {"covered", "total", "share", "measurable", "note"}
        assert entry["covered"] is None or is_count(entry["covered"])
        assert is_count(entry["total"]) and isinstance(entry["measurable"], bool) and is_text(entry["note"])
        assert entry["share"] is None or (is_number(entry["share"]) and 0 <= entry["share"] <= 1)
    assert tuple(body["trend_cards"]) == DONE_METRICS
    for metric, card in body["trend_cards"].items():
        assert set(card) == {"metric", "label", "unit", "current", "previous", "direction_good", "points"}
        assert card["metric"] == metric and is_text(card["label"]) and card["unit"] in UNITS
        assert card["direction_good"] in ("up", "down")
        assert_points(card["points"], 12, now)
        assert card["current"] == card["points"][-1]["value"] and card["previous"] == card["points"][-2]["value"]
    for row in body["status_mix"]:
        assert set(row) == {"status", "stage", "status_category", "count"}
        assert is_text(row["status"]) and (row["stage"] is None or row["stage"] in STAGES)
        assert row["status_category"] in ("new", "indeterminate", "done") and is_count(row["count"])
    teaser = body["latest_insight"]
    if teaser is not None:
        assert set(teaser) == {"week_of", "generated_at", "verified", "sentences"}
        assert is_day(teaser["week_of"]) and is_timestamp(teaser["generated_at"])
        assert isinstance(teaser["verified"], bool) and len(teaser["sentences"]) <= 2
        for sentence in teaser["sentences"]:
            assert set(sentence) == {"id", "text", "tone", "candidate_ids", "issue_keys", "finding_ids"}
            assert is_text(sentence["id"]) and is_text(sentence["text"])
            assert sentence["tone"] in ("blatantly_bad", "trending_bad", "neutral")
            assert all(isinstance(c, str) for c in sentence["candidate_ids"])
            assert all(ISSUE_KEY.fullmatch(k) for k in sentence["issue_keys"])
            assert all(FINDING_ID.fullmatch(f) for f in sentence["finding_ids"])


def make_settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        jira_projects="DEMO",
        api_service_key=KEY,
        db_path=root / "oversight.db",
        config_dir=CONFIG_FIXTURES,
        reports_dir=root / "reports",
    )


def make_client(settings: Settings, now: datetime) -> TestClient:
    app = create_app(settings, provider=ScriptedProvider(), now=lambda: now)
    return TestClient(app, headers={"X-Service-Key": KEY, **VIEWER_HEADERS})


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """The seeded DEMO store with three rules runs and a briefing, all on fixed clocks."""
    settings = make_settings(tmp_path_factory.mktemp("overview_demo"))
    process = ProcessConfig.load(settings.process_file)
    rules = RulesConfig.load(settings.rules_file)
    fields = FieldsConfig.load(settings.fields_file)
    looser = rules.model_copy(deep=True)  # fires on more work, so the second run clears some findings
    for rule in looser.rules:
        if rule.id == "R-EFF-01":
            rule.params["threshold"] = 1.2
        elif rule.id == "W-EFF-01":
            rule.params["threshold"] = 0.5
    store = Store.open(settings.db_file)
    try:
        seed_demo(store, now=NOW, seed=20261104)
        run_derive(store, process, projects=["DEMO"], now=lambda: NOW)
        run_rules(store, process, looser, fields, projects=["DEMO"], now=lambda: FIRST_RUN)
        run_rules(store, process, rules, fields, projects=["DEMO"], now=lambda: SECOND_RUN)
        run_rules(store, process, rules, fields, projects=["DEMO"], now=lambda: NOW)
        run_insights(store, process, rules, ScriptedProvider(), settings, projects=["DEMO"], now=lambda: NOW)
    finally:
        store.close()
    return settings


@pytest.fixture(scope="module")
def client(demo: Settings) -> TestClient:
    return make_client(demo, NOW)


@pytest.fixture(scope="module")
def db(demo: Settings) -> Iterator[sqlite3.Connection]:
    conn = open_readonly(demo.db_file)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def overview(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/overview")
    assert response.status_code == 200, response.text
    return response.json()


TINY_ISSUES = [
    ("DEMO-1", "Task", "Done", "done", "2026-10-01T09:00:00.000Z", 3600, 3.0, None, None),
    ("DEMO-2", "Task", "Done", "done", "2026-10-01T09:00:00.000Z", 0, 2.0, None, None),
    ("DEMO-3", "Bug", "Done", "done", "2026-10-01T09:00:00.000Z", 7200, None, None, None),
    ("DEMO-4", "Task", "In Progress", "indeterminate", "2026-10-02T09:00:00.000Z", 600, None, "DEMO-5", "DEMO-5"),
    ("DEMO-5", "Epic", "To Do", "new", "2026-09-01T09:00:00.000Z", None, None, None, None),
    ("DEMO-6", "Subtask", "In Progress", "indeterminate", to_iso(NOW - timedelta(days=1)), None, None, None, "DEMO-4"),
]
TINY_METRICS = [
    ("DEMO-1", "2026-11-02T00:00:00.000Z", 10.0, 2.0, "[]", 0),  # the current week's first instant
    ("DEMO-2", "2026-11-01T23:59:59.999Z", 30.0, 0.0, '["development"]', 1),  # the week before's last; no time
    ("DEMO-3", "2026-10-27T12:00:00.000Z", 50.0, 1.0, "[]", 0),
]
TINY_FINDINGS = [
    ("R-EST-01", "rule", "breach", "DEMO-4", NOW - timedelta(days=20), None),
    ("W-AGE-01", "warning", "warning", "DEMO-6", NOW - timedelta(days=3), None),  # a subtask: its parent's epic
    ("W-SPR-01", "warning", "warning", "sprint:77", NOW - timedelta(days=1), None),  # a sprint: no epic
    ("R-FLOW-02", "rule", "breach", "DEMO-3", NOW - timedelta(days=30), NOW - timedelta(days=2)),
    ("R-EFF-01", "rule", "breach", "DEMO-2", NOW - timedelta(days=8), None),
    ("R-HYG-01", "rule", "breach", "DEMO-1", NOW - timedelta(days=7), None),  # first seen on a window edge
]


def build_tiny_store(path: Path) -> None:
    store = Store.open(path)
    try:
        conn = store.conn
        conn.execute(
            "INSERT INTO sync_state (scope, last_synced_at, account_timezone) VALUES ('DEMO', ?, 'UTC')",
            (to_iso(NOW),),
        )
        for number, (key, kind, status, category, created, spent, points, epic, parent) in enumerate(TINY_ISSUES, 1):
            conn.execute(
                "INSERT INTO issues (issue_key, issue_id, project_key, issue_type, summary, status, status_category,"
                " epic_key, parent_key, created_at, time_spent_s, story_points)"
                " VALUES (?, ?, 'DEMO', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, str(10000 + number), kind, "Payments" if kind == "Epic" else f"Work item {number}", status,
                 category, epic, parent, created, spent, points),
            )
        conn.executemany(
            "INSERT INTO issue_metrics (issue_key, done_at, cycle_time_h, effort_ratio, skipped_stages, reopen_count)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            TINY_METRICS,
        )
        for number, (rule_id, kind, severity, issue_key, first, resolved) in enumerate(TINY_FINDINGS, 1):
            conn.execute(
                "INSERT INTO findings (finding_id, rule_id, rule_version, issue_key, project_key, kind, severity,"
                " evidence_key, message, first_seen_at, last_seen_at, resolved_at, resolved_why)"
                " VALUES (?, ?, '0.1.0', ?, 'DEMO', ?, ?, ?, 'test', ?, ?, ?, ?)",
                (f"{number:032x}", rule_id, issue_key, kind, severity, f"{rule_id}:{issue_key}", to_iso(first),
                 to_iso(resolved or NOW), to_iso(resolved), "cleared" if resolved else None),
            )
        conn.commit()
    finally:
        store.close()


@pytest.fixture(scope="module")
def tiny(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    settings = make_settings(tmp_path_factory.mktemp("overview_tiny"))
    build_tiny_store(settings.db_file)
    return make_client(settings, NOW)


@pytest.fixture(scope="module")
def tiny_overview(tiny: TestClient) -> dict[str, Any]:
    response = tiny.get("/api/v1/overview")
    assert response.status_code == 200, response.text
    return response.json()


def values(points: list[dict[str, Any]]) -> list[Any]:
    return [point["value"] for point in points]


def week_bounds(point: dict[str, Any]) -> tuple[str, str]:
    """A trend point's week in storage format: its Monday 00:00 UTC and the next Monday's."""
    start = datetime.fromisoformat(point["t"]).replace(tzinfo=UTC)
    return to_iso(start) or "", to_iso(start + timedelta(days=7)) or ""


def test_tiny_overview_has_the_contract_shape(tiny_overview: dict[str, Any]) -> None:
    assert_overview_shape(tiny_overview)


def test_tiny_totals_count_rolling_windows_with_both_edges(tiny_overview: dict[str, Any]) -> None:
    totals = tiny_overview["totals"]
    assert values(totals["open_breaches"]["series"]) == [0] * 7 + [1, 1, 2, 4, 3]
    assert values(totals["open_warnings"]["series"]) == [0] * 11 + [2]
    assert values(totals["new_7d"]["series"]) == [0] * 7 + [1, 0, 1, 2, 3]
    assert values(totals["resolved_7d"]["series"]) == [0] * 11 + [1]
    assert values(totals["issues_in_scope"]["series"]) == [0] * 7 + [4, 4, 4, 4, 5]
    assert values(totals["rules_not_measurable"]["series"]) == [2] * 12
    assert totals["open_breaches"]["series"][-2]["end_at"] == "2026-10-28T14:30:00.000Z"


def test_tiny_hot_spots_put_the_no_epic_group_after_the_epics(tiny_overview: dict[str, Any]) -> None:
    spots = tiny_overview["hot_spots"]
    assert [(s["epic_key"], s["epic_title"], s["open_breaches"], s["open_warnings"], s["issues"]) for s in spots] == [
        ("DEMO-5", "Payments", 1, 1, 2),
        (None, "(no epic)", 2, 1, 2),
    ]
    assert values(spots[0]["series"]) == [0] * 8 + [1, 1, 2, 2]
    assert values(spots[1]["series"]) == [0] * 7 + [1, 1, 1, 3, 3]


def test_tiny_trend_cards_use_monday_weeks_and_skip_unlogged_effort(tiny_overview: dict[str, Any]) -> None:
    cards = tiny_overview["trend_cards"]
    assert {metric: (card["current"], card["previous"]) for metric, card in cards.items()} == {
        "median_cycle_time_h": (10.0, 40.0),
        "median_effort_ratio": (2.0, 1.0),
        "skipped_stage_share": (0.0, 0.5),
        "reopen_rate": (0.0, 0.5),
    }
    for card in cards.values():
        assert values(card["points"])[:10] == [None] * 10
    assert [(c["label"], c["unit"], c["direction_good"]) for c in cards.values()] == [
        ("Median cycle time", "hours", "down"),
        ("Median effort ratio", "ratio", "down"),
        ("Skipped a stage", "share", "down"),
        ("Reopen rate", "share", "down"),
    ]


def test_tiny_trends_count_open_findings_at_each_weeks_end(tiny: TestClient) -> None:
    breaches = tiny.get("/api/v1/trends", params={"metric": "open_breaches"}).json()
    warnings = tiny.get("/api/v1/trends", params={"metric": "open_warnings"}).json()
    assert values(breaches["series"]) == [0] * 7 + [1, 2, 2, 4, 3]
    assert values(warnings["series"]) == [0] * 10 + [1, 2]
    assert breaches["series"][-1]["t"] == "2026-11-02"


def test_tiny_coverage_status_mix_rules_and_teaser(tiny_overview: dict[str, Any]) -> None:
    coverage = tiny_overview["coverage"]
    assert {name: (e["covered"], e["total"], e["share"], e["measurable"]) for name, e in coverage.items()} == {
        "estimate": (2, 5, 0.4, True),
        "story_points": (2, 5, 0.4, True),
        "time_logged": (3, 5, 0.6, True),
        "code_review": (None, 5, None, False),
        "test_evidence": (None, 5, None, False),
    }
    assert coverage["estimate"]["note"] == (
        "Issues with an estimate (story points), of the issues created in the last 90 days, Epics left out."
    )
    assert coverage["code_review"]["note"] == (
        "Jira records no code review for DEMO, so it cannot be counted; issues created in the last 90 days,"
        " Epics left out."
    )
    assert tiny_overview["status_mix"] == [
        {"status": "In Progress", "stage": "development", "status_category": "indeterminate", "count": 2},
        {"status": "Done", "stage": "done", "status_category": "done", "count": 3},
    ]
    opened = {rule["rule_id"]: rule["open"] for rule in tiny_overview["by_rule"] if rule["open"]}
    assert opened == {"R-EST-01": 1, "R-EFF-01": 1, "R-HYG-01": 1, "W-AGE-01": 1, "W-SPR-01": 1}
    assert tiny_overview["latest_insight"] is None  # no briefing yet


def test_a_condition_raised_again_under_a_new_id_was_first_seen_with_its_first_finding() -> None:
    warned = Finding("warning", "2026-10-01T09:00:00.000Z", "2026-11-03T09:00:00.000Z", "DEMO-1", None, None,
                     "R-FLOW-03", "reopen-7", "superseded")
    as_breach = warned._replace(severity="breach", first_seen_at="2026-11-03T09:00:00.000Z", resolved_at=None,
                                resolved_why=None)
    elsewhere = as_breach._replace(issue_key="DEMO-2", first_seen_at="2026-11-02T09:00:00.000Z")
    another_reopen = as_breach._replace(evidence_key="reopen-9")
    from_the_start = warned._replace(issue_key="DEMO-3", first_seen_at="")
    assert sorted(first_sightings([as_breach, warned, elsewhere, another_reopen, from_the_start])) == [
        "", "2026-10-01T09:00:00.000Z", "2026-11-02T09:00:00.000Z", "2026-11-03T09:00:00.000Z"]
    assert as_breach.condition == warned.condition == ("R-FLOW-03", "DEMO-1", "reopen-7")


def test_weeks_start_on_monday_at_midnight_utc() -> None:
    monday = datetime(2026, 11, 9, tzinfo=UTC)
    assert weeks(monday, 2) == [
        ("2026-11-02", "2026-11-02T00:00:00.000Z", "2026-11-09T00:00:00.000Z"),
        ("2026-11-09", "2026-11-09T00:00:00.000Z", "2026-11-16T00:00:00.000Z"),
    ]
    assert weeks(monday - timedelta(milliseconds=1), 1)[0][0] == "2026-11-02"
    assert weeks(datetime(2026, 11, 9, 2, 0, tzinfo=timezone(timedelta(hours=5))), 1)[0][0] == "2026-11-02"
    assert [t for t, _start, _end in weeks(NOW)][0] == "2026-08-17"


WINDOW_SQL = {
    "open_breaches": "SELECT COUNT(*) FROM findings WHERE project_key = 'DEMO' AND severity = 'breach'"
    " AND first_seen_at <= :end AND (resolved_at IS NULL OR resolved_at > :end)",
    "open_warnings": "SELECT COUNT(*) FROM findings WHERE project_key = 'DEMO' AND severity = 'warning'"
    " AND first_seen_at <= :end AND (resolved_at IS NULL OR resolved_at > :end)",
    "new_7d": "SELECT COUNT(*) FROM (SELECT MIN(first_seen_at) AS first FROM findings WHERE project_key = 'DEMO'"
    " GROUP BY rule_id, issue_key, evidence_key) WHERE first >= :start AND first <= :end",
    "resolved_7d": "SELECT COUNT(*) FROM findings WHERE project_key = 'DEMO' AND resolved_why = 'cleared'"
    " AND resolved_at >= :start AND resolved_at <= :end",
    "issues_in_scope": "SELECT COUNT(*) FROM issues WHERE project_key = 'DEMO' AND issue_type <> 'Epic'"
    " AND created_at <= :end",
}
OPEN_BY_EPIC_SQL = """
    SELECT COALESCE(i.epic_key, p.epic_key) AS epic, f.severity, f.issue_key, f.first_seen_at, f.resolved_at
    FROM findings AS f
    LEFT JOIN issues AS i ON i.issue_key = f.issue_key
    LEFT JOIN issues AS p ON p.issue_key = i.parent_key
    WHERE f.project_key = 'DEMO'
"""


def count(db: sqlite3.Connection, sql: str, **params: Any) -> int:
    value: int = db.execute(sql, params).fetchone()[0]
    return value


def test_overview_has_the_contract_shape(overview: dict[str, Any]) -> None:
    assert_overview_shape(overview)
    assert overview["meta"]["last_synced_at"] == "2026-11-04T14:30:00.000Z"
    assert overview["meta"]["process_version"] == "0.1.0-test"  # the fixture's process.yaml
    assert overview["meta"]["report_timezone"] == "UTC"  # REPORT_TIMEZONE is blank: never the demo sync's zone
    assert len(overview["hot_spots"]) == 5 and len(overview["by_rule"]) == 12


def test_meta_names_the_report_zone_setting_never_the_sync_accounts(demo: Settings) -> None:
    chicago = make_client(demo.model_copy(update={"report_timezone": "America/Chicago"}), NOW)
    for path in ("/api/v1/overview", "/api/v1/findings", "/api/v1/health"):
        response = chicago.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["meta"]["report_timezone"] == "America/Chicago"
        assert "Europe/London" not in response.text
    assert chicago.get("/api/v1/health").json()["report_timezone"] == "America/Chicago"


def test_totals_equal_the_counts_in_findings(overview: dict[str, Any], db: sqlite3.Connection) -> None:
    totals = overview["totals"]
    open_sql = "SELECT COUNT(*) FROM findings WHERE project_key = 'DEMO' AND severity = :s AND resolved_at IS NULL"
    assert totals["open_breaches"]["current"] == count(db, open_sql, s="breach") > 0
    assert totals["open_warnings"]["current"] == count(db, open_sql, s="warning") > 0
    since = to_iso(NOW - timedelta(days=7))
    assert totals["new_7d"]["current"] == count(db, "SELECT COUNT(*) FROM findings WHERE first_seen_at >= :s", s=since)
    assert totals["issues_in_scope"]["current"] == count(
        db, "SELECT COUNT(*) FROM issues WHERE project_key = 'DEMO' AND issue_type <> 'Epic'"
    )
    assert totals["rules_not_measurable"]["current"] == sum(1 for r in overview["by_rule"] if not r["measurable"])
    for name, sql in WINDOW_SQL.items():
        for point in totals[name]["series"]:
            end = parse_ts(point["end_at"])
            assert end is not None
            start = to_iso(end - timedelta(days=7))
            assert point["value"] == count(db, sql, start=start, end=point["end_at"]), (name, point)


def test_totals_follow_the_findings_history(overview: dict[str, Any], db: sqlite3.Connection) -> None:
    totals = overview["totals"]
    resolved = count(db, "SELECT COUNT(*) FROM findings WHERE resolved_at = :at", at=to_iso(SECOND_RUN))
    cleared = count(db, "SELECT COUNT(*) FROM findings WHERE resolved_at = :at AND resolved_why = 'cleared'",
                    at=to_iso(SECOND_RUN))
    assert (totals["resolved_7d"]["previous"], totals["resolved_7d"]["current"]) == (cleared, 0)
    assert cleared == resolved > 0
    first = count(db, "SELECT COUNT(*) FROM findings WHERE first_seen_at = :at", at=to_iso(FIRST_RUN))
    new = totals["new_7d"]["series"]
    assert new[6]["end_at"] == to_iso(FIRST_RUN)
    assert values(new) == [0] * 6 + [first, first, 0, 0, totals["new_7d"]["previous"], totals["new_7d"]["current"]]
    assert values(totals["open_breaches"]["series"])[:6] == [0] * 6  # nothing was open before the first run


def test_hot_spots_match_the_findings(overview: dict[str, Any], db: sqlite3.Connection) -> None:
    rows = db.execute(OPEN_BY_EPIC_SQL).fetchall()
    groups: dict[str | None, dict[str, Any]] = {}
    for epic, severity, issue_key, _first, resolved in rows:
        if resolved is None:
            group = groups.setdefault(epic, {"breach": 0, "warning": 0, "issues": set()})
            group[severity] += 1
            if not issue_key.startswith("sprint:"):
                group["issues"].add(issue_key)
    ranked = sorted(groups, key=lambda e: (e is None, -groups[e]["breach"], -groups[e]["warning"], e or ""))
    spots = overview["hot_spots"]
    assert [spot["epic_key"] for spot in spots] == ranked[:5]
    assert ranked[-1] is None  # the no-epic group exists, after every epic
    for spot in spots:
        group = groups[spot["epic_key"]]
        expected = (group["breach"], group["warning"], len(group["issues"]))
        assert (spot["open_breaches"], spot["open_warnings"], spot["issues"]) == expected
        title = db.execute("SELECT summary FROM issues WHERE issue_key = ?", (spot["epic_key"],)).fetchone()[0]
        assert spot["epic_title"] == title
        for point in spot["series"]:
            _start, end = week_bounds(point)
            assert point["value"] == sum(
                1
                for epic, _severity, _key, first, resolved in rows
                if epic == spot["epic_key"] and first < end and (resolved is None or resolved >= end)
            )


def test_the_no_epic_group_holds_sprint_findings(db: sqlite3.Connection) -> None:
    every = hot_spots(load_findings(db, "DEMO"), NOW, limit=100)
    assert every[-1]["epic_key"] is None and every[-1]["epic_title"] == "(no epic)"
    assert all(spot["epic_key"] for spot in every[:-1])
    sprint_open = count(db, "SELECT COUNT(*) FROM findings WHERE issue_key LIKE 'sprint:%' AND resolved_at IS NULL")
    no_epic_open = sum(1 for epic, *_rest, resolved in db.execute(OPEN_BY_EPIC_SQL) if epic is None and not resolved)
    assert sprint_open >= 1
    assert every[-1]["open_breaches"] + every[-1]["open_warnings"] == no_epic_open
    assert sum(spot["open_breaches"] + spot["open_warnings"] for spot in every) == count(
        db, "SELECT COUNT(*) FROM findings WHERE project_key = 'DEMO' AND resolved_at IS NULL"
    )


def test_by_rule_follows_the_catalogue(overview: dict[str, Any], db: sqlite3.Connection, demo: Settings) -> None:
    rules = RulesConfig.load(demo.rules_file).enabled_rules
    assert [summary["rule_id"] for summary in overview["by_rule"]] == [rule.id for rule in rules]
    open_counts = dict(
        db.execute(
            "SELECT rule_id, COUNT(*) FROM findings WHERE project_key = 'DEMO' AND resolved_at IS NULL GROUP BY rule_id"
        ).fetchall()
    )
    for summary, rule in zip(overview["by_rule"], rules, strict=True):
        assert (summary["title"], summary["severity"], summary["kind"]) == (rule.title, rule.severity, rule.kind)
        assert summary["open"] == open_counts.get(rule.id, 0)
    missing = {r["rule_id"]: r["missing_evidence"] for r in overview["by_rule"] if not r["measurable"]}
    assert missing == {"R-FLOW-01": ["code_review", "testing"], "W-REV-01": ["code_review"]}


def test_coverage_counts_work_created_in_the_last_90_days(overview: dict[str, Any], db: sqlite3.Connection) -> None:
    scope = "FROM issues WHERE project_key = 'DEMO' AND issue_type <> 'Epic' AND created_at >= :since"
    since = to_iso(NOW - timedelta(days=90))
    total = count(db, f"SELECT COUNT(*) {scope}", since=since)
    points = count(db, f"SELECT COUNT(*) {scope} AND story_points > 0", since=since)
    logged = count(db, f"SELECT COUNT(*) {scope} AND time_spent_s > 0", since=since)
    coverage = overview["coverage"]
    assert total > points > 0 and total > logged > 0
    for name, covered in (("estimate", points), ("story_points", points), ("time_logged", logged)):
        entry = coverage[name]
        assert (entry["covered"], entry["total"], entry["measurable"]) == (covered, total, True)
        assert entry["share"] == pytest.approx(covered / total, abs=TOLERANCE)
    for name in ("code_review", "test_evidence"):
        entry = coverage[name]
        assert (entry["covered"], entry["total"], entry["share"], entry["measurable"]) == (None, total, None, False)
    assert coverage["test_evidence"]["note"].startswith("Jira records no testing for DEMO")


def test_status_mix_adds_up_to_issues_in_scope(overview: dict[str, Any], db: sqlite3.Connection) -> None:
    mix = overview["status_mix"]
    assert sum(row["count"] for row in mix) == overview["totals"]["issues_in_scope"]["current"]
    expected = dict(
        db.execute(
            "SELECT status, COUNT(*) FROM issues WHERE project_key = 'DEMO' AND issue_type <> 'Epic' GROUP BY status"
        ).fetchall()
    )
    assert {row["status"]: row["count"] for row in mix} == expected
    assert [(row["status"], row["stage"], row["status_category"]) for row in mix] == [
        ("To Do", "backlog", "new"),
        ("In Progress", "development", "indeterminate"),
        ("Done", "done", "done"),
    ]


def test_latest_insight_is_the_newest_briefing(overview: dict[str, Any], db: sqlite3.Connection) -> None:
    row = db.execute(
        "SELECT week_of, generated_at, verified, sentences_json FROM insights WHERE project_key = 'DEMO'"
        " ORDER BY generated_at DESC"
    ).fetchone()
    teaser = overview["latest_insight"]
    assert (teaser["week_of"], teaser["generated_at"]) == (row["week_of"], row["generated_at"])
    assert teaser["week_of"] == "2026-11-02" and teaser["verified"] is bool(row["verified"])
    stored = json.loads(row["sentences_json"])
    assert len(stored) > 2
    fields = ("id", "text", "tone", "candidate_ids", "issue_keys", "finding_ids")
    assert teaser["sentences"] == [{name: sentence[name] for name in fields} for sentence in stored[:2]]


def test_the_overview_names_no_one(overview: dict[str, Any], db: sqlite3.Connection) -> None:
    accounts = [row[0] for row in db.execute("SELECT DISTINCT assignee_id FROM issues WHERE assignee_id IS NOT NULL")]
    text = json.dumps(overview)
    assert accounts and not [account for account in accounts if account in text]


DONE_SQL = """
    SELECT m.cycle_time_h, m.effort_ratio, m.skipped_stages, m.reopen_count, i.time_spent_s
    FROM issue_metrics AS m JOIN issues AS i ON i.issue_key = m.issue_key
    WHERE i.project_key = 'DEMO' AND m.done_at >= ? AND m.done_at < ?
"""


def done_in(db: sqlite3.Connection, point: dict[str, Any]) -> list[sqlite3.Row]:
    return db.execute(DONE_SQL, week_bounds(point)).fetchall()


def expected_value(metric: str, rows: list[sqlite3.Row]) -> float | None:
    if metric == "skipped_stage_share":
        return sum(1 for r in rows if json.loads(r["skipped_stages"] or "[]")) / len(rows) if rows else None
    if metric == "reopen_rate":
        return sum(1 for r in rows if (r["reopen_count"] or 0) > 0) / len(rows) if rows else None
    if metric == "median_cycle_time_h":
        sample = [r["cycle_time_h"] for r in rows if r["cycle_time_h"] is not None]
    else:
        sample = [r["effort_ratio"] for r in rows if r["effort_ratio"] is not None and (r["time_spent_s"] or 0) > 0]
    return statistics.median(sample) if sample else None


def trend(client: TestClient, metric: str, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/trends", params={"metric": metric, **params})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_trends_have_the_contract_shape(client: TestClient) -> None:
    for metric in METRICS:
        body = trend(client, metric)
        assert set(body) == {"meta", "metric", "period", "last_n", "label", "unit", "direction_good", "note", "series"}
        assert_meta(body["meta"])
        assert (body["metric"], body["period"], body["last_n"]) == (metric, "week", 12)
        assert is_text(body["label"]) and is_text(body["note"]) and body["direction_good"] in ("up", "down")
        assert body["unit"] in UNITS
        assert_points(body["series"], 12)
    for metric in ("open_breaches", "open_warnings"):
        body = trend(client, metric)
        assert body["unit"] == "count" and all(is_count(value) for value in values(body["series"]))


def test_trend_cards_are_the_trends(client: TestClient, overview: dict[str, Any]) -> None:
    for metric, card in overview["trend_cards"].items():
        body = trend(client, metric, last_n=12)
        assert card["points"] == body["series"]
        assert card["current"] == body["series"][-1]["value"] and card["previous"] == body["series"][-2]["value"]
        assert (card["label"], card["unit"], card["direction_good"]) == (body["label"], body["unit"], body["direction_good"])


def test_open_trends_end_with_the_overview_totals(client: TestClient, overview: dict[str, Any]) -> None:
    for metric in ("open_breaches", "open_warnings"):
        assert trend(client, metric)["series"][-1]["value"] == overview["totals"][metric]["current"]


def test_trend_values_match_the_database(client: TestClient, db: sqlite3.Connection) -> None:
    for metric in DONE_METRICS:
        for point in trend(client, metric)["series"]:
            expected = expected_value(metric, done_in(db, point))
            if expected is None:
                assert point["value"] is None, (metric, point)
            else:
                assert point["value"] == pytest.approx(expected, abs=TOLERANCE), (metric, point)
    open_sql = (
        "SELECT COUNT(*) FROM findings WHERE project_key = 'DEMO' AND severity = :severity"
        " AND first_seen_at < :end AND (resolved_at IS NULL OR resolved_at >= :end)"
    )
    for metric, severity in (("open_breaches", "breach"), ("open_warnings", "warning")):
        for point in trend(client, metric)["series"]:
            assert point["value"] == count(db, open_sql, severity=severity, end=week_bounds(point)[1]), (metric, point)


def test_effort_median_leaves_out_work_with_no_time_logged(client: TestClient, db: sqlite3.Connection) -> None:
    differs = 0
    for point in trend(client, "median_effort_ratio")["series"]:
        rows = done_in(db, point)
        unlogged = [r["effort_ratio"] for r in rows if r["effort_ratio"] is not None and not (r["time_spent_s"] or 0)]
        everything = [r["effort_ratio"] for r in rows if r["effort_ratio"] is not None]
        assert all(ratio == 0 for ratio in unlogged)  # estimated, nothing logged: a ratio of 0
        naive, right = statistics.median(everything) if everything else None, expected_value("median_effort_ratio", rows)
        if unlogged and naive is not None and (right is None or abs(naive - right) > TOLERANCE):
            differs += 1
            assert point["value"] != pytest.approx(naive, abs=TOLERANCE)
    assert differs > 0  # the rule changes some week here, so the test above pins it


def test_trends_last_n_counts_back_from_the_current_week(client: TestClient) -> None:
    full = trend(client, "reopen_rate")["series"]
    four = trend(client, "reopen_rate", last_n=4, period="week")
    assert four["last_n"] == 4 and four["series"] == full[-4:]
    one = trend(client, "reopen_rate", last_n=1)
    assert one["series"] == full[-1:] and one["series"][0]["t"] == "2026-11-02"


@pytest.mark.parametrize(
    "query",
    [
        "metric=median_lead_time",
        "",
        "metric=",
        "metric=reopen_rate&last_n=0",
        "metric=reopen_rate&last_n=13",
        "metric=reopen_rate&last_n=two",
        "metric=reopen_rate&period=month",
        "metric=reopen_rate&board=AIL",  # ?project= picks a board (test_api_projects.py)
        "metric=reopen_rate&metric=open_breaches",
        "metric=reopen_rate&last_n=4&last_n=5",
    ],
)
def test_trends_refuses_bad_parameters(client: TestClient, query: str) -> None:
    response = client.get(f"/api/v1/trends?{query}")
    assert response.status_code == 422
    assert response.json()["detail"] and "Traceback" not in response.text


def test_both_routes_need_the_service_key(demo: Settings) -> None:
    bare = TestClient(create_app(demo, provider=ScriptedProvider(), now=lambda: NOW))
    assert bare.get("/api/v1/overview").status_code == 401
    assert bare.get("/api/v1/trends", params={"metric": "reopen_rate"}).status_code == 401
    assert bare.get("/api/v1/trends", params={"metric": "reopen_rate"}, headers={"X-Service-Key": "no"}).status_code == 401


def test_a_week_with_nothing_finished_is_null_never_zero(demo: Settings, db: sqlite3.Connection) -> None:
    later = NOW + timedelta(weeks=10)  # ten weeks after the snapshot: nothing finished since
    client = make_client(demo, later)
    response = client.get("/api/v1/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert_overview_shape(body, later)
    for metric in DONE_METRICS:
        series = trend(client, metric)["series"]
        assert_points(series, 12, later)
        assert values(series)[-10:] == [None] * 10
        for point in series:
            assert (point["value"] is None) == (expected_value(metric, done_in(db, point)) is None), (metric, point)
        assert (body["trend_cards"][metric]["current"], body["trend_cards"][metric]["previous"]) == (None, None)
    assert trend(client, "open_breaches")["series"][-1]["value"] == body["totals"]["open_breaches"]["current"] > 0


def test_tracking_since_is_the_first_time_the_rules_saw_the_project(overview: dict[str, Any], db: sqlite3.Connection) -> None:
    first = db.execute("SELECT MIN(first_seen_at) FROM findings WHERE project_key = 'DEMO'").fetchone()[0]
    assert overview["tracking_since"] == first
    assert parse_ts(overview["tracking_since"]) <= FIRST_RUN + timedelta(seconds=1)
