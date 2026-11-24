from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from oversight.ask.people import CompareParams, loosely_named_in, no_tickets_sentence
from oversight.ask.prompts import PEOPLE_HELP, ambiguous_first_name
from oversight.ask.router import CATALOG_NAMES, PEOPLE_NAMES, asks_of_someone, route
from oversight.ask.scorecards import character_labels
from oversight.ask.service import _NOT_UNDERSTOOD_DEFAULT
from oversight.llm.scripted import ScriptedProvider, _render
from oversight.settings import Settings
from tests.test_ask_scorecard import _ask, board, client, demo, post, qa_row, scope_of  # noqa: F401

PEOPLE = [*CATALOG_NAMES, *PEOPLE_NAMES]
OFF = [name for name in PEOPLE if name != "oversight_people_compare"]


def people_plan(question: str, mentions: tuple[str, ...] = (), catalog: list[str] = PEOPLE) -> Any:
    return route(question, catalog, project="DEMO", mentions=mentions)


def calls(planned: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(call.name, {key: value for key, value in call.params.items() if key != "project"})
            for call in planned.calls]


def ask(client: TestClient, question: str, mentions: list[str] | None = None) -> dict[str, Any]:
    return post(client, {"question": question, "level": "people", "mentions": mentions or []})


OVERALL = [("oversight_people_compare", {"measure": "overall", "days": 90, "order": "closer"})]
FURTHER_FIRST = [("oversight_people_compare", {"measure": "overall", "days": 90, "order": "fewest"})]
MEMBERS = ("oversight_board_people", {"holding": "none"})


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Which developers are struggling?", FURTHER_FIRST),
        ("Who needs help?", FURTHER_FIRST),
        ("Who needs more support?", FURTHER_FIRST),
        ("Who is falling behind?", FURTHER_FIRST),
        ("Which people are behind?", FURTHER_FIRST),
        ("Who is the MVP?", OVERALL),
        ("Who is the star of the board?", OVERALL),
        ("Who is overloaded?", [("oversight_people_compare", {"measure": "open_tickets", "order": "most"})]),
        ("Who is swamped?", [("oversight_people_compare", {"measure": "open_tickets", "order": "most"})]),
        ("Who is idle?", [("oversight_people_compare", {"measure": "in_progress", "order": "fewest"}), MEMBERS]),
        ("Who has nothing to do?", [("oversight_people_compare", {"measure": "open_tickets", "order": "fewest"}),
                                    MEMBERS]),
        ("Who has spare capacity?", [("oversight_people_compare", {"measure": "open_tickets", "order": "fewest"}),
                                     MEMBERS]),
        ("Who contributes least?", [("oversight_people_compare", {"measure": "done_in_period", "days": 90,
                                                                  "order": "fewest"})]),
        ("Who is the biggest contributor this month?",
         [("oversight_people_compare", {"measure": "done_in_period", "days": 30, "order": "most"})]),
        ("List the members of this board", [("oversight_board_people", {"holding": "any"})]),
        ("Who is on this board?", [("oversight_board_people", {"holding": "any"})]),
        ("Who has no tickets?", [MEMBERS]),
        ("Which people have no tickets?", [MEMBERS]),
    ],
)
def test_the_questions_the_check_found_turned_away_are_planned(question: str, expected: list[Any]) -> None:
    planned = people_plan(question)
    assert planned.understood and calls(planned) == expected, question


@pytest.mark.parametrize(
    "question",
    ["Which developers are struggling?", "Who needs help?", "Who is falling behind?", "Which people are behind?",
     "Who is overloaded?", "Who is idle?", "Who contributes least?", "Who is the MVP?",
     "List the members of this board", "Who has no tickets?"],
)
def test_none_of_them_gets_the_help_or_the_not_understood_message(client: TestClient, question: str) -> None:
    body = ask(client, question)
    assert body["outcome"] == "answered", (question, body["message"])
    assert body["message"] is None and PEOPLE_HELP not in (body["answer"] or "")
    assert body["plan"] and body["people"]


