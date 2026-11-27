from __future__ import annotations

import hashlib
import itertools
import re
import shutil
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from oversight.api.app import context, create_app
from oversight.api.routes.people import people_scope
from oversight.api.scorecard import load_board
from oversight.ask.catalog import CatalogContext, Facts, validate_params
from oversight.ask.people import PEOPLE_CATALOG
from oversight.config import FieldsConfig, ProcessConfig
from oversight.derive.run import run_derive
from oversight.errors import ConfigError
from oversight.ingest.avatars import PictureFetcher
from oversight.ingest.sync import run_sync
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.settings import PROJECT_ROOT, Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES, VIEWER_HEADERS
from tests.fakes.fake_jira import FakeIssue, FakeJira
from tests.test_api_ask_insights import _seed

T = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)  # every board's last sync: the end of every period
SAVED = T + timedelta(hours=1)  # the service's clock: when a choice is saved
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
KEY = "credit-key"
ADMIN = "0123456789abcdef0123456789abcdef"
ADMIN_NAME = "Morgan Reyes"  # a fictional admin
RULES = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")
FIELDS = FieldsConfig.load(CONFIG_FIXTURES / "fields.yaml")
PROGRAMMER, CODE_REVIEWERS, TESTERS, TEST_REVIEWERS = (
    FIELDS.id_of(name) for name in ("programmer", "code_reviewers", "testers", "test_reviewers"))
NAMES = {"pat": "Pat Programmer", "quinn": "Quinn Coder", "riley": "Riley Developer", "sam": "Sam Reviewer",
         "terry": "Terry Holder", "uma": "Uma Builder", "vic": "Vic Tester", "xena": "Xena Late", "wes": "Wes Member"}
ACCOUNTS = {who: f"acc-{who}" for who in [*NAMES, "ghost"]}  # ghost: in the Programmer field, never synced as a person
CATEGORY = {"To Do": "new", "In Progress": "indeterminate", "Code Review": "indeterminate",
            "In Testing": "indeterminate", "Test Review": "indeterminate", "Done": "done"}
STAGES = {"To Do": "backlog", "In Progress": "development", "Code Review": "code_review", "In Testing": "testing",
          "Test Review": "test_review", "Done": "done"}
UNCHANGED = "unchanged"
F, N = "field", "assignee_now"
PERIODS = (30, 90, 180)


def pid(who: str) -> str:
    return "p" + hashlib.sha256(ACCOUNTS[who].encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Step:
    """One Jira history entry: everything in it shares a changelog id and a moment."""

    days: float  # before the sync
    hours: float = 0  # and this many hours after that
    status: str | None = None
    assignee: str | None = UNCHANGED  # None: the assignee cleared
    programmer: tuple[str, ...] | None = None
    testers: tuple[str, ...] | None = None

    @property
    def at(self) -> datetime:
        return T - self.days * DAY + self.hours * HOUR


@dataclass(frozen=True)
class Work:
    number: int
    created: float  # days before the sync
    history: tuple[Step, ...] = ()
    kind: str = "Task"
    spent: int = 0


# A board that clears the assignee at every hand-off, as MJH DASHBOARD's workflow does.
TEMPLATE: tuple[Work, ...] = (
    # The Programmer field: one person.
    Work(1, 20, (Step(18, status="In Progress", programmer=("pat",)), Step(18, 1, assignee="riley"),
                 Step(15, status="Code Review", assignee=None), Step(14, assignee="sam"),
                 Step(13, status="In Testing", assignee=None), Step(12, assignee="vic"), Step(11, status="Test Review"),
                 Step(10, status="Done", assignee=None))),
    # Two people in it, and one in Testers.
    Work(2, 25, (Step(24, status="In Progress", programmer=("pat", "quinn")), Step(24, 1, assignee="terry"),
                 Step(20, status="Code Review", assignee=None), Step(19, status="In Testing", testers=("vic",)),
                 Step(19, 1, assignee="vic"), Step(9, status="Done", assignee=None)), spent=7200),
    # Its Programmer taken out after it was done.
    Work(3, 30, (Step(29, programmer=("quinn",)), Step(28, status="In Progress"), Step(28, 1, assignee="riley"),
                 Step(26, status="Code Review", assignee=None), Step(8, status="Done"), Step(7, programmer=()))),
    # Assigned and kept to the end.
    Work(4, 20, (Step(19, status="In Progress"), Step(19, 1, assignee="terry"), Step(7, status="Done"))),
    # Cleared at each hand-off: development, code review, testing, test review, done.
    Work(5, 40, (Step(35, status="In Progress"), Step(35, 1, assignee="riley"),
                 Step(30, status="Code Review", assignee=None), Step(29, assignee="sam"),
                 Step(28, status="In Testing", assignee=None), Step(27, assignee="vic"), Step(26, status="Test Review"),
                 Step(6, status="Done", assignee=None)), spent=3600),
    # Reassigned during development.
    Work(6, 45, (Step(44, status="In Progress"), Step(44, 1, assignee="riley"), Step(42, assignee="terry"),
                 Step(40, status="Code Review", assignee=None), Step(39, assignee="sam"),
                 Step(35, status="Done", assignee=None))),
    # Never in development: straight to code review.
    Work(7, 20, (Step(19, assignee="sam"), Step(18, status="Code Review"), Step(17, assignee="vic"),
                 Step(4, status="Done", assignee=None))),
    # Assigned only once it was done, then cleared.
    Work(8, 15, (Step(14, status="In Progress"), Step(3, status="Done"), Step(2, assignee="xena"),
                 Step(1, assignee=None))),
    # No assignee history at all.
    Work(9, 15, (Step(14, status="In Progress"), Step(2, status="Done"))),
    # Its Programmer is an account the snapshot holds no name for.
    Work(10, 15, (Step(14, status="In Progress", programmer=("ghost",)), Step(2, status="Done"))),
    # An Epic: no one's work.
    Work(11, 60, (Step(50, status="In Progress", programmer=("uma",)), Step(1, status="Done")), kind="Epic"),
    Work(12, 50, (Step(49, status="In Progress", programmer=("uma",)), Step(49, 1, assignee="riley"),
                  Step(45, status="Code Review", assignee=None), Step(40, status="Done"))),
    # Its Programmer holds it too: counted once.
    Work(14, 12, (Step(11, status="In Progress", programmer=("terry",)), Step(11, 1, assignee="terry"),
                  Step(5, status="Done"))),
    # Held now by someone other than its Programmer.
    Work(15, 12, (Step(11, status="In Progress", programmer=("quinn",)), Step(11, 1, assignee="terry"),
                  Step(6, status="Done"))),
    Work(20, 10, (Step(9, status="In Progress", programmer=("pat",)), Step(9, 1, assignee="riley"))),
    Work(21, 5, (Step(4, assignee="terry"),)),
    Work(22, 12, (Step(11, status="In Progress", programmer=("quinn",)), Step(11, 1, assignee="riley"),
                  Step(10, status="Code Review", assignee=None))),
    # Done before the last 90 days.
    Work(30, 150, (Step(140, status="In Progress", programmer=("pat",)), Step(140, 1, assignee="terry"),
                   Step(130, status="Code Review", assignee=None), Step(120, status="Done"))),
)
# Assigned before development started and carried into it; and assigned in the very entry that started it.
HELD: tuple[Work, ...] = (
    Work(1, 20, (Step(19, assignee="riley"), Step(18, status="In Progress"),
                 Step(16, status="Code Review", assignee=None), Step(15, assignee="sam"),
                 Step(5, status="Done", assignee=None))),
    Work(2, 20, (Step(18, status="In Progress", assignee="riley"), Step(16, status="Code Review", assignee=None),
                 Step(15, assignee="sam"), Step(5, status="Done", assignee=None))),
)
# Testers as a single-user picker.
PICKED: tuple[Work, ...] = (
    Work(1, 12, (Step(10, status="In Progress", testers=("vic",)), Step(10, 1, assignee="riley"),
                 Step(8, status="Code Review", assignee=None), Step(5, status="Done"))),
)
# Nothing finished yet; the Programmer field filled on open work only.
STARTED: tuple[Work, ...] = (
    Work(1, 5, (Step(4, status="In Progress", programmer=("pat",)), Step(4, 1, assignee="riley"))),
)
BOARDS: dict[str, tuple[Work, ...]] = {"MJH": TEMPLATE, "MJF": TEMPLATE, "MJA": TEMPLATE, "MJT": TEMPLATE,
                                       "HLD": HELD, "OPN": STARTED, "UPK": PICKED}
CREDIT_FILE: dict[str, dict[str, str]] = {"MJF": {"source": "field", "field": "programmer"},
                                          "MJA": {"source": "assignee"},
                                          "MJT": {"source": "field", "field": "testers"},
                                          "UPK": {"source": "field", "field": "testers"}}

# Whom each ticket counts for, worked out by hand for each choice: (who, basis). Only the chosen source is read, as
# it is now: no assignee history, and a ticket it names no one on counts for no one.
WORK_NUMBERS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 15, 20, 21, 22, 30)
NOBODY: dict[int, set[tuple[str, str]]] = {n: set() for n in WORK_NUMBERS}
ASSIGNEE: dict[int, set[tuple[str, str]]] = {
    **NOBODY, 4: {("terry", N)}, 14: {("terry", N)}, 15: {("terry", N)}, 20: {("riley", N)}, 21: {("terry", N)}}
