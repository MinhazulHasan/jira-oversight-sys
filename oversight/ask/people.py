"""Ask at people level: who holds the work and how it follows the process, as facts."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from oversight.api.people import PERSON_ID, UNASSIGNED_LABEL, UNNAMED, Person, directory_key, fold
from oversight.api.scorecard import (
    COMPARABLE,
    DEFAULT_DAYS,
    NOW_COUNTS,
    ORDERS,
    PERIODS,
    RANKED_COUNTS,
    Board,
    effective_order,
)
from oversight.ask.catalog import (
    _EPIC_OF_I,
    _FINDING_COLUMNS,
    _FINDINGS_SQL,
    _PARENT_JOIN,
    CATALOG,
    RULE_ID_PATTERN,
    CatalogContext,
    Column,
    Facts,
    QuerySpec,
    _as_of,
    _cap,
    _day,
    _facts,
    _findings_evidence,
    _project_of,
    _ProjectParams,
    _row,
    _rows,
    _rule_title,
)
from oversight.ask.prompts import COMPARISON_OFF
from oversight.ask.scorecards import SCORECARD_COLUMNS, ComparisonOff, compare_rows, scorecard_rows

Level = Literal["team", "people"]
LEVELS: tuple[str, ...] = ("team", "people")
MAX_MENTIONS = 10
PERSON_ID_PATTERN = r"^p[0-9a-f]{12}$"
# GET /people's figures, as the people view and oversight_people name them.
FIGURES: tuple[str, ...] = ("tickets", "open_tickets", "open_breaches", "open_warnings", "tickets_with_findings")
PEOPLE_COLUMNS: tuple[str, ...] = ("person_id", "name", *FIGURES)


def mention_problem(mentions: Sequence[object]) -> str | None:
    """Why these mentions cannot be asked about, in plain words; None when they can."""
    if len(mentions) > MAX_MENTIONS:
        return f"at most {MAX_MENTIONS} people can be mentioned"
    if not all(isinstance(mention, str) and PERSON_ID.fullmatch(mention) for mention in mentions):
        return "a mention must be a person id: p and 12 lower-case hex characters"
    if len(set(mentions)) != len(mentions):
        return "a person is mentioned twice"
    return None


@dataclass(frozen=True)
class PeopleScope:
    """The people of one board as Ask may know them at people level."""

    rows: tuple[Mapping[str, Any], ...]
    assignees: Mapping[str, Person]
    open_keys: frozenset[str]
    in_progress_keys: frozenset[str] = frozenset()
    members: tuple[Person, ...] = ()
    scorecard: Board | None = None
    comparison: bool = True

    @property
    def people(self) -> list[Person]:
        """Everyone assigned a ticket on the board now."""
        return [row["person"] for row in self.rows if row["person"] is not None]

    def person(self, person_id: str) -> Person | None:
        """The person with this id, when they hold a ticket on the board now."""
        return next((who for who in self.people if who["id"] == person_id), None)

    def member(self, person_id: str) -> Person | None:
        """The person with this id, when they are a member of the board who holds none of its tickets."""
        return next((who for who in self.members if who["id"] == person_id), None)

    def tickets_of(self, person_id: str) -> set[str]:
        """The keys of the tickets the person holds now."""
        return {key for key, who in self.assignees.items() if who["id"] == person_id}


def people_figures(scope: PeopleScope) -> list[dict[str, Any]]:
    """The people view's rows in GET /people's order; the Unassigned row as NULL and "Unassigned"."""
    return [
        {
            "person_id": row["person"]["id"] if row["person"] is not None else None,
            "name": row["person"]["name"] if row["person"] is not None else UNASSIGNED_LABEL,
            **{figure: row[figure] for figure in FIGURES},
        }
        for row in scope.rows
    ]


def mentions_payload(people: Iterable[Person]) -> list[dict[str, str]]:
    """The mentioned people as the model is told them: name and person id, nothing more."""
    return [{"id": who["id"], "name": who["name"]} for who in people]


def no_tickets_sentence(names: Sequence[str]) -> str:
    """The answer for mentioned people who hold no ticket on the board, word for word."""
    if not names:
        raise ValueError("no one to say it of")
    if len(names) == 1:
        return f"{names[0]} has no tickets on this board."
    return f"{', '.join(names[:-1])} and {names[-1]} have no tickets on this board."


# --- the person tools ------------------------------------------------------------------------

PersonId = Annotated[str, StringConstraints(pattern=PERSON_ID_PATTERN)]


Order = Literal["most", "fewest"]


class PeopleParams(_ProjectParams):
    rank_by: Literal["open_breaches", "open_warnings", "open_findings", "open_tickets", "tickets",
                     "tickets_with_findings"] = Field(
        default="open_breaches",
        description="The figure to rank people by, highest first. open_findings: open breaches and open warnings "
        "together (a question about findings), shown as its own column.",
    )
    order: Order | None = Field(
        default=None,
        description="most (the default): highest first. fewest: lowest first, for a question about who has the "
        "least. Either way only people are ranked: the tickets nobody holds come after, as one row.",
    )
    person_ids: list[PersonId] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MENTIONS,
        description="Only these people's rows, by person id (the mentioned people's ids). Leave it out for everyone.",
    )
    n: int = Field(default=10, ge=1, le=50)


class _PersonParams(_ProjectParams):
    person: str = Field(
        pattern=PERSON_ID_PATTERN,
        description="The person's id, p and 12 hex characters: a mentioned person's, or a person_id a query returned.",
    )


class PersonTicketsParams(_PersonParams):
    state: Literal["open", "in_progress", "done", "all"] = Field(
        default="open",
        description="open: the status category is not done (their backlog). in_progress: it is in progress (what the "
        "person is working on). done: done within the last `days` (what they finished). all: every ticket they hold. "
        "Each leaves Epics out, as the scorecard counts them.",
    )
    days: Literal[30, 90, 180] | None = Field(
        default=None, description="For done only: the last 30, 90 (the default) or 180 days to the snapshot."
    )
    n: int = Field(default=20, ge=1, le=200)


class BoardPeopleParams(_ProjectParams):
    holding: Literal["any", "none"] = Field(
        default="any",
        description="any: everyone on the board, those assigned one of its tickets now and then its members who hold "
        "none. none: only the members who hold none of its tickets.",
    )


class TicketAssigneeParams(_ProjectParams):
    issue_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,19}-[1-9]\d{0,8}$", description="The ticket, e.g. AIL-552.")


class PersonFindingsParams(_PersonParams):
    rule_id: str | None = Field(default=None, pattern=RULE_ID_PATTERN, description="A rule id such as R-EST-01.")
    severity: Literal["breach", "warning"] | None = None
    state: Literal["open", "resolved", "all"] = "open"
    n: int = Field(default=20, ge=1, le=50)


_PEOPLE_COLUMNS = [
    Column("person_id", "Person id", "text"),
    Column("name", "Person", "text"),
    Column("tickets", "Tickets", "count"),
    Column("open_tickets", "Open tickets", "count"),
    Column("open_breaches", "Open breaches", "count"),
    Column("open_warnings", "Open warnings", "count"),
    Column("tickets_with_findings", "Tickets with findings", "count"),
]
_ASSIGNEE_COLUMNS = [Column("assignee_id", "Person id", "text"), Column("assignee", "Assignee", "text")]
_TICKET_COLUMNS = [
    Column("issue_key", "Issue", "issue_key"),
    Column("summary", "Summary", "text"),
    Column("issue_type", "Type", "text"),
    Column("status", "Status", "text"),
    Column("epic_key", "Epic", "issue_key"),
    *_ASSIGNEE_COLUMNS,
]
_RANKED_BY = {
    "open_breaches": "open breaches",
    "open_warnings": "open warnings",
    "open_findings": "open findings (open breaches and open warnings together)",
    "open_tickets": "open tickets",
    "tickets": "tickets",
    "tickets_with_findings": "tickets with findings",
}
_FOLLOW = (
    "findings follow the ticket's current assignee: a person's findings are those on the tickets they hold now, "
    "and a sprint finding is no one's"
)
# The board's tickets, most recently updated first; a subtask's epic is its parent's.
_BOARD_TICKETS_SQL = f"""
    SELECT i.issue_key, i.summary, i.issue_type, i.status, {_EPIC_OF_I} AS epic_key
    FROM issues AS i {_PARENT_JOIN}
    WHERE i.project_key = ?
    ORDER BY i.updated_at DESC, i.issue_key
