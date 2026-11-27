from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oversight.api.app import context, create_app
from oversight.api.credit import Credit
from oversight.api.proof import MEASURE_KEYS, proof
from oversight.api.scorecard import MEASURES, Board, Ticket, load_board, scorecard
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.rules.ignores import IgnoreInfo, Ignores
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES, VIEWER_HEADERS
from tests.test_scorecard import ACCOUNTS, DAY, HOUR, KEY, MS, S, T, build, pid, serve, write_config

PATH = "/api/v1/people/scorecard/tickets"
NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
PROGRAMMER = "customfield_10138"  # the fixture site's Programmer field
LEVEL_ONLY = {"detail": "person scorecards are for people level only", "code": "people_level_only"}
WHOLE_BOARD: None = None
ADMIN_NAME = "Morgan Reyes"
SECRETS = [*ACCOUNTS.values(), "acc-", "@example.invalid", "avatar_url", "Europe/London"]

# Alpha's tickets behind each figure over 90 days: counted, then the rest, each in the contract's order.
ALPHA_90: dict[str, tuple[list[str], list[str]]] = {
    "open_tickets": (["REV-9", "REV-10", "REV-11"], []),
    "in_progress": (["REV-9", "REV-11"], []),
    "done_in_period": (["REV-2", "REV-3", "REV-4", "REV-5", "REV-1"], []),
    "estimated_before_start": (["REV-7", "REV-2", "REV-9", "REV-3"], ["REV-11", "REV-5", "REV-4"]),
    "time_logged": (["REV-2", "REV-3", "REV-4", "REV-1"], ["REV-5"]),
    "within_estimate": (["REV-3", "REV-1"], ["REV-2"]),
    "stuck_now": (["REV-9"], ["REV-10", "REV-11"]),
    "reopened": (["REV-2"], ["REV-3", "REV-4", "REV-5", "REV-1"]),
    "skipped_stage": (["REV-3"], ["REV-2", "REV-4", "REV-5", "REV-1"]),
    "code_review": (["REV-3", "REV-1"], ["REV-2", "REV-5"]),
    "open_findings_per_10": (["REV-9", "REV-10", "REV-11"], []),
    "tickets": ([f"REV-{n}" for n in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11)], []),
}


def get(client: TestClient, measure: str, person: str | None = None, *, days: int | None = 90, board: str = "REV",
        **extra: str) -> dict[str, Any]:
    response = client.get(PATH, params=query(measure, person, days=days, board=board, **extra))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def query(measure: str, person: str | None = None, *, days: int | None = 90, board: str = "REV",
          **extra: str) -> list[tuple[str, Any]]:
    params: list[tuple[str, Any]] = [("project", board), ("level", "people"), ("measure", measure)]
    if days is not None:
        params.append(("days", days))
    if person is not None:
        params.append(("person", person))
    return params + list(extra.items())


