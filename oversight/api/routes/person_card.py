from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from oversight.api.access_log import PERSON_CARD_ONCE_IN, PERSON_CARD_VIEW, AccessLog, need_viewer
from oversight.api.deps import LEVEL_PARAM, LEVELS, PROJECT_PARAM, ApiContext, get_ctx, people_level_only, read_conn
from oversight.api.meta import build_meta
from oversight.api.people import PERSON_ID
from oversight.api.routes.people import board_tickets, people_rows
from oversight.api.scorecard import (
    COMPARABLE,
    DEFAULT_DAYS,
    PERIODS,
    Board,
    Period,
    Standing,
    Ticket,
    board_values,
    load_board,
    person_values,
    too_few,
)
from oversight.api.timelines import Categories, key_number, ts
from oversight.ask.catalog import _rows
from oversight.derive.timeline import Interval, build_intervals
from oversight.timeutil import to_iso

router = APIRouter()

CARD_TICKETS = 3  # the tickets in progress a card lists; the rest are counted (in_progress_more)
HOLDS_TICKETS = "holds_tickets"
MEMBER_NO_TICKETS = "member_no_tickets"
IN_PROGRESS = "indeterminate"  # the status category of work in progress
_PARAMETERS = frozenset({PROJECT_PARAM, LEVEL_PARAM, "days"})
_CHUNK = 500  # keys per IN (...) list, well under SQLite's variable limit
_NOT_ON_THE_BOARD = "nobody by that id is on this board"

# The tickets in progress: what the card lists of each.
_ISSUES_SQL = "SELECT issue_key, summary, status, status_category, created_at FROM issues WHERE issue_key IN ({marks})"
_CHANGES_SQL = """
    SELECT issue_key, changelog_id, changed_at, from_status, to_status FROM status_changes
    WHERE issue_key IN ({marks})
"""
# The latest status change and the latest logged work on some tickets, up to a moment.
_LAST_CHANGE_SQL = "SELECT MAX(changed_at) AS at FROM status_changes WHERE issue_key IN ({marks}) AND changed_at <= ?"
_LAST_WORKLOG_SQL = "SELECT MAX(started_at) AS at FROM worklogs WHERE issue_key IN ({marks}) AND started_at <= ?"


@dataclass(frozen=True)
class CardQuery:
    """GET /people/{person_id}/card's parameters, checked."""

    level: str
    days: int


def _problem(name: str, message: str, kind: str = "value_error") -> dict[str, Any]:
    return {"loc": ["query", name], "msg": message, "type": kind}


def card_query(request: Request) -> CardQuery:
    """The card's parameters as the contract allows them (4.19); a 422 naming every problem otherwise."""
    query = request.query_params
    problems: list[dict[str, Any]] = []
    for name in dict.fromkeys(query.keys()):
        if name not in _PARAMETERS:
            problems.append(_problem(name, "is not a parameter of this endpoint", "unknown"))
        elif len(query.getlist(name)) > 1:
            problems.append(_problem(name, "may be given only once", "repeated"))
    level = query.get(LEVEL_PARAM, "team")
    if level not in LEVELS:
        problems.append(_problem(LEVEL_PARAM, "must be team or people"))
    days = query.get("days", str(DEFAULT_DAYS))
    if days not in {str(period) for period in PERIODS}:
        problems.append(_problem("days", f"must be {', '.join(map(str, PERIODS[:-1]))} or {PERIODS[-1]}"))
    if problems:
        raise HTTPException(422, problems)
    return CardQuery(level=level, days=int(days))


# --- the figures ----------------------------------------------------------------------------------------------


def _chunks(keys: Sequence[str]) -> list[Sequence[str]]:
    return [keys[start : start + _CHUNK] for start in range(0, len(keys), _CHUNK)]


def _marks(keys: Sequence[str]) -> str:
    return ", ".join("?" * len(keys))


def process_standing(board: Board, person: Standing, period: Period) -> dict[str, int] | None:
    """The process meter's counts (see the module docstring); None when they were compared on no measure."""
    if not person.compared:
        return None
    return {
        "closer": person.closer,
        "level": person.level,
        "further": person.further,
        "too_few": too_few(board, person, period),
        "measurable": sum(board.reason(key, period) is None for key in COMPARABLE),
    }


def timeline_of(
    issue: Mapping[str, Any] | sqlite3.Row,
    changes: Sequence[Mapping[str, Any]],
    stage_of: Callable[[str | None], str | None],
) -> list[Interval]:
    try:
        return build_intervals(issue["created_at"], issue["status"], changes, stage_of)
    except (ValueError, TypeError):
        return []


def in_progress_since(intervals: Sequence[Interval], category: Callable[[str | None, str | None], str]) -> str | None:
    """When a ticket in progress now last went into progress"""
    if not intervals or category(intervals[-1].status, intervals[-1].stage) != IN_PROGRESS:
        return None
    since = ts(intervals[-1].entered_at)
    for interval in reversed(intervals[:-1]):
        if category(interval.status, interval.stage) != IN_PROGRESS:
            break
        since = ts(interval.entered_at) or since
    return since


