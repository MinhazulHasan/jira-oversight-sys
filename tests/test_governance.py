"""The access log: every read of person-level data through the API is recorded and read back."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from structlog.testing import capture_logs

from oversight.api import access_log as access_log_module
from oversight.api.access_log import (
    ACTIONS,
    FORMER_MEMBER,
    LOCAL_VIEWER_NAME,
    SIGNED_IN,
    decode_cursor,
    encode_cursor,
    viewer_name,
)
from oversight.api.app import create_app, prepare_snapshot
from oversight.ask.analyst import QUERY_TOOL
from oversight.llm.scripted import ScriptedProvider
from oversight.settings import Settings
from oversight.store.db import SCHEMA_VERSION, Store, open_readonly
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES
from tests.test_analyst import FakeAnalyst, turn, use
from tests.test_api_ask_insights import _seed
from tests.test_api_overview import KEY, make_settings
from tests.test_db import VERSION_8

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
ADMIN = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"
ADMIN_NAME = "Morgan Reyes"
UNAVAILABLE = {"detail": "the access log could not be written, so nothing was served", "code": "access_log_unavailable"}
GONE = "p000000000000"


class Clock:
    """A clock a test can move."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def headers(viewer: str | None = ADMIN, name: str | None = ADMIN_NAME) -> dict[str, str]:
    found = {"X-Service-Key": KEY}
    if viewer is not None:
        found["X-Oversight-Viewer"] = viewer
    if name is not None:
        found["X-Oversight-Viewer-Name"] = quote(name)
    return found


def client_of(settings: Settings, *, clock: Clock | None = None, **sent: Any) -> TestClient:
    app = create_app(settings, provider=ScriptedProvider(), now=clock or (lambda: NOW))
    return TestClient(app, headers=headers(**sent))


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    settings = make_settings(tmp_path_factory.mktemp("governance"))
    _seed(settings, briefing=False)
    return settings


@pytest.fixture(scope="module")
def client(demo: Settings) -> TestClient:
    return client_of(demo)


@pytest.fixture
def log(demo: Settings) -> Settings:
    """The demo with an empty access log, for a test that counts its entries."""
    conn = sqlite3.connect(demo.db_file)
    try:
        conn.execute("DELETE FROM access_log")
        conn.commit()
    finally:
        conn.close()
    return demo


def entries(settings: Settings) -> list[dict[str, Any]]:
    """The access log's rows, oldest first, their JSON read."""
    conn = open_readonly(settings.db_file)
    try:
        rows = [dict(row) for row in conn.execute("SELECT * FROM access_log ORDER BY at, rowid")]
    finally:
        conn.close()
    for row in rows:
        row["subjects"], row["detail"] = json.loads(row.pop("subject_ids_json")), json.loads(row.pop("detail_json"))
    return rows


def count(settings: Settings, table: str) -> int:
    conn = open_readonly(settings.db_file)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def people(client: TestClient) -> dict[str, dict[str, Any]]:
    """The board's people by name (the directory's person objects)."""
    return {entry["person"]["name"]: entry["person"] for entry in client.get("/api/v1/people/directory").json()["people"]}


def add(settings: Settings, *rows: dict[str, Any]) -> None:
    """Entries written straight into the log, as earlier reads left them."""
    with Store.open(settings.db_file) as store:
        for row in rows:
            store.upsert("access_log", {
                "viewer_hash": ADMIN, "viewer_name": ADMIN_NAME, "level": "people", "project_key": "DEMO",
                "action": "scorecard_view", "subject_ids_json": [], "detail_json": {"days": 90}, **row,
            })


def test_one_persons_scorecard_is_a_scorecard_view_and_everyone_or_several_a_comparison(
    client: TestClient, log: Settings
) -> None:
    board = people(client)
    ada, ben = board["Ada Brooks"]["id"], board["Ben Okafor"]["id"]

    one = client.get("/api/v1/people/scorecard", params={"level": "people", "person": ada})
    assert one.status_code == 200 and one.headers["cache-control"] == "no-store"
    everyone = client.get("/api/v1/people/scorecard", params={"level": "people", "days": 30}).json()
    two = client.get("/api/v1/people/scorecard", params=[("level", "people"), ("person", ben), ("person", ada)])
    assert two.status_code == 200

    found = entries(log)
    assert [(row["action"], row["subjects"], row["detail"]) for row in found] == [
        ("scorecard_view", [ada], {"days": 90}),
        ("people_compare_view", [row["person"]["id"] for row in everyone["people"]], {"days": 30}),
        ("people_compare_view", [row["person"]["id"] for row in two.json()["people"]], {"days": 90}),
    ]
    assert len(found[1]["subjects"]) >= 5 and sorted(found[2]["subjects"]) == sorted([ada, ben])
    for row in found:
        assert (row["viewer_hash"], row["viewer_name"], row["level"], row["project_key"]) == (
            ADMIN, ADMIN_NAME, "people", "DEMO")
        assert row["at"] == to_iso(NOW) and len(row["id"]) == 32


