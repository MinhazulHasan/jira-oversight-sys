"""The proof behind every scorecard figure (GET /people/scorecard/tickets), checked from the outside."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import yaml
from fastapi.testclient import TestClient

from oversight.api.app import create_app
from oversight.api.refresh import Refresher
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.jira.mapping import map_issue
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.rules.settings import Change, load_board_settings, save_board_settings
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES

T = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)  # the synthetic boards' last sync: every period ends here
DAY, HOUR, MINUTE, MS = timedelta(days=1), timedelta(hours=1), timedelta(minutes=1), timedelta(milliseconds=1)
S90 = T - 90 * DAY
DEMO_NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
KEY = "proof-key"
ADMIN = "0123456789abcdef0123456789abcdef"
ADMIN_NAME = "Morgan Reyes"
HEADERS = {"X-Service-Key": KEY, "X-Oversight-Viewer": ADMIN, "X-Oversight-Viewer-Name": quote(ADMIN_NAME)}
PROOF = "/api/v1/people/scorecard/tickets"
SCORECARD = "/api/v1/people/scorecard"
RULES = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")
PROGRAMMER = yaml.safe_load((CONFIG_FIXTURES / "fields.yaml").read_text(encoding="utf-8"))["fields"]["programmer"]["id"]

MEASURES = ("open_tickets", "in_progress", "done_in_period", "estimated_before_start", "time_logged", "within_estimate",
            "stuck_now", "reopened", "skipped_stage", "code_review", "open_findings_per_10")
EVERY_KEY = (*MEASURES, "tickets")
PERIODS = (30, 90, 180)
TICKETS_MEASURE = {"key": "tickets", "label": "Tickets", "kind": "count", "direction": "neutral", "rule_ids": [],
                   "measurable": True, "reason": None}
BODY_KEYS = {"meta", "period", "measure", "person", "value", "total", "counted", "tickets", "left_out"}
TICKET_KEYS = {"key", "summary", "issue_type", "status", "status_category", "counted", "credit", "at", "detail",
               "ignore", "url"}
DETAIL_KEYS: dict[str, set[str] | None] = {
    "open_tickets": None, "in_progress": None, "done_in_period": None, "tickets": None,
    "estimated_before_start": {"started_at", "estimate_set_at", "grace_hours"},
    "time_logged": {"time_spent_s", "subtask_time_spent_s"},
    "within_estimate": {"logged_s", "estimate_s", "ratio", "limit"},
    "stuck_now": {"status", "since", "hours", "threshold"},
    "reopened": {"times"},
    "skipped_stage": {"stages"},
    "code_review": {"breach"},
    "open_findings_per_10": {"findings", "rule_ids"},
}
DONE_BASED = {"done_in_period", "time_logged", "within_estimate", "reopened", "skipped_stage", "code_review"}
NOW_MEASURES = {"open_tickets", "in_progress", "stuck_now", "open_findings_per_10"}
BASES = {"assignee_now", "field", "assignee_in_development", "assignee_last"}
IGNORE_KEYS = {"scope", "rule_id", "reason", "by", "at"}
ISSUE_RULES = [rule.id for rule in RULES.rules if rule.scope == "issue"]
EFFORT_LIMIT = 1.5  # R-EFF-01's threshold in the test catalogue
GRACE_HOURS = 1  # R-EST-02's grace in the test catalogue

NAMES = {"ann": "Ann Archer", "bob": "Bob Baker", "cy": "Cy Cole", "dot": "Dot Dixon", "eve": "Eve Evans",
         "fay": "Fay Fisher", "gus": "Gus Grant", "hal": "Hal Hughes"}
ACCOUNTS = {who: f"acc-{who}" for who in NAMES}
SECRETS = ["acc-", "accountId", "@example.invalid", "avatar_url", "avatarUrls", "Europe/London"]
CATEGORY = {"To Do": "new", "In Progress": "indeterminate", "Code Review": "indeterminate", "QA": "indeterminate",
            "Done": "done"}
STAGE = {"To Do": "backlog", "In Progress": "development", "Code Review": "code_review", "QA": "testing",
         "Done": "done"}


def pid(who: str) -> str:
    return "p" + hashlib.sha256(ACCOUNTS[who].encode()).hexdigest()[:12]


def person(who: str) -> dict[str, Any]:
    return {"id": pid(who), "name": NAMES[who], "avatar": None}


def ts(at: datetime | None) -> str | None:
    return to_iso(at)


# --- the synthetic boards ---------------------------------------------------------------------


@dataclass(frozen=True)
class Work:
    key: str
    holder: str | None
    status: str
    kind: str = "Task"
    started: datetime | None = None
    done: datetime | None = None
    set_at: datetime | None = None
    spent: int | None = None
    below: int | None = None  # aggregate time: its own and its subtasks'
    ratio: float | None = None
    reopens: int = 0
    skipped: tuple[str, ...] = ()
    testing: bool = False
    since: datetime | None = None  # when it entered its current status (open work)
    findings: tuple[tuple[str, str | None], ...] = ()


PRF = (
    Work("PRF-1", "ann", "Done", started=T - 5 * DAY, done=T - 2 * DAY, set_at=T - 6 * DAY, spent=3600, below=3600,
         ratio=1.2, testing=True),
    Work("PRF-2", "ann", "Done", started=T - 12 * DAY, done=T - 10 * DAY, set_at=T - 12 * DAY + 30 * MINUTE,
         spent=7200, below=7200, ratio=2.0, reopens=2, skipped=("testing",), testing=True,
         findings=(("R-FLOW-01", None),)),
    Work("PRF-3", "ann", "Done", started=T - 41 * DAY, done=T - 40 * DAY, below=1800, findings=(("R-EST-01", None),)),
    Work("PRF-4", "ann", "Done", started=S90 - 2 * DAY, done=S90 - MS, set_at=S90 - 3 * DAY, spent=3600, below=3600,
         ratio=1.0, reopens=1, testing=True),
    Work("PRF-5", "ann", "Done", started=S90 - DAY, done=S90, set_at=S90 - 2 * DAY, spent=3600, below=1800, ratio=1.5,
         skipped=("code_review",), testing=True, findings=(("R-FLOW-01", "cleared"),)),
    Work("PRF-6", "ann", "In Progress", started=T - 3 * DAY, set_at=T - 4 * DAY, since=T - 3 * DAY,
         findings=(("W-AGE-01", None), ("W-EFF-01", None))),
    Work("PRF-7", "ann", "To Do", since=S90 - 20 * DAY, findings=(("W-EST-01", None), ("W-AGE-01", "cleared"))),
    Work("PRF-8", "ann", "Code Review", started=T - DAY, since=T - 12 * HOUR, findings=(("R-EST-01", None),)),
    Work("PRF-9", "ann", "Done", kind="Epic", done=T - 5 * DAY, findings=(("R-EST-01", None),)),
    Work("PRF-10", "ann", "Done", started=T - DAY, done=T + MS, set_at=T - 2 * DAY, spent=3600, below=3600, ratio=1.0),
    Work("PRF-11", "dot", "In Progress", kind="Epic", started=T - 9 * DAY, since=T - 9 * DAY,
         findings=(("W-AGE-01", None),)),
    Work("PRF-12", "ann", "Done", started=T - 42 * DAY, done=T - 40 * DAY, set_at=T - 43 * DAY),
    Work("PRF-20", "bob", "Done", started=T - 2 * DAY, done=T - DAY, set_at=T - DAY, spent=3600, below=3600, ratio=1.0,
         testing=True, findings=(("R-FLOW-01", "cleared"), ("R-EST-02", None))),
    Work("PRF-21", "bob", "To Do", since=T - 2 * DAY),
    Work("PRF-30", None, "Done", started=T - 4 * DAY, done=T - 3 * DAY, set_at=T - 5 * DAY, spent=3600, below=3600,
         ratio=0.5),
    Work("PRF-31", None, "To Do", since=T - 25 * DAY, findings=(("W-AGE-01", None),)),
)
BARE = (
    Work("BARE-1", "bob", "Done", started=T - 3 * DAY, done=T - 2 * DAY, set_at=T - 4 * DAY),
    Work("BARE-2", "bob", "In Progress", started=T - DAY, since=T - DAY),
)
STUCK = {
    "PRF-6": {"status": "In Progress", "age_h": 72.0, "threshold_h": 48.5, "pct": 90.0, "history_n": 30},
    "PRF-31": {"status": "To Do", "age_h": 600.0, "threshold_h": 524.8, "pct": 90.0, "history_n": 40},
}


def finding_id(key: str, rule_id: str) -> str:
    return hashlib.sha256(f"{key}/{rule_id}".encode()).hexdigest()[:32]


def add_finding(store: Store, key: str, rule_id: str, board: str, why: str | None,
                evidence: dict[str, Any] | None = None) -> None:
    rule = RULES.by_id(rule_id)
    store.upsert("findings", {
        "finding_id": finding_id(key, rule_id), "rule_id": rule_id, "rule_version": RULES.version, "issue_key": key,
        "project_key": board, "kind": rule.kind, "severity": rule.severity, "evidence_key": f"{key}/{rule_id}",
        "message": rule.title, "evidence_json": evidence or {}, "first_seen_at": to_iso(T - 5 * DAY),
        "last_seen_at": to_iso(T), "resolved_at": None if why is None else to_iso(T - DAY), "resolved_why": why,
        "run_id": "r1",
    })


def add_statuses(store: Store, board: str) -> None:
    for n, (name, category) in enumerate(CATEGORY.items()):
        store.upsert("statuses", {"project_key": board, "status_id": str(10 + n), "name": name, "category": category})
    store.set_cursor(board, last_synced_at=to_iso(T), run_id="r1", account_timezone="Europe/London")


def add_people(store: Store) -> None:
    for who, name in NAMES.items():
        store.upsert("people", {"account_id": ACCOUNTS[who], "display_name": name, "active": 1,
                                "updated_at": to_iso(T)})


def add_work(store: Store, tickets: Sequence[Work], offset: int) -> None:
    for n, ticket in enumerate(tickets, offset):
        board = ticket.key.split("-")[0]
        store.upsert("issues", {
            "issue_key": ticket.key, "issue_id": str(1000 + n), "project_key": board, "issue_type": ticket.kind,
            "summary": f"Work on {ticket.key}", "status": ticket.status, "status_category": CATEGORY[ticket.status],
            "assignee_id": ACCOUNTS[ticket.holder] if ticket.holder else None, "created_at": to_iso(S90 - 60 * DAY),
            "updated_at": to_iso(T - DAY), "time_spent_s": ticket.spent, "aggregate_time_spent_s": ticket.below,
            "story_points": 3.0 if ticket.set_at else None,
        })
        store.upsert("issue_metrics", {
            "issue_key": ticket.key, "started_at": to_iso(ticket.started), "done_at": to_iso(ticket.done),
            "reopen_count": ticket.reopens,
            "skipped_stages": [{"changelog_id": "1", "from": "In Progress", "to": "Done",
                                "skipped": list(ticket.skipped)}] if ticket.skipped else [],
            "estimate_basis": "points" if ticket.set_at else "none", "estimate_set_at": to_iso(ticket.set_at),
            "effort_ratio": ticket.ratio, "computed_at": to_iso(T),
            "status_age_h": None if ticket.since is None else (T - ticket.since).total_seconds() / 3600,
        })
        if ticket.testing:
            store.conn.execute(
                "INSERT INTO status_intervals (issue_key, seq, status, stage, entered_at, left_at, hours)"
                " VALUES (?, 0, 'QA', 'testing', ?, ?, 24.0)",
                (ticket.key, to_iso(T - 60 * DAY), to_iso(T - 59 * DAY)),
            )
        if ticket.since is not None:
            store.conn.execute(
                "INSERT INTO status_intervals (issue_key, seq, status, stage, entered_at, left_at, hours)"
                " VALUES (?, 1, ?, ?, ?, NULL, NULL)",
                (ticket.key, ticket.status, STAGE[ticket.status], to_iso(ticket.since)),
            )
        for rule_id, why in ticket.findings:
            evidence = STUCK.get(ticket.key) if rule_id == "W-AGE-01" else None
            add_finding(store, ticket.key, rule_id, board, why, evidence)


# The credit cases: who each finished ticket counts for when its assignee was cleared on the way.
@dataclass(frozen=True)
class Case:
    key: str
    status: str
    holder: str | None
    programmers: tuple[str, ...]
    done: datetime | None
    stages: tuple[tuple[str, datetime, datetime | None], ...]  # status, entered, left
    assigned: tuple[tuple[datetime, str | None, str | None], ...]  # when, from, to


def flow(done: datetime) -> tuple[tuple[str, datetime, datetime | None], ...]:
    return (("To Do", done - 8 * DAY, done - 6 * DAY), ("In Progress", done - 6 * DAY, done - 4 * DAY),
            ("Code Review", done - 4 * DAY, done - 3 * DAY), ("QA", done - 3 * DAY, done), ("Done", done, None))


D1, D2, D3, D4, D5, D6, D7 = (T - n * DAY for n in range(2, 9))
CRD = (
    Case("CRD-1", "Done", None, ("eve",), D1, flow(D1),
         ((D1 - 5 * DAY, None, "fay"), (D1 - 4 * DAY + MINUTE, "fay", None))),
    Case("CRD-2", "Done", None, ("eve", "hal"), D2, flow(D2), ()),
    Case("CRD-3", "Done", "ann", (), D3, flow(D3), ((D3 - 7 * DAY, None, "ann"),)),
    Case("CRD-4", "Done", None, (), D4, flow(D4),
         ((D4 - 7 * DAY, None, "gus"), (D4 - 5 * DAY, "gus", "fay"), (D4 - 4 * DAY + MINUTE, "fay", None))),
    Case("CRD-5", "Done", None, (), D5, (("To Do", D5 - 5 * DAY, D5), ("Done", D5, None)),
         ((D5 - 4 * DAY, None, "gus"), (D5 + MINUTE, "gus", None))),
    Case("CRD-6", "Done", None, (), D6, flow(D6), ((D6 + DAY, None, "hal"), (D6 + 2 * DAY, "hal", None))),
    Case("CRD-7", "Done", None, (), D7, flow(D7), ()),
    Case("CRD-8", "In Progress", "ann", ("eve",), None,
         (("To Do", T - 5 * DAY, T - 2 * DAY), ("In Progress", T - 2 * DAY, None)),
         ((T - 2 * DAY + HOUR, None, "ann"),)),
)


def jira_user(who: str) -> dict[str, Any]:
    return {"accountId": ACCOUNTS[who], "displayName": NAMES[who], "active": True, "accountType": "atlassian"}


def add_cases(store: Store, fields: FieldsConfig) -> None:
    for n, case in enumerate(CRD):
        issue = {"key": case.key, "id": str(5000 + n), "fields": {
            "project": {"key": "CRD"}, "issuetype": {"name": "Task"}, "summary": f"Credit case {case.key}",
            "status": {"name": case.status, "statusCategory": {"key": CATEGORY[case.status]}},
            "assignee": jira_user(case.holder) if case.holder else None,
            PROGRAMMER: [jira_user(who) for who in case.programmers] or None,
            "created": to_iso(T - 30 * DAY), "updated": to_iso(T - DAY), "resolutiondate": to_iso(case.done),
        }}
        store.upsert("issues", map_issue(issue, fields))
        started = next((entered for status, entered, _left in case.stages if status == "In Progress"), None)
        store.upsert("issue_metrics", {"issue_key": case.key, "started_at": to_iso(started),
                                       "done_at": to_iso(case.done), "reopen_count": 0, "skipped_stages": [],
                                       "estimate_basis": "none", "computed_at": to_iso(T)})
        for seq, (status, entered, left) in enumerate(case.stages):
            store.conn.execute(
                "INSERT INTO status_intervals (issue_key, seq, status, stage, entered_at, left_at, hours)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (case.key, seq, status, STAGE[status], to_iso(entered), to_iso(left),
                 None if left is None else (left - entered).total_seconds() / 3600),
            )
        for m, (at, before, after) in enumerate(case.assigned):
            store.upsert("field_changes", {
                "issue_key": case.key, "changelog_id": str(9000 + 10 * n + m), "field": "assignee",
                "changed_at": to_iso(at), "from_value": ACCOUNTS[before] if before else None,
                "to_value": ACCOUNTS[after] if after else None,
            })


PROCESS = {
    "process_version": "0.1.0-proof",
    "stages": ["backlog", "estimate", "development", "code_review", "dev_signoff", "testing", "test_review",
               "test_signoff", "done"],
    "start_stage": "development",
    "done_stage": "done",
    "projects": {
        board: {
            "name": name,
            "status_to_stage": dict(STAGE) if reviewed else {"To Do": "backlog", "In Progress": "development",
                                                             "Done": "done"},
            "estimate": {"basis": "points", "field": "story_points", "hours_per_point": 8},
            "evidence": {"code_review": {"status": "Code Review"} if reviewed else None, "dev_signoff": None,
                         "testing": {"status": "QA"} if reviewed else None, "test_review": None,
                         "test_signoff": None},
        }
        for board, name, reviewed in (("PRF", "Proof board", True), ("BARE", "Bare board", False),
                                      ("CRD", "Credit board", True), ("BIG", "Big board", True))
    },
}


def write_config(root: Path) -> Path:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "process.yaml").write_text(yaml.safe_dump(PROCESS), encoding="utf-8")
    for name in ("fields.yaml", "rules.yaml"):
        (config / name).write_text((CONFIG_FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    return config


def build(root: Path) -> Path:
    config = write_config(root)
    with Store.open(root / "oversight.db") as store, store.transaction():
        for board in ("PRF", "BARE", "CRD"):
            add_statuses(store, board)
        add_people(store)
        for who, board in (("ann", "PRF"), ("cy", "PRF"), ("bob", "BARE")):
            store.upsert("board_members", {"project_key": board, "account_id": ACCOUNTS[who], "seen_at": to_iso(T)})
        add_work(store, PRF, 0)
        add_work(store, BARE, 100)
        add_cases(store, FieldsConfig.load(config / "fields.yaml"))
        add_finding(store, "sprint:7", "W-SPR-01", "PRF", None)
        add_finding(store, "PRF-999", "R-EST-01", "PRF", None)
        store.upsert("worklogs", {"worklog_id": "1", "issue_key": "PRF-1", "started_at": to_iso(T - 10 * DAY),
                                  "seconds": 3600, "author_id": "app-tempo", "updated_at": to_iso(T - 10 * DAY)})
        store.upsert("worklogs", {"worklog_id": "2", "issue_key": "BARE-1", "started_at": to_iso(T - 100 * DAY),
                                  "seconds": 3600, "author_id": "app-tempo", "updated_at": to_iso(T - 100 * DAY)})
        store.upsert("worklogs", {"worklog_id": "3", "issue_key": "CRD-1", "started_at": to_iso(T - 3 * DAY),
                                  "seconds": 3600, "author_id": "app-tempo", "updated_at": to_iso(T - 3 * DAY)})
    return root


def settings_of(root: Path, projects: str = "PRF,BARE,CRD", **overrides: Any) -> Settings:
    return Settings(_env_file=None, jira_projects=projects, api_service_key=KEY, db_path=root / "oversight.db",
                    config_dir=root / "config", reports_dir=root / "reports", board_discovery="off", **overrides)


def serve(settings: Settings, now: datetime = T + HOUR, headers: dict[str, str] | None = None) -> TestClient:
    # The re-check after a restore runs nothing here: no step of the real command line is ever started.
    refresher = Refresher(runner=lambda _argv: 0, now=lambda: now)
    app = create_app(settings, provider=ScriptedProvider(), now=lambda: now, refresher=refresher)
    return TestClient(app, headers=HEADERS if headers is None else headers)


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("proof"))


@pytest.fixture(scope="module")
def client(root: Path) -> TestClient:
    return serve(settings_of(root))


@pytest.fixture
def fresh(tmp_path: Path) -> Settings:
    """The synthetic boards for this test alone (it writes to them)."""
    return settings_of(build(tmp_path))


# --- reading the proof ------------------------------------------------------------------------


def proof(client: TestClient, measure: str, who: str | None = None, days: int | None = None,
          board: str = "PRF") -> dict[str, Any]:
    params: list[tuple[str, str]] = [("project", board), ("level", "people"), ("measure", measure)]
    if who is not None:
        params.append(("person", who if who.startswith("p") and len(who) == 13 else pid(who)))
    if days is not None:
        params.append(("days", str(days)))
    response = client.get(PROOF, params=params)
    assert response.status_code == 200, (measure, who, days, response.text)
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def scorecard(client: TestClient, who: str | None = None, days: int = 90, board: str = "PRF") -> dict[str, Any]:
    params: list[tuple[str, str]] = [("project", board), ("level", "people"), ("days", str(days))]
    if who is not None:
        params.append(("person", who))
    response = client.get(SCORECARD, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def listed(body: dict[str, Any]) -> list[tuple[str, bool]]:
    return [(ticket["key"], ticket["counted"]) for ticket in body["tickets"]]


def details(body: dict[str, Any]) -> dict[str, Any]:
    return {ticket["key"]: ticket["detail"] for ticket in body["tickets"]}


def number(key: str) -> int:
    return int(key.rsplit("-", 1)[1])


def moment(at: str) -> float:
    return datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()


def in_order(tickets: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The contract's order: counted first, then `at` newest first (null last), then the key by its number."""
    return sorted(tickets, key=lambda ticket: (not ticket["counted"], ticket["at"] is None,
                                               -moment(ticket["at"]) if ticket["at"] else 0.0, number(ticket["key"])))