"""


def _scope(ctx: CatalogContext) -> PeopleScope:
    if ctx.people is None:
        raise ValueError("the person tools answer at people level only")
    scope: PeopleScope = ctx.people
    return scope


def run_people(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, PeopleParams)
    project = _project_of(params, ctx)
    figures = people_figures(_scope(ctx))
    columns = _PEOPLE_COLUMNS
    if params.rank_by == "open_findings":
        # Findings are breaches and warnings together: their sum is the figure ranked by, a column of its own.
        figures = [{**row, "open_findings": row["open_breaches"] + row["open_warnings"]} for row in figures]
        columns = [*_PEOPLE_COLUMNS, Column("open_findings", "Open findings", "count")]
    fewest = params.order == "fewest"
    # A "who" ranking ranks people only: the tickets nobody holds follow it in a sentence of their own.
    people = [row for row in figures if row["person_id"] is not None]
    nobody = [row for row in figures if row["person_id"] is None]
    if params.person_ids is not None:
        wanted = set(params.person_ids)
        people, nobody = [row for row in people if row["person_id"] in wanted], []
    # Ties in the directory's order, as the comparison orders them, so one count gives one order everywhere.
    people.sort(key=lambda row: (row[params.rank_by] if fewest else -row[params.rank_by], fold(row["name"]),
                                 row["person_id"]))
    rows, truncated = _cap([_row(**row) for row in people], min(params.n, ctx.row_limit))
    rows += [_row(**row) for row in nobody]
    if params.person_ids is not None:
        whose = "one row for each person asked about, by person id, who is assigned a ticket on this board now"
    else:
        whose = "one row per person assigned a ticket on this board now"
    ranked = f"by {_RANKED_BY[params.rank_by]}, {'fewest' if fewest else 'most'} first, ties by name"
    after = ("; then one row for the tickets nobody holds (Unassigned), which is no one's and never ranked"
             if nobody else "")
    note = (f"{whose}, {ranked}{after}; a person's tickets are the board's work, Epics left out, as the scorecard "
            f"counts them; {_FOLLOW}")
    return _facts("oversight_people", params, columns, rows, truncated=truncated,
                  as_of=_as_of(conn, ctx, project), note=note)


# What each state of oversight_person_tickets reads, as its note says it.
_TICKETS_IN = {
    "open": "open tickets (status not done, Epics left out, as the scorecard counts them)",
    "in_progress": "tickets in progress (status in progress, Epics left out)",
    "done": "tickets done in {period} (Epics left out, as the scorecard counts them)",
    "all": "tickets (any status, Epics left out, as GET /people counts them)",
}


def run_person_tickets(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, PersonTicketsParams)
    scope = _scope(ctx)
    project = _project_of(params, ctx)
    who = scope.person(params.person)
    keys = scope.tickets_of(params.person)
    what = _TICKETS_IN[params.state]
    if params.state == "open":
        keys &= scope.open_keys
    elif params.state == "in_progress":
        keys &= scope.in_progress_keys
    elif params.state == "done":
        # Done in the period as the scorecard's "Done in the period" counts it: the same board, the same period.
        board = _board(scope)
        period = board.period(params.days or DEFAULT_DAYS)
        keys &= {ticket.issue_key for ticket in board.tickets_of(params.person) if period.holds(ticket.done_at)}
        what = what.format(period=f"the last {period.days} days ({_day(period.since)} to {_day(period.until)})")
    # The facts say the period only where there is one: an open backlog, or the work in progress, is now.
    params = params.model_copy(update={"days": (params.days or DEFAULT_DAYS) if params.state == "done" else None})
    found = [row for row in _rows(conn, _BOARD_TICKETS_SQL, (project,)) if row["issue_key"] in keys] if who else []
    # An Epic holds work rather than being it, so it is left out, as GET /people and the scorecards count.
    containers = {name.casefold() for name in ctx.rules.defaults.exclude_types}
    found = [row for row in found if (row["issue_type"] or "").casefold() not in containers]
    fetched, truncated = _cap(found, min(params.n, ctx.row_limit))
    rows = [
        _row(issue_key=r["issue_key"], summary=r["summary"], issue_type=r["issue_type"], status=r["status"],
             epic_key=r["epic_key"], assignee_id=who["id"], assignee=who["name"])
        for r in fetched
        if who is not None
    ]
    note = f"{what} assigned to this person now, most recently updated first; {len(found)} in all" if who else (
        "no ticket on this board is assigned to that person id now")
    return _facts("oversight_person_tickets", params, _TICKET_COLUMNS, rows, truncated=truncated,
                  as_of=_as_of(conn, ctx, project), note=note)


_BOARD_PEOPLE_COLUMNS = [
    Column("person_id", "Person id", "text"),
    Column("name", "Person", "text"),
    Column("tickets", "Tickets", "count"),
]


def run_board_people(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    """Everyone on the board as the @ list shows them, or only the members who hold no ticket."""
    assert isinstance(params, BoardPeopleParams)
    scope = _scope(ctx)
    project = _project_of(params, ctx)
    held = {row["person"]["id"]: row["tickets"] for row in scope.rows if row["person"] is not None}
    holders = sorted(scope.people, key=directory_key)
    members = sorted(scope.members, key=directory_key)
    chosen = members if params.holding == "none" else [*holders, *members]
    found = [_row(person_id=who["id"], name=who["name"], tickets=held.get(who["id"], 0)) for who in chosen]
    rows, truncated = _cap(found, ctx.row_limit)
    if params.holding == "none":
        note = (f"the members of this board (its last member fetch) who hold none of its tickets now, by name; "
                f"{len(members)} in all")
    else:
        note = (f"everyone on this board, as the @ list shows them: the {len(holders)} "
                f"{'person' if len(holders) == 1 else 'people'} assigned its tickets now (tickets: the ones they hold, "
                f"any status, Epics left out), then the {len(members)} "
                f"{'member who holds' if len(members) == 1 else 'members who hold'} none, each by name; "
                f"{len(holders) + len(members)} in all")
    return _facts("oversight_board_people", params, _BOARD_PEOPLE_COLUMNS, rows, truncated=truncated,
                  as_of=_as_of(conn, ctx, project), note=note)


def run_ticket_assignee(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    """One ticket and who holds it now: its assignee by name (and person id), or nobody."""
    assert isinstance(params, TicketAssigneeParams)
    scope = _scope(ctx)
    project = _project_of(params, ctx)
    found = [row for row in _rows(conn, _BOARD_TICKETS_SQL, (project,)) if row["issue_key"] == params.issue_key]
    who = scope.assignees.get(params.issue_key)
    rows = [
        _row(issue_key=r["issue_key"], summary=r["summary"], issue_type=r["issue_type"], status=r["status"],
             epic_key=r["epic_key"], assignee_id=who["id"] if who else None,
             assignee=who["name"] if who else UNASSIGNED_LABEL)
        for r in found
    ]
    if not found:
        note = f"no ticket {params.issue_key} on this board"
    elif who is None:
        note = "the ticket and who holds it now: nobody is assigned it"
    else:
        note = "the ticket and who holds it now (its current assignee)"
    return _facts("oversight_ticket_assignee", params, _TICKET_COLUMNS, rows, truncated=False,
                  as_of=_as_of(conn, ctx, project), note=note)


def run_person_findings(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, PersonFindingsParams)
    scope = _scope(ctx)
    project = _project_of(params, ctx)
    who = scope.person(params.person)
    keys = scope.tickets_of(params.person)
    sql = _FINDINGS_SQL
    values: list[Any] = [project]
    described = [params.state + " findings"]
    if params.rule_id is not None:
        sql += " AND f.rule_id = ?"
        values.append(params.rule_id)
        described.append(f"rule {params.rule_id} ({_rule_title(ctx.rules, params.rule_id)})")
    if params.severity is not None:
        sql += " AND f.severity = ?"
        values.append(params.severity)
    if params.state == "open":
        sql += " AND f.resolved_at IS NULL"
    elif params.state == "resolved":
        sql += " AND f.resolved_at IS NOT NULL"
    sql += " ORDER BY (f.resolved_at IS NULL) DESC, f.first_seen_at DESC, f.finding_id"
    # On the tickets the person holds now: a sprint finding, or one whose issue has left the snapshot, is no one's.
    found = [row for row in _rows(conn, sql, values) if row["issue_key"] in keys] if who else []
    fetched, truncated = _cap(found, min(params.n, ctx.row_limit))
    rows = [
        _row(
            finding_id=r["finding_id"],
            rule_id=r["rule_id"],
            rule_title=_rule_title(ctx.rules, r["rule_id"]),
            severity=r["severity"],
            issue_key=r["issue_key"],
            summary=r["summary"],
            epic_key=r["epic_key"],
            message=r["message"],
            first_seen_at=r["first_seen_at"],
            resolved_at=r["resolved_at"],
            assignee_id=who["id"],
            assignee=who["name"],
        )
        for r in fetched
        if who is not None
    ]
    note = (f"{', '.join(described)} on the tickets this person holds now, newest first; {len(found)} in all; {_FOLLOW}"
            if who else "no ticket on this board is assigned to that person id now")
    return _facts("oversight_person_findings", params, [*_FINDING_COLUMNS, *_ASSIGNEE_COLUMNS], rows,
                  truncated=truncated, as_of=_as_of(conn, ctx, project), note=note)


# --- the scorecard tools ----------------------------------------------------------------------

Days = Literal[30, 90, 180]
CompareMeasure = Literal[
    "overall", "estimated_before_start", "time_logged", "within_estimate", "stuck_now", "reopened", "skipped_stage",
    "code_review", "open_findings_per_10", "open_tickets", "in_progress", "done_in_period",
]
CompareOrder = Literal["closer", "most", "fewest"]
assert set(Days.__args__) == set(PERIODS)  # type: ignore[attr-defined]
assert set(CompareMeasure.__args__) == {"overall", *COMPARABLE, *RANKED_COUNTS}  # type: ignore[attr-defined]
assert CompareOrder.__args__ == ORDERS  # type: ignore[attr-defined]


class ScorecardParams(_ProjectParams):
    person_ids: list[PersonId] = Field(
        min_length=1,
        max_length=MAX_MENTIONS,
        description="The people, by person id (the mentioned people's ids): one, or up to ten side by side.",
    )
    days: Days = Field(default=90, description="The period: the last 30, 90 or 180 days to the snapshot.")

    @field_validator("person_ids")
    @classmethod
    def _once_each(cls, ids: list[str]) -> list[str]:
        if len(set(ids)) != len(ids):
            raise ValueError("a person is named twice")
        return ids


class CompareParams(_ProjectParams):
    measure: CompareMeasure = Field(
        default="overall",
        description="The measure to order everyone by: a rate or the ratio (open_findings_per_10); overall: by how "
        "many measures each person is closer to the process than the board on; open_tickets, in_progress or "
        "done_in_period: by that count (whose backlog is biggest, who has the most in progress, who finished the most "
        "work).",
    )
    days: Days | None = Field(
        default=None,
        description="The period: the last 30, 90 (the default) or 180 days to the snapshot. open_tickets and "
        "in_progress are counts of now and take none.",
    )
    order: CompareOrder = Field(
        default="closer",
        description="The order the question asks for, for every measure. most: the figure highest first, for who has "
        "the most, the highest, the largest, the biggest or the top N. fewest: lowest first, for who has the fewest, "
        "the least, the lowest, the smallest or the bottom N. closer (the default): when the question has no such "
        "word (rank everyone, who follows the process best), closer to the process first; a count has no closer "
        "side, so it goes most first. A measure named the other way round takes the other order: who overruns, goes "
        "over the estimate or has the highest effort ratio the most is within_estimate fewest; the most work without "
        "an estimate, or not logging time, is that measure fewest. A question about the other side of the process "
        "(worst, struggling, needs help, falling behind, furthest from it) puts those further from it first: overall "
        "fewest; a measure where lower is closer, most; one where higher is closer, fewest; and the least of that "
        "(who needs the least help) is closer. People with too few tickets come last whatever the order.",
    )
    n: int | None = Field(default=None, ge=1, le=50,
                          description="At most this many people (\"top 5\", \"bottom 3\"); else everyone.")
    person_ids: list[PersonId] | None = Field(
        default=None,
        min_length=2,
        max_length=MAX_MENTIONS,
        description="Only these people (the mentioned people's ids, two to ten), set side by side by the measure, "
        "then the whole board; leave it out for everyone.",
    )

    @field_validator("person_ids")
    @classmethod
    def _each_once(cls, ids: list[str] | None) -> list[str] | None:
        if ids is not None and len(set(ids)) != len(ids):
            raise ValueError("a person is named twice")
        return ids

    @field_validator("order", mode="before")
    @classmethod
    def _none_is_the_default(cls, order: object) -> object:
        # A model may send null for a parameter it leaves at its default: closer, as if it had left it out.
        return "closer" if order is None else order


def _board(scope: PeopleScope) -> Board:
    if scope.scorecard is None:
        raise ValueError("the scorecards need the board read for them")
    return scope.scorecard


def run_person_scorecard(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, ScorecardParams)
    scope = _scope(ctx)
    if len(params.person_ids) > 1 and not scope.comparison:
        raise ComparisonOff(COMPARISON_OFF)
    project = _project_of(params, ctx)
    rows, note = scorecard_rows(_board(scope), params.person_ids, params.days)
    fetched, truncated = _cap(rows, ctx.row_limit)
    return _facts("oversight_person_scorecard", params, SCORECARD_COLUMNS, fetched, truncated=truncated,
                  as_of=_as_of(conn, ctx, project), note=note)


def run_people_compare(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, CompareParams)
    scope = _scope(ctx)
    if not scope.comparison:
        raise ComparisonOff(COMPARISON_OFF)
    project = _project_of(params, ctx)
    days = params.days or DEFAULT_DAYS
    only = frozenset(params.person_ids) if params.person_ids is not None else None
    # The order used (a count has no closer side, so "closer" is most first there), which the facts record.
    first = effective_order(params.measure, params.order)
    columns, rows, note = compare_rows(_board(scope), params.measure, days, first=first, only=only)
    # The whole board's row is always kept: everyone is set beside it.
    people, board = [row for row in rows if row.get("person_id")], [row for row in rows if not row.get("person_id")]
    fetched, truncated = _cap(people, min(params.n or ctx.row_limit, ctx.row_limit))
    # The facts state the period a figure is over (none for a count of now) and the order everyone is in.
    shown = params.model_copy(update={"days": None if params.measure in NOW_COUNTS else days, "order": first})
    return _facts("oversight_people_compare", shown, columns, [*fetched, *board], truncated=truncated,
                  as_of=_as_of(conn, ctx, project), note=note)


PEOPLE_CATALOG: dict[str, QuerySpec] = {
    spec.name: spec
    for spec in (
        QuerySpec(
            "oversight_people",
            "Everyone assigned a ticket on the board now, with their tickets, open tickets (Epics left out), open "
            "breaches, open warnings and tickets with findings, ranked by one of those figures, most first or fewest "
            "first, then one unranked row for the tickets nobody holds; or, with person_ids, only those people (the "
            "mentioned people, compared side by side). Findings follow the ticket's current assignee.",
            PeopleParams,
            run_people,
        ),
        QuerySpec(
            "oversight_board_people",
            "Everyone on the board, by name: the people assigned one of its tickets now (with how many they hold), "
            "then its members who hold none; or only those members. Use it for who is on the board, its members, and "
            "who has no tickets.",
            BoardPeopleParams,
            run_board_people,
        ),
        QuerySpec(
            "oversight_person_tickets",
            "One person's tickets on the board now, by person id: the open ones (their backlog), the ones in progress "
            "(what they are working on), the ones done in the last 30, 90 or 180 days (what they finished), or all of "
            "them.",
            PersonTicketsParams,
            run_person_tickets,
        ),
        QuerySpec(
            "oversight_ticket_assignee",
            "One ticket, by issue key, and who holds it now (its current assignee), or that nobody does.",
            TicketAssigneeParams,
            run_ticket_assignee,
        ),
        QuerySpec(
            "oversight_person_findings",
            "The breaches and warnings on the tickets one person holds now, by person id, filtered by rule, "
            "severity and state.",
            PersonFindingsParams,
            run_person_findings,
            _findings_evidence,
        ),
        QuerySpec(
            "oversight_person_scorecard",
            "How closely each person's tickets follow the process, next to the whole board, over the last 30, 90 or "
            "180 days: estimated before work started, time logged, done within the effort limit, stuck, reopened, "
            "skipped a stage, code review, open findings per 10 open tickets, and their open, in-progress and done "
            "counts. One to ten people by person id; use it whenever a question asks how someone is doing.",
            ScorecardParams,
            run_person_scorecard,
        ),
        QuerySpec(
            "oversight_people_compare",
            "Everyone who holds a ticket, in the order the question asks: by one scorecard measure, overall (how many "
            "measures each is closer to the process than the board on), or a count (open tickets, in progress now, "
            "done in the period). order most: the figure highest first (who has the most, the highest, the top N); "
            "fewest: lowest first (who has the fewest, the least, the lowest, the bottom N); closer, the default, when "
            "the question names no direction: closer to the process first (a count, most first). A measure named the "
            "other way round takes the other order (who overruns the most: within_estimate fewest), and a question "
            "about the other side of the process (worst, struggling, needs help) puts those further from it first. "
            "People with too few tickets come last whatever the order, and the whole board's figure is beside them.",
            CompareParams,
            run_people_compare,
        ),
    )
}
# The comparison is offered only where people may be compared (PEOPLE_COMPARISON on).
COMPARE_TOOL = "oversight_people_compare"


def specs_for(level: str, *, comparison: bool = True) -> dict[str, QuerySpec]:
    """The catalogue a question at this level may use; person tools only at people level."""
    if level != "people":
        return dict(CATALOG)
    people = {name: spec for name, spec in PEOPLE_CATALOG.items() if comparison or name != COMPARE_TOOL}
    return {**CATALOG, **people}


def people_catalog_schema(*, comparison: bool = True) -> list[dict[str, Any]]:
    """[{name, description, input_schema}] for the person tools, as catalog_schema() gives the others."""
    return [
        {"name": spec.name, "description": spec.description, "input_schema": spec.params_model.model_json_schema()}
        for spec in PEOPLE_CATALOG.values()
        if comparison or spec.name != COMPARE_TOOL
    ]


# --- the names in an answer ------------------------------------------------------------------

# A person id written into an answer: people are named, never numbered.
_PERSON_ID_IN_TEXT = re.compile(r"(?<![0-9A-Za-z])p[0-9a-f]{12}(?![0-9A-Za-z])")
# Between the facts' texts, so no name is found across two of them.
_APART = "\x00"


@dataclass(frozen=True)
class NameCheck:
    problems: list[str]
    people: list[Person]  # the people the answer names, verified, in order of first appearance
    masked: str  # the answer with every board name blanked, for the number check

    @property
    def ok(self) -> bool:
        return not self.problems


def name_pattern(name: str) -> re.Pattern[str]:
    """A display name as whole words, case and all, with any run of spaces between its words."""
    words = name.split()
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)")


def check_names(
    text: str, facts: Sequence[Facts], scope: PeopleScope, mentions: Sequence[Person], also: Sequence[Person] = ()
) -> NameCheck:
    """Every board person's name in the answer must be in its facts or among the mentions."""
    board: dict[str, list[Person]] = {}
    for who in [*scope.people, *also]:
        key = " ".join(who["name"].split())
        if key and key.casefold() != UNASSIGNED_LABEL.casefold():
            board.setdefault(key, []).append(who)
    found: list[tuple[int, str]] = []
    work = text
    for key in sorted(board, key=lambda name: (-len(name), name)):
        pattern = name_pattern(board[key][0]["name"])
        first = pattern.search(work)
        if first is None:
            continue
        found.append((first.start(), key))
        work = pattern.sub(lambda match: " " * len(match.group()), work)
    if not found:
        return NameCheck(problems=_ids_written(work), people=[], masked=work)

    texts, ids = _evidence(facts)
    mentioned = {who["id"] for who in mentions}
    problems: list[str] = []
    named: list[Person] = []
    for _start, key in sorted(found):
        candidates = board[key]
        in_facts = name_pattern(candidates[0]["name"]).search(texts) is not None
        if not in_facts and not any(who["id"] in mentioned for who in candidates):
            problems.append(f"unverified name {candidates[0]['name']}")
            continue
        shown = [who for who in candidates if who["id"] in ids or who["id"] in mentioned] or candidates
        named.extend(who for who in sorted(shown, key=lambda who: who["id"]) if who not in named)
    return NameCheck(problems=problems + _ids_written(work), people=named, masked=work)