def test_a_refused_or_failed_scorecard_read_records_nothing(client: TestClient, log: Settings) -> None:
    ada = people(client)["Ada Brooks"]["id"]
    assert client.get("/api/v1/people/scorecard", params={"person": ada}).json()["code"] == "people_level_only"
    assert client.get("/api/v1/people/scorecard", params={"level": "people", "person": GONE}).status_code == 404
    assert client.get("/api/v1/people/scorecard", params={"level": "people", "days": 7}).status_code == 422
    assert entries(log) == []


def test_with_comparison_off_the_boards_figures_alone_record_nothing(client: TestClient, log: Settings) -> None:
    ada = people(client)["Ada Brooks"]["id"]
    off = client_of(log.model_copy(update={"people_comparison": "off"}))
    board_only = off.get("/api/v1/people/scorecard", params={"level": "people"})
    assert board_only.status_code == 200
    assert board_only.json()["comparison"] == "off" and board_only.json()["people"] == []
    assert entries(log) == []

    assert off.get("/api/v1/people/scorecard", params={"level": "people", "person": ada}).status_code == 200
    assert [(row["action"], row["subjects"]) for row in entries(log)] == [("scorecard_view", [ada])]


def test_findings_for_a_person_are_recorded_and_other_lists_are_not(client: TestClient, log: Settings) -> None:
    ada = people(client)["Ada Brooks"]["id"]
    assert client.get("/api/v1/findings").status_code == 200
    assert client.get("/api/v1/findings", params={"person": "unassigned"}).status_code == 200
    assert entries(log) == []

    assert client.get("/api/v1/findings", params=[("person", ada), ("person", "unassigned"), ("state", "all"),
                                                  ("level", "people")]).status_code == 200
    assert client.get("/api/v1/findings", params={"person": ada, "page": 2}).status_code == 200
    found = entries(log)
    assert [(row["action"], row["level"], row["subjects"], row["detail"]) for row in found] == [
        ("person_findings_view", "people", [ada], {"state": "all", "page": 1}),
        ("person_findings_view", "team", [ada], {"state": "open", "page": 2}),
    ]


def test_a_people_level_question_about_people_is_recorded_with_its_qa_id(client: TestClient, log: Settings) -> None:
    ada = people(client)["Ada Brooks"]["id"]
    mentioned = client.post("/api/v1/ask", json={"question": "What is @Ada Brooks working on?", "level": "people",
                                                 "mentions": [ada]}).json()
    ranked = client.post("/api/v1/ask", json={"question": "Who has the most open breaches?", "level": "people"}).json()
    assert ranked["outcome"] == "answered" and ranked["people"]
    carmen = people(client)["Carmen Ruiz"]["id"]
    typed = client.post("/api/v1/ask", json={"question": "How many open breaches does Carmen Ruiz have?",
                                             "level": "people"}).json()

    found = entries(log)
    assert [row["action"] for row in found] == ["person_ask", "person_ask", "person_ask"]
    assert found[2]["subjects"][0] == carmen and found[2]["detail"]["qa_id"] == typed["qa_id"]
    assert found[0]["subjects"][0] == ada
    assert found[0]["detail"] == {"qa_id": mentioned["qa_id"], "outcome": mentioned["outcome"]}
    assert found[1]["detail"] == {"qa_id": ranked["qa_id"], "outcome": "answered"}
    assert {who["id"] for who in ranked["people"]} <= set(found[1]["subjects"])
    assert all(row["project_key"] == "DEMO" and row["level"] == "people" for row in found)


TOP_THREE = ("SELECT name, open_breaches FROM people WHERE person_id IS NOT NULL"
             " ORDER BY open_breaches DESC, name LIMIT 3")
EVERYONE = "SELECT name, open_breaches FROM people WHERE person_id IS NOT NULL ORDER BY open_breaches DESC, name"
BY_ASSIGNEE = ("SELECT assignee, COUNT(*) AS open_tickets FROM issues WHERE status_category != 'done'"
               " AND assignee IS NOT NULL GROUP BY assignee")


