"""Ask honours "most" and "fewest" in people comparisons (JO-14, the product owner's decision of 13 Nov 2026: "answer
exactly what was asked").

JO-11 put every rate in the comparison closer to the process first, whatever the question said, so "Who has the most
reopened tickets?" named the person with the fewest first, and its contract refused an explicit fewest on a rate. Now:
- most, highest, largest, biggest, top N (and close paraphrases): the figure highest first;
- fewest, least, lowest, smallest, bottom N: lowest first;
- no direction word: closer to the process first, as before (a count, most first);
for rates, the ratio and counts alike, with people on too few tickets last whatever the order, vs_board, the caveat
and every figure unchanged. The first sentence says the order in plain words ("Reopened after Done, last 90 days,
highest first: ..."), never calling the highest worst or the lowest best. oversight_people_compare takes one
parameter for it, order: closer (the default) | most | fewest, for every measure, and the facts record the order used.
A rate named the other way round turns the order ("who overruns the most?" is the lowest share within the limit
first), and words that only say how little ("the least work") are the figure named beside them ("who reopens the
least work?" is reopened, fewest first), where JO-12's workload words took them for open tickets.

QUESTIONS is the table, question for question, that the dashboard's sample is held to as well
(src/server/__tests__/ask-most-fewest.test.ts): the measure, the order and the first sentence, through the router and
through POST /ask at people level. Against the seeded DEMO store (fictional people); nothing calls a model or the
network.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from oversight.api.scorecard import BY_KEY, MEASURES, OVERALL, RANKED_COUNTS, Standing, effective_order, order
from oversight.ask.analyst import analyst_system
from oversight.ask.people import PEOPLE_CATALOG, CompareParams, people_catalog_schema, strip_names
from oversight.ask.prompts import COMPARISON_OFF, LAST_RESORT_OVERALL, PEOPLE_PLAN_SYSTEM
from oversight.ask.router import CATALOG_NAMES, PEOPLE_NAMES, last_resort, route
from oversight.ask.scorecards import CHARACTER_LABELS, order_words
from oversight.settings import Settings
from tests.test_analyst import FakeAnalyst, turn, use
from tests.test_ask_scorecard import _ask, board, client, demo, off, post, scope_of  # noqa: F401  (fixtures)

PEOPLE = [*CATALOG_NAMES, *PEOPLE_NAMES]
ADA, CARMEN = "Ada Brooks", "Carmen Ruiz"
ROUTER_IDS = {ADA: "pbc7fe62842ca", CARMEN: "pfedcba987654"}  # any person ids: the router reads only the words
WHOM = {"": [], "one": [ADA], "two": [ADA, CARMEN]}
COMPARE, SCORECARD, RANKING = "oversight_people_compare", "oversight_person_scorecard", "oversight_people"


def first(label: str, order_said: str, days: int | None = 90) -> str:
    """The first sentence's opening, as a pattern: "{Label}, last {days} days, {order}: " (no period for a figure of
    now)."""
    period = f", last {days} days" if days else ""
    return "^" + re.escape(f"{label}{period}, {order_said}: ")


OVERALL_FIRST = "^" + re.escape("Measures closer to the process than the board, last 90 days, ")

# (question, who is mentioned, tool, measure, order, days, n, the answer's opening). Direction words of both kinds and
# none, phrased several ways; rates, the ratio, counts and the overall order; no one, one and two people mentioned.
QUESTIONS: list[tuple[str, str, str, str | None, str | None, int | None, int | None, str]] = [
    # The JO-12 check's two cases.
    ("Who has the most reopened tickets?", "", COMPARE, "reopened", "most", 90, None,
     first("Reopened after Done", "highest first")),
    ("Who has the fewest tickets stuck in a status?", "", COMPARE, "stuck_now", "fewest", 90, None,
     first("Open tickets stuck in a status", "lowest first", None)),
    # Rates, most first.
    ("Whose tickets are reopened the most?", "", COMPARE, "reopened", "most", 90, None,
     first("Reopened after Done", "highest first")),
    ("Who has the highest reopen rate this month?", "", COMPARE, "reopened", "most", 30, None,
     first("Reopened after Done", "highest first", 30)),
    ("Whose tickets get stuck the most?", "", COMPARE, "stuck_now", "most", 90, None,
     first("Open tickets stuck in a status", "highest first", None)),
    ("Who skips stages the most?", "", COMPARE, "skipped_stage", "most", 90, None,
     first("Skipped a stage", "highest first")),
    ("Top 3 people by tickets that skipped a stage", "", COMPARE, "skipped_stage", "most", 90, 3,
     first("Skipped a stage", "highest first")),
    ("Who estimates before starting the most?", "", COMPARE, "estimated_before_start", "most", 90, None,
     first("Estimated before work started", "highest first")),
    ("Who logged the most hours in the last 6 months?", "", COMPARE, "time_logged", "most", 180, None,
     first("Time logged on done work", "highest first", 180)),
    ("Who stays within the effort limit the most?", "", COMPARE, "within_estimate", "most", 90, None,
     first("Done within the effort limit", "highest first")),
    ("Who has the most code review recorded?", "", COMPARE, "code_review", "most", 90, None,
     "^" + re.escape("Not measurable on this board: Code review recorded.")),
    # Rates, fewest first.
    ("Who has the lowest share of reopened work?", "", COMPARE, "reopened", "fewest", 90, None,
     first("Reopened after Done", "lowest first")),
    ("Who skips the fewest stages?", "", COMPARE, "skipped_stage", "fewest", 90, None,
     first("Skipped a stage", "lowest first")),
    ("Who has the lowest share of work estimated before it started?", "", COMPARE, "estimated_before_start", "fewest",
     90, None, first("Estimated before work started", "lowest first")),
    ("Who logs the least time?", "", COMPARE, "time_logged", "fewest", 90, None,
     first("Time logged on done work", "lowest first")),
    ("Bottom 3 people by time logged", "", COMPARE, "time_logged", "fewest", 90, 3,
     first("Time logged on done work", "lowest first")),
    ("From lowest to highest, rank everyone by time logged", "", COMPARE, "time_logged", "fewest", 90, None,
     first("Time logged on done work", "lowest first")),
    # A rate named the other way round: the most overruns are the lowest share within the limit, and so on.
    ("Who overruns the most?", "", COMPARE, "within_estimate", "fewest", 90, None,
     first("Done within the effort limit", "lowest first")),
    ("Who goes over the effort limit the least?", "", COMPARE, "within_estimate", "most", 90, None,
     first("Done within the effort limit", "highest first")),
    ("Who has the most work without an estimate?", "", COMPARE, "estimated_before_start", "fewest", 90, None,
     first("Estimated before work started", "lowest first")),
    ("Who has the most unestimated work?", "", COMPARE, "estimated_before_start", "fewest", 90, None,
     first("Estimated before work started", "lowest first")),
    ("Who has the highest effort ratio?", "", COMPARE, "within_estimate", "fewest", 90, None,
     first("Done within the effort limit", "lowest first")),
    ("Who has the lowest effort ratio?", "", COMPARE, "within_estimate", "most", 90, None,
     first("Done within the effort limit", "highest first")),
    # Done, closed or in progress beside a rate name the tickets the rate is of: the rate is the figure (the review).
    ("Who has the most tickets reopened after Done?", "", COMPARE, "reopened", "most", 90, None,
     first("Reopened after Done", "highest first")),
    ("Who has the most time logged on done work?", "", COMPARE, "time_logged", "most", 90, None,
     first("Time logged on done work", "highest first")),
    ("Who completed the most tickets without logging time?", "", COMPARE, "time_logged", "fewest", 90, None,
     first("Time logged on done work", "lowest first")),
    ("Who has the most tickets closed without code review?", "", COMPARE, "code_review", "fewest", 90, None,
     "^" + re.escape("Not measurable on this board: Code review recorded.")),
    ("Who has the most done tickets that skipped a stage?", "", COMPARE, "skipped_stage", "most", 90, None,
     first("Skipped a stage", "highest first")),
    ("Who has the most stuck tickets in progress?", "", COMPARE, "stuck_now", "most", 90, None,
     first("Open tickets stuck in a status", "highest first", None)),
    # Top N and bottom N give the order only where no other direction word does; the word nearest the measure decides
    # a question that asks both ways; how many and which end, in other words.
    ("Top 3 people with the fewest reopened tickets", "", COMPARE, "reopened", "fewest", 90, 3,
     first("Reopened after Done", "lowest first")),
    ("Top 5 people who log the least time", "", COMPARE, "time_logged", "fewest", 90, 5,
     first("Time logged on done work", "lowest first")),
    ("Who has the most reopened tickets and the fewest stuck?", "", COMPARE, "stuck_now", "fewest", 90, None,
     first("Open tickets stuck in a status", "lowest first", None)),
    ("Who has the least reopened tickets but the most stuck?", "", COMPARE, "stuck_now", "most", 90, None,
     first("Open tickets stuck in a status", "highest first", None)),
    ("Which 3 people have the most stuck tickets?", "", COMPARE, "stuck_now", "most", 90, 3,
     first("Open tickets stuck in a status", "highest first", None)),
    ("Who is at the bottom for estimates?", "", COMPARE, "estimated_before_start", "fewest", 90, None,
     first("Estimated before work started", "lowest first")),
    ("Who is at the top for reopened tickets?", "", COMPARE, "reopened", "most", 90, None,
     first("Reopened after Done", "highest first")),
    # Words that only say how little ("the least work") are the figure named beside them; a word of room itself
    # ("capacity") stays the open tickets.
    ("Who reopens the least work?", "", COMPARE, "reopened", "fewest", 90, None,
     first("Reopened after Done", "lowest first")),
    ("Who has the least work in progress?", "", COMPARE, "in_progress", "fewest", None, None,
     first("In progress now", "fewest first", None)),
    ("Who finished the least work this month?", "", COMPARE, "done_in_period", "fewest", 30, None,
     first("Done in the period", "fewest first", 30)),
    ("Who works the least hours?", "", COMPARE, "time_logged", "fewest", 90, None,
     first("Time logged on done work", "lowest first")),
    ("Who has capacity to review code?", "", COMPARE, "open_tickets", "fewest", None, None,
     first("Open tickets", "fewest first", None)),
    # Rates, no direction word: closer to the process first, as before.
    ("Rank everyone by reopened tickets", "", COMPARE, "reopened", "closer", 90, None,
     first("Reopened after Done", "closest to the process first")),
    ("Who has tickets stuck?", "", COMPARE, "stuck_now", "closer", 90, None,
     first("Open tickets stuck in a status", "closest to the process first", None)),
    ("Who is best at estimating before they start?", "", COMPARE, "estimated_before_start", "closer", 90, None,
     first("Estimated before work started", "closest to the process first")),
    # The other side of the process (the JO-14 review): further from it first, and the least of that closer first.
    ("Who is worst for reopened work?", "", COMPARE, "reopened", "most", 90, None,
     first("Reopened after Done", "highest first")),
    ("Who has the worst reopen rate?", "", COMPARE, "reopened", "most", 90, None,
     first("Reopened after Done", "highest first")),
    ("Who is worst at estimating?", "", COMPARE, "estimated_before_start", "fewest", 90, None,
     first("Estimated before work started", "lowest first")),
    ("Whose estimates are weakest?", "", COMPARE, "estimated_before_start", "fewest", 90, None,
     first("Estimated before work started", "lowest first")),
    ("Who has the least trouble with estimates?", "", COMPARE, "estimated_before_start", "closer", 90, None,
     first("Estimated before work started", "closest to the process first")),
    ("Who has the most trouble with estimates?", "", COMPARE, "estimated_before_start", "fewest", 90, None,
     first("Estimated before work started", "lowest first")),
    ("Who has the fewest problems logging time?", "", COMPARE, "time_logged", "closer", 90, None,
     first("Time logged on done work", "closest to the process first")),
    ("Who has the most problems logging time?", "", COMPARE, "time_logged", "fewest", 90, None,
     first("Time logged on done work", "lowest first")),
    ("Who has the worst effort ratio?", "", COMPARE, "within_estimate", "fewest", 90, None,
     first("Done within the effort limit", "lowest first")),
    # The ratio.
    ("Who has the lowest open findings per 10 open tickets?", "", COMPARE, "open_findings_per_10", "fewest", 90, None,
     first("Open findings per 10 open tickets", "lowest first", None)),
    ("Who has the most findings per 10 tickets?", "", COMPARE, "open_findings_per_10", "most", 90, None,
     first("Open findings per 10 open tickets", "highest first", None)),
    ("Top 3 people by findings per 10 tickets", "", COMPARE, "open_findings_per_10", "most", 90, 3,
     first("Open findings per 10 open tickets", "highest first", None)),
    ("Rank everyone by open findings per 10 open tickets", "", COMPARE, "open_findings_per_10", "closer", 90, None,
     first("Open findings per 10 open tickets", "closest to the process first", None)),
    ("Who has the worst open findings?", "", COMPARE, "open_findings_per_10", "most", 90, None,
     first("Open findings per 10 open tickets", "highest first", None)),
    # Counts: most first, or fewest first (a count has no closer side).
    ("Who has the biggest backlog?", "", COMPARE, "open_tickets", "most", None, None,
     first("Open tickets", "most first", None)),
    ("Who has the smallest backlog?", "", COMPARE, "open_tickets", "fewest", None, None,
     first("Open tickets", "fewest first", None)),
    ("Who is the busiest?", "", COMPARE, "open_tickets", "most", None, None, first("Open tickets", "most first", None)),
    ("Who could take more work?", "", COMPARE, "open_tickets", "fewest", None, None,
     first("Open tickets", "fewest first", None)),
    ("Who has the most tickets in progress?", "", COMPARE, "in_progress", "most", None, None,
     first("In progress now", "most first", None)),
    ("Who has the fewest tickets in progress?", "", COMPARE, "in_progress", "fewest", None, None,
     first("In progress now", "fewest first", None)),
    ("Who finished the most work this month?", "", COMPARE, "done_in_period", "most", 30, None,
     first("Done in the period", "most first", 30)),
    ("Who closed the fewest tickets this quarter?", "", COMPARE, "done_in_period", "fewest", 90, None,
     first("Done in the period", "fewest first")),
    ("Bottom 3 people by open tickets", "", RANKING, None, "fewest", None, 3, r"^.+ (?:has|have) the fewest open tickets"),
    ("Top 3 people with the fewest open breaches", "", RANKING, None, "fewest", None, 3,
     r"^.+ (?:has|have) the fewest open breaches"),
    ("Top 3 busiest people", "", COMPARE, "open_tickets", "most", None, 3, first("Open tickets", "most first", None)),
    # The overall order: the most measures closer is closer first; the fewest, the exact reverse.
    ("Who follows the process most closely?", "", COMPARE, OVERALL, "closer", 90, None, OVERALL_FIRST + "most first: "),
    ("Who is best on the board?", "", COMPARE, OVERALL, "closer", 90, None, OVERALL_FIRST + "most first: "),
    ("Rank everyone", "", COMPARE, OVERALL, "closer", 90, None, OVERALL_FIRST + "most first: "),
    ("Who follows the process least?", "", COMPARE, OVERALL, "fewest", 90, None, OVERALL_FIRST + "fewest first: "),
    ("Which developer is least compliant?", "", COMPARE, OVERALL, "fewest", 90, None, OVERALL_FIRST + "fewest first: "),
    ("Who is the most compliant?", "", COMPARE, OVERALL, "closer", 90, None, OVERALL_FIRST + "most first: "),
    # The other side of the process, overall: further from it first (the fewest measures closer), and the least of
    # that closer first, as JO-12 answered "who needs the least help?" before JO-14 turned it round.
    ("Who is struggling the least?", "", COMPARE, OVERALL, "closer", 90, None, OVERALL_FIRST + "most first: "),
    ("Who needs the least help?", "", COMPARE, OVERALL, "closer", 90, None, OVERALL_FIRST + "most first: "),
    ("Who needs less help?", "", COMPARE, OVERALL, "closer", 90, None, OVERALL_FIRST + "most first: "),
    ("Who is falling behind the least?", "", COMPARE, OVERALL, "closer", 90, None, OVERALL_FIRST + "most first: "),
    ("Who doesn't struggle?", "", COMPARE, OVERALL, "closer", 90, None, OVERALL_FIRST + "most first: "),
    ("Who is struggling the most?", "", COMPARE, OVERALL, "fewest", 90, None, OVERALL_FIRST + "fewest first: "),
    ("Who needs the most help?", "", COMPARE, OVERALL, "fewest", 90, None, OVERALL_FIRST + "fewest first: "),
    ("Who is falling behind the most?", "", COMPARE, OVERALL, "fewest", 90, None, OVERALL_FIRST + "fewest first: "),
    ("Who is the worst performer?", "", COMPARE, OVERALL, "fewest", 90, None, OVERALL_FIRST + "fewest first: "),
    ("Who is the slowest?", "", COMPARE, OVERALL, "fewest", 90, None, OVERALL_FIRST + "fewest first: "),
    ("Who are the top 3 slowest?", "", COMPARE, OVERALL, "fewest", 90, 3, OVERALL_FIRST + "fewest first: "),
    ("Who is furthest from the process?", "", COMPARE, OVERALL, "fewest", 90, None, OVERALL_FIRST + "fewest first: "),
    ("Who deviates most from the process?", "", COMPARE, OVERALL, "fewest", 90, None,
     OVERALL_FIRST + "fewest first: "),
    ("Who is not pulling their weight?", "", COMPARE, OVERALL, "fewest", 90, None, OVERALL_FIRST + "fewest first: "),
    ("Who is careless with estimates?", "", COMPARE, "estimated_before_start", "fewest", 90, None,
     first("Estimated before work started", "lowest first")),
    # Two people side by side.
    ("Who has more reopened tickets, @Ada Brooks or @Carmen Ruiz?", "two", COMPARE, "reopened", "most", 90, None,
     first("Reopened after Done", "highest first")),
    ("Which of @Ada Brooks and @Carmen Ruiz has fewer stuck tickets?", "two", COMPARE, "stuck_now", "fewest", 90,
     None, first("Open tickets stuck in a status", "lowest first", None)),
    ("Who logs less time, @Ada Brooks or @Carmen Ruiz?", "two", COMPARE, "time_logged", "fewest", 90, None,
     first("Time logged on done work", "lowest first")),
    ("Time logged by @Ada Brooks and @Carmen Ruiz", "two", COMPARE, "time_logged", "closer", 90, None,
     first("Time logged on done work", "closest to the process first")),
    ("Who closed more tickets this month, @Ada Brooks or @Carmen Ruiz?", "two", COMPARE, "done_in_period", "most",
     30, None, first("Done in the period", "most first", 30)),
    # A measure named with a word that ranks, compares or evaluates: that measure, in the order asked (the review).
    ("Rank @Ada Brooks and @Carmen Ruiz by reopened tickets, highest first", "two", COMPARE, "reopened", "most", 90,
     None, first("Reopened after Done", "highest first")),
    ("Who is worse on reopened tickets, @Ada Brooks or @Carmen Ruiz?", "two", COMPARE, "reopened", "most", 90, None,
     first("Reopened after Done", "highest first")),
    # One person: their scorecard, each measure next to the board (one person is in no order).
    ("Does @Ada Brooks have the most reopened tickets?", "one", SCORECARD, None, None, 90, None,
     "^" + re.escape("Ada Brooks, last 90 days, next to the board")),
    ("Is @Ada Brooks the lowest on time logged?", "one", SCORECARD, None, None, 90, None,
     "^" + re.escape("Ada Brooks, last 90 days, next to the board")),
]


def test_the_table_has_at_least_forty_questions_each_once() -> None:
    assert len(QUESTIONS) >= 90  # 54 at JO-14, and the review's questions since
    assert len(QUESTIONS) >= 40 and len({row[0] for row in QUESTIONS}) == len(QUESTIONS)
    orders = {row[4] for row in QUESTIONS}
    assert {"most", "fewest", "closer"} <= orders
    kinds = {BY_KEY[row[3]].kind for row in QUESTIONS if row[3] in BY_KEY}
    assert kinds == {"rate", "ratio", "count"}
    assert {row[1] for row in QUESTIONS} == {"", "one", "two"}


def _planned(question: str, who: str) -> list[tuple[str, dict[str, Any]]]:
    names = WHOM[who]
    # As the service routes it: the mentioned names taken out of the words, the people by id.
    planned = route(strip_names(question, names), PEOPLE, project="DEMO", mentions=[ROUTER_IDS[name] for name in names])
    assert planned.understood, question
    return [(call.name, {key: value for key, value in call.params.items() if key != "project"})
            for call in planned.calls]


@pytest.mark.parametrize(("question", "who", "tool", "measure", "asked", "days", "n", "opening"), QUESTIONS)
def test_the_router_plans_the_measure_and_the_order_asked_for(question: str, who: str, tool: str,
                                                              measure: str | None, asked: str | None,
                                                              days: int | None, n: int | None, opening: str) -> None:
    (name, params), *_rest = _planned(question, who)
    assert name == tool, question
    if tool == COMPARE:
        assert (params["measure"], params["order"], params.get("days"), params.get("n")) == (measure, asked, days, n)
        if who == "two":
            assert params["person_ids"] == [ROUTER_IDS[ADA], ROUTER_IDS[CARMEN]]
    elif tool == RANKING:
        assert (params.get("order"), params.get("n")) == (asked, n)
    else:
        assert "order" not in params and params["person_ids"] == [ROUTER_IDS[ADA]]


def _ordered_as_asked(fact: dict[str, Any], asked: str) -> None:
    """The people in the facts in the order asked: those compared first, by their figure, those on too few tickets
    after them whatever the order."""
    people = [row for row in fact["rows"] if row["person_id"]]
    measure = fact["params"]["measure"]
    if measure == OVERALL:
        compared = [row["closer"] for row in people if row["compared"]]
        assert compared == sorted(compared, reverse=asked != "fewest")
        assert all(row["compared"] for row in people[:len(compared)])
        return
    counted = measure in RANKED_COUNTS
    enough = [row for row in people if counted or (row["enough"] and row["value"] is not None)]
    assert people[:len(enough)] == enough  # too few tickets: last, whatever the order
    values = [row["value"] for row in enough]
    if asked == "closer":
        highest = BY_KEY[measure].direction == "higher_is_closer"
    else:
        highest = asked == "most"
    assert values == sorted(values, reverse=highest), (measure, asked, values)


@pytest.mark.parametrize(("question", "who", "tool", "measure", "asked", "days", "n", "opening"), QUESTIONS)
def test_the_answer_is_in_the_order_asked_and_says_so_first(client: TestClient, question: str, who: str, tool: str,
                                                           measure: str | None, asked: str | None, days: int | None,
                                                           n: int | None, opening: str) -> None:
    people = board(client)
    body = post(client, {"question": question, "level": "people", "mentions": [people[name]["id"] for name in
                                                                                WHOM[who]]})
    assert body["outcome"] == "answered", (question, body["message"])
    step = body["plan"][0]
    assert step["tool"] == tool, question
    fact = body["facts"][0]
    assert re.match(opening, body["answer"]), (question, body["answer"][:160])
    # It reports the order; it never calls anyone the best or the worst, nor labels anyone.
    assert not re.search(r"\b(?:best|worst)\b", body["answer"], re.IGNORECASE), body["answer"]
    assert not CHARACTER_LABELS.search(body["answer"])
    if tool != COMPARE:
        return
    assert (fact["params"]["measure"], fact["params"]["order"]) == (measure, asked)  # the facts record the order
    assert step["params"]["order"] == asked
    if not any(row["person_id"] for row in fact["rows"]):
        return  # not measurable on this board: no one's figures to order
    _ordered_as_asked(fact, asked or "closer")
    # The answer names the people compared in the facts' order (those on too few tickets after them).
    sentence = body["answer"].split("\n\n")[0]
    if measure == OVERALL:
        named = [row["name"] for row in fact["rows"] if row["compared"]]
    else:
        named = [row["name"] for row in fact["rows"] if row["person_id"]
                 and (measure in RANKED_COUNTS or (row["enough"] and row["value"] is not None))]
    places = [sentence.index(name) for name in named]
    assert named and places == sorted(places), (question, sentence)


def test_the_two_named_cases(client: TestClient) -> None:
    """The JO-12 check's two cases: the most reopened lists the highest reopened rate first; the fewest stuck, the
    lowest first."""
    most = post(client, {"question": "Who has the most reopened tickets?", "level": "people"})
    rows = [row for row in most["facts"][0]["rows"] if row["person_id"] and row["enough"]]
    top = max(row["value"] for row in rows)
    assert rows[0]["value"] == top and most["answer"].startswith(
        f"Reopened after Done, last 90 days, highest first: {rows[0]['name']} {rows[0]['value']}%")
    fewest = post(client, {"question": "Who has the fewest tickets stuck in a status?", "level": "people"})
    rows = [row for row in fewest["facts"][0]["rows"] if row["person_id"] and row["enough"]]
    low = min(row["value"] for row in rows)
    assert rows[0]["value"] == low and fewest["answer"].startswith(
        f"Open tickets stuck in a status, lowest first: {rows[0]['name']} {rows[0]['value']}%")


@pytest.mark.parametrize(("asked", "closer"), [("Who has the most reopened tickets?", "Rank everyone by reopened tickets"),
                                               ("Who has the lowest open findings per 10 open tickets?",
                                                "Rank everyone by open findings per 10 open tickets"),
                                               ("Who follows the process least?", "Who follows the process most closely?")])
def test_only_the_order_changes(client: TestClient, asked: str, closer: str) -> None:
    """vs_board, the standing, the caveat and every figure are the same whatever the order: only the rows' order and
    the first sentence's order words differ."""
    one, other = (post(client, {"question": question, "level": "people"}) for question in (asked, closer))
    rows, others = one["facts"][0]["rows"], other["facts"][0]["rows"]

    def whose(row: dict[str, Any]) -> str:
        return str(row["person_id"] or row["name"])

    assert sorted(rows, key=whose) == sorted(others, key=whose)
    assert [row for row in rows if not row["person_id"]] == [row for row in others if not row["person_id"]]
    assert one["answer"].split("\n\n")[-1] == other["answer"].split("\n\n")[-1]  # the caveat
    assert one["answer"].split("\n\n")[-1].startswith("Figures from Jira over the last 90 days")
    too_few = [row["name"] for row in rows if row["person_id"] and row.get("standing") == "Too few tickets"]
    if too_few:  # labelled so and last, whatever the order
        assert [row["name"] for row in rows if row["person_id"]][-len(too_few):] == too_few
        assert f"Too few tickets to compare: {too_few[0]}" in one["answer"]


