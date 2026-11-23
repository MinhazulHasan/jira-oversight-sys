"""The JO-11 review's findings on Ask at people level, each with the test that would have caught it (12 Nov 2026).

At people level nothing about people is declined (the product owner's direction): a question about people is
answered from the figures whenever one answers it per person, and the help (prompts.PEOPLE_HELP, which never says the
Jira data lacks it) is only for what no figure here answers per person. Personnel questions in any common wording get
the scorecard and the decision sentence. The wording check holds for every word family and for every message a model
writes. Comparing people, where an organisation turns it off, is refused per ask. The overall order never compares a
figure that rests on too few tickets. The answer built in code cites each figure to its own person's row, and a
measure's label is never a figure. The caveat names every period. The analyst's scorecard view says its period. Code
review counts only work the rules have checked. Against the seeded DEMO store (fictional people) and the hand-made
boards of test_scorecard; nothing calls a model or the network.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from oversight.api.scorecard import COMPARABLE, MEASURES, OVERALL, Standing, order
from oversight.ask.people import first_named_in, personnel, ranks_only
from oversight.ask.prompts import (
    COMPARISON_OFF,
    DECISION_YOURS,
    LAST_RESORT_SCORECARD,
    PEOPLE_HELP,
    ambiguous_first_name,
    caveat,
)
from oversight.ask.router import CATALOG_NAMES, COMPARISON_OFF_REASON, PEOPLE_NAMES, PEOPLE_REASON, judging, route
from oversight.ask.scorecards import OVERALL_COLUMNS, character_labels
from oversight.ask.service import ANALYST_DECLINED, _NOT_UNDERSTOOD_DEFAULT
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import to_iso
from tests.fakes.fake_llm import FakeProvider, audit_shim, plan  # noqa: F401  (audit_shim is a fixture)
from tests.test_analyst import FakeAnalyst, turn, use
from tests.test_ask_scorecard import DEMO_CAVEAT, _ask, board, client, demo, off, post, scope_of  # noqa: F401
from tests.test_scorecard import CONFIG_FIXTURES, DAY, S, T, build, serve, write_config

PEOPLE = [*CATALOG_NAMES, *PEOPLE_NAMES]
OFF = [name for name in PEOPLE if name != "oversight_people_compare"]
ADA, BEN = "pbc7fe62842ca", "p1234567890ab"


def people_plan(question: str, mentions: tuple[str, ...] = (), catalog: list[str] = PEOPLE) -> Any:
    return route(question, catalog, project="DEMO", mentions=mentions)


def calls(planned: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(call.name, {key: value for key, value in call.params.items() if key != "project"})
            for call in planned.calls]


def compare(measure: str, days: int = 90, order: str = "closer") -> list[tuple[str, dict[str, Any]]]:
    """The comparison as planned: its order is always in the plan (JO-14), closer to the process first with no
    direction word, highest first for "most", lowest first for "fewest"."""
    return [("oversight_people_compare", {"measure": measure, "days": days, "order": order})]


# --- nothing about people is declined -------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # Finished work: everyone by the count, most first (found by the review: "nothing ranks people by it").
        ("Who closed the most tickets this month?", compare("done_in_period", 30, "most")),
        ("Who finished the most work last quarter?", compare("done_in_period", order="most")),
        # A count of now has no period (found by the JO-11 check: "by In progress now now", "last 90 days").
        ("Who has the most tickets in progress?", [("oversight_people_compare", {"measure": "in_progress", "order": "most"})]),
        # A measure named with no ranking word is that measure's comparison, never the open-tickets ranking, in the
        # order the question asks (JO-14): the most, highest first; the least, lowest first; else closer first.
        ("Whose tickets get stuck the most?", compare("stuck_now", order="most")),
        ("Who has the most reopened tickets?", compare("reopened", order="most")),
        ("Who reopens the most work?", compare("reopened", order="most")),
        ("Who estimates before starting work?", compare("estimated_before_start")),
        ("Who logs the least time?", compare("time_logged", order="fewest")),
        ("Who logs their time?", compare("time_logged")),
        ("Who skips stages the most?", compare("skipped_stage", order="most")),
        # One ticket's assignee.
        ("Who is assigned DEMO-12?", [("oversight_ticket_assignee", {"issue_key": "DEMO-12"})]),
        ("Who is working on DEMO-716?", [("oversight_ticket_assignee", {"issue_key": "DEMO-716"})]),
        # The biggest backlog is counted as one person's backlog is (the scorecard's open tickets, found by the JO-11
        # check: two figures for one word); who is on the board is everyone on it, members with no tickets too.
        ("Who has the biggest backlog?", [("oversight_people_compare", {"measure": "open_tickets", "order": "most"})]),
        ("Who is on this board?", [("oversight_board_people", {"holding": "any"})]),
        # Evaluating words beyond JO-09's, and the people words beyond "who" and "everyone". The other side of the
        # process (not pulling their weight, lazy, slacking, underperforming, worst, useless, careless) is further from
        # it first (the JO-14 review): the answer orders by the figures and never uses the word.
        ("Who is the most diligent person here?", compare(OVERALL)),
        ("Who is the most careful with estimates?", compare("estimated_before_start", order="most")),
        ("Who is not pulling their weight?", compare(OVERALL, order="fewest")),
        ("Who is the most efficient developer?", compare(OVERALL)),
        ("Is anyone lazy?", compare(OVERALL, order="fewest")),
        ("Should anybody be fired?", compare(OVERALL)),
        ("Is anyone on the board slacking?", compare(OVERALL, order="fewest")),
        ("Which dev is the best?", compare(OVERALL)),
        ("Rank the assignees", compare(OVERALL)),
        ("Rank the team", compare(OVERALL)),
        ("Is somebody underperforming?", compare(OVERALL, order="fewest")),
        ("Which of the devs follows the process best?", compare(OVERALL)),
        ("Best performer this sprint?", compare(OVERALL, 30)),
        ("Which tester is worst?", compare(OVERALL, order="fewest")),
        # Personnel wording beyond the brief's list, and the labels an answer may never use.
        ("Who should we let go?", compare(OVERALL)),
        ("Who should be demoted?", compare(OVERALL)),
        ("Who should we dismiss?", compare(OVERALL)),
        ("Which developer should we replace?", compare(OVERALL)),
        ("Who should get a bonus?", compare(OVERALL)),
        ("Who is useless?", compare(OVERALL, order="fewest")),
        ("Who is careless with estimates?", compare("estimated_before_start", order="fewest")),
        ("Who is the most dishonest about logging time?", compare("time_logged", order="most")),
    ],
)
def test_a_question_about_people_is_answered_from_the_figures(question: str, expected: list[Any]) -> None:
    planned = people_plan(question)
    assert planned.understood and calls(planned) == expected, question


@pytest.mark.parametrize(
    ("question", "mentions", "expected"),
    [
        ("How many tickets did  finish this month?", (ADA,),
         [("oversight_person_tickets", {"person": ADA, "state": "done", "days": 30, "n": 200})]),
        ("How many tickets has  done in the last 30 days?", (ADA,),
         [("oversight_person_tickets", {"person": ADA, "state": "done", "days": 30, "n": 200})]),
        ("What did  get done over the last six months?", (ADA,),
         [("oversight_person_tickets", {"person": ADA, "state": "done", "days": 180, "n": 200})]),
        ("What are 's resolved warnings?", (ADA,),
         [("oversight_person_findings", {"person": ADA, "severity": "warning", "state": "resolved"})]),
        ("Compare  and ", (ADA, BEN), [("oversight_person_scorecard", {"person_ids": [ADA, BEN], "days": 90})]),
        ("  vs  ", (ADA, BEN), [("oversight_person_scorecard", {"person_ids": [ADA, BEN], "days": 90})]),
        ("Set  and  side by side", (ADA, BEN),
         [("oversight_person_scorecard", {"person_ids": [ADA, BEN], "days": 90})]),
        # A count named stays JO-09's raw counts, side by side.
        ("Compare  and  by open breaches", (ADA, BEN),
         [("oversight_people", {"rank_by": "open_breaches", "person_ids": [ADA, BEN]})]),
    ],
)
def test_one_or_two_people_mentioned(question: str, mentions: tuple[str, ...], expected: list[Any]) -> None:
    planned = people_plan(question, mentions)
    assert planned.understood and calls(planned) == expected, question


@pytest.mark.parametrize("question", ["Should we let  go?", "Should we demote ?", "Should  get a bonus?",
                                      "Should  get a pay rise?", "Is  useless?", "Is  careless?",
                                      "Should  be laid off?", "Should  be made redundant?", "Write an appraisal of ",
                                      "Should we terminate ?", "Should we dismiss ?"])
def test_every_common_personnel_wording_gets_the_scorecard_and_the_decision(question: str) -> None:
    assert judging(question, [ADA]) == "personnel" and personnel(question)
    assert calls(people_plan(question, (ADA,))) == [("oversight_person_scorecard", {"person_ids": [ADA], "days": 90})]


@pytest.mark.parametrize("question", ["Who replaced the estimate on DEMO-5?", "Who raised this ticket?",
                                      "Is anyone careful?", "Which tickets are in dev?",
                                      "Who has the most tickets not done?"])
def test_everyday_words_are_not_personnel(question: str) -> None:
    assert not personnel(question)


def test_ranking_and_comparing_words_alone_do_not_evaluate() -> None:
    for question in ("Top 5 people by breaches", "Rank people by open tickets", "Compare  and  by open breaches"):
        assert ranks_only(question), question
    assert calls(people_plan("Rank everyone by open findings")) == [("oversight_people", {"rank_by": "open_findings"})]


def test_with_comparison_off_a_count_ranking_of_everyone_is_the_refusal_not_the_help() -> None:
    planned = people_plan("Who closed the most tickets?", catalog=OFF)
    assert (planned.understood, planned.reason) == (False, COMPARISON_OFF_REASON)


@pytest.mark.parametrize("question", ["Show cycle time per person", "Who writes the most comments?"])
def test_the_help_is_only_for_what_no_figure_answers_per_person(question: str) -> None:
    planned = people_plan(question)
    assert (planned.understood, planned.reason) == (False, PEOPLE_REASON) and "Jira data" not in PEOPLE_HELP


@pytest.mark.parametrize(("question", "days"), [
    ("How is  doing this sprint?", 30), ("How is  doing this week?", 30), ("over the last 7 days, how is  doing?", 30),
    ("How is  doing over a month?", 30), ("How is  doing over 30 days?", 30), ("How is  doing all time?", 180),
    ("How is  doing since the start?", 180), ("How is  doing over the last 26 weeks?", 180),
    ("How is  doing over the last half year?", 180), ("How is  doing overall?", 90),
])
def test_the_period_from_the_words(question: str, days: int) -> None:
    """The dashboard's sample reads the same words (src/server/sample/ask/scorecard.ts: daysIn)."""
    assert calls(people_plan(question, (ADA,)))[0][1]["days"] == days