def _evidence(facts: Sequence[Facts]) -> tuple[str, set[str]]:
    """The facts' texts (every text cell and every note) in one string, and the person ids among their cells."""
    texts: list[str] = []
    ids: set[str] = set()
    for fact in facts:
        for row in fact.rows:
            for value in row.values():
                if isinstance(value, str):
                    texts.append(value)
                    if PERSON_ID.fullmatch(value):
                        ids.add(value)
        if fact.note:
            texts.append(fact.note)
    return _APART.join(texts), ids


def _ids_written(text: str) -> list[str]:
    return [f"person id {match.group()} written instead of a name" for match in _PERSON_ID_IN_TEXT.finditer(text)]


# --- questions that evaluate people ---------------------------------------------------------------

# Two families of evaluating words; the dashboard's sample (match.ts) reads the same lists word for word.
SCORECARD_PATTERN = (
    r"\b(?:slow\w*|fast\w*|quick\w*|best|worst|better|worse|good|bad|badly|great|poor(?:ly)?"
    r"|productiv\w*|unproductive|perform\w*|underperform\w*|effort\w*|hard(?:er|est|ly)?|hard[\s-]?work\w*"
    r"|slack(?:er|ers|ing|ed)|weak\w*|strong\w*|efficien\w*|inefficien\w*|diligen\w*|careful\w*|conscientious\w*"
    r"|judge\w*|rating|rated|doing (?:well|badly|ok|okay|fine)|how (?:is|are|was|were|has|have)\b.*\bdoing"
    r"|sincer\w*|insincer\w*|follow(?:s|ing|ed)?\s+(?:the\s+)?(?:agile|process)\w*"
    r"|pull(?:s|ing)?\s+(?:their|his|her)\s+(?:own\s+)?weight"
    r"|struggl\w*|(?:fall(?:s|ing|en)?|fell|running|lagging|is|are|was|were|get(?:s|ting)?|got)\s+behind"
    r"|need(?:s|ed|ing)?\s+(?:\w+\s+){0,2}?(?:help|support|attention|coaching|mentoring)"
    r"|mvps?|most\s+valuable|valuable|(?:rock|super)?stars?|standouts?|stand(?:s|ing)?\s+out|outperform\w*"
    r"|lagg(?:ing|ed|ards?)|lags?"
    # How closely someone keeps to the process, either way round ("who deviates from it most?").
    r"|complian\w*|non-?complian\w*|compl(?:y|ies|ied|ying)|adher\w*|deviat\w*|stray\w*|non-?conform\w*"
    r"|(?:further|furthest|farthest|closest|closer)\s+(?:away\s+)?(?:from|to)\s+(?:the\s+|our\s+)?(?:agile\s+)?process"
    r"|(?:ignor|break|bend|skirt|flout|disregard)\w*\s+(?:the\s+|our\s+|their\s+)?(?:agile\s+)?"
    r"(?:process|workflow|rules?)"
    r"|rank\w*|top|bottom|scorecards?|compar\w*|versus|vs|side[\s-]by[\s-]side)\b"
)
PERSONNEL_PATTERN = (
    r"\b(?:fire|fired|fires|firing|sack\w*|dismiss\w*|terminat\w*|let\s+(?:\S+\s+){0,3}?go|laid\s+off"
    r"|lay(?:s|ing)?\s+(?:\S+\s+){0,3}?off|made\s+redundant|make\s+(?:\S+\s+){0,3}?redundant|redundanc(?:y|ies)"
    r"|demot\w*|(?:should|could|must|can|do|would|shall)\s+(?:we|i|you)\s+replace|be\s+replaced"
    r"|replac(?:e|ing)\s+(?:him|her|them)|promot\w*|reward\w*|bonus\w*|appraisal\w*|performance\s+reviews?"
    r"|pay\s+(?:rise|raise|increase)|(?:get|gets|give|deserves?|earn|earns|got)\s+(?:\S+\s+){0,2}?(?:raise|rise)"
    r"|deserv\w*|blam\w*|fault\w*|trust\w*|disciplin\w*|probation\w*|lazy|lazi\w*|attitude\w*|competen\w*"
    r"|incompeten\w*|skill\w*|reliab\w*|unreliab\w*|useless\w*|careless\w*|dishonest\w*|stupid\w*|idiot\w*"
    r"|hopeless\w*|worthless\w*|pathetic\w*)\b"
)
SCORECARD_WORDS = re.compile(SCORECARD_PATTERN)
PERSONNEL_WORDS = re.compile(PERSONNEL_PATTERN)
# Ranking words: a ranking by one of GET /people's counts stays a raw-count ranking (oversight_people).
RANKING_WORDS = re.compile(r"rank\w*|top|bottom|compar\w*|versus|vs|side[\s-]by[\s-]side")