def keys(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return [row["key"] for row in rows]


def number(key: str) -> int:
    return int(key.rpartition("-")[2])


def check(body: Mapping[str, Any], cell: Mapping[str, Any]) -> None:
    """The contract's invariants of a proof, and its figure exactly as the scorecard shows it."""
    assert body["value"] == cell
    rows, left_out, value = body["tickets"], body["left_out"], body["value"]
    every = keys(rows) + keys(left_out)
    assert len(every) == len(set(every)), "a key twice"
    assert body["total"] == len(rows)
    assert body["counted"] == sum(row["counted"] for row in rows)
    assert all(row["ignore"] is not None and not row["counted"] for row in left_out)
    assert all(row["url"] is None for row in [*rows, *left_out])
    flags = [row["counted"] for row in rows]
    assert flags == sorted(flags, reverse=True), "counted first"
    for group in (True, False):
        dated = [row for row in rows if row["counted"] is group]
        expected = sorted(dated, key=lambda row: (number(row["key"]), row["key"]))
        expected.sort(key=lambda row: row["at"] or "", reverse=True)
        assert keys(dated) == keys(expected), "newest first, then by key number"
    kind = body["measure"]["kind"]
    if value["value"] is None and value["numerator"] is None:
        assert (body["total"], body["counted"], rows) == (0, 0, []), "nothing behind a figure there is none of"
    elif kind == "count":
        assert body["total"] == body["counted"] == value["value"]
        assert all(flags)
    elif kind == "rate":
        assert (body["counted"], body["total"]) == (value["numerator"], value["denominator"])
    else:
        assert body["total"] == value["denominator"]
        assert sum(row["detail"]["findings"] for row in rows) == value["numerator"]
        assert body["counted"] == sum(row["detail"]["findings"] > 0 for row in rows)


def cells(board: Board, days: int, person: str | None) -> dict[str, Any]:
    """Each figure the scorecard shows for the person (None: the whole board), with "N tickets" as `tickets`."""
    card = scorecard(board, days, None if person is None else [person], comparison=True)
    row = card["board"] if person is None else card["people"][0]
    found = dict(row["values"])
    found["tickets"] = {"value": row["tickets"], "numerator": None, "denominator": None, "enough": True,
                        "vs_board": None}
    return found


def board_of(client: TestClient, project: str) -> Board:
    ctx = replace(context(client.app), selected=project)  # type: ignore[arg-type]
    conn = open_readonly(ctx.db_path)
    try:
        ctx.served(conn)
        return load_board(conn, ctx)
    finally:
        conn.close()


def every_proof(board: Board) -> int:
    """Check every proof the board can show against its scorecard; how many were checked."""
    checked = 0
    for days in (30, 90, 180):
        for who in [WHOLE_BOARD, *(one["id"] for one in board.everyone)]:
            shown = cells(board, days, who)
            for key in MEASURE_KEYS:
                if board.reason(key, board.period(days)) is not None:
                    continue
                check(proof(board, key, who, days), shown[key])
                checked += 1
    return checked


def access_rows(path: Path) -> list[dict[str, Any]]:
    conn = open_readonly(path)
    try:
        rows = [dict(row) for row in conn.execute("SELECT * FROM access_log ORDER BY at, rowid")]
    finally:
        conn.close()
    for row in rows:
        row["subjects"], row["detail"] = json.loads(row.pop("subject_ids_json")), json.loads(row.pop("detail_json"))
    return rows


def clear_log(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM access_log")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("proof")
    build(path / "oversight.db")
    return path


@pytest.fixture(scope="module")
def client(root: Path) -> Iterator[TestClient]:
    with serve(root, write_config(root)) as served:
        yield served


# --- the tickets behind each figure ----------------------------------------------------------


@pytest.mark.parametrize("key", MEASURE_KEYS)
def test_each_figure_of_a_person_lists_exactly_its_tickets(client: TestClient, key: str) -> None:
    body = get(client, key, pid("alpha"))
    counted, rest = ALPHA_90[key]
    assert keys(body["tickets"]) == counted + rest
    assert [row["counted"] for row in body["tickets"]] == [True] * len(counted) + [False] * len(rest)
    assert body["left_out"] == []
    assert body["person"] == {"id": pid("alpha"), "name": "Alpha Tester", "avatar": None}
    assert body["period"] == {"days": 90, "from": to_iso(S), "to": to_iso(T)}
    assert set(body) == {"meta", "period", "measure", "person", "value", "total", "counted", "tickets", "left_out"}


def test_the_measure_is_the_scorecards_and_tickets_has_its_own(client: TestClient) -> None:
    card = client.get("/api/v1/people/scorecard", params={"project": "REV", "level": "people"}).json()
    for measure in card["measures"]:
        assert get(client, measure["key"])["measure"] == measure
    assert get(client, "tickets")["measure"] == {"key": "tickets", "label": "Tickets", "kind": "count",
                                                  "direction": "neutral", "rule_ids": [], "measurable": True,
                                                  "reason": None}


def test_the_value_is_the_scorecards_cell_and_the_lists_add_up_to_it(client: TestClient) -> None:
    for days in (30, 90, 180):
        card = client.get("/api/v1/people/scorecard", params=[("project", "REV"), ("level", "people"),
                                                               ("days", days), ("person", pid("alpha")),
                                                               ("person", pid("mike"))]).json()
        rows = {row["person"]["id"]: row for row in card["people"]}
        for key in MEASURE_KEYS:
            board_cell = card["board"]["values"].get(key) or {"value": card["board"]["tickets"], "numerator": None,
                                                              "denominator": None, "enough": True, "vs_board": None}
            check(get(client, key, days=days), board_cell)
            for who in (pid("alpha"), pid("mike")):
                cell = rows[who]["values"].get(key) or {"value": rows[who]["tickets"], "numerator": None,
                                                         "denominator": None, "enough": True, "vs_board": None}
                check(get(client, key, who, days=days), cell)


def test_days_is_ninety_when_left_out(client: TestClient) -> None:
    assert get(client, "done_in_period", pid("alpha"), days=None) == get(client, "done_in_period", pid("alpha"))


def test_each_row_says_what_it_is_and_why_it_counts(client: TestClient) -> None:
    alpha = {"id": pid("alpha"), "name": "Alpha Tester", "avatar": None}
    rows = {row["key"]: row for row in get(client, "done_in_period", pid("alpha"))["tickets"]}
    assert rows["REV-2"] == {
        "key": "REV-2", "summary": "Sample 1", "issue_type": "Task", "status": "Done", "status_category": "done",
        "counted": True, "credit": {"person": alpha, "basis": "assignee_now", "field": None}, "at": to_iso(T),
        "detail": None, "ignore": None, "url": None,
    }
    whole = {row["key"]: row for row in get(client, "open_tickets")["tickets"]}
    assert whole["REV-41"]["credit"] == {"person": None, "basis": None, "field": None}
    assert whole["REV-40"]["credit"]["person"]["name"] == "Bravo Tester"
    assert all(row["at"] is None for row in whole.values())


def test_each_measures_detail_in_raw_numbers(client: TestClient) -> None:
    def detail(key: str, ticket: str) -> Any:
        return next(row for row in get(client, key, pid("alpha"))["tickets"] if row["key"] == ticket)["detail"]

    assert detail("estimated_before_start", "REV-4") == {"started_at": to_iso(S), "estimate_set_at": to_iso(S + 2 * HOUR),
                                                         "grace_hours": 1.0}
    assert detail("estimated_before_start", "REV-11") == {"started_at": to_iso(T - DAY), "estimate_set_at": None,
                                                          "grace_hours": 1.0}
    assert detail("time_logged", "REV-4") == {"time_spent_s": 0, "subtask_time_spent_s": 1800}
    assert detail("time_logged", "REV-3") == {"time_spent_s": 7200, "subtask_time_spent_s": 0}
    assert detail("within_estimate", "REV-2") == {"logged_s": 3600, "estimate_s": 3 * 8 * 3600, "ratio": 1.5,
                                                  "limit": 1.5}
    assert detail("stuck_now", "REV-11") == {"status": "Code Review", "since": None, "hours": None, "threshold": None}
    assert detail("reopened", "REV-2") == {"times": 1}
    assert detail("reopened", "REV-3") == {"times": 0}
    assert detail("skipped_stage", "REV-3") == {"stages": ["Testing"]}
    assert detail("skipped_stage", "REV-2") == {"stages": []}
    assert detail("code_review", "REV-5") == {"breach": True}
    assert detail("code_review", "REV-3") == {"breach": False}
    assert detail("open_findings_per_10", "REV-9") == {"findings": 2, "rule_ids": ["W-EFF-01", "W-AGE-01"]}
    assert detail("open_findings_per_10", "REV-10") == {"findings": 1, "rule_ids": ["W-EST-01"]}
    for key in ("open_tickets", "in_progress", "done_in_period", "tickets"):
        assert {row["detail"] is None for row in get(client, key, pid("alpha"))["tickets"]} == {True}


def test_the_dates_rows_are_ordered_by(client: TestClient) -> None:
    started = get(client, "estimated_before_start", pid("alpha"))["tickets"]
    assert [row["at"] for row in started[:4]] == [to_iso(T - DAY), to_iso(T - 2 * DAY), to_iso(T - 3 * DAY),
                                                  to_iso(T - 12 * DAY)]
    done = get(client, "reopened", pid("alpha"))["tickets"]
    assert [row["at"] for row in done] == [to_iso(T), to_iso(T - 10 * DAY), to_iso(T - 20 * DAY), to_iso(T - 30 * DAY),
                                           to_iso(S)]
    assert keys(get(client, "tickets", pid("alpha"))["tickets"])[-3:] == ["REV-9", "REV-10", "REV-11"]


def test_a_member_with_no_tickets_has_nothing_behind_their_dashes(client: TestClient) -> None:
    for key in MEASURE_KEYS:
        body = get(client, key, pid("mike"))
        assert (body["total"], body["counted"], body["tickets"], body["left_out"]) == (0, 0, [], [])
    assert get(client, "tickets", pid("mike"))["value"]["value"] == 0
    assert get(client, "open_tickets", pid("mike"))["value"]["value"] is None


def test_every_proof_of_the_fixture_boards_adds_up_to_its_cell(client: TestClient) -> None:
    assert sum(every_proof(board_of(client, board)) for board in ("REV", "FLAT", "HOURS")) > 100


# --- builders: work counts for whoever built it ------------------------------------------------


def give_credit(path: Path) -> None:
    """The REV board with Programmers on some tickets and assignees cleared at hand-offs, as MJH DASHBOARD has them."""
    with Store.open(path) as store, store.transaction():
        for key, who in (("REV-1", ["zed", "bravo"]), ("REV-3", ["alpha"]), ("REV-20", ["mike"])):
            payload = {"fields": {PROGRAMMER: [{"accountId": ACCOUNTS[one], "displayName": "x"} for one in who]}}
            store.conn.execute("UPDATE issues SET raw_json = ? WHERE issue_key = ?", (json.dumps(payload), key))
        store.conn.execute("UPDATE issues SET assignee_id = NULL WHERE issue_key IN ('REV-21', 'REV-22')")
        for n, (key, at, before, after) in enumerate([
            ("REV-21", T - 2 * DAY, ACCOUNTS["bravo"], None),
            ("REV-22", T - 9 * DAY, ACCOUNTS["zed"], ACCOUNTS["mike"]),
            ("REV-22", T - 4 * DAY, ACCOUNTS["mike"], None),
        ]):
            store.upsert("field_changes", {"issue_key": key, "changelog_id": str(900 + n), "field": "assignee",
                                           "changed_at": to_iso(at), "from_value": before, "to_value": after})
        store.conn.execute(
            "INSERT INTO status_intervals (issue_key, seq, status, stage, entered_at, left_at, hours)"
            " VALUES ('REV-21', 0, 'In Progress', 'development', ?, ?, 24.0)", (to_iso(T - 3 * DAY), to_iso(T - 2 * DAY)))


@pytest.fixture(scope="module")
def credited(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    path = tmp_path_factory.mktemp("proof_credit")
    build(path / "oversight.db")
    give_credit(path / "oversight.db")
    with serve(path, write_config(path)) as served:
        yield served


def test_work_counts_for_its_programmers_else_its_assignee_now(credited: TestClient) -> None:
    programmer = {"basis": "field", "field": "Programmer"}
    zed = get(credited, "done_in_period", pid("zed"))
    assert keys(zed["tickets"]) == ["REV-1"]
    assert zed["tickets"][0]["credit"] == {"person": {"id": pid("zed"), "name": "Zed Tester", "avatar": None},
                                           **programmer}
    alpha = get(credited, "done_in_period", pid("alpha"))
    assert "REV-1" not in keys(alpha["tickets"]) and "REV-3" in keys(alpha["tickets"])
    assert next(row for row in alpha["tickets"] if row["key"] == "REV-3")["credit"]["basis"] == "field"
    # Alpha still holds REV-1, but its Programmers are Zed and Bravo, so it is not among Alpha's tickets.
    theirs = {row["key"]: row["credit"] for row in get(credited, "tickets", pid("alpha"))["tickets"]}
    assert "REV-1" not in theirs and theirs["REV-3"]["basis"] == "field"
    bravo = {row["key"]: row["credit"]["basis"] for row in get(credited, "done_in_period", pid("bravo"))["tickets"]}
    assert bravo["REV-1"] == "field" and "REV-21" not in bravo
    mike = {row["key"]: row["credit"]["basis"] for row in get(credited, "tickets", pid("mike"))["tickets"]}
    assert mike == {"REV-20": "field"}
    board = {row["key"]: row["credit"] for row in get(credited, "done_in_period")["tickets"]}
    assert board["REV-1"]["person"]["id"] == pid("zed")  # the first Programmer
    # No Programmer and no one assigned now: it counts for no one, whoever held it before.
    assert board["REV-22"] == {"person": None, "basis": None, "field": None}
    now = {row["key"]: row["credit"] for row in get(credited, "open_tickets")["tickets"]}
    assert now["REV-9"] == {"person": {"id": pid("alpha"), "name": "Alpha Tester", "avatar": None},
                            "basis": "assignee_now", "field": None}


def test_every_proof_of_a_board_with_builders_adds_up_to_its_cell(credited: TestClient) -> None:
    board = board_of(credited, "REV")
    assert {one["id"] for one in board.holders} >= {pid("alpha"), pid("bravo"), pid("zed"), pid("mike")}
    assert every_proof(board) > 100
    for who in (pid("zed"), pid("mike"), None):
        for key in ("tickets", "done_in_period", "reopened"):
            card = credited.get("/api/v1/people/scorecard", params=query(key, who)[:2] + (
                [("person", who)] if who else [])).json()
            row = card["board"] if who is None else card["people"][0]
            cell = row["values"].get(key) or {"value": row["tickets"], "numerator": None, "denominator": None,
                                              "enough": True, "vs_board": None}
            check(get(credited, key, who), cell)


# --- ignores: left out, in their own list -----------------------------------------------------


def ignore(path: Path, issue_key: str, rule_id: str | None, reason: str | None = "Closed long ago") -> None:
    """An admin's ignore, stored as an ignore is, and the open findings it holds resolved as ignored."""
    with Store.open(path) as store, store.transaction():
        store.conn.execute(
            "INSERT INTO finding_ignores (ignore_id, project_key, issue_key, rule_id, scope, reason, created_by,"
            " created_at) VALUES (?, 'REV', ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, issue_key, rule_id, "ticket" if rule_id is None else "finding", reason, ADMIN_NAME,
             to_iso(T - HOUR)))
        store.conn.execute(
            "UPDATE findings SET resolved_at = ?, resolved_why = 'ignored'"
            " WHERE resolved_at IS NULL AND issue_key = ? AND (? IS NULL OR rule_id = ?)",
            (to_iso(T - HOUR), issue_key, rule_id, rule_id))


@pytest.fixture(scope="module")
def ignored(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    path = tmp_path_factory.mktemp("proof_ignored")
    build(path / "oversight.db")
    for issue_key, rule_id in (("REV-9", "W-AGE-01"), ("REV-9", "W-EFF-01"), ("REV-10", None), ("REV-4", "R-EST-02"),
                               ("REV-2", "W-REV-01")):
        ignore(path / "oversight.db", issue_key, rule_id)
    with serve(path, write_config(path)) as served:
        yield served


def test_an_ignored_ticket_is_left_out_of_both_sides_of_a_rate_and_listed_apart(ignored: TestClient) -> None:
    stuck = get(ignored, "stuck_now", pid("alpha"))
    assert keys(stuck["tickets"]) == ["REV-11"] and stuck["counted"] == 0
    assert stuck["value"] == {"value": 0, "numerator": 0, "denominator": 1, "enough": False, "vs_board": None}
    assert keys(stuck["left_out"]) == ["REV-9", "REV-10"]
    assert stuck["left_out"][0]["ignore"] == {"scope": "finding", "rule_id": "W-AGE-01", "reason": "Closed long ago",
                                              "by": ADMIN_NAME, "at": to_iso(T - HOUR)}
    assert stuck["left_out"][1]["ignore"]["scope"] == "ticket"
    assert [row["counted"] for row in stuck["left_out"]] == [False, False]
    estimated = get(ignored, "estimated_before_start", pid("alpha"))
    assert (estimated["counted"], estimated["total"], keys(estimated["left_out"])) == (4, 6, ["REV-4"])
    assert keys(get(ignored, "code_review", pid("alpha"))["left_out"]) == ["REV-2"]


def test_an_ignore_never_changes_a_count_or_a_measure_without_rules(ignored: TestClient, client: TestClient) -> None:
    for key in ("open_tickets", "in_progress", "done_in_period", "tickets", "time_logged"):
        before, after = get(client, key, pid("alpha")), get(ignored, key, pid("alpha"))
        assert after["value"] == before["value"] and keys(after["tickets"]) == keys(before["tickets"]), key
        assert after["left_out"] == []


def test_only_a_left_out_row_carries_an_ignore(ignored: TestClient) -> None:
    for key in ("open_tickets", "stuck_now", "open_findings_per_10", "estimated_before_start", "code_review"):
        body = get(ignored, key, pid("alpha"))
        assert all(row["ignore"] is None for row in body["tickets"]), key
        assert all(row["ignore"] is not None for row in body["left_out"]), key


def test_the_ratio_drops_ignored_findings_and_leaves_out_only_a_ticket_ignored_whole(ignored: TestClient) -> None:
    ratio = get(ignored, "open_findings_per_10", pid("alpha"))
    assert keys(ratio["tickets"]) == ["REV-11", "REV-9"] and keys(ratio["left_out"]) == ["REV-10"]
    rows = {row["key"]: row for row in ratio["tickets"]}
    assert rows["REV-9"]["detail"] == {"findings": 0, "rule_ids": []} and not rows["REV-9"]["counted"]
    assert rows["REV-9"]["ignore"] is None
    assert ratio["value"] == {"value": 5.0, "numerator": 1, "denominator": 2, "enough": False, "vs_board": None}


def test_every_proof_with_ignores_adds_up_to_its_cell(ignored: TestClient) -> None:
    assert every_proof(board_of(ignored, "REV")) > 100


def held(key: str, *rules: str | None) -> Ignores:
    """Ignores of these rules on one ticket (None: the whole ticket)"""
    at = to_iso(T) or ""
    tickets = {key: IgnoreInfo("i0", "ticket", None, None, "Admin", at)} if None in rules else {}
    findings = {(key, rule): IgnoreInfo(f"i{n}", "finding", rule, "why", "Admin", at)
                for n, rule in enumerate(rules) if rule is not None}
    return Ignores(tickets, findings)


@pytest.mark.parametrize("measure", [measure for measure in MEASURES if measure.rule_ids])
def test_ignoring_any_rule_of_a_measure_leaves_the_ticket_out_of_it(client: TestClient, measure: Any) -> None:
    board = board_of(client, "REV")
    for rule_id in [*measure.rule_ids, None]:
        for ticket in board.work:
            changed = replace(board, ignores=held(ticket.issue_key, rule_id))
            body = proof(changed, measure.key, WHOLE_BOARD, 180)
            before = proof(board, measure.key, WHOLE_BOARD, 180)
            listed = ticket.issue_key in keys(before["tickets"])
            assert (ticket.issue_key in keys(body["left_out"])) == listed
            assert ticket.issue_key not in keys(body["tickets"])
            check(body, cells(changed, 180, WHOLE_BOARD)[measure.key])


def test_the_ratio_and_the_counts_follow_their_own_ignore_rules(client: TestClient) -> None:
    board = board_of(client, "REV")
    for key in ("open_tickets", "in_progress", "done_in_period", "tickets", "time_logged", "open_findings_per_10"):
        for ticket in board.work:
            for rules in ((None,), ("R-EST-01", "W-AGE-01", "W-EST-01")):
                changed = replace(board, ignores=held(ticket.issue_key, *rules))
                body = proof(changed, key, WHOLE_BOARD, 180)
                out = key == "open_findings_per_10" and rules == (None,) and ticket.open
                assert (ticket.issue_key in keys(body["left_out"])) == out, (key, ticket.issue_key, rules)
                check(body, cells(changed, 180, WHOLE_BOARD)[key])


# --- refusals, the access log, nothing about anyone -------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        "measure=open_tickets&days=60", "measure=open_tickets&days=", "measure=open_tickets&days=ninety",
        "measure=open_tickets&days=90.0", "measure=open_tickets&days=090", "measure=open_tickets&days=90&days=90",
        "measure=open_tickets&level=admin", "measure=open_tickets&level=People",
        "measure=open_tickets&level=people&level=people", "measure=Open_tickets", "measure=", "measure=overall", "measure=open_tickets&measure=open_tickets",
        "measure=tickets&measure=open_tickets", "days=90", "measure=open_tickets&person=p0123",
        "measure=open_tickets&person=P0123456789AB", "measure=open_tickets&person=acc-alpha",
        "measure=open_tickets&person=board", "measure=open_tickets&person=",
        "measure=open_tickets&person=p0123456789ab&person=p0123456789ab",
        "measure=open_tickets&person=p0123456789ab&person=p0123456789ac", "measure=open_tickets&page=2",
        "measure=open_tickets&project=REV&project=REV", "measure=open_tickets&people=p0123456789ab",
    ],
)
def test_bad_parameters_are_422(client: TestClient, root: Path, params: str) -> None:
    clear_log(root / "oversight.db")
    level = "" if "level=" in params else "level=people&"
    response = client.get(f"{PATH}?{level}{params}")
    assert response.status_code == 422, (params, response.text)
    assert access_rows(root / "oversight.db") == []


@pytest.mark.parametrize("params", [{"measure": "open_tickets"}, {"measure": "tickets", "level": "team"},
                                    {"measure": "stuck_now", "level": "team", "person": "p" + "0" * 12}])
def test_team_level_is_403_people_level_only(client: TestClient, root: Path, params: dict[str, str]) -> None:
    clear_log(root / "oversight.db")
    response = client.get(PATH, params={"project": "REV", **params})
    assert response.status_code == 403 and response.json() == LEVEL_ONLY
    assert response.headers["cache-control"] == "no-store"
    assert access_rows(root / "oversight.db") == []


def test_a_person_not_on_the_board_is_404_and_a_measure_it_cannot_show_409(client: TestClient, root: Path) -> None:
    clear_log(root / "oversight.db")
    for who, board in (("p" + "0" * 12, "REV"), (pid("zed"), "REV"), (pid("alpha"), "FLAT")):
        response = client.get(PATH, params=query("open_tickets", who, board=board))
        assert response.status_code == 404 and response.json() == {"detail": "nobody by that id is on this board"}
    for key, reason in (("code_review", "This board doesn't record code review in Jira."),
                        ("time_logged", "No one logged time on this board in the period.")):
        for who in (None, pid("zed")):
            response = client.get(PATH, params=query(key, who, board="FLAT"))
            assert response.status_code == 409
            assert response.json() == {"detail": reason, "code": "not_measurable"}
    assert client.get(PATH, params=query("time_logged", days=180, board="HOURS")).status_code == 200
    assert client.get(PATH, params=query("time_logged", days=90, board="HOURS")).status_code == 409
    assert client.get(PATH, params=query("open_tickets", board="NOPE")).status_code == 404
    assert client.get(PATH, params=query("open_tickets"), headers={"X-Service-Key": "wrong"}).status_code == 401
    assert len(access_rows(root / "oversight.db")) == 1  # the HOURS board's 180 days


def test_a_read_with_no_viewer_named_is_422(root: Path) -> None:
    configured = Settings(_env_file=None, jira_projects="REV,FLAT,HOURS", api_service_key=KEY,
                          db_path=root / "oversight.db", config_dir=write_config(root / "no_viewer"),
                          reports_dir=root / "reports")
    app = create_app(configured, provider=ScriptedProvider(), now=lambda: T + HOUR)
    with TestClient(app, headers={"X-Service-Key": KEY}) as nameless:
        response = nameless.get(PATH, params=query("open_tickets"))
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["header", "X-Oversight-Viewer"]


def test_each_read_is_recorded_with_its_person_measure_and_days(client: TestClient, root: Path) -> None:
    clear_log(root / "oversight.db")
    get(client, "stuck_now", pid("alpha"), days=30)
    get(client, "tickets")
    found = access_rows(root / "oversight.db")
    assert [(row["action"], row["subjects"], row["detail"]) for row in found] == [
        ("scorecard_tickets_view", [pid("alpha")], {"days": 30, "measure": "stuck_now"}),
        ("scorecard_tickets_view", [], {"days": 90, "measure": "tickets"}),
    ]
    for row in found:
        assert (row["viewer_hash"], row["viewer_name"], row["level"], row["project_key"]) == (
            "local", "Local (sign-in off)", "people", "REV")
    page = client.get("/api/v1/access-log", params={"level": "people", "action": "scorecard_tickets_view"})
    assert page.status_code == 200 and len(page.json()["entries"]) == 2


def test_with_comparison_off_one_person_and_the_whole_board_are_both_allowed(root: Path) -> None:
    with serve(root, write_config(root / "off"), people_comparison="off") as off:
        assert keys(get(off, "stuck_now", pid("alpha"))["tickets"]) == ["REV-9", "REV-10", "REV-11"]
        assert get(off, "open_tickets")["total"] == 6


def test_no_proof_holds_an_account_id_or_anything_else_about_anyone(credited: TestClient) -> None:
    for key in MEASURE_KEYS:
        for who in (None, pid("alpha"), pid("mike")):
            text = json.dumps(get(credited, key, who, days=180))
            assert not [secret for secret in SECRETS if secret in text], key
            for row in json.loads(text)["tickets"]:
                if row["credit"]["person"] is not None:
                    assert set(row["credit"]["person"]) == {"id", "name", "avatar"}


# --- DEMO: every figure, and fast -------------------------------------------------------------


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    root = tmp_path_factory.mktemp("proof_demo")
    settings = Settings(_env_file=None, jira_projects="DEMO", api_service_key=KEY, db_path=root / "oversight.db",
                        config_dir=CONFIG_FIXTURES, reports_dir=root / "reports")
    process = ProcessConfig.load(settings.process_file)
    rules = RulesConfig.load(settings.rules_file)
    fields = FieldsConfig.load(settings.fields_file)
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=NOW, seed=20261105)
        run_derive(store, process, projects=["DEMO"], now=lambda: NOW)
        run_rules(store, process, rules, fields, projects=["DEMO"], now=lambda: NOW)
    app = create_app(settings, provider=ScriptedProvider(), now=lambda: NOW)
    with TestClient(app, headers={"X-Service-Key": KEY, **VIEWER_HEADERS}) as served:
        yield served


