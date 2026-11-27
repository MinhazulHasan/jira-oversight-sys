"""The tickets behind one figure of the People page (contract 4.25), from the shares the figure is the size of."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from oversight.api.credit import Credit
from oversight.api.people import UNNAMED, Person
from oversight.api.scorecard import (
    MEASURES,
    TICKETS,
    Board,
    Measure,
    Share,
    Ticket,
    Value,
    board_values,
    person_values,
    tickets_share,
    whose_shares,
)
from oversight.api.timelines import CATEGORIES, key_number
from oversight.rules.checks import label
from oversight.rules.ignores import IgnoreInfo

MEASURE_KEYS: tuple[str, ...] = (*(measure.key for measure in MEASURES), TICKETS.key)
# The measures whose tickets are dated by when they were done.
DONE_BASED = frozenset(("done_in_period", "time_logged", "within_estimate", "reopened", "skipped_stage", "code_review"))
NOBODY: dict[str, Any] = {"person": None, "basis": None, "field": None}


def proof(board: Board, key: str, person_id: str | None, days: int) -> dict[str, Any]:
    """The body but meta: the figure as the scorecard shows it, its tickets and those an ignore leaves out."""
    period = board.period(days)
    people = {who["id"]: who for who in board.everyone}
    if key == TICKETS.key:
        share = tickets_share(board, person_id)
        value: Value = {"value": board.total if person_id is None else board.holdings.get(person_id, 0),
                        "numerator": None, "denominator": None, "enough": True, "vs_board": None}
    else:
        share = whose_shares(board, person_id, period)[key]
        against = board_values(board, period)
        value = against[key] if person_id is None else person_values(board, person_id, period, against)[key]
    counted = {ticket.issue_key for ticket in share.counted}
    tickets = _ordered([_row(board, share, ticket, person_id, people, counted=ticket.issue_key in counted,
                              left_out=False)
                        for ticket in share.tickets])
    left_out = _ordered([_row(board, share, ticket, person_id, people, counted=False, left_out=True)
                         for ticket in share.left_out])
    return {
        "period": {"days": period.days, "from": period.since, "to": period.until},
        "measure": board.measure_body(share.measure, period),
        "person": None if person_id is None else people.get(person_id),
        "value": value,
        "total": len(tickets),
        "counted": len(counted),
        "tickets": tickets,
        "left_out": left_out,
    }


def _row(board: Board, share: Share, ticket: Ticket, person_id: str | None, people: Mapping[str, Person], *,
         counted: bool, left_out: bool) -> dict[str, Any]:
    key = share.measure.key
    ignore = _ignore(board, share.measure, ticket.issue_key) if left_out else None
    return {
        "key": ticket.issue_key,
        "summary": ticket.summary,
        "issue_type": ticket.issue_type,
        "status": ticket.status,
        "status_category": ticket.category if ticket.category in CATEGORIES else None,
        "counted": counted,
        "credit": _credit(_credit_of(ticket, person_id), people),
        "at": ticket.done_at if key in DONE_BASED else ticket.started_at if key == "estimated_before_start" else None,
        "detail": _detail(board, ticket, key),
        "ignore": ignore.body() if ignore is not None else None,
        "url": None,
    }


def _ignore(board: Board, measure: Measure, issue_key: str) -> IgnoreInfo | None:
    """The ignore a row shows: one covering the measure's rules, or the ticket's own for a measure without rules"""
    rule_ids = board.rule_ids(measure)
    return board.ignores.covers(issue_key, rule_ids) if rule_ids else board.ignores.ticket(issue_key)


def _credit_of(ticket: Ticket, person_id: str | None) -> Credit | None:
    """Why the ticket counts for the person's figure; for the whole board's, whom it counts for (None: nobody)"""
    if person_id is None:
        return ticket.builders[0] if ticket.builders else None
    return next((credit for credit in ticket.builders if credit.person_id == person_id), None)


def _credit(credit: Credit | None, people: Mapping[str, Person]) -> dict[str, Any]:
    if credit is None:
        return dict(NOBODY)
    who = people.get(credit.person_id) or {"id": credit.person_id, "name": UNNAMED, "avatar": None}
    return {"person": who, "basis": credit.basis, "field": credit.field if credit.basis == "field" else None}


def _detail(board: Board, ticket: Ticket, key: str) -> dict[str, Any] | None:
    if key == "estimated_before_start":
        return {"started_at": ticket.started_at, "estimate_set_at": ticket.estimate_set_at,
                "grace_hours": board.grace_hours}
    if key == "time_logged":
        return {"time_spent_s": ticket.time_spent_s, "subtask_time_spent_s": ticket.subtask_time_spent_s}
    if key == "within_estimate":
        return {"logged_s": ticket.time_spent_s, "estimate_s": ticket.estimate_s, "ratio": ticket.effort_ratio,
                "limit": board.effort_limit}
    if key == "stuck_now":
        return {"status": ticket.status, "since": ticket.status_since, "hours": ticket.status_hours,
                "threshold": ticket.stuck_threshold if ticket.stuck else None}
    if key == "reopened":
        return {"times": ticket.reopen_count}
    if key == "skipped_stage":
        return {"stages": [_stage_name(stage) for stage in ticket.skipped_stages]}
    if key == "code_review":
        return {"breach": ticket.review_breach}
    if key == "open_findings_per_10":
        return {"findings": ticket.open_findings, "rule_ids": list(ticket.open_rule_ids)}
    return None


def _stage_name(stage: str) -> str:
    """A stage as the process page names it ("Code review")"""
    text = label(stage)
    return text[:1].upper() + text[1:]


def _ordered(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Counted first, then the newest `at` (none last), then by key number"""
    found = sorted(rows, key=lambda row: (key_number(row["key"]) or 0, row["key"]))
    found.sort(key=lambda row: row["at"] or "", reverse=True)
    found.sort(key=lambda row: not row["counted"])
    return found