def test_with_comparison_off_a_count_ranking_is_still_the_refusal_and_the_board_list_is_not() -> None:
    assert people_plan("Who is overloaded?", catalog=OFF).understood is False
    assert calls(people_plan("Who is on this board?", catalog=OFF)) == [("oversight_board_people", {"holding": "any"})]


def test_who_is_on_the_board_names_everyone_on_it_members_with_no_tickets_too(client: TestClient) -> None:
    directory = client.get("/api/v1/people/directory").json()["people"]
    holders = [entry["person"]["name"] for entry in directory if entry["tickets"] > 0]
    members = [entry["person"]["name"] for entry in directory if entry["tickets"] == 0]
    assert members, "the demo has members with no tickets"
    body = ask(client, "Who is on this board?")
    (facts,) = body["facts"]
    assert [column["key"] for column in facts["columns"]] == ["person_id", "name", "tickets"]
    assert [row["name"] for row in facts["rows"]] == holders + members
    assert all(row["tickets"] == 0 for row in facts["rows"][len(holders):])
    held = ", ".join(holders[:-1]) + f" and {holders[-1]}"
    others = ", ".join(members[:-1]) + f" and {members[-1]}"
    assert body["answer"] == (f"{len(directory)} people are on this board: {held}, and {len(members)} members who "
                              f"hold none of its tickets: {others}.")
    assert all(body["answer"].count(name) == 1 for name in holders + members)
    assert [who["name"] for who in body["people"]] == holders + members
    assert not re.search(r"\bp[0-9a-f]{12}\b", body["answer"])

    none = ask(client, "Who has no tickets?")
    assert none["answer"] == no_tickets_sentence(members)
    assert [row["name"] for row in none["facts"][0]["rows"]] == members


def test_where_every_member_holds_a_ticket_the_answer_says_so() -> None:
    empty = {"tool": "oversight_board_people", "params": {"holding": "none"}, "rows": [], "note": "", "columns": []}
    assert _render(empty) == ["Everyone on this board holds at least one of its tickets."]


@pytest.mark.parametrize(
    ("tickets", "in_all", "said"),
    [
        ([4, 0], 2, "2 people are on this board: Ada Brooks, and 1 member who holds none of its tickets: Ben Okafor."),
        ([4, 2], 2, "2 people are on this board: Ada Brooks and Ben Okafor."),
        ([0, 0], 2, "2 people are on this board, all members who hold none of its tickets: Ada Brooks and Ben Okafor."),
        ([0], 1, "1 person is on this board, a member who holds none of its tickets: Ada Brooks."),
    ],
)
def test_who_is_on_the_board_says_each_name_once(tickets: list[int], in_all: int, said: str) -> None:
    names = ["Ada Brooks", "Ben Okafor"]
    rows = [{"person_id": f"p{n:012x}", "name": names[n], "tickets": count} for n, count in enumerate(tickets)]
    fact = {"tool": "oversight_board_people", "params": {"holding": "any"}, "rows": rows,
            "note": f"everyone on this board; {in_all} in all", "columns": []}
    assert _render(fact) == [said]


def test_a_lower_case_first_name_is_that_person_when_only_one_has_it(client: TestClient, demo: Settings) -> None:
    ada = board(client)["Ada Brooks"]
    body = ask(client, "how is ada doing?")
    assert body["outcome"] == "answered" and body["people"] == [ada]
    assert body["plan"] == [{"tool": "oversight_person_scorecard", "params": {"person_ids": [ada["id"]], "days": 90}}]
    assert body["answer"].startswith("Ada Brooks, last 90 days, next to the board")
    row = qa_row(demo, body["qa_id"])
    assert json.loads(row["mentions_json"]) == [ada["id"]] and "Ada" not in row["mentions_json"]

    working = ask(client, "what is ada working on?")
    assert working["plan"][0]["tool"] == "oversight_person_tickets"
    assert working["plan"][0]["params"]["state"] == "in_progress" and working["people"] == [ada]
    backlog = ask(client, "ada's backlog?")
    assert backlog["plan"][0]["params"] == {"person": ada["id"], "state": "open", "n": 200}