def test_every_proof_on_demo_adds_up_to_its_cell(demo: TestClient) -> None:
    board = board_of(demo, "DEMO")
    assert len(board.holders) >= 5
    assert every_proof(board) > 300


def test_demo_proofs_through_the_api_match_the_scorecard(demo: TestClient) -> None:
    card = demo.get("/api/v1/people/scorecard", params={"level": "people", "days": 180}).json()
    for row in [None, *card["people"][:3]]:
        who = None if row is None else row["person"]["id"]
        values = card["board"] if row is None else row
        for key in MEASURE_KEYS:
            if key != "tickets" and not next(m for m in card["measures"] if m["key"] == key)["measurable"]:
                continue
            cell = values["values"].get(key) or {"value": values["tickets"], "numerator": None, "denominator": None,
                                                 "enough": True, "vs_board": None}
            check(get(demo, key, who, days=180, board="DEMO"), cell)


def test_a_stuck_tickets_limit_is_the_one_its_finding_states(demo: TestClient) -> None:
    stuck = get(demo, "stuck_now", days=180, board="DEMO")
    counted = [row for row in stuck["tickets"] if row["counted"]]
    assert counted and all(row["detail"]["threshold"] is None for row in stuck["tickets"] if not row["counted"])
    findings = demo.get("/api/v1/findings", params={"rule": "W-AGE-01", "state": "open", "page_size": 100}).json()
    limits = {item["issue_key"]: item["threshold"] for item in findings["items"]}
    for row in counted:
        detail = row["detail"]
        assert re.fullmatch(r"p\d+ of .+: \d+\.\d h", detail["threshold"]), detail
        assert detail["threshold"] == limits[row["key"]]
        assert detail["status"] == row["status"] and detail["since"] is not None and detail["hours"] > 0