# --- the tool's parameter ---------------------------------------------------------------------------------


@pytest.mark.parametrize("measure", ["overall", *(measure.key for measure in MEASURES)])
@pytest.mark.parametrize("asked", ["closer", "most", "fewest"])
def test_every_measure_takes_every_order(measure: str, asked: str) -> None:
    # JO-11 refused fewest on a rate: "order fewest is for a count only".
    assert CompareParams(measure=measure, order=asked).order == asked


def test_closer_is_the_default_and_nothing_else_is_an_order() -> None:
    assert CompareParams(measure="reopened").order == "closer"
    assert CompareParams.model_validate({"measure": "reopened", "order": None}).order == "closer"  # a model's null
    for wrong in ("worst", "best", "desc", "asc", ""):
        with pytest.raises(ValidationError):
            CompareParams(measure="reopened", order=wrong)


def test_the_facts_record_the_order_used() -> None:
    # A count has no closer side (most first); overall, the most measures closer is the closer order itself.
    assert effective_order("open_tickets", "closer") == "most"
    assert effective_order("done_in_period", "fewest") == "fewest"
    assert effective_order(OVERALL, "most") == "closer"
    assert effective_order(OVERALL, "fewest") == "fewest"
    assert effective_order("reopened", "most") == "most"
    assert effective_order("estimated_before_start", "closer") == "closer"
    with pytest.raises(ValueError, match="not an order"):
        effective_order("reopened", "worst")