def evaluates(question: str) -> bool:
    """Whether a question evaluates people (SCORECARD_WORDS) or is a personnel question (PERSONNEL_WORDS)."""
    lower = question.lower()
    return SCORECARD_WORDS.search(lower) is not None or PERSONNEL_WORDS.search(lower) is not None


def personnel(question: str) -> bool:
    """Whether a question is a personnel one (PERSONNEL_WORDS); its answer ends with prompts.DECISION_YOURS."""
    return PERSONNEL_WORDS.search(question.lower()) is not None


def ranks_only(question: str) -> bool:
    """Whether the only evaluating words a question holds are ranking words."""
    lower = question.lower()
    found = {match.group() for match in SCORECARD_WORDS.finditer(lower)}
    return (bool(found) and all(RANKING_WORDS.fullmatch(word) for word in found)
            and PERSONNEL_WORDS.search(lower) is None)


def strip_names(question: str, names: Iterable[str]) -> str:
    """The question without these people's names and their @, so "@Bug Hunter" is not a bug."""
    for name in sorted({name for name in names if name.strip()}, key=lambda name: -len(name)):
        question = re.sub(rf"@?(?<!\w){re.escape(name)}(?!\w)", " ", question, flags=re.IGNORECASE)
    return question


# An @ typed where a mention could start: someone named in the text rather than picked.
_TYPED_AT = re.compile(r"(?:^|(?<=[\s(\[{\"'“‘«]))@\w")