def test_the_answers_from_the_demo(client: TestClient) -> None:
    people = board(client)
    ada = people["Ada Brooks"]
    month = client.get("/api/v1/people/scorecard", params={"level": "people", "days": 30}).json()
    done = {row["person"]["name"]: row["values"]["done_in_period"]["value"] for row in month["people"]}

    most = post(client, {"question": "Who closed the most tickets this month?", "level": "people"})
    assert most["outcome"] == "answered" and most["plan"][0]["params"]["measure"] == "done_in_period"
    ranked = sorted(done.items(), key=lambda item: -item[1])
    assert most["answer"].startswith(f"Done in the period, last 30 days, most first: {ranked[0][0]} {ranked[0][1]}")

    finished = post(client, {"question": "How many tickets did @Ada Brooks finish this month?", "level": "people",
                             "mentions": [ada["id"]]})
    assert finished["outcome"] == "answered" and finished["answer"].startswith(
        f"Ada Brooks has {done['Ada Brooks']} tickets done in the last 30 days")

    holder = post(client, {"question": "Who is assigned DEMO-12?", "level": "people"})
    (row,) = holder["facts"][0]["rows"]
    assert holder["outcome"] == "answered" and holder["answer"].startswith("DEMO-12 ")
    assert (row["assignee"] in holder["answer"]) if row["assignee_id"] else ("Nobody is assigned" in holder["answer"])

    let_go = post(client, {"question": "Should we let @Ada Brooks go?", "level": "people", "mentions": [ada["id"]]})
    assert let_go["plan"] == [{"tool": "oversight_person_scorecard", "params": {"person_ids": [ada["id"]], "days": 90}}]
    assert let_go["answer"].split("\n\n")[-2:] == [DECISION_YOURS, DEMO_CAVEAT]

    first = post(client, {"question": "Is Ada doing well?", "level": "people"})
    assert first["outcome"] == "answered" and first["people"] == [ada]


