"""JO-13, the person card: GET /people/{person_id}/card (contract 4.19 in jira-oversight-ui), one person's work on a
board at a glance, for admins (people level).

Two seeded boards share one store (DEMO and AIL, the demo's fictional people on both): every person's card on each
board, over each period, is checked against their scorecard and GET /people (one computation), members with no
tickets and the demo's holder with too few tickets to compare included; the tickets in progress, their order and
since, and the last activity against SQL written here. The hand-built store of test_api_people pins a card word for
word, with numbers worked out by hand. Then the level, the parameters, the 404s, no-store, the access log (an entry
at most once per viewer, person and board in any ten minutes, two reads at once leaving one, nothing served when the
log cannot be written or looked at), and that a card holds nobody's account id. No network.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oversight.api import access_log as access_log_module
from oversight.api.access_log import (
    ACTIONS,
    PERSON_CARD_ONCE_IN,
    PERSON_CARD_VIEW,
    AccessLog,
    Viewer,
)
from oversight.api.app import create_app
from oversight.api.routes.person_card import CARD_TICKETS, in_progress_since, timeline_of
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import ASSIGNEE_NAMES, MEMBER_NAMES, seed_demo
from oversight.derive.run import run_derive
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES
from tests.test_api_findings import check_meta, check_person, fields_are, is_ts
from tests.test_api_people import ACCOUNTS, build, pid, serve, shown
from tests.test_api_people import T as BUILT_AT
from tests.test_governance import ADMIN, ADMIN_NAME, GONE, OTHER, UNAVAILABLE, Clock, entries, headers

KEY = "test-key"
NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
BOARDS = ("DEMO", "AIL")  # JIRA_PROJECTS, in order
BOARD_NAMES = {"DEMO": "Demo project", "AIL": "AIL"}  # process.yaml's names (AIL has none: its key)
CARD_FIELDS = ("meta", "person", "board", "membership", "work", "findings", "process", "in_progress_now",
               "in_progress_more", "other_boards", "last_activity_at")
DEMO_PEOPLE = {pid(account): name for account, name in {**ASSIGNEE_NAMES, **MEMBER_NAMES}.items()}
ACCOUNT_OF = {pid(account): account for account in {**ASSIGNEE_NAMES, **MEMBER_NAMES}}


def make_settings(root: Path, **update: Any) -> Settings:
    return Settings(_env_file=None, jira_projects=",".join(BOARDS), api_service_key=KEY, db_path=root / "oversight.db",
                    config_dir=CONFIG_FIXTURES, reports_dir=root / "reports", **update)


def client_of(settings: Settings, *, clock: Clock | None = None, **sent: Any) -> TestClient:
    app = create_app(settings, provider=ScriptedProvider(), now=clock or (lambda: NOW))
    return TestClient(app, headers=headers(**sent))


@pytest.fixture(scope="module")
def two(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """DEMO (the full demo, its people's habits and all) and a smaller AIL, both seeded, derived and checked at NOW."""
    settings = make_settings(tmp_path_factory.mktemp("person_card"))
    process = ProcessConfig.load(settings.process_file)
    rules = RulesConfig.load(settings.rules_file)
    fields = FieldsConfig.load(settings.fields_file)
    with Store.open(settings.db_file) as store:
        for project, issues in (("DEMO", 1150), ("AIL", 240)):
            seed_demo(store, now=NOW, seed=20261105, project=project, issues=issues)
            run_derive(store, process, projects=[project], now=lambda: NOW)
            run_rules(store, process, rules, fields, projects=[project], now=lambda: NOW)
    return settings


@pytest.fixture(scope="module")
def client(two: Settings) -> TestClient:
    return client_of(two)


@pytest.fixture
def fresh(two: Settings) -> Settings:
    """The store with an empty access log, for a test that counts its entries."""
    conn = sqlite3.connect(two.db_file)
    try:
        conn.execute("DELETE FROM access_log")
        conn.commit()
    finally:
        conn.close()
    return two


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """test_api_people's hand-built store: DEMO and AIL, full Jira user objects, members on each board."""
    root = tmp_path_factory.mktemp("person_card_built")
    build(root / "oversight.db")
    with serve(root) as served:
        yield served


def get(client: TestClient, path: str, params: Any = None) -> Any:
    response = client.get(f"/api/v1{path}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def card(client: TestClient, person_id: str, board: str = "DEMO", days: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"level": "people", "project": board}
    if days is not None:
        params["days"] = days
    body: dict[str, Any] = get(client, f"/people/{person_id}/card", params)
    return body


def by_name(client: TestClient, board: str = "DEMO") -> dict[str, dict[str, Any]]:
    """The board's people by name (the directory's person objects)."""
    directory = get(client, "/people/directory", {"project": board})["people"]
    return {entry["person"]["name"]: entry["person"] for entry in directory}


def standing(measures: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, int] | None:
    """The process meter worked out from a scorecard row, as the dashboard reads one (people/model.ts standingOf):
    each measure the board can show that is not a count is closer, level, further, or too few tickets (no mark)."""
    marks = [values[measure["key"]]["vs_board"] for measure in measures
             if measure["measurable"] and measure["kind"] != "count"]
    found = {"closer": marks.count("closer"), "level": marks.count("level"), "further": marks.count("further"),
             "too_few": marks.count(None), "measurable": len(marks)}
    return found if found["closer"] + found["level"] + found["further"] else None


# --- one computation: every figure is the scorecard's and GET /people's -----------------------------------------


@pytest.mark.parametrize("days", [30, 90, 180])
def test_every_figure_is_the_scorecards_and_get_peoples_for_everyone_on_each_board(
    client: TestClient, days: int
) -> None:
    open_elsewhere = {
        board: {row["person"]["id"]: row["open_tickets"] for row in get(client, "/people", {"project": board})["people"]
                if row["person"] is not None}
        for board in BOARDS
    }
    seen = {"holds_tickets": 0, "member_no_tickets": 0, "process": 0, "no_process": 0}
    for board in BOARDS:
        directory = get(client, "/people/directory", {"project": board})["people"]
        scorecard = get(client, "/people/scorecard", {"project": board, "level": "people", "days": days})
        scored = {row["person"]["id"]: row for row in scorecard["people"]}
        listed = get(client, "/people", {"project": board})["people"]
        rows = {row["person"]["id"]: row for row in listed if row["person"] is not None}
        assert {entry["person"]["id"] for entry in directory} == set(DEMO_PEOPLE)  # the demo's people, on both boards
        for entry in directory:
            who = entry["person"]
            body = card(client, who["id"], board, days)
            assert body["person"] == who and body["board"] == {"key": board, "name": BOARD_NAMES[board]}
            values, row = scored.get(who["id"]), rows.get(who["id"])
            if entry["tickets"]:
                assert body["membership"] == "holds_tickets" and values is not None and row is not None
                figures = values["values"]
                assert body["work"] == {"open_tickets": figures["open_tickets"]["value"],
                                        "in_progress": figures["in_progress"]["value"],
                                        "done_in_period": figures["done_in_period"]["value"], "days": days}
                assert body["work"]["open_tickets"] == row["open_tickets"]  # G5: one meaning of open tickets
                assert body["findings"] == {"open_breaches": row["open_breaches"],
                                            "open_warnings": row["open_warnings"]}
                assert body["process"] == standing(scorecard["measures"], figures)
            else:  # a member who holds none: on the directory only
                assert body["membership"] == "member_no_tickets" and values is None and row is None
                assert body["work"] == {"open_tickets": 0, "in_progress": 0, "done_in_period": 0, "days": days}
                assert body["findings"] == {"open_breaches": 0, "open_warnings": 0}
                assert (body["process"], body["in_progress_now"], body["last_activity_at"]) == (None, [], None)
            assert len(body["in_progress_now"]) == min(CARD_TICKETS, body["work"]["in_progress"])
            assert len(body["in_progress_now"]) + body["in_progress_more"] == body["work"]["in_progress"]
            assert body["other_boards"] == [
                {"key": other, "name": BOARD_NAMES[other], "open_tickets": open_elsewhere[other][who["id"]]}
                for other in BOARDS if other != board and open_elsewhere[other].get(who["id"], 0) > 0
            ]
            seen[body["membership"]] += 1
            seen["process" if body["process"] else "no_process"] += 1
    # Holders and members on both boards, and holders compared with the board.
    assert seen["member_no_tickets"] == len(BOARDS) * len(MEMBER_NAMES)
    assert seen["holds_tickets"] >= len(BOARDS) * len(ASSIGNEE_NAMES) - 2
    assert seen["process"] >= len(ASSIGNEE_NAMES) // 2


def test_nothing_measurable_for_someone_with_too_few_tickets(client: TestClient) -> None:
    """Hana Mori, part time of late (the demo's habit): she holds tickets, but too few to compare on any measure."""
    hana = card(client, by_name(client)["Hana Mori"]["id"])
    assert hana["membership"] == "holds_tickets" and hana["work"]["open_tickets"] + hana["work"]["done_in_period"] > 0
    assert hana["process"] is None
    ada = card(client, by_name(client)["Ada Brooks"]["id"])
    process = ada["process"]
    assert process is not None and process["closer"] + process["level"] + process["further"] >= 1
    assert sum(process[key] for key in ("closer", "level", "further", "too_few")) == process["measurable"]


def test_the_body_has_exactly_the_contracts_keys(client: TestClient) -> None:
    people = by_name(client)
    for name in ("Ada Brooks", "Hana Mori", "Noor Haddad"):
        body = card(client, people[name]["id"])
        fields_are(body, *CARD_FIELDS)
        check_meta(body["meta"])
        check_person(body["person"])
        fields_are(body["board"], "key", "name")
        assert body["membership"] in ("holds_tickets", "member_no_tickets")
        fields_are(body["work"], "open_tickets", "in_progress", "done_in_period", "days")
        fields_are(body["findings"], "open_breaches", "open_warnings")
        if body["process"] is not None:
            fields_are(body["process"], "closer", "level", "further", "too_few", "measurable")
        for item in body["in_progress_now"]:
            fields_are(item, "key", "summary", "status", "since")
            assert item["summary"] and item["status"] and is_ts(item["since"])
        for other in body["other_boards"]:
            fields_are(other, "key", "name", "open_tickets")
        whole = [*body["work"].values(), *body["findings"].values(), body["in_progress_more"],
                 *(body["process"] or {}).values(), *(other["open_tickets"] for other in body["other_boards"])]
        assert all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in whole)
        assert body["last_activity_at"] is None or is_ts(body["last_activity_at"])


# --- the tickets in progress, and the last activity, against SQL ------------------------------------------------


def timeline(two: Settings) -> tuple[dict[str, list[sqlite3.Row]], dict[str, str]]:
    """DEMO's status intervals by ticket (timeline order) and its statuses' categories, read here."""
    conn = open_readonly(two.db_file)
    try:
        intervals: dict[str, list[sqlite3.Row]] = {}
        for row in conn.execute("SELECT si.issue_key, si.status, si.entered_at FROM status_intervals AS si"
                                " JOIN issues AS i ON i.issue_key = si.issue_key WHERE i.project_key = 'DEMO'"
                                " ORDER BY si.issue_key, si.seq"):
            intervals.setdefault(row["issue_key"], []).append(row)
        categories = {row["name"]: row["category"]
                      for row in conn.execute("SELECT name, category FROM statuses WHERE project_key = 'DEMO'")}
    finally:
        conn.close()
    return intervals, categories


def theirs(two: Settings, account: str, where: str = "") -> list[sqlite3.Row]:
    """A person's tickets on DEMO, Epics left out (as the rules leave them out), read here."""
    conn = open_readonly(two.db_file)
    try:
        return list(conn.execute(
            "SELECT issue_key, summary, status, status_category, created_at FROM issues WHERE project_key = 'DEMO'"
            f" AND assignee_id = ? AND lower(COALESCE(issue_type, '')) != 'epic'{where}", (account,)))
    finally:
        conn.close()


def test_in_progress_now_is_longest_in_progress_first_at_most_three_and_the_rest_counted(
    client: TestClient, two: Settings
) -> None:
    intervals, categories = timeline(two)
    checked_more = 0
    for person_id, account in ACCOUNT_OF.items():
        expected = []
        for ticket in theirs(two, account, " AND status_category = 'indeterminate'"):
            run = intervals[ticket["issue_key"]]
            since = run[-1]["entered_at"]  # the status it is in now, then back while still in progress
            for interval in reversed(run[:-1]):
                if categories[interval["status"]] != "indeterminate":
                    break
                since = interval["entered_at"]
            expected.append({"key": ticket["issue_key"], "summary": ticket["summary"], "status": ticket["status"],
                             "since": since})
        expected.sort(key=lambda item: (item["since"], int(item["key"].rpartition("-")[2])))
        body = card(client, person_id)
        assert body["in_progress_now"] == expected[:CARD_TICKETS]
        assert body["in_progress_more"] == max(0, len(expected) - CARD_TICKETS)
        checked_more += body["in_progress_more"] > 0
    assert checked_more >= 5  # most of the demo's people have more than three in progress


def test_last_activity_is_the_latest_status_change_or_logged_work_on_their_tickets(
    client: TestClient, two: Settings
) -> None:
    conn = open_readonly(two.db_file)
    try:
        for person_id, account in ACCOUNT_OF.items():
            keys = [row["issue_key"] for row in theirs(two, account)]
            marks = ", ".join("?" * len(keys)) or "NULL"
            moments = [row[0] for row in conn.execute(
                f"SELECT changed_at FROM status_changes WHERE issue_key IN ({marks}) UNION ALL"
                f" SELECT started_at FROM worklogs WHERE issue_key IN ({marks}) AND started_at <= ?",
                (*keys, *keys, to_iso(NOW)))]
            expected = max(moments) if moments else None
            assert card(client, person_id)["last_activity_at"] == expected, DEMO_PEOPLE[person_id]
    finally:
        conn.close()


SINCE_CATEGORY = {"To Do": "new", "In Progress": "indeterminate", "In Review": "indeterminate", "Done": "done"}
CREATED = "2026-07-01T09:00:00.000Z"


def changes_of(*moves: tuple[str, str, str]) -> list[dict[str, Any]]:
    """Status changes as sync stores them: (from, to, when), their changelog ids in order."""
    return [{"changelog_id": str(100 + n), "changed_at": at, "from_status": before, "to_status": after}
            for n, (before, after, at) in enumerate(moves)]


@pytest.mark.parametrize(("status", "moves", "expected"), [
    # Created in progress and never moved: since its creation.
    ("In Progress", [], CREATED),
    # Into progress once.
    ("In Progress", [("To Do", "In Progress", "2026-11-01T09:00:00.000Z")], "2026-11-01T09:00:00.000Z"),
    # Still in progress in another status: In Progress to In Review keeps it going.
    ("In Review", [("To Do", "In Progress", "2026-10-03T09:00:00.000Z"),
                   ("In Progress", "In Review", "2026-10-05T09:00:00.000Z")], "2026-10-03T09:00:00.000Z"),
    # Reopened from Done: the clock starts again.
    ("In Progress", [("To Do", "In Progress", "2026-09-02T09:00:00.000Z"),
                     ("In Progress", "Done", "2026-09-09T09:00:00.000Z"),
                     ("Done", "In Progress", "2026-10-20T09:00:00.000Z")], "2026-10-20T09:00:00.000Z"),
    # Created in progress, moved on within progress: since its creation.
    ("In Review", [("In Progress", "In Review", "2026-08-03T09:00:00.000Z")], CREATED),
    # The changes in any order, as a query returns them: the timeline sorts them as derive does.
    ("In Review", [("In Progress", "In Review", "2026-10-05T09:00:00.000Z"),
                   ("To Do", "In Progress", "2026-10-03T09:00:00.000Z")], "2026-10-03T09:00:00.000Z"),
    # The history ends in To Do though Jira says In Progress now (a change missing from it): when it went back into
    # progress is not known, so no time is made up (not its creation, nor the run that ended).
    ("In Progress", [("To Do", "In Progress", "2026-10-05T09:00:00.000Z"),
                     ("In Progress", "To Do", "2026-10-25T09:00:00.000Z")], None),
])
def test_since_is_when_the_ticket_last_went_into_progress(
    status: str, moves: list[tuple[str, str, str]], expected: str | None
) -> None:
    timeline = timeline_of({"created_at": CREATED, "status": status}, changes_of(*moves), lambda _status: None)
    assert in_progress_since(timeline, lambda name, _stage: SINCE_CATEGORY[name or ""]) == expected


def test_since_is_never_made_up_when_there_is_no_timeline() -> None:
    category = (lambda name, _stage: SINCE_CATEGORY.get(name or "", "new"))
    assert in_progress_since([], category) is None
    # No creation time, or a moment that cannot be read: no timeline, so no since.
    assert timeline_of({"created_at": None, "status": "In Progress"}, [], lambda _status: None) == []
    unreadable = changes_of(("To Do", "In Progress", "not a moment"))
    assert timeline_of({"created_at": CREATED, "status": "In Progress"}, unreadable, lambda _status: None) == []


def resync(root: Path, *moves: tuple[str, str, timedelta]) -> None:
    """A later sync of echo's DEMO-1 (In Progress in Jira now), as sync writes it: its status changes, (from, to,
    how long before T), after the ones the store already has. No derive after it: derive's intervals still end
    with the move into progress 20 days before T."""
    with Store.open(root / "oversight.db") as store, store.transaction():
        for n, (before, after, ago) in enumerate(moves, 1):
            store.upsert("status_changes", {"issue_key": "DEMO-1", "changelog_id": str(100 + n),
                                            "changed_at": to_iso(BUILT_AT - ago), "author_id": ACCOUNTS["delta"],
                                            "from_status": before, "to_status": after})


def test_since_follows_a_sync_that_derive_has_not_followed_yet(tmp_path: Path) -> None:
    """Sync, derive and the rules are separate steps: a sync that has stored a reopening, and no derive since, must
    not leave the card counting from the run before it."""
    build(tmp_path / "oversight.db")
    resync(tmp_path, ("In Progress", "Done", timedelta(days=10)), ("Done", "In Progress", timedelta(hours=4)))
    with serve(tmp_path) as served:
        listed = built_card(served, "echo", "DEMO")["in_progress_now"]
    assert listed == [{"key": "DEMO-1", "summary": "Sample task 1", "status": "In Progress",
                       "since": to_iso(BUILT_AT - timedelta(hours=4))}]


def test_a_history_that_ends_outside_progress_gives_no_since(tmp_path: Path) -> None:
    """The history says DEMO-1 went back to To Do; Jira says it is in progress now (the change back is not in the
    history, which derive keeps, flagging current_status_mismatch): its since is not known, derived or not."""
    build(tmp_path / "oversight.db")
    resync(tmp_path, ("In Progress", "To Do", timedelta(days=3)))
    with serve(tmp_path) as served:
        assert built_card(served, "echo", "DEMO")["in_progress_now"][0]["since"] is None
    with Store.open(tmp_path / "oversight.db") as store:
        derived = run_derive(store, ProcessConfig.load(CONFIG_FIXTURES / "process.yaml"), projects=["DEMO"],
                             now=lambda: BUILT_AT)
    assert derived.warnings["current_status_mismatch"] == 1
    with serve(tmp_path) as served:
        assert built_card(served, "echo", "DEMO")["in_progress_now"][0]["since"] is None


# --- a card word for word, on the hand-built store ------------------------------------------------------------


def built_card(client: TestClient, who: str, board: str) -> dict[str, Any]:
    body = card(client, pid(ACCOUNTS[who]), board)
    body.pop("meta")
    return body


def test_a_card_word_for_word(built: TestClient) -> None:
    """echo holds DEMO-1 (in progress since it moved there 20 days before T, with time logged then) and DEMO-13
    (done), two open breaches on DEMO-1, and AIL-2 on AIL; two tickets are too few to compare on any measure."""
    moved = "2026-10-15T12:00:00.000Z"  # T less 20 days
    assert built_card(built, "echo", "DEMO") == {
        "person": shown("echo"),
        "board": {"key": "DEMO", "name": "Demo project"},
        "membership": "holds_tickets",
        "work": {"open_tickets": 1, "in_progress": 1, "done_in_period": 1, "days": 90},
        "findings": {"open_breaches": 2, "open_warnings": 0},
        "process": None,
        "in_progress_now": [{"key": "DEMO-1", "summary": "Sample task 1", "status": "In Progress", "since": moved}],
        "in_progress_more": 0,
        "other_boards": [{"key": "AIL", "name": "AIL", "open_tickets": 1}],
        "last_activity_at": moved,
    }
    # On AIL: AIL-2, created in progress 25 days before T and never moved, and DEMO beside it.
    on_ail = built_card(built, "echo", "AIL")
    assert on_ail["in_progress_now"] == [{"key": "AIL-2", "summary": "Sample task 15", "status": "In Progress",
                                          "since": "2026-10-10T12:00:00.000Z"}]
    assert on_ail["other_boards"] == [{"key": "DEMO", "name": "Demo project", "open_tickets": 1}]
    assert on_ail["last_activity_at"] is None  # no status change and no time logged on AIL-2


def test_someone_whose_tickets_are_all_done_holds_tickets_with_none_open(built: TestClient) -> None:
    zulu = built_card(built, "zulu", "DEMO")
    assert zulu["membership"] == "holds_tickets"
    assert zulu["work"] == {"open_tickets": 0, "in_progress": 0, "done_in_period": 1, "days": 90}
    assert (zulu["in_progress_now"], zulu["other_boards"], zulu["process"]) == ([], [], None)


def test_members_with_no_tickets_and_people_on_one_board_only(built: TestClient) -> None:
    for who, board in (("mike", "DEMO"), ("olof", "DEMO"), ("oscar", "AIL")):
        member = built_card(built, who, board)
        assert member == {
            "person": shown(who), "board": {"key": board, "name": BOARD_NAMES[board]},
            "membership": "member_no_tickets",
            "work": {"open_tickets": 0, "in_progress": 0, "done_in_period": 0, "days": 90},
            "findings": {"open_breaches": 0, "open_warnings": 0}, "process": None, "in_progress_now": [],
            "in_progress_more": 0, "other_boards": [], "last_activity_at": None,
        }
    # outsider holds AIL-1 only: a card on AIL, with no other board; on DEMO, as oscar, no one by that id.
    assert built_card(built, "outsider", "AIL")["other_boards"] == []
    for who in ("outsider", "oscar"):
        response = built.get(f"/api/v1/people/{pid(ACCOUNTS[who])}/card", params={"level": "people"})
        assert response.status_code == 404 and response.json() == {"detail": "nobody by that id is on this board"}


# --- the level, the parameters, the 404s, no-store --------------------------------------------------------------


@pytest.mark.parametrize("params", [{}, {"level": "team"}, {"level": "team", "days": "30"}])
def test_team_level_is_403_people_level_only_whoever_is_asked_about(
    client: TestClient, fresh: Settings, params: dict[str, str]
) -> None:
    ada = by_name(client)["Ada Brooks"]["id"]
    for person_id in (ada, GONE, "not-an-id"):
        response = client.get(f"/api/v1/people/{person_id}/card", params=params)
        assert response.status_code == 403, response.text
        assert response.json() == {"detail": "person cards are for people level only", "code": "people_level_only"}
    assert entries(fresh) == []


@pytest.mark.parametrize("query", [
    "days=7", "days=abc", "days=", "days=90&days=30", "level=admin", "level=people&level=people",
    "person=p1234567890ab", "project=DEMO&project=AIL", "v=1",
])
def test_bad_parameters_are_422(client: TestClient, fresh: Settings, query: str) -> None:
    ada = by_name(client)["Ada Brooks"]["id"]
    level = "" if "level=" in query else "&level=people"
    response = client.get(f"/api/v1/people/{ada}/card?{query}{level}")
    assert response.status_code == 422, response.text
    assert "Ada Brooks" not in response.text and entries(fresh) == []


@pytest.mark.parametrize("person_id", [GONE, "pBC7FE62842CA", "p0123456789a", "p0123456789abc", "unassigned", "Ada"])
def test_a_person_not_on_the_board_or_not_a_person_id_is_404(
    client: TestClient, fresh: Settings, person_id: str
) -> None:
    response = client.get(f"/api/v1/people/{person_id}/card", params={"level": "people"})
    assert response.status_code == 404 and response.json() == {"detail": "nobody by that id is on this board"}
    assert entries(fresh) == []


def test_a_board_this_service_does_not_serve_is_404_unknown_board(client: TestClient, fresh: Settings) -> None:
    """A board not served and a person not on a served board are both 404s; the code tells them apart (the dashboard
    never reads the words), so a card on a board taken out of JIRA_PROJECTS never says the person is not on it."""
    ada = by_name(client)["Ada Brooks"]["id"]
    response = client.get(f"/api/v1/people/{ada}/card", params={"level": "people", "project": "NOPE"})
    assert response.status_code == 404
    assert response.json() == {"detail": "that board is not one this service serves", "code": "unknown_board"}
    absent = client.get(f"/api/v1/people/{GONE}/card", params={"level": "people", "project": "AIL"})
    assert absent.status_code == 404 and "code" not in absent.json()
    # Every board-scoped route answers a board it does not serve the same way.
    for path in ("/people?project=NOPE", "/people/directory?project=NOPE", "/people/scorecard?level=people&project=NOPE",
                 f"/people/{ada}/avatar?project=NOPE"):
        refused = client.get(f"/api/v1{path}")
        assert (refused.status_code, refused.json().get("code")) == (404, "unknown_board"), path
    assert entries(fresh) == []


def test_the_card_is_never_cached_and_needs_the_service_key(client: TestClient, two: Settings) -> None:
    ada = by_name(client)["Ada Brooks"]["id"]
    response = client.get(f"/api/v1/people/{ada}/card", params={"level": "people"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    keyless = TestClient(create_app(two, provider=ScriptedProvider(), now=lambda: NOW))
    assert keyless.get(f"/api/v1/people/{ada}/card", params={"level": "people"}).status_code == 401


def test_comparing_people_turned_off_still_shows_one_person_next_to_the_board(
    client: TestClient, two: Settings
) -> None:
    ada = by_name(client)["Ada Brooks"]["id"]
    off = client_of(two.model_copy(update={"people_comparison": "off"}))
    on_body, off_body = card(client, ada), card(off, ada)
    assert {**off_body, "meta": None} == {**on_body, "meta": None} and off_body["process"] is not None


def test_a_card_holds_no_account_id_and_no_ones_address_zone_or_picture_url(client: TestClient) -> None:
    for board in BOARDS:
        for entry in get(client, "/people/directory", {"project": board})["people"]:
            text = client.get(f"/api/v1/people/{entry['person']['id']}/card",
                              params={"level": "people", "project": board}).text
            for secret in ("acc-", "app-tempo", "@", "Europe/London", "avatarUrls", "accountId", "emailAddress",
                           "timeZone", "displayName", "http"):
                assert secret not in text, (board, secret)


# --- the access log: once per viewer, person and board in any ten minutes ---------------------------------------


def test_a_card_is_recorded_at_most_once_per_viewer_person_and_board_in_any_ten_minutes(
    two: Settings, fresh: Settings
) -> None:
    clock = Clock(NOW)
    admin, other = client_of(two, clock=clock), client_of(two, clock=clock, viewer=OTHER, name="Riley Chen")
    people = by_name(admin)
    ada, ben = people["Ada Brooks"]["id"], people["Ben Okafor"]["id"]

    def read(client: TestClient, person_id: str, board: str = "DEMO", days: int = 90) -> None:
        assert client.get(f"/api/v1/people/{person_id}/card",
                          params={"level": "people", "project": board, "days": days}).status_code == 200

    read(admin, ada)
    read(admin, ada)  # hovered again at once
    clock.at = NOW + timedelta(minutes=5)
    read(admin, ada, days=30)  # another period is the same card
    clock.at = NOW + PERSON_CARD_ONCE_IN  # exactly ten minutes after the entry: still held
    read(admin, ada)
    assert [(row["action"], row["subjects"], row["detail"], row["at"]) for row in entries(fresh)] == [
        ("person_card_view", [ada], {"days": 90}, to_iso(NOW))]

    clock.at = NOW + PERSON_CARD_ONCE_IN + timedelta(milliseconds=1)  # more than ten minutes: a new entry
    read(admin, ada)
    read(admin, ben)  # another person
    read(other, ada)  # another viewer
    read(admin, ada, board="AIL")  # another board
    found = entries(fresh)
    later = to_iso(clock.at)
    assert [(row["viewer_hash"], row["project_key"], row["subjects"], row["at"]) for row in found] == [
        (ADMIN, "DEMO", [ada], to_iso(NOW)),
        (ADMIN, "DEMO", [ada], later),
        (ADMIN, "DEMO", [ben], later),
        (OTHER, "DEMO", [ada], later),
        (ADMIN, "AIL", [ada], later),
    ]
    assert all((row["action"], row["level"]) == (PERSON_CARD_VIEW, "people") for row in found)
    assert found[0]["viewer_name"] == ADMIN_NAME and found[3]["viewer_name"] == "Riley Chen"


def test_two_reads_at_once_leave_one_entry(two: Settings, fresh: Settings) -> None:
    """Two card reads at the same moment (the hover's fetch and the click's, say): the look and the write are one
    write transaction, so exactly one of them writes the entry, whichever comes first."""
    access = AccessLog(two.db_file, days=365, now=lambda: NOW)
    viewer = Viewer(hash=ADMIN, name=ADMIN_NAME)
    ada = pid("acc-01")
    start = threading.Barrier(8)
    written: list[str | None] = []
    failed: list[BaseException] = []

    def read() -> None:
        try:
            start.wait()
            written.append(access.record(viewer=viewer, level="people", project="DEMO", action=PERSON_CARD_VIEW,
                                         subjects=[ada], detail={"days": 90}, once_within=PERSON_CARD_ONCE_IN))
        except BaseException as exc:  # the test fails below, naming it
            failed.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert failed == [] and len(written) == 8
    assert len([entry for entry in written if entry is not None]) == 1
    assert [(row["id"], row["subjects"]) for row in entries(fresh)] == [
        (next(entry for entry in written if entry is not None), [ada])]


def test_a_refused_card_read_records_nothing(client: TestClient, two: Settings, fresh: Settings) -> None:
    ada = by_name(client)["Ada Brooks"]["id"]
    unnamed = client_of(two, viewer=None, name=None)
    assert client.get(f"/api/v1/people/{ada}/card").status_code == 403
    assert client.get(f"/api/v1/people/{GONE}/card", params={"level": "people"}).status_code == 404
    assert client.get(f"/api/v1/people/{ada}/card", params={"level": "people", "days": 7}).status_code == 422
    response = unnamed.get(f"/api/v1/people/{ada}/card", params={"level": "people"})  # no viewer named
    assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["header", "X-Oversight-Viewer"]
    assert "Ada Brooks" not in response.text
    assert entries(fresh) == []


def test_when_the_entry_cannot_be_written_the_card_is_not_served(
    client: TestClient, fresh: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ada = by_name(client)["Ada Brooks"]["id"]

    def unwritable(_path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(access_log_module, "_connect", unwritable)
    response = client.get(f"/api/v1/people/{ada}/card", params={"level": "people"})
    assert response.status_code == 500 and response.json() == UNAVAILABLE
    assert "Ada Brooks" not in response.text and ada not in response.text


def test_when_the_log_cannot_be_looked_at_the_card_is_not_served(
    client: TestClient, fresh: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer holds the snapshot past the busy timeout: the look for a recent entry cannot be made, so
    nothing is served, rather than a card that may go unrecorded."""
    ada = by_name(client)["Ada Brooks"]["id"]
    real = access_log_module._connect

    def impatient(path: Path) -> sqlite3.Connection:
        conn = real(path)
        conn.execute("PRAGMA busy_timeout = 0")
        return conn

    monkeypatch.setattr(access_log_module, "_connect", impatient)
    holder = sqlite3.connect(fresh.db_file, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        response = client.get(f"/api/v1/people/{ada}/card", params={"level": "people"})
        assert response.status_code == 500 and response.json() == UNAVAILABLE
        holder.execute("ROLLBACK")
    finally:
        holder.close()
    assert entries(fresh) == []
    assert client.get(f"/api/v1/people/{ada}/card", params={"level": "people"}).status_code == 200  # free again
    assert len(entries(fresh)) == 1


def test_a_repeat_read_needs_no_write_lock(
    client: TestClient, fresh: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name hovered again within ten minutes finds its entry with a plain read: it is served even while another
    writer (a sync, say) holds the snapshot, and writes nothing."""
    ada = by_name(client)["Ada Brooks"]["id"]
    assert client.get(f"/api/v1/people/{ada}/card", params={"level": "people"}).status_code == 200
    real = access_log_module._connect

    def impatient(path: Path) -> sqlite3.Connection:
        conn = real(path)
        conn.execute("PRAGMA busy_timeout = 0")
        return conn

    monkeypatch.setattr(access_log_module, "_connect", impatient)
    holder = sqlite3.connect(fresh.db_file, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        assert client.get(f"/api/v1/people/{ada}/card", params={"level": "people"}).status_code == 200
        ben = by_name(client)["Ben Okafor"]["id"]  # someone not read yet: an entry to write, which must wait
        assert client.get(f"/api/v1/people/{ben}/card", params={"level": "people"}).json() == UNAVAILABLE
        holder.execute("ROLLBACK")
    finally:
        holder.close()
    assert [row["subjects"] for row in entries(fresh)] == [[ada]]


def test_the_access_log_lists_card_views_and_filters_by_them(client: TestClient, fresh: Settings) -> None:
    assert PERSON_CARD_VIEW in ACTIONS
    ada = by_name(client)["Ada Brooks"]
    assert client.get(f"/api/v1/people/{ada['id']}/card", params={"level": "people", "days": 30}).status_code == 200
    assert client.get("/api/v1/people/scorecard", params={"level": "people", "person": ada["id"]}).status_code == 200
    body = get(client, "/access-log", {"level": "people", "action": PERSON_CARD_VIEW})
    assert [(entry["action"], entry["project"], entry["level"], entry["subjects"], entry["detail"])
            for entry in body["entries"]] == [(PERSON_CARD_VIEW, "DEMO", "people", [ada], {"days": 30})]
    assert body["entries"][0]["viewer"] == {"hash": ADMIN, "name": ADMIN_NAME}
