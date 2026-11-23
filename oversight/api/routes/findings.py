"""GET /findings and GET /findings/{finding_id} (contract 4.3, 4.4).

The list takes the interface's query parameters (src/lib/api/params.ts in the
jira-oversight-ui repository) with the same names, bounds and defaults, and
refuses what the interface refuses: an unknown name, a single-valued name given
twice, a value out of range (422 here; the interface answers 400 before it
would ever forward one). An empty value counts as not given, as there.

Filtering, facets, sorting and paging follow the interface's sample mode:
- every filter narrows: any of the rules, the severity, the state (open by
  default), any of the epics ("none": no epic, which includes every sprint
  finding), any of the people (a person id: the issue's current assignee is
  that person; "unassigned": an issue finding with no assignee; a sprint finding
  belongs to no one, so it never matches, nor does a finding whose issue is no
  longer in the snapshot, and an id nobody on the board has matches nothing), q,
  and the first-seen window counted back from now;
- q is an exact key (any case), a bare number ("142", "#142") matched against
  the key's number, or text found in the key, the summary or the sprint's
  name, any case;
- each facet's counts apply every filter except its own, and every entry is
  listed, even at zero, in a fixed order: rules in catalogue order (then the
  board's custom rules, JO-20, and any other rule its findings name, a deleted
  custom rule by its title), epics by
  key then "none", breach then warning, open then resolved, people (everyone
  assigned a ticket on the board now) by name lower-cased, then id, then
  "unassigned"; a people entry also carries the person's picture version
  (avatar, null for "unassigned");
- the sort column in the chosen order with empty values last either way, ties
  newest first, then finding_id; key columns sort by issue number;
- a page past the end is an empty page.

A list filtered by one or more people (a person id; "unassigned" is no one) is
recorded in the access log (JO-12, oversight/api/access_log.py) as
person_findings_view, with those person ids, the state and the page, at the
level the dashboard's server names (level=team|people, team when left out: the
Person filter is on Findings at both levels). Such a request must name its
viewer (X-Oversight-Viewer, else 422), and when the entry cannot be written
nothing is served (500, access_log_unavailable).
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from oversight.api.access_log import PERSON_FINDINGS_VIEW, AccessLog, need_viewer
from oversight.api.deps import LEVEL_PARAM, LEVELS, PROJECT_PARAM, ApiContext, get_ctx, read_conn
from oversight.api.meta import build_meta
from oversight.api.people import UNASSIGNED, UNASSIGNED_LABEL, board_people
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
from oversight.timeutil import to_iso

router = APIRouter()

NO_EPIC = "none"
FIRST_SEEN_DAYS = {"7d": 7, "30d": 30, "90d": 90}
SEVERITY_RANK = {"breach": 2, "warning": 1}
LIST_PARAMS = frozenset(("rule", "epic", "person"))
Facet = Literal["rule", "epic", "severity", "state", "person"]

RuleId = Annotated[str, StringConstraints(pattern=r"^[RW]-[A-Z]+-[0-9]{2,3}$")]
EpicValue = Annotated[str, StringConstraints(pattern=r"^(?:[A-Z][A-Z0-9_]{1,19}-[1-9][0-9]{0,8}|none)$")]
PersonValue = Annotated[str, StringConstraints(pattern=r"^(?:p[0-9a-f]{12}|unassigned)$")]


class FindingsQuery(BaseModel):
    """findingsQuerySchema (params.ts): the same names, bounds and defaults."""

    model_config = ConfigDict(extra="forbid")

    rule: list[RuleId] = Field(default_factory=list, max_length=24)
    severity: Literal["breach", "warning"] | None = None
    state: Literal["open", "resolved", "all"] = "open"
    epic: list[EpicValue] = Field(default_factory=list, max_length=50)
    person: list[PersonValue] = Field(default_factory=list, max_length=50)
    q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    first_seen: Literal["7d", "30d", "90d", "all"] = "all"
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
        # No one's finding (a sprint finding, a gone issue's) has person None: in no list, so any
        # person filter leaves it out.
        if ignore != "person" and people and record.person not in people:
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
    # A rule since removed from the catalogue keeps its findings, so it keeps an entry (after the board's rules), by
    # its title when the board still knows it (a custom rule it deleted, JO-20), else by its id.
    rules += [(rule_id, ctx.rules.title_of(rule_id))
              for rule_id in sorted({record.rule_id for record in records} - catalogued)]
    return {
        "rules": [entry(value, label, by_rule) for value, label in rules],
        "epics": [entry(key, title, by_epic) for key, title in _epics(conn, ctx, records)]
        + [entry(NO_EPIC, "No epic", by_epic)],
        "severity": [entry("breach", "Breach", by_severity), entry("warning", "Warning", by_severity)],
        "state": [entry("open", "Open", by_state), entry("resolved", "Resolved", by_state)],
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
    """The column in the chosen order, empty values last either way; ties newest first, then finding_id.
    Python's sort is stable (reverse=True too), so sorting the tie-breaks first keeps them under the column."""
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
    # Findings for a person are person-level data: the read is recorded, so its viewer must be named (JO-12).
    subjects = [value for value in query.person if value != UNASSIGNED]
    viewer = need_viewer(request) if subjects else None
    now = ctx.now()
    records = load_records(conn, ctx)
    keep = _predicate(query, now)
    matching = _sorted([record for record in records if keep(record)], query)
    start = (query.page - 1) * query.page_size
    body = {
        "meta": build_meta(conn, ctx),
        "items": finding_payloads(conn, ctx, matching[start : start + query.page_size]),
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


@router.get("/findings/{finding_id}", response_model=None)
def get_finding(
    finding_id: str,
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    """The evidence drawer: the finding and its issue's status intervals (none for a sprint finding)."""
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
        "finding": finding_payloads(conn, ctx, records, history)[0],
        "timeline": timeline,
    }
