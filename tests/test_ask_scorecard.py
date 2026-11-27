from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oversight.api.app import context, create_app
from oversight.api.routes.people import people_scope
from oversight.api.scorecard import MEASURES
from oversight.ask.analyst import QUERY_TOOL, SnapshotQuery, analyst_tools
from oversight.ask.catalog import CATALOG, validate_params
from oversight.ask.people import (
    PEOPLE_CATALOG,
    PERSONNEL_WORDS,
    SCORECARD_WORDS,
    PeopleScope,
    evaluates,
    named_in,
    personnel,
    specs_for,
)
from oversight.ask.prompts import (
    CAVEAT_HERE,
    COMPARISON_OFF,
    DECISION_YOURS,
    JUDGEMENT_DECLINE,
    caveat,
)
from oversight.ask.router import CATALOG_NAMES, COMPARISON_OFF_REASON, PEOPLE_NAMES, PERSON_REASON, judging, route
from oversight.ask.scorecards import (
    COMPARE_COLUMNS,
    OVERALL_COLUMNS,
    SCORECARD_COLUMNS,
    ComparisonOff,
    character_labels,
    code_built,
)
from oversight.ask.service import ask
from oversight.config import ProcessConfig
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC
from tests.conftest import VIEWER_HEADERS
from tests.fakes.fake_llm import FakeProvider, audit_shim, plan  # noqa: F401  (audit_shim is a fixture)
from tests.test_analyst import FakeAnalyst, turn, use
from tests.test_api_ask_insights import ASK_FIELDS, _seed
from tests.test_api_overview import KEY, make_settings

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
PEOPLE = [*CATALOG_NAMES, *PEOPLE_NAMES]  # a people-level catalogue, comparing people on
OFF = [name for name in PEOPLE if name != "oversight_people_compare"]  # comparing people off
ADA, BEN, CARMEN = "pbc7fe62842ca", "p1234567890ab", "pfedcba987654"
DEMO_CAVEAT = ("Figures from Jira over the last 90 days, next to the whole board. Tickets count for their Programmer in "
               "Jira, or their assignee where there is no Programmer. Code review isn't recorded on this board, so "
               "reviewers' work doesn't show.")
PERSON_ID = re.compile(r"\bp[0-9a-f]{12}\b")


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    settings = make_settings(tmp_path_factory.mktemp("ask_scorecard"))
    _seed(settings, briefing=False)
    return settings


def client_of(settings: Settings, provider: Any = None) -> TestClient:
    app = create_app(settings, provider=provider or ScriptedProvider(), now=lambda: NOW)
    return TestClient(app, headers={"X-Service-Key": KEY, **VIEWER_HEADERS})


@pytest.fixture(scope="module")
def client(demo: Settings) -> TestClient:
    return client_of(demo)


@pytest.fixture(scope="module")
def off(demo: Settings) -> TestClient:
    """The same store, served by a service whose organisation turned comparing people off."""
    return client_of(demo.model_copy(update={"people_comparison": "off"}))