def test_a_first_name_two_people_share_asks_for_at_naming_them() -> None:
    def person(name: str, n: int) -> dict[str, Any]:
        return {"id": f"p{n:012x}", "name": name, "avatar": None}

    everyone = [person("Ada Brooks", 1), person("Ada Lane", 2), person("Ben Okafor", 3), person("Will", 4)]
    assert first_named_in("Is Ada doing well? And Ben?", everyone) == [("Ada", everyone[:2]), ("Ben", [everyone[2]])]
    assert first_named_in("Is Ada Brooks doing well?", everyone) == []  # the full name is named_in's
    assert first_named_in("Ada is she doing well? is ben?", everyone) == []  # a sentence's start, and no capital
    assert ambiguous_first_name("Ada", ["Ada Brooks", "Ada Lane"]) == (
        "Ada could be Ada Brooks or Ada Lane. Type @ and pick the one you mean.")


# --- the wording check holds everywhere -------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Ada Brooks's incompetence shows.", "Ada Brooks shows laziness in her time logging.",
    "Ada Brooks's carelessness with estimates is plain.", "The stupidity of how Ada Brooks estimates.",
    "Ada Brooks's dishonesty about time is clear.", "Ada Brooks works lazily and carelessly.",
    "Ada Brooks is a slacker and a liar.", "Ada Brooks is sloppy.", "Hopelessness, worthlessness, pathetically.",
    "Ada Brooks is useless.",
])
def test_the_wording_check_knows_every_form_of_each_label(text: str) -> None:
    assert character_labels(text), text


