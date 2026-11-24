from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from oversight.api.access_log import PEOPLE_COMPARE_VIEW, SCORECARD_VIEW, AccessLog, need_viewer
from oversight.api.deps import (
    LEVELS,
    PROJECT_PARAM,
    ApiContext,
    ApiError,
    get_ctx,
    people_level_only,
    read_conn,
)
from oversight.api.meta import build_meta
from oversight.api.people import PERSON_ID, UNASSIGNED, Person, board_account, directory_key, person, sort_key
from oversight.api.scorecard import DEFAULT_DAYS, PERIODS, load_board, scorecard
from oversight.api.timelines import Categories, issue_category, load_records
from oversight.ask.catalog import _rows
from oversight.ask.people import MAX_MENTIONS, PeopleScope
from oversight.ingest.avatars import IMAGE_TYPES, sniff

router = APIRouter()

_PARAMETERS = frozenset({PROJECT_PARAM})
_SCORECARD_PARAMETERS = frozenset({PROJECT_PARAM, "level", "days", "person"})
_UNPROCESSABLE = 422
COMPARISON_OFF = "comparison_off"
# The board's tickets with their type, their assignee, the display name the last sync saw and their picture's version.
_TICKETS_SQL = """
    SELECT i.issue_key, i.issue_type, i.status, i.status_category, i.assignee_id, a.display_name, a.avatar_hash
    FROM issues AS i LEFT JOIN people AS a ON a.account_id = i.assignee_id
    WHERE i.project_key = ?
"""
_DIRECTORY_SQL = """
    SELECT b.account_id, b.tickets, a.display_name, a.avatar_hash
    FROM (
        SELECT assignee_id AS account_id,
               SUM(CASE WHEN LOWER(COALESCE(issue_type, '')) IN ({excluded}) THEN 0 ELSE 1 END) AS tickets
        FROM issues
        WHERE project_key = ? AND assignee_id IS NOT NULL AND assignee_id != ''
        GROUP BY assignee_id
        UNION ALL
        SELECT account_id, 0
        FROM board_members
        WHERE project_key = ?
          AND account_id NOT IN (SELECT assignee_id FROM issues WHERE project_key = ? AND assignee_id IS NOT NULL)
    ) AS b LEFT JOIN people AS a ON a.account_id = b.account_id
"""
_PICTURE_SQL = "SELECT avatar, avatar_type FROM people WHERE account_id = ?"
# Contract 4.13. The middleware's no-store gives way to the Cache-Control set here.
PICTURE_HEADERS = {
    "Cache-Control": "private, max-age=86400",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}


def _known_parameters(request: Request) -> None:
    """Refuse a parameter name this endpoint does not take, and a name given twice."""
    problems = []
    for name in dict.fromkeys(request.query_params.keys()):
        if name not in _PARAMETERS:
            problems.append({"loc": ["query", name], "msg": "is not a parameter of this endpoint", "type": "unknown"})
        elif len(request.query_params.getlist(name)) > 1:
            problems.append({"loc": ["query", name], "msg": "may be given only once", "type": "repeated"})
    if problems:
        raise HTTPException(_UNPROCESSABLE, problems)


@dataclass
class _Tally:
    person: Person | None  # None: the Unassigned row
    tickets: int = 0
    open_tickets: int = 0
    open_breaches: int = 0
    open_warnings: int = 0
    with_findings: set[str] = field(default_factory=set)

    def order(self) -> tuple[int, int, int, str, str]:
        return (-self.open_breaches, -self.open_warnings, -self.open_tickets, *sort_key(self.person))

    def body(self) -> dict[str, Any]:
        return {
            "person": self.person,
            "tickets": self.tickets,
            "open_tickets": self.open_tickets,
            "open_breaches": self.open_breaches,
            "open_warnings": self.open_warnings,
            "tickets_with_findings": len(self.with_findings),
        }


@dataclass(frozen=True)
class BoardTicket:
    """One of the board's tickets"""

    issue_key: str
    person: Person | None
    category: str
    work: bool = True

    @property
    def open(self) -> bool:
        """Open: the status category is not done."""
        return self.category != "done"