def ask_the_analyst(settings: Settings, question: str, *turns: Any) -> dict[str, Any]:
    app = create_app(settings, provider=FakeAnalyst(list(turns)), now=lambda: NOW)
    reply = TestClient(app, headers=headers()).post("/api/v1/ask", json={"question": question, "level": "people"})
    assert reply.status_code == 200, reply.text
    body: dict[str, Any] = reply.json()
    assert body["plan"] and all(step["tool"] == QUERY_TOOL for step in body["plan"])
    return body


def named_in_facts(body: dict[str, Any], board: dict[str, dict[str, Any]], column: str) -> list[str]:
    return [board[row[column]]["id"] for facts in body["facts"] for row in facts["rows"]]


def test_an_analysts_names_without_ids_are_recorded_as_the_people_they_name(client: TestClient, log: Settings) -> None:
    board = people(client)
    quiet = ask_the_analyst(log, "Who has the most open breaches?",
                            turn(uses=[use(QUERY_TOOL, {"sql": TOP_THREE})]),
                            turn(text="The table lists them, most open breaches first."))
    assert quiet["outcome"] == "answered" and quiet["people"] == []
    top = named_in_facts(quiet, board, "name")
    assert len(top) == 3

    one_named = ask_the_analyst(log, "Who has the most open breaches of all?",
                                turn(uses=[use(QUERY_TOOL, {"sql": EVERYONE})]),
                                turn(text=f"{quiet['facts'][0]['rows'][0]['name']} has the most open breaches."))
    everyone = named_in_facts(one_named, board, "name")
    assert one_named["outcome"] == "answered" and [who["id"] for who in one_named["people"]] == everyone[:1]

    by_assignee = ask_the_analyst(log, "How are open tickets spread across the board?",
                                  turn(uses=[use(QUERY_TOOL, {"sql": BY_ASSIGNEE})]),
                                  turn(text="The table lists everyone who holds open tickets, with their counts."))
    holders = named_in_facts(by_assignee, board, "assignee")

    found = entries(log)
    assert [(row["action"], row["detail"]["outcome"]) for row in found] == [("person_ask", "answered")] * 3
    assert found[0]["subjects"] == top and found[0]["detail"]["qa_id"] == quiet["qa_id"]
    assert found[1]["subjects"] == everyone and len(everyone) >= 5
    assert sorted(found[2]["subjects"]) == sorted(holders) and len(holders) >= 5


def test_an_analysts_read_is_recorded_when_the_answer_is_withheld(client: TestClient, log: Settings) -> None:
    board = people(client)
    withheld = ask_the_analyst(log, "Who has the most open breaches?",
                               turn(uses=[use(QUERY_TOOL, {"sql": TOP_THREE})]),
                               turn(text="Someone has 987654 open breaches."),
                               turn(text="Someone still has 987654 open breaches."))
    assert withheld["outcome"] == "not_verified" and withheld["answer"] is None and withheld["facts"]
    found = entries(log)
    assert [(row["action"], row["detail"]) for row in found] == [
        ("person_ask", {"qa_id": withheld["qa_id"], "outcome": "not_verified"})]
    assert found[0]["subjects"] == named_in_facts(withheld, board, "name")


def test_an_analysts_query_over_people_is_recorded_even_when_it_names_no_one(client: TestClient, log: Settings) -> None:
    listed = ask_the_analyst(log, "Who is on the board?",
                             turn(uses=[use(QUERY_TOOL, {"sql": "SELECT GROUP_CONCAT(name, ', ') AS everyone"
                                                                " FROM people WHERE person_id IS NOT NULL"})]),
                             turn(text="The table lists everyone who holds a ticket."))
    counted = ask_the_analyst(log, "How many people have open breaches?",
                              turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM people"
                                                                 " WHERE open_breaches > 0"})]),
                              turn(text="The count is in the table."))
    epics = ask_the_analyst(log, "How many epics are there?",
                            turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"})]),
                            turn(text="The count is in the table."))
    assert listed["outcome"] == counted["outcome"] == epics["outcome"] == "answered"
    found = entries(log)
    assert [row["detail"]["qa_id"] for row in found] == [listed["qa_id"], counted["qa_id"]]
    assert len(found[0]["subjects"]) >= 5
    assert found[1]["subjects"] == []


def test_questions_that_read_no_ones_data_are_not_recorded(client: TestClient, log: Settings) -> None:
    for body in ({"question": "Where do tickets get stuck most?", "level": "people"},
                 {"question": "Which epics have the most open breaches?"},
                 {"question": "   ", "level": "people"}):
        assert client.post("/api/v1/ask", json=body).status_code == 200
    assert entries(log) == []


