from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from oversight.api.access_log import PERSON_FINDINGS_VIEW, AccessLog, Viewer, need_viewer
from oversight.api.deps import (
    LEVEL_PARAM,
    LEVELS,
    PROJECT_PARAM,
    ApiContext,
    get_ctx,
    people_level_only,
    read_conn,
)
from oversight.api.ignore import (
    BODY_LIMIT,
    RESTORE_BODY_LIMIT,
    IgnoredQuery,
    IgnoreRequest,
    Problem,
    apply_ignore,
    apply_restore,
    board_tickets,
    ignored_page,
    invalid,
    parse_ignore,
    parse_preview,
    parse_restore,
    plan,
    preview_body,
    too_large,
)
from oversight.api.meta import build_meta
from oversight.api.people import UNASSIGNED, UNASSIGNED_LABEL, board_people
from oversight.api.routes.refresh import refresher
from oversight.api.rule_settings import apply_state
from oversight.api.timelines import (
    Categories,
    FindingRecord,
    finding_payloads,
    interval_items,
    key_number,
    load_history,
    load_records,
)
from oversight.ask.catalog import _rows
from oversight.logs import get_logger
from oversight.rules.settings import load_board_settings
from oversight.timeutil import to_iso

router = APIRouter()
log = get_logger("api")

NO_EPIC = "none"
FIRST_SEEN_DAYS = {"7d": 7, "30d": 30, "90d": 90}
SEVERITY_RANK = {"breach": 2, "warning": 1}
LIST_PARAMS = frozenset(("rule", "epic", "person"))
Facet = Literal["rule", "epic", "severity", "state", "person"]

RuleId = Annotated[str, StringConstraints(pattern=r"^[RW]-[A-Z]+-[0-9]{2,3}$")]
EpicValue = Annotated[str, StringConstraints(pattern=r"^(?:[A-Z][A-Z0-9_]{1,19}-[1-9][0-9]{0,8}|none)$")]
PersonValue = Annotated[str, StringConstraints(pattern=r"^(?:p[0-9a-f]{12}|unassigned)$")]


class Filters(BaseModel):
    """Every filter of the findings list but the state: what an ignore's query target names too."""

    model_config = ConfigDict(extra="forbid")

    rule: list[RuleId] = Field(default_factory=list, max_length=24)
    severity: Literal["breach", "warning"] | None = None
    epic: list[EpicValue] = Field(default_factory=list, max_length=50)
    person: list[PersonValue] = Field(default_factory=list, max_length=50)
    q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    first_seen: Literal["7d", "30d", "90d", "all"] = "all"
    ticket: Literal["open", "finished"] | None = None  # the ticket's status category now


class FindingsQuery(Filters):
    """findingsQuerySchema (params.ts): the same names, bounds and defaults."""

    state: Literal["open", "resolved", "ignored", "all"] = "open"
    sort: Literal[
        "first_seen_at", "last_seen_at", "resolved_at", "severity", "rule_id", "issue_key", "epic_key"
    ] = "first_seen_at"
    order: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1, le=10_000)
    page_size: int = Field(default=25, ge=1, le=100)


def _problem(path: str, message: str) -> dict[str, Any]:
    return {"loc": ["query", path], "msg": message, "type": "value_error"}


def parse_query(request: Request) -> FindingsQuery:
    """The query string as the interface's parser reads it (params.ts parseSearch); 422 on any problem."""
    pairs = request.query_params.multi_items()
    raw: dict[str, Any] = {}
    problems: list[dict[str, Any]] = []
    for name in dict.fromkeys(name for name, _ in pairs):
        values = [value for key, value in pairs if key == name]
        if name == PROJECT_PARAM:
            continue  # the board, read by get_ctx
        if name == LEVEL_PARAM:  # the level the dashboard's server reads at, for the access log
            if len(values) > 1:
                problems.append(_problem(name, "may be given only once"))
            elif values[0] not in LEVELS:
                problems.append(_problem(name, "must be team or people"))
            continue
        if name not in FindingsQuery.model_fields:
            problems.append(_problem(name, "is not a parameter of this endpoint"))
        elif name in LIST_PARAMS:
            raw[name] = [value for value in values if value != ""]
        elif len(values) > 1:
            problems.append(_problem(name, "may be given only once"))
        elif values[0] != "":
            raw[name] = values[0]
    if not problems:
        try:
            return FindingsQuery.model_validate(raw)
        except ValidationError as exc:
            problems = [
                _problem(".".join(str(part) for part in error["loc"]) or "(query)", error["msg"])
                for error in exc.errors(include_url=False)
            ]
    raise HTTPException(422, detail=problems)