def names_someone(question: str, people: Iterable[Person]) -> bool:
    """Whether a question names someone on the board in its own words rather than by a mention."""
    if _TYPED_AT.search(question):
        return True
    folded = fold(question)
    for who in people:
        name = " ".join(who["name"].split())
        if not name or name in (UNASSIGNED_LABEL, UNNAMED):
            continue
        if " " in name:
            if name_pattern(fold(name)).search(folded):
                return True
        elif any(not _starts_a_sentence(question, match.start()) for match in name_pattern(name).finditer(question)):
            return True
    return False


def _starts_a_sentence(text: str, at: int) -> bool:
    before = text[:at].rstrip()
    return not before or before[-1] in ".!?"


def _folded_with_places(text: str) -> tuple[str, list[int]]:
    """The text folded a character at a time (fold()), and where each folded character stood in the text."""
    folded: list[str] = []
    places: list[int] = []
    for index, char in enumerate(text):
        for piece in fold(char):
            folded.append(piece)
            places.append(index)
    return "".join(folded), places


def named_in(question: str, people: Iterable[Person]) -> list[list[Person]]:
    """The people a question names by full display name: one group per name found, in order of appearance."""
    groups: dict[str, list[Person]] = {}
    for who in people:
        name = " ".join(who["name"].split())
        if not name or name in (UNASSIGNED_LABEL, UNNAMED):
            continue
        key = _folded_with_places(name)[0] if " " in name else name
        group = groups.setdefault(key, [])
        if who not in group:
            group.append(who)
    text = question
    found: list[tuple[int, str]] = []
    for key in sorted(groups, key=lambda key: (-len(key), key)):
        if " " in key:
            folded, places = _folded_with_places(text)
            match = name_pattern(key).search(folded)
            if match is None:
                continue
            start, end = places[match.start()], places[match.end() - 1] + 1
        else:
            hits = (hit for hit in name_pattern(key).finditer(text) if not _starts_a_sentence(question, hit.start()))
            first = next(hits, None)
            if first is None:
                continue
            start, end = first.start(), first.end()
        found.append((start, key))
        text = text[:start] + " " * (end - start) + text[end:]
    return [groups[key] for _start, key in sorted(found)]


