"""JO-12, GET /portfolio (contract 4.18): every board at a glance, each figure exactly its own Overview's.

Two synthetic boards share one store: AIL, synced and checked three days before NOW (so it is stale and its breaches
are the oldest), and DEMO, synced and checked at NOW. A third case has a board in JIRA_PROJECTS that never synced (no
row, counted in boards_not_synced: JO-17), and
a fourth checks both boards at one moment, where only the board order and the issue numbers break the ties.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oversight.api.app import create_app
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import _ACTIVE_SPRINT_ENDS_IN, seed_demo
from oversight.derive.run import run_derive
from oversight.llm.scripted import ScriptedProvider
from oversight.notify import digest
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import STALE_AFTER, UTC, parse_ts, to_iso
from tests.conftest import CONFIG_FIXTURES, VIEWER_HEADERS
from tests.test_governance import Clock

KEY = "test-key"
NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
EARLIER = NOW - timedelta(days=3)
BOARD_FIELDS = {"project", "last_sync_at", "open_tickets", "in_progress", "open_breaches", "open_warnings",
                "new_findings_7d", "resolved_findings_7d", "open_findings_trend", "tracking_since", "active_sprint",
                "unestimated_in_active_sprint", "sprint_at_risk", "sprint_overdue", "top_rule", "stale"}
ATTENTION_FIELDS = {"project", "issue_key", "rule_id", "title", "severity", "opened_at", "last_seen_at", "age_days",
                    "findings"}


def make_settings(root: Path, projects: str) -> Settings:
    return Settings(_env_file=None, jira_projects=projects, api_service_key=KEY, db_path=root / "oversight.db",
                    config_dir=CONFIG_FIXTURES, reports_dir=root / "reports")


def seed(settings: Settings, boards: dict[str, tuple[int, datetime]]) -> None:
    """Each board seeded, derived and checked at its own moment."""
    process = ProcessConfig.load(settings.process_file)
    rules = RulesConfig.load(settings.rules_file)
    fields = FieldsConfig.load(settings.fields_file)
    with Store.open(settings.db_file) as store:
        for project, (issues, moment) in boards.items():
            seed_demo(store, now=moment, seed=20261104, project=project, issues=issues)
            run_derive(store, process, projects=[project], now=lambda moment=moment: moment)
            run_rules(store, process, rules, fields, projects=[project], now=lambda moment=moment: moment)


def client_of(settings: Settings, clock: Any = None) -> TestClient:
    app = create_app(settings, provider=ScriptedProvider(), now=clock or (lambda: NOW))
    return TestClient(app, headers={"X-Service-Key": KEY, **VIEWER_HEADERS})


@pytest.fixture(scope="module")
def two(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    settings = make_settings(tmp_path_factory.mktemp("portfolio"), "DEMO,AIL")
    seed(settings, {"AIL": (160, EARLIER), "DEMO": (240, NOW)})
    return settings


@pytest.fixture(scope="module")
def client(two: Settings) -> TestClient:
    return client_of(two)


@pytest.fixture(scope="module")
def portfolio(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/portfolio")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def board_of(portfolio: dict[str, Any], key: str) -> dict[str, Any]:
    return next(board for board in portfolio["boards"] if board["project"]["key"] == key)


def active_sprint(settings: Settings, project: str) -> bool:
    conn = open_readonly(settings.db_file)
    try:
        return conn.execute("SELECT 1 FROM issues WHERE project_key = ? AND raw_json LIKE '%\"state\": \"active\"%'"
                            " OR (project_key = ? AND raw_json LIKE '%\"state\":\"active\"%') LIMIT 1",
                            (project, project)).fetchone() is not None
    finally:
        conn.close()


def test_one_row_per_board_in_order_and_meta_names_every_board(portfolio: dict[str, Any]) -> None:
    # boards_not_synced is the contract's name (the dashboard's schema requires it: 18 Nov 2026, a body with not_synced
    # alone was refused as a 502); not_synced, the first JO-17 build's name, goes out beside it with the same count.
    assert set(portfolio) == {"meta", "boards", "attention", "boards_not_synced", "not_synced"}
    assert portfolio["boards_not_synced"] == portfolio["not_synced"] == 0
    assert [board["project"] for board in portfolio["boards"]] == [{"key": "DEMO", "name": "Demo project"},
                                                                   {"key": "AIL", "name": "AIL"}]
    assert all(set(board) == BOARD_FIELDS for board in portfolio["boards"])
    assert portfolio["meta"]["projects"] == ["DEMO", "AIL"]
    assert portfolio["meta"]["last_synced_at"] == to_iso(NOW)  # the latest of the boards' syncs


@pytest.mark.parametrize("key", ["DEMO", "AIL"])
def test_each_boards_figures_are_its_overviews(client: TestClient, two: Settings, portfolio: dict[str, Any],
                                               key: str) -> None:
    board = board_of(portfolio, key)
    overview = client.get("/api/v1/overview", params={"project": key}).json()
    totals, by_rule, mix = overview["totals"], overview["by_rule"], overview["status_mix"]
    assert board["open_breaches"] == totals["open_breaches"]["current"] > 0
    assert board["open_warnings"] == totals["open_warnings"]["current"] > 0
    assert board["new_findings_7d"] == totals["new_7d"]["current"]
    assert board["resolved_findings_7d"] == totals["resolved_7d"]["current"]
    assert board["open_tickets"] == sum(status["count"] for status in mix if status["status_category"] != "done") > 0
    assert board["in_progress"] == sum(status["count"] for status in mix if status["status_category"] == "indeterminate")
    assert board["last_sync_at"] == overview["meta"]["last_synced_at"]
    most = max(rule["open"] for rule in by_rule)
    top = next(rule for rule in by_rule if rule["open"] == most)  # the first of equals, in catalogue order
    assert board["top_rule"] == {"id": top["rule_id"], "title": top["title"], "open": most}
    unestimated = next(rule for rule in by_rule if rule["rule_id"] == "W-EST-01")
    at_risk = next(rule for rule in by_rule if rule["rule_id"] == "W-SPR-01")
    active = at_risk["open"] > 0 or unestimated["open"] > 0 or active_sprint(two, key)
    assert board["active_sprint"] is active
    # A figure only where there is an active sprint and the rule is measured: never a 0 that means "cannot say".
    assert board["unestimated_in_active_sprint"] == (unestimated["open"] if active and unestimated["measurable"] else None)
    assert board["sprint_at_risk"] == ((at_risk["open"] > 0) if active and at_risk["measurable"] else None)
    assert board["sprint_overdue"] is None  # no sprint is past its end yet
    assert board["tracking_since"] == overview["tracking_since"] is not None
    # The Overview's findings by rule add up to the same open findings.
    assert sum(rule["open"] for rule in by_rule) == board["open_breaches"] + board["open_warnings"]


@pytest.mark.parametrize("key", ["DEMO", "AIL"])
def test_the_trend_is_eight_weeks_ending_with_the_open_findings_now(client: TestClient, portfolio: dict[str, Any],
                                                                     key: str) -> None:
    trend = board_of(portfolio, key)["open_findings_trend"]
    weekly = [
        [point["value"] for point in client.get("/api/v1/trends", params={"project": key, "metric": metric,
                                                                           "last_n": 8}).json()["series"]]
        for metric in ("open_breaches", "open_warnings")
    ]
    assert len(trend) == 8
    assert trend[:-1] == [breaches + warnings for breaches, warnings in zip(*weekly, strict=True)][:-1]
    board = board_of(portfolio, key)
    assert trend[-1] == board["open_breaches"] + board["open_warnings"]


def test_a_board_last_synced_over_two_hours_ago_is_stale(two: Settings, portfolio: dict[str, Any]) -> None:
    # JO-14: one staleness rule everywhere, the top bar's and the digest's two hours (it was a day here).
    assert (board_of(portfolio, "AIL")["stale"], board_of(portfolio, "DEMO")["stale"]) == (True, False)
    assert board_of(portfolio, "AIL")["last_sync_at"] == to_iso(EARLIER)
    clock = Clock(NOW + timedelta(hours=2))
    later = client_of(two, clock)
    assert board_of(later.get("/api/v1/portfolio").json(), "DEMO")["stale"] is False  # exactly two hours: not yet
    clock.at = NOW + timedelta(hours=2, milliseconds=1)
    assert board_of(later.get("/api/v1/portfolio").json(), "DEMO")["stale"] is True
    assert STALE_AFTER == timedelta(hours=2) and digest.STALE_AFTER is STALE_AFTER  # the digest's rule is this one


Attention = tuple[str, str, str, str, int]  # project, issue key, rule, first seen, open findings


def expected_attention(settings: Settings, boards: list[str], now: datetime) -> list[Attention]:
    """The oldest open breaches on issues across the boards, worked out here from the findings themselves: one for each
    ticket and rule, first seen at its oldest open finding's first sighting, with how many distinct findings are open;
    oldest first, and breaches first seen at one moment taking turns by board (each board's in issue-number order)."""
    at = to_iso(now)
    conn = sqlite3.connect(settings.db_file)
    try:
        rows = conn.execute(
            "SELECT project_key, issue_key, rule_id, finding_id, first_seen_at FROM findings WHERE severity = 'breach'"
            " AND issue_key NOT LIKE 'sprint:%' AND first_seen_at <= ? AND (resolved_at IS NULL OR resolved_at > ?)",
            (at, at)).fetchall()
    finally:
        conn.close()
    pairs: dict[tuple[str, str, str], tuple[str, set[str]]] = {}
    for project, issue, rule, finding, first in rows:
        if project in boards:
            opened, ids = pairs.get((project, issue, rule), (first, set()))
            pairs[(project, issue, rule)] = (min(opened, first), ids | {finding})
    grouped = [(project, issue, rule, opened, len(ids)) for (project, issue, rule), (opened, ids) in pairs.items()]
    number = lambda row: int(row[1].rpartition("-")[2])  # noqa: E731
    grouped.sort(key=lambda row: (row[0], row[3], number(row), row[1], row[2]))
    turns: dict[tuple[str, str], int] = {}
    keyed = []
    for row in grouped:
        turn = turns.get((row[0], row[3]), 0)
        turns[(row[0], row[3])] = turn + 1
        keyed.append(((row[3], turn, boards.index(row[0]), number(row), row[1], row[2]), row))
    keyed.sort(key=lambda item: item[0])
    return [row for _key, row in keyed[:10]]


def listed(attention: list[dict[str, Any]]) -> list[Attention]:
    return [(item["project"], item["issue_key"], item["rule_id"], item["opened_at"], item["findings"])
            for item in attention]


def test_attention_is_the_ten_oldest_open_breaches_across_the_boards(two: Settings, portfolio: dict[str, Any]) -> None:
    rules = {rule.id: rule.title for rule in RulesConfig.load(two.rules_file).rules}
    attention = portfolio["attention"]
    assert len(attention) == 10 and all(set(item) == ATTENTION_FIELDS for item in attention)
    assert listed(attention) == expected_attention(two, ["DEMO", "AIL"], NOW)
    for item in attention:
        assert item["severity"] == "breach" and item["title"] == rules[item["rule_id"]]
        assert item["issue_key"].startswith(f"{item['project']}-")
        opened, seen = parse_ts(item["opened_at"]), parse_ts(item["last_seen_at"])
        assert opened is not None and seen is not None
        # AIL last synced three days ago, so it is stale: its breaches are aged as at that sync (one clock, JO-14).
        assert seen == EARLIER and item["age_days"] == (seen - opened).days
    assert attention[0]["project"] == "AIL" and attention[0]["age_days"] == 0  # AIL's are the oldest, seen at its sync


def test_seen_for_runs_on_the_clock_findings_reads_stopping_at_the_last_sync_once_it_is_stale(tmp_path: Path) -> None:
    """JO-14: an open breach's age counts to now while its board's last sync is fresh, and stops at the last rules run
    that saw it once that sync is stale, as Findings' "Open for" does ("at last sync"): never "Seen for 9 days" on the
    Portfolio beside "Open for 2 d at last sync" in the drawer for the same breach."""
    settings = make_settings(tmp_path, "AIL")
    seed(settings, {"AIL": (160, EARLIER)})
    seen = EARLIER + timedelta(days=2)  # a later sync, and the rules run after it that still saw every open breach
    conn = sqlite3.connect(settings.db_file)
    try:
        conn.execute("UPDATE findings SET last_seen_at = ? WHERE resolved_at IS NULL", (to_iso(seen),))
        conn.execute("UPDATE sync_state SET last_synced_at = ? WHERE scope = 'AIL'", (to_iso(seen),))
        conn.commit()
    finally:
        conn.close()
    fresh = client_of(settings, Clock(seen + timedelta(hours=1))).get("/api/v1/portfolio").json()
    assert board_of(fresh, "AIL")["stale"] is False
    assert [(item["last_seen_at"], item["age_days"]) for item in fresh["attention"][:3]] == [(to_iso(seen), 2)] * 3
    week = client_of(settings, Clock(seen + timedelta(days=7))).get("/api/v1/portfolio").json()
    assert board_of(week, "AIL")["stale"] is True
    assert [item["age_days"] for item in week["attention"][:3]] == [2] * 3  # as at the last sync, not 9


def test_ties_take_turns_by_board_so_no_board_fills_the_list(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "AIL,DEMO")
    seed(settings, {"DEMO": (120, NOW), "AIL": (60, NOW)})  # both first seen at one moment, as one rules run does
    attention = client_of(settings).get("/api/v1/portfolio").json()["attention"]
    assert listed(attention) == expected_attention(settings, ["AIL", "DEMO"], NOW)
    assert [item["project"] for item in attention] == ["AIL", "DEMO"] * 5  # turns, the first board first
    for project in ("AIL", "DEMO"):
        numbers = [int(item["issue_key"].rpartition("-")[2]) for item in attention if item["project"] == project]
        assert numbers == sorted(numbers)  # by number: AIL-9 before AIL-10
    assert all(item["age_days"] == 0 for item in attention)


def add_finding(conn: sqlite3.Connection, like: str, finding_id: str, first_seen: datetime,
                resolved: datetime | None = None) -> None:
    """Another finding of `like`'s rule on `like`'s ticket, as another transition that skipped a stage makes one."""
    conn.execute(
        "INSERT INTO findings (finding_id, rule_id, rule_version, issue_key, project_key, kind, severity, evidence_key,"
        " message, evidence_json, first_seen_at, last_seen_at, resolved_at, resolved_why, run_id)"
        " SELECT ?, rule_id, rule_version, issue_key, project_key, kind, severity, ?, message, evidence_json, ?,"
        " last_seen_at, ?, ?, run_id FROM findings WHERE finding_id = ?",
        (finding_id, f"skip-{finding_id}", to_iso(first_seen), to_iso(resolved) if resolved else None,
         "cleared" if resolved else None, like))


def open_finding(conn: sqlite3.Connection, item: dict[str, Any]) -> str:
    row = conn.execute("SELECT finding_id FROM findings WHERE project_key = ? AND issue_key = ? AND rule_id = ?"
                       " AND resolved_at IS NULL ORDER BY first_seen_at LIMIT 1",
                       (item["project"], item["issue_key"], item["rule_id"])).fetchone()
    return str(row[0])


def test_a_ticket_with_several_open_findings_of_one_rule_is_one_row_with_how_many(tmp_path: Path) -> None:
    """13 Nov 2026: R-FLOW-02 opens a finding for each transition that skipped a stage, so AIL-99 to AIL-102 each held
    two open "Skipped a stage" findings, and "Needs attention" listed each ticket and rule twice, which reads as a
    duplicate (and React refused the repeated key). One row for each ticket and rule: first seen at its oldest open
    finding, aged from that, with how many are open, and still the 10 oldest distinct tickets and rules."""
    settings = make_settings(tmp_path, "DEMO")
    seed(settings, {"DEMO": (120, NOW)})
    before = client_of(settings).get("/api/v1/portfolio").json()["attention"]
    assert len(before) == 10
    first, second = before[0], before[1]
    conn = sqlite3.connect(settings.db_file)
    try:
        # The first: an older skip still open (first seen 5 days ago), and an older one still that has been resolved.
        add_finding(conn, open_finding(conn, first), "older-skip", NOW - timedelta(days=5))
        add_finding(conn, open_finding(conn, first), "resolved-skip", NOW - timedelta(days=9),
                    resolved=NOW - timedelta(days=1))
        # The second: two skips one rules run saw at the same moment, as on AIL-99.
        add_finding(conn, open_finding(conn, second), "same-moment-skip", parse_ts(second["opened_at"]) or NOW)
        conn.commit()
    finally:
        conn.close()
    after = client_of(settings).get("/api/v1/portfolio").json()["attention"]
    pairs = [(item["project"], item["issue_key"], item["rule_id"]) for item in after]
    assert len(set(pairs)) == len(pairs) == 10  # never one ticket and rule twice
    assert pairs == [(item["project"], item["issue_key"], item["rule_id"]) for item in before]  # none pushed out
    assert listed(after) == expected_attention(settings, ["DEMO"], NOW)
    top = after[0]
    # Aged from the oldest open one; the resolved one is neither counted nor dated.
    assert (top["findings"], top["opened_at"], top["age_days"]) == (
        first["findings"] + 1, to_iso(NOW - timedelta(days=5)), 5)
    assert top["last_seen_at"] == first["last_seen_at"]
    assert (after[1]["findings"], after[1]["opened_at"]) == (second["findings"] + 1, second["opened_at"])
    assert after[2:] == before[2:]


def test_each_open_finding_is_counted_once_whatever_else_the_store_holds_for_it(tmp_path: Path) -> None:
    """A join to what else the store keeps on a finding or its ticket (the warnings that foreshadowed it, its ticket's
    status changes) would repeat the finding for each row it meets, listing it twice or counting it as two: the list
    reads the findings alone, so each finding is one, once."""
    settings = make_settings(tmp_path, "DEMO")
    seed(settings, {"DEMO": (120, NOW)})
    before = client_of(settings).get("/api/v1/portfolio").json()["attention"]
    conn = sqlite3.connect(settings.db_file)
    try:
        breach = open_finding(conn, before[0])
        conn.executemany("INSERT INTO warning_links (warning_finding_id, breach_finding_id, linked_at) VALUES (?, ?, ?)",
                         [(f"warning-{index}", breach, to_iso(NOW)) for index in range(3)])
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM status_changes WHERE issue_key = ?",
                            (before[0]["issue_key"],)).fetchone()[0] > 1  # a join to these would repeat it too
    finally:
        conn.close()
    after = client_of(settings).get("/api/v1/portfolio").json()["attention"]
    assert after == before
    assert listed(after) == expected_attention(settings, ["DEMO"], NOW)  # counted by distinct finding id
    pairs = [(item["project"], item["issue_key"], item["rule_id"]) for item in after]
    assert len(set(pairs)) == len(pairs)