def test_the_wording_check_passes_plain_words() -> None:
    assert character_labels("Ada Brooks is careful, hopes to finish, and is sincere; reliable figures.") == []


@pytest.mark.usefixtures("audit_shim")
def test_a_message_a_model_writes_with_no_facts_is_held_to_the_wording(demo: Settings) -> None:
    scope = scope_of(demo)
    ada = next(who for who in scope.people if who["name"] == "Ada Brooks")
    reason = "Ada Brooks is lazy and careless, so there is nothing to show."
    planned = plan(understood=False, reason=reason)
    response = _ask(demo, FakeProvider(plans=[planned]), "Tell me a story", level="people", people=scope)
    assert (response.outcome, response.message) == ("not_understood", _NOT_UNDERSTOOD_DEFAULT)
    # About someone mentioned, a plan that is not understood is never the answer at people level (JO-12 G3): their
    # scorecard is, said so first.
    response = _ask(demo, FakeProvider(plans=[planned]), "Tell me about @Ada Brooks", level="people",
                    mentions=[ada["id"]], people=scope)
    assert response.outcome == "answered" and response.answer is not None
    assert response.answer.startswith(LAST_RESORT_SCORECARD + "\n\nAda Brooks, last 90 days, next to the board")
    assert not character_labels(response.answer)

    said = FakeAnalyst([turn(text="Ada Brooks is lazy and careless; there is no need to look anything up.")])
    response = _ask(demo, said, "What do you make of @Ada Brooks?", level="people", mentions=[ada["id"]],
                    people=scope)
    assert (response.outcome, response.message) == ("not_understood", ANALYST_DECLINED)


