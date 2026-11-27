from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from oversight.api.app import context, create_app
from oversight.api.credit import (
    AUTOMATIC,
    Choice,
    Credit,
    PeopleField,
    Setting,
    StaleRevision,
    board_credit,
    board_setting,
    coverage,
    people_fields,
    save_setting,
    step_one,
)
from oversight.api.routes.people import people_scope
from oversight.api.scorecard import load_board
from oversight.ask.catalog import CatalogContext, validate_params
from oversight.ask.people import PEOPLE_CATALOG
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.ingest.avatars import PictureFetcher, due, named_in_fields
from oversight.ingest.sync import run_sync
from oversight.jira.mapping import map_field_people, people_named
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.settings import Settings
from oversight.store.db import SCHEMA_VERSION, Store, open_readonly
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES, VIEWER_HEADERS
from tests.fakes.fake_jira import FakeIssue, FakeJira

T = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
KEY = "credit-unit-key"
BOARD = "CRD"
FIELDS = FieldsConfig.load(CONFIG_FIXTURES / "fields.yaml")
PROGRAMMER, TESTERS, CODE_REVIEWERS = (FIELDS.id_of(name) for name in ("programmer", "testers", "code_reviewers"))
NAMES = {"pat": "Pat Programmer", "quinn": "Quinn Coder", "riley": "Riley Developer", "sam": "Sam Reviewer",
         "terry": "Terry Holder", "vic": "Vic Tester", "xena": "Xena Late", "wes": "Wes Member"}
PROCESS = ProcessConfig.model_validate({
    "process_version": "0.1.0-credit-unit",
    "stages": ["backlog", "estimate", "development", "code_review", "dev_signoff", "testing", "test_review",
               "test_signoff", "done"],
    "start_stage": "development",
    "done_stage": "done",
    "projects": {BOARD: {
        "status_to_stage": {"To Do": "backlog", "In Progress": "development", "Code Review": "code_review",
                            "In Testing": "testing", "Done": "done"},
        "estimate": {"basis": "points", "field": "story_points", "hours_per_point": 8},
        "evidence": {"code_review": {"status": "Code Review"}, "dev_signoff": None, "testing": {"status": "In Testing"},
                     "test_review": None, "test_signoff": None},
        "credit": {"source": "assignee"},
    }},
})
F, N = "field", "assignee_now"
PNG = b"\x89PNG\r\n\x1a\n" + b"a builder's picture"


def pid_of(account_id: str) -> str:
    return "p" + hashlib.sha256(account_id.encode()).hexdigest()[:12]


def account(who: str) -> str:
    return f"acc-{who}"


def pid(who: str) -> str:
    return "p" + hashlib.sha256(account(who).encode()).hexdigest()[:12]


def at(days: float) -> str:
    moment = to_iso(T - days * DAY)
    assert moment is not None
    return moment


def user(who: str) -> dict[str, Any]:
    found: dict[str, Any] = {"accountId": account(who), "accountType": "atlassian", "active": True,
                             "emailAddress": f"{who}@example.invalid", "timeZone": "Europe/Berlin"}
    if who in NAMES:
        found["displayName"] = NAMES[who]
    return found


@dataclass(frozen=True)
class Case:
    """One ticket: who holds it now, its people fields, its assignee history and its status timeline."""

    number: int
    holder: str | None = None
    programmer: tuple[str, ...] = ()
    testers: Any = ()  # a list of people, or one person (a single-user picker)
    changes: tuple[tuple[float, str | None, str | None], ...] = ()  # (days before, from, to)
    stages: tuple[tuple[str, float, float | None], ...] = ()  # (stage, entered days before, left days before)
    kind: str = "Task"
    raw: str | None = None  # raw_json as stored, when it is not the payload

    @property
    def key(self) -> str:
        return f"{BOARD}-{self.number}"


