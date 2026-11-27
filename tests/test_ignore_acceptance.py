from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from oversight.api.app import create_app
from oversight.api.people import person_id
from oversight.api.refresh import Refresher
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.insights.weekly import build_candidates
from oversight.llm.scripted import ScriptedProvider
from oversight.mcp_server.server import build_server
from oversight.notify.digest import build_digest
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.rules.settings import Change, save_board_settings
from oversight.settings import Settings
from oversight.store.db import SCHEMA_VERSION, Store, open_readonly
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)  # the seed's last rules run
SAVED = NOW + timedelta(minutes=5)  # when an admin ignores
KEY = "test-key"
ADMIN = "0123456789abcdef0123456789abcdef"  # a viewer hash, as the dashboard's server makes one
ADMIN_NAME = "Morgan Reyes"  # a fictional admin
PREVIEW = "/api/v1/findings/ignore/preview?level=people"
IGNORE = "/api/v1/findings/ignore?level=people"
RESTORE = "/api/v1/findings/restore?level=people"
IGNORED = "/api/v1/findings/ignored?level=people"
CATALOGUE = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")
WAIT_S = 120
BODY_CAP = 256 * 1024
FINDING_ID = re.compile(r"^[0-9a-f]{32}$")
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$")
META_KEYS = {"data_mode", "last_synced_at", "projects", "rules_version", "process_version", "generated_at",
             "report_timezone"}
FINDING_KEYS = {
    "finding_id", "rule_id", "rule_title", "rule_version", "kind", "severity", "state", "message", "scope",
    "issue_key", "issue_summary", "issue_type", "epic_key", "epic_title", "assignee", "sprint", "first_seen_at",
    "last_seen_at", "resolved_at", "resolved_why", "threshold", "evidence", "warned_before", "ignore",
}
IGNORE_KEYS = {"scope", "rule_id", "reason", "by", "at"}
LISTED_KEYS = {"ignore_id", "scope", "issue_key", "issue_summary", "issue_type", "status", "status_category",
               "rule_id", "rule_title", "findings", "reason", "by", "at"}
# The tables of schema version 11: anything else in a snapshot came with version 12.
VERSION_11_TABLES = frozenset({
    "issues", "status_changes", "field_changes", "worklogs", "links", "statuses", "boards", "sync_state", "people",
    "board_members", "status_intervals", "issue_metrics", "runs", "findings", "rule_status", "warning_links",
    "digests", "qa_log", "llm_calls", "insights", "access_log", "rule_settings", "rule_settings_changes",
    "custom_rules",
})
FINDING_IGNORE_COLUMNS = {"ignore_id", "project_key", "issue_key", "rule_id", "scope", "reason", "created_by",
                          "created_at", "restored_by", "restored_at"}

OPEN = """
    FROM findings AS f JOIN issues AS i ON i.issue_key = f.issue_key
    WHERE f.project_key = 'DEMO' AND f.resolved_at IS NULL
"""
BY_NUMBER = "CAST(substr(f.issue_key, instr(f.issue_key, '-') + 1) AS INTEGER)"


class Clock:
    """A clock a test can move."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def settings_at(root: Path) -> Settings:
    return Settings(
        _env_file=None, jira_projects="DEMO", api_service_key=KEY, db_path=root / "oversight.db",
        config_dir=CONFIG_FIXTURES, reports_dir=root / "reports",
    )


def seed(settings: Settings) -> None:
    """DEMO with three rules runs: two rules since 35 days back, the rest since 3 days back; a looser effort limit
    clears some findings at the last run, so the board holds open, old, new and cleared findings."""
    process, fields = ProcessConfig.load(settings.process_file), FieldsConfig.load(settings.fields_file)
    early = CATALOGUE.model_copy(deep=True)
    for rule in early.rules:
        rule.enabled = rule.id in ("R-EST-01", "R-FLOW-02")
    looser = CATALOGUE.model_copy(deep=True)
    for rule in looser.rules:
        if rule.id in ("R-EFF-01", "W-EFF-01"):
            rule.params["threshold"] = {"R-EFF-01": 1.2, "W-EFF-01": 0.5}[rule.id]
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=NOW, seed=20261104)
        run_derive(store, process, projects=["DEMO"], now=lambda: NOW)
        for catalogue, when in ((early, NOW - timedelta(days=35)), (looser, NOW - timedelta(days=3)), (CATALOGUE, NOW)):
            run_rules(store, process, catalogue, fields, projects=["DEMO"], now=lambda when=when: when)


def run_the_rules(settings: Settings, at: datetime, catalogue: RulesConfig | None = None) -> None:
    process, fields = ProcessConfig.load(settings.process_file), FieldsConfig.load(settings.fields_file)
    with Store.open(settings.db_file) as store:
        run_rules(store, process, catalogue or RulesConfig.load(settings.rules_file), fields, projects=["DEMO"],
                  now=lambda: at)


class Steps:
    """The re-check's runner: the rules step runs in this process, on the test's clock."""

    def __init__(self, settings: Settings, clock: Clock, *, hold: bool = False) -> None:
        self.settings = settings
        self.clock = clock
        self.calls: list[tuple[str, ...]] = []
        self.release = threading.Event()
        if not hold:
            self.release.set()

    def __call__(self, argv: Sequence[str]) -> int:
        self.release.wait(WAIT_S)
        self.calls.append(tuple(argv))
        if argv[0] == "rules":
            run_the_rules(self.settings, self.clock())
        return 0


def headers(viewer: str | None = ADMIN, name: str | None = ADMIN_NAME) -> dict[str, str]:
    found = {"X-Service-Key": KEY}
    if viewer is not None:
        found["X-Oversight-Viewer"] = viewer
    if name is not None:
        found["X-Oversight-Viewer-Name"] = quote(name)
    return found


def serve(
    settings: Settings, *, clock: Clock | None = None, steps: Steps | None = None
) -> tuple[TestClient, Refresher]:
    clock = clock or Clock(SAVED)
    refresher = Refresher(runner=steps or Steps(settings, clock), now=clock)
    app = create_app(settings, provider=ScriptedProvider(), now=clock, refresher=refresher)
    return TestClient(app, headers=headers()), refresher


def settle(refresher: Refresher) -> None:
    refresher.wait(WAIT_S)
    job = refresher.recheck_of("DEMO")
    assert job is None or job.state in ("succeeded", "failed"), "the re-check did not end in time"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    settings = settings_at(tmp_path_factory.mktemp("ignore_seed"))
    seed(settings)
    return settings.db_file


@pytest.fixture
def board(seeded: Path, tmp_path: Path) -> Settings:
    """A copy of the seeded snapshot for this test alone: it may write to it."""
    settings = settings_at(tmp_path)
    for suffix in ("", "-wal"):
        source = Path(f"{seeded}{suffix}")
        if source.exists():
            shutil.copyfile(source, Path(f"{settings.db_file}{suffix}"))
    return settings


def query(settings: Settings, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    conn = open_readonly(settings.db_file)
    try:
        return [tuple(row) for row in conn.execute(sql, tuple(params))]
    finally:
        conn.close()


def write(settings: Settings, sql: str, params: Sequence[Any] = ()) -> None:
    """A change to the synthetic board's Jira data, as a sync would bring it."""
    with Store.open(settings.db_file) as store, store.transaction():
        store.conn.execute(sql, tuple(params))


def everything(settings: Settings) -> dict[str, list[tuple[Any, ...]]]:
    """Every row an ignore or a restore could touch, to show that nothing was written."""
    return {table: query(settings, f"SELECT * FROM {table} ORDER BY 1") for table in ("findings", "finding_ignores")}


def first_open(settings: Settings, where: str = "", params: Sequence[Any] = ()) -> tuple[str, str, str]:
    """(finding id, issue key, rule id) of the open finding matching `where`, on the lowest-numbered ticket."""
    rows = query(settings, f"SELECT f.finding_id, f.issue_key, f.rule_id {OPEN} {where}"
                           f" ORDER BY {BY_NUMBER}, f.rule_id, f.finding_id LIMIT 1", params)
    assert rows, f"no open finding {where}"
    return rows[0]


def ticket_with_rules(settings: Settings, category: str, *, besides: Sequence[str] = ()) -> str:
    """A ticket in this status category with open findings of two rules or more (none of `besides`)."""
    others = f" AND f.issue_key NOT IN ({', '.join('?' * len(besides))})" if besides else ""
    rows = query(settings, f"SELECT f.issue_key {OPEN} AND i.status_category = ?{others}"
                           f" GROUP BY f.issue_key HAVING COUNT(DISTINCT f.rule_id) > 1 ORDER BY {BY_NUMBER} LIMIT 1",
                 (category, *besides))
    assert rows, category
    return rows[0][0]


def open_on(settings: Settings, issue_key: str) -> dict[str, str]:
    """The ticket's open findings: finding id -> rule id."""
    return dict(query(settings, "SELECT finding_id, rule_id FROM findings WHERE issue_key = ? AND resolved_at IS NULL",
                      (issue_key,)))


def ignored_ids(settings: Settings) -> set[str]:
    return {row[0] for row in query(settings, "SELECT finding_id FROM findings WHERE resolved_why = 'ignored'")}