# --- filters ----------------------------------------------------------------------------------

_KEY_QUERY = re.compile(r"[A-Z][A-Z0-9_]+-[0-9]+")
_NUMBER_QUERY = re.compile(r"#?([0-9]{1,9})")


def _search(q: str | None) -> Callable[[FindingRecord], bool]:
    """q as the interface reads it: an exact key, a key's number, or text in key, summary or sprint name."""
    if not q:
        return lambda record: True
    text, upper = q.strip().lower(), q.strip().upper()

    def in_summary(record: FindingRecord) -> bool:
        return text in (record.summary or "").lower()

    def in_text(record: FindingRecord) -> bool:
        sprint_name = record.sprint["name"] if record.sprint else ""
        return in_summary(record) or text in (record.issue_key or "").lower() or text in sprint_name.lower()

    if _KEY_QUERY.fullmatch(upper):
        return lambda record: record.issue_key == upper or in_summary(record)
    number = _NUMBER_QUERY.fullmatch(text)
    if number:
        suffix = f"-{int(number.group(1))}"
        return lambda record: (record.issue_key or "").endswith(suffix) or in_text(record)
    return in_text


def _predicate(query: FindingsQuery, now: datetime, ignore: Facet | None = None) -> Callable[[FindingRecord], bool]:
    """Every filter of the query, less the one facet being counted."""
    rules, epics, people = set(query.rule), set(query.epic), set(query.person)
    search = _search(query.q)
    days = FIRST_SEEN_DAYS.get(query.first_seen)
    since = to_iso(now - timedelta(days=days)) if days is not None else None

    def keep(record: FindingRecord) -> bool:
        if ignore != "state" and query.state != "all" and record.state != query.state:
            return False
        if ignore != "rule" and rules and record.rule_id not in rules:
            return False
        if ignore != "severity" and query.severity and record.severity != query.severity:
            return False
        if ignore != "epic" and epics and (record.epic_key or NO_EPIC) not in epics:
            return False
        if ignore != "person" and people and record.person not in people:
            return False
        if query.ticket is not None and record.ticket != query.ticket:  # a sprint finding is no ticket's
            return False
        if since is not None and record.first_seen_at < since:
            return False
        return search(record)

    return keep


# --- facets -----------------------------------------------------------------------------------

_EPICS_SQL = """
    SELECT issue_key, summary FROM issues
    WHERE project_key = ?
      AND (lower(issue_type) = 'epic'
           OR issue_key IN (SELECT epic_key FROM issues WHERE project_key = ? AND epic_key IS NOT NULL))
"""


def _key_order(key: str) -> tuple[str, int, str]:
    project, _, number = key.rpartition("-")
    return project, int(number) if number.isdigit() else 0, key


def _epics(conn: sqlite3.Connection, ctx: ApiContext, records: Sequence[FindingRecord]) -> list[tuple[str, str]]:
    """Every epic by key, with its title: the project's epics, and any other a finding's issue sits in."""
    titles: dict[str, str | None] = {
        row["issue_key"]: row["summary"] for row in _rows(conn, _EPICS_SQL, (ctx.project, ctx.project))
    }
    for record in records:
        if record.epic_key and record.epic_key not in titles:
            titles[record.epic_key] = record.row["epic_title"]
    return [(key, titles[key] or key) for key in sorted(titles, key=_key_order)]