def test_a_reader_of_some_boards_gets_their_own_ten_oldest(two: Settings, client: TestClient,
                                                           portfolio: dict[str, Any]) -> None:
    # Every one of the service's ten oldest is on AIL: a reader of DEMO alone still gets DEMO's ten oldest.
    assert {item["project"] for item in portfolio["attention"]} == {"AIL"}
    mine = client.get("/api/v1/portfolio", params={"board": "DEMO"}).json()
    assert [board["project"]["key"] for board in mine["boards"]] == ["DEMO"]
    assert mine["boards"][0] == board_of(portfolio, "DEMO")
    assert listed(mine["attention"]) == expected_attention(two, ["DEMO"], NOW)
    assert len(mine["attention"]) == 10
    both = client.get("/api/v1/portfolio", params=[("board", "ail"), ("board", "DEMO")]).json()
    assert (both["boards"], both["attention"]) == (portfolio["boards"], portfolio["attention"])  # in the service's order
    gone = client.get("/api/v1/portfolio", params={"board": "GONE"})  # a board the service no longer serves
    assert gone.status_code == 200 and (gone.json()["boards"], gone.json()["attention"]) == ([], [])


def test_a_board_that_never_synced_has_no_row_and_is_counted(tmp_path: Path) -> None:
    """JO-17: the portfolio covers the boards that have synced, and says how many more have not yet."""
    settings = make_settings(tmp_path, "DEMO,AIL")
    seed(settings, {"DEMO": (60, NOW)})
    client = client_of(settings)
    body = client.get("/api/v1/portfolio").json()
    assert [board["project"]["key"] for board in body["boards"]] == ["DEMO"]
    assert body["boards_not_synced"] == body["not_synced"] == 1
    assert body["meta"]["projects"] == ["DEMO", "AIL"]  # every board the service serves
    assert {item["project"] for item in body["attention"]} <= {"DEMO"}
    asked = client.get("/api/v1/portfolio", params={"board": "AIL"}).json()  # a reader of AIL alone
    assert (asked["boards"], asked["attention"], asked["boards_not_synced"]) == ([], [], 1)