def test_a_read_that_names_no_viewer_is_refused_before_anything_is_read(demo: Settings, log: Settings) -> None:
    unnamed = client_of(demo, viewer=None, name=None)
    ada = people(unnamed)["Ada Brooks"]["id"]
    questions = count(demo, "qa_log")
    for response in (
        unnamed.get("/api/v1/people/scorecard", params={"level": "people", "person": ada}),
        unnamed.get("/api/v1/findings", params={"person": ada}),
        unnamed.post("/api/v1/ask", json={"question": "How is @Ada Brooks doing?", "level": "people", "mentions": [ada]}),
        unnamed.get("/api/v1/access-log", params={"level": "people"}),
    ):
        assert response.status_code == 422, response.text
        assert response.json()["detail"][0]["loc"] == ["header", "X-Oversight-Viewer"]
        assert "Ada Brooks" not in response.text
    assert entries(log) == [] and count(demo, "qa_log") == questions
    assert unnamed.get("/api/v1/findings").status_code == 200
    assert unnamed.post("/api/v1/ask", json={"question": "Where do tickets get stuck most?"}).status_code == 200


@pytest.mark.parametrize("viewer", ["ADMIN", "0123456789ABCDEF0123456789ABCDEF", "0123", "Local", "x" * 32])
def test_a_viewer_that_is_not_a_hash_or_local_is_refused(demo: Settings, log: Settings, viewer: str) -> None:
    named = client_of(demo, viewer=viewer)
    response = named.get("/api/v1/people/scorecard", params={"level": "people"})
    assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["header", "X-Oversight-Viewer"]
    assert entries(log) == []


def test_sign_in_off_is_the_local_viewer_whatever_name_is_sent(demo: Settings, log: Settings) -> None:
    local = client_of(demo, viewer="local", name="Someone Else")
    assert local.get("/api/v1/people/scorecard", params={"level": "people"}).status_code == 200
    assert [(row["viewer_hash"], row["viewer_name"]) for row in entries(log)] == [("local", LOCAL_VIEWER_NAME)]


@pytest.mark.parametrize(("sent", "kept"), [
    (quote("Zoë O'Brien"), "Zoë O'Brien"),
    (quote("  Morgan \t Reyes\n"), "Morgan Reyes"),
    (quote("Morgan‮Reyes"), "MorganReyes"),
    ("Morgan Reyes", "Morgan Reyes"),
    (None, SIGNED_IN),
    ("", SIGNED_IN),
    (quote("morgan.reyes@example.com"), SIGNED_IN),
    (quote("Morgan (morgan@example.com)"), SIGNED_IN),
    ("5b10ac8d82e05b22cc7d4ef5", SIGNED_IN),
    ("557058%3Af58131cb-b67d-43c7-b30d-6b58d40bd077", SIGNED_IN),
    ("%FF%FE", SIGNED_IN),
    (quote("M" * 129), SIGNED_IN),
])
def test_the_viewer_is_kept_by_display_name_never_by_address_or_id(sent: str | None, kept: str) -> None:
    assert viewer_name(sent, ADMIN) == kept
    assert viewer_name(sent, "local") == LOCAL_VIEWER_NAME