# --- comparing people off, per ask ---------------------------------------------------------------------


@pytest.mark.usefixtures("audit_shim")
def test_comparing_people_off_is_refused_per_ask_not_per_call(demo: Settings) -> None:
    settings = demo.model_copy(update={"people_comparison": "off"})
    scope = replace(scope_of(demo), comparison=False)
    ada, ben = (next(who for who in scope.people if who["name"] == name) for name in ("Ada Brooks", "Ben Okafor"))
    question, mentions = "Tell me about @Ada Brooks and @Ben Okafor", [ada["id"], ben["id"]]
    split = plan(("oversight_person_scorecard", {"person_ids": [ada["id"]]}),
                 ("oversight_person_scorecard", {"person_ids": [ben["id"]]}))
    response = _ask(settings, FakeProvider(plans=[split]), question, level="people", mentions=mentions, people=scope)
    assert (response.outcome, response.message, response.facts) == ("not_understood", COMPARISON_OFF, [])

    one_turn = FakeAnalyst([turn(uses=[use("oversight_person_scorecard", {"person_ids": [ada["id"]]}, "a"),
                                       use("oversight_person_scorecard", {"person_ids": [ben["id"]]}, "b")])])
    response = _ask(settings, one_turn, question, level="people", mentions=mentions, people=scope)
    assert (response.outcome, response.message, response.facts) == ("not_understood", COMPARISON_OFF, [])

    one_then_another = FakeAnalyst([turn(uses=[use("oversight_person_scorecard", {"person_ids": [ada["id"]]})]),
                                    turn(uses=[use("oversight_person_scorecard", {"person_ids": [ben["id"]]})])])
    response = _ask(settings, one_then_another, "How much of each person's work was estimated before it started?",
                    level="people", people=scope)
    assert (response.outcome, response.message) == ("not_understood", COMPARISON_OFF)
    assert len(one_then_another.calls) == 2  # ended at the second person, with no further turn

    alone = FakeAnalyst([turn(uses=[use("oversight_person_scorecard", {"person_ids": [ada["id"]]})]),
                         turn(text="Ada Brooks is closer to the process than the board on some measures.")])
    response = _ask(settings, alone, "Tell me about @Ada Brooks", level="people", mentions=[ada["id"]], people=scope)
    assert response.outcome == "answered"  # one person's scorecard is always there


# --- the overall order, and what its rows carry -----------------------------------------------------------


def _standing(name: str, closer: int, ratio: float, open_tickets: int) -> Standing:
    values: dict[str, Any] = {key: {"value": None, "numerator": None, "denominator": None, "enough": False,
                                    "vs_board": None} for key in COMPARABLE}
    for key in list(COMPARABLE)[:closer]:
        values[key] = {"value": 50, "numerator": 5, "denominator": 10, "enough": True, "vs_board": "closer"}
    values["open_findings_per_10"] = {"value": ratio, "numerator": round(ratio * open_tickets / 10),
                                      "denominator": open_tickets, "enough": open_tickets >= 5, "vs_board": None}
    return Standing(person={"id": f"p{len(name):012x}", "name": name, "avatar": None}, tickets=open_tickets,
                    values=values)