def active_rows(settings: Settings) -> list[tuple[Any, ...]]:
    return query(settings, "SELECT ignore_id, issue_key, rule_id, scope FROM finding_ignores"
                           " WHERE restored_at IS NULL ORDER BY issue_key, rule_id")


def ignore(client: TestClient, target: dict[str, Any], scope: str = "finding", **extra: Any) -> dict[str, Any]:
    response = client.post(IGNORE, json={"target": target, "scope": scope, **extra})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def preview(client: TestClient, target: dict[str, Any], scope: str = "finding") -> dict[str, Any]:
    response = client.post(PREVIEW, json={"target": target, "scope": scope})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def all_items(client: TestClient, params: Sequence[tuple[str, Any]] = ()) -> list[dict[str, Any]]:
    """Every finding a query lists, page by page."""
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        response = client.get("/api/v1/findings", params=[*params, ("page_size", 100), ("page", page)])
        assert response.status_code == 200, response.text
        body = response.json()
        items += body["items"]
        if not body["items"] or len(items) >= body["total"]:
            assert len(items) == body["total"]
            return items
        page += 1


def facet(body: dict[str, Any], name: str) -> dict[str, int]:
    return {entry["value"]: entry["count"] for entry in body["facets"][name]}


def is_timestamp(value: Any) -> bool:
    return isinstance(value, str) and ISO_Z.fullmatch(value) is not None


def assert_meta(meta: dict[str, Any]) -> None:
    assert set(meta) == META_KEYS and meta["projects"] == ["DEMO"] and meta["data_mode"] == "live"
    assert is_timestamp(meta["generated_at"])


def assert_ignore(value: Any, *, scope: str, rule_id: str | None, reason: str | None, by: str | None,
                  at: datetime = SAVED, ignore_id: str | None = None) -> None:
    """The finding's ignore: the contract's five fields, and at most the id a Restore of it needs."""
    assert isinstance(value, dict) and IGNORE_KEYS <= set(value) <= IGNORE_KEYS | {"ignore_id"}, value
    assert {name: value[name] for name in IGNORE_KEYS} == {
        "scope": scope, "rule_id": rule_id, "reason": reason, "by": by, "at": to_iso(at)}
    if "ignore_id" in value:
        assert FINDING_ID.fullmatch(value["ignore_id"]) and (ignore_id is None or value["ignore_id"] == ignore_id)


# --- one finding, one ticket, a query ---------------------------------------------------------------------------


def test_ignoring_a_finding_resolves_it_at_once_and_keeps_the_ticket_and_rule_never_the_finding_id(
    board: Settings,
) -> None:
    client, _ = serve(board)
    key = ticket_with_rules(board, "done")
    on_ticket = open_on(board, key)
    finding_id, rule_id = sorted(on_ticket.items(), key=lambda item: item[1])[0]

    response = client.post(IGNORE, json={"target": {"finding_ids": [finding_id]}, "scope": "finding",
                                         "reason": "Closed long ago"})

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"meta", "ignored", "ignore_ids"}
    assert_meta(body["meta"])
    assert body["ignored"] == {"findings": 1, "tickets": 1}
    [ignore_id] = body["ignore_ids"]
    assert isinstance(ignore_id, str) and ignore_id
    assert query(board, "SELECT project_key, issue_key, rule_id, scope, reason, created_by, created_at, restored_by,"
                        " restored_at FROM finding_ignores WHERE ignore_id = ?", (ignore_id,)) == [
        ("DEMO", key, rule_id, "finding", "Closed long ago", ADMIN_NAME, to_iso(SAVED), None, None)]
    assert query(board, "SELECT resolved_at, resolved_why FROM findings WHERE finding_id = ?", (finding_id,)) == [
        (to_iso(SAVED), "ignored")]
    assert open_on(board, key) == {fid: rule for fid, rule in on_ticket.items() if rule != rule_id}  # the rest stay

    shown = client.get(f"/api/v1/findings/{finding_id}").json()["finding"]
    assert set(shown) == FINDING_KEYS
    assert (shown["state"], shown["resolved_at"], shown["resolved_why"]) == ("ignored", to_iso(SAVED), "ignored")
    assert_ignore(shown["ignore"], scope="finding", rule_id=rule_id, reason="Closed long ago", by=None,
                  ignore_id=ignore_id)
    named = client.get(f"/api/v1/findings/{finding_id}?level=people").json()["finding"]
    assert_ignore(named["ignore"], scope="finding", rule_id=rule_id, reason="Closed long ago", by=ADMIN_NAME,
                  ignore_id=ignore_id)


def test_a_ticket_ignore_covers_every_rule_on_the_ticket(board: Settings) -> None:
    client, _ = serve(board)
    key = ticket_with_rules(board, "indeterminate")
    on_ticket = open_on(board, key)

    body = ignore(client, {"issue_keys": [key]}, scope="ticket")

    assert body["ignored"] == {"findings": len(on_ticket), "tickets": 1}
    [ignore_id] = body["ignore_ids"]
    assert query(board, "SELECT issue_key, rule_id, scope, created_by FROM finding_ignores WHERE ignore_id = ?",
                 (ignore_id,)) == [(key, None, "ticket", ADMIN_NAME)]
    assert open_on(board, key) == {}
    assert ignored_ids(board) == set(on_ticket)
    for item in all_items(client, [("state", "ignored"), ("level", "people")]):
        assert (item["issue_key"], item["state"], item["resolved_why"]) == (key, "ignored", "ignored")
        assert_ignore(item["ignore"], scope="ticket", rule_id=None, reason=None, by=ADMIN_NAME, ignore_id=ignore_id)


def test_finding_ids_with_the_ticket_scope_ignore_every_rule_on_their_tickets(board: Settings) -> None:
    client, _ = serve(board)
    key = ticket_with_rules(board, "done")
    on_ticket = open_on(board, key)
    one = sorted(on_ticket)[0]

    asked = preview(client, {"finding_ids": [one]}, scope="ticket")
    body = ignore(client, {"finding_ids": [one]}, scope="ticket")

    assert (asked["findings"], asked["tickets"]) == (len(on_ticket), 1)
    assert body["ignored"] == {"findings": len(on_ticket), "tickets": 1}
    assert [row[1:] for row in active_rows(board)] == [(key, None, "ticket")]
    assert ignored_ids(board) == set(on_ticket)


def test_issue_keys_name_whole_tickets_so_they_need_the_ticket_scope(board: Settings) -> None:
    client, _ = serve(board)
    key = ticket_with_rules(board, "done")
    before = everything(board)
    for path in (PREVIEW, IGNORE):
        response = client.post(path, json={"target": {"issue_keys": [key]}, "scope": "finding"})
        assert response.status_code == 422, response.text
    assert everything(board) == before


def test_ignore_findings_on_finished_tickets_takes_exactly_what_the_findings_filter_lists(board: Settings) -> None:
    client, _ = serve(board)
    listed = {item["finding_id"] for item in all_items(client, [("ticket", "finished")])}
    expected = query(board, f"SELECT f.finding_id, f.issue_key, f.rule_id {OPEN} AND i.status_category = 'done'")
    assert listed == {row[0] for row in expected} and len(listed) > 100
    on_open_tickets = {row[0] for row in query(board, f"SELECT f.finding_id {OPEN} AND i.status_category != 'done'")}

    asked = preview(client, {"query": {"ticket": "finished"}})
    body = ignore(client, {"query": {"ticket": "finished"}}, reason="Finished work: not going back to it")

    tickets = {row[1] for row in expected}
    assert (asked["findings"], asked["tickets"], asked["already_ignored"]) == (len(listed), len(tickets), 0)
    assert body["ignored"] == {"findings": len(listed), "tickets": len(tickets)}
    pairs = {(row[1], row[2]) for row in expected}
    assert len(body["ignore_ids"]) == len(set(body["ignore_ids"])) == len(pairs)
    assert {row[1:3] for row in active_rows(board)} == pairs
    assert client.get("/api/v1/findings?ticket=finished").json()["total"] == 0
    assert {item["finding_id"] for item in all_items(client, [("state", "ignored")])} == listed
    assert {row[0] for row in query(board, f"SELECT f.finding_id {OPEN}")} == on_open_tickets


def test_a_query_target_is_the_open_findings_the_same_filters_list_on_get_findings(board: Settings) -> None:
    client, _ = serve(board)
    filters = {"rule": ["W-EST-01", "W-AGE-01"], "severity": "warning", "ticket": "open", "first_seen": "7d"}
    params = [("rule", "W-EST-01"), ("rule", "W-AGE-01"), ("severity", "warning"), ("ticket", "open"),
              ("first_seen", "7d")]
    listed = {item["finding_id"] for item in all_items(client, params)}
    assert listed

    body = ignore(client, {"query": filters})

    assert body["ignored"]["findings"] == len(listed)
    assert ignored_ids(board) == listed
    assert client.get("/api/v1/findings", params=params).json()["total"] == 0