def test_the_order_words_of_the_first_sentence() -> None:
    assert [order_words("reopened", asked) for asked in ("closer", "most", "fewest", None)] == [
        "closest to the process first", "highest first", "lowest first", "closest to the process first"]
    assert [order_words("open_findings_per_10", asked) for asked in ("most", "fewest")] == ["highest first",
                                                                                            "lowest first"]
    assert [order_words("open_tickets", asked) for asked in ("closer", "most", "fewest")] == [
        "most first", "most first", "fewest first"]
    assert [order_words(OVERALL, asked) for asked in ("closer", "most", "fewest")] == [
        "most first", "most first", "fewest first"]


def test_the_tool_and_the_analyst_are_told_the_order() -> None:
    schema = next(tool for tool in people_catalog_schema() if tool["name"] == COMPARE)
    prop = schema["input_schema"]["properties"]["order"]
    assert prop["enum"] == ["closer", "most", "fewest"] and prop["default"] == "closer"
    assert "highest first" in prop["description"] and "lowest first" in prop["description"]
    assert "for every measure" in prop["description"]
    assert "order most: the figure highest first" in PEOPLE_CATALOG[COMPARE].description
    # A measure named the other way round, and the other side of the process, which only the code-planned path read
    # until the JO-14 review: the tool, the analyst and the live planner are told too.
    for told in (prop["description"], PEOPLE_CATALOG[COMPARE].description,
                 analyst_system("DEMO", "2026-11-04", 4, "people"), PEOPLE_PLAN_SYSTEM):
        assert "within_estimate fewest" in told and "further from it first" in told, told[:80]
    for told in (prop["description"], analyst_system("DEMO", "2026-11-04", 4, "people"), PEOPLE_PLAN_SYSTEM):
        assert "overall fewest" in told and "the least of that" in told
    system = analyst_system("DEMO", "2026-11-04", 4, "people")
    assert "order most, the figure highest first" in system and "never call the highest worst" in system