def test_when_the_entry_cannot_be_written_nothing_is_served(
    client: TestClient, log: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ada = people(client)["Ada Brooks"]["id"]
    questions = count(log, "qa_log")

    def unwritable(_path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("disk I/O error")

    writable = access_log_module._connect
    monkeypatch.setattr(access_log_module, "_connect", unwritable)
    for response in (
        client.get("/api/v1/people/scorecard", params={"level": "people", "person": ada}),
        client.get("/api/v1/people/scorecard", params={"level": "people"}),
        client.get("/api/v1/findings", params={"person": ada}),
        client.post("/api/v1/ask", json={"question": "What is @Ada Brooks working on?", "level": "people",
                                         "mentions": [ada]}),
        client.get("/api/v1/access-log", params={"level": "people"}),
    ):
        assert response.status_code == 500 and response.json() == UNAVAILABLE, response.text
        assert "Ada Brooks" not in response.text and ada not in response.text
    assert count(log, "qa_log") == questions + 1
    monkeypatch.setattr(access_log_module, "_connect", writable)
    assert entries(log) == []
    assert client.get("/api/v1/overview").status_code == 200


def test_the_log_lines_carry_ids_and_counts_never_a_name(client: TestClient, log: Settings) -> None:
    ada = people(client)["Ada Brooks"]["id"]
    with capture_logs() as captured:
        assert client.get("/api/v1/people/scorecard", params={"level": "people", "person": ada}).status_code == 200
    logged = [line for line in captured if line["event"] == "access_logged"]
    assert len(logged) == 1
    assert {key: value for key, value in logged[0].items() if key not in ("event", "log_level", "logger")} == {
        "entry": entries(log)[0]["id"], "action": "scorecard_view", "at_level": "people", "project": "DEMO",
        "subjects": 1,
    }
    assert not [line for line in captured if "Ada Brooks" in repr(line) or ADMIN_NAME in repr(line)]


def at(minutes: int) -> str:
    """A stored time, minutes before NOW."""
    return to_iso(NOW - timedelta(minutes=minutes)) or ""


def test_the_log_newest_first_a_page_at_a_time(client: TestClient, log: Settings) -> None:
    board = people(client)
    ada, carmen = board["Ada Brooks"], board["Carmen Ruiz"]
    add(log, *({"id": f"{n:032x}", "at": at(60 - 10 * (n // 2))} for n in range(7)))
    add(log, {"id": "f" * 32, "at": at(5), "action": "people_compare_view", "subject_ids_json": [carmen["id"], GONE,
                                                                                                  ada["id"]]})
    expected = sorted((row["at"], row["id"]) for row in entries(log))[::-1]

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(3):
        params: dict[str, Any] = {"level": "people", "limit": 3, **({"cursor": cursor} if cursor else {})}
        response = client.get("/api/v1/access-log", params=params)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        body = response.json()
        assert set(body) == {"meta", "entries", "next_cursor", "facets"}
        assert (body["facets"] is None) is (cursor is not None)
        seen.extend(body["entries"])
        cursor = body["next_cursor"]
    assert cursor is None
    assert [(entry["at"], entry["id"]) for entry in seen] == expected
    # A page never holds its own entry: the read is recorded after it.
    assert [row["action"] for row in entries(log)][-2:] == ["people_compare_view", "access_log_view"]

    first = seen[0]
    assert set(first) == {"id", "at", "viewer", "level", "project", "action", "subjects", "detail"}
    assert first["viewer"] == {"hash": ADMIN, "name": ADMIN_NAME}
    assert (first["level"], first["project"], first["action"], first["detail"]) == (
        "people", "DEMO", "people_compare_view", {"days": 90})
    assert first["subjects"] == [carmen, {"id": GONE, "name": FORMER_MEMBER, "avatar": None}, ada]


def test_the_filters(client: TestClient, log: Settings) -> None:
    ada = people(client)["Ada Brooks"]["id"]
    add(log,
        {"id": "a" * 32, "at": "2026-11-02T23:59:59.999Z", "subject_ids_json": [ada]},
        {"id": "b" * 32, "at": "2026-11-03T00:00:00.000Z", "action": "person_ask", "viewer_hash": OTHER,
         "viewer_name": "Sam Lee", "detail_json": {"qa_id": "c" * 32, "outcome": "answered"}},
        {"id": "c" * 32, "at": "2026-11-03T23:59:59.999Z", "action": "person_findings_view", "subject_ids_json": [ada],
         "detail_json": {"state": "open", "page": 1}},
        {"id": "d" * 32, "at": "2026-11-04T00:00:00.000Z", "action": "people_compare_view"})

    def ids(**params: Any) -> list[str]:
        response = client.get("/api/v1/access-log", params={"level": "people", **params})
        assert response.status_code == 200, response.text
        return [entry["id"][0] for entry in response.json()["entries"] if entry["action"] != "access_log_view"]

    assert ids() == ["d", "c", "b", "a"]
    assert ids(**{"from": "2026-11-03", "to": "2026-11-03"}) == ["c", "b"]
    assert ids(**{"from": "2026-11-03"}) == ["d", "c", "b"] and ids(to="2026-11-02") == ["a"]
    assert ids(person=ada) == ["c", "a"]
    assert ids(viewer=OTHER) == ["b"]
    assert ids(action=["person_ask", "person_findings_view"]) == ["c", "b"]
    assert ids(action="person_ask", viewer=ADMIN) == []


def test_a_first_page_offers_every_viewer_and_person_the_log_holds_whatever_the_filters(
    client: TestClient, log: Settings
) -> None:
    board = people(client)
    ada, carmen = board["Ada Brooks"], board["Carmen Ruiz"]
    add(log,
        {"id": "a" * 32, "at": at(50), "subject_ids_json": [ada["id"], GONE]},
        {"id": "b" * 32, "at": at(40), "viewer_hash": OTHER, "viewer_name": "Sam Lee (old name)",
         "subject_ids_json": [carmen["id"]]},
        {"id": "c" * 32, "at": at(30), "viewer_hash": OTHER, "viewer_name": "Sam Lee", "action": "person_ask",
         "detail_json": {"qa_id": "c" * 32, "outcome": "answered"}})
    first = client.get("/api/v1/access-log", params={"level": "people", "viewer": OTHER, "action": "person_ask",
                                                     "limit": 1}).json()
    assert [entry["id"] for entry in first["entries"]] == ["c" * 32]
    assert first["facets"] == {
        "viewers": [{"hash": ADMIN, "name": ADMIN_NAME}, {"hash": OTHER, "name": "Sam Lee"}],
        "subjects": [ada, carmen, {"id": GONE, "name": FORMER_MEMBER, "avatar": None}],
    }
    assert client.get("/api/v1/access-log", params={"level": "people", "export": "true"}).json()["facets"] is None


def test_a_day_no_zone_can_hold_is_a_422_not_a_fault(demo: Settings, log: Settings) -> None:
    tokyo = client_of(demo.model_copy(update={"report_timezone": "Asia/Tokyo"}))
    for params in ({"to": "9999-12-31"}, {"from": "0001-01-01"}, {"from": "1999-12-31"}, {"to": "2101-01-01"}):
        response = tokyo.get("/api/v1/access-log", params={"level": "people", **params})
        assert response.status_code == 422, (params, response.text)
        assert "must be a day from 2000-01-01 to 2100-12-31" in response.text
    assert tokyo.get("/api/v1/access-log", params={"level": "people", "from": "2000-01-01",
                                                   "to": "2100-12-31"}).status_code == 200
    assert [row["action"] for row in entries(log)] == ["access_log_view"]


def test_the_days_are_cut_in_the_organisations_zone(demo: Settings, log: Settings) -> None:
    chicago = client_of(demo.model_copy(update={"report_timezone": "America/Chicago"}))
    add(log, {"id": "a" * 32, "at": "2026-11-04T05:59:59.999Z"}, {"id": "b" * 32, "at": "2026-11-04T06:00:00.000Z"})
    body = chicago.get("/api/v1/access-log", params={"level": "people", "from": "2026-11-04", "to": "2026-11-04"}).json()
    assert [entry["id"] for entry in body["entries"]] == ["b" * 32]  # midnight in Chicago is 06:00 UTC that day
    assert body["meta"]["report_timezone"] == "America/Chicago"


def test_reloading_the_log_never_fills_it_with_its_own_views(demo: Settings, log: Settings) -> None:
    clock = Clock(NOW)
    reader = client_of(demo, clock=clock)
    ada = people(reader)["Ada Brooks"]["id"]

    def views() -> list[tuple[str, str, list[str]]]:
        return [(row["action"], row["viewer_hash"], row["subjects"]) for row in entries(log)
                if row["action"] in ("access_log_view", "access_log_export")]

    for _reload in range(5):
        assert reader.get("/api/v1/access-log", params={"level": "people"}).status_code == 200
    assert reader.get("/api/v1/access-log", params={"level": "people", "action": "person_ask"}).status_code == 200
    assert views() == [("access_log_view", ADMIN, [])]
    assert reader.get("/api/v1/access-log", params={"level": "people", "person": ada}).status_code == 200
    other = client_of(demo, clock=clock, viewer=OTHER, name="Sam Lee")
    assert other.get("/api/v1/access-log", params={"level": "people"}).status_code == 200
    for _export in range(2):
        assert reader.get("/api/v1/access-log", params={"level": "people", "export": "true"}).status_code == 200
    assert views() == [("access_log_view", ADMIN, []), ("access_log_view", ADMIN, [ada]),
                       ("access_log_view", OTHER, []), ("access_log_export", ADMIN, []),
                       ("access_log_export", ADMIN, [])]
    first = next(row for row in entries(log) if row["action"] == "access_log_view")
    assert first["detail"]["actions"] == []

    clock.at = NOW + timedelta(minutes=10, milliseconds=1)
    assert reader.get("/api/v1/access-log", params={"level": "people"}).status_code == 200
    assert views()[-1] == ("access_log_view", ADMIN, []) and len(views()) == 6


def test_reading_the_log_is_recorded_and_an_export_as_an_export(client: TestClient, log: Settings) -> None:
    ada = people(client)["Ada Brooks"]["id"]
    add(log, *({"id": f"{n:032x}", "at": at(n + 1), "subject_ids_json": [ada]} for n in range(260)))
    page = client.get("/api/v1/access-log", params={"level": "people", "person": ada, "action": "scorecard_view",
                                                    "from": "2026-10-01", "to": "2026-11-04"}).json()
    assert len(page["entries"]) == 50 and page["next_cursor"] is not None
    export = client.get("/api/v1/access-log", params={"level": "people", "person": ada, "export": "true"}).json()
    assert len(export["entries"]) == 261 and export["next_cursor"] is None

    view, exported = entries(log)[-2:]
    assert (view["action"], view["subjects"], view["project_key"], view["level"]) == (
        "access_log_view", [ada], None, "people")
    assert view["detail"] == {"from": "2026-10-01", "to": "2026-11-04", "actions": ["scorecard_view"], "viewer": None,
                              "entries": 50}
    assert (exported["action"], exported["subjects"]) == ("access_log_export", [ada])
    assert exported["detail"] == {"from": None, "to": None, "actions": [], "viewer": None, "entries": 261}


# The detail each action is recorded with, in its table.
UI_CONTRACT = Path(__file__).resolve().parents[2] / "jira-oversight-ui" / "docs" / "API_CONTRACT.md"


@pytest.mark.skipif(not UI_CONTRACT.exists(), reason="the dashboard's repository is not beside this one")
def test_each_actions_detail_has_exactly_the_keys_of_the_dashboards_contract(client: TestClient, log: Settings) -> None:
    table = re.findall(r"^\| `(\w+)` \| [^|]+ \| `\{([^}]*)\}` \|$", UI_CONTRACT.read_text(encoding="utf-8"), re.M)
    contract = {action: {key.strip() for key in keys.split(",")} for action, keys in table}
    assert set(contract) == {"scorecard_view", "people_compare_view", "person_findings_view", "person_ask",
                             "access_log_view", "access_log_export", "person_card_view"}
    ada = people(client)["Ada Brooks"]["id"]
    assert client.get("/api/v1/people/scorecard", params={"level": "people", "person": ada}).status_code == 200
    assert client.get(f"/api/v1/people/{ada}/card", params={"level": "people"}).status_code == 200
    assert client.get("/api/v1/people/scorecard", params={"level": "people"}).status_code == 200
    assert client.get("/api/v1/findings", params={"person": ada, "level": "people"}).status_code == 200
    assert client.post("/api/v1/ask", json={"question": "What is @Ada Brooks working on?", "level": "people",
                                            "mentions": [ada]}).status_code == 200
    assert client.get("/api/v1/access-log", params={"level": "people"}).status_code == 200
    assert client.get("/api/v1/access-log", params={"level": "people", "export": "true"}).status_code == 200
    assert {row["action"]: set(row["detail"]) for row in entries(log)} == contract


@pytest.mark.parametrize("params", [
    {"limit": "0"}, {"limit": "201"}, {"limit": "²"}, {"limit": "ten"}, {"limit": "-1"},
    {"limit": "10001", "export": "true"}, {"export": "maybe"},
    {"from": "2026-13-01"}, {"from": "20261104"}, {"to": "2026-11-04T00:00:00Z"}, {"from": "2026-11-05", "to": "2026-11-04"},
    {"action": "deleted"}, {"person": "Ada Brooks"}, {"person": "unassigned"}, {"viewer": "ADMIN"},
    {"cursor": "not-a-cursor"}, {"cursor": encode_cursor("yesterday", "a" * 32)},
    {"page": "2"}, {"level": "admin"},
])
def test_a_bad_parameter_is_a_422(client: TestClient, log: Settings, params: dict[str, str]) -> None:
    response = client.get("/api/v1/access-log", params={"level": "people", **params})
    assert response.status_code == 422, response.text
    assert entries(log) == []


def test_a_parameter_given_twice_is_a_422_but_action_repeats(client: TestClient, log: Settings) -> None:
    twice = client.get("/api/v1/access-log", params=[("level", "people"), ("limit", "5"), ("limit", "6")])
    assert twice.status_code == 422 and twice.json()["detail"][0]["type"] == "repeated"
    actions = [("action", action) for action in ACTIONS]
    assert client.get("/api/v1/access-log", params=[("level", "people"), *actions]).status_code == 200


def test_the_access_log_and_usage_are_people_level_only(client: TestClient, log: Settings) -> None:
    for path in ("/api/v1/access-log", "/api/v1/usage"):
        for params in ({}, {"level": "team"}):
            response = client.get(path, params=params)
            assert response.status_code == 403, (path, response.text)
            assert response.json()["code"] == "people_level_only" and set(response.json()) == {"detail", "code"}
    assert entries(log) == []


def test_a_cursor_names_one_entry_and_nothing_else() -> None:
    cursor = encode_cursor("2026-11-04T14:30:00.000Z", "a" * 32)
    assert decode_cursor(cursor) == ("2026-11-04T14:30:00.000Z", "a" * 32)
    for bad in ("", "x" * 129, "!!!", encode_cursor("2026-11-04T14:30:00Z", "a" * 32), encode_cursor("2026", "A" * 32)):
        assert decode_cursor(bad) is None


def small(tmp_path: Path, **update: Any) -> Settings:
    settings = Settings(_env_file=None, jira_projects="DEMO", api_service_key=KEY, db_path=tmp_path / "oversight.db",
                        config_dir=CONFIG_FIXTURES, reports_dir=tmp_path / "reports", **update)
    Store.open(settings.db_file).close()
    return settings


def test_entries_older_than_access_log_days_are_deleted_at_start(tmp_path: Path) -> None:
    settings = small(tmp_path, access_log_days=30)
    add(settings, {"id": "a" * 32, "at": to_iso(NOW - timedelta(days=30, seconds=1))},
        {"id": "b" * 32, "at": to_iso(NOW - timedelta(days=30))}, {"id": "c" * 32, "at": to_iso(NOW)})
    prepare_snapshot(settings.db_file, access_log_days=30, now=lambda: NOW)
    assert [row["id"][0] for row in entries(settings)] == ["b", "c"]


def test_and_once_a_day_while_the_api_runs(tmp_path: Path) -> None:
    settings = small(tmp_path, access_log_days=30)
    clock = Clock(NOW)
    api = client_of(settings, clock=clock)
    add(settings, {"id": "a" * 32, "at": to_iso(NOW - timedelta(days=31))})
    assert api.get("/api/v1/health").status_code == 200
    assert entries(settings) == []
    add(settings, {"id": "b" * 32, "at": to_iso(NOW - timedelta(days=31))})
    clock.at = NOW + timedelta(hours=23)
    assert api.get("/api/v1/health").status_code == 200
    assert [row["id"][0] for row in entries(settings)] == ["b"]
    clock.at = NOW + timedelta(days=1)
    assert api.get("/api/v1/health").status_code == 200
    assert entries(settings) == []


def test_a_purge_that_fails_never_fails_a_request_or_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = small(tmp_path)

    def broken(*_args: Any) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(access_log_module, "purge_access_log", broken)
    monkeypatch.setattr("oversight.api.app.purge_access_log", broken)
    prepare_snapshot(settings.db_file, access_log_days=365, now=lambda: NOW)
    assert client_of(settings).get("/api/v1/health").status_code == 200


def test_access_log_days_is_a_year_by_default_and_never_under_30(tmp_path: Path) -> None:
    assert Settings(_env_file=None).access_log_days == 365
    assert Settings(_env_file=None, access_log_days=30).access_log_days == 30
    with pytest.raises(ValidationError, match="ACCESS_LOG_DAYS must be at least 30"):
        Settings(_env_file=None, access_log_days=29)


def test_a_version_8_snapshot_with_entries_keeps_them_through_this_version_and_serves_them(tmp_path: Path) -> None:
    settings = small(tmp_path)
    question = {"qa_id": "e" * 32, "outcome": "answered"}
    add(settings, *({"id": f"{n:032x}", "at": at(10 - n), "action": "people_compare_view", "viewer_hash": "local",
                     "viewer_name": LOCAL_VIEWER_NAME, "subject_ids_json": ["pbc7fe62842ca"]} for n in range(3)),
        {"id": "9" * 32, "at": at(1), "action": "person_ask", "viewer_hash": "local", "viewer_name": LOCAL_VIEWER_NAME,
         "subject_ids_json": ["pbc7fe62842ca"], "detail_json": question})
    with Store.open(settings.db_file) as store:
        store.conn.executescript(VERSION_8)
    before = entries(settings)
    prepare_snapshot(settings.db_file)
    with Store.open(settings.db_file) as store:
        assert store.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 11
    assert entries(settings) == before

    body = client_of(settings).get("/api/v1/access-log", params={"level": "people"}).json()
    assert [entry["action"] for entry in body["entries"]] == ["person_ask", *["people_compare_view"] * 3]
    assert body["entries"][0]["detail"] == question
    assert body["entries"][0]["subjects"] == [{"id": "pbc7fe62842ca", "name": FORMER_MEMBER, "avatar": None}]