def test_a_lower_case_first_name_two_people_share_asks_for_at(demo: Settings) -> None:
    scope = scope_of(demo)
    other = {"id": "p00000000a0a0", "name": "Ada Lane", "avatar": None}
    response = _ask(demo, ScriptedProvider(), "how is ada doing?", level="people",
                    people=replace(scope, members=(*scope.members, other)))
    assert (response.outcome, response.message) == (
        "not_understood", ambiguous_first_name("Ada", ["Ada Brooks", "Ada Lane"]))


def test_a_word_that_is_also_a_first_name_is_no_one_where_no_name_stands() -> None:
    def person(name: str, n: int) -> dict[str, Any]:
        return {"id": f"p{n:012x}", "name": name, "avatar": None}

    everyone = [person("Ada Brooks", 1), person("Will Ash", 2), person("Dev Malhotra", 3), person("Prince", 4)]
    assert loosely_named_in("how is ada doing?", everyone) == [("Ada", [everyone[0]])]
    assert loosely_named_in("what's dev working on?", everyone) == [("Dev", [everyone[2]])]
    assert loosely_named_in("tickets for prince", everyone) == [("Prince", [everyone[3]])]
    assert loosely_named_in("when will the sprint end?", everyone) == []  # "will" is no name there
    assert loosely_named_in("when will dev finish the work?", everyone) == []
    assert not asks_of_someone("when will the sprint end?")
    assert asks_of_someone("how is ada doing?") and asks_of_someone("ada's backlog?")


def test_a_question_with_no_name_keeps_the_true_message(client: TestClient) -> None:
    body = ask(client, "when will the sprint end?")
    assert (body["outcome"], body["message"]) == ("not_understood", _NOT_UNDERSTOOD_DEFAULT) and body["people"] == []


def test_the_biggest_backlog_and_one_persons_backlog_are_the_same_figure(client: TestClient) -> None:
    ada = board(client)["Ada Brooks"]
    biggest = ask(client, "Who has the biggest backlog?")
    (facts,) = biggest["facts"]
    theirs = next(row for row in facts["rows"] if row["person_id"] == ada["id"])
    backlog = ask(client, "What is @Ada Brooks's backlog?", [ada["id"]])
    count = int(re.search(r"(\d+) in all", backlog["facts"][0]["note"]).group(1))  # type: ignore[union-attr]
    assert theirs["value"] == count and f"Ada Brooks {count}" in biggest["answer"]
    assert backlog["answer"].startswith(f"Ada Brooks has {count} open tickets")
    scorecard = client.get("/api/v1/people/scorecard", params={"level": "people", "person": ada["id"]}).json()
    assert scorecard["people"][0]["values"]["open_tickets"]["value"] == count
    nobody = next(row for row in facts["rows"] if row["name"] == "Unassigned")
    ranking, after = biggest["answer"].split(". Unassigned: ")
    assert "Unassigned" not in ranking and after.startswith(f"{nobody['value']} open tickets. The whole board: ")


def test_a_question_about_the_least_is_answered_fewest_first(client: TestClient) -> None:
    done = ask(client, "Who has the fewest done tickets this month?")
    (facts,) = done["facts"]
    values = [row["value"] for row in facts["rows"] if row["person_id"]]
    assert values == sorted(values) and facts["params"]["order"] == "fewest"
    assert done["answer"].startswith("Done in the period, last 30 days, fewest first: ")
    assert "most first" not in done["answer"]

    nothing = ask(client, "Who has nothing to do?")
    first, members = nothing["facts"]
    assert nothing["answer"].startswith("Open tickets, fewest first: ")
    assert [row["value"] for row in first["rows"] if row["person_id"]] == sorted(
        row["value"] for row in first["rows"] if row["person_id"])
    names = [row["name"] for row in members["rows"]]
    assert nothing["answer"].endswith(no_tickets_sentence(names))
    nobody = next(row for row in first["rows"] if row["name"] == "Unassigned")
    assert f". Unassigned: {nobody['value']} open tickets. The whole board: " in nothing["answer"]  # never ranked

    breaches = ask(client, "Who has the fewest open breaches?")
    rows = breaches["facts"][0]["rows"]
    people = [row for row in rows if row["person_id"]]
    assert rows[len(people):] == [row for row in rows if row["name"] == "Unassigned"]  # nobody's least: after
    assert [row["open_breaches"] for row in people] == sorted(row["open_breaches"] for row in people)
    assert " the fewest open breaches" in breaches["answer"] and "most" not in breaches["answer"]
    assert breaches["answer"].split(". ")[1] == f"Unassigned: {rows[-1]['open_breaches']} open breaches"


