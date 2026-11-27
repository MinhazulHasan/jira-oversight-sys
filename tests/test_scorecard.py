from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from oversight.api.app import create_app
from oversight.api.scorecard import (
    COMPARABLE,
    MEASURES,
    MIN_TICKETS,
    NO_CODE_REVIEW,
    NO_EFFORT_LIMIT,
    NO_ESTIMATES,
    NO_STAGES_TO_SKIP,
    NO_TIME_LOGGED,
    RULE_OFF,
    RULES_OFF,
    Board,
    Measure,
    Value,
    percent,
    vs_board,
)
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.settings import Change, load_board_settings, save_board_settings
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES, VIEWER_HEADERS

T = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
S = T - timedelta(days=90)
MS = timedelta(milliseconds=1)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
KEY = "scorecard-key"
NAMES = {"alpha": "Alpha Tester", "bravo": "Bravo Tester", "mike": "Mike Member", "zed": "Zed Tester"}
ACCOUNTS = {who: f"acc-{who}" for who in NAMES}
LABELS = {measure.key: measure.label for measure in MEASURES}
RULES = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")


def pid(who: str) -> str:
    return "p" + hashlib.sha256(ACCOUNTS[who].encode()).hexdigest()[:12]


@dataclass
class Ticket:
    key: str
    holder: str | None
    status: str
    started: datetime | None = None
    done: datetime | None = None
    set_at: datetime | None = None
    spent: int = 0
    below: int = 0
    ratio: float | None = None
    reopens: int = 0
    skipped: bool = False
    testing: bool = False
    kind: str = "Task"
    findings: list[tuple[str, str | None]] = field(default_factory=list)


CATEGORY = {"To Do": "new", "In Progress": "indeterminate", "Code Review": "indeterminate", "QA": "indeterminate",
            "Done": "done"}

ALPHA = [
    Ticket("REV-1", "alpha", "Done", started=S - MS, done=S, set_at=S - 2 * DAY, spent=3600, ratio=1.0, testing=True),
    Ticket("REV-2", "alpha", "Done", started=T - 2 * DAY, done=T, set_at=T - 2 * DAY, spent=3600, ratio=1.5, reopens=1,
           testing=True, findings=[("R-FLOW-01", None)]),
    Ticket("REV-3", "alpha", "Done", started=T - 12 * DAY, done=T - 10 * DAY, set_at=T - 12 * DAY + timedelta(minutes=30),
           spent=7200, ratio=0.8, skipped=True, testing=True, findings=[("R-FLOW-01", "cleared")]),
    Ticket("REV-4", "alpha", "Done", started=S, done=T - 20 * DAY, set_at=S + 2 * HOUR, below=1800, ratio=1.2,
           findings=[("R-EST-02", None)]),
    Ticket("REV-5", "alpha", "Done", started=T - 31 * DAY, done=T - 30 * DAY, testing=True,
           findings=[("R-FLOW-01", "aged_out")]),
    Ticket("REV-6", "alpha", "Done", started=S - 2 * DAY, done=S - MS, set_at=S - 3 * DAY, spent=3600, ratio=1.0),
    Ticket("REV-7", "alpha", "Done", started=T - DAY, done=T + MS, set_at=T - 2 * DAY, spent=3600, ratio=1.0),
    Ticket("REV-8", "alpha", "Done", done=T - 5 * DAY, kind="Epic", findings=[("R-EST-01", None)]),
    Ticket("REV-9", "alpha", "In Progress", started=T - 3 * DAY, set_at=T - 4 * DAY,
           findings=[("W-AGE-01", None), ("W-EFF-01", None)]),
    Ticket("REV-10", "alpha", "To Do", findings=[("W-EST-01", None), ("W-AGE-01", "cleared")]),
    Ticket("REV-11", "alpha", "Code Review", started=T - DAY, findings=[("R-EST-01", None)]),
]
OTHERS = [
    Ticket(f"REV-{20 + n}", "bravo" if n < 5 else None, "Done", started=T - (n + 2) * DAY, done=T - (n + 1) * DAY,
           set_at=T - (n + 3) * DAY if n < 6 else None, spent=3600 if n < 14 else 0,
           ratio=1.0 if n in (0, 1, 2, 3) else None, reopens=1 if n in (5, 6) else 0, skipped=n == 7,
           testing=n in (8, 9, 10, 11))
    for n in range(15)
] + [Ticket("REV-40", "bravo", "To Do"), Ticket("REV-41", None, "To Do"), Ticket("REV-42", None, "To Do")]
FLAT = [Ticket("FLAT-1", "zed", "Done", started=T - 3 * DAY, done=T - DAY, set_at=T - 4 * DAY, spent=3600, ratio=1.0),
        Ticket("FLAT-2", "zed", "In Progress", started=T - 2 * DAY)]