def test_the_overall_order_never_compares_a_figure_on_too_few_tickets() -> None:
    few_low, many, few_high, many_more = (_standing("Alpha Few", 1, 0.0, 2), _standing("Bravo Many", 1, 5.0, 6),
                                          _standing("Charlie Few", 1, 10.0, 1), _standing("Delta Many", 1, 6.0, 20))
    ordered = order([few_low, many, few_high, many_more], OVERALL)
    assert [one.person["name"] for one in ordered] == ["Bravo Many", "Delta Many", "Alpha Few", "Charlie Few"]


def test_the_overall_rows_carry_the_ratios_counts_and_whether_it_rests_on_enough(client: TestClient) -> None:
    body = post(client, {"question": "Who is best on the board?", "level": "people"})
    (facts,) = body["facts"]
    assert [column["key"] for column in facts["columns"]] == [column.key for column in OVERALL_COLUMNS]
    card = client.get("/api/v1/people/scorecard", params={"level": "people"}).json()
    ratios = {row["person"]["id"]: row["values"]["open_findings_per_10"] for row in card["people"]}
    for row in facts["rows"]:
        mine = ratios[row["person_id"]]
        assert (row["open_findings_per_10"], row["open_findings"], row["open_tickets"], row["enough"]) == (
            mine["value"], mine["numerator"], mine["denominator"], mine["enough"])
    assert "a figure on too few tickets is never compared" in facts["note"] and "vs_board" not in facts["note"]


# --- citations: each figure to its own row; a label is no figure ----------------------------------------