def targets(settings: Settings, kind: str) -> tuple[dict[str, Any], str]:
    if kind == "finding ids":
        rows = query(settings, f"SELECT f.finding_id {OPEN} AND f.rule_id IN ('W-AGE-01', 'R-EST-01', 'R-FLOW-02')"
                               f" ORDER BY {BY_NUMBER} LIMIT 12")
        return {"finding_ids": [row[0] for row in rows]}, "finding"
    if kind == "issue keys":
        done = ticket_with_rules(settings, "done")
        return {"issue_keys": [done, ticket_with_rules(settings, "indeterminate", besides=[done])]}, "ticket"
    return {"query": {"rule": ["R-FLOW-02", "R-EFF-01"], "ticket": "finished"}}, "finding"


@pytest.mark.parametrize("kind", ["finding ids", "issue keys", "query"])
def test_a_preview_says_what_the_ignore_then_does_and_writes_nothing(board: Settings, kind: str) -> None:
    client, _ = serve(board)
    target, scope = targets(board, kind)
    before = everything(board)

    response = client.post(PREVIEW, json={"target": target, "scope": scope})

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    asked = response.json()
    assert everything(board) == before  # a preview never writes
    assert set(asked) == {"meta", "findings", "tickets", "by_rule", "already_ignored"}
    assert_meta(asked["meta"])
    assert asked["already_ignored"] == 0
    assert sum(entry["findings"] for entry in asked["by_rule"]) == asked["findings"] > 0
    for entry in asked["by_rule"]:
        assert set(entry) == {"rule_id", "rule_title", "findings"} and entry["findings"] > 0
        assert entry["rule_title"] == CATALOGUE.by_id(entry["rule_id"]).title
    order = [rule.id for rule in CATALOGUE.rules]
    assert [entry["rule_id"] for entry in asked["by_rule"]] == sorted(
        (entry["rule_id"] for entry in asked["by_rule"]), key=order.index)

    done = ignore(client, target, scope=scope)

    assert done["ignored"] == {"findings": asked["findings"], "tickets": asked["tickets"]}
    now_ignored = Counter(rule for (rule,) in query(board, "SELECT rule_id FROM findings WHERE resolved_why = 'ignored'"))
    assert now_ignored == Counter({entry["rule_id"]: entry["findings"] for entry in asked["by_rule"]})
    tickets = query(board, "SELECT COUNT(DISTINCT issue_key) FROM findings WHERE resolved_why = 'ignored'")[0][0]
    assert tickets == asked["tickets"]


def test_findings_already_ignored_are_counted_as_such_and_their_ignore_is_reused_never_duplicated(
    board: Settings,
) -> None:
    client, _ = serve(board)
    rows = query(board, f"SELECT f.finding_id {OPEN} AND f.rule_id = 'R-EST-01' ORDER BY {BY_NUMBER} LIMIT 3")
    first, second, third = (row[0] for row in rows)
    ignore(client, {"finding_ids": [first, second]})
    held = active_rows(board)
    assert len(held) == 2

    asked = preview(client, {"finding_ids": [first, second, third]})
    body = ignore(client, {"finding_ids": [first, second, third]})

    assert (asked["findings"], asked["already_ignored"]) == (1, 2)
    assert body["ignored"]["findings"] == 1
    now = active_rows(board)
    assert len(now) == 3 and set(held) <= set(now)  # the two ignores stand as they were, one more for the third
    assert len({row[1:3] for row in now}) == 3


@pytest.mark.parametrize(("target", "scope"), [
    ({"query": {"q": "zzzz-no-such-thing"}}, "finding"),
    ({"finding_ids": ["f" * 32]}, "finding"),
    ({"issue_keys": ["DEMO-99999"]}, "ticket"),
    ({"issue_keys": ["NOPE-1"]}, "ticket"),
], ids=["a query matching nothing", "a finding id nobody has", "a key not on the board", "another board's key"])
def test_a_target_that_matches_no_open_finding_ignores_nothing_and_writes_nothing(
    board: Settings, target: dict[str, Any], scope: str
) -> None:
    client, _ = serve(board)
    before = everything(board)
    asked = preview(client, target, scope=scope)
    assert (asked["findings"], asked["tickets"], asked["by_rule"], asked["already_ignored"]) == (0, 0, [], 0)
    body = ignore(client, target, scope=scope)
    assert (body["ignored"], body["ignore_ids"]) == ({"findings": 0, "tickets": 0}, [])
    assert everything(board) == before


@pytest.mark.parametrize("filters", [
    {"person": ["unassigned"]},
    {"epic": ["none"], "severity": "breach"},
    {"q": "Pagination"},
], ids=["unassigned", "no epic, breaches", "search text"])
def test_a_query_target_takes_the_person_epic_and_search_filters_as_the_findings_list_does(
    board: Settings, filters: dict[str, Any]
) -> None:
    client, _ = serve(board)
    params = [(name, value) for name, given in filters.items()
              for value in (given if isinstance(given, list) else [given])]
    listed = {item["finding_id"] for item in all_items(client, params) if item["scope"] == "issue"}
    assert listed

    body = ignore(client, {"query": filters})

    assert body["ignored"]["findings"] == len(listed)
    assert ignored_ids(board) == listed
    assert {item["scope"] for item in all_items(client, params)} <= {"sprint"}  # a sprint is no ticket to ignore


def test_a_pair_restored_can_be_ignored_again_and_keeps_its_history(board: Settings) -> None:
    clock = Clock(SAVED)
    client, refresher = serve(board, clock=clock)
    first, key, rule_id = first_open(board, "AND f.rule_id = 'R-FLOW-02'")
    made = ignore(client, {"finding_ids": [first]}, reason="First time")
    clock.at = SAVED + timedelta(minutes=1)
    assert client.post(RESTORE, json={"ignore_ids": made["ignore_ids"]}).json()["restored"] == 1
    settle(refresher)
    assert open_on(board, key).get(first) == rule_id
    clock.at = SAVED + timedelta(minutes=2)

    again = ignore(client, {"finding_ids": [first]}, reason="Second time")

    assert again["ignored"] == {"findings": 1, "tickets": 1} and again["ignore_ids"] != made["ignore_ids"]
    assert query(board, "SELECT reason, created_at, restored_at FROM finding_ignores ORDER BY created_at") == [
        ("First time", to_iso(SAVED), to_iso(SAVED + timedelta(minutes=1))),
        ("Second time", to_iso(SAVED + timedelta(minutes=2)), None)]
    [listed] = client.get(IGNORED).json()["items"]
    assert (listed["ignore_id"], listed["reason"], listed["findings"]) == (again["ignore_ids"][0], "Second time", 1)
    assert client.post(RESTORE, json={"ignore_ids": made["ignore_ids"]}).json()["restored"] == 0  # restored already


A_FINDING = "0" * 32
BAD_BODIES: list[tuple[str, Any, str | None]] = [
    ("not json", "not json", None),
    ("a list", [], None),
    ("no target", {"scope": "finding"}, "target"),
    ("an empty target", {"target": {}, "scope": "finding"}, "target"),
    ("two targets", {"target": {"finding_ids": [A_FINDING], "issue_keys": ["DEMO-1"]}, "scope": "ticket"}, "target"),
    ("no finding ids", {"target": {"finding_ids": []}, "scope": "finding"}, "finding_ids"),
    ("a finding id that is not one", {"target": {"finding_ids": ["DEMO-1"]}, "scope": "finding"}, "finding_ids"),
    ("10,001 issue keys", {"target": {"issue_keys": [f"DEMO-{n}" for n in range(1, 10_002)]}, "scope": "ticket"},
     "issue_keys"),
    ("an issue key that is not one", {"target": {"issue_keys": ["DEMO1"]}, "scope": "ticket"}, "issue_keys"),
    ("a scope that is not one", {"target": {"finding_ids": [A_FINDING]}, "scope": "epic"}, "scope"),
    ("a reason over 500 characters", {"target": {"finding_ids": [A_FINDING]}, "scope": "finding",
                                      "reason": "x" * 501}, "reason"),
    ("a reason that is not text", {"target": {"finding_ids": [A_FINDING]}, "scope": "finding", "reason": 7}, "reason"),
    ("a field it does not take", {"target": {"finding_ids": [A_FINDING]}, "scope": "finding", "colour": "red"},
     "colour"),
    ("a filter GET /findings does not take", {"target": {"query": {"state": "open"}}, "scope": "finding"}, "state"),
    ("a ticket filter that is not one", {"target": {"query": {"ticket": "closed"}}, "scope": "finding"}, "ticket"),
    ("a rule that is not one", {"target": {"query": {"rule": ["EFF"]}}, "scope": "finding"}, "rule"),
]


@pytest.mark.parametrize(("body", "field"), [case[1:] for case in BAD_BODIES], ids=[case[0] for case in BAD_BODIES])
def test_a_bad_body_is_refused_naming_its_field_and_nothing_is_written(
    board: Settings, body: Any, field: str | None
) -> None:
    client, _ = serve(board)
    before = everything(board)
    content = body if isinstance(body, str) else json.dumps(body)
    for path in (IGNORE, PREVIEW):
        response = client.post(path, content=content, headers={"Content-Type": "application/json"})
        assert response.status_code == 422, (path, response.text)
        if field is not None:
            assert field in response.text, response.text
    assert everything(board) == before