def fastest(client: TestClient, params: list[tuple[str, Any]], runs: int = 5) -> float:
    """The quickest of a few reads, in seconds (the first warms the app)"""
    assert client.get(PATH, params=params).status_code == 200
    times = []
    for _ in range(runs):
        started = time.perf_counter()
        response = client.get(PATH, params=params)
        times.append(time.perf_counter() - started)
        assert response.status_code == 200
    return min(times)


@pytest.mark.slow
def test_the_whole_boards_tickets_on_demo_come_back_in_under_300_ms(
    demo: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    body = get(demo, "tickets", days=180, board="DEMO")
    assert body["total"] > 1000
    elapsed = fastest(demo, query("tickets", days=180, board="DEMO"))
    with capsys.disabled():
        print(f"\nproof of the whole board's tickets: DEMO, {body['total']:,} tickets in {elapsed * 1000:.0f} ms")
    assert elapsed < 0.3


def synthetic(path: Path, tickets: int) -> None:
    """A board of this many tickets, forty people, findings, timelines and Programmers, as a large board has them."""
    statuses = {"To Do": "new", "In Progress": "indeterminate", "Code Review": "indeterminate", "QA": "indeterminate",
                "Done": "done"}
    names = list(statuses)
    with Store.open(path) as store, store.transaction():
        for n, (name, category) in enumerate(statuses.items()):
            store.upsert("statuses", {"project_key": "REV", "status_id": str(10 + n), "name": name, "category": category})
        store.set_cursor("REV", last_synced_at=to_iso(T), run_id="r1", account_timezone="Europe/London")
        store.upsert_many("people", [{"account_id": f"acc-{k}", "display_name": f"Person {k}", "active": 1,
                                      "updated_at": to_iso(T)} for k in range(40)])
        issues, metrics, intervals, findings = [], [], [], []
        for n in range(tickets):
            key, status = f"REV-{n + 1}", names[n % len(names)]
            done = T - (n % 170) * DAY if status == "Done" else None
            started = (done or T) - 3 * DAY if status != "To Do" else None
            raw = {"fields": {PROGRAMMER: [{"accountId": f"acc-{(n + 7) % 40}"}]}} if n % 4 == 0 else {}
            issues.append({"issue_key": key, "issue_id": str(10_000 + n), "project_key": "REV", "issue_type": "Task",
                           "summary": f"Synthetic {n}", "status": status, "status_category": statuses[status],
                           "assignee_id": None if n % 9 == 0 else f"acc-{n % 40}", "created_at": to_iso(S - 30 * DAY),
                           "updated_at": to_iso(T - DAY), "time_spent_s": 3600 * (n % 5), "story_points": 3.0,
                           "aggregate_time_spent_s": 3600 * (n % 5) + 600 * (n % 3), "raw_json": json.dumps(raw)})
            metrics.append({"issue_key": key, "started_at": to_iso(started), "done_at": to_iso(done),
                            "reopen_count": n % 7 == 0, "estimate_basis": "points",
                            "skipped_stages": [{"changelog_id": "1", "from": "In Progress", "to": "Done",
                                                "skipped": ["testing"]}] if n % 11 == 0 else [],
                            "estimate_set_at": to_iso((started or T) - (2 * HOUR if n % 3 else -5 * HOUR)),
                            "effort_ratio": (n % 9) / 4 if n % 5 else None, "status_age_h": 30.0 + n % 400,
                            "computed_at": to_iso(T)})
            intervals.append((key, "QA" if n % 6 == 0 else status, "testing" if n % 6 == 0 else "development",
                              to_iso(S - DAY)))
            for m, rule_id in enumerate(("W-AGE-01", "R-FLOW-01", "R-EST-01")[: n % 4]):
                findings.append({"finding_id": uuid.uuid4().hex, "rule_id": rule_id, "rule_version": "0.1.0",
                                 "issue_key": key, "project_key": "REV", "kind": "warning", "severity": "warning",
                                 "evidence_key": f"e{m}", "message": rule_id,
                                 "evidence_json": {"status": status, "age_h": 900.0, "threshold_h": 524.8, "pct": 90},
                                 "first_seen_at": to_iso(T - 5 * DAY), "last_seen_at": to_iso(T),
                                 "resolved_at": None if m != 1 else to_iso(T - DAY), "resolved_why": None,
                                 "run_id": "r1"})
        store.upsert_many("issues", issues)
        store.upsert_many("issue_metrics", metrics)
        store.conn.executemany("INSERT INTO status_intervals (issue_key, seq, status, stage, entered_at)"
                               " VALUES (?, 0, ?, ?, ?)", intervals)
        store.upsert_many("findings", findings)


@pytest.mark.slow
def test_the_whole_boards_tickets_on_a_board_of_5000_come_back_in_under_a_second_and_a_half(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    synthetic(tmp_path / "oversight.db", 5000)
    with serve(tmp_path, write_config(tmp_path)) as served:
        body = get(served, "tickets", days=180)
        assert body["total"] == 5000 and body["value"]["value"] == 5000
        elapsed = fastest(served, query("tickets", days=180))
        with capsys.disabled():
            print(f"\nproof of the whole board's tickets: 5,000 tickets in {elapsed * 1000:.0f} ms")
        assert elapsed < 1.5
        check(get(served, "stuck_now", days=180), served.get("/api/v1/people/scorecard", params={
            "project": "REV", "level": "people", "days": 180}).json()["board"]["values"]["stuck_now"])


def test_a_tickets_dates_and_figures_are_read_once_with_the_board(client: TestClient) -> None:
    board = board_of(client, "REV")
    rev9: Ticket = next(ticket for ticket in board.work if ticket.issue_key == "REV-9")
    assert (rev9.summary, rev9.issue_type, rev9.status, rev9.open_rule_ids) == (
        "Sample 8", "Task", "In Progress", ("W-EFF-01", "W-AGE-01"))
    assert rev9.builders == (Credit(pid("alpha"), "assignee_now", None),)
    assert board.grace_hours == 1.0 and board.ignores.covers("REV-9", board.every_rule) is None
    rev1 = next(ticket for ticket in board.work if ticket.issue_key == "REV-1")
    assert (rev1.started_at, rev1.done_at) == (to_iso(S - MS), to_iso(S))