def post(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/v1/ask", json=body)
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()
    assert set(answer) == ASK_FIELDS
    return answer


def board(client: TestClient) -> dict[str, dict[str, Any]]:
    """GET /people/directory's people by name."""
    response = client.get("/api/v1/people/directory")
    assert response.status_code == 200
    return {entry["person"]["name"]: entry["person"] for entry in response.json()["people"]}


def qa_row(settings: Settings, qa_id: str) -> dict[str, Any]:
    conn = open_readonly(settings.db_file)
    try:
        row = conn.execute("SELECT * FROM qa_log WHERE qa_id = ?", (qa_id,)).fetchone()
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def llm_calls(settings: Settings, qa_id: str) -> int:
    conn = open_readonly(settings.db_file)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM llm_calls WHERE qa_id = ?", (qa_id,)).fetchone()[0])
    finally:
        conn.close()


def scope_of(settings: Settings, project: str = "DEMO") -> PeopleScope:
    ctx = replace(context(create_app(settings, provider=ScriptedProvider(), now=lambda: NOW)), selected=project)
    conn = open_readonly(settings.db_file)
    try:
        return people_scope(conn, ctx)
    finally:
        conn.close()


def _ask(settings: Settings, provider: Any, question: str, **kw: Any) -> Any:
    process, rules = ProcessConfig.load(settings.process_file), RulesConfig.load(settings.rules_file)
    with Store.open(settings.db_file) as store:
        return ask(question, store=store, process=process, rules=rules, settings=settings, provider=provider,
                   now=lambda: NOW, **kw)


@pytest.mark.parametrize(
    "words",
    [
        "Is  doing well?", "Is  doing badly?", "How is  doing these days?", "How are  and  doing?", "Is  sincere?",
        "Does  follow the agile process?", "Who follows the process?", "Who is following process best?",
        "Rank everyone", "Who is top of the board?", "Who is at the bottom?", "Who is best on the board?",
        "Who is worst?", "Who is slowest?", "Who is fastest?", "Who closes tickets quickest?", "Is  better than ?",
        "Is  good?", "Is  a great developer?", "Is  poor at estimating?", "Who is most productive?",
        "How is  performing?", "Who underperforms?", "How much effort does  put in?", "Who works hardest?",
        "Is  hard-working?", "Who is slacking?", "Who is the weakest developer?", "Who is strongest?",
        "Show 's scorecard",
    ],
)
def test_every_scorecard_word_family_is_answered_with_figures(words: str) -> None:
    assert evaluates(words) and not personnel(words) and SCORECARD_WORDS.search(words.lower()), words
    assert judging(words, [ADA, BEN] if " and " in words else [ADA]) == "scorecard"


@pytest.mark.parametrize(
    "words",
    [
        "Should we fire ?", "Should  be fired?", "Should we sack ?", "Should  be promoted?", "Who deserves a promotion?",
        "Should we reward ?", "Does  deserve a raise?", "Who is to blame?", "Whose fault is it?", "Can we trust ?",
        "Is  trustworthy?", "Is  lazy?", "Who is the laziest?", "Is  lazier than ?", "What is 's attitude like?",
        "Is  competent?", "Is  incompetent?", "Does  have the skills?", "Is  reliable?", "Is  unreliable?",
    ],
)
def test_every_personnel_word_family_ends_with_the_decision(words: str) -> None:
    assert personnel(words) and PERSONNEL_WORDS.search(words.lower()), words
    assert judging(words, [ADA]) == "personnel"


@pytest.mark.parametrize(
    "question",
    [
        "Which epics are worst?",  # about no one: the routing table answers it, as before
        "Top 5 tickets by cycle time",
        "Who has the most open breaches?",  # a count, not an evaluation
        "Top 5 people by breaches",
        "Rank people by open tickets",
        "Who holds the hardware tickets?",  # "hard" in another word
        "Who mentioned it on Slack?",
        "Why is W-AGE-01 firing so often?",
    ],
)
def test_a_question_that_evaluates_no_one_is_not_judging(question: str) -> None:
    assert judging(question) is None


def people_plan(question: str, mentions: tuple[str, ...] = (), names: list[str] = PEOPLE) -> Any:
    return route(question, names, project="", mentions=mentions)


@pytest.mark.parametrize(
    ("question", "mentions", "expected"),
    [
        ("Is  doing well overall?", (ADA,), [("oversight_person_scorecard", {"person_ids": [ADA], "days": 90})]),
        ("How is  doing over the last 30 days?", (ADA,), [("oversight_person_scorecard", {"person_ids": [ADA], "days": 30})]),
        ("Is  doing well this week?", (ADA,), [("oversight_person_scorecard", {"person_ids": [ADA], "days": 30})]),
        ("How has  performed over the last 6 months?", (ADA,),
         [("oversight_person_scorecard", {"person_ids": [ADA], "days": 180})]),
        ("How is  doing since the start?", (ADA,), [("oversight_person_scorecard", {"person_ids": [ADA], "days": 180})]),
        ("Is  doing well overall this quarter?", (ADA,),  # "overall" is in general, not all time
         [("oversight_person_scorecard", {"person_ids": [ADA], "days": 90})]),
        ("Is  better than  and ?", (ADA, BEN, CARMEN),
         [("oversight_person_scorecard", {"person_ids": [ADA, BEN, CARMEN], "days": 90})]),
        ("Which user is best on the board? Rank the others", (),
         [("oversight_people_compare", {"measure": "overall", "days": 90, "order": "closer"})]),
        ("Rank everyone", (), [("oversight_people_compare", {"measure": "overall", "days": 90, "order": "closer"})]),
        ("Who is best at estimating before they start?", (),
         [("oversight_people_compare", {"measure": "estimated_before_start", "days": 90, "order": "closer"})]),
        ("Who keeps best within the effort limit?", (),
         [("oversight_people_compare", {"measure": "within_estimate", "days": 90, "order": "closer"})]),
        ("Who is worst at logging time?", (),
         [("oversight_people_compare", {"measure": "time_logged", "days": 90, "order": "fewest"})]),
        ("Whose tickets are worst for getting stuck?", (),
         [("oversight_people_compare", {"measure": "stuck_now", "days": 90, "order": "most"})]),
        ("Who is worst for reopened work?", (),
         [("oversight_people_compare", {"measure": "reopened", "days": 90, "order": "most"})]),
        ("Who skips stages the worst?", (),
         [("oversight_people_compare", {"measure": "skipped_stage", "days": 90, "order": "most"})]),
        ("Who is best at code review?", (),
         [("oversight_people_compare", {"measure": "code_review", "days": 90, "order": "closer"})]),
        ("Who has the worst open findings?", (),
         [("oversight_people_compare", {"measure": "open_findings_per_10", "days": 90, "order": "most"})]),
        ("Rank the top 5 developers", (),
         [("oversight_people_compare", {"measure": "overall", "days": 90, "order": "closer", "n": 5})]),
        ("Should we fire ?", (ADA,), [("oversight_person_scorecard", {"person_ids": [ADA], "days": 90})]),
        ("Who should be promoted?", (),
         [("oversight_people_compare", {"measure": "overall", "days": 90, "order": "closer"})]),
        ("What is 's backlog?", (ADA,), [("oversight_person_tickets", {"person": ADA, "state": "open", "n": 200})]),
        ("What does  have to do?", (ADA,), [("oversight_person_tickets", {"person": ADA, "state": "open", "n": 200})]),
        ("Show 's open work", (ADA,), [("oversight_person_tickets", {"person": ADA, "state": "open", "n": 200})]),
        ("What is  working on?", (ADA,), [("oversight_person_tickets", {"person": ADA, "state": "in_progress"})]),
        ("Top 5 people by breaches", (), [("oversight_people", {"rank_by": "open_breaches", "n": 5})]),
        ("Rank people by open tickets", (), [("oversight_people", {"rank_by": "open_tickets"})]),
        ("Who has the most open warnings?", (), [("oversight_people", {"rank_by": "open_warnings"})]),
        ("Rank everyone by open findings", (), [("oversight_people", {"rank_by": "open_findings"})]),
        ("Rank people by reopened tickets", (),
         [("oversight_people_compare", {"measure": "reopened", "days": 90, "order": "closer"})]),
        ("Rank everyone by stuck tickets", (),
         [("oversight_people_compare", {"measure": "stuck_now", "days": 90, "order": "closer"})]),
        ("Rank everyone on estimating before work starts", (),
         [("oversight_people_compare", {"measure": "estimated_before_start", "days": 90, "order": "closer"})]),
        ("Top 3 people by tickets that skipped a stage", (),
         [("oversight_people_compare", {"measure": "skipped_stage", "days": 90, "order": "most", "n": 3})]),
        ("Rank everyone by tickets with time logged over the last 30 days", (),
         [("oversight_people_compare", {"measure": "time_logged", "days": 30, "order": "closer"})]),
    ],
)
def test_people_level_routing_of_questions_that_evaluate_people(
    question: str, mentions: tuple[str, ...], expected: list[tuple[str, dict[str, Any]]]
) -> None:
    planned = people_plan(question, mentions)
    assert planned.understood and [(call.name, call.params) for call in planned.calls] == expected


@pytest.mark.parametrize(("question", "mentions"), [("Is  better than ?", (ADA, BEN)), ("Rank everyone", ()),
                                                    ("Who should be promoted?", ())])
def test_comparing_people_turned_off_is_the_sentence(question: str, mentions: tuple[str, ...]) -> None:
    planned = people_plan(question, mentions, OFF)
    assert (planned.understood, planned.calls, planned.reason) == (False, [], COMPARISON_OFF_REASON)
    assert COMPARISON_OFF == "Comparing people side by side is turned off. Ask about one person at a time instead."
    one = people_plan("Is  doing well?", (ADA,), OFF)
    assert [(call.name, call.params) for call in one.calls] == [("oversight_person_scorecard",
                                                                 {"person_ids": [ADA], "days": 90})]


def test_team_level_routing_is_unchanged() -> None:
    for question in ("Who is best on the board?", "Should we fire the slowest developer?", "Is Ada doing well?"):
        planned = route(question, list(CATALOG_NAMES), project="")
        assert (planned.understood, planned.calls) == (False, [])
        assert planned.reason == PERSON_REASON or question == "Is Ada doing well?"


def test_the_tools_are_people_levels_only_and_the_comparison_only_when_on() -> None:
    assert {"oversight_person_scorecard", "oversight_people_compare"} <= set(PEOPLE_CATALOG)
    assert not {"oversight_person_scorecard", "oversight_people_compare"} & set(CATALOG)  # nor the MCP server
    assert "oversight_people_compare" not in specs_for("people", comparison=False)
    assert "oversight_person_scorecard" in specs_for("people", comparison=False)
    assert not {"oversight_person_scorecard", "oversight_people_compare"} & set(specs_for("team"))
    names = {tool["name"] for tool in analyst_tools("people", comparison=False)}
    assert "oversight_person_scorecard" in names and "oversight_people_compare" not in names
    for name in ("oversight_person_scorecard", "oversight_people_compare"):
        with pytest.raises(ValueError, match="unknown query"):
            validate_params(name, {"person_ids": [ADA]})  # the team catalogue does not know them


def labels_where(rows: list[dict[str, Any]], where: str) -> list[str]:
    return [row["label"] for row in rows if row["vs_board"] == where]


def joined(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def expected_scorecard_paragraph(name: str, rows: list[dict[str, Any]], *, alone: bool) -> str:
    clauses = [f"{words} {joined(labels_where(rows, where))}" for where, words in
               (("closer", "closer to the process on"), ("further", "further from it on"), ("level", "level on"))
               if labels_where(rows, where)]
    text = f"{name}, last 90 days, next to the board" + (f": {'; '.join(clauses)}." if clauses else ".")
    few = [row["label"] for row in rows if row["measurable"] and row["kind"] != "count" and row["vs_board"] is None]
    if few:
        text += f" Too few tickets to compare: {joined(few)}."
    if alone:
        text += " Not measurable on this board: Code review recorded."
    return text


def test_one_person_judged_gets_their_scorecard_word_for_word(client: TestClient, demo: Settings) -> None:
    ada = board(client)["Ada Brooks"]
    body = post(client, {"question": "Is @Ada Brooks doing well overall?", "level": "people", "mentions": [ada["id"]]})
    assert body["outcome"] == "answered" and body["model"] is None and llm_calls(demo, body["qa_id"]) == 0
    assert body["plan"] == [{"tool": "oversight_person_scorecard", "params": {"person_ids": [ada["id"]], "days": 90}}]
    (facts,) = body["facts"]
    assert [column["key"] for column in facts["columns"]] == [column.key for column in SCORECARD_COLUMNS]
    assert [row["measure"] for row in facts["rows"]] == [measure.key for measure in MEASURES]
    assert {(row["person_id"], row["name"]) for row in facts["rows"]} == {(ada["id"], "Ada Brooks")}
    paragraphs = body["answer"].split("\n\n")
    assert paragraphs == [expected_scorecard_paragraph("Ada Brooks", facts["rows"], alone=True), DEMO_CAVEAT]
    assert "closer to the process on Estimated before work started" in paragraphs[0]  # her habit in the demo
    assert not PERSON_ID.search(body["answer"]) and body["people"] == [ada]
    assert all(citation["text"] in body["answer"] for citation in body["citations"])
    row = qa_row(demo, body["qa_id"])
    assert (row["level"], json.loads(row["mentions_json"]), row["outcome"]) == ("people", [ada["id"]], "answered")
    assert "Ada" not in row["mentions_json"]


def test_several_people_side_by_side(client: TestClient) -> None:
    people = board(client)
    picked = [people[name] for name in ("Finn Gallagher", "Ada Brooks", "Hana Mori")]
    question = "Are " + ", ".join(f"@{who['name']}" for who in picked) + " doing well?"
    body = post(client, {"question": question, "level": "people", "mentions": [who["id"] for who in picked]})
    assert body["plan"] == [{"tool": "oversight_person_scorecard",
                             "params": {"person_ids": [who["id"] for who in picked], "days": 90}}]
    (facts,) = body["facts"]
    order = list(dict.fromkeys(row["name"] for row in facts["rows"]))
    assert order == ["Ada Brooks", "Finn Gallagher", "Hana Mori"]  # the directory's order
    expected = [expected_scorecard_paragraph(name, [row for row in facts["rows"] if row["name"] == name], alone=False)
                for name in order]
    assert body["answer"].split("\n\n") == [*expected, "Not measurable on this board: Code review recorded.",
                                            DEMO_CAVEAT]
    assert "Hana Mori, last 90 days, next to the board. Too few tickets to compare: " in body["answer"]  # part time
    assert body["people"] == [people[name] for name in order]


def test_who_is_best_gets_the_comparison_closer_first_never_best(client: TestClient) -> None:
    body = post(client, {"question": "Which user is best on the board? Rank the others.", "level": "people"})
    assert body["outcome"] == "answered"
    assert body["plan"] == [{"tool": "oversight_people_compare",
                             "params": {"measure": "overall", "days": 90, "order": "closer"}}]
    (facts,) = body["facts"]
    assert [column["key"] for column in facts["columns"]] == [column.key for column in OVERALL_COLUMNS]
    compared = [row for row in facts["rows"] if row["compared"]]
    closer = [row["closer"] for row in compared]
    assert closer == sorted(closer, reverse=True) and facts["rows"][-1]["standing"] == "Too few tickets"
    assert facts["rows"][-1]["name"] == "Hana Mori"  # too few recent tickets: last, labelled so
    first, last = body["answer"].split("\n\n")
    assert first == ("Measures closer to the process than the board, last 90 days, most first: "
                     + joined([f"{row['name']} {row['closer']} of {row['compared']}" for row in compared])
                     + ". Too few tickets to compare: Hana Mori.")
    assert last == DEMO_CAVEAT and "best" not in body["answer"].lower()


def test_comparison_by_one_measure_with_the_whole_boards_figure(client: TestClient) -> None:
    body = post(client, {"question": "Who is best at estimating before they start?", "level": "people"})
    (facts,) = body["facts"]
    assert [column["key"] for column in facts["columns"]] == [column.key for column in COMPARE_COLUMNS]
    whole = facts["rows"][-1]
    assert (whole["person_id"], whole["name"], whole["standing"]) == (None, "Whole board", None)
    ranked = [row for row in facts["rows"][:-1] if row["enough"]]
    assert [row["value"] for row in ranked] == sorted((row["value"] for row in ranked), reverse=True)
    assert ranked[0]["name"] == "Ada Brooks" and ranked[0]["standing"] == "Closer to the process"
    first = body["answer"].split("\n\n")[0]
    assert first.startswith("Estimated before work started, last 90 days, closest to the process first: Ada Brooks ")
    assert f"The whole board: {whole['value']}%." in first and first.endswith("Too few tickets to compare: Hana Mori.")
    assert body["people"][0]["name"] == "Ada Brooks"


def test_a_personnel_question_gets_the_figures_then_the_decision(client: TestClient, demo: Settings) -> None:
    ada = board(client)["Ada Brooks"]
    body = post(client, {"question": "Should we fire @Ada Brooks?", "level": "people", "mentions": [ada["id"]]})
    assert body["outcome"] == "answered" and JUDGEMENT_DECLINE not in body["answer"]
    paragraphs = body["answer"].split("\n\n")
    assert paragraphs[-2:] == [DECISION_YOURS, DEMO_CAVEAT]
    assert DECISION_YOURS == "The figures are above; the decision is yours."
    everyone = post(client, {"question": "Who deserves a promotion?", "level": "people"})
    assert everyone["plan"][0]["tool"] == "oversight_people_compare"
    assert everyone["answer"].split("\n\n")[-2:] == [DECISION_YOURS, DEMO_CAVEAT]


def test_the_backlog_of_someone_is_their_open_tickets(client: TestClient) -> None:
    ada = board(client)["Ada Brooks"]
    body = post(client, {"question": "What is the backlog of @Ada Brooks?", "level": "people", "mentions": [ada["id"]]})
    assert body["plan"] == [{"tool": "oversight_person_tickets",
                             "params": {"person": ada["id"], "state": "open", "n": 200}}]
    assert body["answer"].startswith("Ada Brooks has ") and "open ticket" in body["answer"]
    assert DEMO_CAVEAT not in body["answer"]  # not a scorecard: no caveat
    card = client.get("/api/v1/people/scorecard", params={"level": "people", "person": ada["id"]}).json()
    (facts,) = body["facts"]
    assert len(facts["rows"]) == card["people"][0]["values"]["open_tickets"]["value"] and not facts["truncated"]
    assert all(row["issue_type"] != "Epic" for row in facts["rows"]) and "Epics left out" in facts["note"]
    assert body["answer"].startswith(f"Ada Brooks has {len(facts['rows'])} open tickets")


def test_a_member_with_no_tickets_beside_someone_judged(client: TestClient) -> None:
    people = board(client)
    ada, noor = people["Ada Brooks"], people["Noor Haddad"]
    body = post(client, {"question": "Should we promote @Ada Brooks or @Noor Haddad?", "level": "people",
                         "mentions": [ada["id"], noor["id"]]})
    paragraphs = body["answer"].split("\n\n")
    assert paragraphs[-3:] == ["Noor Haddad has no tickets on this board.", DECISION_YOURS, DEMO_CAVEAT]
    assert body["people"] == [ada, noor]


def test_comparing_people_turned_off_on_every_path(off: TestClient, demo: Settings) -> None:
    people = board(off)
    ada, ben = people["Ada Brooks"], people["Ben Okafor"]
    for body in (
        {"question": "Is @Ada Brooks better than @Ben Okafor?", "level": "people", "mentions": [ada["id"], ben["id"]]},
        {"question": "Who is best on the board?", "level": "people"},
        {"question": "Who should we promote?", "level": "people"},
    ):
        answer = post(off, body)
        assert (answer["outcome"], answer["message"], answer["answer"]) == ("not_understood", COMPARISON_OFF, None)
        assert answer["plan"] == answer["facts"] == answer["people"] == []
        assert llm_calls(demo, answer["qa_id"]) == 0
    one = post(off, {"question": "Is @Ada Brooks doing well?", "level": "people", "mentions": [ada["id"]]})
    assert one["outcome"] == "answered" and one["answer"].endswith(DEMO_CAVEAT)
    off_settings = demo.model_copy(update={"people_comparison": "off"})
    refused = _ask(off_settings, NoModel(), "Who is best on the board?", level="people",
                   people=replace(scope_of(demo), comparison=False))
    assert (refused.outcome, refused.message) == ("not_understood", COMPARISON_OFF)
    scope = replace(scope_of(demo), comparison=False)
    from oversight.ask.catalog import CatalogContext

    ctx = CatalogContext(process=ProcessConfig.load(demo.process_file), rules=RulesConfig.load(demo.rules_file),
                         project="DEMO", now=NOW, people=scope)
    conn = open_readonly(demo.db_file)
    try:
        spec = PEOPLE_CATALOG["oversight_person_scorecard"]
        with pytest.raises(ComparisonOff, match="turned off"):
            spec.run(conn, validate_params(spec.name, {"person_ids": [ada["id"], ben["id"]]}, PEOPLE_CATALOG), ctx)
        compare = PEOPLE_CATALOG["oversight_people_compare"]
        with pytest.raises(ComparisonOff):
            compare.run(conn, validate_params(compare.name, {}, PEOPLE_CATALOG), ctx)
    finally:
        conn.close()


class NoModel:
    name = "none"

    def structured(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the model was asked")

    compose = converse = structured


def test_the_caveat_with_and_without_its_last_sentence(tmp_path: Path) -> None:
    assert caveat(30, records_code_review=True) == (
        "Figures from Jira over the last 30 days, next to the whole board. Tickets count for their assignee; tickets "
        "without one count for no one.")
    assert caveat(90, records_code_review=False, credit_field="Programmer") == DEMO_CAVEAT
    assert CAVEAT_HERE == " Code review isn't recorded on this board, so reviewers' work doesn't show."
    from tests.test_scorecard import build as build_boards
    from tests.test_scorecard import pid as board_pid
    from tests.test_scorecard import serve, write_config

    build_boards(tmp_path / "oversight.db")
    with serve(tmp_path, write_config(tmp_path)) as rev:
        body = post(rev, {"question": "How is @Alpha Tester doing over the last 30 days?", "level": "people",
                          "mentions": [board_pid("alpha")]})
    assert body["answer"].split("\n\n")[-1] == caveat(30, records_code_review=True, credit_field="Programmer")
    assert "Not measurable" not in body["answer"]


@pytest.mark.parametrize(
    ("text", "labels"),
    [
        ("Ada Brooks is lazy.", ["lazy"]), ("The laziest of them", ["laziest"]), ("lazier than most", ["lazier"]),
        ("Useless work", ["useless"]), ("An INCOMPETENT estimate", ["incompetent"]), ("careless", ["careless"]),
        ("dishonest and insincere", ["dishonest", "insincere"]), ("stupid", ["stupid"]),
        ("an idiotic plan, idiots", ["idiotic", "idiots"]), ("hopeless, worthless, pathetic",
                                                             ["hopeless", "worthless", "pathetic"]),
        ("Ada Brooks is closer to the process than the board on 5 of 8 measures.", []),
        ("Sincere thanks; a careful estimate; hopes", []),
    ],
)
def test_the_words_that_label_character(text: str, labels: list[str]) -> None:
    assert character_labels(text) == labels


@pytest.mark.usefixtures("audit_shim")
def test_a_written_answer_that_labels_someone_is_rewritten_once(demo: Settings) -> None:
    scope = scope_of(demo)
    ada = next(who for who in scope.people if who["name"] == "Ada Brooks")
    planned = plan(("oversight_person_scorecard", {"person_ids": [ada["id"]]}))
    fine = "Ada Brooks is closer to the process than the board on some measures."
    provider = FakeProvider(plans=[planned], answers=["Ada Brooks is lazy about time logging.", fine])
    response = _ask(demo, provider, "Show @Ada Brooks's figures", level="people", mentions=[ada["id"]], people=scope)
    assert response.outcome == "answered" and response.answer == f"{fine}\n\n{DEMO_CAVEAT}"
    first, strict = provider.compose_calls
    assert "labels someone's character (lazy)" in strict.user


@pytest.mark.usefixtures("audit_shim")
def test_a_written_answer_that_still_labels_someone_gives_way_to_the_answer_built_in_code(demo: Settings) -> None:
    scope = scope_of(demo)
    ada = next(who for who in scope.people if who["name"] == "Ada Brooks")
    planned = plan(("oversight_person_scorecard", {"person_ids": [ada["id"]]}))
    provider = FakeProvider(plans=[planned], answers=["Ada Brooks is useless.", "Ada Brooks is careless."])
    response = _ask(demo, provider, "Show @Ada Brooks's figures", level="people", mentions=[ada["id"]], people=scope)
    assert response.outcome == "answered" and response.extra["answer"] == "code"
    built = code_built(facts.to_dict() for facts in response.facts)
    assert response.answer == f"{built}\n\n{DEMO_CAVEAT}"
    assert not character_labels(response.answer) and response.people == [ada]


def test_the_analysts_answer_is_checked_for_labels_rewritten_then_built_in_code(demo: Settings) -> None:
    scope = scope_of(demo)
    ada = next(who for who in scope.people if who["name"] == "Ada Brooks")
    card = use("oversight_person_scorecard", {"person_ids": [ada["id"]]})
    fixed = FakeAnalyst([turn(uses=[card]), turn(text="Ada Brooks is lazy."),
                         turn(text="Ada Brooks is closer to the process than the board on some measures.")])
    response = _ask(demo, fixed, "Is @Ada Brooks doing well?", level="people", mentions=[ada["id"]], people=scope)
    assert response.outcome == "answered" and response.answer.endswith(f"some measures.\n\n{DEMO_CAVEAT}")
    retry = fixed.calls[2]["messages"][-1]["content"]
    assert "labels someone's character (lazy)" in retry

    stubborn = FakeAnalyst([turn(uses=[card]), turn(text="Ada Brooks is lazy."), turn(text="Ada Brooks is hopeless.")])
    response = _ask(demo, stubborn, "Is @Ada Brooks doing well?", level="people", mentions=[ada["id"]], people=scope)
    assert response.outcome == "answered" and response.extra["answer"] == "code"
    assert response.answer.startswith("Ada Brooks, last 90 days, next to the board") and not character_labels(
        response.answer)


def test_the_analyst_answers_a_personnel_question_with_the_decision_and_the_caveat(demo: Settings) -> None:
    scope = scope_of(demo)
    ada = next(who for who in scope.people if who["name"] == "Ada Brooks")
    analyst = FakeAnalyst([turn(uses=[use("oversight_person_scorecard", {"person_ids": [ada["id"]], "days": 30})]),
                           turn(text="Ada Brooks is closer to the process than the board on some measures.")])
    response = _ask(demo, analyst, "Should we promote @Ada Brooks?", level="people", mentions=[ada["id"]], people=scope)
    assert response.answer.split("\n\n")[-2:] == [DECISION_YOURS,
                                                   caveat(30, records_code_review=False, credit_field="Programmer")]
    system = analyst.calls[0]["system"]
    assert JUDGEMENT_DECLINE not in system and "Never insult anyone or label their character" in system


def test_named_in_finds_full_names_folded_longest_first(demo: Settings) -> None:
    scope = scope_of(demo)
    everyone = [*scope.people, *scope.members]

    def found(question: str) -> list[list[str]]:
        return [[who["name"] for who in group] for group in named_in(question, everyone)]

    assert found("Is ada brooks better than TOMAS FERREIRA?") == [["Ada Brooks"], ["Tomás Ferreira"]]
    assert found("Élif Aydın's open findings") == [["Elif Aydin"]]
    assert found("What did Ada say?") == [] and found("Unassigned tickets") == []


def test_a_name_two_people_share_asks_for_at(tmp_path: Path) -> None:
    from tests.test_api_people import build as build_people
    from tests.test_api_people import serve as serve_people

    build_people(tmp_path / "oversight.db")
    with serve_people(tmp_path) as shared:
        body = post(shared, {"question": "Is Whiskey Tester doing well?", "level": "people"})
        assert (body["outcome"], body["message"]) == (
            "not_understood", "More than one person on this board is called Whiskey Tester. Type @ and pick the one "
                              "you mean.")
        assert body["plan"] == body["facts"] == body["people"] == []
        whiskey = next(row["person"] for row in shared.get("/api/v1/people").json()["people"]
                       if row["person"] and row["person"]["name"] == "Whiskey Tester")
        picked = post(shared, {"question": "Is @Whiskey Tester doing well?", "level": "people",
                               "mentions": [whiskey["id"]]})
        assert picked["outcome"] == "answered" and picked["people"] == [whiskey]


def test_the_scorecard_view_holds_everyone_with_tickets_and_the_board(demo: Settings) -> None:
    rules = RulesConfig.load(demo.rules_file)
    scope = scope_of(demo)
    query = SnapshotQuery(demo.db_file, project="DEMO", sprint_field=None, rules=rules, people=scope)
    try:
        columns, rows, _more = query.run("SELECT * FROM scorecard")
        assert columns == ["person_id", "name", "measure", "label", "kind", "direction", "measurable", "value",
                           "numerator", "denominator", "shown", "enough", "vs_board", "board_value",
                           "board_numerator", "board_denominator", "board_shown", "days", "period_from", "period_to"]
        assert len(rows) == (1 + len(scope.people)) * len(MEASURES)
        board_rows = query.run("SELECT measure, value FROM scorecard WHERE person_id IS NULL")[1]
        assert len(board_rows) == len(MEASURES)
        shown = query.run("SELECT shown FROM scorecard WHERE name = 'Ada Brooks' AND measure = 'time_logged'")[1]
        assert re.fullmatch(r"\d+% · \d+ of \d+", shown[0][0])
        whole = query.run("SELECT value FROM scorecard WHERE name = 'Ada Brooks' AND kind = 'rate' LIMIT 1")[1]
        assert isinstance(whole[0][0], int)  # a whole percentage stays whole
    finally:
        query.close()
    ada = next(who for who in scope.people if who["name"] == "Ada Brooks")
    only = SnapshotQuery(demo.db_file, project="DEMO", sprint_field=None, rules=rules,
                         people=replace(scope, comparison=False), mentioned=[ada["id"]])
    try:
        assert {row[0] for row in only.run("SELECT DISTINCT name FROM scorecard")[1]} == {None, "Ada Brooks"}
    finally:
        only.close()
    nobody = SnapshotQuery(demo.db_file, project="DEMO", sprint_field=None, rules=rules,
                           people=replace(scope, comparison=False), mentioned=[ada["id"], scope.people[1]["id"]])
    try:
        assert {row[0] for row in nobody.run("SELECT DISTINCT person_id FROM scorecard")[1]} == {None}
    finally:
        nobody.close()


def test_an_analyst_answer_from_the_scorecard_view_ends_with_the_caveat(demo: Settings) -> None:
    scope = scope_of(demo)
    sql = "SELECT name, shown FROM scorecard WHERE measure = 'time_logged' AND name = 'Ada Brooks'"
    query = SnapshotQuery(demo.db_file, project="DEMO", sprint_field=None, rules=RulesConfig.load(demo.rules_file),
                          people=scope)
    try:
        shown = query.run(sql)[1][0][1]
    finally:
        query.close()
    percent = shown.split(" ")[0]
    analyst = FakeAnalyst([turn(uses=[use(QUERY_TOOL, {"sql": sql})]),
                           turn(text=f"Ada Brooks logged time on {percent} of her done work.")])
    response = _ask(demo, analyst, "How often does Ada Brooks log time?", level="people", people=scope)
    assert response.outcome == "answered"
    assert response.answer == f"Ada Brooks logged time on {percent} of her done work.\n\n{DEMO_CAVEAT}"