# Development is ten to eight days ago, code review eight to six, testing six to four, done since four.
FLOW = (("development", 10, 8), ("code_review", 8, 6), ("testing", 6, 4), ("done", 4, None))
CASES: dict[str, Case] = {
    "one programmer": Case(1, programmer=("pat",), changes=((9.5, None, "riley"), (8, "riley", None)), stages=FLOW),
    "several programmers": Case(2, programmer=("pat", "quinn", "pat"), stages=FLOW),
    "programmer removed": Case(3, changes=((9.5, None, "riley"), (8, "riley", None)), stages=FLOW),
    "assignee kept": Case(4, holder="terry", changes=((11, None, "terry"),), stages=FLOW),
    "cleared at each hand-off": Case(5, changes=((9.5, None, "riley"), (8, "riley", None), (7, None, "sam"),
                                                 (6, "sam", None), (5, None, "vic"), (4, "vic", None)), stages=FLOW),
    "reassigned in development": Case(6, changes=((9.5, None, "riley"), (9, "riley", "terry"), (8, "terry", None),
                                                  (7, None, "sam"), (4, "sam", None)), stages=FLOW),
    "never in development": Case(7, changes=((9, None, "sam"), (7, "sam", "vic"), (4, "vic", None)),
                                 stages=(("backlog", 10, 8), ("code_review", 8, 4), ("done", 4, None))),
    "no history": Case(8, stages=FLOW),
    "assigned only once done": Case(9, changes=((2, None, "xena"), (1, "xena", None)), stages=FLOW),
    "an unknown person": Case(10, programmer=("ghost",), stages=FLOW),
    "testers": Case(11, programmer=("pat",), testers=["vic"], changes=((9.5, None, "riley"), (8, "riley", None)),
                    stages=FLOW),
    "a single-user picker": Case(12, testers="vic", changes=((9.5, None, "riley"), (8, "riley", None)), stages=FLOW),
    "an instant in development": Case(13, changes=((6, None, "riley"), (3, "riley", None)),
                                      stages=(("backlog", 10, 5), ("development", 5, 5), ("done", 5, None))),
    "assigned as it left development": Case(14, changes=((8, None, "sam"), (4, "sam", None)), stages=FLOW),
    "unreadable raw_json": Case(15, programmer=("pat",), changes=((9.5, None, "riley"), (8, "riley", None)),
                                stages=FLOW, raw="{not json"),
    "an epic": Case(16, testers=["sam"], kind="Epic", stages=FLOW),
}
# Whom each ticket counts for, by hand, under each choice: (who, basis). Only the chosen source is read, as it is
# now: no assignee history, and a ticket it names no one on counts for no one.
NOBODY: dict[str, set[tuple[str, str]]] = {name: set() for name in CASES}
BY_ASSIGNEE = {**NOBODY, "assignee kept": {("terry", N)}}
BY_PROGRAMMER = {**BY_ASSIGNEE, "one programmer": {("pat", F)}, "several programmers": {("pat", F), ("quinn", F)},
                 "an unknown person": {("ghost", F)}, "testers": {("pat", F)}}
BY_TESTERS = {**NOBODY, "testers": {("vic", F)}, "a single-user picker": {("vic", F)}, "an epic": {("sam", F)}}