PROGRAMMER_ONLY: dict[int, set[tuple[str, str]]] = {
    **NOBODY, 1: {("pat", F)}, 2: {("pat", F), ("quinn", F)}, 10: {("ghost", F)}, 12: {("uma", F)}, 14: {("terry", F)},
    15: {("quinn", F)}, 20: {("pat", F)}, 22: {("quinn", F)}, 30: {("pat", F)}}
# Automatic: the Programmer where it is filled, otherwise the assignee now (4 and 21).
AUTO: dict[int, set[tuple[str, str]]] = {**PROGRAMMER_ONLY, 4: {("terry", N)}, 21: {("terry", N)}}
CHOSEN_TESTERS: dict[int, set[tuple[str, str]]] = {**NOBODY, 2: {("vic", F)}}
EXPECTED_CREDIT: dict[str, tuple[dict[int, set[tuple[str, str]]], str | None]] = {
    "MJH": (AUTO, "Programmer"), "MJF": (PROGRAMMER_ONLY, "Programmer"), "MJA": (ASSIGNEE, None),
    "MJT": (CHOSEN_TESTERS, "Testers"), "HLD": ({1: set(), 2: set()}, None),
    "OPN": ({1: {("pat", F)}}, "Programmer"),
}


@dataclass(frozen=True)
class Row:
    tickets: int
    open_tickets: int
    in_progress: int
    done: int
    logged: tuple[int | None, int | None]  # time logged on done work: numerator, denominator (None: not measurable)


# Hand counts over the last 90 days: finished are 1-10, 12, 14 and 15 (11 is an Epic); open are 20, 21 and 22
# (20 and 22 in progress). Every figure, open work too, follows the choice.
BY_AUTO_90 = {
    "pat": Row(4, 1, 1, 2, (1, 2)), "quinn": Row(3, 1, 1, 2, (1, 2)), "terry": Row(3, 1, 0, 2, (0, 2)),
    "uma": Row(1, 0, 0, 1, (0, 1)), "ghost": Row(1, 0, 0, 1, (0, 1)),
}
BY_PROGRAMMER_90 = {**BY_AUTO_90, "terry": Row(1, 0, 0, 1, (0, 1))}  # 4 and 21 are only assigned to Terry
BY_ASSIGNEE_90 = {"riley": Row(1, 1, 1, 0, (0, 0)), "terry": Row(4, 1, 0, 3, (0, 3))}
BY_TESTERS_90 = {"vic": Row(1, 0, 0, 1, (1, 1))}
BOARD_90 = Row(17, 3, 2, 13, (2, 13))
# The directory's order: by name folded, the unnamed account as "Unnamed account".
AUTO_ORDER = ["pat", "quinn", "terry", "uma", "ghost"]
ASSIGNEE_ORDER = ["riley", "terry"]
TESTERS_ORDER = ["vic"]
# Assigned now on the template boards, so listed by the directory whatever the choice credits them.
ASSIGNED_NOW = ("riley", "terry")

# Coverage by hand: (finished, [(source, field key, source_filled, credited)]) for each period, on MJH.
COVERAGE: dict[int, tuple[int, list[tuple[str, str | None, int, int]]]] = {
    30: (11, [("auto", None, 5, 6), ("assignee", None, 3, 3), ("field", "programmer", 5, 5),
              ("field", "testers", 1, 1)]),
    90: (13, [("auto", None, 6, 7), ("assignee", None, 3, 3), ("field", "programmer", 6, 6),
              ("field", "testers", 1, 1)]),
    180: (14, [("auto", None, 7, 8), ("assignee", None, 3, 3), ("field", "programmer", 7, 7),
               ("field", "testers", 1, 1)]),
}
FIELD_NAMES = {"programmer": "Programmer", "testers": "Testers", "code_reviewers": "Code Reviewers",
               "test_reviewers": "Test Reviewers"}


# --- the snapshot ---------------------------------------------------------------------------------------------


def board_process(credit: Mapping[str, str] | None = None) -> dict[str, Any]:
    project: dict[str, Any] = {
        "status_to_stage": STAGES,
        "estimate": {"basis": "points", "field": "story_points", "hours_per_point": 8},
        "evidence": {"code_review": {"status": "Code Review"}, "dev_signoff": None, "testing": {"status": "In Testing"},
                     "test_review": {"status": "Test Review"}, "test_signoff": None},
    }
    if credit is not None:
        project["credit"] = dict(credit)
    return project


def process_yaml(boards: Mapping[str, Mapping[str, str] | None]) -> dict[str, Any]:
    return {
        "process_version": "0.1.0-credit",
        "stages": ["backlog", "estimate", "development", "code_review", "dev_signoff", "testing", "test_review",
                   "test_signoff", "done"],
        "start_stage": "development",
        "done_stage": "done",
        "projects": {key: board_process(credit) for key, credit in boards.items()},
    }


def write_config(root: Path, boards: Mapping[str, Mapping[str, str] | None] | None = None,
                 fields: dict[str, Any] | None = None) -> Path:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    chosen = boards if boards is not None else {key: CREDIT_FILE.get(key) for key in BOARDS if key != "UPK"}
    (config / "process.yaml").write_text(yaml.safe_dump(process_yaml(chosen)), encoding="utf-8")
    catalogue = fields or yaml.safe_load((CONFIG_FIXTURES / "fields.yaml").read_text(encoding="utf-8"))
    (config / "fields.yaml").write_text(yaml.safe_dump(catalogue), encoding="utf-8")
    shutil.copyfile(CONFIG_FIXTURES / "rules.yaml", config / "rules.yaml")
    return config