HOURS_BOARD = [Ticket("HOURS-1", "zed", "Done", started=T - 3 * DAY, done=T - DAY, set_at=T - 4 * DAY, spent=3600,
                      ratio=1.0)]
SECRETS = [*ACCOUNTS.values(), "acc-", "@example.invalid", "avatar_url", "Europe/London"]

PROCESS = {
    "process_version": "0.1.0-scorecard",
    "stages": ["backlog", "estimate", "development", "code_review", "dev_signoff", "testing", "test_review",
               "test_signoff", "done"],
    "start_stage": "development",
    "done_stage": "done",
    "projects": {
        "REV": {
            "name": "Review board",
            "status_to_stage": {"To Do": "backlog", "In Progress": "development", "Code Review": "code_review",
                                "QA": "testing", "Done": "done"},
            "estimate": {"basis": "points", "field": "story_points", "hours_per_point": 8},
            "evidence": {"code_review": {"status": "Code Review"}, "dev_signoff": None, "testing": {"status": "QA"},
                         "test_review": None, "test_signoff": None},
        },
        "FLAT": {
            "status_to_stage": {"To Do": "backlog", "In Progress": "development", "Done": "done"},
            "estimate": {"basis": "points", "field": "story_points", "hours_per_point": 8},
            "evidence": {"code_review": None, "dev_signoff": None, "testing": None, "test_review": None,
                         "test_signoff": None},
        },
        "HOURS": {
            "status_to_stage": {"To Do": "backlog", "In Progress": "development", "Done": "done"},
            "estimate": {"basis": "hours", "field": "original_estimate"},
            "evidence": {"code_review": None},
        },
    },
}


def write_config(root: Path, *, story_points: bool = True, rules: dict[str, Any] | None = None) -> Path:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "process.yaml").write_text(yaml.safe_dump(PROCESS), encoding="utf-8")
    fields = yaml.safe_load((CONFIG_FIXTURES / "fields.yaml").read_text(encoding="utf-8"))
    if not story_points:
        fields["fields"]["story_points"] = None
    (config / "fields.yaml").write_text(yaml.safe_dump(fields), encoding="utf-8")
    catalogue = rules or yaml.safe_load((CONFIG_FIXTURES / "rules.yaml").read_text(encoding="utf-8"))
    (config / "rules.yaml").write_text(yaml.safe_dump(catalogue), encoding="utf-8")
    return config