def test_before_any_board_syncs_the_service_wide_routes_answer_with_no_sync_time(tmp_path: Path) -> None:
    # A first deployment, or Jira settings that do not work yet: the dashboard reads a null sync time (4.16 to 4.18).
    settings = make_settings(tmp_path, "DEMO,AIL")
    with Store.open(settings.db_file):
        pass
    client = client_of(settings)
    for path, params in (("/api/v1/portfolio", {}), ("/api/v1/usage", {"level": "people"}),
                         ("/api/v1/access-log", {"level": "people"})):
        answer = client.get(path, params=params)
        assert answer.status_code == 200, (path, answer.text)
        assert answer.json()["meta"]["last_synced_at"] is None
    body = client.get("/api/v1/portfolio").json()
    assert (body["boards"], body["boards_not_synced"]) == ([], 2)


def test_a_sprint_past_its_end_with_work_not_done_is_at_risk_and_says_when_it_ended(tmp_path: Path) -> None:
    """18 Nov 2026: AILED's active sprint ended on 16 Nov and is still open with 2 tickets not done, and its row said
    "On track". A sprint past its end with work not done is at risk (W-SPR-01 finds it), and the row says when it
    ended and how much is left, from that finding's own evidence."""
    settings = make_settings(tmp_path, "DEMO")
    seed(settings, {"DEMO": (120, NOW)})
    ended = NOW + _ACTIVE_SPRINT_ENDS_IN  # the demo's active sprint ends two days after NOW
    later = ended + timedelta(days=2)
    with Store.open(settings.db_file) as store:  # the next rules run, with the sprint still open past its end
        run_rules(store, ProcessConfig.load(settings.process_file), RulesConfig.load(settings.rules_file),
                  FieldsConfig.load(settings.fields_file), projects=["DEMO"], now=lambda: later)
    conn = sqlite3.connect(settings.db_file)
    try:
        [(message, evidence)] = conn.execute(
            "SELECT message, evidence_json FROM findings WHERE rule_id = 'W-SPR-01' AND resolved_at IS NULL").fetchall()
    finally:
        conn.close()
    found = json.loads(evidence)
    assert found["overdue"] is True and found["not_done"] > 0
    assert message.endswith(f"ended 2.0 days ago with {found['not_done']} of {found['issues']} issues not done")
    board = board_of(client_of(settings, lambda: later).get("/api/v1/portfolio").json(), "DEMO")
    assert (board["active_sprint"], board["sprint_at_risk"]) == (True, True)
    assert board["sprint_overdue"] == {"ended_at": to_iso(ended), "not_done": found["not_done"]}