def cell_of(card: dict[str, Any], measure: str, who: str | None) -> dict[str, Any]:
    """The value the scorecard shows for this measure and row (the "N tickets" line for `tickets`)."""
    if who is None:
        tickets, values = card["board"]["tickets"], card["board"]["values"]
    else:
        row = next(row for row in card["people"] if row["person"]["id"] == who)
        tickets, values = row["tickets"], row["values"]
    if measure == "tickets":
        return {"value": tickets, "numerator": None, "denominator": None, "enough": True, "vs_board": None}
    cell: dict[str, Any] = values[measure]
    return cell


def measure_of(card: dict[str, Any], measure: str) -> dict[str, Any]:
    if measure == "tickets":
        return TICKETS_MEASURE
    found: dict[str, Any] = next(m for m in card["measures"] if m["key"] == measure)
    return found


def whole(value: int | None) -> int:
    return 0 if value is None else value


def hours_from(start: str, end: str) -> float:
    return (moment(end) - moment(start)) / 3600


def check_ticket(ticket: dict[str, Any], measure: str, who: str | None, period: dict[str, Any]) -> None:
    assert set(ticket) == TICKET_KEYS, ticket
    assert ticket["url"] is None
    assert isinstance(ticket["counted"], bool)
    assert ticket["status_category"] in ("new", "indeterminate", "done", None)
    credit = ticket["credit"]
    assert set(credit) == {"person", "basis", "field"}, credit
    assert credit["basis"] in (*BASES, None)
    assert (credit["field"] is not None) == (credit["basis"] == "field"), credit
    assert (credit["person"] is None) == (credit["basis"] is None), credit
    if credit["person"] is not None:
        assert set(credit["person"]) == {"id", "name", "avatar"}
    if who is not None:
        assert credit["person"] is not None and credit["person"]["id"] == who, (measure, ticket)
        if measure in NOW_MEASURES:
            assert credit["basis"] == "assignee_now", (measure, ticket)
    if measure in DONE_BASED:
        assert ticket["at"] is not None and period["from"] <= ticket["at"] <= period["to"], (measure, ticket)
    elif measure == "estimated_before_start":
        assert ticket["at"] is not None and period["from"] <= ticket["at"] <= period["to"], (measure, ticket)
    else:
        assert ticket["at"] is None, (measure, ticket)
    detail, keys = ticket["detail"], DETAIL_KEYS[measure]
    if keys is None:
        assert detail is None, (measure, ticket)
        return
    assert isinstance(detail, dict) and set(detail) == keys, (measure, ticket)
    counted = ticket["counted"]
    if measure == "estimated_before_start":
        assert detail["started_at"] == ticket["at"]
        set_at = detail["estimate_set_at"]
        assert counted == (set_at is not None and hours_from(detail["started_at"], set_at) <= detail["grace_hours"])
    elif measure == "time_logged":
        own, below = detail["time_spent_s"], detail["subtask_time_spent_s"]
        assert isinstance(own, int) and isinstance(below, int) and own >= 0 and below >= 0, detail
        assert counted == (own > 0 or below > 0)
    elif measure == "within_estimate":
        assert isinstance(detail["logged_s"], int) and detail["logged_s"] >= 0
        assert detail["estimate_s"] is None or isinstance(detail["estimate_s"], int)
        assert counted == (detail["ratio"] < detail["limit"])
    elif measure == "stuck_now":
        if counted:
            assert isinstance(detail["threshold"], str) and detail["threshold"], detail
        else:
            assert detail["threshold"] is None, detail
        assert detail["hours"] is None or detail["hours"] >= 0
    elif measure == "reopened":
        assert isinstance(detail["times"], int) and counted == (detail["times"] > 0)
    elif measure == "skipped_stage":
        assert all(isinstance(stage, str) and stage for stage in detail["stages"])
        assert counted == (len(detail["stages"]) > 0)
    elif measure == "code_review":
        assert isinstance(detail["breach"], bool) and counted == (not detail["breach"])
    elif measure == "open_findings_per_10":
        assert isinstance(detail["findings"], int) and detail["findings"] >= 0
        assert counted == (detail["findings"] > 0)
        assert set(detail["rule_ids"]) <= set(ISSUE_RULES) and len(set(detail["rule_ids"])) <= detail["findings"]