def build(path: Path) -> None:
    with Store.open(path) as store, store.transaction():
        for board in ("REV", "FLAT", "HOURS"):
            for n, (name, category) in enumerate(CATEGORY.items()):
                store.upsert("statuses", {"project_key": board, "status_id": str(10 + n), "name": name,
                                          "category": category})
            store.set_cursor(board, last_synced_at=to_iso(T), run_id="r1", account_timezone="Europe/London")
        for who, name in NAMES.items():
            store.upsert("people", {"account_id": ACCOUNTS[who], "display_name": name, "active": 1,
                                    "updated_at": to_iso(T)})
        for who in ("alpha", "mike"):
            store.upsert("board_members", {"project_key": "REV", "account_id": ACCOUNTS[who], "seen_at": to_iso(T)})
        for n, ticket in enumerate([*ALPHA, *OTHERS, *FLAT, *HOURS_BOARD]):
            board = ticket.key.split("-")[0]
            store.upsert("issues", {
                "issue_key": ticket.key, "issue_id": str(1000 + n), "project_key": board, "issue_type": ticket.kind,
                "summary": f"Sample {n}", "status": ticket.status, "status_category": CATEGORY[ticket.status],
                "assignee_id": ACCOUNTS[ticket.holder] if ticket.holder else None, "created_at": to_iso(S - 30 * DAY),
                "updated_at": to_iso(T - DAY), "time_spent_s": ticket.spent or None,
                "aggregate_time_spent_s": (ticket.spent + ticket.below) or None,
                "story_points": 3.0 if ticket.set_at else None,
            })
            store.upsert("issue_metrics", {
                "issue_key": ticket.key, "started_at": to_iso(ticket.started), "done_at": to_iso(ticket.done),
                "reopen_count": ticket.reopens,
                "skipped_stages": [{"changelog_id": "1", "from": "In Progress", "to": "Done", "skipped": ["testing"]}]
                if ticket.skipped else [],
                "estimate_basis": "points" if ticket.set_at else "none", "estimate_set_at": to_iso(ticket.set_at),
                "effort_ratio": ticket.ratio, "computed_at": to_iso(T),
            })
            if ticket.testing:
                store.conn.execute(
                    "INSERT INTO status_intervals (issue_key, seq, status, stage, entered_at, left_at, hours)"
                    " VALUES (?, 1, 'QA', 'testing', ?, ?, 24.0)",
                    (ticket.key, to_iso(T - 40 * DAY), to_iso(T - 39 * DAY)),
                )
            for m, (rule_id, why) in enumerate(ticket.findings):
                finding(store, f"{ticket.key}-{m}", rule_id, ticket.key, board, why)
        finding(store, "sprint", "W-SPR-01", "sprint:7", "REV", None)
        finding(store, "gone", "R-EST-01", "REV-999", "REV", None)
        store.upsert("worklogs", {"worklog_id": "1", "issue_key": "REV-1", "started_at": to_iso(T - 10 * DAY),
                                  "seconds": 3600, "author_id": "app-tempo", "updated_at": to_iso(T - 10 * DAY)})
        store.upsert("worklogs", {"worklog_id": "2", "issue_key": "HOURS-1", "started_at": to_iso(S - DAY),
                                  "seconds": 3600, "author_id": "app-tempo", "updated_at": to_iso(S - DAY)})


def finding(store: Store, name: str, rule_id: str, issue_key: str, board: str, why: str | None) -> None:
    rule = RULES.by_id(rule_id)
    store.upsert("findings", {
        "finding_id": hashlib.sha256(name.encode()).hexdigest()[:32], "rule_id": rule_id, "rule_version": RULES.version,
        "issue_key": issue_key, "project_key": board, "kind": rule.kind, "severity": rule.severity,
        "evidence_key": name, "message": rule.title, "evidence_json": {}, "first_seen_at": to_iso(T - 5 * DAY),
        "last_seen_at": to_iso(T), "resolved_at": None if why is None else to_iso(T - DAY), "resolved_why": why,
        "run_id": "r1",
    })


def serve(root: Path, config: Path, **settings: Any) -> TestClient:
    configured = Settings(_env_file=None, jira_projects="REV,FLAT,HOURS", api_service_key=KEY,
                          db_path=root / "oversight.db", config_dir=config, reports_dir=root / "reports", **settings)
    return TestClient(create_app(configured, provider=ScriptedProvider(), now=lambda: T + HOUR),
                      headers={"X-Service-Key": KEY, **VIEWER_HEADERS})


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("scorecard")
    build(path / "oversight.db")
    return path


@pytest.fixture(scope="module")
def client(root: Path) -> Iterator[TestClient]:
    with serve(root, write_config(root)) as served:
        yield served


def scorecard(client: TestClient, params: Any = None, board: str = "REV") -> dict[str, Any]:
    response = client.get("/api/v1/people/scorecard", params=[("project", board), ("level", "people"), *(params or [])])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def rate(numerator: int, denominator: int, enough: bool, vs: str | None) -> Value:
    return {"value": percent(numerator, denominator), "numerator": numerator, "denominator": denominator,
            "enough": enough, "vs_board": vs}


def count(n: int) -> Value:
    return {"value": n, "numerator": None, "denominator": None, "enough": True, "vs_board": None}