def jira_user(who: str) -> dict[str, Any]:
    user: dict[str, Any] = {"accountId": ACCOUNTS[who], "accountType": "atlassian", "active": who != "ghost"}
    if who in NAMES:
        user["displayName"] = NAMES[who]
    return user


def accounts_text(people: tuple[str, ...]) -> str | None:
    return ", ".join(ACCOUNTS[who] for who in people) or None


def account(who: str | None) -> str | None:
    return ACCOUNTS[who] if who is not None else None


def write_ticket(store: Store, board: str, issue_id: int, work: Work, changelog: Iterator[int]) -> None:
    key = f"{board}-{work.number}"
    status: str = "To Do"
    holder: str | None = None
    people: dict[str, tuple[str, ...]] = {"programmer": (), "testers": ()}
    last = T - work.created * DAY
    for step in work.history:
        changelog_id = str(next(changelog))
        moment = to_iso(step.at)
        if step.status is not None:
            store.upsert("status_changes", {"issue_key": key, "changelog_id": changelog_id, "changed_at": moment,
                                            "author_id": None, "from_status": status, "to_status": step.status})
            status = step.status
        if step.assignee != UNCHANGED:
            store.upsert("field_changes", {"issue_key": key, "changelog_id": changelog_id, "field": "assignee",
                                           "changed_at": moment, "from_value": account(holder),
                                           "to_value": account(step.assignee)})
            holder = step.assignee
        for name, value in (("programmer", step.programmer), ("testers", step.testers)):
            if value is not None:
                store.upsert("field_changes", {"issue_key": key, "changelog_id": changelog_id, "field": name,
                                               "changed_at": moment, "from_value": accounts_text(people[name]),
                                               "to_value": accounts_text(value)})
                people[name] = value
        last = step.at
    testers: Any = [jira_user(who) for who in people["testers"]] or None
    if board == "UPK" and people["testers"]:
        testers = jira_user(people["testers"][0])  # a single-user picker holds one user, not a list
    raw = {"id": str(issue_id), "key": key, "fields": {
        "summary": f"Sample {work.number}", "issuetype": {"name": work.kind},
        "status": {"name": status, "statusCategory": {"key": CATEGORY[status]}},
        "assignee": jira_user(holder) if holder is not None else None,
        PROGRAMMER: [jira_user(who) for who in people["programmer"]] or None, CODE_REVIEWERS: None,
        TESTERS: testers, TEST_REVIEWERS: None,
    }}
    store.upsert("issues", {
        "issue_key": key, "issue_id": str(issue_id), "project_key": board, "issue_type": work.kind,
        "summary": f"Sample {work.number}", "status": status, "status_category": CATEGORY[status],
        "assignee_id": account(holder), "created_at": to_iso(T - work.created * DAY), "updated_at": to_iso(last),
        "resolved_at": to_iso(last) if status == "Done" else None, "time_spent_s": work.spent or None,
        "aggregate_time_spent_s": work.spent or None, "raw_json": raw,
    })


def build(path: Path) -> None:
    process = ProcessConfig.model_validate(process_yaml({key: CREDIT_FILE.get(key) for key in BOARDS}))
    changelog = itertools.count(400001)
    with Store.open(path) as store:
        with store.transaction():
            for who, name in NAMES.items():
                store.upsert("people", {"account_id": ACCOUNTS[who], "display_name": name, "active": 1,
                                        "updated_at": to_iso(T)})
            for number, (board, tickets) in enumerate(BOARDS.items()):
                for n, (name, category) in enumerate(CATEGORY.items()):
                    store.upsert("statuses", {"project_key": board, "status_id": str(10 + n), "name": name,
                                              "category": category})
                store.upsert("board_members", {"project_key": board, "account_id": ACCOUNTS["wes"],
                                               "seen_at": to_iso(T)})
                store.set_cursor(board, last_synced_at=to_iso(T), run_id="r1", account_timezone="Europe/London")
                for work in tickets:
                    write_ticket(store, board, 10000 * (number + 1) + work.number, work, changelog)
                if tickets is TEMPLATE:
                    store.upsert("worklogs", {"worklog_id": f"{board}-w1", "issue_key": f"{board}-2",
                                              "started_at": to_iso(T - 10 * DAY), "seconds": 7200,
                                              "author_id": "app-tempo", "updated_at": to_iso(T - 10 * DAY)})
            # MJH-20 is Riley's now and counts for Pat, its Programmer: a finding on it is Riley's.
            rule = RULES.by_id("R-EST-01")
            store.upsert("findings", {
                "finding_id": "c" * 32, "rule_id": rule.id, "rule_version": RULES.version, "issue_key": "MJH-20",
                "project_key": "MJH", "kind": rule.kind, "severity": rule.severity, "evidence_key": "MJH-20",
                "message": rule.title, "evidence_json": {}, "first_seen_at": to_iso(T - DAY), "last_seen_at": to_iso(T),
                "resolved_at": None, "resolved_why": None, "run_id": "r1",
            })
        run_derive(store, process, projects=list(BOARDS), now=lambda: T)


def headers(viewer: str | None = ADMIN, name: str | None = ADMIN_NAME) -> dict[str, str]:
    found = {"X-Service-Key": KEY}
    if viewer is not None:
        found["X-Oversight-Viewer"] = viewer
    if name is not None:
        found["X-Oversight-Viewer-Name"] = quote(name)
    return found


def settings_for(root: Path, config: Path, projects: str = "MJH,MJF,MJA,MJT,HLD,OPN", **options: Any) -> Settings:
    return Settings(_env_file=None, jira_projects=projects, api_service_key=KEY, db_path=root / "oversight.db",
                    config_dir=config, reports_dir=root / "reports", board_discovery="off", **options)


def serve(settings: Settings, **header_options: Any) -> TestClient:
    return TestClient(create_app(settings, provider=ScriptedProvider(), now=lambda: SAVED),
                      headers=headers(**header_options))


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("credit_snapshot") / "oversight.db"
    build(path)
    return path


@pytest.fixture
def snapshot(built: Path, tmp_path: Path) -> Settings:
    """A copy of the boards for this test alone (it may save a choice), with the standard config."""
    shutil.copyfile(built, tmp_path / "oversight.db")
    return settings_for(tmp_path, write_config(tmp_path))