def _facets(
    conn: sqlite3.Connection, ctx: ApiContext, records: Sequence[FindingRecord], query: FindingsQuery, now: datetime
) -> dict[str, list[dict[str, Any]]]:
    def counted(facet: Facet, value_of: Callable[[FindingRecord], str | None]) -> Counter[str | None]:
        keep = _predicate(query, now, ignore=facet)
        return Counter(value_of(record) for record in records if keep(record))

    by_rule = counted("rule", lambda record: record.rule_id)
    by_epic = counted("epic", lambda record: record.epic_key or NO_EPIC)
    by_severity = counted("severity", lambda record: record.severity)
    by_state = counted("state", lambda record: record.state)
    by_person = counted("person", lambda record: record.person)  # no one's finding counts under None: no entry

    def entry(value: str, label: str, counts: Counter[str | None]) -> dict[str, Any]:
        return {"value": value, "label": label, "count": counts.get(value, 0)}

    rules = [(rule.id, rule.title or rule.id) for rule in ctx.rules.rules]
    catalogued = {rule_id for rule_id, _ in rules}
    rules += [(rule_id, ctx.rules.title_of(rule_id))
              for rule_id in sorted({record.rule_id for record in records} - catalogued)]
    return {
        "rules": [entry(value, label, by_rule) for value, label in rules],
        "epics": [entry(key, title, by_epic) for key, title in _epics(conn, ctx, records)]
        + [entry(NO_EPIC, "No epic", by_epic)],
        "severity": [entry("breach", "Breach", by_severity), entry("warning", "Warning", by_severity)],
        "state": [entry("open", "Open", by_state), entry("resolved", "Resolved", by_state),
                  entry("ignored", "Ignored", by_state)],
        # A person's entry also carries their picture's version, as a person object does; Unassigned's is null.
        "people": [entry(who["id"], who["name"], by_person) | {"avatar": who["avatar"]}
                   for who in board_people(conn, ctx.project)]
        + [entry(UNASSIGNED, UNASSIGNED_LABEL, by_person) | {"avatar": None}],
    }


# --- sorting ----------------------------------------------------------------------------------


def _sort_value(record: FindingRecord, sort: str) -> str | int | None:
    if sort == "severity":
        return SEVERITY_RANK.get(record.severity, 0)
    if sort == "issue_key":
        return key_number(record.issue_key)
    if sort == "epic_key":
        return key_number(record.epic_key)
    if sort == "resolved_at":
        return record.resolved_at
    if sort == "rule_id":
        return record.rule_id
    if sort == "last_seen_at":
        return record.last_seen_at
    return record.first_seen_at


def _sorted(records: Sequence[FindingRecord], query: FindingsQuery) -> list[FindingRecord]:
    """The column in the chosen order, empty values last either way"""
    ordered = sorted(records, key=lambda record: record.finding_id)
    ordered.sort(key=lambda record: record.first_seen_at, reverse=True)
    present = [record for record in ordered if _sort_value(record, query.sort) is not None]
    empty = [record for record in ordered if _sort_value(record, query.sort) is None]
    present.sort(key=lambda record: _sort_value(record, query.sort), reverse=query.order == "desc")  # type: ignore[arg-type, return-value]
    return present + empty


# --- routes -----------------------------------------------------------------------------------