@pytest.mark.parametrize(("asked", "opening"), [("most", "Reopened after Done, last 90 days, highest first: "),
                                               ("fewest", "Reopened after Done, last 90 days, lowest first: "),
                                               (None, "Reopened after Done, last 90 days, closest to the process "
                                                      "first: ")])
def test_in_analyst_mode_the_order_the_model_asked_for_is_kept(demo: Settings, asked: str | None,
                                                                opening: str) -> None:
    """Analyst mode: the model calls the comparison with the order (or none: closer); the facts record it, and when its
    own answer cannot be used (a label, twice), the answer built in code says that order first."""
    params: dict[str, Any] = {"measure": "reopened", **({"order": asked} if asked else {})}
    stubborn = FakeAnalyst([turn(uses=[use(COMPARE, params)]), turn(text="Ada Brooks is lazy."),
                            turn(text="Ada Brooks is hopeless.")])
    response = _ask(demo, stubborn, "Who has the most reopened tickets?", level="people", people=scope_of(demo))
    assert response.outcome == "answered" and response.extra["answer"] == "code"
    (fact,) = response.facts
    assert fact.params["order"] == (asked or "closer") and response.plan[0].params["order"] == (asked or "closer")
    assert response.answer.startswith(opening), response.answer[:120]
    values = [row["value"] for row in fact.rows if row["person_id"] and row["enough"]]
    assert values == sorted(values, reverse=asked == "most")