# Where a lowercase name stands as a person's: after a word that asks about someone, or before a possessive.
_SLOT_BEFORE = r"\b(?:is|was|has|does|did|how['’]?s|what['’]?s|for|of|about|by|from|with|to|show|me)\s+"


def loosely_named_in(question: str, people: Iterable[Person]) -> list[tuple[str, list[Person]]]:
    """The people a question names by first name, surname or a mis-cased one-word name, where a name stands."""
    groups: dict[str, tuple[str, list[Person]]] = {}
    for who in people:
        words = who["name"].split()
        if not words or " ".join(words) in (UNASSIGNED_LABEL, UNNAMED):
            continue
        surname = [words[-1]] if len(words) > 1 and fold(words[-1]) not in NOT_NAMES else []
        for written in (words[0], *surname):
            key = fold(written)
            if len(key) < 2:
                continue
            _, group = groups.setdefault(key, (written, []))
            if who not in group:
                group.append(who)
    folded = fold(question)
    found: list[tuple[int, str]] = []
    for key in groups:
        name = re.escape(key)
        slot = re.search(rf"{_SLOT_BEFORE}({name})(?!\w)|(?<!\w)({name})['’]s?(?!\w)", folded)
        if slot is not None:
            found.append((slot.start(1) if slot.group(1) is not None else slot.start(2), key))
    return [groups[key] for _start, key in sorted(found)]