ALPHA_90: dict[str, Value] = {
    "open_tickets": count(3),
    "in_progress": count(2),
    "done_in_period": count(5),
    "estimated_before_start": rate(4, 7, True, "closer"),
    "time_logged": rate(4, 5, True, "further"),
    "within_estimate": rate(2, 3, False, None),
    "stuck_now": rate(1, 3, False, None),
    "reopened": rate(1, 5, True, "level"),
    "skipped_stage": rate(1, 5, True, "further"),
    "code_review": rate(2, 4, False, None),
    "open_findings_per_10": {"value": 13.3, "numerator": 4, "denominator": 3, "enough": False, "vs_board": None},
}
BOARD_90: dict[str, Value] = {
    "open_tickets": count(6),
    "in_progress": count(2),
    "done_in_period": count(20),
    "estimated_before_start": rate(10, 22, True, None),
    "time_logged": rate(18, 20, True, None),
    "within_estimate": rate(6, 7, True, None),
    "stuck_now": rate(1, 6, True, None),
    "reopened": rate(3, 20, True, None),
    "skipped_stage": rate(2, 20, True, None),
    "code_review": rate(6, 8, True, None),
    "open_findings_per_10": {"value": 6.7, "numerator": 4, "denominator": 6, "enough": True, "vs_board": None},
}


def test_every_measure_on_known_numbers_and_the_period_edges(client: TestClient) -> None:
    body = scorecard(client, [("person", pid("alpha"))])
    assert body["period"] == {"days": 90, "from": to_iso(S), "to": to_iso(T)}
    assert (body["basis"], body["comparison"]) == ("credit", "on")
    assert body["board"] == {"tickets": len(ALPHA) + len(OTHERS) - 1, "values": BOARD_90}
    (alpha,) = body["people"]
    assert alpha["person"] == {"id": pid("alpha"), "name": "Alpha Tester", "avatar": None}
    assert alpha["tickets"] == len(ALPHA) - 1
    assert alpha["values"] == ALPHA_90


def test_the_measures_are_all_eleven_in_the_contracts_order_with_their_rules(client: TestClient) -> None:
    measures = scorecard(client)["measures"]
    assert [(m["key"], m["label"], m["kind"], m["direction"]) for m in measures] == [
        ("open_tickets", "Open tickets", "count", "neutral"),
        ("in_progress", "In progress now", "count", "neutral"),
        ("done_in_period", "Done in the period", "count", "neutral"),
        ("estimated_before_start", "Estimated before work started", "rate", "higher_is_closer"),
        ("time_logged", "Time logged on done work", "rate", "higher_is_closer"),
        ("within_estimate", "Done within the effort limit", "rate", "higher_is_closer"),
        ("stuck_now", "Open tickets stuck in a status", "rate", "lower_is_closer"),
        ("reopened", "Reopened after Done", "rate", "lower_is_closer"),
        ("skipped_stage", "Skipped a stage", "rate", "lower_is_closer"),
        ("code_review", "Code review recorded", "rate", "higher_is_closer"),
        ("open_findings_per_10", "Open findings per 10 open tickets", "ratio", "lower_is_closer"),
    ]
    rules = {m["key"]: m["rule_ids"] for m in measures}
    assert rules == {
        "open_tickets": [], "in_progress": [], "done_in_period": [], "estimated_before_start": ["R-EST-01", "R-EST-02"],
        "time_logged": [], "within_estimate": ["R-EFF-01"], "stuck_now": ["W-AGE-01"], "reopened": ["R-FLOW-03"],
        "skipped_stage": ["R-FLOW-02"], "code_review": ["R-FLOW-01", "W-REV-01"],
        "open_findings_per_10": [rule.id for rule in RULES.rules if rule.scope == "issue"],
    }
    assert all(m["measurable"] and m["reason"] is None for m in measures)
    assert set(m["key"] for m in measures) - set(COMPARABLE) == {"open_tickets", "in_progress", "done_in_period"}


def test_a_shorter_period_moves_the_edges(client: TestClient) -> None:
    body = scorecard(client, [("person", pid("alpha")), ("days", 30)])
    assert body["period"] == {"days": 30, "from": to_iso(T - timedelta(days=30)), "to": to_iso(T)}
    values = body["people"][0]["values"]
    assert values["done_in_period"] == count(4)
    assert values["reopened"] == rate(1, 4, False, None)
    assert values["estimated_before_start"] == rate(4, 5, True, values["estimated_before_start"]["vs_board"])
    assert values["open_tickets"] == count(3)