def excluded_types(ctx: ApiContext) -> frozenset[str]:
    """The issue types that are no one's work, folded for case (rules.yaml's defaults.exclude_types: Epic)."""
    return frozenset(name.casefold() for name in ctx.rules.defaults.exclude_types)


def board_tickets(conn: sqlite3.Connection, ctx: ApiContext) -> list[BoardTicket]:
    """Every ticket on the board, in one query, with its assignee by the person rules."""
    categories = Categories(conn, ctx)
    containers = excluded_types(ctx)
    return [
        BoardTicket(
            issue_key=ticket["issue_key"],
            person=person(ticket["assignee_id"], ticket["display_name"], ticket["avatar_hash"]),
            category=issue_category(ticket, ctx, categories),
            work=(ticket["issue_type"] or "").casefold() not in containers,
        )
        for ticket in _rows(conn, _TICKETS_SQL, (ctx.project,))
    ]


def people_rows(
    conn: sqlite3.Connection, ctx: ApiContext, tickets: Sequence[BoardTicket] | None = None
) -> list[dict[str, Any]]:
    """The list's rows for the board, in its order (the board's tickets, when the caller has read them)"""
    tallies: dict[str, _Tally] = {}
    work: set[str] = set()
    for ticket in board_tickets(conn, ctx) if tickets is None else tickets:
        if not ticket.work:
            continue  # a container, not anyone's work
        work.add(ticket.issue_key)
        tally = tallies.setdefault(ticket.person["id"] if ticket.person is not None else UNASSIGNED, _Tally(ticket.person))
        tally.tickets += 1
        tally.open_tickets += ticket.open
    # The same records, and the same person key, as /findings?person=...
    for record in load_records(conn, ctx):
        tally = tallies.get(record.person or "")  # no one's (a sprint finding, a gone issue's): no row
        if tally is None or record.state != "open" or record.issue_key not in work:
            continue
        if record.severity == "breach":
            tally.open_breaches += 1
        elif record.severity == "warning":
            tally.open_warnings += 1
        tally.with_findings.add(record.issue_key)
    return [tally.body() for tally in sorted(tallies.values(), key=_Tally.order)]


def directory_rows(conn: sqlite3.Connection, ctx: ApiContext) -> list[dict[str, Any]]:
    """The directory's entries for the board, in its order"""
    containers = sorted(excluded_types(ctx))
    sql = _DIRECTORY_SQL.replace("{excluded}", ", ".join("?" * len(containers)) or "NULL")
    project = ctx.project
    entries: list[dict[str, Any]] = []
    for row in _rows(conn, sql, (*containers, project, project, project)):
        who = person(row["account_id"], row["display_name"], row["avatar_hash"])
        if who is not None:
            entries.append({"person": who, "tickets": row["tickets"]})
    return sorted(entries, key=lambda entry: directory_key(entry["person"]))


def people_scope(conn: sqlite3.Connection, ctx: ApiContext) -> PeopleScope:
    """What Ask may know about the board's people at people level"""
    tickets = board_tickets(conn, ctx)
    rows = tuple(people_rows(conn, ctx, tickets))
    holding = {row["person"]["id"] for row in rows if row["person"] is not None}
    return PeopleScope(
        rows=rows,
        assignees={ticket.issue_key: ticket.person for ticket in tickets if ticket.person is not None},
        open_keys=frozenset(ticket.issue_key for ticket in tickets if ticket.open),
        in_progress_keys=frozenset(ticket.issue_key for ticket in tickets if ticket.category == "indeterminate"),
        members=tuple(entry["person"] for entry in directory_rows(conn, ctx)
                      if entry["person"]["id"] not in holding),
        scorecard=load_board(conn, ctx),
        comparison=ctx.settings.people_comparison == "on",
    )


@dataclass(frozen=True)
class ScorecardQuery:
    """GET /people/scorecard's parameters, checked."""

    level: str
    days: int
    people: list[str] | None  # None: no person asked for