# As _SLOT_BEFORE without "to", which a verb follows; the dashboard's sample reads the same slots.
_NAME_SLOT = r"\b(?:is|was|has|does|did|how['’]?s|what['’]?s|for|of|about|by|from|with|show|me)\s+"
# Words that stand in those places but are never anyone's name; compared folded.
NOT_NAMES = frozenset("""
    a about after all also always an and another any anybody anyone anything are as at back bad badly be been before
    being best better both but by can could do does doing done down each either else enough ever every everybody
    everyone everything few fewer fewest for from get gets getting go goes going gone good got had has have having he
    her here hers herself him himself his how however i if in into is it its itself just last least less let lot lots
    many may me might mine more most much must my myself need needs neither never new next no nobody none noone nor not
    nothing now of off often old on once one ones only or other others our ours out over overall own please quite
    rather really same she should since so some somebody someone something soon still such than that the their theirs
    them themselves then there these they this those through thus to today tomorrow too under unless until up upon us
    usually very was we well were what whatever when where whether which while who whoever whole whom whose why will
    with within without worse worst would yes yesterday yet you your yours yourself ok okay thanks hello hi
    day days week weeks weekly month months monthly year years quarter quarters time times period periods date dates
    morning afternoon evening tonight
    board boards project projects team teams squad group groups company organisation organization department dept
    people person persons member members developer developers dev devs engineer engineers tester testers assignee
    assignees user users staff admin admins lead leads manager managers owner owners reporter reporters unassigned
    work works worked working workload workloads load loads backlog backlogs ticket tickets issue issues task tasks
    subtask subtasks story stories bug bugs epic epics release releases version versions feature features sprint
    sprints velocity process processes workflow workflows flow delivery quality progress review reviews reviewed
    testing test tests tested status statuses stage stages estimate estimates estimated estimation estimating effort
    finding findings breach breaches warning warnings rule rules jira data figure figures number numbers scorecard
    scorecards rate rates average median total totals count counts cycle overview report reports dashboard open
    closed todo blocked blocker blockers stuck plate capacity
    lower lowest higher highest larger largest bigger biggest greater greatest smaller smallest maximum minimum
    ascending descending top bottom
    """.split())