def test_the_effort_threshold_and_the_grace_are_read_from_the_rules(root: Path) -> None:
    catalogue = yaml.safe_load((CONFIG_FIXTURES / "rules.yaml").read_text(encoding="utf-8"))
    for rule in catalogue["rules"]:
        if rule["id"] == "R-EFF-01":
            rule["params"]["threshold"] = 2.0
        if rule["id"] == "R-EST-02":
            rule["params"]["grace_hours"] = 0
    with serve(root, write_config(root / "other", rules=catalogue)) as other:
        values = scorecard(other, [("person", pid("alpha"))])["people"][0]["values"]
    assert values["within_estimate"] == rate(3, 3, False, None)
    assert values["estimated_before_start"] == rate(3, 7, True, values["estimated_before_start"]["vs_board"])


def test_everyone_who_holds_a_ticket_in_the_directorys_order_and_no_member_without_one(client: TestClient) -> None:
    body = scorecard(client)
    assert [row["person"]["name"] for row in body["people"]] == ["Alpha Tester", "Bravo Tester"]
    bravo = body["people"][1]
    assert bravo["tickets"] == 6 and bravo["values"]["done_in_period"] == count(5)
    assert bravo["values"]["estimated_before_start"] == rate(5, 5, True, "closer")
    assert pid("mike") not in json.dumps(body)


def test_a_member_with_no_tickets_asked_for_has_no_figures(client: TestClient) -> None:
    body = scorecard(client, [("person", pid("mike")), ("person", pid("alpha"))])
    assert [row["person"]["name"] for row in body["people"]] == ["Alpha Tester", "Mike Member"]
    mike = body["people"][1]
    assert mike["tickets"] == 0
    assert mike["values"] == {measure.key: {"value": None, "numerator": None, "denominator": None, "enough": False,
                                            "vs_board": None} for measure in MEASURES}


def test_each_reason_a_board_cannot_show_a_measure(client: TestClient, root: Path) -> None:
    flat = scorecard(client, board="FLAT")
    reasons = {m["key"]: m["reason"] for m in flat["measures"] if not m["measurable"]}
    assert reasons == {"code_review": NO_CODE_REVIEW, "time_logged": NO_TIME_LOGGED}
    assert reasons == {"code_review": "This board doesn't record code review in Jira.",
                       "time_logged": "No one logged time on this board in the period."}
    assert NO_STAGES_TO_SKIP == "This board has no stages to skip."
    assert flat["board"]["values"]["skipped_stage"] == rate(0, 1, False, None)
    for key in reasons:
        assert flat["board"]["values"][key] == {"value": None, "numerator": None, "denominator": None,
                                                "enough": False, "vs_board": None}
        assert all(row["values"][key]["value"] is None for row in flat["people"])
    hours = scorecard(client, board="HOURS")
    assert [m["key"] for m in hours["measures"] if not m["measurable"]] == ["time_logged", "code_review"]
    assert "time_logged" not in [m["key"] for m in scorecard(client, [("days", 180)], board="HOURS")["measures"]
                                 if not m["measurable"]]
    with serve(root, write_config(root / "no_points", story_points=False)) as other:
        rev = {m["key"]: m["reason"] for m in scorecard(other)["measures"] if not m["measurable"]}
        assert rev == {"estimated_before_start": NO_ESTIMATES, "within_estimate": NO_ESTIMATES}
        assert NO_ESTIMATES == "This board has no estimates."
        assert {m["key"] for m in scorecard(other, board="HOURS")["measures"] if not m["measurable"]} == {
            "time_logged", "code_review"}
    catalogue = yaml.safe_load((CONFIG_FIXTURES / "rules.yaml").read_text(encoding="utf-8"))
    catalogue["rules"] = [rule for rule in catalogue["rules"] if rule["id"] != "R-EFF-01"]
    for rule in catalogue["rules"]:
        if rule.get("escalates_to") == "R-EFF-01":
            del rule["escalates_to"]
    with serve(root, write_config(root / "no_limit", rules=catalogue)) as other:
        assert {m["key"]: m["reason"] for m in scorecard(other)["measures"] if not m["measurable"]} == {
            "within_estimate": NO_EFFORT_LIMIT}