def check_proof(body: dict[str, Any], card: dict[str, Any], measure: str, who: str | None) -> None:
    """Everything the contract holds a proof to, against the scorecard it proves."""
    assert set(body) == BODY_KEYS, set(body) ^ BODY_KEYS
    assert body["period"] == card["period"]
    assert body["measure"] == measure_of(card, measure)
    expected_person = None if who is None else next(row["person"] for row in card["people"]
                                                    if row["person"]["id"] == who)
    assert body["person"] == expected_person
    cell = cell_of(card, measure, who)
    # Byte for byte: the very value the scorecard shows in that cell.
    assert json.dumps(body["value"]) == json.dumps(cell), (measure, who, body["value"], cell)
    tickets, left_out, value = body["tickets"], body["left_out"], body["value"]
    assert body["total"] == len(tickets)
    assert body["counted"] == sum(ticket["counted"] for ticket in tickets)
    kind = body["measure"]["kind"]
    if kind == "count":
        assert body["total"] == body["counted"] == whole(value["value"]), (measure, who)
        assert all(ticket["counted"] for ticket in tickets)
    elif kind == "rate":
        assert body["counted"] == whole(value["numerator"]), (measure, who)
        assert body["total"] == whole(value["denominator"]), (measure, who)
    else:
        assert body["total"] == whole(value["denominator"]), (measure, who)
        assert sum(ticket["detail"]["findings"] for ticket in tickets) == whole(value["numerator"]), (measure, who)
        assert body["counted"] == sum(ticket["detail"]["findings"] > 0 for ticket in tickets)
    keys = [ticket["key"] for ticket in (*tickets, *left_out)]
    assert len(keys) == len(set(keys)), Counter(keys).most_common(3)
    assert [ticket["key"] for ticket in tickets] == [ticket["key"] for ticket in in_order(tickets)], (measure, who)
    for ticket in tickets:
        check_ticket(ticket, measure, who, body["period"])
    for ticket in left_out:
        assert set(ticket) == TICKET_KEYS and ticket["url"] is None
        assert isinstance(ticket["ignore"], dict) and set(ticket["ignore"]) == IGNORE_KEYS, ticket
    if measure in ("open_tickets", "in_progress", "done_in_period", "tickets", "time_logged"):
        assert left_out == [], (measure, who)