def write_case(store: Store, issue_id: int, case: Case) -> None:
    fields: dict[str, Any] = {
        "summary": f"Case {case.number}", "issuetype": {"name": case.kind},
        "assignee": user(case.holder) if case.holder else None,
        PROGRAMMER: [user(who) for who in case.programmer] or None, CODE_REVIEWERS: None,
        TESTERS: ([user(who) for who in case.testers] or None) if isinstance(case.testers, (list, tuple))
        else user(case.testers),
    }
    store.upsert("issues", {
        "issue_key": case.key, "issue_id": str(issue_id), "project_key": BOARD, "issue_type": case.kind,
        "summary": f"Case {case.number}", "status": "Done", "status_category": "done",
        "assignee_id": account(case.holder) if case.holder else None, "created_at": at(20), "updated_at": at(1),
        "raw_json": case.raw if case.raw is not None else {"key": case.key, "fields": fields},
    })
    for n, (days, before, after) in enumerate(case.changes):
        store.upsert("field_changes", {"issue_key": case.key, "changelog_id": str(1000 * case.number + n),
                                       "field": "assignee", "changed_at": at(days),
                                       "from_value": account(before) if before else None,
                                       "to_value": account(after) if after else None})
    for seq, (stage, entered, left) in enumerate(case.stages):
        store.conn.execute(
            "INSERT INTO status_intervals (issue_key, seq, status, stage, entered_at, left_at, hours)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (case.key, seq, stage, stage, at(entered), at(left) if left is not None else None, None))
    done = next(entered for stage, entered, _left in case.stages if stage == "done")
    store.upsert("issue_metrics", {"issue_key": case.key, "done_at": at(done), "computed_at": at(0)})


def build_cases(path: Path) -> None:
    with Store.open(path) as store, store.transaction():
        for who, name in NAMES.items():
            store.upsert("people", {"account_id": account(who), "display_name": name, "active": 1,
                                    "updated_at": to_iso(T)})
        for n, case in enumerate(CASES.values()):
            write_case(store, 9000 + n, case)


@pytest.fixture(scope="module")
def cases(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    path = tmp_path_factory.mktemp("credit_cases") / "oversight.db"
    build_cases(path)
    conn = open_readonly(path)
    yield conn
    conn.close()


def credited(conn: sqlite3.Connection, choice: Choice,
             fields: FieldsConfig = FIELDS) -> dict[str, set[tuple[str, ...]]]:
    found = board_credit(conn, PROCESS, fields, BOARD, Setting(choice, 0))
    return {name: set(found.of(case.key)) for name, case in CASES.items()}


def expected(by_hand: dict[str, set[tuple[str, str]]], field_name: str | None) -> dict[str, set[tuple[str, ...]]]:
    return {name: {Credit(pid(who), basis, field_name if basis == F else None) for who, basis in credits}
            for name, credits in by_hand.items()}


# --- the rule, under each choice ----------------------------------------------------------------------------


def test_automatic_reads_the_programmer_field_and_else_the_assignee_now(
    cases: sqlite3.Connection,
) -> None:
    assert credited(cases, AUTOMATIC) == expected(BY_PROGRAMMER, "Programmer")


def test_assignee_reads_the_assignee_now_alone(cases: sqlite3.Connection) -> None:
    assert credited(cases, Choice("assignee")) == expected(BY_ASSIGNEE, None)


def test_a_chosen_field_reads_that_field_alone(cases: sqlite3.Connection) -> None:
    assert credited(cases, Choice("field", "testers")) == expected(BY_TESTERS, "Testers")


def test_a_field_the_site_does_not_have_skips_the_first_step(cases: sqlite3.Connection) -> None:
    without = FIELDS.model_copy(update={"fields": {**FIELDS.fields, "programmer": None, "testers": None}})
    assert credited(cases, AUTOMATIC, without) == expected(BY_ASSIGNEE, None)
    assert step_one(AUTOMATIC, without) is None
    # A chosen field the site has lost stays the choice and names no one, so every ticket counts for no one.
    assert credited(cases, Choice("field", "testers"), without) == expected(NOBODY, None)
    assert step_one(Choice("field", "testers"), without) == PeopleField("testers", "Testers", None)
    assert credited(cases, Choice("field", "nope")) == expected(NOBODY, None)


def test_several_people_in_the_field_each_count_once_in_its_order(cases: sqlite3.Connection) -> None:
    found = board_credit(cases, PROCESS, FIELDS, BOARD, Setting(AUTOMATIC, 0))
    assert found.of(CASES["several programmers"].key) == (Credit(pid("pat"), F, "Programmer"),
                                                           Credit(pid("quinn"), F, "Programmer"))


def test_every_builder_is_a_person_and_an_unknown_one_is_named_safely(cases: sqlite3.Connection) -> None:
    found = board_credit(cases, PROCESS, FIELDS, BOARD, Setting(AUTOMATIC, 0))
    builders = {one.person_id for credits in found.builders.values() for one in credits}
    assert set(found.people) == builders == set(found.accounts)
    assert found.people[pid("ghost")] == {"id": pid("ghost"), "name": "Unnamed account", "avatar": None}
    assert found.people[pid("pat")] == {"id": pid("pat"), "name": "Pat Programmer", "avatar": None}
    assert found.accounts[pid("terry")] == account("terry")
    assert "acc-" not in json.dumps(found.people)


def test_the_field_step_reads_whom_the_field_names_now_whatever_its_history(tmp_path: Path) -> None:
    path = tmp_path / "oversight.db"
    build_cases(path)
    with Store.open(path) as store:
        store.upsert("field_changes", {"issue_key": CASES["programmer removed"].key, "changelog_id": "1",
                                       "field": "programmer", "changed_at": at(3), "from_value": account("pat"),
                                       "to_value": None})
    conn = open_readonly(path)
    try:
        assert credited(conn, AUTOMATIC)["programmer removed"] == set()
    finally:
        conn.close()


# --- the setting --------------------------------------------------------------------------------------------


def test_the_file_choice_stands_until_an_admin_saves_one(store: Store) -> None:
    assert board_setting(store.conn, PROCESS, BOARD) == Setting(Choice("assignee"), 0, None, None)
    assert board_setting(store.conn, PROCESS, "ELSE") == Setting(AUTOMATIC, 0, None, None)
    saved = save_setting(store, PROCESS, BOARD, Choice("field", "testers"), revision=0, by="Morgan Reyes",
                         at=at(0))
    assert saved == Setting(Choice("field", "testers"), 1, "Morgan Reyes", at(0))
    assert board_setting(store.conn, PROCESS, BOARD) == saved
    assert board_setting(store.conn, PROCESS, "ELSE") == Setting(AUTOMATIC, 0, None, None)


def test_a_save_from_a_revision_the_board_has_left_is_refused_and_the_same_choice_changes_nothing(
    store: Store,
) -> None:
    save_setting(store, PROCESS, BOARD, AUTOMATIC, revision=0, by="A", at=at(2))
    with pytest.raises(StaleRevision) as refused:
        save_setting(store, PROCESS, BOARD, Choice("assignee"), revision=0, by="B", at=at(1))
    assert refused.value.revision == 1
    same = save_setting(store, PROCESS, BOARD, AUTOMATIC, revision=1, by="B", at=at(1))
    assert same == Setting(AUTOMATIC, 1, "A", at(2))
    assert board_setting(store.conn, PROCESS, BOARD).revision == 1


def test_going_back_to_an_earlier_choice_is_another_save(store: Store) -> None:
    for revision, choice in enumerate((AUTOMATIC, Choice("field", "programmer"), Choice("assignee"))):
        save_setting(store, PROCESS, BOARD, choice, revision=revision, by="A", at=at(10 - revision))
    save_setting(store, PROCESS, BOARD, Choice("field", "programmer"), revision=3, by="B", at=at(0))
    assert board_setting(store.conn, PROCESS, BOARD) == Setting(Choice("field", "programmer"), 4, "B", at(0))


def test_coverage_counts_each_choice_over_the_finished_work_and_offers_filled_fields_only(
    cases: sqlite3.Connection,
) -> None:
    finished = [case.key for case in CASES.values() if case.kind != "Epic"]
    eligible, options = coverage(cases, FIELDS, BOARD, excluded={"epic"}, finished=finished)
    assert [one.key for one in eligible] == ["programmer", "testers"]  # Code Reviewers names no one; the Epic's
    # Testers would count on its own, but an Epic is no one's work.
    by_choice = {(option.choice.source, option.choice.field): (option.source_filled, option.credited)
                 for option in options}
    assert list(by_choice) == [("auto", None), ("assignee", None), ("field", "programmer"), ("field", "testers")]
    # Programmer fills 1, 2, 10 and 11, and Automatic adds 4, the one assigned now.
    assert by_choice[("auto", None)] == (4, 5)
    assert by_choice[("field", "programmer")] == (4, 4)
    assert by_choice[("assignee", None)] == (1, 1)  # 4 alone is assigned now
    assert by_choice[("field", "testers")] == (2, 2)  # 11 and 12
    same, none_finished = coverage(cases, FIELDS, BOARD, excluded={"epic"}, finished=[])
    assert same == eligible and [(option.source_filled, option.credited) for option in none_finished] == [(0, 0)] * 4


def test_people_fields_are_the_sites_people_and_user_picker_fields_by_name() -> None:
    assert [(one.key, one.name) for one in people_fields(FIELDS)] == [
        ("code_reviewers", "Code Reviewers"), ("programmer", "Programmer"), ("test_reviewers", "Test Reviewers"),
        ("testers", "Testers")]
    picker = FIELDS.fields["sprint"].model_copy(update={"custom_type": "x:multiuserpicker"})  # type: ignore[union-attr]
    assert "sprint" in FIELDS.model_copy(update={"fields": {**FIELDS.fields, "sprint": picker}}).people_fields()
    assert "story_points" not in FIELDS.people_fields()


# --- the API -------------------------------------------------------------------------------------------------


def serve(root: Path, **options: Any) -> TestClient:
    config = root / "config"
    config.mkdir(exist_ok=True)
    (config / "process.yaml").write_text(json.dumps(PROCESS.model_dump(mode="json", exclude_defaults=True)),
                                         encoding="utf-8")
    for name in ("fields.yaml", "rules.yaml"):
        (config / name).write_text((CONFIG_FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    settings = Settings(_env_file=None, jira_projects=BOARD, api_service_key=KEY, db_path=root / "oversight.db",
                        config_dir=config, reports_dir=root / "reports", board_discovery="off", **options)
    return TestClient(create_app(settings, provider=ScriptedProvider(), now=lambda: T + HOUR),
                      headers={"X-Service-Key": KEY, **VIEWER_HEADERS})


@pytest.fixture
def api(tmp_path: Path) -> TestClient:
    build_cases(tmp_path / "oversight.db")
    with Store.open(tmp_path / "oversight.db") as store:
        store.set_cursor(BOARD, last_synced_at=to_iso(T) or "", run_id="r1", account_timezone=None)
    return serve(tmp_path)


def put(client: TestClient, body: Any, **params: Any) -> Any:
    return client.put("/api/v1/people/credit", params={"level": "people", **params},
                      content=body if isinstance(body, (bytes, str)) else json.dumps(body))


def test_a_save_of_the_choice_that_applies_changes_nothing(api: TestClient, tmp_path: Path) -> None:
    first = api.get("/api/v1/people/credit", params={"level": "people"}).json()
    again = put(api, {"source": "assignee", "field": None, "revision": 0})
    assert again.status_code == 200, again.text
    assert again.json()["setting"] == first["setting"] and "history" not in again.json()
    # A field that names no one on the board is never offered, but the choice that applies stays choosable.
    with Store.open(tmp_path / "oversight.db") as store:
        save_setting(store, PROCESS, BOARD, Choice("field", "test_reviewers"), revision=0, by="A", at=at(0))
    kept = put(api, {"source": "field", "field": "test_reviewers", "revision": 1})
    assert kept.status_code == 200 and kept.json()["setting"]["revision"] == 1
    assert [option["field"] for option in kept.json()["options"]] == [
        None, None, {"key": "programmer", "name": "Programmer"}, {"key": "testers", "name": "Testers"}]
    assert put(api, {"source": "field", "field": "code_reviewers", "revision": 1}).status_code == 422


@pytest.mark.parametrize("body", [
    {"source": "assignee", "field": None, "revision": 0, "reason": "x"},
    {"source": "assignee", "revision": -1},
    {"source": "assignee", "revision": True},
    {"source": "assignee", "revision": 0.5},
    b"\xff\xfe",
    b'{"source": "assignee", "revision": NaN}',
    json.dumps({"source": "assignee", "field": None, "revision": 0, "padding": "x" * 5000}),
])
def test_a_body_that_is_not_a_choice_is_refused_with_what_is_wrong(api: TestClient, body: Any) -> None:
    response = put(api, body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "credit_invalid" and response.json()["errors"]


def test_whose_figures_follow_the_setting_at_once(api: TestClient) -> None:
    def done_by_person() -> dict[str, int]:
        body = api.get("/api/v1/people/scorecard", params={"level": "people", "days": 30}).json()
        return {row["person"]["name"]: row["values"]["done_in_period"]["value"] for row in body["people"]}

    assert done_by_person() == {"Terry Holder": 1}  # Assignee: only case 4 is assigned now
    assert put(api, {"source": "field", "field": "testers", "revision": 0}).status_code == 200
    assert done_by_person() == {"Vic Tester": 2}  # Testers: 11 and 12 (the Epic is no one's work)
    body = api.get("/api/v1/people/credit", params={"level": "people", "days": 30}).json()
    assert body["setting"]["field"] == body["effective_field"] == {"key": "testers", "name": "Testers"}
    assert body["setting"]["changed_by"] == "Local (sign-in off)"
    assert not [secret for secret in ("acc-", "example.invalid", "Europe/Berlin", "customfield_")
                if secret in json.dumps(body)]


def test_a_builder_who_holds_nothing_now_is_on_the_board_with_their_picture(api: TestClient, tmp_path: Path) -> None:
    assert put(api, {"source": "field", "field": "programmer", "revision": 0}).status_code == 200
    with Store.open(tmp_path / "oversight.db") as store:
        store.conn.execute("UPDATE people SET avatar = ?, avatar_type = 'image/png', avatar_hash = ? "
                           "WHERE account_id = 'acc-pat'", (PNG, hashlib.sha256(PNG).hexdigest()[:12]))
    pat = {"id": pid("pat"), "name": "Pat Programmer", "avatar": hashlib.sha256(PNG).hexdigest()[:12]}
    people = api.get("/api/v1/people").json()["people"]
    assert {"person": pat, "tickets": 3, "open_tickets": 0, "open_breaches": 0, "open_warnings": 0,
            "tickets_with_findings": 0} in people  # the Programmer of 1, 2 and 11, never assigned
    assert {"person": pat, "tickets": 3} in api.get("/api/v1/people/directory").json()["people"]
    picture = api.get(f"/api/v1/people/{pid('pat')}/avatar")
    assert (picture.status_code, picture.content) == (200, PNG)
    assert api.get(f"/api/v1/people/{pid('quinn')}/avatar").status_code == 404  # credited, but no picture held
    assert api.get("/api/v1/people/p000000000000/avatar").status_code == 404
    card = api.get(f"/api/v1/people/{pid('pat')}/card", params={"level": "people"}).json()
    assert (card["membership"], card["work"]["done_in_period"], card["last_activity_at"]) == ("holds_tickets", 3, None)
    entries = api.get("/api/v1/access-log", params={"level": "people"}).json()["entries"]
    assert [subject for entry in entries for subject in entry["subjects"]] == [pat]  # never "Former member"


# --- ingest -------------------------------------------------------------------------------------------------


def test_the_people_fields_people_map_to_people_rows_each_once_and_nothing_else() -> None:
    issue = {"key": "AIL-1", "fields": {
        "assignee": user("terry"), PROGRAMMER: [user("pat"), user("quinn"), user("pat"), {"displayName": "No id"}],
        TESTERS: user("vic"), CODE_REVIEWERS: None, FIELDS.id_of("sprint"): [{"id": 4, "name": "Sprint 4"}],
    }}
    rows = map_field_people(issue, FIELDS, at(0))
    assert [row["account_id"] for row in rows] == [account("pat"), account("quinn"), account("vic")]
    assert rows[0] == {"account_id": account("pat"), "display_name": "Pat Programmer", "active": 1,
                       "updated_at": at(0), "avatar_url": None}
    assert "example.invalid" not in json.dumps(rows) and "Europe/Berlin" not in json.dumps(rows)
    assert people_named([user("pat"), "pat", None, user("pat")]) == (account("pat"),)
    assert people_named(user("vic")) == (account("vic"),) and people_named(None) == ()


class PeopleFieldsJira(FakeJira):
    def __init__(self) -> None:
        super().__init__()
        self.named: dict[str, dict[str, Any]] = {}

    def payload(self, issue: FakeIssue, fields: list[str] | None = None) -> dict[str, Any]:
        body = super().payload(issue, fields)
        body["fields"].update(self.named.get(issue.key, {}))
        return body


def test_a_sync_keeps_the_people_fields_people_and_counts_only_assignees(store: Store, fields: FieldsConfig) -> None:
    fake = PeopleFieldsJira()
    fake.add_project("AIL")
    issue = fake.create("AIL", "Task")
    fake.assign(issue.key, "acc-dev-1")
    picture = "https://avatar-management--avatars.us-west-2.prod.public.atl-paas.net/acc-pia/48"
    pia = {"accountId": "acc-pia", "accountType": "atlassian", "displayName": "Pia Programmer", "active": True,
           "avatarUrls": {"48x48": picture}, "emailAddress": "pia@example.invalid"}
    fake.named[issue.key] = {fields.id_of("programmer"): [pia]}
    asked: list[str] = []

    def picture_host(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(404)

    with PictureFetcher(transport=httpx.MockTransport(picture_host)) as pictures:
        result = run_sync(fake.client(), store, fields, projects=["AIL"], mode="full", now=lambda: fake.clock,
                          pictures=pictures)
    assert asked == [picture]  # the picture step asks for the Programmer's picture too
    row = store.query_one("SELECT display_name, avatar_url FROM people WHERE account_id = 'acc-pia'")
    assert tuple(row) == ("Pia Programmer", picture)
    assert result.people.as_dict() == {"inserted": 1, "updated": 0, "deleted": 0, "unchanged": 0}
    assert named_in_fields(store, ["AIL"], [fields.id_of("programmer")]) == {"acc-pia"}
    assert "acc-pia" in {row["account_id"] for row in due(store, ["AIL"], fake.clock, 200,
                                                          [fields.id_of("programmer")])}
    assert "acc-pia" not in {row["account_id"] for row in due(store, ["AIL"], fake.clock, 200)}
    assert "pia@example.invalid" not in json.dumps([dict(row) for row in store.query("SELECT * FROM people")],
                                                   default=str)


# --- the schema ---------------------------------------------------------------------------------------------

CREDIT_SHAPE = {
    "people_credit": [("project_key", "TEXT", 0, 1), ("source", "TEXT", 1, 0), ("field_key", "TEXT", 0, 0),
                      ("revision", "INTEGER", 1, 0), ("changed_by", "TEXT", 1, 0), ("changed_at", "TEXT", 1, 0)],
}


def credit_shape(path: Path) -> dict[str, list[tuple[str, str, int, int]]]:
    conn = sqlite3.connect(path)
    try:
        return {table: [(row[1], row[2], row[3], row[5]) for row in conn.execute(f"PRAGMA table_info({table})")]
                for table in CREDIT_SHAPE}
    finally:
        conn.close()


def test_a_version_11_snapshot_gains_the_credit_tables_and_keeps_a_saved_choice(tmp_path: Path) -> None:
    path = tmp_path / "v11.sqlite"
    with Store.open(path) as store:
        store.conn.executescript("DROP TABLE people_credit; DROP TABLE IF EXISTS finding_ignores;"
                                 " PRAGMA user_version = 11;")
    assert credit_shape(path) == {"people_credit": []}
    with Store.open(path) as store:
        assert store.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 12
        assert board_setting(store.conn, PROCESS, BOARD).revision == 0
        save_setting(store, PROCESS, BOARD, AUTOMATIC, revision=0, by="A", at=at(0))
    assert credit_shape(path) == CREDIT_SHAPE
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 11")  # as if it ran again: nothing is lost
    conn.close()
    with Store.open(path) as store:
        assert board_setting(store.conn, PROCESS, BOARD) == Setting(AUTOMATIC, 1, "A", at(0))


def test_a_snapshot_without_the_credit_tables_reads_the_files_choice(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    with Store.open(path) as store:
        store.conn.executescript("DROP TABLE people_credit;")
        assert board_setting(store.conn, PROCESS, BOARD) == Setting(Choice("assignee"), 0)


# --- DEMO: the credit rule gives today's figures, and every view agrees ----------------------------------------

DEMO_NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    root = tmp_path_factory.mktemp("credit_unit_demo")
    settings = Settings(_env_file=None, jira_projects="DEMO", api_service_key=KEY, db_path=root / "oversight.db",
                        config_dir=CONFIG_FIXTURES, reports_dir=root / "reports", board_discovery="off")
    process = ProcessConfig.load(settings.process_file)
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=DEMO_NOW)
        run_derive(store, process, projects=["DEMO"], now=lambda: DEMO_NOW)
        run_rules(store, process, RulesConfig.load(settings.rules_file), FieldsConfig.load(settings.fields_file),
                  projects=["DEMO"], now=lambda: DEMO_NOW)
    return settings


def test_on_demo_each_ticket_counts_for_whoever_holds_it_now(demo: Settings) -> None:
    app = create_app(demo, provider=ScriptedProvider(), now=lambda: DEMO_NOW)
    conn = open_readonly(demo.db_file)
    try:
        board = load_board(conn, replace(context(app), selected="DEMO"))
        holders = {row["issue_key"]: row["assignee_id"] for row in conn.execute(
            "SELECT issue_key, assignee_id FROM issues WHERE project_key = 'DEMO'")}
    finally:
        conn.close()
    # DEMO fills no Programmer, so Automatic counts each ticket for its assignee now, an unassigned one for no one.
    assert board.work and all(
        ticket.builders == ((Credit(pid_of(holders[ticket.issue_key]), N, None),) if holders[ticket.issue_key] else ())
        for ticket in board.work)
    assert (board.credit_source, board.credit_field) == ("auto", "Programmer")


def test_on_demo_people_the_directory_the_scorecard_the_card_and_ask_count_alike(demo: Settings) -> None:
    app = create_app(demo, provider=ScriptedProvider(), now=lambda: DEMO_NOW)
    client = TestClient(app, headers={"X-Service-Key": KEY, **VIEWER_HEADERS})
    people = {row["person"]["id"]: row for row in client.get("/api/v1/people").json()["people"] if row["person"]}
    directory = {entry["person"]["id"]: entry["tickets"]
                 for entry in client.get("/api/v1/people/directory").json()["people"]}
    ctx = replace(context(app), selected="DEMO")
    conn = open_readonly(demo.db_file)
    try:
        scope = people_scope(conn, ctx)
        catalog = CatalogContext(process=ctx.process, rules=ctx.rules, project="DEMO", now=DEMO_NOW, people=scope)

        def facts(name: str, **params: Any) -> Any:
            return PEOPLE_CATALOG[name].run(conn, validate_params(name, params, PEOPLE_CATALOG), catalog)

        listed = {row["person_id"]: row["tickets"] for row in facts("oversight_people", rank_by="tickets", n=50).rows}
        on_board = {row["person_id"]: row["tickets"] for row in facts("oversight_board_people").rows}
        for days in (30, 90, 180):
            card = {row["person"]["id"]: row
                    for row in client.get("/api/v1/people/scorecard",
                                          params={"level": "people", "days": days}).json()["people"]}
            assert set(card) == set(people) and len(people) >= 5
            for key, row in people.items():
                values = card[key]["values"]
                assert card[key]["tickets"] == row["tickets"] == directory[key] == listed[key] == on_board[key]
                assert values["open_tickets"]["value"] == row["open_tickets"]
                shown = client.get(f"/api/v1/people/{key}/card", params={"level": "people", "days": days}).json()
                assert shown["work"] == {"open_tickets": values["open_tickets"]["value"],
                                         "in_progress": values["in_progress"]["value"],
                                         "done_in_period": values["done_in_period"]["value"], "days": days}
                assert (shown["findings"]["open_breaches"], shown["findings"]["open_warnings"]) == (
                    row["open_breaches"], row["open_warnings"])
                for state, count in (("all", row["tickets"]), ("open", row["open_tickets"]),
                                     ("done", values["done_in_period"]["value"])):
                    note = facts("oversight_person_tickets", person=key, state=state, n=200,
                                 **({"days": days} if state == "done" else {})).note
                    assert note.endswith(f"; {count} in all"), (key, state, note)
    finally:
        conn.close()