@pytest.fixture(scope="module")
def reader(built: Path, tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    """The boards as they were built, for the tests that only read."""
    root = tmp_path_factory.mktemp("credit_reader")
    shutil.copyfile(built, root / "oversight.db")
    return serve(settings_for(root, write_config(root)))


# --- reading the API ----------------------------------------------------------------------------------------


def query(settings: Settings, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    conn = open_readonly(settings.db_file)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get(client: TestClient, path: str, board: str, **params: Any) -> dict[str, Any]:
    response = client.get(f"/api/v1/{path}", params={"project": board, **params})
    assert response.status_code == 200, (path, board, response.text)
    body: dict[str, Any] = response.json()
    return body


def scorecard(client: TestClient, board: str, days: int = 90, **params: Any) -> dict[str, Any]:
    return get(client, "people/scorecard", board, level="people", days=days, **params)


def credit(client: TestClient, board: str, days: int = 90) -> dict[str, Any]:
    response = client.get("/api/v1/people/credit", params={"project": board, "level": "people", "days": days})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def put_credit(client: TestClient, board: str, source: str, field: str | None, revision: int) -> httpx.Response:
    return client.put("/api/v1/people/credit", params={"project": board, "level": "people"},
                      json={"source": source, "field": field, "revision": revision})


def choose(client: TestClient, board: str, source: str, field: str | None = None) -> dict[str, Any]:
    response = put_credit(client, board, source, field, credit(client, board)["setting"]["revision"])
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def choice(value: Mapping[str, Any]) -> tuple[str, str | None]:
    """A history entry's from or to as (source, field key); the field may come as its key or as {key, name}."""
    field = value.get("field")
    return value["source"], field["key"] if isinstance(field, Mapping) else field


def rows_by_person(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["person"]["id"]: row for row in body["people"] if row["person"] is not None}


def figures(row: Mapping[str, Any]) -> Row:
    """A person's row or the board's: its tickets and the figures these tests count by hand."""
    values = row["values"]
    logged = values["time_logged"]
    return Row(row["tickets"], values["open_tickets"]["value"], values["in_progress"]["value"],
               values["done_in_period"]["value"], (logged["numerator"], logged["denominator"]))


def expect_scorecard(body: dict[str, Any], expected: Mapping[str, Row], order: list[str]) -> None:
    assert body["basis"] == "credit"
    assert [row["person"]["id"] for row in body["people"]] == [pid(who) for who in order]
    rows = rows_by_person(body)
    assert {who: figures(rows[pid(who)]) for who in order} == dict(expected)
    assert figures(body["board"]) == BOARD_90


# --- D1: whom each ticket counts for ------------------------------------------------------------------------


@pytest.mark.parametrize("board", list(EXPECTED_CREDIT))
def test_each_ticket_counts_for_its_builders_by_the_first_rule_that_gives_anyone(reader: TestClient,
                                                                                board: str) -> None:
    expected, field_name = EXPECTED_CREDIT[board]
    ctx = replace(context(reader.app), selected=board)
    conn = open_readonly(ctx.db_path)
    try:
        found = {ticket.issue_key: {tuple(credit) for credit in ticket.builders} for ticket in load_board(conn, ctx).work}
    finally:
        conn.close()
    assert found == {
        f"{board}-{n}": {(pid(who), basis, field_name if basis == F else None) for who, basis in credits}
        for n, credits in expected.items()
    }


def test_a_board_that_clears_its_assignees_credits_its_work_to_the_programmer_field(reader: TestClient) -> None:
    expect_scorecard(scorecard(reader, "MJH"), BY_AUTO_90, AUTO_ORDER)
    expect_scorecard(scorecard(reader, "MJF"), BY_PROGRAMMER_90, AUTO_ORDER)


def test_with_the_assignee_chosen_work_counts_for_the_assignee_now_alone(reader: TestClient) -> None:
    expect_scorecard(scorecard(reader, "MJA"), BY_ASSIGNEE_90, ASSIGNEE_ORDER)


def test_with_a_people_field_chosen_its_people_alone_count(reader: TestClient) -> None:
    expect_scorecard(scorecard(reader, "MJT"), BY_TESTERS_90, TESTERS_ORDER)


def test_the_period_bounds_the_work_measures_and_never_the_tickets(reader: TestClient) -> None:
    done = {days: {row["person"]["id"]: row["values"]["done_in_period"]["value"]
                   for row in scorecard(reader, "MJH", days)["people"]} for days in PERIODS}
    assert done[30] == {pid("pat"): 2, pid("quinn"): 2, pid("terry"): 2, pid("uma"): 0, pid("ghost"): 1}
    assert done[180] == {**done[90], pid("pat"): 3}
    for days in PERIODS:
        assert {row["person"]["id"]: row["tickets"] for row in scorecard(reader, "MJH", days)["people"]} == {
            pid(who): row.tickets for who, row in BY_AUTO_90.items()}


def test_someone_credited_for_nothing_is_not_on_the_board_and_an_unnamed_builder_is_named_safely(
    reader: TestClient,
) -> None:
    body = scorecard(reader, "MJH")
    ghost = rows_by_person(body)[pid("ghost")]
    assert ghost["person"] == {"id": pid("ghost"), "name": "Unnamed account", "avatar": None}
    # Sam was assigned in code review on several tickets and is credited for none of them.
    for board in ("MJH", "MJA", "MJT"):
        response = reader.get("/api/v1/people/scorecard",
                              params={"project": board, "level": "people", "person": pid("sam")})
        assert response.status_code == 404, board
    one = scorecard(reader, "MJH", person=pid("uma"))
    assert [row["person"]["name"] for row in one["people"]] == ["Uma Builder"]
    assert one["people"][0]["tickets"] == 1  # the Epic she is the Programmer of is no one's work


def test_comparison_off_still_shows_one_builder_and_the_board(tmp_path: Path, built: Path) -> None:
    shutil.copyfile(built, tmp_path / "oversight.db")
    client = serve(settings_for(tmp_path, write_config(tmp_path), people_comparison="off"))
    body = scorecard(client, "MJH", person=pid("pat"))
    assert body["comparison"] == "off"
    assert figures(body["people"][0]) == BY_AUTO_90["pat"]
    assert figures(body["board"]) == BOARD_90


def test_get_people_and_the_directory_count_each_persons_tickets_by_the_same_rule(reader: TestClient) -> None:
    for board, expected in (("MJH", BY_AUTO_90), ("MJA", BY_ASSIGNEE_90), ("MJT", BY_TESTERS_90)):
        people = {row["person"]["id"]: row for row in get(reader, "people", board)["people"] if row["person"]}
        credited = {key: (row["tickets"], row["open_tickets"]) for key, row in people.items() if row["tickets"]}
        assert credited == {pid(who): (row.tickets, row.open_tickets) for who, row in expected.items()}, board
        # Anyone else listed holds an open finding on a ticket assigned to them, and nothing counts for them.
        assert all(row["tickets"] == row["open_tickets"] == 0 and row["open_breaches"] + row["open_warnings"] > 0
                   for key, row in people.items() if key not in credited), board
        directory = {entry["person"]["id"]: entry["tickets"] for entry in get(reader, "people/directory", board)["people"]}
        assert directory == {**{pid(who): 0 for who in ASSIGNED_NOW}, **{pid(who): row.tickets for who, row in
                                                                         expected.items()}, pid("wes"): 0}, board


def test_findings_and_the_people_lists_open_findings_still_follow_whoever_holds_the_ticket_now(
    reader: TestClient,
) -> None:
    def findings(who: str) -> dict[str, Any]:
        return get(reader, "findings", "MJH", level="people", state="open", person=pid(who))

    riley = findings("riley")
    assert [(item["issue_key"], item["assignee"]["id"]) for item in riley["items"]] == [("MJH-20", pid("riley"))]
    facet = {entry["value"]: entry["count"] for entry in riley["facets"]["people"]}
    assert facet.get(pid("riley")) == 1 and pid("pat") not in facet
    assert findings("pat")["total"] == 0
    people = {row["person"]["id"]: row for row in get(reader, "people", "MJH")["people"] if row["person"]}
    assert [(people[pid(who)]["open_breaches"], people[pid(who)]["tickets_with_findings"]) for who in ("riley", "pat")] == [
        (1, 1), (0, 0)]


def test_the_person_card_agrees_with_the_scorecard_for_everyone_credited(reader: TestClient) -> None:
    for board, expected in (("MJH", BY_AUTO_90), ("MJA", BY_ASSIGNEE_90)):
        for who, row in expected.items():
            card = get(reader, f"people/{pid(who)}/card", board, level="people", days=90)
            assert card["membership"] == "holds_tickets", (board, who)
            assert card["work"] == {"open_tickets": row.open_tickets, "in_progress": row.in_progress,
                                    "done_in_period": row.done, "days": 90}, (board, who)


def ask_tools(settings: Settings, project: str) -> tuple[Any, Any]:
    ctx = replace(context(create_app(settings, provider=ScriptedProvider(), now=lambda: SAVED)), selected=project)
    conn = open_readonly(settings.db_file)
    scope = people_scope(conn, ctx)
    catalog = CatalogContext(process=ProcessConfig.load(settings.process_file), rules=ctx.rules, project=project,
                             now=SAVED, people=scope)

    def run(name: str, **params: Any) -> Facts:
        return PEOPLE_CATALOG[name].run(conn, validate_params(name, params, PEOPLE_CATALOG), catalog)

    return conn, run


IN_ALL = re.compile(r"; (\d+) in all$")


def expect_ask_agrees(client: TestClient, settings: Settings, project: str, days: int = 90) -> int:
    """Ask's people tools against GET /people, the directory and the scorecard; returns the people compared."""
    people = {row["person"]["id"]: row for row in get(client, "people", project)["people"]
              if row["person"] and row["tickets"]}
    directory = {entry["person"]["id"]: entry["tickets"] for entry in get(client, "people/directory", project)["people"]}
    card = rows_by_person(scorecard(client, project, days))
    conn, run = ask_tools(settings, project)
    try:
        listed = {row["person_id"]: row["tickets"] for row in run("oversight_people", rank_by="tickets", n=50).rows
                  if row["person_id"] is not None and row["tickets"]}
        assert listed == {key: row["tickets"] for key, row in people.items()}
        assert {row["person_id"]: row["tickets"] for row in run("oversight_board_people").rows} == directory
        for key, row in people.items():
            for state, expected in (("all", row["tickets"]), ("open", row["open_tickets"]),
                                    ("done", card[key]["values"]["done_in_period"]["value"])):
                facts = run("oversight_person_tickets", person=key, state=state, n=50,
                            **({"days": days} if state == "done" else {}))
                found = IN_ALL.search(facts.note)
                assert found is not None and int(found.group(1)) == expected, (key, state, facts.note)
                if state == "all":  # a person's tickets are no longer only those assigned to them now
                    assert "assigned to this person now" not in facts.note, facts.note
            assert card[key]["tickets"] == row["tickets"] == directory[key]
    finally:
        conn.close()
    return len(people)


def test_ask_counts_each_persons_tickets_as_the_people_page_does(reader: TestClient, built: Path,
                                                                 tmp_path: Path) -> None:
    shutil.copyfile(built, tmp_path / "oversight.db")
    settings = settings_for(tmp_path, write_config(tmp_path))
    client = serve(settings)
    assert expect_ask_agrees(client, settings, "MJH") == len(BY_AUTO_90)
    assert expect_ask_agrees(client, settings, "MJA") == len(BY_ASSIGNEE_90)


# --- D1a: the credit setting --------------------------------------------------------------------------------


def expected_options(days: int, board_fields: tuple[str, ...] = ("programmer", "testers")) -> list[dict[str, Any]]:
    _finished, options = COVERAGE[days]
    return [
        {"source": source, "field": {"key": key, "name": FIELD_NAMES[key]} if key else None,
         "source_filled": filled, "credited": credited}
        for source, key, filled, credited in options
        if key is None or key in board_fields
    ]


@pytest.mark.parametrize("days", PERIODS)
def test_each_choice_shows_its_coverage_of_the_finished_work_counted_by_hand(reader: TestClient, days: int) -> None:
    body = credit(reader, "MJH", days)
    assert body["period"] == {"days": days, "from": to_iso(T - days * DAY), "to": to_iso(T)}
    assert body["finished"] == COVERAGE[days][0]
    assert body["options"] == expected_options(days)
    setting = body["setting"]
    assert (setting["source"], setting["field"], setting["changed_by"], setting["changed_at"]) == (
        "auto", None, None, None)
    assert isinstance(setting["revision"], int) and not isinstance(setting["revision"], bool)
    assert body["effective_field"] == {"key": "programmer", "name": "Programmer"}
    assert "acc-" not in str(body)


def test_the_starting_choice_of_each_board_is_its_process_yaml_credit(reader: TestClient) -> None:
    expected = {
        "MJH": ({"source": "auto", "field": None}, {"key": "programmer", "name": "Programmer"}),
        "MJF": ({"source": "field", "field": {"key": "programmer", "name": "Programmer"}},
                {"key": "programmer", "name": "Programmer"}),
        "MJA": ({"source": "assignee", "field": None}, None),
        "MJT": ({"source": "field", "field": {"key": "testers", "name": "Testers"}}, {"key": "testers", "name": "Testers"}),
    }
    for board, (setting, effective) in expected.items():
        body = credit(reader, board)
        assert {"source": body["setting"]["source"], "field": body["setting"]["field"]} == setting, board
        assert (body["setting"]["changed_by"], body["setting"]["changed_at"]) == (None, None), board
        assert body["effective_field"] == effective, board
        # The coverage is the board's, whichever choice applies.
        assert (body["finished"], body["options"]) == (COVERAGE[90][0], expected_options(90)), board
        assert "history" not in body, board


def test_only_people_fields_filled_on_the_board_are_offered(reader: TestClient) -> None:
    held = credit(reader, "HLD")
    # Neither HLD ticket fills a people field: Automatic and Assignee alone.
    assert [(option["source"], option["field"]) for option in held["options"]] == [("auto", None), ("assignee", None)]
    assert held["finished"] == 2
    assert [option["source_filled"] for option in held["options"]] == [0, 0]
    assert [option["credited"] for option in held["options"]] == [0, 0]  # no one is assigned either now


def test_with_no_finished_work_every_coverage_is_zero_and_a_field_filled_on_open_work_is_offered(
    reader: TestClient,
) -> None:
    body = credit(reader, "OPN")
    assert body["finished"] == 0
    assert body["options"] == [
        {"source": "auto", "field": None, "source_filled": 0, "credited": 0},
        {"source": "assignee", "field": None, "source_filled": 0, "credited": 0},
        {"source": "field", "field": {"key": "programmer", "name": "Programmer"}, "source_filled": 0, "credited": 0},
    ]
    card = scorecard(reader, "OPN")
    # Its Programmer is Pat: the open ticket counts for Pat, though it is assigned to Riley.
    assert [(row["person"]["name"], figures(row)) for row in card["people"]] == [
        ("Pat Programmer", Row(1, 1, 1, 0, (None, None)))]


def test_two_admins_saving_at_once_make_one_change_and_get_one_refusal(snapshot: Settings) -> None:
    clients = [serve(snapshot), serve(snapshot, viewer="f" * 32, name="Robin Ellis")]
    revision = credit(clients[0], "MJH")["setting"]["revision"]
    together = threading.Barrier(2)
    answers: list[int] = []

    def send(client: TestClient, source: str, field: str | None) -> None:
        together.wait()
        answers.append(put_credit(client, "MJH", source, field, revision).status_code)

    threads = [threading.Thread(target=send, args=(clients[0], "assignee", None)),
               threading.Thread(target=send, args=(clients[1], "field", "testers"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted(answers) == [200, 409]
    after = credit(clients[0], "MJH")["setting"]
    assert after["revision"] == revision + 1 and after["source"] in ("assignee", "field")
    assert query(snapshot, "SELECT COUNT(*) FROM people_credit") == [(1,)]


def test_saving_a_choice_changes_every_figure_at_once_without_a_rules_run_and_going_back_restores_it(
    snapshot: Settings,
) -> None:
    client = serve(snapshot)
    runs_before = len(query(snapshot, "SELECT * FROM runs"))
    findings_before = query(snapshot, "SELECT * FROM findings ORDER BY finding_id")
    before = credit(client, "MJH")
    revision = before["setting"]["revision"]

    saved = put_credit(client, "MJH", "assignee", None, revision)
    assert saved.status_code == 200, saved.text
    assert saved.headers["cache-control"] == "no-store"
    body = saved.json()
    assert set(body) >= {"meta", "period", "setting", "effective_field", "finished", "options"}
    assert "history" not in body
    assert body["setting"]["source"] == "assignee" and body["setting"]["field"] is None
    assert body["setting"]["revision"] > revision
    assert (body["setting"]["changed_by"], body["setting"]["changed_at"]) == (ADMIN_NAME, to_iso(SAVED))
    assert body["effective_field"] is None
    assert (body["finished"], body["options"]) == (before["finished"], before["options"])
    assert credit(client, "MJH")["setting"] == body["setting"]

    expect_scorecard(scorecard(client, "MJH"), BY_ASSIGNEE_90, ASSIGNEE_ORDER)
    people = {row["person"]["id"]: row["tickets"] for row in get(client, "people", "MJH")["people"] if row["person"]}
    assert people == {pid(who): row.tickets for who, row in BY_ASSIGNEE_90.items()}
    card = get(client, f"people/{pid('riley')}/card", "MJH", level="people", days=90)
    assert card["work"]["done_in_period"] == BY_ASSIGNEE_90["riley"].done
    # No rules ran and no finding moved; another board keeps its own choice and figures.
    assert len(query(snapshot, "SELECT * FROM runs")) == runs_before
    assert query(snapshot, "SELECT * FROM findings ORDER BY finding_id") == findings_before
    expect_scorecard(scorecard(client, "MJF"), BY_PROGRAMMER_90, AUTO_ORDER)
    assert credit(client, "MJF")["setting"]["changed_by"] is None

    back = put_credit(client, "MJH", "auto", None, body["setting"]["revision"])
    assert back.status_code == 200, back.text
    after = back.json()
    assert (after["setting"]["source"], after["setting"]["field"]) == ("auto", None)
    assert after["setting"]["revision"] == body["setting"]["revision"] + 1
    expect_scorecard(scorecard(client, "MJH"), BY_AUTO_90, AUTO_ORDER)


def test_a_chosen_field_reads_that_field_alone(snapshot: Settings) -> None:
    client = serve(snapshot)
    body = choose(client, "MJH", "field", "testers")
    assert body["setting"]["field"] == {"key": "testers", "name": "Testers"}
    assert body["effective_field"] == {"key": "testers", "name": "Testers"}
    expect_scorecard(scorecard(client, "MJH"), BY_TESTERS_90, TESTERS_ORDER)
    back = choose(client, "MJT", "field", "programmer")
    assert back["effective_field"] == {"key": "programmer", "name": "Programmer"}
    expect_scorecard(scorecard(client, "MJT"), BY_PROGRAMMER_90, AUTO_ORDER)


def test_an_admins_choice_overrides_the_files(snapshot: Settings) -> None:
    client = serve(snapshot)
    body = choose(client, "MJA", "auto")
    assert (body["setting"]["source"], body["setting"]["changed_by"]) == ("auto", ADMIN_NAME)
    expect_scorecard(scorecard(client, "MJA"), BY_AUTO_90, AUTO_ORDER)


def test_a_save_against_a_revision_the_board_has_left_is_refused(snapshot: Settings) -> None:
    client = serve(snapshot)
    revision = credit(client, "MJH")["setting"]["revision"]
    assert put_credit(client, "MJH", "assignee", None, revision).status_code == 200
    stale = put_credit(client, "MJH", "field", "testers", revision)
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision"
    ahead = put_credit(client, "MJH", "auto", None, revision + 50)
    assert (ahead.status_code, ahead.json()["code"]) == (409, "stale_revision")
    body = credit(client, "MJH")
    assert body["setting"]["source"] == "assignee"


@pytest.mark.parametrize(
    "sent",
    [
        {"source": "programmer", "field": None},
        {"source": "", "field": None},
        {"source": "field", "field": None},
        {"source": "field", "field": "code_reviewers"},  # a people field no ticket on the board fills
        {"source": "field", "field": "story_points"},  # not a people field
        {"source": "field", "field": "nope"},
        {"source": "assignee", "field": "programmer"},
        {"source": "auto", "field": "programmer"},
        {"source": "field", "field": {"key": "programmer"}},
    ],
)
def test_a_choice_that_is_not_one_of_the_boards_is_refused_and_nothing_is_saved(snapshot: Settings,
                                                                               sent: dict[str, Any]) -> None:
    client = serve(snapshot)
    before = credit(client, "MJH")
    response = client.put("/api/v1/people/credit", params={"project": "MJH", "level": "people"},
                          json={**sent, "revision": before["setting"]["revision"]})
    assert response.status_code == 422, response.text
    assert credit(client, "MJH")["setting"] == before["setting"]
    assert query(snapshot, "SELECT COUNT(*) FROM people_credit") == [(0,)]


def test_a_body_without_a_revision_or_a_choice_is_refused(snapshot: Settings) -> None:
    client = serve(snapshot)
    for body in ({"source": "assignee", "field": None}, {"field": None, "revision": 0}, [], "assignee"):
        response = client.put("/api/v1/people/credit", params={"project": "MJH", "level": "people"}, json=body)
        assert response.status_code == 422, (body, response.text)
    assert query(snapshot, "SELECT COUNT(*) FROM people_credit") == [(0,)]


def test_the_setting_is_for_people_level_only_and_names_who_changed_it(snapshot: Settings) -> None:
    client = serve(snapshot)
    for level in ({}, {"level": "team"}):
        read = client.get("/api/v1/people/credit", params={"project": "MJH", **level})
        assert (read.status_code, read.json()["code"]) == (403, "people_level_only")
        sent = client.put("/api/v1/people/credit", params={"project": "MJH", **level},
                          json={"source": "assignee", "field": None, "revision": 0})
        assert (sent.status_code, sent.json()["code"]) == (403, "people_level_only")
    nameless = serve(snapshot, viewer=None, name=None)
    revision = credit(client, "MJH")["setting"]["revision"]
    assert put_credit(nameless, "MJH", "assignee", None, revision).status_code == 422
    assert nameless.get("/api/v1/people/credit", params={"project": "MJH", "level": "people"}).status_code == 422
    assert query(snapshot, "SELECT COUNT(*) FROM people_credit") == [(0,)]

    local = TestClient(client.app, headers={"X-Service-Key": KEY, **VIEWER_HEADERS})
    assert choose(local, "MJH", "assignee")["setting"]["changed_by"] == "Local (sign-in off)"
    by_email = serve(snapshot, name="lead@example.org")
    assert choose(by_email, "MJH", "auto")["setting"]["changed_by"] == "Signed-in user"
    assert "@" not in str(credit(client, "MJH"))


@pytest.mark.parametrize(("query_string", "status"),
                         [("days=60", 422), ("days=ninety", 422), ("days=90&days=30", 422), ("project=NOPE", 404)])
def test_bad_parameters_are_refused(reader: TestClient, query_string: str, status: int) -> None:
    project = "" if "project" in query_string else "project=MJH&"
    response = reader.get(f"/api/v1/people/credit?{project}level=people&{query_string}")
    assert response.status_code == status, response.text


def test_a_site_without_a_programmer_field_skips_step_one_on_automatic(built: Path, tmp_path: Path) -> None:
    shutil.copyfile(built, tmp_path / "oversight.db")
    fields = yaml.safe_load((CONFIG_FIXTURES / "fields.yaml").read_text(encoding="utf-8"))
    fields["fields"]["programmer"] = None
    client = serve(settings_for(tmp_path, write_config(tmp_path, {"MJH": None}, fields), projects="MJH"))
    body = credit(client, "MJH")
    assert body["effective_field"] is None
    assert [(option["source"], choice(option)[1]) for option in body["options"]] == [
        ("auto", None), ("assignee", None), ("field", "testers")]
    expect_scorecard(scorecard(client, "MJH"), BY_ASSIGNEE_90, ASSIGNEE_ORDER)


def test_a_user_picker_field_is_a_people_field_too(built: Path, tmp_path: Path) -> None:
    shutil.copyfile(built, tmp_path / "oversight.db")
    fields = yaml.safe_load((CONFIG_FIXTURES / "fields.yaml").read_text(encoding="utf-8"))
    fields["fields"]["testers"]["custom_type"] = "com.atlassian.jira.plugin.system.customfieldtypes:userpicker"
    client = serve(settings_for(tmp_path, write_config(tmp_path, {"UPK": CREDIT_FILE["UPK"]}, fields), projects="UPK"))
    body = credit(client, "UPK")
    assert body["options"][-1] == {"source": "field", "field": {"key": "testers", "name": "Testers"},
                                   "source_filled": 1, "credited": 1}
    assert [(row["person"]["name"], row["values"]["done_in_period"]["value"])
            for row in scorecard(client, "UPK")["people"]] == [("Vic Tester", 1)]


# --- process.yaml -------------------------------------------------------------------------------------------


def test_the_repos_process_yaml_starts_mjh_dashboard_on_programmer_and_ailed_on_the_assignee(tmp_path: Path) -> None:
    config = PROJECT_ROOT / "config"
    projects = yaml.safe_load((config / "process.yaml").read_text(encoding="utf-8"))["projects"]
    assert projects["MJHDASH"]["credit"] == {"source": "field", "field": "programmer"}
    assert projects["AIL"]["credit"] == {"source": "assignee"}
    for key, project in projects.items():
        if key not in ("MJHDASH", "AIL"):
            assert project.get("credit", {"source": "auto"}) == {"source": "auto"}, key
    ProcessConfig.load(config / "process.yaml")
    create_app(Settings(_env_file=None, jira_projects="DEMO", api_service_key=KEY, db_path=tmp_path / "oversight.db",
                        config_dir=config, reports_dir=tmp_path / "reports", board_discovery="off"),
               provider=ScriptedProvider())


@pytest.mark.parametrize(
    "setting",
    [{"source": "field"}, {"source": "auto", "field": "programmer"}, {"source": "assignee", "field": "testers"},
     {"source": "nobody"}, {"source": "field", "field": None}],
)
def test_process_yaml_refuses_a_credit_choice_that_does_not_hold_together(tmp_path: Path,
                                                                         setting: dict[str, Any]) -> None:
    config = write_config(tmp_path, {"MJH": setting})
    with pytest.raises(ConfigError):
        ProcessConfig.load(config / "process.yaml")


@pytest.mark.parametrize("field", ["story_points", "sprint", "development", "nope"])
def test_the_service_will_not_start_on_a_credit_field_that_is_not_a_people_field(tmp_path: Path, field: str) -> None:
    config = write_config(tmp_path, {"MJH": {"source": "field", "field": field}})
    with pytest.raises(ConfigError):
        create_app(settings_for(tmp_path, config, projects="MJH"), provider=ScriptedProvider())


@pytest.mark.parametrize("setting", [None, {"source": "auto"}, {"source": "assignee"},
                                     {"source": "field", "field": "test_reviewers"}])
def test_process_yaml_takes_each_choice_that_holds_together(tmp_path: Path, setting: dict[str, Any] | None) -> None:
    config = write_config(tmp_path, {"MJH": setting})
    ProcessConfig.load(config / "process.yaml")
    create_app(settings_for(tmp_path, config, projects="MJH"), provider=ScriptedProvider())


# --- ingest: the people in the people fields ----------------------------------------------------------------

PICTURE_HOST = "https://avatar-management--avatars.us-west-2.prod.public.atl-paas.net"
PNG = b"\x89PNG\r\n\x1a\n" + b"a programmer's picture"


class PeopleFieldsJira(FakeJira):
    """The fake site with people in some tickets' people fields, which FakeJira leaves empty."""

    def __init__(self) -> None:
        super().__init__()
        self.people_fields: dict[str, dict[str, Any]] = {}

    def payload(self, issue: FakeIssue, fields: list[str] | None = None) -> dict[str, Any]:
        body = super().payload(issue, fields)
        for field_id, value in self.people_fields.get(issue.key, {}).items():
            if fields is None or field_id in fields:
                body["fields"][field_id] = value
        return body


def site_user(account_id: str, name: str, picture: str | None = None) -> dict[str, Any]:
    user: dict[str, Any] = {"self": f"https://fake.atlassian.net/rest/api/3/user?accountId={account_id}",
                            "accountId": account_id, "accountType": "atlassian", "displayName": name, "active": True,
                            "timeZone": "Europe/Berlin"}
    if picture is not None:
        user["avatarUrls"] = {size: picture for size in ("16x16", "24x24", "32x32", "48x48")}
    return user


def picture_host(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    if request.url == httpx.URL(f"{PICTURE_HOST}/acc-pia/48"):
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})
    return httpx.Response(404)


def synced(tmp_path: Path, fields: FieldsConfig, people: dict[str, Any]) -> Settings:
    fake = PeopleFieldsJira()
    fake.add_project("AIL")
    issue = fake.create("AIL", "Task")
    fake.transition(issue.key, "In Progress")
    fake.transition(issue.key, "Done")
    fake.people_fields[issue.key] = people
    settings = Settings(_env_file=None, jira_projects="AIL", api_service_key=KEY, db_path=tmp_path / "oversight.db",
                        config_dir=CONFIG_FIXTURES, reports_dir=tmp_path / "reports", board_discovery="off")
    with Store.open(settings.db_file) as store, PictureFetcher(transport=httpx.MockTransport(picture_host)) as fetcher:
        run_sync(fake.client(), store, fields, projects=["AIL"], mode="full", now=lambda: fake.clock, pictures=fetcher)
        run_derive(store, ProcessConfig.load(settings.process_file), projects=["AIL"], now=lambda: fake.clock)
    return settings


def test_a_sync_keeps_the_programmers_name_and_picture_and_the_board_shows_them(tmp_path: Path,
                                                                                fields: FieldsConfig) -> None:
    pia = site_user("acc-pia", "Pia Programmer", f"{PICTURE_HOST}/acc-pia/48")
    settings = synced(tmp_path, fields, {fields.id_of("programmer"): [pia]})
    (row,) = query(settings, "SELECT display_name, avatar_hash FROM people WHERE account_id = 'acc-pia'")
    version = hashlib.sha256(PNG).hexdigest()[:12]
    assert row == ("Pia Programmer", version)
    client = serve(settings)
    who = {"id": "p" + hashlib.sha256(b"acc-pia").hexdigest()[:12], "name": "Pia Programmer", "avatar": version}
    body = scorecard(client, "AIL")
    assert [row["person"] for row in body["people"]] == [who]
    assert body["people"][0]["values"]["done_in_period"]["value"] == 1
    picture = client.get(f"/api/v1/people/{who['id']}/avatar", params={"project": "AIL"})
    assert (picture.status_code, picture.content, picture.headers["content-type"]) == (200, PNG, "image/png")
    assert "acc-pia" not in str(scorecard(client, "AIL", days=180))


def test_a_sync_names_the_people_of_every_people_field_an_admin_may_choose(tmp_path: Path,
                                                                           fields: FieldsConfig) -> None:
    tess = site_user("acc-tess", "Tess Tester")
    settings = synced(tmp_path, fields, {fields.id_of("testers"): [tess]})
    assert query(settings, "SELECT display_name FROM people WHERE account_id = 'acc-tess'") == [("Tess Tester",)]


# --- DEMO: the credit rule gives today's figures ------------------------------------------------------------

DEMO_NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)  # the seed's clock


@pytest.fixture(scope="module")
def demo_seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("credit_demo")
    settings = settings_for(root, CONFIG_FIXTURES, projects="DEMO")
    _seed(settings, briefing=False)
    return settings.db_file


@pytest.fixture
def demo(demo_seeded: Path, tmp_path: Path) -> Settings:
    for suffix in ("", "-wal"):
        source = Path(f"{demo_seeded}{suffix}")
        if source.exists():
            shutil.copyfile(source, Path(f"{tmp_path / 'oversight.db'}{suffix}"))
    return settings_for(tmp_path, CONFIG_FIXTURES, projects="DEMO")


def demo_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, provider=ScriptedProvider(), now=lambda: DEMO_NOW), headers=headers())


DEMO_WORK_SQL = """
    SELECT i.assignee_id, i.status_category, i.time_spent_s, i.aggregate_time_spent_s, m.done_at, m.reopen_count,
           m.skipped_stages
    FROM issues AS i LEFT JOIN issue_metrics AS m ON m.issue_key = i.issue_key
    WHERE i.project_key = 'DEMO' AND COALESCE(i.issue_type, '') != 'Epic'
"""


def by_assignee(settings: Settings, since: str, until: str) -> dict[str | None, dict[str, Any]]:
    """Each assignee's figures as they were counted before credit: by whom each ticket is assigned to now."""
    found: dict[str | None, dict[str, Any]] = {}
    for account_id, category, spent, below, done_at, reopens, skips in query(settings, DEMO_WORK_SQL):
        key = "p" + hashlib.sha256(account_id.encode()).hexdigest()[:12] if account_id else None
        tally = found.setdefault(key, {"tickets": 0, "open": 0, "in_progress": 0, "done": 0, "logged": 0,
                                       "reopened": 0, "skipped": 0})
        tally["tickets"] += 1
        tally["open"] += category != "done"
        tally["in_progress"] += category == "indeterminate"
        if done_at is not None and since <= done_at <= until:
            tally["done"] += 1
            tally["logged"] += (spent or 0) > 0 or (below or 0) > 0
            tally["reopened"] += (reopens or 0) > 0
            tally["skipped"] += skips not in (None, "", "[]")
    return found


def test_on_demo_every_persons_figures_are_still_by_the_current_assignee(demo: Settings) -> None:
    client = demo_client(demo)
    for days in PERIODS:
        body = scorecard(client, "DEMO", days)
        assert body["basis"] == "credit"
        expected = by_assignee(demo, body["period"]["from"], body["period"]["to"])
        assert {row["person"]["id"] for row in body["people"]} == {key for key in expected if key is not None}
        for row in body["people"]:
            tally, values = expected[row["person"]["id"]], row["values"]
            assert row["tickets"] == tally["tickets"]
            assert (values["open_tickets"]["value"], values["in_progress"]["value"],
                    values["done_in_period"]["value"]) == (tally["open"], tally["in_progress"], tally["done"])
            for key, counted in (("time_logged", "logged"), ("reopened", "reopened"), ("skipped_stage", "skipped")):
                if values[key]["denominator"] is not None:
                    assert (values[key]["numerator"], values[key]["denominator"]) == (tally[counted], tally["done"])
        assert body["board"]["tickets"] == sum(tally["tickets"] for tally in expected.values())


def test_on_demo_people_the_directory_the_scorecard_the_card_and_ask_agree(demo: Settings) -> None:
    client = demo_client(demo)
    compared = expect_ask_agrees(client, demo, "DEMO")
    assert compared >= 5
    card = rows_by_person(scorecard(client, "DEMO"))
    for key, row in card.items():
        shown = get(client, f"people/{key}/card", "DEMO", level="people", days=90)
        values = row["values"]
        assert shown["work"] == {"open_tickets": values["open_tickets"]["value"],
                                 "in_progress": values["in_progress"]["value"],
                                 "done_in_period": values["done_in_period"]["value"], "days": 90}


def test_on_demo_the_choice_changes_no_figure_and_coverage_is_counted_by_hand(demo: Settings) -> None:
    client = demo_client(demo)
    before = {days: scorecard(client, "DEMO", days) for days in PERIODS}
    people_before = get(client, "people", "DEMO")["people"]
    for days in PERIODS:
        body = credit(client, "DEMO", days)
        since, until = body["period"]["from"], body["period"]["to"]
        finished = query(demo, "SELECT i.assignee_id FROM issues AS i JOIN issue_metrics AS m USING (issue_key)"
                               " WHERE i.project_key = 'DEMO' AND COALESCE(i.issue_type, '') != 'Epic'"
                               " AND m.done_at BETWEEN ? AND ?", (since, until))
        assigned = sum(account_id is not None for (account_id,) in finished)
        assert body["finished"] == len(finished)
        assert body["setting"]["source"] == "auto"
        assert body["options"] == [
            {"source": "auto", "field": None, "source_filled": 0, "credited": assigned},
            {"source": "assignee", "field": None, "source_filled": assigned, "credited": assigned},
        ]
    choose(client, "DEMO", "assignee")
    for days in PERIODS:
        after = scorecard(client, "DEMO", days)
        assert (after["board"], after["people"]) == (before[days]["board"], before[days]["people"])
    assert get(client, "people", "DEMO")["people"] == people_before


# --- the proof shows why each ticket counts for whom -----------------------------------------------------------


def proof(client: TestClient, board: str, measure: str, person: str | None = None) -> dict[str, Any]:
    params = {"project": board, "level": "people", "days": 90, "measure": measure}
    if person is not None:
        params["person"] = person
    response = client.get("/api/v1/people/scorecard/tickets", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_the_proof_says_whom_each_finished_ticket_counts_for_and_why(reader: TestClient) -> None:
    whole = {ticket["key"]: ticket["credit"] for ticket in proof(reader, "MJH", "done_in_period")["tickets"]}
    for n, credits in AUTO.items():
        key = f"MJH-{n}"
        if key not in whole or len(credits) != 1:
            continue
        ((who, basis),) = credits
        assert whole[key] == {"person": {"id": pid(who), "name": NAMES.get(who, "Unnamed account"), "avatar": None},
                              "basis": basis, "field": "Programmer" if basis == F else None}, key
    for number in (3, 5, 9):  # no Programmer and no one assigned now: they count for no one
        assert whole[f"MJH-{number}"] == {"person": None, "basis": None, "field": None}
    for who in ("pat", "quinn", "terry"):
        listed = proof(reader, "MJH", "done_in_period", pid(who))["tickets"]
        expected = {f"MJH-{n}": basis for n, credits in AUTO.items() for name, basis in credits
                    if name == who and f"MJH-{n}" in whole}
        assert {ticket["key"]: ticket["credit"]["basis"] for ticket in listed} == expected, who
