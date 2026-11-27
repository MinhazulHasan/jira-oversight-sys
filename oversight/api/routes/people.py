from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from oversight.api.access_log import (
    PEOPLE_COMPARE_VIEW,
    SCORECARD_TICKETS_VIEW,
    SCORECARD_VIEW,
    AccessLog,
    Viewer,
    need_viewer,
)
from oversight.api.deps import (
    LEVELS,
    PROJECT_PARAM,
    ApiContext,
    ApiError,
    get_ctx,
    people_level_only,
    read_conn,
)
from oversight.api.credit import BoardCredit, board_credit
from oversight.api.meta import build_meta
from oversight.api.people import (
    PERSON_ID,
    UNASSIGNED,
    Person,
    board_account,
    board_everyone,
    directory_key,
    person,
    sort_key,
)
from oversight.api.people_credit import CreditQuery, credit_body, credit_json, credit_query, parse_credit, save_credit
from oversight.api.proof import MEASURE_KEYS, proof
from oversight.api.scorecard import DEFAULT_DAYS, PERIODS, load_board, scorecard
from oversight.api.timelines import Categories, issue_category, load_records
from oversight.ask.catalog import _rows
from oversight.ask.people import MAX_MENTIONS, PeopleScope
from oversight.ingest.avatars import IMAGE_TYPES, sniff

router = APIRouter()

_PARAMETERS = frozenset({PROJECT_PARAM})
_SCORECARD_PARAMETERS = frozenset({PROJECT_PARAM, "level", "days", "person"})
_PROOF_PARAMETERS = frozenset({PROJECT_PARAM, "level", "days", "measure", "person"})
_UNPROCESSABLE = 422
COMPARISON_OFF = "comparison_off"
NOT_MEASURABLE = "not_measurable"
# The board's tickets with their type, their assignee, the display name the last sync saw and their picture's version.
_TICKETS_SQL = """
    SELECT i.issue_key, i.issue_type, i.status, i.status_category, i.assignee_id, a.display_name, a.avatar_hash
    FROM issues AS i LEFT JOIN people AS a ON a.account_id = i.assignee_id
    WHERE i.project_key = ?
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


def credited(ticket: BoardTicket, credit: BoardCredit) -> dict[str, Person]:
    """Whom a ticket counts for under the board's credit setting, each once"""
    return {one.person_id: credit.people[one.person_id] for one in credit.of(ticket.issue_key)}


def people_rows(
    conn: sqlite3.Connection, ctx: ApiContext, tickets: Sequence[BoardTicket] | None = None,
    credit: BoardCredit | None = None, *, findings_by_credit: bool = False,
) -> list[dict[str, Any]]:
    """The list's rows, in its order: tickets by the credit setting, open findings by the assignee or by credit too"""
    credit = credit if credit is not None else board_credit(conn, ctx.process, ctx.fields, ctx.project)
    tallies: dict[str, _Tally] = {}
    work: dict[str, tuple[str, ...]] = {}  # each work ticket -> whom it counts for (UNASSIGNED: no one)
    for ticket in board_tickets(conn, ctx) if tickets is None else tickets:
        if not ticket.work:
            continue  # a container, not anyone's work
        whom = credited(ticket, credit)
        work[ticket.issue_key] = tuple(whom) or (UNASSIGNED,)
        for key in work[ticket.issue_key]:
            tally = tallies.setdefault(key, _Tally(whom.get(key)))
            tally.tickets += 1
            tally.open_tickets += ticket.open
    # By /findings' person key (the assignee) unless asked otherwise, so a row clicks through to Findings exactly.
    for record in load_records(conn, ctx):
        if record.state != "open" or record.person is None or record.issue_key not in work:
            continue  # no one's (a sprint finding, a gone issue's), or not the board's work
        for key in work[record.issue_key] if findings_by_credit else (record.person,):
            tally = tallies.setdefault(key, _Tally(record.assignee))
            if record.severity == "breach":
                tally.open_breaches += 1
            elif record.severity == "warning":
                tally.open_warnings += 1
            tally.with_findings.add(record.issue_key)
    return [tally.body() for tally in sorted(tallies.values(), key=_Tally.order)]


def directory_rows(
    conn: sqlite3.Connection, ctx: ApiContext, tickets: Sequence[BoardTicket] | None = None,
    credit: BoardCredit | None = None,
) -> list[dict[str, Any]]:
    """The directory's entries for the board, in its order: everyone with tickets, then anyone else on it with 0"""
    credit = credit if credit is not None else board_credit(conn, ctx.process, ctx.fields, ctx.project)
    entries: dict[str, dict[str, Any]] = {}
    for ticket in board_tickets(conn, ctx) if tickets is None else tickets:
        if ticket.work:
            for person_id, who in credited(ticket, credit).items():
                entries.setdefault(person_id, {"person": who, "tickets": 0})["tickets"] += 1
    # A member, or someone assigned only work that counts for others (or only Epics).
    for who in board_everyone(conn, ctx.project):
        entries.setdefault(who["id"], {"person": who, "tickets": 0})
    return sorted(entries.values(), key=lambda entry: directory_key(entry["person"]))