def entries(settings: Settings) -> list[dict[str, Any]]:
    conn = open_readonly(settings.db_file)
    try:
        rows = [dict(row) for row in conn.execute("SELECT * FROM access_log ORDER BY at, rowid")]
    finally:
        conn.close()
    for row in rows:
        row["subjects"], row["detail"] = json.loads(row.pop("subject_ids_json")), json.loads(row.pop("detail_json"))
    return rows


# --- the synthetic board, figure by figure ----------------------------------------------------

ANN_90: dict[str, list[tuple[str, bool]]] = {
    "tickets": [(f"PRF-{n}", True) for n in (1, 2, 3, 4, 5, 6, 7, 8, 10, 12)],
    "open_tickets": [("PRF-6", True), ("PRF-7", True), ("PRF-8", True)],
    "in_progress": [("PRF-6", True), ("PRF-8", True)],
    "done_in_period": [("PRF-1", True), ("PRF-2", True), ("PRF-3", True), ("PRF-12", True), ("PRF-5", True)],
    "estimated_before_start": [("PRF-10", True), ("PRF-6", True), ("PRF-1", True), ("PRF-2", True), ("PRF-12", True),
                               ("PRF-8", False), ("PRF-3", False)],
    "time_logged": [("PRF-1", True), ("PRF-2", True), ("PRF-3", True), ("PRF-5", True), ("PRF-12", False)],
    "within_estimate": [("PRF-1", True), ("PRF-2", False), ("PRF-5", False)],
    "stuck_now": [("PRF-6", True), ("PRF-7", False), ("PRF-8", False)],
    "reopened": [("PRF-2", True), ("PRF-1", False), ("PRF-3", False), ("PRF-12", False), ("PRF-5", False)],
    "skipped_stage": [("PRF-2", True), ("PRF-5", True), ("PRF-1", False), ("PRF-3", False), ("PRF-12", False)],
    "code_review": [("PRF-1", True), ("PRF-5", True), ("PRF-2", False)],
    "open_findings_per_10": [("PRF-6", True), ("PRF-7", True), ("PRF-8", True)],
}
BOB_90: dict[str, list[tuple[str, bool]]] = {
    "tickets": [("PRF-20", True), ("PRF-21", True)],
    "open_tickets": [("PRF-21", True)],
    "in_progress": [],
    "done_in_period": [("PRF-20", True)],
    "estimated_before_start": [("PRF-20", False)],
    "time_logged": [("PRF-20", True)],
    "within_estimate": [("PRF-20", True)],
    "stuck_now": [("PRF-21", False)],
    "reopened": [("PRF-20", False)],
    "skipped_stage": [("PRF-20", False)],
    "code_review": [("PRF-20", True)],
    "open_findings_per_10": [("PRF-21", False)],
}
BOARD_90: dict[str, list[tuple[str, bool]]] = {
    "tickets": [(f"PRF-{n}", True) for n in (1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 20, 21, 30, 31)],
    "open_tickets": [("PRF-6", True), ("PRF-7", True), ("PRF-8", True), ("PRF-21", True), ("PRF-31", True)],
    "in_progress": [("PRF-6", True), ("PRF-8", True)],
    "done_in_period": [("PRF-20", True), ("PRF-1", True), ("PRF-30", True), ("PRF-2", True), ("PRF-3", True),
                       ("PRF-12", True), ("PRF-5", True)],
    "estimated_before_start": [("PRF-10", True), ("PRF-6", True), ("PRF-30", True), ("PRF-1", True), ("PRF-2", True),
                               ("PRF-12", True), ("PRF-8", False), ("PRF-20", False), ("PRF-3", False)],
    "time_logged": [("PRF-20", True), ("PRF-1", True), ("PRF-30", True), ("PRF-2", True), ("PRF-3", True),
                    ("PRF-5", True), ("PRF-12", False)],
    "within_estimate": [("PRF-20", True), ("PRF-1", True), ("PRF-30", True), ("PRF-2", False), ("PRF-5", False)],
    "stuck_now": [("PRF-6", True), ("PRF-31", True), ("PRF-7", False), ("PRF-8", False), ("PRF-21", False)],
    "reopened": [("PRF-2", True), ("PRF-20", False), ("PRF-1", False), ("PRF-30", False), ("PRF-3", False),
                 ("PRF-12", False), ("PRF-5", False)],
    "skipped_stage": [("PRF-2", True), ("PRF-5", True), ("PRF-20", False), ("PRF-1", False), ("PRF-30", False),
                      ("PRF-3", False), ("PRF-12", False)],
    "code_review": [("PRF-20", True), ("PRF-1", True), ("PRF-5", True), ("PRF-2", False)],
    "open_findings_per_10": [("PRF-6", True), ("PRF-7", True), ("PRF-8", True), ("PRF-31", True), ("PRF-21", False)],
}
RATES_90 = {  # (numerator, denominator) behind each rate and the ratio, worked out by hand
    "ann": {"estimated_before_start": (5, 7), "time_logged": (4, 5), "within_estimate": (1, 3), "stuck_now": (1, 3),
            "reopened": (1, 5), "skipped_stage": (2, 5), "code_review": (2, 3), "open_findings_per_10": (4, 3)},
    "board": {"estimated_before_start": (6, 9), "time_logged": (6, 7), "within_estimate": (3, 5), "stuck_now": (2, 5),
              "reopened": (1, 7), "skipped_stage": (2, 7), "code_review": (3, 4), "open_findings_per_10": (5, 5)},
}


@pytest.mark.parametrize("measure", EVERY_KEY)
def test_each_list_is_the_tickets_worked_out_by_hand_in_the_contracts_order(client: TestClient, measure: str) -> None:
    card = scorecard(client)
    for who, expected in (("ann", ANN_90), ("bob", BOB_90), (None, BOARD_90)):
        body = proof(client, measure, who)
        check_proof(body, card, measure, None if who is None else pid(who))
        assert listed(body) == expected[measure], (who, measure)
        assert body["left_out"] == []
        figures = RATES_90.get(who or "board", {}).get(measure)
        if figures is not None:
            assert (body["value"]["numerator"], body["value"]["denominator"]) == figures, (who, measure)


def test_a_ticket_says_what_it_is_and_never_links_anywhere_itself(client: TestClient) -> None:
    body = proof(client, "tickets", "ann")
    first = body["tickets"][0]
    assert (first["key"], first["summary"], first["issue_type"], first["status"], first["status_category"]) == (
        "PRF-1", "Work on PRF-1", "Task", "Done", "done")
    by_key = {ticket["key"]: ticket for ticket in body["tickets"]}
    assert (by_key["PRF-8"]["status"], by_key["PRF-8"]["status_category"]) == ("Code Review", "indeterminate")
    assert all(ticket["url"] is None and ticket["ignore"] is None for ticket in body["tickets"])
    assert body["person"] == person("ann")


def test_the_period_edges_hold_for_the_list_as_for_the_figure(client: TestClient) -> None:
    thirty = proof(client, "done_in_period", "ann", days=30)
    assert listed(thirty) == [("PRF-1", True), ("PRF-2", True)]
    assert listed(proof(client, "estimated_before_start", "ann", days=30)) == [
        ("PRF-10", True), ("PRF-6", True), ("PRF-1", True), ("PRF-2", True), ("PRF-8", False)]
    half_year = proof(client, "done_in_period", "ann", days=180)
    # Done exactly at the start of the 90 days is in it; a millisecond earlier only in 180; after the sync, in none.
    assert listed(half_year) == [("PRF-1", True), ("PRF-2", True), ("PRF-3", True), ("PRF-12", True), ("PRF-5", True),
                                 ("PRF-4", True)]
    assert listed(proof(client, "reopened", "ann", days=180))[:2] == [("PRF-2", True), ("PRF-4", True)]
    for days, body in ((30, thirty), (180, half_year)):
        assert body["period"] == {"days": days, "from": ts(T - days * DAY), "to": ts(T)}
        check_proof(body, scorecard(client, pid("ann"), days), "done_in_period", pid("ann"))
    assert proof(client, "tickets", "ann")["period"] == {"days": 90, "from": ts(S90), "to": ts(T)}


def test_at_is_when_it_was_done_or_started_and_nothing_for_the_measures_of_now(client: TestClient) -> None:
    done = {ticket["key"]: ticket["at"] for ticket in proof(client, "done_in_period")["tickets"]}
    assert done == {"PRF-20": ts(T - DAY), "PRF-1": ts(T - 2 * DAY), "PRF-30": ts(T - 3 * DAY),
                    "PRF-2": ts(T - 10 * DAY), "PRF-3": ts(T - 40 * DAY), "PRF-12": ts(T - 40 * DAY), "PRF-5": ts(S90)}
    started = {ticket["key"]: ticket["at"] for ticket in proof(client, "estimated_before_start", "ann")["tickets"]}
    assert started["PRF-10"] == ts(T - DAY) and started["PRF-3"] == ts(T - 41 * DAY)
    for measure in ("open_tickets", "in_progress", "stuck_now", "open_findings_per_10", "tickets"):
        assert {ticket["at"] for ticket in proof(client, measure)["tickets"]} == {None}, measure