@router.get("/findings", response_model=None)
def list_findings(
    request: Request,
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    query = parse_query(request)
    subjects = [value for value in query.person if value != UNASSIGNED]
    viewer = need_viewer(request) if subjects else None
    now = ctx.now()
    records = load_records(conn, ctx)
    keep = _predicate(query, now)
    matching = _sorted([record for record in records if keep(record)], query)
    start = (query.page - 1) * query.page_size
    page = matching[start : start + query.page_size]
    body = {
        "meta": build_meta(conn, ctx),
        "items": finding_payloads(conn, ctx, page, people=request.query_params.get(LEVEL_PARAM) == "people"),
        "total": len(matching),
        "page": query.page,
        "page_size": query.page_size,
        "facets": _facets(conn, ctx, records, query, now),
    }
    if viewer is not None:
        access: AccessLog = request.app.state.access_log
        access.record(
            viewer=viewer,
            level=request.query_params.get(LEVEL_PARAM, "team"),
            project=ctx.project,
            action=PERSON_FINDINGS_VIEW,
            subjects=subjects,
            detail={"state": query.state, "page": query.page},
        )
    return body


def reading_level(request: Request) -> str:
    """The level the dashboard's server reads at (team when it names none); 422 for anything else."""
    values = request.query_params.getlist(LEVEL_PARAM)
    if len(values) > 1:
        raise HTTPException(422, [_problem(LEVEL_PARAM, "may be given only once")])
    if values and values[0] not in LEVELS:
        raise HTTPException(422, [_problem(LEVEL_PARAM, "must be team or people")])
    return values[0] if values else "team"


# --- ignoring findings --------------------------------------------------------------------------

_ADMIN_PARAMETERS = frozenset({PROJECT_PARAM, LEVEL_PARAM})
_IGNORED_PARAMETERS = frozenset(("page", "page_size", "rule", "q", "sort", "order"))


class IgnoredParams(BaseModel):
    """GET /findings/ignored's parameters."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, le=10_000)
    page_size: int = Field(default=25, ge=1, le=100)
    rule: list[RuleId] = Field(default_factory=list, max_length=24)
    q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    sort: Literal["at", "issue_key", "rule_id"] = "at"
    order: Literal["asc", "desc"] = "desc"


def _admin(request: Request, allowed: frozenset[str]) -> tuple[Viewer, dict[str, Any]]:
    """What the ignore routes check before anything is read: the viewer, and the other parameters given."""
    raw: dict[str, Any] = {}
    problems: list[dict[str, Any]] = []
    pairs = request.query_params.multi_items()
    for name in dict.fromkeys(name for name, _ in pairs):
        values = [value for key, value in pairs if key == name]
        if name not in allowed:
            problems.append({"loc": ["query", name], "msg": "is not a parameter of this endpoint", "type": "unknown"})
        elif name == "rule":
            raw[name] = [value for value in values if value != ""]
        elif len(values) > 1:
            problems.append({"loc": ["query", name], "msg": "may be given only once", "type": "repeated"})
        elif name not in _ADMIN_PARAMETERS and values[0] != "":
            raw[name] = values[0]
    level = request.query_params.get(LEVEL_PARAM) or "team"
    if level not in LEVELS:
        problems.append({"loc": ["query", LEVEL_PARAM], "msg": "must be team or people", "type": "value_error"})
    if problems:
        raise HTTPException(422, problems)
    people_level_only(level, "ignores")
    return need_viewer(request), raw


def admin(request: Request) -> Viewer:
    return _admin(request, _ADMIN_PARAMETERS)[0]


async def _read_body(request: Request, limit: int) -> bytes:
    """The request's body, at most limit bytes: a larger one is refused before it is read whole."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large(limit)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise too_large(limit)
    return bytes(body)


async def json_body(request: Request) -> bytes:
    return await _read_body(request, BODY_LIMIT)


async def restore_body(request: Request) -> bytes:
    return await _read_body(request, RESTORE_BODY_LIMIT)


def _target_filter(asked: IgnoreRequest, now: datetime) -> tuple[Callable[[FindingRecord], bool] | None, list[str]]:
    """A query target's filters as the findings list applies them (its state aside), and the people it names."""
    if asked.target.query is None:
        return None, []
    given = {name: value for name, value in asked.target.query.items() if value not in (None, "", [])}
    try:
        filters = Filters.model_validate(given)
    except ValidationError as exc:
        raise invalid([
            Problem(".".join(["target", "query", *(str(part) for part in error["loc"])]), error["msg"])
            for error in exc.errors(include_url=False)
        ]) from None
    keep = _predicate(FindingsQuery(state="all", **filters.model_dump()), now)
    return keep, [value for value in filters.person if value != UNASSIGNED]


def _record_read(request: Request, ctx: ApiContext, viewer: Viewer, subjects: list[str], step: str) -> None:
    """A query target naming people reads their findings: recorded as the findings list records it."""
    if not subjects:
        return
    access: AccessLog = request.app.state.access_log
    access.record(viewer=viewer, level="people", project=ctx.project, action=PERSON_FINDINGS_VIEW,
                  subjects=subjects, detail={"state": "open", "ignore": step})


@router.post("/findings/ignore/preview")
def preview_ignore(
    request: Request,
    viewer: Viewer = Depends(admin),
    raw: bytes = Depends(json_body),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    asked = parse_preview(raw)
    keep, subjects = _target_filter(asked, ctx.now())
    tickets = board_tickets(conn, ctx.project) if asked.target.issue_keys else set()
    found = plan(load_records(conn, ctx), asked.target, asked.scope, keep=keep, tickets=tickets)
    body = preview_body(found, ctx, build_meta(conn, ctx))
    _record_read(request, ctx, viewer, subjects, "preview")
    return body


@router.post("/findings/ignore")
def ignore_findings(
    request: Request,
    viewer: Viewer = Depends(admin),
    raw: bytes = Depends(json_body),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    asked = parse_ignore(raw)
    keep, subjects = _target_filter(asked, ctx.now())
    done, made = apply_ignore(ctx, asked, viewer, keep)
    log.info("findings_ignored", project=ctx.project, scope=asked.scope, findings=len(done.findings),
             tickets=done.tickets, ignores=len(made))
    _record_read(request, ctx, viewer, subjects, "ignore")
    return {
        "meta": build_meta(conn, ctx),
        "ignored": {"findings": len(done.findings), "tickets": done.tickets},
        "ignore_ids": made,
    }


@router.post("/findings/restore")
def restore_findings(
    request: Request,
    viewer: Viewer = Depends(admin),
    raw: bytes = Depends(restore_body),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    ignore_ids = parse_restore(raw)
    asked_at = to_iso(ctx.now())
    restored = apply_restore(ctx, ignore_ids, viewer)
    revision = load_board_settings(conn, ctx.project).revision
    runner = refresher(request)
    if restored:  # the rules decide which of their findings reopen, as after a rule settings save
        runner.recheck(ctx.project, revision, name=ctx.process.project(ctx.project).name or ctx.project)
    log.info("findings_restored", project=ctx.project, restored=restored, asked=len(ignore_ids))
    return {
        "meta": build_meta(conn, ctx),
        "restored": restored,
        "recheck": apply_state(conn, ctx.project, revision, runner.recheck_of(ctx.project), saved_at=asked_at),
    }


def ignored_query(request: Request) -> IgnoredQuery:
    """GET /findings/ignored's parameters, checked before anything is read."""
    _viewer, raw = _admin(request, _ADMIN_PARAMETERS | _IGNORED_PARAMETERS)
    try:
        params = IgnoredParams.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(422, detail=[
            _problem(".".join(str(part) for part in error["loc"]) or "(query)", error["msg"])
            for error in exc.errors(include_url=False)
        ]) from None
    return IgnoredQuery(page=params.page, page_size=params.page_size, rules=tuple(dict.fromkeys(params.rule)),
                        q=params.q or None, sort=params.sort, order=params.order)


@router.get("/findings/ignored", response_model=None)
def list_ignored(
    query: IgnoredQuery = Depends(ignored_query),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    return {"meta": build_meta(conn, ctx), **ignored_page(conn, ctx, query)}


@router.get("/findings/{finding_id}", response_model=None)
def get_finding(
    request: Request,
    finding_id: str,
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    """The evidence drawer: the finding and its issue's status intervals (none for a sprint finding)."""
    people = reading_level(request) == "people"
    records = load_records(conn, ctx, " AND f.finding_id = ?", (finding_id,))
    if not records:
        raise HTTPException(404, "no finding with that id")
    record = records[0]
    history = load_history(conn, [record.issue_key])
    timeline: list[dict[str, Any]] = []
    if record.issue_key:
        category = Categories(conn, ctx).for_issue(history.issues.get(record.issue_key))
        timeline = interval_items(history.intervals.get(record.issue_key, []), category)
    return {
        "meta": build_meta(conn, ctx),
        "finding": finding_payloads(conn, ctx, records, history, people=people)[0],
        "timeline": timeline,
    }