def people_scope(conn: sqlite3.Connection, ctx: ApiContext) -> PeopleScope:
    """What Ask may know about the board's people at people level"""
    credit = board_credit(conn, ctx.process, ctx.fields, ctx.project)
    tickets = board_tickets(conn, ctx)
    rows = tuple(people_rows(conn, ctx, tickets, credit, findings_by_credit=True))
    holding = {row["person"]["id"] for row in rows if row["person"] is not None}
    return PeopleScope(
        rows=rows,
        assignees={ticket.issue_key: ticket.person for ticket in tickets if ticket.person is not None},
        open_keys=frozenset(ticket.issue_key for ticket in tickets if ticket.open),
        in_progress_keys=frozenset(ticket.issue_key for ticket in tickets if ticket.category == "indeterminate"),
        members=tuple(entry["person"] for entry in directory_rows(conn, ctx, tickets, credit)
                      if entry["person"]["id"] not in holding),
        scorecard=load_board(conn, ctx, credit),
        comparison=ctx.settings.people_comparison == "on",
        builders={ticket.issue_key: tuple(one.person_id for one in credit.of(ticket.issue_key))
                  for ticket in tickets if ticket.work and credit.of(ticket.issue_key)},
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


@dataclass(frozen=True)
class ProofQuery:
    """GET /people/scorecard/tickets's parameters, checked."""

    level: str
    days: int
    measure: str
    person: str | None  # None: the whole board


def proof_query(request: Request) -> ProofQuery:
    """The proof's parameters as the contract allows them (4.25); a 422 naming every problem otherwise."""
    query = request.query_params
    problems: list[dict[str, Any]] = []
    for name in dict.fromkeys(query.keys()):
        if name not in _PROOF_PARAMETERS:
            problems.append(_problem(name, "is not a parameter of this endpoint", "unknown"))
        elif len(query.getlist(name)) > 1:
            problems.append(_problem(name, "may be given only once", "repeated"))
    level = query.get("level", "team")
    if level not in LEVELS:
        problems.append(_problem("level", "must be team or people"))
    days = query.get("days", str(DEFAULT_DAYS))
    if days not in {str(period) for period in PERIODS}:
        problems.append(_problem("days", f"must be {', '.join(map(str, PERIODS[:-1]))} or {PERIODS[-1]}"))
    measure = query.get("measure")
    if measure is None:
        problems.append(_problem("measure", "is required", "missing"))
    elif measure not in MEASURE_KEYS:
        problems.append(_problem("measure", f"must be one of {', '.join(MEASURE_KEYS)}"))
    who = query.get("person")
    if who is not None and not PERSON_ID.fullmatch(who):
        problems.append(_problem("person", "must be a person id: p and 12 lower-case hex characters"))
    if problems or measure is None:
        raise HTTPException(_UNPROCESSABLE, problems)
    return ProofQuery(level=level, days=int(days), measure=measure, person=who)


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


@router.get("/people/scorecard/tickets", response_class=JSONResponse)
def people_scorecard_tickets(
    request: Request,
    query: ProofQuery = Depends(proof_query),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> JSONResponse:
    # Least privilege first, as the scorecard: a team-level request learns nothing about anyone.
    people_level_only(query.level, "person scorecards")
    viewer = need_viewer(request)
    board = load_board(conn, ctx)
    if query.person is not None and board.who(query.person) is None:
        raise HTTPException(404, "nobody by that id is on this board")
    reason = board.reason(query.measure, board.period(query.days))
    if reason is not None:
        raise ApiError(409, NOT_MEASURABLE, reason)
    body = {"meta": build_meta(conn, ctx), **proof(board, query.measure, query.person, query.days)}
    access: AccessLog = request.app.state.access_log
    access.record(
        viewer=viewer,
        level=query.level,
        project=ctx.project,
        action=SCORECARD_TICKETS_VIEW,
        subjects=[query.person] if query.person is not None else [],
        detail={"days": query.days, "measure": query.measure},
    )
    # A proof can list thousands of tickets: sent as built (plain JSON already), without a second encoding pass.
    return JSONResponse(body)


@router.get("/people/credit")
def get_people_credit(
    request: Request,
    query: CreditQuery = Depends(credit_query),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    need_viewer(request)
    return credit_body(conn, ctx, query.days)


@router.put("/people/credit")
def put_people_credit(
    request: Request,
    query: CreditQuery = Depends(credit_query),
    viewer: Viewer = Depends(need_viewer),
    raw: bytes = Depends(credit_json),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    save_credit(conn, ctx, parse_credit(raw), viewer)
    return credit_body(conn, ctx, query.days)


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
    if account is None and PERSON_ID.fullmatch(person_id):
        # Someone the board's tickets count for, though they hold none of them now.
        account = board_credit(conn, ctx.process, ctx.fields, ctx.project).accounts.get(person_id)
    found = _rows(conn, _PICTURE_SQL, (account,)) if account is not None else []
    if found:
        data, kind = found[0]["avatar"], found[0]["avatar_type"]
        if isinstance(data, bytes) and kind in IMAGE_TYPES and sniff(data) == kind:
            return Response(content=data, media_type=kind, headers=PICTURE_HEADERS)
    raise HTTPException(404, "no picture of that person on this board")