def test_each_detail_says_why_the_ticket_counts_or_not(client: TestClient) -> None:
    estimated = details(proof(client, "estimated_before_start", "ann"))
    assert estimated["PRF-2"] == {"started_at": ts(T - 12 * DAY), "estimate_set_at": ts(T - 12 * DAY + 30 * MINUTE),
                                  "grace_hours": GRACE_HOURS}
    assert estimated["PRF-8"] == {"started_at": ts(T - DAY), "estimate_set_at": None, "grace_hours": GRACE_HOURS}
    late = details(proof(client, "estimated_before_start", "bob"))["PRF-20"]
    assert late == {"started_at": ts(T - 2 * DAY), "estimate_set_at": ts(T - DAY), "grace_hours": GRACE_HOURS}

    logged = details(proof(client, "time_logged", "ann"))
    assert logged == {"PRF-1": {"time_spent_s": 3600, "subtask_time_spent_s": 0},
                      "PRF-2": {"time_spent_s": 7200, "subtask_time_spent_s": 0},
                      "PRF-3": {"time_spent_s": 0, "subtask_time_spent_s": 1800},
                      # An aggregate below its own time (Jira's rounding) is never a negative subtask time.
                      "PRF-5": {"time_spent_s": 3600, "subtask_time_spent_s": 0},
                      "PRF-12": {"time_spent_s": 0, "subtask_time_spent_s": 0}}

    effort = details(proof(client, "within_estimate", "ann"))
    assert {key: (found["ratio"], found["limit"]) for key, found in effort.items()} == {
        "PRF-1": (1.2, EFFORT_LIMIT), "PRF-2": (2.0, EFFORT_LIMIT), "PRF-5": (1.5, EFFORT_LIMIT)}

    stuck = details(proof(client, "stuck_now", "ann"))
    assert (stuck["PRF-6"]["status"], stuck["PRF-6"]["since"]) == ("In Progress", ts(T - 3 * DAY))
    assert stuck["PRF-6"]["threshold"] == "p90 of In Progress: 48.5 h"
    assert stuck["PRF-7"] == {"status": "To Do", "since": ts(S90 - 20 * DAY), "hours": stuck["PRF-7"]["hours"],
                              "threshold": None}
    assert stuck["PRF-7"]["hours"] > stuck["PRF-8"]["hours"] > 0
    assert (stuck["PRF-8"]["status"], stuck["PRF-8"]["since"]) == ("Code Review", ts(T - 12 * HOUR))
    nobodys = details(proof(client, "stuck_now"))["PRF-31"]
    assert nobodys["threshold"] == "p90 of To Do: 524.8 h"

    assert details(proof(client, "reopened", "ann")) == {"PRF-2": {"times": 2}, "PRF-1": {"times": 0},
                                                          "PRF-3": {"times": 0}, "PRF-12": {"times": 0},
                                                          "PRF-5": {"times": 0}}
    skips = details(proof(client, "skipped_stage", "ann"))
    assert skips == {"PRF-2": {"stages": ["Testing"]}, "PRF-5": {"stages": ["Code review"]}, "PRF-1": {"stages": []},
                     "PRF-3": {"stages": []}, "PRF-12": {"stages": []}}
    assert details(proof(client, "code_review", "ann")) == {"PRF-1": {"breach": False}, "PRF-5": {"breach": False},
                                                             "PRF-2": {"breach": True}}
    ratio = details(proof(client, "open_findings_per_10", "ann"))
    assert {key: (found["findings"], sorted(found["rule_ids"])) for key, found in ratio.items()} == {
        "PRF-6": (2, ["W-AGE-01", "W-EFF-01"]), "PRF-7": (1, ["W-EST-01"]), "PRF-8": (1, ["R-EST-01"])}
    board = details(proof(client, "open_findings_per_10"))
    assert (board["PRF-21"]["findings"], board["PRF-21"]["rule_ids"]) == (0, [])


def test_the_whole_board_says_who_each_ticket_counts_for_and_nobody_for_the_unassigned(client: TestClient) -> None:
    for measure in ("open_tickets", "done_in_period", "estimated_before_start", "open_findings_per_10"):
        credits = {ticket["key"]: ticket["credit"] for ticket in proof(client, measure)["tickets"]}
        for key, credit in credits.items():
            holder = next(work.holder for work in PRF if work.key == key)
            if holder is None:
                assert credit == {"person": None, "basis": None, "field": None}, (measure, key)
            else:
                assert credit == {"person": person(holder), "basis": "assignee_now", "field": None}, (measure, key)


def test_no_epic_sprint_finding_or_gone_issue_is_ever_listed(client: TestClient) -> None:
    for measure in EVERY_KEY:
        for who in ("ann", None):
            text = json.dumps(proof(client, measure, who))
            for absent in ("PRF-9", "PRF-11", "sprint:7", "PRF-999"):
                assert f'"{absent}"' not in text, (measure, who, absent)


def test_a_person_with_nothing_behind_a_figure_gets_an_empty_list_never_an_error(client: TestClient) -> None:
    card = scorecard(client)
    body = proof(client, "in_progress", "bob")
    check_proof(body, card, "in_progress", pid("bob"))
    assert (body["total"], body["counted"], body["tickets"], body["value"]["value"]) == (0, 0, [], 0)
    member = scorecard(client, pid("cy"))
    for measure in EVERY_KEY:
        body = proof(client, measure, "cy")
        check_proof(body, member, measure, pid("cy"))
        assert (body["total"], body["counted"], body["tickets"], body["left_out"]) == (0, 0, [], []), measure
    assert proof(client, "tickets", "cy")["value"] == {"value": 0, "numerator": None, "denominator": None,
                                                       "enough": True, "vs_board": None}
    # Someone who holds only an Epic is on the board, with nothing of theirs behind any figure.
    epics_only = scorecard(client, pid("dot"))
    for measure in ("tickets", "open_tickets", "stuck_now", "open_findings_per_10"):
        body = proof(client, measure, "dot")
        check_proof(body, epics_only, measure, pid("dot"))
        assert body["tickets"] == [] and "PRF-11" not in json.dumps(body), measure


def test_the_figures_of_now_are_the_same_tickets_whatever_the_period(client: TestClient) -> None:
    for measure in ("open_tickets", "in_progress", "stuck_now", "open_findings_per_10", "tickets"):
        for who in ("ann", None):
            lists = {days: listed(proof(client, measure, who, days)) for days in PERIODS}
            assert lists[30] == lists[90] == lists[180], (measure, who)


# --- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "level=people&measure=open_tickets&page=2", "level=people&measure=open_tickets&colour=red", "level=people",
        "level=people&measure=", "level=people&measure=nope", "level=people&measure=Open_tickets",
        "level=people&measure=overall", "level=people&measure=open_tickets&measure=open_tickets",
        "level=people&measure=open_tickets&measure=stuck_now", "level=people&measure=open_tickets&days=60",
        "level=people&measure=open_tickets&days=", "level=people&measure=open_tickets&days=ninety",
        "level=people&measure=open_tickets&days=90.0", "level=people&measure=open_tickets&days=090",
        "level=people&measure=open_tickets&days=90&days=90", "level=people&level=people&measure=open_tickets",
        "level=admin&measure=open_tickets", "level=People&measure=open_tickets",
        "level=people&measure=open_tickets&person=p0123", "level=people&measure=open_tickets&person=P0123456789AB",
        "level=people&measure=open_tickets&person=acc-ann", "level=people&measure=open_tickets&person=unassigned",
        "level=people&measure=open_tickets&person=board", "project=PRF&project=PRF&level=people&measure=open_tickets",
        "level=people&measure=open_tickets%27%20OR%201%3D1", "level=people&measure=open_tickets%00",
        f"level=people&measure=open_tickets&person=p{'0' * 12}%20", "level=people&measure=" + "x" * 5000,
    ],
)
def test_bad_parameters_are_422(client: TestClient, query: str) -> None:
    project = "" if "project=" in query else "project=PRF&"
    response = client.get(f"{PROOF}?{project}{query}")
    assert response.status_code == 422, (query, response.text)
    assert response.headers["cache-control"] == "no-store"


def test_a_person_given_twice_or_two_people_are_422(client: TestClient) -> None:
    base = f"{PROOF}?project=PRF&level=people&measure=open_tickets"
    assert client.get(f"{base}&person={pid('ann')}&person={pid('ann')}").status_code == 422
    assert client.get(f"{base}&person={pid('ann')}&person={pid('bob')}").status_code == 422


@pytest.mark.parametrize("query", ["measure=open_tickets", "measure=stuck_now&level=team",
                                   f"measure=tickets&level=team&person={'p' + '0' * 12}",
                                   "measure=code_review&level=team&project=BARE"])
def test_team_level_is_403_people_level_only_and_learns_nothing(client: TestClient, query: str) -> None:
    project = "" if "project=" in query else "project=PRF&"
    response = client.get(f"{PROOF}?{project}{query}")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "people_level_only"
    assert response.headers["cache-control"] == "no-store"
    assert "PRF-" not in response.text