def _turn_off(path: Path, board: str, *rule_ids: str) -> None:
    with Store.open(path) as store:
        revision = load_board_settings(store.conn, board).revision
        save_board_settings(store, RULES, board, revision=revision, changes=[Change(rule_id, enabled=False)
                                                                             for rule_id in rule_ids],
                            viewer_hash="v" + "0" * 12, viewer_name="Admin Tester", now=T)


def test_a_measure_whose_rules_the_board_turned_off_says_so(tmp_path: Path) -> None:
    build(tmp_path / "oversight.db")
    _turn_off(tmp_path / "oversight.db", "REV", "R-FLOW-02", "W-AGE-01", "R-FLOW-03", "R-EFF-01", "R-EST-02")
    with serve(tmp_path, write_config(tmp_path)) as served:
        body = scorecard(served, [("person", pid("alpha"))])
        reasons = {m["key"]: m["reason"] for m in body["measures"] if not m["measurable"]}
        assert reasons == {"within_estimate": RULE_OFF, "stuck_now": RULE_OFF, "reopened": RULE_OFF,
                           "skipped_stage": RULE_OFF}
        assert RULE_OFF == "Its rule is off on this board."
        for key in reasons:
            assert body["board"]["values"][key]["value"] is None
            assert body["people"][0]["values"][key]["value"] is None
        assert body["board"]["values"]["estimated_before_start"] == BOARD_90["estimated_before_start"]
        assert body["board"]["values"]["code_review"] == BOARD_90["code_review"]
        _turn_off(tmp_path / "oversight.db", "REV", "R-EST-01", "R-FLOW-01")
        _turn_off(tmp_path / "oversight.db", "FLAT", "R-FLOW-01")
        rev = {m["key"]: m["reason"] for m in scorecard(served)["measures"] if not m["measurable"]}
        assert rev["estimated_before_start"] == RULES_OFF == "Its rules are off on this board."
        assert rev["code_review"] == RULE_OFF
        flat = {m["key"]: m["reason"] for m in scorecard(served, board="FLAT")["measures"] if not m["measurable"]}
        assert flat == {"code_review": NO_CODE_REVIEW, "time_logged": NO_TIME_LOGGED}


def test_a_board_that_records_code_review_still_does_with_its_rule_turned_off() -> None:
    board = Board(project="REV", as_of=to_iso(T) or "", total=0, work=(), holdings={}, holders=(), members=(),
                  reasons={}, last_worklog=None, effort_limit=1.5, every_rule=())
    assert board.records_code_review
    assert replace(board, reasons={"code_review": RULE_OFF}).records_code_review
    assert not replace(board, reasons={"code_review": NO_CODE_REVIEW}).records_code_review


def value(figure: int | float | None, denominator: int, enough: bool | None = None) -> Value:
    return {"value": figure, "numerator": 0, "denominator": denominator,
            "enough": denominator >= MIN_TICKETS if enough is None else enough, "vs_board": None}


RATE = Measure("x", "X", "rate", "higher_is_closer")
LOWER = Measure("y", "Y", "rate", "lower_is_closer")
RATIO = Measure("z", "Z", "ratio", "lower_is_closer")
COUNT = Measure("c", "C", "count", "neutral")


@pytest.mark.parametrize(
    ("measure", "mine", "board", "expected"),
    [
        (RATE, 72, 67, "level"),
        (RATE, 73, 67, "closer"),
        (RATE, 61, 67, "further"),
        (RATE, 62, 67, "level"),
        (LOWER, 61, 67, "closer"),
        (LOWER, 73, 67, "further"),
        (RATIO, 2.3, 1.8, "level"),
        (RATIO, 2.4, 1.8, "further"),
        (RATIO, 1.2, 1.8, "closer"),
        (RATIO, 1.3, 1.8, "level"),
    ],
)
def test_vs_board_is_level_within_five_points_or_half_and_otherwise_by_direction(
    measure: Measure, mine: float, board: float, expected: str
) -> None:
    assert vs_board(measure, value(mine, 5), value(board, 50)) == expected