@pytest.mark.parametrize("params", [{"days": "7"}, [("project", "DEMO"), ("project", "AIL")], {"level": "admin"},
                                    [("board", "DEMO"), ("board", "demo")], {"board": "not a key"}, {"board": ""}])
def test_it_takes_only_project_level_and_boards(client: TestClient, params: Any) -> None:
    assert client.get("/api/v1/portfolio", params=params).status_code == 422


def test_the_level_and_the_board_picked_change_nothing(client: TestClient, portfolio: dict[str, Any]) -> None:
    for params in ({"level": "team"}, {"level": "people"}, {"project": "AIL"}):
        body = client.get("/api/v1/portfolio", params=params).json()
        assert (body["boards"], body["attention"]) == (portfolio["boards"], portfolio["attention"])
    assert client.get("/api/v1/portfolio", params={"project": "NOPE"}).status_code == 404


def test_the_portfolio_holds_no_person_and_needs_no_viewer(two: Settings, portfolio: dict[str, Any]) -> None:
    text = str(portfolio)
    assert "acc-" not in text and "Ada Brooks" not in text and "avatar" not in text
    unnamed = TestClient(create_app(two, provider=ScriptedProvider(), now=lambda: NOW), headers={"X-Service-Key": KEY})
    assert unnamed.get("/api/v1/portfolio").status_code == 200