def _problem(name: str, message: str, kind: str = "value_error") -> dict[str, Any]:
    return {"loc": ["query", name], "msg": message, "type": kind}


def scorecard_query(request: Request) -> ScorecardQuery:
    """The scorecard's parameters as the contract allows them (4.15); a 422 naming every problem otherwise."""
    query = request.query_params
    problems: list[dict[str, Any]] = []
    for name in dict.fromkeys(query.keys()):
        if name not in _SCORECARD_PARAMETERS:
            problems.append(_problem(name, "is not a parameter of this endpoint", "unknown"))
        elif name != "person" and len(query.getlist(name)) > 1:
            problems.append(_problem(name, "may be given only once", "repeated"))
    level = query.get("level", "team")
    if level not in LEVELS:
        problems.append(_problem("level", "must be team or people"))
    days = query.get("days", str(DEFAULT_DAYS))
    if days not in {str(period) for period in PERIODS}:
        problems.append(_problem("days", f"must be {', '.join(map(str, PERIODS[:-1]))} or {PERIODS[-1]}"))
    wanted = query.getlist("person")
    if len(wanted) > MAX_MENTIONS:
        problems.append(_problem("person", f"at most {MAX_MENTIONS} people can be asked for"))
    elif not all(PERSON_ID.fullmatch(value) for value in wanted):
        problems.append(_problem("person", "must be a person id: p and 12 lower-case hex characters"))
    elif len(set(wanted)) != len(wanted):
        problems.append(_problem("person", "names a person twice"))
    if problems:
        raise HTTPException(_UNPROCESSABLE, problems)
    return ScorecardQuery(level=level, days=int(days), people=wanted or None)


@router.get("/people", dependencies=[Depends(_known_parameters)])
def list_people(
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    return {"meta": build_meta(conn, ctx), "people": people_rows(conn, ctx)}


@router.get("/people/directory", dependencies=[Depends(_known_parameters)])
def people_directory(
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    return {"meta": build_meta(conn, ctx), "people": directory_rows(conn, ctx)}


@router.get("/people/scorecard")
def people_scorecard(
    request: Request,
    query: ScorecardQuery = Depends(scorecard_query),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    # Least privilege first: a team-level request learns nothing about anyone, whoever it asks for.
    people_level_only(query.level, "person scorecards")
    viewer = need_viewer(request)
    comparison = ctx.settings.people_comparison == "on"
    if not comparison and query.people is not None and len(query.people) > 1:
        raise ApiError(403, COMPARISON_OFF, "comparing people is turned off on this service (PEOPLE_COMPARISON=off)")
    board = load_board(conn, ctx)
    if query.people is not None and any(board.who(person_id) is None for person_id in query.people):
        raise HTTPException(404, "nobody by that id is on this board")
    body = {"meta": build_meta(conn, ctx), **scorecard(board, query.days, query.people, comparison=comparison)}
    if not body["people"]:
        return body
    access: AccessLog = request.app.state.access_log
    access.record(
        viewer=viewer,
        level=query.level,
        project=ctx.project,
        action=SCORECARD_VIEW if query.people is not None and len(query.people) == 1 else PEOPLE_COMPARE_VIEW,
        subjects=[row["person"]["id"] for row in body["people"]],
        detail={"days": query.days},
    )
    return body


@router.get(
    "/people/{person_id}/avatar",
    response_class=Response,
    responses={200: {"content": {kind: {} for kind in IMAGE_TYPES}, "description": "The picture's bytes"}},
)
def person_avatar(
    person_id: str,
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> Response:
    account = board_account(conn, ctx.project, person_id)
    found = _rows(conn, _PICTURE_SQL, (account,)) if account is not None else []
    if found:
        data, kind = found[0]["avatar"], found[0]["avatar_type"]
        if isinstance(data, bytes) and kind in IMAGE_TYPES and sniff(data) == kind:
            return Response(content=data, media_type=kind, headers=PICTURE_HEADERS)
    raise HTTPException(404, "no picture of that person on this board")