def test_the_order_is_for_every_measure() -> None:
    assert CompareParams(measure="in_progress", order="fewest").order == "fewest"
    assert CompareParams(measure="reopened", order="fewest").order == "fewest"
    assert CompareParams(measure="open_findings_per_10", order="most").order == "most"
    assert CompareParams(measure="overall").order == "closer"
    with pytest.raises(ValidationError):
        CompareParams(measure="reopened", order="worst")
    assert people_plan("Who has at least one open breach?").calls[0].params.get("order") is None


def test_a_count_of_now_says_now_once_and_states_no_period(client: TestClient) -> None:
    body = ask(client, "Who has the most tickets in progress?")
    (facts,) = body["facts"]
    assert "now now" not in facts["note"] and "by in progress now, most first" in facts["note"]
    assert "days" not in body["plan"][0]["params"] and "days" not in facts["params"]
    assert body["answer"].startswith("In progress now, most first: ")
    assert "last 90 days" not in body["answer"] and "Figures from Jira over" not in body["answer"]

    backlog = ask(client, "Who has the biggest backlog?")
    assert "by open tickets now, most first" in backlog["facts"][0]["note"]
    assert "days" not in backlog["facts"][0]["params"]

    done = ask(client, "Who finished the most work this month?")
    assert "days" in done["facts"][0]["params"] and done["answer"].split("\n\n")[-1].startswith(
        "Figures from Jira over the last 30 days")


def test_an_open_backlog_carries_no_period_in_its_facts(client: TestClient) -> None:
    ada = board(client)["Ada Brooks"]
    open_work = ask(client, "What is @Ada Brooks's backlog?", [ada["id"]])
    assert "days" not in open_work["facts"][0]["params"]
    done = ask(client, "What did @Ada Brooks finish this month?", [ada["id"]])
    assert done["facts"][0]["params"]["days"] == 30


def test_a_comparison_carries_n_only_for_a_questions_top_n(client: TestClient) -> None:
    everyone = ask(client, "Who is best on the board? Rank the others")
    assert "n" not in everyone["plan"][0]["params"] and "n" not in everyone["facts"][0]["params"]
    top = ask(client, "Rank the top 3 developers")
    assert top["plan"][0]["params"]["n"] == 3 and top["facts"][0]["params"]["n"] == 3
    assert len([row for row in top["facts"][0]["rows"] if row["person_id"]]) == 3


@pytest.mark.parametrize("text", ["Ana Lazić closed 12 tickets.", "Lazic logged time on 9 of 10.",
                                  "Eleni Lazidou is level with the board.", "Ivan Stupidov estimates first.",
                                  "Lazio is the team's name."])
def test_a_surname_or_a_place_is_no_label(text: str) -> None:
    assert character_labels(text) == []


def test_lazybones_is_a_label_and_a_board_name_is_not() -> None:
    assert character_labels("Ada Brooks is a lazybones.") == ["lazybones"]
    assert character_labels("Ada Brooks is a lazy-bones.") == ["lazy-bones"]
    assert character_labels("Ivy Lazy closed 12 tickets; Ms Lazy logged time.", ["Ivy Lazy"]) == []
    assert character_labels("Ivy Lazy is lazy.", ["Ivy Lazy"]) == ["lazy"]