def test_someone_not_on_the_board_is_404_and_an_unknown_board_too(client: TestClient) -> None:
    base = f"{PROOF}?level=people&measure=open_tickets"
    assert client.get(f"{base}&project=PRF&person=p000000000000").status_code == 404
    # Bob holds work on PRF and BARE; Eve only counts on CRD: neither is anyone on a board they have nothing on.
    assert client.get(f"{base}&project=BARE&person={pid('ann')}").status_code == 404
    assert client.get(f"{base}&project=PRF&person={pid('eve')}").status_code == 404
    assert client.get(f"{base}&project=NOPE").status_code == 404
    assert client.get(f"{base}&project=PRF", headers={"X-Service-Key": "wrong"}).status_code == 401


def test_a_measure_the_board_cannot_show_is_409_not_measurable_for_its_period(client: TestClient) -> None:
    card = scorecard(client, board="BARE")
    unmeasurable = {m["key"] for m in card["measures"] if not m["measurable"]}
    assert {"code_review", "time_logged"} <= unmeasurable
    for who in (None, pid("bob")):
        for measure in unmeasurable:
            params = {"project": "BARE", "level": "people", "measure": measure, **({"person": who} if who else {})}
            response = client.get(PROOF, params=params)
            assert response.status_code == 409, (measure, response.text)
            assert response.json()["code"] == "not_measurable"
            assert response.headers["cache-control"] == "no-store"
    # Time logged is a fact of the period: the last worklog was 100 days ago, so 180 days can show it.
    assert {m["key"]: m["measurable"] for m in scorecard(client, board="BARE", days=180)["measures"]}["time_logged"]
    half_year = proof(client, "time_logged", "bob", days=180, board="BARE")
    check_proof(half_year, scorecard(client, pid("bob"), 180, "BARE"), "time_logged", pid("bob"))
    assert listed(half_year) == [("BARE-1", False)]
    for measure in ("tickets", "open_tickets", "done_in_period"):
        check_proof(proof(client, measure, "bob", board="BARE"), scorecard(client, pid("bob"), 90, "BARE"), measure,
                    pid("bob"))


def test_a_measure_whose_rule_the_board_turned_off_is_409(fresh: Settings) -> None:
    with Store.open(fresh.db_file) as store:
        revision = load_board_settings(store.conn, "PRF").revision
        save_board_settings(store, RULES, "PRF", revision=revision, changes=[Change("R-FLOW-03", enabled=False)],
                            viewer_hash="v" + "0" * 12, viewer_name="Admin Tester", now=T)
    client = serve(fresh)
    response = client.get(PROOF, params={"project": "PRF", "level": "people", "measure": "reopened"})
    assert response.status_code == 409 and response.json()["code"] == "not_measurable"
    check_proof(proof(client, "skipped_stage"), scorecard(client), "skipped_stage", None)


def test_with_comparison_off_one_person_and_the_whole_board_are_both_shown(root: Path) -> None:
    off = serve(settings_of(root, people_comparison="off"))
    card = scorecard(off, pid("ann"))
    check_proof(proof(off, "stuck_now", "ann"), card, "stuck_now", pid("ann"))
    check_proof(proof(off, "stuck_now"), card, "stuck_now", None)
    assert listed(proof(off, "reopened", "ann")) == ANN_90["reopened"]


def test_no_proof_holds_an_account_id_an_address_or_a_zone(client: TestClient) -> None:
    for measure in EVERY_KEY:
        for who in (None, "ann", "bob"):
            text = json.dumps(proof(client, measure, who))
            assert not [secret for secret in SECRETS if secret in text], (measure, who)


# --- the access log -----------------------------------------------------------------------------


def test_each_proof_read_is_recorded_as_a_scorecard_tickets_view(fresh: Settings) -> None:
    client = serve(fresh)
    proof(client, "stuck_now", "ann", days=30)
    proof(client, "tickets")
    found = entries(fresh)
    assert [(row["action"], row["subjects"], row["detail"]["measure"], row["detail"]["days"]) for row in found] == [
        ("scorecard_tickets_view", [pid("ann")], "stuck_now", 30),
        ("scorecard_tickets_view", [], "tickets", 90),
    ]
    for row in found:
        assert (row["viewer_hash"], row["viewer_name"], row["level"], row["project_key"]) == (
            ADMIN, ADMIN_NAME, "people", "PRF")
        assert row["at"] == ts(T + HOUR) and len(row["id"]) == 32
    log = client.get("/api/v1/access-log", params={"level": "people", "action": "scorecard_tickets_view"})
    assert log.status_code == 200, log.text
    assert len([entry for entry in log.json()["entries"] if entry["action"] == "scorecard_tickets_view"]) == 2


def test_a_refused_proof_read_records_nothing(fresh: Settings) -> None:
    client = serve(fresh)
    base = {"project": "PRF", "measure": "stuck_now"}
    assert client.get(PROOF, params=base).status_code == 403
    assert client.get(PROOF, params={**base, "level": "people", "person": "p000000000000"}).status_code == 404
    assert client.get(PROOF, params={**base, "level": "people", "days": "7"}).status_code == 422
    assert client.get(PROOF, params={"project": "BARE", "level": "people", "measure": "code_review"}).status_code == 409
    assert entries(fresh) == []


def test_a_proof_read_without_a_named_viewer_is_refused(fresh: Settings) -> None:
    client = serve(fresh, headers={"X-Service-Key": KEY})
    response = client.get(PROOF, params={"project": "PRF", "level": "people", "measure": "tickets"})
    assert response.status_code == 422, response.text
    assert entries(fresh) == []


# --- ignored findings leave their tickets out, with who and why ---------------------------------