# --- the order itself, on hand-made standings ---------------------------------------------------------------


def _standing(name: str, figures: dict[str, tuple[float, bool]], closer: int = 0) -> Standing:
    """Someone with these figures (value, enough) and `closer` measures closer to the process than the board."""
    values: dict[str, Any] = {measure.key: {"value": None, "numerator": None, "denominator": None, "enough": False,
                                            "vs_board": None} for measure in MEASURES}
    for key, (value, enough) in figures.items():
        values[key] = {"value": value, "numerator": None, "denominator": None, "enough": enough, "vs_board": None}
    for key in [key for key in BY_KEY if BY_KEY[key].direction != "neutral"][:closer]:
        values[key] = {**values[key], "vs_board": "closer"}
    person = {"id": "p" + hashlib.sha1(name.encode()).hexdigest()[:12], "name": name, "avatar": None}
    return Standing(person=person, tickets=10, values=values)  # type: ignore[arg-type]


def _names(ordered: list[Standing]) -> list[str]:
    return [one.person["name"] for one in ordered]


@pytest.mark.parametrize(("measure", "figures", "expected"), [
    # Lower is closer (reopened): closer is lowest first; most, highest first; fewest, lowest first.
    ("reopened", {"A": (10, True), "B": (40, True), "C": (25, True), "D": (90, False)},
     {"closer": "ACBD", "most": "BCAD", "fewest": "ACBD"}),
    # Higher is closer (estimated before start): closer is highest first.
    ("estimated_before_start", {"A": (80, True), "B": (20, True), "C": (50, True), "D": (100, False)},
     {"closer": "ACBD", "most": "ACBD", "fewest": "BCAD"}),
    # The ratio, lower is closer; a figure on too few tickets is last whichever way.
    ("open_findings_per_10", {"A": (1.5, True), "B": (4.0, True), "C": (0.5, True), "D": (0.0, False)},
     {"closer": "CABD", "most": "BACD", "fewest": "CABD"}),
    # A count: most first, closer too; fewest first; ties in the directory's order either way.
    ("open_tickets", {"A": (3, True), "B": (9, True), "C": (5, True), "D": (5, True)},
     {"closer": "BCDA", "most": "BCDA", "fewest": "ACDB"}),
])
def test_the_order_by_one_measure(measure: str, figures: dict[str, tuple[float, bool]],
                                  expected: dict[str, str]) -> None:
    everyone = [_standing(name, {measure: figure}) for name, figure in figures.items()]
    for asked, names in expected.items():
        assert "".join(_names(order(everyone, measure, first=asked))) == names, (measure, asked)