def test_vs_board_compares_only_when_both_sides_have_enough_and_never_a_count() -> None:
    assert MIN_TICKETS == 5
    assert vs_board(RATE, value(100, 4), value(10, 50)) is None
    assert vs_board(RATE, value(100, 5), value(10, 4)) is None
    assert vs_board(RATE, value(None, 0), value(10, 50)) is None
    assert vs_board(COUNT, value(12, 50, True), value(3, 50, True)) is None


@pytest.mark.parametrize(("numerator", "denominator", "shown"), [(5, 8, 63), (1, 8, 13), (1, 3, 33), (2, 3, 67),
                                                                  (1, 2, 50), (0, 7, 0), (7, 7, 100), (1, 200, 1)])
def test_a_rate_is_a_whole_percentage_rounded_half_up(numerator: int, denominator: int, shown: int) -> None:
    assert percent(numerator, denominator) == shown
    assert percent(7, 3) / 10 == 23.3 and percent(1, 6) / 10 == 1.7


@pytest.mark.parametrize(
    "query",
    [
        "days=60", "days=", "days=ninety", "days=90.0", "days=090", "days=90&days=90", "level=admin", "level=People",
        "level=people&level=people", "person=p0123", "person=P0123456789AB", "person=acc-alpha",
        "person=p0123456789ab&person=p0123456789ab", "page=2", "project=REV&project=REV",
        "&".join(f"person=p{n:012x}" for n in range(11)),
    ],
)
def test_bad_parameters_are_422(client: TestClient, query: str) -> None:
    level = "" if "level" in query else "level=people&"
    response = client.get(f"/api/v1/people/scorecard?{level}{query}")
    assert response.status_code == 422, (query, response.text)


def test_ten_people_are_allowed_and_a_person_not_on_the_board_is_404(client: TestClient) -> None:
    ten = "&".join(f"person=p{n:012x}" for n in range(10))
    assert client.get(f"/api/v1/people/scorecard?level=people&{ten}").status_code == 404
    response = client.get(f"/api/v1/people/scorecard?level=people&person={pid('alpha')}&person=p000000000000")
    assert response.status_code == 404 and response.json() == {"detail": "nobody by that id is on this board"}
    assert client.get(f"/api/v1/people/scorecard?level=people&person={pid('zed')}").status_code == 404
    assert client.get(f"/api/v1/people/scorecard?level=people&project=FLAT&person={pid('zed')}").status_code == 200
    assert client.get("/api/v1/people/scorecard?level=people&project=NOPE").status_code == 404
    assert client.get("/api/v1/people/scorecard?level=people", headers={"X-Service-Key": "wrong"}).status_code == 401


@pytest.mark.parametrize("query", ["", "?level=team", f"?level=team&person={'p' + '0' * 12}", "?days=30"])
def test_team_level_is_403_people_level_only(client: TestClient, query: str) -> None:
    response = client.get(f"/api/v1/people/scorecard{query}")
    assert response.status_code == 403
    assert response.json() == {"detail": "person scorecards are for people level only", "code": "people_level_only"}
    assert response.headers["cache-control"] == "no-store"


def test_comparing_people_turned_off(root: Path) -> None:
    with serve(root, write_config(root / "off"), people_comparison="off") as off:
        two = off.get(f"/api/v1/people/scorecard?level=people&person={pid('alpha')}&person={pid('bravo')}")
        assert two.status_code == 403
        assert two.json() == {"detail": "comparing people is turned off on this service (PEOPLE_COMPARISON=off)",
                              "code": "comparison_off"}
        none = scorecard(off)
        assert (none["comparison"], none["people"]) == ("off", [])
        assert none["board"]["values"] == BOARD_90
        one = scorecard(off, [("person", pid("alpha"))])
        assert one["comparison"] == "off" and one["people"][0]["values"] == ALPHA_90


def test_no_response_holds_an_account_id_or_anything_else_about_anyone(client: TestClient) -> None:
    texts = [json.dumps(scorecard(client, params, board)) for board in ("REV", "FLAT", "HOURS")
             for params in ([], [("days", 30)], [("days", 180)])]
    texts.append(json.dumps(scorecard(client, [("person", pid("mike"))])))
    for text in texts:
        assert not [secret for secret in SECRETS if secret in text]
        for person in json.loads(text)["people"]:
            assert set(person["person"]) == {"id", "name", "avatar"}