def ignore(client: TestClient, target: dict[str, Any], scope: str, reason: str | None = None) -> dict[str, Any]:
    response = client.post("/api/v1/findings/ignore", params={"project": "PRF", "level": "people"},
                           json={"target": target, "scope": scope, "reason": reason})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def left_out(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {ticket["key"]: ticket["ignore"] for ticket in body["left_out"]}


def test_an_ignored_finding_leaves_its_ticket_out_of_its_measure_and_nowhere_else(fresh: Settings) -> None:
    client = serve(fresh)
    ignore(client, {"finding_ids": [finding_id("PRF-6", "W-AGE-01")]}, "finding", "Agreed with the team")
    card = scorecard(client)
    stuck = proof(client, "stuck_now", "ann")
    check_proof(stuck, card, "stuck_now", pid("ann"))
    assert listed(stuck) == [("PRF-7", False), ("PRF-8", False)]
    gone = left_out(stuck)["PRF-6"]
    assert (gone["scope"], gone["rule_id"], gone["reason"], gone["by"]) == (
        "finding", "W-AGE-01", "Agreed with the team", ADMIN_NAME)
    assert gone["at"] == ts(T + HOUR)
    board = proof(client, "stuck_now")
    check_proof(board, card, "stuck_now", None)
    assert listed(board) == [("PRF-31", True), ("PRF-7", False), ("PRF-8", False), ("PRF-21", False)]
    assert list(left_out(board)) == ["PRF-6"]
    # A count never moves for an ignore; the ratio keeps the ticket, its ignored finding no longer open.
    assert listed(proof(client, "open_tickets", "ann")) == ANN_90["open_tickets"]
    ratio = proof(client, "open_findings_per_10", "ann")
    check_proof(ratio, card, "open_findings_per_10", pid("ann"))
    assert (ratio["value"]["numerator"], ratio["value"]["denominator"], ratio["left_out"]) == (3, 3, [])
    assert details(ratio)["PRF-6"]["findings"] == 1
    for measure in ("estimated_before_start", "in_progress", "tickets"):
        assert proof(client, measure, "ann")["left_out"] == [], measure


def test_an_ignored_ticket_leaves_every_measure_with_rules_and_no_count(fresh: Settings) -> None:
    client = serve(fresh)
    ignore(client, {"issue_keys": ["PRF-2"]}, "ticket", None)
    card = scorecard(client)
    for measure, kept in (("reopened", [("PRF-1", False), ("PRF-3", False), ("PRF-12", False), ("PRF-5", False)]),
                          ("skipped_stage", [("PRF-5", True), ("PRF-1", False), ("PRF-3", False), ("PRF-12", False)]),
                          ("within_estimate", [("PRF-1", True), ("PRF-5", False)]),
                          ("code_review", [("PRF-1", True), ("PRF-5", True)]),
                          ("estimated_before_start", [("PRF-10", True), ("PRF-6", True), ("PRF-1", True),
                                                      ("PRF-12", True), ("PRF-8", False), ("PRF-3", False)])):
        body = proof(client, measure, "ann")
        check_proof(body, card, measure, pid("ann"))
        assert listed(body) == kept, measure
        found = left_out(body)
        assert list(found) == ["PRF-2"], measure
        assert (found["PRF-2"]["scope"], found["PRF-2"]["rule_id"], found["PRF-2"]["by"]) == ("ticket", None,
                                                                                              ADMIN_NAME)
    for measure in ("done_in_period", "time_logged", "tickets"):
        body = proof(client, measure, "ann")
        check_proof(body, card, measure, pid("ann"))
        assert listed(body) == ANN_90[measure] and body["left_out"] == [], measure


def test_an_ignored_open_ticket_leaves_the_ratio_and_its_findings_with_it(fresh: Settings) -> None:
    client = serve(fresh)
    ignore(client, {"issue_keys": ["PRF-6"]}, "ticket", "Moved to another team")
    card = scorecard(client)
    ratio = proof(client, "open_findings_per_10", "ann")
    check_proof(ratio, card, "open_findings_per_10", pid("ann"))
    assert listed(ratio) == [("PRF-7", True), ("PRF-8", True)]
    assert (ratio["value"]["numerator"], ratio["value"]["denominator"]) == (2, 2)
    assert left_out(ratio)["PRF-6"]["reason"] == "Moved to another team"
    stuck = proof(client, "stuck_now", "ann")
    assert (listed(stuck), list(left_out(stuck))) == ([("PRF-7", False), ("PRF-8", False)], ["PRF-6"])
    for measure in ("open_tickets", "in_progress"):
        assert listed(proof(client, measure, "ann")) == ANN_90[measure], measure


def test_an_ignore_counts_only_against_the_measures_of_its_rule(fresh: Settings) -> None:
    client = serve(fresh)
    ignore(client, {"finding_ids": [finding_id("PRF-3", "R-EST-01"), finding_id("PRF-8", "R-EST-01")]}, "finding")
    card = scorecard(client)
    estimated = proof(client, "estimated_before_start", "ann")
    check_proof(estimated, card, "estimated_before_start", pid("ann"))
    assert listed(estimated) == [("PRF-10", True), ("PRF-6", True), ("PRF-1", True), ("PRF-2", True), ("PRF-12", True)]
    assert sorted(left_out(estimated)) == ["PRF-3", "PRF-8"]
    assert left_out(estimated)["PRF-3"]["reason"] is None
    reopened = proof(client, "reopened", "ann")
    assert (listed(reopened), reopened["left_out"]) == (ANN_90["reopened"], [])
    ratio = proof(client, "open_findings_per_10", "ann")
    check_proof(ratio, card, "open_findings_per_10", pid("ann"))
    assert listed(ratio) == [("PRF-6", True), ("PRF-7", True), ("PRF-8", False)]
    assert (ratio["value"]["numerator"], ratio["value"]["denominator"]) == (3, 3)


def test_a_restored_ignore_puts_the_ticket_back_at_once(fresh: Settings) -> None:
    client = serve(fresh)
    made = ignore(client, {"issue_keys": ["PRF-2"]}, "ticket", "For now")
    assert list(left_out(proof(client, "reopened", "ann"))) == ["PRF-2"]
    restored = client.post("/api/v1/findings/restore", params={"project": "PRF", "level": "people"},
                           json={"ignore_ids": made["ignore_ids"]})
    assert restored.status_code == 200, restored.text
    body = proof(client, "reopened", "ann")
    check_proof(body, scorecard(client), "reopened", pid("ann"))
    assert body["left_out"] == [] and "PRF-2" in [key for key, _ in listed(body)]


# --- credit: the proof names who each ticket counts for, and why -------------------------------


def credit_of(body: dict[str, Any]) -> dict[str, tuple[str | None, str | None, str | None]]:
    return {ticket["key"]: (ticket["credit"]["person"]["id"] if ticket["credit"]["person"] else None,
                            ticket["credit"]["basis"], ticket["credit"]["field"]) for ticket in body["tickets"]}


def test_finished_work_counts_for_its_programmer_else_its_assignee_now(client: TestClient) -> None:
    card = scorecard(client, board="CRD")
    board = proof(client, "done_in_period", board="CRD")
    check_proof(board, card, "done_in_period", None)
    credits = credit_of(board)
    assert credits["CRD-1"] == (pid("eve"), "field", "Programmer")
    assert credits["CRD-2"][1:] == ("field", "Programmer") and credits["CRD-2"][0] in (pid("eve"), pid("hal"))
    assert credits["CRD-3"] == (pid("ann"), "assignee_now", None)
    # No Programmer and no one assigned now: they count for no one, whoever held them before.
    for key in ("CRD-4", "CRD-5", "CRD-6", "CRD-7"):
        assert credits[key] == (None, None, None), key
    expected = {"eve": {"CRD-1": "field", "CRD-2": "field"}, "hal": {"CRD-2": "field"}, "ann": {"CRD-3": "assignee_now"}}
    for who, keys in expected.items():
        card = scorecard(client, pid(who), board="CRD")
        body = proof(client, "done_in_period", who, board="CRD")
        check_proof(body, card, "done_in_period", pid(who))
        assert {key: basis for key, (_id, basis, _field) in credit_of(body).items()} == keys, who


def test_open_work_counts_for_its_programmer_too(client: TestClient) -> None:
    card = scorecard(client, board="CRD")
    opened = proof(client, "open_tickets", board="CRD")
    check_proof(opened, card, "open_tickets", None)
    assert credit_of(opened) == {"CRD-8": (pid("eve"), "field", "Programmer")}
    assert proof(client, "open_tickets", "ann", board="CRD")["tickets"] == []
    eve = proof(client, "tickets", "eve", board="CRD")
    check_proof(eve, scorecard(client, pid("eve"), board="CRD"), "tickets", pid("eve"))
    assert sorted(key for key, _counted in listed(eve)) == ["CRD-1", "CRD-2", "CRD-8"]
    ann = proof(client, "tickets", "ann", board="CRD")
    assert sorted(key for key, _counted in listed(ann)) == ["CRD-3"]
    started = proof(client, "estimated_before_start", "eve", board="CRD")
    assert credit_of(started) == {key: (pid("eve"), "field", "Programmer") for key in ("CRD-8", "CRD-1", "CRD-2")}


def test_everyones_tickets_mean_the_same_on_every_page_that_counts_them(client: TestClient) -> None:
    card = scorecard(client, board="CRD")
    assert card["basis"] == "credit"
    rows = {row["person"]["id"]: row["tickets"] for row in
            client.get("/api/v1/people", params={"project": "CRD"}).json()["people"] if row["person"]}
    directory = {entry["person"]["id"]: entry["tickets"] for entry in
                 client.get("/api/v1/people/directory", params={"project": "CRD"}).json()["people"]}
    expected = {"ann": 1, "eve": 3, "hal": 1}
    for who, tickets in expected.items():
        listed_for = proof(client, "tickets", who, board="CRD")
        assert listed_for["total"] == tickets, who
        assert rows.get(pid(who)) == directory.get(pid(who)) == tickets, who
        row = next(row for row in card["people"] if row["person"]["id"] == pid(who))
        assert row["tickets"] == tickets, who
    # Fay and Gus only held tickets before: nothing counts for them.
    for who in ("fay", "gus"):
        assert not rows.get(pid(who)) and not directory.get(pid(who)), who
        assert pid(who) not in {row["person"]["id"] for row in card["people"]}, who


def test_the_proof_follows_the_boards_credit_setting_at_once(fresh: Settings) -> None:
    client = serve(fresh)
    setting = client.get("/api/v1/people/credit", params={"project": "CRD", "level": "people"})
    assert setting.status_code == 200, setting.text
    changed = client.put("/api/v1/people/credit", params={"project": "CRD", "level": "people"},
                         json={"source": "assignee", "field": None, "revision": setting.json()["setting"]["revision"]})
    assert changed.status_code == 200, changed.text
    card = scorecard(client, board="CRD")
    board = proof(client, "done_in_period", board="CRD")
    check_proof(board, card, "done_in_period", None)
    credits = credit_of(board)
    # The Programmer is no longer read, and no one is assigned to either now.
    assert credits["CRD-1"] == credits["CRD-2"] == (None, None, None)
    assert credits["CRD-3"] == (pid("ann"), "assignee_now", None)
    ann = proof(client, "done_in_period", "ann", board="CRD")
    check_proof(ann, scorecard(client, pid("ann"), board="CRD"), "done_in_period", pid("ann"))
    assert sorted(key for key, _counted in listed(ann)) == ["CRD-3"]
    assert credit_of(proof(client, "open_tickets", board="CRD")) == {"CRD-8": (pid("ann"), "assignee_now", None)}


# --- DEMO: every list adds up to its figure, for everyone, every measure and period -------------


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    root = tmp_path_factory.mktemp("proof_demo")
    settings = Settings(_env_file=None, jira_projects="DEMO", api_service_key=KEY, db_path=root / "oversight.db",
                        config_dir=CONFIG_FIXTURES, reports_dir=root / "reports", board_discovery="off")
    process, catalogue = ProcessConfig.load(settings.process_file), RulesConfig.load(settings.rules_file)
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=DEMO_NOW, seed=20261105)
        run_derive(store, process, projects=["DEMO"], now=lambda: DEMO_NOW)
        run_rules(store, process, catalogue, FieldsConfig.load(settings.fields_file), projects=["DEMO"],
                  now=lambda: DEMO_NOW)
    return settings


@pytest.fixture(scope="module")
def demo_client(demo: Settings) -> TestClient:
    return serve(demo, DEMO_NOW)


def everyone(client: TestClient) -> list[str]:
    directory = client.get("/api/v1/people/directory", params={"project": "DEMO"}).json()["people"]
    return [entry["person"]["id"] for entry in directory]


DEMO_WORK_SQL = """
    SELECT i.issue_key, i.status_category, i.assignee_id, i.time_spent_s, i.aggregate_time_spent_s,
           m.started_at, m.done_at, m.reopen_count, m.skipped_stages, m.estimate_set_at, m.effort_ratio
    FROM issues AS i LEFT JOIN issue_metrics AS m ON m.issue_key = i.issue_key
    WHERE i.project_key = 'DEMO' AND COALESCE(i.issue_type, '') != 'Epic'
"""
DEMO_FINDINGS_SQL = """
    SELECT issue_key, COUNT(*) AS open_findings, MAX(rule_id = 'W-AGE-01') AS stuck
    FROM findings WHERE project_key = 'DEMO' AND resolved_at IS NULL GROUP BY issue_key
"""