def test_the_overall_order_and_its_exact_reverse() -> None:
    p = _standing("P", {"open_findings_per_10": (1.0, True)}, closer=3)
    q = _standing("Q", {"open_findings_per_10": (2.0, True)}, closer=1)
    r = _standing("R", {"open_findings_per_10": (2.0, True)}, closer=3)
    s = _standing("S", {"open_findings_per_10": (0.0, False)}, closer=0)  # compared on none: last, either way
    t = _standing("T", {"open_findings_per_10": (0.0, False)}, closer=1)  # its ratio on too few tickets
    everyone = [p, q, r, s, t]
    assert "".join(_names(order(everyone, OVERALL))) == "PRQTS"
    assert "".join(_names(order(everyone, OVERALL, first="most"))) == "PRQTS"
    # Fewest measures closer first; ties by the ratio highest first, a figure on too few tickets counting as the most.
    assert "".join(_names(order(everyone, OVERALL, first="fewest"))) == "TQRPS"


# --- the JO-14 review's further cases ------------------------------------------------------------------------------


@pytest.mark.parametrize(("question", "order"), [
    # A question about the other side of the process that no figure places: the overall comparison, further from the
    # process first, and the least of that closer first; a direction word alone stays closer first.
    ("Who makes the most mistakes?", "fewest"),
    ("Who has the most trouble?", "fewest"),
    ("Who makes the fewest mistakes?", "closer"),
    ("Who writes the most comments?", "closer"),
])
def test_the_last_resort_keeps_the_side_of_the_process_asked_about(client: TestClient, question: str,
                                                                  order: str) -> None:
    planned = last_resort(question, PEOPLE, project="DEMO")
    assert planned is not None and planned.calls[0].params["order"] == order
    body = post(client, {"question": question, "level": "people"})
    assert body["outcome"] == "answered" and body["answer"].startswith(LAST_RESORT_OVERALL), body["answer"][:120]
    fact = body["facts"][0]
    assert fact["params"]["order"] == order
    _ordered_as_asked(fact, order)
    assert not re.search(r"\b(?:best|worst)\b", body["answer"], re.IGNORECASE)