def in_progress_now(
    conn: sqlite3.Connection, ctx: ApiContext, tickets: Sequence[Ticket]
) -> tuple[list[dict[str, Any]], int]:
    keys = [ticket.issue_key for ticket in tickets if ticket.category == IN_PROGRESS]
    issues: dict[str, sqlite3.Row] = {}
    changes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(keys):
        issues.update((row["issue_key"], row) for row in _rows(conn, _ISSUES_SQL.format(marks=_marks(chunk)), chunk))
        for row in _rows(conn, _CHANGES_SQL.format(marks=_marks(chunk)), chunk):
            changes[row["issue_key"]].append(dict(row))
    categories = Categories(conn, ctx)
    stage_of = ctx.process.project(ctx.project).stage_of
    listed: list[dict[str, Any]] = []
    for key in keys:
        issue = issues.get(key)
        if issue is None:
            continue  # read in the same snapshot, so never gone; kept out rather than shown half known
        timeline = timeline_of(issue, changes.get(key, []), stage_of)
        listed.append({
            "key": key,
            "summary": issue["summary"] or "",
            "status": issue["status"] or "Unknown",
            "since": in_progress_since(timeline, categories.for_issue(issue)),
        })
    # Longest in progress first: the earliest since; one not known (its history ends outside progress) last.
    listed.sort(key=lambda item: (item["since"] is None, item["since"] or "", key_number(item["key"]) or 0,
                                  item["key"]))
    return listed[:CARD_TICKETS], len(listed) - len(listed[:CARD_TICKETS])


def last_activity(conn: sqlite3.Connection, keys: Sequence[str], until: str) -> str | None:
    """The latest status change or logged work (when the work was done) on these tickets, up to `until`."""
    found: list[str] = []
    for chunk in _chunks(keys):
        for sql in (_LAST_CHANGE_SQL, _LAST_WORKLOG_SQL):
            for row in _rows(conn, sql.format(marks=_marks(chunk)), (*chunk, until)):
                moment = ts(row["at"])
                if moment is not None:
                    found.append(moment)
    return max(found) if found else None


def other_boards(conn: sqlite3.Connection, ctx: ApiContext, person_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for board in ctx.served().boards:
        key = board.key
        if key == ctx.project or not board.synced:
            continue
        theirs = sum(
            ticket.work and ticket.open and ticket.person is not None and ticket.person["id"] == person_id
            for ticket in board_tickets(conn, replace(ctx, selected=key))
        )
        if theirs:
            found.append({"key": key, "name": ctx.process.project(key).name or key, "open_tickets": theirs})
    return found


def person_card(conn: sqlite3.Connection, ctx: ApiContext, person_id: str, days: int) -> dict[str, Any] | None:
    """The card's body but meta (contract 4.19); None when the person is not on the board."""
    if not PERSON_ID.fullmatch(person_id):
        return None
    board = load_board(conn, ctx)
    who = board.who(person_id)
    if who is None:
        return None
    period = board.period(days)
    values = person_values(board, person_id, period, board_values(board, period))
    standing = Standing(person=who, tickets=board.holdings.get(person_id, 0), values=values)
    row = next((row for row in people_rows(conn, ctx)
                if row["person"] is not None and row["person"]["id"] == person_id), None)
    theirs = board.tickets_of(person_id)
    listed, more = in_progress_now(conn, ctx, theirs)

    def count(key: str) -> int:
        return int(values[key]["value"] or 0)  # null only for someone who holds no tickets: they have 0

    return {
        "person": who,
        "board": {"key": ctx.project, "name": ctx.process.project(ctx.project).name or ctx.project},
        "membership": HOLDS_TICKETS if standing.tickets else MEMBER_NO_TICKETS,
        "work": {"open_tickets": count("open_tickets"), "in_progress": count("in_progress"),
                 "done_in_period": count("done_in_period"), "days": period.days},
        "findings": {"open_breaches": row["open_breaches"] if row else 0,
                     "open_warnings": row["open_warnings"] if row else 0},
        "process": process_standing(board, standing, period),
        "in_progress_now": listed,
        "in_progress_more": more,
        "other_boards": other_boards(conn, ctx, person_id),
        "last_activity_at": last_activity(conn, [ticket.issue_key for ticket in theirs], to_iso(ctx.now()) or ""),
    }


@router.get("/people/{person_id}/card")
def get_person_card(
    person_id: str,
    request: Request,
    query: CardQuery = Depends(card_query),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    # Least privilege first: a team-level request learns nothing about anyone, not even whether they are on the board.
    people_level_only(query.level, "person cards")
    viewer = need_viewer(request)
    card = person_card(conn, ctx, person_id, query.days)
    if card is None:
        raise HTTPException(404, _NOT_ON_THE_BOARD)
    body = {"meta": build_meta(conn, ctx), **card}
    # Written (or found written in the last ten minutes) before the card goes out, or nothing goes (access_log.py).
    access: AccessLog = request.app.state.access_log
    access.record(viewer=viewer, level=query.level, project=ctx.project, action=PERSON_CARD_VIEW,
                  subjects=[person_id], detail={"days": query.days}, once_within=PERSON_CARD_ONCE_IN)
    return body