def test_each_figure_of_a_comparison_is_cited_to_its_own_persons_row(client: TestClient) -> None:
    body = post(client, {"question": "Who is best on the board? Rank the others.", "level": "people"})
    rows = body["facts"][0]["rows"]
    pairs = [cite for cite in body["citations"] if cite["column"] in ("closer", "compared")]
    assert pairs and [cite["row_index"] for cite in pairs] == [n // 2 for n in range(len(pairs))]
    for cite in pairs:
        assert rows[cite["row_index"]][cite["column"]] == int(cite["text"])
    # The answer names people in the rows' order, so the n-th pair of figures is the n-th row's own.
    named = [row["name"] for row in rows if row["compared"]]
    assert [body["answer"].index(name) for name in named] == sorted(body["answer"].index(name) for name in named)

    # The same for a model's answer: a figure written after a name is that person's (validate: its context).
    stuck = post(client, {"question": "Whose tickets get stuck the most?", "level": "people"})
    rows = stuck["facts"][0]["rows"]
    at = 0
    # Markers are in text order, so each figure is found after the one before it (a "3" may also stand inside an
    # earlier "33%": in the order asked for, highest first, it does).
    for cite in sorted(stuck["citations"], key=lambda cite: cite["marker"]):
        at = stuck["answer"].index(cite["text"], at)
        if cite["row_index"] >= 0 and rows[cite["row_index"]]["person_id"]:
            assert rows[cite["row_index"]]["name"] in stuck["answer"][:at][-40:]
        at += len(cite["text"])


def test_a_model_written_table_ties_each_figure_to_its_own_persons_row(demo: Settings) -> None:
    # Found by the product owner on 13 Nov 2026: asked "Who has the most reopened tickets?", the model's table tied a
    # figure in one person's row to the first row of the facts that holds the same value, someone else's. The number
    # check reads the answer with names blanked; the answer as written still says whose each figure is. The queries
    # select names alone, no person ids, as a model writes them (what the old name-then-figure hint could not follow).
    scope = scope_of(demo)
    queries = [
        use("oversight_query", {"sql": "SELECT name, tickets % 3 + 1 AS tickets_bucket FROM people "
                                       "WHERE person_id IS NOT NULL ORDER BY name"}, "a"),
        use("oversight_query", {"sql": "SELECT assignee, COUNT(*) % 2 + 1 AS reopened FROM issues "
                                       "WHERE assignee_id IS NOT NULL GROUP BY assignee_id ORDER BY assignee DESC"}, "b"),
    ]
    question = "Who has the most reopened tickets?"
    probe = _ask(demo, FakeAnalyst([turn(uses=queries), turn(text="-")]), question, level="people", people=scope)
    buckets, reopened = probe.facts
    # A name two people share is a row neither figure can be told apart by: only names one person goes by.
    names = [row["name"] for row in buckets.rows]
    by_person = {row["assignee"]: row["reopened"] for row in reopened.rows
                 if [other["assignee"] for other in reopened.rows].count(row["assignee"]) == 1}
    rows = [f"| {row['name']} | {row['tickets_bucket']} | {by_person[row['name']]} |"
            for row in buckets.rows if row["name"] in by_person and names.count(row["name"]) == 1]
    # No number in words above the table ("two figures" is a figure, checked and cited: 18 Nov 2026).
    answer = "Each person's figures:\n\n| Person | Tickets bucket | Reopened |\n|---|---:|---:|\n" + "\n".join(rows)
    response = _ask(demo, FakeAnalyst([turn(uses=queries), turn(text=answer)]), question, level="people", people=scope)
    assert response.outcome == "answered" and response.answer.startswith(answer)

    facts = {fact.ref: fact for fact in response.facts}
    cites = sorted(response.citations, key=lambda cite: cite.marker)
    assert len(rows) >= 3 and len(cites) == 2 * len(rows)
    first_places = 0
    for cite, line in zip(cites, [line for line in rows for _ in range(2)], strict=True):
        row = facts[cite.fact_ref].rows[cite.row_index]
        assert line.split("|")[1].strip() == (row.get("name") or row.get("assignee"))  # its own person's row
        assert cite.column == ("tickets_bucket" if cite.fact_ref == buckets.ref else "reopened")  # its own column
        assert str(row[cite.column]) == cite.text
        # The first place that holds the value, as every figure was bound before.
        first = next((fact.ref, index) for fact in response.facts for index, other in enumerate(fact.rows)
                     if any(str(value) == cite.text for key, value in other.items() if key in ("tickets_bucket", "reopened")))
        first_places += first == (cite.fact_ref, cite.row_index)
    assert first_places < len(cites)  # the data holds the trap: some figure's value stands first in another's row


def test_a_measures_label_is_never_cited_as_a_figure(client: TestClient) -> None:
    ada = board(client)["Ada Brooks"]
    body = post(client, {"question": "Is @Ada Brooks doing well?", "level": "people", "mentions": [ada["id"]]})
    assert "Open findings per 10 open tickets" in body["answer"]
    assert [cite["text"] for cite in body["citations"]] == ["90"]


# --- the caveat names every period; the analyst's view says its period ----------------------------------


def test_the_caveat_names_each_period_an_answer_mixes(demo: Settings) -> None:
    scope = scope_of(demo)
    ada = next(who for who in scope.people if who["name"] == "Ada Brooks")
    analyst = FakeAnalyst([
        turn(uses=[use("oversight_person_scorecard", {"person_ids": [ada["id"]], "days": 30}, "a"),
                   use("oversight_people_compare", {"measure": "overall", "days": 180}, "b")]),
        turn(text="Ada Brooks is closer to the process than the board on some measures.")])
    response = _ask(demo, analyst, "Is @Ada Brooks doing well?", level="people", mentions=[ada["id"]], people=scope)
    assert response.answer.endswith(caveat([30, 180], records_code_review=False))
    assert "over the last 30 and 180 days" in response.answer


def test_an_answer_may_say_the_period_the_scorecard_view_covers(demo: Settings) -> None:
    scope = scope_of(demo)
    ada = next(who for who in scope.people if who["name"] == "Ada Brooks")
    sql = (f"SELECT name, shown, board_shown, days, period_from, period_to FROM scorecard WHERE person_id = '{ada['id']}'"
           " AND measure = 'time_logged'")
    probe = FakeAnalyst([turn(uses=[use("oversight_query", {"sql": sql})]), turn(text="-")])
    first = _ask(demo, probe, "What share of @Ada Brooks's done work has time logged?", level="people",
                 mentions=[ada["id"]], people=scope)
    (row,) = first.facts[0].rows
    said = (f"Over the last {row['days']} days, Ada Brooks logged time on {row['shown'].split('%')[0]}% of her done "
            f"work; the whole board {row['board_shown'].split('%')[0]}%.")
    analyst = FakeAnalyst([turn(uses=[use("oversight_query", {"sql": sql})]), turn(text=said)])
    response = _ask(demo, analyst, "What share of @Ada Brooks's done work has time logged?", level="people",
                    mentions=[ada["id"]], people=scope)
    assert response.outcome == "answered" and len(analyst.calls) == 2  # no rewrite asked for "90"


# --- code review counts only work the rules have checked ------------------------------------------------


def test_code_review_counts_only_work_the_rules_checked(tmp_path: Path) -> None:
    rules = yaml.safe_load((CONFIG_FIXTURES / "rules.yaml").read_text(encoding="utf-8"))
    looked_back = dict(rules, defaults={**rules.get("defaults", {}), "lookback_days": 90})
    path = tmp_path / "oversight.db"
    build(path)
    with Store.open(path) as store, store.transaction():
        # Done well before the rules' first look-back, reached testing, no finding: the rules never checked it.
        store.upsert("issues", {"issue_key": "REV-50", "issue_id": "5050", "project_key": "REV", "issue_type": "Task",
                                "summary": "Sample old", "status": "Done", "status_category": "done",
                                "created_at": to_iso(S - 60 * DAY), "updated_at": to_iso(S - 10 * DAY)})
        store.upsert("issue_metrics", {"issue_key": "REV-50", "started_at": to_iso(S - 12 * DAY),
                                       "done_at": to_iso(S - 10 * DAY), "reopen_count": 0, "skipped_stages": [],
                                       "estimate_basis": "none", "computed_at": to_iso(T)})
        store.conn.execute("INSERT INTO status_intervals (issue_key, seq, status, stage, entered_at, left_at, hours)"
                           " VALUES ('REV-50', 1, 'QA', 'testing', ?, ?, 24.0)",
                           (to_iso(S - 11 * DAY), to_iso(S - 10 * DAY)))

    def code_review(config: Path) -> dict[str, Any]:
        with serve(tmp_path, config) as client:
            body = client.get("/api/v1/people/scorecard", params={"level": "people", "days": "180", "project": "REV"})
            assert body.status_code == 200, body.text
            return body.json()["board"]["values"]["code_review"]

    every = code_review(write_config(tmp_path / "plain"))
    before_any_run = code_review(write_config(tmp_path / "looked", rules=looked_back))
    with Store.open(path) as store, store.transaction():
        store.conn.execute("INSERT INTO runs (run_id, kind, status, started_at) VALUES ('rules-1', 'rules', 'succeeded',"
                           " ?)", (to_iso(T - timedelta(hours=2)),))
        store.conn.execute("INSERT INTO rule_status (run_id, project_key, rule_id, rule_version, measurable)"
                           " VALUES ('rules-1', 'REV', 'R-FLOW-01', '1', 1)")
    after_a_run = code_review(write_config(tmp_path / "looked", rules=looked_back))
    # With no look-back every done ticket that reached testing counts; with one, REV-50 (never checked) is left out,
    # and before any run only the tickets the rules found something on count.
    assert after_a_run["denominator"] == every["denominator"] - 1
    assert after_a_run["numerator"] == every["numerator"] - 1
    assert before_any_run["denominator"] < after_a_run["denominator"]
    assert {measure.key for measure in MEASURES} >= {"code_review"}