@pytest.mark.parametrize("question", ["Who has lower findings per 10?", "Who has higher findings per 10?",
                                      "Who has lower findings rate?", "Who has the highest findings per 10?",
                                      "Who has fewer findings per 10 tickets?"])
def test_with_comparison_off_a_direction_word_is_never_taken_for_a_name(off: TestClient, question: str) -> None:
    """Regression (the JO-14 review): with comparison off, "Who has lower findings per 10?" got "No one called Lower
    is on this board.", an untrue turn-away: JO-14's direction words were not among the words that are never names."""
    body = post(off, {"question": question, "level": "people"})
    assert (body["outcome"], body["message"]) == ("not_understood", COMPARISON_OFF), body["message"]


def test_a_cut_comparison_says_nothing_of_rows_and_a_ranking_counts_people_only(client: TestClient) -> None:
    """Regression (the JO-14 review): "The table shows the first 3." for two people and the board, and "the first 7"
    for five people: the comparison names everyone it shows, as the comparison answered in code and the dashboard's
    sample do; the people ranking counts its people, never the row of tickets nobody holds."""
    for question in ("Top 2 people by findings rate", "Who are the top 5 people with the most work?",
                     "Show me the top 3 people for reopened work"):
        body = post(client, {"question": question, "level": "people"})
        assert body["plan"][0]["tool"] == COMPARE and "The table shows" not in body["answer"], (question, body["answer"])
    for question in ("Top 3 people by open breaches", "Bottom 3 people by workload"):
        ranked = post(client, {"question": question, "level": "people"})
        assert ranked["plan"][0]["tool"] == RANKING and ranked["facts"][0]["truncated"]
        people = [row for row in ranked["facts"][0]["rows"] if row["person_id"]]
        assert len(people) == 3 and ranked["answer"].endswith("The table shows the first 3."), ranked["answer"]