def worked_out(settings: Settings, period: dict[str, Any], account: str | None) -> dict[str, dict[str, Any]]:
    """Each measure's tickets on DEMO, read straight from the snapshot: key -> counted (or open findings)."""
    conn = sqlite3.connect(settings.db_file)
    conn.row_factory = sqlite3.Row
    try:
        work = [row for row in conn.execute(DEMO_WORK_SQL) if account is None or row["assignee_id"] == account]
        open_findings = {row["issue_key"]: row for row in conn.execute(DEMO_FINDINGS_SQL)}
    finally:
        conn.close()

    def held(at: str | None) -> bool:
        return at is not None and period["from"] <= at <= period["to"]

    opened = [row for row in work if row["status_category"] != "done"]
    done = [row for row in work if held(row["done_at"])]
    started = [row for row in work if held(row["started_at"])]
    effort = [row for row in done if row["effort_ratio"] is not None and (row["time_spent_s"] or 0) > 0]
    found = {key: open_findings.get(key) for key in (row["issue_key"] for row in opened)}
    return {
        "tickets": {row["issue_key"]: True for row in work},
        "open_tickets": {row["issue_key"]: True for row in opened},
        "in_progress": {row["issue_key"]: True for row in work if row["status_category"] == "indeterminate"},
        "done_in_period": {row["issue_key"]: True for row in done},
        "estimated_before_start": {
            row["issue_key"]: row["estimate_set_at"] is not None
            and hours_from(row["started_at"], row["estimate_set_at"]) <= GRACE_HOURS for row in started},
        "time_logged": {row["issue_key"]: bool(row["time_spent_s"] or row["aggregate_time_spent_s"]) for row in done},
        "within_estimate": {row["issue_key"]: row["effort_ratio"] < EFFORT_LIMIT for row in effort},
        "stuck_now": {key: bool(row and row["stuck"]) for key, row in found.items()},
        "reopened": {row["issue_key"]: (row["reopen_count"] or 0) > 0 for row in done},
        "skipped_stage": {row["issue_key"]: bool(json.loads(row["skipped_stages"] or "[]")) for row in done},
        "open_findings_per_10": {key: row["open_findings"] if row else 0 for key, row in found.items()},
    }


@pytest.mark.parametrize("days", PERIODS)
def test_on_demo_every_proof_adds_up_to_its_cell_and_lists_the_snapshots_own_tickets(
    demo: Settings, demo_client: TestClient, days: int
) -> None:
    ids = everyone(demo_client)
    assert len(ids) == 11
    accounts = {"p" + hashlib.sha256(f"acc-{n:02d}".encode()).hexdigest()[:12]: f"acc-{n:02d}" for n in range(1, 12)}
    assert set(ids) == set(accounts)
    board_card = scorecard(demo_client, days=days, board="DEMO")
    for who in (None, *ids):
        card = board_card if who is None else scorecard(demo_client, who, days, "DEMO")
        expected = worked_out(demo, card["period"], None if who is None else accounts[who])
        for measure in EVERY_KEY:
            if measure != "tickets" and not measure_of(card, measure)["measurable"]:
                params = {"project": "DEMO", "level": "people", "measure": measure, "days": days,
                          **({"person": who} if who else {})}
                response = demo_client.get(PROOF, params=params)
                assert response.status_code == 409 and response.json()["code"] == "not_measurable", (who, measure)
                continue
            body = proof(demo_client, measure, who, days, "DEMO")
            check_proof(body, card, measure, who)
            if measure not in expected:
                continue
            if measure == "open_findings_per_10":
                got: dict[str, Any] = {ticket["key"]: ticket["detail"]["findings"] for ticket in body["tickets"]}
            else:
                got = {ticket["key"]: ticket["counted"] for ticket in body["tickets"]}
            assert got == expected[measure], (who, measure, set(got) ^ set(expected[measure]))


def test_on_demo_the_stuck_tickets_are_the_findings_pages_own(demo_client: TestClient) -> None:
    for who in everyone(demo_client)[:8]:
        stuck = {ticket["key"] for ticket in proof(demo_client, "stuck_now", who, board="DEMO")["tickets"]
                 if ticket["counted"]}
        response = demo_client.get("/api/v1/findings", params={"project": "DEMO", "rule": "W-AGE-01", "state": "open",
                                                               "person": who, "page_size": 100})
        assert response.status_code == 200, response.text
        assert stuck == {item["issue_key"] for item in response.json()["items"]}, who


def test_on_demo_the_people_lists_counts_are_the_proofs_lists(demo_client: TestClient) -> None:
    rows = demo_client.get("/api/v1/people", params={"project": "DEMO"}).json()["people"]
    for row in rows:
        who = row["person"]["id"] if row["person"] else None
        if who is None:
            continue
        assert proof(demo_client, "tickets", who, board="DEMO")["total"] == row["tickets"], who
        assert proof(demo_client, "open_tickets", who, board="DEMO")["total"] == row["open_tickets"], who


def test_on_demo_the_whole_boards_tickets_answer_within_300_ms(demo_client: TestClient) -> None:
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        body = proof(demo_client, "tickets", board="DEMO")
        timings.append(time.perf_counter() - started)
    assert body["total"] == 1136
    assert min(timings) < 0.3, timings


# --- a large board -------------------------------------------------------------------------------

BIG_TICKETS = 5000


def build_big(root: Path) -> Settings:
    write_config(root)
    with Store.open(root / "oversight.db") as store, store.transaction():
        add_statuses(store, "BIG")
        holders = [f"acc-big-{n:02d}" for n in range(40)]
        store.conn.executemany(
            "INSERT INTO people (account_id, display_name, active, updated_at) VALUES (?, ?, 1, ?)",
            [(account, f"Big Tester {n:02d}", to_iso(T)) for n, account in enumerate(holders)],
        )
        statuses = ("To Do", "In Progress", "Code Review", "Done", "Done", "Done")
        issues, metrics, findings = [], [], []
        for n in range(1, BIG_TICKETS + 1):
            status = statuses[n % len(statuses)]
            done = T - (n % 170) * DAY if status == "Done" else None
            started = (done or T) - 2 * DAY if status != "To Do" else None
            issues.append((f"BIG-{n}", str(20_000 + n), "BIG", "Task", f"Big work {n}", status, CATEGORY[status],
                           holders[n % 40] if n % 9 else None, to_iso(T - 200 * DAY), to_iso(T), 3600, 3600, 3.0))
            metrics.append((f"BIG-{n}", to_iso(started), to_iso(done), n % 7 == 0, "[]", "points",
                            to_iso(started - HOUR) if started else None, 1.0 + (n % 5) / 4, to_iso(T)))
            if n % 3 == 0 and status != "Done":
                evidence = {"status": status, "age_h": 300.0, "threshold_h": 120.0, "pct": 90.0, "history_n": 30}
                findings.append((hashlib.sha256(f"big-{n}".encode()).hexdigest()[:32], "W-AGE-01", RULES.version,
                                 f"BIG-{n}", "BIG", "warning", "warning", f"stuck-{status}", "Stuck",
                                 json.dumps(evidence), to_iso(T), to_iso(T), "r1"))
        store.conn.executemany(
            "INSERT INTO issues (issue_key, issue_id, project_key, issue_type, summary, status, status_category,"
            " assignee_id, created_at, updated_at, time_spent_s, aggregate_time_spent_s, story_points)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", issues)
        store.conn.executemany(
            "INSERT INTO issue_metrics (issue_key, started_at, done_at, reopen_count, skipped_stages, estimate_basis,"
            " estimate_set_at, effort_ratio, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", metrics)
        store.conn.executemany(
            "INSERT INTO findings (finding_id, rule_id, rule_version, issue_key, project_key, kind, severity,"
            " evidence_key, message, evidence_json, first_seen_at, last_seen_at, run_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", findings)
        store.upsert("worklogs", {"worklog_id": "big", "issue_key": "BIG-1", "started_at": to_iso(T - DAY),
                                  "seconds": 3600, "author_id": "app-tempo", "updated_at": to_iso(T - DAY)})
    return settings_of(root, projects="BIG")


@pytest.mark.slow
def test_a_board_of_5000_tickets_lists_them_all_within_one_and_a_half_seconds(tmp_path: Path) -> None:
    client = serve(build_big(tmp_path))
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        body = proof(client, "tickets", board="BIG")
        timings.append(time.perf_counter() - started)
    assert body["total"] == body["counted"] == BIG_TICKETS
    assert min(timings) < 1.5, timings
    card = scorecard(client, days=180, board="BIG")
    for measure in ("done_in_period", "stuck_now", "open_findings_per_10", "reopened"):
        check_proof(proof(client, measure, days=180, board="BIG"), card, measure, None)


def test_every_response_is_never_cached(client: TestClient) -> None:
    for params in ({"project": "PRF", "level": "people", "measure": "tickets"},
                   {"project": "PRF", "measure": "tickets"},
                   {"project": "PRF", "level": "people", "measure": "nope"}):
        assert client.get(PROOF, params=params).headers["cache-control"] == "no-store", params
