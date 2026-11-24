"""Ask routing, ordering and figures that must agree across the board."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from oversight.api.scorecard import MEASURES, NO_CODE_REVIEW, NO_STAGES_TO_SKIP, NO_TIME_LOGGED, RULE_OFF
from oversight.ask.people import unknown_name
from oversight.ask.prompts import COMPARISON_OFF, LAST_RESORT_OVERALL, PEOPLE_HELP
from oversight.ask.router import CATALOG_NAMES, PEOPLE_NAMES, asks_of_someone, route
from oversight.ask.service import _NOT_UNDERSTOOD_DEFAULT
from oversight.store.db import Store
from oversight.timeutil import to_iso
from tests.conftest import CONFIG_FIXTURES
from tests.test_ask_scorecard import board, client, demo, off, post  # noqa: F401
from tests.test_scorecard import DAY, T, build, pid, scorecard, serve, write_config

PEOPLE = [*CATALOG_NAMES, *PEOPLE_NAMES]
_LABELS = ("Closer to the process", "Further from the process", "Level with the board", "Measures compared",
           "Too few tickets", "Open findings", "Open tickets", "Enough tickets", "Next to the board", "Value",
           "Denominator", "Numerator", *(measure.label for measure in MEASURES))


def ask(client: TestClient, question: str, mentions: list[str] | None = None) -> dict[str, Any]:
    return post(client, {"question": question, "level": "people", "mentions": mentions or []})


MEMBERS = ("oversight_board_people", {"holding": "none"})
MOST = [("oversight_people_compare", {"measure": "open_tickets", "order": "most"})]
LEAST = [("oversight_people_compare", {"measure": "open_tickets", "order": "fewest"}), MEMBERS]
IDLE = [("oversight_people_compare", {"measure": "in_progress", "order": "fewest"}), MEMBERS]
ACTIVE = [("oversight_people_compare", {"measure": "in_progress", "order": "most"})]
LAST = [("oversight_people_compare", {"measure": "overall", "days": 90, "order": "closer"})]


def doing(days: int = 90) -> list[tuple[str, dict[str, Any]]]:
    return [*MOST, ("oversight_people_compare", {"measure": "done_in_period", "days": days, "order": "most"})]


PARAPHRASES: list[tuple[str, list[tuple[str, dict[str, Any]]]]] = [
    ("Who is the busiest?", MOST), ("Who's the busiest person on the board?", MOST), ("Which developer is busiest?", MOST),
    ("Who is busy?", MOST), ("Who is the most busy?", MOST), ("Who has the most work?", MOST),
    ("Who has the most work on?", MOST), ("Who works the most?", MOST), ("Who is working the most?", MOST),
    ("Who has the most on their plate?", MOST), ("Who has the most going on?", MOST), ("Who has the most tasks?", MOST),
    ("Who has the most?", MOST), ("Which person has the most work right now?", MOST), ("Who carries the most work?", MOST),
    ("Who is the most loaded?", MOST), ("Who has the heaviest load?", MOST), ("Who is over capacity?", MOST),
    ("Who is swamped?", MOST),
    ("Who is doing the most?", doing()), ("Who does the most?", doing()), ("Who's doing the most work?", doing()),
    ("Which developer does the most?", doing()), ("Who did the most this month?", doing(30)),
    ("Who is doing the most on this board?", doing()),
    ("Who is doing the least?", LEAST), ("Who does the least?", LEAST), ("Who works the least?", LEAST),
    ("Who is working the least?", LEAST), ("Who is underworked?", LEAST), ("Who is under-worked?", LEAST),
    ("Who has capacity?", LEAST), ("Who has spare capacity?", LEAST), ("Who has free capacity?", LEAST),
    ("Who has the most capacity?", LEAST), ("Who has bandwidth?", LEAST), ("Who could take more work?", LEAST),
    ("Who can take on more?", LEAST), ("Who could take on extra work?", LEAST), ("Who could pick up more tickets?", LEAST),
    ("Which developer could handle more?", LEAST), ("Who has the lightest load?", LEAST),
    ("Who has the lightest workload?", LEAST), ("Who has the least work?", LEAST), ("Who has the least to do?", LEAST),
    ("Who has the least on their plate?", LEAST), ("Who is the least busy?", LEAST), ("Who is not busy?", LEAST),
    ("Who isn't busy?", LEAST), ("Who has nothing to do?", LEAST), ("Who is available?", LEAST),
    ("Who is free to take something?", LEAST), ("Who has room for more?", LEAST), ("Who is underused?", LEAST),
    ("Who has the least?", LEAST), ("Who is the quietest?", LEAST), ("Who is on the bench?", LEAST),
    ("Who is underutilised?", LEAST),
    ("Who is idle?", IDLE), ("Who has nothing in progress?", IDLE), ("Who is not working on anything?", IDLE),
    ("Who is at full capacity?", MOST), ("Who has reached capacity?", MOST), ("Who is near capacity?", MOST),
    ("Who has the least capacity?", MOST), ("Who has the least bandwidth?", MOST), ("Who has no bandwidth left?", MOST),
    ("Who isn't available?", MOST), ("Who is not available?", MOST), ("Who could not take more work?", MOST),
    ("Who can't take on more?", MOST), ("Who has no room for more?", MOST), ("Who is not underworked?", MOST),
    ("Who is overworked?", MOST), ("Who is the most overworked?", MOST), ("Who's flat out?", MOST),
    ("Who's maxed out?", MOST), ("Who's got the most on?", MOST), ("Who has the most on?", MOST),
    ("Who is not overloaded?", LEAST), ("Who isn't swamped?", LEAST), ("Who is the least loaded?", LEAST),
    ("Who has the fewest tasks?", LEAST), ("Who has spare time?", LEAST), ("Who has free time?", LEAST),
    ("Who has spare cycles?", LEAST), ("Who's light on work?", LEAST), ("Who's got the least on?", LEAST),
    ("Who has the least on?", LEAST),
    ("Who is not idle?", ACTIVE), ("Who is the least idle?", ACTIVE),
    ("Show cycle time per person", LAST), ("Who writes the most comments?", LAST),
    ("Who should own the next release?", LAST), ("Which person joined most recently?", LAST),
    ("Who works weekends?", LAST), ("Who is the team lead?", LAST),
]


def test_there_are_more_than_sixty_paraphrases() -> None:
    assert len(PARAPHRASES) >= 60 and len({question for question, _ in PARAPHRASES}) == len(PARAPHRASES)


@pytest.mark.parametrize(("question", "expected"), PARAPHRASES)
def test_no_paraphrase_is_turned_away(client: TestClient, question: str, expected: list[Any]) -> None:
    body = ask(client, question)
    assert body["outcome"] == "answered" and body["message"] is None, (question, body["message"])
    plan = [(step["tool"], {key: value for key, value in step["params"].items() if key != "project"})
            for step in body["plan"]]
    assert plan == expected, question
    assert PEOPLE_HELP not in body["answer"] and _NOT_UNDERSTOOD_DEFAULT not in body["answer"]
    if expected is LAST:
        assert body["answer"].startswith(LAST_RESORT_OVERALL + "\n\nMeasures closer to the process than the board")
    else:
        assert route(question, PEOPLE, project="DEMO").understood
        assert not body["answer"].startswith("This answer uses")
    for fact in body["facts"]:
        if fact["tool"] != "oversight_people_compare" or fact["params"]["measure"] == "overall":
            continue
        people = [row for row in fact["rows"] if row["person_id"]]
        values = [row["value"] for row in people]
        fewest = fact["params"].get("order") == "fewest"
        assert values == sorted(values, reverse=not fewest), question
        start = body["answer"].index(f"{people[0]['label']}, ")
        sentence = body["answer"][start:body["answer"].index("The whole board", start)]
        places = [sentence.index(f"{row['name']} {row['value']}") for row in people]
        assert places == sorted(places), question
        if "Unassigned:" in sentence:
            assert sentence.index("Unassigned:") > places[-1]


@pytest.mark.parametrize("question", ["Who's not logging hours?", "Who isn't booking their hours?",
                                      "Who logs the fewest hours?", "Who never logs time?",
                                      "Who logged the most hours?", "Which developer logged the most hours?",
                                      "Who has the most hours logged?", "Who has logged the fewest hours?",
                                      "Who records the most hours?", "Who books the most hours?",
                                      "Who tracks time?", "Who records their time?", "Who books time on their tickets?",
                                      "Who keeps track of their time?", "Who fills in their time?",
                                      "Who worked the most hours?", "Who spent the fewest hours?"])
def test_logging_hours_is_the_time_logged_measure(client: TestClient, question: str) -> None:
    body = ask(client, question)
    assert body["outcome"] == "answered" and [step["params"]["measure"] for step in body["plan"]] == ["time_logged"]
    assert body["answer"].startswith("Time logged on done work"), body["answer"]
    assert route(question, PEOPLE, project="DEMO").understood


def test_an_hours_question_gets_one_answer_whatever_its_word_order(client: TestClient) -> None:
    logged, held = ask(client, "Who logged the most hours?"), ask(client, "Who has the most hours logged?")
    assert logged["plan"] == held["plan"] and logged["answer"] == held["answer"]
    assert not logged["answer"].startswith(LAST_RESORT_OVERALL)


def named(client: TestClient, name: str) -> dict[str, Any]:
    return next(row for row in client.get("/api/v1/people").json()["people"]
                if row["person"] and row["person"]["name"] == name)


def test_two_people_side_by_side_by_the_figure_the_question_names(client: TestClient) -> None:
    ada, dev, ben = (named(client, name) for name in ("Ada Brooks", "Dev Malhotra", "Ben Okafor"))
    ids = [ada["person"]["id"], dev["person"]["id"]]
    tickets = ask(client, "Who has more open tickets, @Ada Brooks or @Dev Malhotra?", ids)
    assert [(step["tool"], step["params"]["rank_by"]) for step in tickets["plan"]] == [("oversight_people", "open_tickets")]
    leader = ada if ada["open_tickets"] >= dev["open_tickets"] else dev
    assert tickets["answer"].startswith(f"{leader['person']['name']} has the most open tickets: {leader['open_tickets']}")
    progress = ask(client, "Who has more in progress, @Ada Brooks or @Dev Malhotra?", ids)
    assert [(step["tool"], step["params"]["measure"]) for step in progress["plan"]] == [
        ("oversight_people_compare", "in_progress")]
    (fact,) = progress["facts"]
    assert {row["person_id"] for row in fact["rows"] if row["person_id"]} == set(ids)
    assert all(row["name"] != "Unassigned" for row in fact["rows"])
    closed = ask(client, "Who closed more tickets this month, @Ada Brooks or @Ben Okafor?",
                 [ada["person"]["id"], ben["person"]["id"]])
    assert [(step["params"]["measure"], step["params"]["days"]) for step in closed["plan"]] == [("done_in_period", 30)]
    assert closed["answer"].startswith("Done in the period, last 30 days, most first: ")
    findings = ask(client, "Who has more findings, @Ada Brooks or @Dev Malhotra?", ids)
    assert [step["params"]["rank_by"] for step in findings["plan"]] == ["open_findings"]


NAMED_MEASURES = [
    ("Time logged by @Ada Brooks and @Carmen Ruiz", "time_logged"),
    ("Do @Ada Brooks and @Carmen Ruiz skip stages?", "skipped_stage"),
    ("Do @Ada Brooks and @Carmen Ruiz estimate before starting?", "estimated_before_start"),
    ("How often are @Ada Brooks and @Carmen Ruiz's tickets reopened?", "reopened"),
    ("How many of @Ada Brooks and @Carmen Ruiz's tickets are stuck?", "stuck_now"),
    ("How much time do @Ada Brooks and @Carmen Ruiz log on their done work?", "time_logged"),
    ("Do @Ada Brooks and @Carmen Ruiz go over their estimates?", "within_estimate"),
    ("How many hours did @Ada Brooks and @Carmen Ruiz log?", "time_logged"),
    ("Do @Ada Brooks and @Carmen Ruiz track time?", "time_logged"),
    ("Do @Ada Brooks and @Carmen Ruiz record their time?", "time_logged"),
    ("Do @Ada Brooks and @Carmen Ruiz book time on their tickets?", "time_logged"),
]


@pytest.mark.parametrize(("question", "measure"), NAMED_MEASURES)
def test_named_people_asked_about_a_scorecard_measure_get_that_measure(client: TestClient, question: str,
                                                                       measure: str) -> None:
    people = board(client)
    ids = [people["Ada Brooks"]["id"], people["Carmen Ruiz"]["id"]]
    body = ask(client, question, ids)
    assert body["outcome"] == "answered", body["message"]
    plan = [(step["tool"], {key: value for key, value in step["params"].items() if key != "project"})
            for step in body["plan"]]
    assert plan == [("oversight_people_compare", {"measure": measure, "days": 90, "order": "closer", "person_ids": ids})
                    ], question
    (fact,) = body["facts"]
    named = [row for row in fact["rows"] if row["person_id"]]
    assert sorted(row["person_id"] for row in named) == sorted(ids)
    assert [row["name"] for row in fact["rows"] if not row["person_id"]] == ["Whole board"]
    label = next(row["label"] for row in fact["rows"])
    assert body["answer"].startswith(f"{label}, "), body["answer"]
    for figure in ("open breaches", "open tickets:", "Done in the period"):
        assert figure not in body["answer"].split("\n\n")[0], (question, body["answer"])


@pytest.mark.parametrize(("question", "measure"), [(question.replace(" and @Carmen Ruiz", ""), measure)
                                                   for question, measure in NAMED_MEASURES])
def test_one_person_asked_about_a_scorecard_measure_gets_their_scorecard(client: TestClient, question: str,
                                                                         measure: str) -> None:
    ada = board(client)["Ada Brooks"]["id"]
    body = ask(client, question, [ada])
    assert body["outcome"] == "answered", body["message"]
    assert [(step["tool"], step["params"]["person_ids"], step["params"]["days"]) for step in body["plan"]] == [
        ("oversight_person_scorecard", [ada], 90)], question
    (fact,) = body["facts"]
    assert measure in {row["measure"] for row in fact["rows"]}
    assert body["answer"].startswith("Ada Brooks, last 90 days, next to the board"), body["answer"]


@pytest.mark.parametrize("question", ["Does @Carmen Ruiz track time?", "Does @Carmen Ruiz track her time?",
                                      "Does @Carmen Ruiz record her time?", "Does @Carmen Ruiz book time?"])
def test_one_person_asked_whether_they_track_time_gets_their_scorecard(client: TestClient, question: str) -> None:
    carmen = board(client)["Carmen Ruiz"]["id"]
    body = ask(client, question, [carmen])
    assert [(step["tool"], step["params"]["person_ids"]) for step in body["plan"]] == [
        ("oversight_person_scorecard", [carmen])], question
    assert "time_logged" in {row["measure"] for row in body["facts"][0]["rows"]}


def test_with_comparison_off_two_people_asked_whether_they_track_time_are_refused(off: TestClient) -> None:
    people = board(off)
    ids = [people["Ada Brooks"]["id"], people["Carmen Ruiz"]["id"]]
    for question in ("Do @Ada Brooks and @Carmen Ruiz track time?", "Do @Ada Brooks and @Carmen Ruiz record their time?"):
        body = post(off, {"question": question, "level": "people", "mentions": ids})
        assert (body["outcome"], body["message"]) == ("not_understood", COMPARISON_OFF), question


def test_with_comparison_off_one_measure_is_one_persons_scorecard_and_two_are_refused(off: TestClient) -> None:
    people = board(off)
    ada, carmen = people["Ada Brooks"]["id"], people["Carmen Ruiz"]["id"]
    two = post(off, {"question": "Time logged by @Ada Brooks and @Carmen Ruiz", "level": "people",
                     "mentions": [ada, carmen]})
    assert (two["outcome"], two["message"]) == ("not_understood", COMPARISON_OFF)
    one = post(off, {"question": "Time logged by @Ada Brooks", "level": "people", "mentions": [ada]})
    assert one["outcome"] == "answered" and [step["tool"] for step in one["plan"]] == ["oversight_person_scorecard"]


def test_the_most_findings_are_breaches_and_warnings_together(client: TestClient) -> None:
    people = [row for row in client.get("/api/v1/people").json()["people"] if row["person"]]
    top = max(people, key=lambda row: (row["open_breaches"] + row["open_warnings"], -ord(row["person"]["name"][0])))
    body = ask(client, "Who has the most findings?")
    assert [step["params"]["rank_by"] for step in body["plan"]] == ["open_findings"]
    (fact,) = body["facts"]
    assert "open_findings" in [column["key"] for column in fact["columns"]]
    total = top["open_breaches"] + top["open_warnings"]
    assert body["answer"].startswith(f"{top['person']['name']} has the most open findings: {total}")
    with_findings = ask(client, "Who has the most tickets with findings?")
    assert [step["params"]["rank_by"] for step in with_findings["plan"]] == ["tickets_with_findings"]


def test_a_surname_alone_names_its_one_holder(client: TestClient) -> None:
    ada = named(client, "Ada Brooks")
    body = ask(client, "How is Brooks doing?")
    assert body["outcome"] == "answered" and [who["id"] for who in body["people"]][:1] == [ada["person"]["id"]]
    assert "can't be answered" not in (body["message"] or "")


@pytest.mark.parametrize("level", ["people", "team"])
def test_open_tickets_counted_are_the_open_tickets_everywhere_else(client: TestClient, level: str) -> None:
    portfolio = client.get("/api/v1/portfolio").json()["boards"][0]
    for question, state, figure in (("How many open tickets are there?", "open", "open_tickets"),
                                    ("How many open issues does the board have?", "open", "open_tickets"),
                                    ("How many tickets are in progress?", "in_progress", "in_progress")):
        body = post(client, {"question": question, "level": level})
        assert [step["params"].get("state") for step in body["plan"]] == [state], question
        assert body["facts"][0]["rows"][0]["count"] == portfolio[figure], question
        assert body["answer"].startswith(f"There are {portfolio[figure]} "), (question, body["answer"])


def test_a_rate_of_now_says_no_period(client: TestClient) -> None:
    body = ask(client, "Who has tickets stuck?")
    assert body["answer"].startswith("Open tickets stuck in a status, closest to the process first: ")
    assert "last 90 days" not in body["answer"].split("\n")[0]


@pytest.mark.parametrize(("question", "name"), [
    ("how is ana doing?", "Ana"), ("Who is Zara?", "Zara"), ("What is zara's backlog?", "Zara"),
    ("Is Zoë doing well?", "Zoë"), ("what is olu working on?", "Olu"), ("Show me Priya's tickets", "Priya"),
])
def test_a_typed_name_no_one_has_is_said_so(client: TestClient, question: str, name: str) -> None:
    body = ask(client, question)
    assert (body["outcome"], body["message"]) == ("not_understood", f"No one called {name} is on this board.")
    assert body["people"] == [] and body["facts"] == []


@pytest.mark.parametrize("question", ["How is the sprint doing?", "how is brooks doing?", "How is DEMO doing?",
                                      "when will the sprint end?", "who is zara?", "How is velocity doing?",
                                      "What is the team's backlog?", "How is everyone doing?"])
def test_no_name_is_read_where_none_stands(client: TestClient, question: str) -> None:
    people = list(board(client).values())
    assert unknown_name(question, people, asks=asks_of_someone(question)) is None
    assert "No one called" not in (ask(client, question)["message"] or "")


def test_with_comparison_off_the_help_stays_for_what_nothing_places(off: TestClient) -> None:
    body = post(off, {"question": "Show cycle time per person", "level": "people"})
    assert (body["outcome"], body["message"]) == ("not_understood", PEOPLE_HELP)
    one = post(off, {"question": "Who could take more work?", "level": "people"})
    assert (one["outcome"], one["message"]) == ("not_understood", COMPARISON_OFF)


def test_the_overall_comparisons_note_says_what_each_column_means(client: TestClient) -> None:
    body = ask(client, "Who follows the process most closely?")
    note = body["facts"][0]["note"]
    assert body["facts"][0]["params"]["measure"] == "overall"
    assert "is Open findings" not in note and "per 10 Open" not in note
    assert note.endswith(
        "; by each ticket's current assignee, Epics left out; the closer, further and level counts are the measures on "
        "which the person's figure stands closer to the process than the board's, further from it, or level with it "
        "(within 5 points, 0.5 for the ratio); measures compared is their sum; too few tickets counts the measures "
        "where either side rests on fewer than 5 tickets; open findings per 10 open tickets is the open breaches and "
        "warnings on the person's open tickets for every 10 of those tickets, compared only when it rests on enough "
        "tickets")
    assert body["outcome"] == "answered"


@pytest.mark.parametrize("question", ["Who follows the process most closely?", "Who reopens the most work?",
                                      "Who skips stages the most?",
                                      "Who has the biggest backlog?"])
def test_no_note_capitalises_a_column_label_mid_sentence(client: TestClient, question: str) -> None:
    for fact in ask(client, question)["facts"]:
        for label in _LABELS:
            assert not re.search(rf"(?<=[a-z,;] ){re.escape(label)}\b", fact["note"]), (label, fact["note"])


def _people_part(rows: list[dict[str, Any]], figure: str) -> list[tuple[str, int]]:
    return [(row["name"], row[figure]) for row in rows if row["person_id"]]


def test_one_persons_tickets_are_one_figure_everywhere(client: TestClient) -> None:
    rows = {row["person"]["id"]: row for row in client.get("/api/v1/people").json()["people"] if row["person"]}
    directory = {entry["person"]["id"]: entry["tickets"] for entry in client.get("/api/v1/people/directory").json()[
        "people"]}
    scorecards = client.get("/api/v1/people/scorecard", params={"level": "people"}).json()
    ranked = ask(client, "Who holds the most open tickets?")["facts"][0]["rows"]
    backlog = ask(client, "Who has the biggest backlog?")["facts"][0]["rows"]
    assert len(rows) == len(scorecards["people"]) == 8
    for person in scorecards["people"]:
        who = person["person"]["id"]
        assert rows[who]["tickets"] == directory[who] == person["tickets"] > 0
        open_tickets = person["values"]["open_tickets"]["value"]
        assert rows[who]["open_tickets"] == open_tickets
        assert next(row["open_tickets"] for row in ranked if row["person_id"] == who) == open_tickets
        assert next(row["value"] for row in backlog if row["person_id"] == who) == open_tickets
        mine = ask(client, f"What is @{person['person']['name']}'s backlog?", [who])
        assert f"; {open_tickets} in all" in mine["facts"][0]["note"]
        every = ask(client, f"Show all the tickets @{person['person']['name']} has ever had", [who])
        assert every["facts"][0]["params"]["state"] == "all" and f"; {person['tickets']} in all" in every["facts"][0][
            "note"]
    (nobody,) = [row for row in client.get("/api/v1/people").json()["people"] if row["person"] is None]
    board_open = scorecards["board"]["values"]["open_tickets"]["value"]
    assert sum(row["open_tickets"] for row in rows.values()) + nobody["open_tickets"] == board_open
    assert sum(row["tickets"] for row in rows.values()) + nobody["tickets"] == scorecards["board"]["tickets"]


@pytest.mark.parametrize(("ranking", "backlog"), [
    ("Who holds the most open tickets?", "Who has the biggest backlog?"),
    ("Who has the fewest open tickets?", "Who has the smallest backlog?"),
])
def test_one_count_gives_one_order_and_unassigned_is_never_ranked(client: TestClient, ranking: str,
                                                                   backlog: str) -> None:
    by_people, by_backlog = ask(client, ranking), ask(client, backlog)
    people_rows, backlog_rows = by_people["facts"][0]["rows"], by_backlog["facts"][0]["rows"]
    assert _people_part(people_rows, "open_tickets") == [(row["name"], row["value"]) for row in backlog_rows
                                                         if row["person_id"]]
    (leader, figure), nobody = _people_part(people_rows, "open_tickets")[0], people_rows[-1]
    assert nobody["name"] == "Unassigned" and nobody["person_id"] is None
    first = "fewest" if "fewest" in ranking else "most"
    assert by_people["answer"].startswith(f"{leader} has the {first} open tickets: {figure}, then ")
    assert by_backlog["answer"].startswith(f"Open tickets, {first} first: {leader} {figure}, ")
    for body in (by_people, by_backlog):
        assert body["answer"].count("Unassigned") == 1
        assert f"Unassigned: {nobody['open_tickets']} open tickets." in body["answer"]
        assert body["answer"].index(f"{leader}") < body["answer"].index("Unassigned")


def test_someone_who_holds_only_epics_holds_no_tickets(tmp_path: Path) -> None:
    build(tmp_path / "oversight.db")
    with Store.open(tmp_path / "oversight.db") as store, store.transaction():
        store.upsert("issues", {
            "issue_key": "REV-90", "issue_id": "9090", "project_key": "REV", "issue_type": "Epic", "summary": "A theme",
            "status": "In Progress", "status_category": "indeterminate", "assignee_id": "acc-zed",
            "created_at": to_iso(T - 30 * DAY), "updated_at": to_iso(T - DAY),
        })
    zed = pid("zed")
    with serve(tmp_path, write_config(tmp_path)) as served:
        rows = served.get("/api/v1/people", params={"project": "REV"}).json()["people"]
        assert zed not in {row["person"]["id"] for row in rows if row["person"]}
        directory = {entry["person"]["id"]: entry["tickets"]
                     for entry in served.get("/api/v1/people/directory", params={"project": "REV"}).json()["people"]}
        assert directory[zed] == 0
        everyone = scorecard(served)
        assert zed not in {row["person"]["id"] for row in everyone["people"]}
        (one,) = scorecard(served, [("person", zed)])["people"]
        assert one["tickets"] == 0 and all(value["value"] is None for value in one["values"].values())
        body = served.post("/api/v1/ask", params={"project": "REV"},
                           json={"question": "Who has no tickets?", "level": "people"}).json()
        assert body["answer"] == "Mike Member and Zed Tester have no tickets on this board."


def _unmeasured(client: TestClient, board: str) -> dict[str, str]:
    body = scorecard(client, board=board)
    return {measure["key"]: measure["reason"] for measure in body["measures"] if not measure["measurable"]}


def test_the_demo_scorecard_measures_what_the_overview_lists(client: TestClient) -> None:
    overview = client.get("/api/v1/overview").json()
    by_rule = {row["rule_id"]: row for row in overview["by_rule"]}
    measures = {measure["key"]: measure for measure in
                client.get("/api/v1/people/scorecard", params={"level": "people"}).json()["measures"]}
    assert measures["skipped_stage"]["measurable"] is by_rule["R-FLOW-02"]["measurable"] is True
    assert by_rule["R-FLOW-02"]["open"] > 0
    assert measures["code_review"]["measurable"] is by_rule["R-FLOW-01"]["measurable"] is False
    assert measures["code_review"]["reason"] == NO_CODE_REVIEW
    ada = board(client)["Ada Brooks"]
    one = ask(client, "How is @Ada Brooks doing?", [ada["id"]])
    assert "Not measurable on this board: Code review recorded." in one["answer"]
    assert "Not measurable on this board: Skipped a stage" not in one["answer"]
    skips = ask(client, "Who skips stages the most?")
    assert skips["answer"].startswith("Skipped a stage, last 90 days, highest first: ")


def test_before_any_rules_run_the_engines_own_test_stands_for_it(tmp_path: Path) -> None:
    build(tmp_path / "oversight.db")
    with serve(tmp_path, write_config(tmp_path)) as served:
        assert _unmeasured(served, "REV") == {}
        assert _unmeasured(served, "FLAT") == {"code_review": NO_CODE_REVIEW, "time_logged": NO_TIME_LOGGED}
    catalogue = yaml.safe_load((CONFIG_FIXTURES / "rules.yaml").read_text(encoding="utf-8"))
    for rule in catalogue["rules"]:
        if rule["id"] in ("R-FLOW-02", "R-FLOW-01"):
            rule["enabled"] = False
    with serve(tmp_path, write_config(tmp_path / "off", rules=catalogue)) as served:
        assert _unmeasured(served, "REV") == {"skipped_stage": RULE_OFF, "code_review": RULE_OFF}
        assert _unmeasured(served, "FLAT") == {"skipped_stage": RULE_OFF, "code_review": NO_CODE_REVIEW,
                                               "time_logged": NO_TIME_LOGGED}


def _rules_run(store: Store, run_id: str, at: datetime, measured: dict[tuple[str, str], int]) -> None:
    store.conn.execute("INSERT INTO runs (run_id, kind, status, started_at, finished_at) VALUES (?, 'rules', "
                       "'succeeded', ?, ?)", (run_id, to_iso(at), to_iso(at)))
    for (board, rule_id), measurable in measured.items():
        store.upsert("rule_status", {"run_id": run_id, "project_key": board, "rule_id": rule_id,
                                     "rule_version": "0.1.0", "measurable": measurable})


def test_the_latest_rules_run_on_the_board_decides(tmp_path: Path) -> None:
    build(tmp_path / "oversight.db")
    with Store.open(tmp_path / "oversight.db") as store, store.transaction():
        _rules_run(store, "rules-old", T - 2 * DAY, {("REV", "R-FLOW-02"): 1, ("REV", "R-FLOW-01"): 1,
                                                     ("FLAT", "R-FLOW-02"): 1})
        _rules_run(store, "rules-new", T - DAY, {("REV", "R-FLOW-02"): 0, ("REV", "R-FLOW-01"): 1,
                                                 ("FLAT", "R-FLOW-01"): 1})
        _rules_run(store, "rules-hours", T, {("HOURS", "R-FLOW-02"): 1})
    with serve(tmp_path, write_config(tmp_path)) as served:
        assert _unmeasured(served, "REV") == {"skipped_stage": NO_STAGES_TO_SKIP}
        assert _unmeasured(served, "FLAT") == {"skipped_stage": NO_STAGES_TO_SKIP, "time_logged": NO_TIME_LOGGED}
        assert _unmeasured(served, "HOURS") == {"code_review": NO_CODE_REVIEW, "time_logged": NO_TIME_LOGGED}
        rev = scorecard(served, board="REV")
        assert rev["board"]["values"]["skipped_stage"]["value"] is None