def unknown_name(question: str, people: Iterable[Person], *, asks: bool) -> str | None:
    """A name typed where a person's name stands that no one on the board goes by; None when there is none."""
    taken = {fold(word) for who in people for word in who["name"].split()}
    folded, places = _folded_with_places(question)
    found: list[tuple[int, int]] = []
    for match in re.finditer(rf"{_NAME_SLOT}([^\W\d_]+)(?![\w'’-])|(?<![\w'’-])([^\W\d_]+)['’]s?(?![\w'’-])", folded):
        group = 1 if match.group(1) is not None else 2
        found.append((match.start(group), match.end(group)))
    for start, end in sorted(found):
        word, first = folded[start:end], places[start]
        typed = question[first:places[end - 1] + 1]
        if len(word) < 2 or word in NOT_NAMES or word in taken or (len(typed) > 1 and typed.isupper()):
            continue
        capital = typed[:1].isupper() and not _starts_a_sentence(question, first)
        if asks or capital:
            return typed[:1].upper() + typed[1:]
    return None


def first_named_in(question: str, people: Iterable[Person]) -> list[tuple[str, list[Person]]]:
    """The people a question names by first name alone, after full names are taken out."""
    everyone = list(people)
    whole = {" ".join(who["name"].split()) for who in everyone}
    groups: dict[str, list[Person]] = {}
    for who in everyone:
        words = who["name"].split()
        if len(words) < 2 or " ".join(words) in (UNASSIGNED_LABEL, UNNAMED) or words[0] in whole:
            continue
        group = groups.setdefault(words[0], [])
        if who not in group:
            group.append(who)
    # Every full name blanked first, so "Ada Brooks" is never read as the first name "Ada".
    text = question
    for group in named_in(question, everyone):
        name = " ".join(group[0]["name"].split())
        pattern = name_pattern(fold(name)) if " " in name else name_pattern(name)
        folded, places = _folded_with_places(text) if " " in name else (text, list(range(len(text))))
        for hit in pattern.finditer(folded):
            start, end = places[hit.start()], places[hit.end() - 1] + 1
            text = text[:start] + " " * (end - start) + text[end:]
    found: list[tuple[int, str]] = []
    for first in groups:
        hit = next((hit for hit in name_pattern(first).finditer(text)
                    if not _starts_a_sentence(question, hit.start())), None)
        if hit is not None:
            found.append((hit.start(), first))
    return [(first, groups[first]) for _start, first in sorted(found)]