@pytest.mark.parametrize("body", [
    {"ignore_ids": []},
    {"ignore_ids": "all"},
    {},
    {"ignore_ids": [f"{n:032x}" for n in range(25_001)]},
    {"ignore_ids": [uuid.uuid4().hex], "reason": "x"},
], ids=["none", "not a list", "no ids", "25,001 ids", "a field it does not take"])
def test_a_bad_restore_is_refused_and_nothing_is_written(board: Settings, body: dict[str, Any]) -> None:
    client, refresher = serve(board)
    before = everything(board)
    response = client.post(RESTORE, json=body)
    assert response.status_code in (413, 422), response.text  # the body cap may refuse it first
    settle(refresher)
    assert everything(board) == before
    assert refresher.recheck_of("DEMO") is None  # and no re-check started


def test_the_body_is_capped_at_256_kb_and_a_body_under_the_cap_is_read(board: Settings) -> None:
    client, _ = serve(board)
    first, _key, _rule = first_open(board, "AND f.rule_id = 'R-EST-01'")
    body = json.dumps({"target": {"finding_ids": [first]}, "scope": "finding"})
    before = everything(board)

    padded = body[:-1] + " " * (BODY_CAP + 4096) + "}"  # valid JSON, over the cap
    for path in (PREVIEW, IGNORE):
        response = client.post(path, content=padded, headers={"Content-Type": "application/json"})
        assert response.status_code in (413, 422), response.text
    assert everything(board) == before

    roomy = body[:-1] + " " * 200_000 + "}"  # far beyond the 16 KB of rule settings, well within 256 KB
    response = client.post(IGNORE, content=roomy, headers={"Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    assert response.json()["ignored"]["findings"] == 1


def test_ignore_restore_and_the_ignored_list_are_for_admins_only(board: Settings) -> None:
    client, refresher = serve(board)
    first, _key, _rule = first_open(board, "AND f.rule_id = 'R-EST-01'")
    target = {"target": {"finding_ids": [first]}, "scope": "finding"}
    before = everything(board)
    for level in ("", "?level=team"):
        for path, body in (("/findings/ignore/preview", target), ("/findings/ignore", target),
                           ("/findings/restore", {"ignore_ids": [uuid.uuid4().hex]})):
            response = client.post(f"/api/v1{path}{level}", json=body)
            assert response.status_code == 403, (path, level, response.text)
            assert response.json()["code"] == "people_level_only"
        listed = client.get(f"/api/v1/findings/ignored{level}")
        assert (listed.status_code, listed.json()["code"]) == (403, "people_level_only")

    nameless = TestClient(client.app, headers=headers(viewer=None, name=None))
    for path in (PREVIEW, IGNORE):
        response = nameless.post(path, json=target)
        assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["header", "X-Oversight-Viewer"]
    settle(refresher)
    assert everything(board) == before


def test_who_ignored_is_the_viewers_display_name_never_an_email_and_hidden_below_the_people_level(
    board: Settings,
) -> None:
    client, _ = serve(board)
    emailed = TestClient(client.app, headers=headers(name="jane.doe@example.com"))
    first, _key, rule_id = first_open(board, "AND f.rule_id = 'R-EST-01'")

    body = emailed.post(IGNORE, json={"target": {"finding_ids": [first]}, "scope": "finding"}).json()

    [ignore_id] = body["ignore_ids"]
    [(stored,)] = query(board, "SELECT created_by FROM finding_ignores WHERE ignore_id = ?", (ignore_id,))
    assert stored == "Signed-in user"
    everywhere = json.dumps([client.get(f"/api/v1/findings/{first}?level=people").json(),
                             client.get(IGNORED).json(), all_items(client, [("state", "ignored")])])
    assert "@" not in everywhere
    [team] = all_items(client, [("state", "ignored")])
    assert_ignore(team["ignore"], scope="finding", rule_id=rule_id, reason=None, by=None)
    [people] = all_items(client, [("state", "ignored"), ("level", "people")])
    assert_ignore(people["ignore"], scope="finding", rule_id=rule_id, reason=None, by="Signed-in user")


def test_the_reason_is_kept_as_plain_text_of_at_most_500_characters(board: Settings) -> None:
    client, _ = serve(board)
    rows = query(board, f"SELECT f.finding_id {OPEN} AND f.rule_id = 'R-EST-01' ORDER BY {BY_NUMBER} LIMIT 4")
    ids = [row[0] for row in rows]

    def reason_of(response: dict[str, Any]) -> str | None:
        [ignore_id] = response["ignore_ids"]
        [(stored,)] = query(board, "SELECT reason FROM finding_ignores WHERE ignore_id = ?", (ignore_id,))
        return stored

    messy = reason_of(ignore(client, {"finding_ids": [ids[0]]}, reason="  Closed\x00 long\x07 ago\r\n\tnot\x1b worth it  "))
    assert messy is not None and not any(unicodedata.category(char) == "Cc" for char in messy)
    assert messy == messy.strip() and messy.split() == ["Closed", "long", "ago", "not", "worth", "it"]
    assert reason_of(ignore(client, {"finding_ids": [ids[1]]}, reason="   ")) is None
    assert reason_of(ignore(client, {"finding_ids": [ids[2]]}, reason=None)) is None
    longest = "📈" * 500  # characters as Python counts them, an emoji one
    assert reason_of(ignore(client, {"finding_ids": [ids[3]]}, reason=longest)) == longest


@pytest.mark.parametrize("reason", ["As jane.doe@example.com asked", "For 557058:f58131cb-9b2c-4f1a-8e1d-0c2b4a6e8f10"])
def test_a_reason_never_keeps_an_email_address_or_an_account_id(board: Settings, reason: str) -> None:
    client, _ = serve(board)
    first, _key, _rule = first_open(board, "AND f.rule_id = 'R-EST-01'")
    response = client.post(IGNORE, json={"target": {"finding_ids": [first]}, "scope": "finding", "reason": reason})
    assert response.status_code in (200, 422), response.text
    stored = query(board, "SELECT reason FROM finding_ignores")
    assert all(row[0] is None or ("@" not in row[0] and "557058:" not in row[0]) for row in stored)
    shown = json.dumps([client.get(IGNORED).json(), client.get(f"/api/v1/findings/{first}?level=people").json()])
    assert "jane.doe@example.com" not in shown and "557058:" not in shown


# --- reading what is ignored ----------------------------------------------------------------------------------


def test_ignored_is_its_own_state_neither_open_nor_resolved(board: Settings) -> None:
    client, _ = serve(board)
    key = ticket_with_rules(board, "done")
    on_ticket = open_on(board, key)
    resolved = {row[0] for row in query(board, "SELECT finding_id FROM findings WHERE resolved_at IS NOT NULL")}
    open_before = client.get("/api/v1/findings").json()["total"]
    ignore(client, {"issue_keys": [key]}, scope="ticket", reason="Old work")

    shown = {state: {item["finding_id"] for item in all_items(client, [("state", state)])}
             for state in ("open", "resolved", "ignored", "all")}

    assert shown["ignored"] == set(on_ticket)
    assert shown["resolved"] == resolved and not shown["resolved"] & shown["ignored"]
    assert len(shown["open"]) == open_before - len(on_ticket) and not shown["open"] & shown["ignored"]
    assert shown["all"] == shown["open"] | shown["resolved"] | shown["ignored"]
    body = client.get("/api/v1/findings").json()
    assert [(entry["value"], entry["label"]) for entry in body["facets"]["state"]] == [
        ("open", "Open"), ("resolved", "Resolved"), ("ignored", "Ignored")]
    assert facet(body, "state") == {"open": len(shown["open"]), "resolved": len(resolved), "ignored": len(on_ticket)}
    for item in all_items(client, [("state", "all")]):
        assert set(item) == FINDING_KEYS
        if item["finding_id"] in shown["ignored"]:
            assert (item["state"], item["resolved_why"]) == ("ignored", "ignored") and item["resolved_at"]
            assert_ignore(item["ignore"], scope="ticket", rule_id=None, reason="Old work", by=None)
        else:
            assert item["ignore"] is None and item["state"] in ("open", "resolved") and item["resolved_why"] != "ignored"
    timeline = client.get(f"/api/v1/issues/{key}/timeline").json()
    assert {finding["finding_id"]: finding["state"] for finding in timeline["findings"]
            if finding["finding_id"] in on_ticket} == {finding_id: "ignored" for finding_id in on_ticket}


def test_the_ticket_filter_reads_each_tickets_status_category_now_and_never_matches_a_sprint(board: Settings) -> None:
    client, _ = serve(board)
    categories = dict(query(board, "SELECT issue_key, status_category FROM issues"))
    for ticket, expected in (("finished", {"done"}), ("open", {"new", "indeterminate"})):
        items = all_items(client, [("ticket", ticket), ("state", "all")])
        assert items and all(item["scope"] == "issue" for item in items)
        assert {categories[item["issue_key"]] for item in items} == expected
        sql = ("SELECT COUNT(*) FROM findings AS f JOIN issues AS i ON i.issue_key = f.issue_key"
               f" WHERE i.status_category IN ({', '.join('?' * len(expected))})")
        assert len(items) == query(board, sql, sorted(expected))[0][0]
    sprints = all_items(client, [("q", "sprint"), ("state", "all")])
    assert any(item["scope"] == "sprint" for item in sprints)
    finished = client.get("/api/v1/findings?ticket=finished").json()
    assert facet(finished, "state")["open"] == sum(facet(finished, "severity").values()) == finished["total"]
    assert sum(facet(finished, "rules").values()) == finished["total"]
    assert client.get("/api/v1/findings?ticket=closed").status_code == 422
    assert client.get("/api/v1/findings?ticket=open&ticket=finished").status_code == 422


def test_the_ignored_list_pages_sorts_filters_and_counts_each_ignores_findings(board: Settings) -> None:
    client, _ = serve(board)
    whole = ticket_with_rules(board, "indeterminate")
    rows = [row for rule_id in ("R-EST-01", "R-FLOW-02") for row in query(
        board, f"SELECT f.finding_id, f.issue_key, f.rule_id {OPEN} AND f.rule_id = ? AND f.issue_key != ?"
               f" ORDER BY {BY_NUMBER} LIMIT 15", (rule_id, whole))]
    ignore(client, {"finding_ids": [row[0] for row in rows]}, reason="Agreed with the team")
    ignore(client, {"issue_keys": [whole]}, scope="ticket")
    pairs = {(row[1], row[2]) for row in rows}

    response = client.get(IGNORED)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"meta", "items", "total", "page", "page_size"}
    assert_meta(body["meta"])
    assert (body["total"], body["page"], body["page_size"], len(body["items"])) == (len(pairs) + 1, 1, 25, 25)
    everyone = [item for page in (1, 2) for item in client.get(f"{IGNORED}&page={page}").json()["items"]]
    assert len(everyone) == len({item["ignore_id"] for item in everyone}) == len(pairs) + 1
    issues = {row[0]: row[1:] for row in query(board, "SELECT issue_key, summary, issue_type, status, status_category"
                                                      " FROM issues")}
    for item in everyone:
        assert set(item) == LISTED_KEYS
        assert (item["issue_summary"], item["issue_type"], item["status"], item["status_category"]) == issues[
            item["issue_key"]]
        assert (item["by"], item["at"]) == (ADMIN_NAME, to_iso(SAVED))
        if item["scope"] == "ticket":
            assert (item["issue_key"], item["rule_id"], item["rule_title"], item["reason"]) == (whole, None, None, None)
            assert item["findings"] == query(board, "SELECT COUNT(*) FROM findings WHERE issue_key = ?"
                                                    " AND resolved_why = 'ignored'", (whole,))[0][0] > 1
        else:
            assert (item["issue_key"], item["rule_id"]) in pairs and item["findings"] == 1
            assert (item["rule_title"], item["reason"]) == (CATALOGUE.by_id(item["rule_id"]).title,
                                                            "Agreed with the team")

    by_key = client.get(f"{IGNORED}&sort=issue_key&order=asc&page_size=100").json()["items"]
    numbers = [int(item["issue_key"].rsplit("-", 1)[1]) for item in by_key]
    assert numbers == sorted(numbers)
    by_rule = client.get(f"{IGNORED}&sort=rule_id&order=desc&page_size=100").json()["items"]
    named = [item["rule_id"] for item in by_rule if item["rule_id"] is not None]
    assert named == sorted(named, reverse=True)
    flows = client.get(f"{IGNORED}&rule=R-FLOW-02&page_size=100").json()["items"]
    assert flows and {item["rule_id"] for item in flows if item["scope"] == "finding"} == {"R-FLOW-02"}
    assert len([item for item in flows if item["scope"] == "finding"]) == sum(rule == "R-FLOW-02" for _, rule in pairs)
    one_key = rows[0][1]
    found = client.get(f"{IGNORED}&q={one_key}").json()["items"]
    assert found and {item["issue_key"] for item in found} == {one_key}
    for bad in ("page_size=0", "page_size=101", "sort=severity", "order=up", "colour=red", "page=0"):
        assert client.get(f"{IGNORED}&{bad}").status_code == 422, bad


def test_the_shared_helper_answers_which_ignore_covers_a_ticket_and_its_rules(board: Settings) -> None:
    from oversight.rules.ignores import active_ignores

    client, refresher = serve(board)
    finding, single_key, rule_id = first_open(board, "AND f.rule_id = 'R-EST-01' AND i.status_category = 'done'")
    whole = ticket_with_rules(board, "indeterminate", besides=[single_key])
    first = ignore(client, {"finding_ids": [finding]}, reason="Old")
    ignore(client, {"issue_keys": [whole]}, scope="ticket")

    conn = open_readonly(board.db_file)
    try:
        held = active_ignores(conn, "DEMO")
    finally:
        conn.close()

    found = held.covers(single_key, [rule_id, "R-FLOW-02"])
    assert found is not None and (found.scope, found.rule_id) == ("finding", rule_id)
    assert held.covers(single_key, ["R-FLOW-02"]) is None and held.ticket(single_key) is None
    whole_ignore = held.ticket(whole)
    assert whole_ignore is not None and (whole_ignore.scope, whole_ignore.rule_id) == ("ticket", None)
    assert held.covers(whole, ["W-AGE-01"]) == whole_ignore
    assert held.covers("DEMO-99999", [rule_id]) is None

    restored = client.post(RESTORE, json={"ignore_ids": first["ignore_ids"]})
    assert restored.status_code == 200, restored.text
    settle(refresher)
    conn = open_readonly(board.db_file)
    try:
        assert active_ignores(conn, "DEMO").covers(single_key, [rule_id]) is None
    finally:
        conn.close()


# --- every count -----------------------------------------------------------------------------------------------


def ask_rule_status(client: TestClient) -> dict[str, int]:
    response = client.post("/api/v1/ask", json={"question": "Which rules can't be measured in Jira today?"})
    assert response.status_code == 200, response.text
    rows = [row for fact in response.json()["facts"] if fact["tool"] == "oversight_rule_status" for row in fact["rows"]]
    assert rows
    return {row["rule_id"]: row["open_findings"] for row in rows}


def mcp_call(server: FastMCP, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def go() -> Any:
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool(name, arguments)

    result = anyio.run(go)
    assert result.isError is False and isinstance(result.content[0], TextContent)
    found: dict[str, Any] = json.loads(result.content[0].text)
    return found


def counts(client: TestClient, settings: Settings) -> dict[str, Any]:
    """Every open, new and resolved figure the dashboard, the digest, the insights, Ask and MCP show."""
    overview = client.get("/api/v1/overview").json()
    [row] = client.get("/api/v1/portfolio").json()["boards"]
    attention = client.get("/api/v1/portfolio").json()["attention"]
    findings = client.get("/api/v1/findings").json()
    process = ProcessConfig.load(settings.process_file)
    server = build_server(settings, process, CATALOGUE, db_path=settings.db_file)
    with Store.open(settings.db_file) as store:
        digest = build_digest(store, process, CATALOGUE, project="DEMO", now=SAVED)
        candidates = build_candidates(store, process, CATALOGUE, project="DEMO", now=SAVED)
    people = client.get("/api/v1/people").json()["people"]
    cards = {}
    for held in people:
        if held["person"] is not None:
            card = client.get(f"/api/v1/people/{held['person']['id']}/card?level=people&days=90")
            assert card.status_code == 200, card.text
            cards[held["person"]["id"]] = card.json()["findings"]
    return {
        "overview": {name: total["current"] for name, total in overview["totals"].items()},
        "by_rule": {rule["rule_id"]: rule["open"] for rule in overview["by_rule"]},
        "hot_spots": {spot["epic_key"]: (spot["open_breaches"], spot["open_warnings"]) for spot in overview["hot_spots"]},
        "portfolio": {name: row[name] for name in ("open_breaches", "open_warnings", "new_findings_7d",
                                                   "resolved_findings_7d")} | {"trend": row["open_findings_trend"][-1]},
        "attention": {(item["issue_key"], item["rule_id"]) for item in attention},
        "people": {(person["person"] or {}).get("id", "unassigned"): (person["open_breaches"], person["open_warnings"])
                   for person in people},
        "cards": cards,
        "findings": findings["total"],
        "facets": {name: facet(findings, name) for name in ("rules", "severity", "people", "epics")},
        "trends": {metric: client.get(f"/api/v1/trends?metric={metric}").json()["series"][-1]["value"]
                   for metric in ("open_breaches", "open_warnings")},
        "rules": {rule["rule_id"]: rule["open"] for rule in client.get("/api/v1/rules").json()["rules"]},
        "digest": {
            "hot_spots": {spot.epic_key: (spot.open_breaches, spot.open_warnings) for spot in digest.hot_spots},
            "listed": {item.finding_id for item in digest.new_breaches + digest.new_warnings + digest.resolved},
        },
        "insights": {candidate.id: (candidate.figure("open_breaches"), set(candidate.finding_ids))
                     for candidate in candidates},
        "ask": ask_rule_status(client),
        "mcp": {
            "rule_status": {r["rule_id"]: r["open_findings"]
                            for r in mcp_call(server, "oversight_rule_status", {})["rows"]},
            "resolved": {r["finding_id"] for rule_id in ("R-EST-01", "W-EST-01", "W-AGE-01")
                         for r in mcp_call(server, "oversight_list_findings",
                                           {"rule_id": rule_id, "state": "resolved", "n": 50})["rows"]},
        },
    }


def test_every_open_count_drops_by_exactly_the_ignored_findings_and_none_counts_them_as_new_or_resolved(
    board: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oversight.mcp_server import server as mcp_server

    monkeypatch.setattr(mcp_server, "utcnow", lambda: SAVED)
    client, _ = serve(board)
    with Store.open(board.db_file) as store, store.transaction():  # an earlier digest, so "resolved since" is asked
        store.upsert("digests", {"digest_id": "d" * 32, "project_key": "DEMO", "channel": "file",
                                 "sent_at": to_iso(NOW - timedelta(hours=1)), "finding_ids_json": []})
    before = counts(client, board)

    old = query(board, f"SELECT f.finding_id {OPEN} AND f.rule_id = 'R-EST-01' AND i.status_category = 'done'"
                       f" ORDER BY {BY_NUMBER} LIMIT 3")
    new = query(board, f"SELECT f.finding_id {OPEN} AND f.rule_id = 'W-EST-01' ORDER BY {BY_NUMBER} LIMIT 2")
    stuck, _key, _rule = first_open(board, "AND f.rule_id = 'W-AGE-01'")
    ignore(client, {"finding_ids": [row[0] for row in old + new] + [stuck]})
    ignore(client, {"issue_keys": [ticket_with_rules(board, "indeterminate")]}, scope="ticket")

    after = counts(client, board)
    rows = query(board, """
        SELECT f.finding_id, f.rule_id, f.severity, f.first_seen_at, COALESCE(i.epic_key, p.epic_key), i.assignee_id,
               f.issue_key
        FROM findings AS f JOIN issues AS i ON i.issue_key = f.issue_key LEFT JOIN issues AS p ON p.issue_key = i.parent_key
        WHERE f.resolved_why = 'ignored'
    """)
    ids = {row[0] for row in rows}
    assert len(ids) >= 8
    severity = Counter(row[2] for row in rows)
    by_rule = Counter(row[1] for row in rows)
    by_epic = Counter((row[4], row[2]) for row in rows)
    by_person = Counter((person_id(row[5]) if row[5] else "unassigned", row[2]) for row in rows)
    fresh = sum(row[3] > to_iso(SAVED - timedelta(days=7)) for row in rows)
    assert 0 < fresh < len(rows)  # some were first seen this week, some long before

    o, a = before["overview"], after["overview"]
    assert a["open_breaches"] == o["open_breaches"] - severity["breach"]
    assert a["open_warnings"] == o["open_warnings"] - severity["warning"]
    assert a["new_7d"] == o["new_7d"] - fresh  # never new
    assert a["resolved_7d"] == o["resolved_7d"]  # never fixed
    assert after["by_rule"] == {rule: count - by_rule[rule] for rule, count in before["by_rule"].items()}
    for epic, (breaches, warnings) in after["hot_spots"].items():
        if epic in before["hot_spots"]:
            assert (breaches, warnings) == (before["hot_spots"][epic][0] - by_epic[(epic, "breach")],
                                            before["hot_spots"][epic][1] - by_epic[(epic, "warning")])
    assert after["portfolio"] == {
        "open_breaches": before["portfolio"]["open_breaches"] - severity["breach"],
        "open_warnings": before["portfolio"]["open_warnings"] - severity["warning"],
        "new_findings_7d": before["portfolio"]["new_findings_7d"] - fresh,
        "resolved_findings_7d": before["portfolio"]["resolved_findings_7d"],
        "trend": before["portfolio"]["trend"] - len(rows),
    }
    assert not after["attention"] & {(row[6], row[1]) for row in rows if row[2] == "breach"}
    for who, (breaches, warnings) in before["people"].items():
        assert after["people"][who] == (breaches - by_person[(who, "breach")], warnings - by_person[(who, "warning")])
    for who, found in before["cards"].items():
        assert after["cards"][who] == {"open_breaches": found["open_breaches"] - by_person[(who, "breach")],
                                       "open_warnings": found["open_warnings"] - by_person[(who, "warning")]}
    assert any(by_person[(who, severity)] for who in before["cards"] for severity in ("breach", "warning"))
    assert after["findings"] == before["findings"] - len(rows)
    assert after["facets"]["rules"] == {rule: n - by_rule[rule] for rule, n in before["facets"]["rules"].items()}
    assert after["facets"]["severity"] == {s: n - severity[s] for s, n in before["facets"]["severity"].items()}
    people_drop = Counter(who for who, _severity in by_person.elements())
    assert after["facets"]["people"] == {who: n - people_drop[who] for who, n in before["facets"]["people"].items()}
    epic_drop = Counter(epic or "none" for epic, _severity in by_epic.elements())
    assert after["facets"]["epics"] == {epic: n - epic_drop[epic] for epic, n in before["facets"]["epics"].items()}
    assert after["trends"] == {"open_breaches": before["trends"]["open_breaches"] - severity["breach"],
                               "open_warnings": before["trends"]["open_warnings"] - severity["warning"]}
    assert after["rules"] == {rule: n - by_rule[rule] for rule, n in before["rules"].items()}
    for epic, (breaches, warnings) in after["digest"]["hot_spots"].items():
        assert (breaches, warnings) == (before["digest"]["hot_spots"][epic][0] - by_epic[(epic, "breach")],
                                        before["digest"]["hot_spots"][epic][1] - by_epic[(epic, "warning")])
    assert not after["digest"]["listed"] & ids
    for candidate, (figure, found) in after["insights"].items():
        assert not found & ids, candidate
        if candidate.startswith("breach_hot_spot:") and candidate in before["insights"]:
            epic = candidate.split(":", 1)[1]
            assert figure == before["insights"][candidate][0] - by_epic[(epic, "breach")]
    assert after["ask"] == {rule: n - by_rule[rule] for rule, n in before["ask"].items()}
    assert after["mcp"]["rule_status"] == {rule: n - by_rule[rule] for rule, n in before["mcp"]["rule_status"].items()}
    assert not after["mcp"]["resolved"] & ids


def test_ask_and_mcp_say_a_ticket_s_ignored_findings_are_ignored_never_resolved(board: Settings) -> None:
    from oversight.ask.catalog import CatalogContext, run_query

    client, _ = serve(board)
    [(key,)] = query(board, f"SELECT f.issue_key {OPEN} AND i.status_category = 'done' AND f.issue_key NOT IN"
                            f" (SELECT issue_key FROM findings WHERE resolved_at IS NOT NULL) GROUP BY f.issue_key"
                            f" HAVING COUNT(DISTINCT f.rule_id) > 1 ORDER BY {BY_NUMBER} LIMIT 1")
    ignore(client, {"issue_keys": [key]}, scope="ticket")
    conn = open_readonly(board.db_file)
    try:
        context = CatalogContext(process=ProcessConfig.load(board.process_file), rules=CATALOGUE, project="DEMO",
                                 now=SAVED)
        note = run_query(conn, "oversight_issue_timeline", {"issue_key": key}, context, ref="F1").to_dict()["note"]
    finally:
        conn.close()
    assert "ignored" in note and "resolved" not in note and "(open)" not in note


def scorecard(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/people/scorecard?level=people&days=180")
    assert response.status_code == 200, response.text
    board: dict[str, Any] = response.json()["board"]
    return board


def test_an_ignore_leaves_its_ticket_out_of_the_measures_its_rules_judge_and_never_changes_a_count(
    board: Settings,
) -> None:
    client, _ = serve(board)
    before = scorecard(client)
    whole = ticket_with_rules(board, "indeterminate")
    stuck, stuck_key, _rule = first_open(board, "AND f.rule_id = 'W-AGE-01' AND i.issue_type != 'Epic'"
                                                " AND f.issue_key != ?", (whole,))
    other, other_key, _rule = first_open(board, "AND f.rule_id = 'W-EST-01' AND i.status_category != 'done'"
                                                " AND i.issue_type != 'Epic' AND f.issue_key NOT IN (?, ?)",
                                         (stuck_key, whole))
    held = len(open_on(board, whole))

    ignore(client, {"finding_ids": [stuck, other]})
    ignore(client, {"issue_keys": [whole]}, scope="ticket")
    after = scorecard(client)

    stuck_before, stuck_after = before["values"]["stuck_now"], after["values"]["stuck_now"]
    whole_stuck = any(rule == "W-AGE-01" for rule in open_on(board, whole).values()) or bool(query(
        board, "SELECT 1 FROM findings WHERE issue_key = ? AND rule_id = 'W-AGE-01' AND resolved_why = 'ignored'",
        (whole,)))
    assert stuck_after["denominator"] == stuck_before["denominator"] - 2  # the stuck ticket and the whole ticket
    assert stuck_after["numerator"] == stuck_before["numerator"] - 1 - whole_stuck
    ratio_before, ratio_after = before["values"]["open_findings_per_10"], after["values"]["open_findings_per_10"]
    assert ratio_after["numerator"] == ratio_before["numerator"] - 2 - held
    assert ratio_after["denominator"] == ratio_before["denominator"] - 1  # only the whole ticket leaves the count
    for count in ("open_tickets", "in_progress", "done_in_period"):
        assert after["values"][count] == before["values"][count]
    assert after["tickets"] == before["tickets"]


# --- later rules runs and restore ----------------------------------------------------------------------------------


def test_later_rules_runs_keep_an_ignore_and_record_new_findings_on_it_already_ignored(board: Settings) -> None:
    client, _ = serve(board)
    reopened, reopened_key, _rule = first_open(board, "AND f.rule_id = 'R-FLOW-03'")
    hygiene, hygiene_key, _rule = first_open(board, "AND f.rule_id = 'R-HYG-01' AND f.issue_key != ?", (reopened_key,))
    whole = query(board, f"SELECT f.issue_key {OPEN} AND i.status_category = 'indeterminate' AND i.assignee_id IS NOT NULL"
                         f" AND f.issue_key NOT IN (SELECT issue_key FROM findings WHERE rule_id IN ('R-HYG-01',"
                         f" 'R-FLOW-03')) ORDER BY {BY_NUMBER} LIMIT 1")[0][0]
    ignore(client, {"finding_ids": [reopened, hygiene]})
    ignore(client, {"issue_keys": [whole]}, scope="ticket")
    held = ignored_ids(board)
    pairs = {(reopened_key, "R-FLOW-03"), (hygiene_key, "R-HYG-01")}

    def open_on_ignored() -> list[tuple[Any, ...]]:
        rows = query(board, "SELECT finding_id, issue_key, rule_id FROM findings WHERE resolved_at IS NULL")
        return [row for row in rows if (row[1], row[2]) in pairs or row[1] == whole]

    run_the_rules(board, SAVED + timedelta(hours=1))
    assert query(board, "SELECT finding_id, resolved_at, resolved_why FROM findings WHERE finding_id IN"
                        f" ({', '.join('?' * len(held))}) ORDER BY 1", sorted(held)) == [
        (finding_id, to_iso(SAVED), "ignored") for finding_id in sorted(held)]

    # The whole ticket loses its assignee: R-HYG-01 starts on it, already ignored.
    write(board, "UPDATE issues SET assignee_id = NULL WHERE issue_key = ?", (whole,))
    run_the_rules(board, SAVED + timedelta(hours=2))
    fresh = query(board, "SELECT resolved_at, resolved_why FROM findings WHERE issue_key = ? AND rule_id = 'R-HYG-01'",
                  (whole,))
    assert fresh == [(to_iso(SAVED + timedelta(hours=2)), "ignored")]
    assert open_on_ignored() == []

    # R-HYG-01 clears on its ticket (someone is assigned) and fires again (nobody is): the same finding, still ignored.
    write(board, "UPDATE issues SET assignee_id = 'acc-01' WHERE issue_key = ?", (hygiene_key,))
    run_the_rules(board, SAVED + timedelta(hours=3))
    write(board, "UPDATE issues SET assignee_id = NULL WHERE issue_key = ?", (hygiene_key,))
    run_the_rules(board, SAVED + timedelta(hours=4))
    [(why, resolved)] = query(board, "SELECT resolved_why, resolved_at FROM findings WHERE finding_id = ?", (hygiene,))
    assert why == "ignored" and resolved is not None
    assert open_on_ignored() == []

    # A severity change gives R-FLOW-03 another rule version, so other finding ids: the ignore still holds, the
    # finding of the version that now stands is the ignored one, and none of the pair's findings is ever open.
    with Store.open(board.db_file) as store:
        save_board_settings(store, CATALOGUE, "DEMO", revision=0, changes=[Change("R-FLOW-03", severity="breach")],
                            now=SAVED + timedelta(hours=5), viewer_hash=ADMIN, viewer_name=ADMIN_NAME)
    run_the_rules(board, SAVED + timedelta(hours=5))
    versions = dict(query(board, "SELECT rule_version, resolved_why FROM findings WHERE issue_key = ?"
                                 " AND rule_id = 'R-FLOW-03'", (reopened_key,)))
    assert versions.get("0.1.0+breach") == "ignored"
    assert set(versions.values()) <= {"ignored", "superseded"}
    assert open_on_ignored() == []

    # So does a new catalogue version.
    bumped = CATALOGUE.model_copy(update={"version": "0.2.0"})
    run_the_rules(board, SAVED + timedelta(hours=6), bumped)
    assert query(board, "SELECT COUNT(*) FROM findings WHERE rule_version LIKE '0.2.0%' AND resolved_at IS NULL") != [(0,)]
    assert open_on_ignored() == []
    newest = query(board, "SELECT resolved_why FROM findings WHERE rule_version LIKE '0.2.0%' AND (issue_key = ?"
                          " OR (issue_key = ? AND rule_id = 'R-HYG-01') OR (issue_key = ? AND rule_id = 'R-FLOW-03'))",
                   (whole, hygiene_key, reopened_key))
    assert newest and {why for (why,) in newest} == {"ignored"}
    older = query(board, "SELECT resolved_why FROM findings WHERE rule_version NOT LIKE '0.2.0%' AND (issue_key = ?"
                         " OR (issue_key = ? AND rule_id = 'R-HYG-01') OR (issue_key = ? AND rule_id = 'R-FLOW-03'))",
                  (whole, hygiene_key, reopened_key))
    assert {why for (why,) in older} <= {"ignored", "superseded"}
    assert client.get(f"/api/v1/findings?q={whole}").json()["total"] == 0
    listed = client.get(IGNORED).json()["items"]
    assert sorted((item["issue_key"], item["rule_id"]) for item in listed) == sorted(
        [(whole, None), (hygiene_key, "R-HYG-01"), (reopened_key, "R-FLOW-03")])
    assert {item["issue_key"]: item["findings"] for item in listed if item["scope"] == "finding"} == {
        hygiene_key: 1, reopened_key: 1}  # one finding each: the version that stands, never duplicates


def test_a_restore_rechecks_the_board_and_reopens_exactly_what_the_rules_still_hold_as_the_same_findings(
    board: Settings,
) -> None:
    clock = Clock(SAVED)
    steps = Steps(board, clock, hold=True)
    client, refresher = serve(board, clock=clock, steps=steps)
    held, held_key, _rule = first_open(board, "AND f.rule_id = 'R-EST-01' AND i.status_category = 'done'")
    gone, gone_key, _rule = first_open(board, "AND f.rule_id = 'R-HYG-01'")
    whole = ticket_with_rules(board, "indeterminate", besides=[held_key, gone_key])
    keep = set(open_on(board, whole)) | {held}
    first_seen = dict(query(board, "SELECT finding_id, first_seen_at FROM findings"))
    made = [ignore(client, {"finding_ids": [held, gone]}), ignore(client, {"issue_keys": [whole]}, scope="ticket")]
    ignore_ids = [ignore_id for body in made for ignore_id in body["ignore_ids"]]
    assert len(ignore_ids) == 3
    write(board, "UPDATE issues SET assignee_id = 'acc-01' WHERE issue_key = ?", (gone_key,))  # R-HYG-01 no longer holds
    clock.at = SAVED + timedelta(minutes=10)

    response = client.post(RESTORE, json={"ignore_ids": ignore_ids})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"meta", "restored", "recheck"}
    assert_meta(body["meta"])
    assert body["restored"] == 3
    assert isinstance(body["recheck"], dict) and "state" in body["recheck"]
    assert client.get(IGNORED).json()["total"] == 0
    assert query(board, "SELECT ignore_id, restored_by, restored_at FROM finding_ignores ORDER BY 1") == [
        (ignore_id, ADMIN_NAME, to_iso(clock())) for ignore_id in sorted(ignore_ids)]  # kept, as restored
    waiting = client.get(f"/api/v1/findings/{held}")
    assert waiting.status_code == 200 and set(waiting.json()["finding"]) == FINDING_KEYS
    assert waiting.json()["finding"]["state"] != "open"  # the re-check reopens it, not the restore itself

    steps.release.set()
    settle(refresher)
    assert steps.calls == [("rules", "--project", "DEMO")]  # the board's re-check, and no sync
    found = {row[0]: row[1:] for row in query(board, "SELECT finding_id, resolved_at, resolved_why, first_seen_at"
                                                     " FROM findings")}
    for finding_id in keep:
        assert found[finding_id] == (None, None, first_seen[finding_id]), finding_id
    assert found[gone][0] is not None  # its rule no longer holds: it stays closed
    for finding_id in keep:
        shown = client.get(f"/api/v1/findings/{finding_id}").json()["finding"]
        assert (shown["state"], shown["ignore"]) == ("open", None)
    run_the_rules(board, clock() + timedelta(hours=1))
    assert {row[0] for row in query(board, "SELECT finding_id FROM findings WHERE resolved_at IS NULL")} >= keep


def test_undo_after_ignoring_every_finished_ticket_brings_every_finding_back(board: Settings) -> None:
    clock = Clock(SAVED)
    client, refresher = serve(board, clock=clock)
    before = dict(query(board, "SELECT finding_id, first_seen_at FROM findings WHERE resolved_at IS NULL"))
    body = ignore(client, {"query": {"ticket": "finished"}})
    clock.at = SAVED + timedelta(minutes=1)

    response = client.post(RESTORE, json={"ignore_ids": body["ignore_ids"]})

    assert response.status_code == 200, response.text
    assert response.json()["restored"] == len(body["ignore_ids"])
    settle(refresher)
    after = dict(query(board, "SELECT finding_id, first_seen_at FROM findings WHERE resolved_at IS NULL"))
    assert {finding_id: after.get(finding_id) for finding_id in before} == before
    assert client.get("/api/v1/findings?state=ignored").json()["total"] == 0


def test_two_ignores_at_once_make_one_ignore_per_ticket_and_rule(board: Settings) -> None:
    client, _ = serve(board)
    whole = ticket_with_rules(board, "indeterminate")
    rows = query(board, f"SELECT f.finding_id {OPEN} AND f.rule_id = 'R-FLOW-02' AND f.issue_key != ?"
                        f" ORDER BY {BY_NUMBER} LIMIT 20", (whole,))
    same = [row[0] for row in rows]
    start = threading.Barrier(3)
    answers: list[Any] = [None, None, None]
    bodies = [{"target": {"finding_ids": same}, "scope": "finding"}, {"target": {"finding_ids": same}, "scope": "finding"},
              {"target": {"issue_keys": [whole]}, "scope": "ticket"}]

    def post(index: int) -> None:
        own = TestClient(client.app, headers=headers())
        start.wait(WAIT_S)
        answers[index] = own.post(IGNORE, json=bodies[index])

    threads = [threading.Thread(target=post, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT_S)

    assert [answer.status_code for answer in answers] == [200, 200, 200], [answer.text for answer in answers]
    rows_now = active_rows(board)
    assert len(rows_now) == len({row[1:3] for row in rows_now}) == len(same) + 1
    assert sum(answer.json()["ignored"]["findings"] for answer in answers[:2]) == len(same)  # each finding once
    assert ignored_ids(board) == set(same) | set(query_ids(board, whole))
    assert query(board, "SELECT DISTINCT resolved_at FROM findings WHERE resolved_why = 'ignored'") == [(to_iso(SAVED),)]


def query_ids(settings: Settings, issue_key: str) -> list[str]:
    return [row[0] for row in query(settings, "SELECT finding_id FROM findings WHERE issue_key = ?"
                                              " AND resolved_why = 'ignored'", (issue_key,))]


@pytest.mark.slow
def test_ten_thousand_findings_are_ignored_in_under_three_seconds(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    process, fields = ProcessConfig.load(settings.process_file), FieldsConfig.load(settings.fields_file)
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=NOW, seed=20261104, issues=200)
        run_derive(store, process, projects=["DEMO"], now=lambda: NOW)
        run_rules(store, process, CATALOGUE, fields, projects=["DEMO"], now=lambda: NOW)
        at = to_iso(NOW)
        with store.transaction():
            store.conn.executemany(
                "INSERT INTO issues (issue_key, issue_id, project_key, issue_type, summary, status, status_category,"
                " created_at) VALUES (?, ?, 'DEMO', 'Task', ?, 'Done', 'done', ?)",
                [(f"DEMO-{n}", str(900_000 + n), f"Synthetic ticket {n}", at) for n in range(5_001, 15_001)],
            )
            store.conn.executemany(
                "INSERT INTO findings (finding_id, rule_id, rule_version, issue_key, project_key, kind, severity,"
                " evidence_key, message, evidence_json, first_seen_at, last_seen_at, run_id)"
                " VALUES (?, 'R-EST-01', '0.1.0', ?, 'DEMO', 'rule', 'breach', 'no-estimate',"
                " 'Started with no estimate', '{}', ?, ?, 'synthetic')",
                [(uuid.uuid4().hex, f"DEMO-{n}", at, at) for n in range(5_001, 15_001)],
            )
    client, _ = serve(settings)
    open_now = client.get("/api/v1/findings?ticket=finished").json()["total"]
    assert open_now >= 10_000

    started = time.perf_counter()
    body = ignore(client, {"query": {"ticket": "finished"}})
    elapsed = time.perf_counter() - started

    assert body["ignored"]["findings"] == open_now
    assert client.get("/api/v1/findings?ticket=finished").json()["total"] == 0
    assert elapsed < 3.0, f"{open_now:,} findings took {elapsed:.2f} s"


# --- the snapshot ------------------------------------------------------------------------------------------------


def to_version_11(path: Path) -> None:
    """The snapshot as schema version 11 left it: every table version 12 added dropped."""
    conn = sqlite3.connect(path)
    try:
        added = [name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                                                  " AND name NOT LIKE 'sqlite_%'") if name not in VERSION_11_TABLES]
        conn.executescript("".join(f"DROP TABLE {name};" for name in added) + "PRAGMA user_version = 11;")
    finally:
        conn.close()


def schema_of(path: Path, table: str) -> tuple[set[str], list[str], set[str]]:
    """The table's columns, its primary key and the names of every table in the file."""
    conn = sqlite3.connect(path)
    try:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return {row[1] for row in info}, [row[1] for row in info if row[5]], tables
    finally:
        conn.close()


def an_ignore(n: int, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ignore_id": f"{n:032x}", "project_key": "AIL", "issue_key": f"AIL-{n}", "rule_id": "R-EST-01",
        "scope": "finding", "reason": None, "created_by": "Local (sign-in off)",
        "created_at": "2026-11-26T09:00:00.000Z", "restored_by": None, "restored_at": None,
    }
    row.update(extra)
    return row


INSERT_IGNORE = (
    "INSERT INTO finding_ignores (ignore_id, project_key, issue_key, rule_id, scope, reason, created_by, created_at,"
    " restored_by, restored_at) VALUES (:ignore_id, :project_key, :issue_key, :rule_id, :scope, :reason, :created_by,"
    " :created_at, :restored_by, :restored_at)"
)


def test_a_version_11_snapshot_gains_finding_ignores_and_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "v11.sqlite"
    with Store.open(path) as store:
        store.upsert("issues", {"issue_key": "AIL-1", "issue_id": "1", "project_key": "AIL", "status": "Done"})
        store.upsert("findings", {
            "finding_id": "f" * 32, "rule_id": "R-EST-01", "rule_version": "0.1.0", "issue_key": "AIL-1",
            "project_key": "AIL", "kind": "rule", "severity": "breach", "evidence_key": "no-estimate",
            "first_seen_at": "2026-11-04T09:00:00.000Z", "last_seen_at": "2026-11-04T09:00:00.000Z"})
    to_version_11(path)
    assert "finding_ignores" not in schema_of(path, "findings")[2]

    with Store.open(path) as store:  # any command's first open migrates
        assert SCHEMA_VERSION == 12
        assert store.query_one("PRAGMA user_version")[0] == 12
        assert (store.count("issues"), store.count("findings"), store.count("finding_ignores")) == (1, 1, 0)
        with store.transaction():
            store.conn.execute(INSERT_IGNORE, an_ignore(1))
    columns, keys, _tables = schema_of(path, "finding_ignores")
    assert columns == FINDING_IGNORE_COLUMNS and keys == ["ignore_id"]

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 11")  # as if it had to run again: nothing changes, nothing is lost
    conn.close()
    with Store.open(path) as store:
        assert store.query_one("PRAGMA user_version")[0] == 12
        assert (store.count("findings"), store.count("finding_ignores")) == (1, 1)


def test_one_active_ignore_per_ticket_and_rule_and_any_number_restored(tmp_path: Path) -> None:
    path = tmp_path / "v12.sqlite"
    Store.open(path).close()
    restored = {"restored_by": "Local (sign-in off)", "restored_at": "2026-11-26T10:00:00.000Z"}
    conn = sqlite3.connect(path)
    try:
        conn.execute(INSERT_IGNORE, an_ignore(1, issue_key="AIL-1", **restored))
        conn.execute(INSERT_IGNORE, an_ignore(2, issue_key="AIL-1", **restored))
        conn.execute(INSERT_IGNORE, an_ignore(3, issue_key="AIL-1"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(INSERT_IGNORE, an_ignore(4, issue_key="AIL-1"))
        conn.execute(INSERT_IGNORE, an_ignore(5, issue_key="AIL-1", rule_id=None, scope="ticket"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(INSERT_IGNORE, an_ignore(6, issue_key="AIL-1", rule_id=None, scope="ticket"))
        conn.execute(INSERT_IGNORE, an_ignore(7, issue_key="AIL-1", project_key="MOB"))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM finding_ignores").fetchone()[0] == 5
    finally:
        conn.close()


def test_two_processes_opening_a_version_11_snapshot_at_once_both_end_at_version_12(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v11.sqlite"
    Store.open(path).close()
    to_version_11(path)
    gather = Store._new_columns
    raced: list[int] = []

    def racing(self: Store, version: int) -> str:
        statements = gather(self, version)
        if version == 11 and not raced:
            raced.append(version)
            with Store.open(path) as other, other.transaction():  # the other process migrates first, and ignores one
                other.conn.execute(INSERT_IGNORE, an_ignore(1))
        return statements

    monkeypatch.setattr(Store, "_new_columns", racing)
    with Store.open(path) as late:
        assert late.query_one("PRAGMA user_version")[0] == 12
        assert late.count("finding_ignores") == 1
    assert raced == [11]
