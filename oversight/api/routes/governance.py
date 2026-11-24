from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from oversight.api.access_log import (
    ACCESS_LOG_EXPORT,
    ACCESS_LOG_VIEW,
    ACCESS_LOG_VIEW_ONCE_IN,
    ACTIONS,
    LOCAL_VIEWER,
    VIEWER_HASH,
    AccessLog,
    LogQuery,
    decode_cursor,
    encode_cursor,
    entry_body,
    everyone_served,
    need_viewer,
    read_entries,
    read_facets,
)
from oversight.api.deps import LEVEL_PARAM, LEVELS, PROJECT_PARAM, ApiContext, get_ctx, people_level_only, service_conn
from oversight.api.meta import service_meta
from oversight.api.people import PERSON_ID
from oversight.llm.budget import usage_report
from oversight.timeutil import local_midnight, to_iso

router = APIRouter()

PAGE_DEFAULT = 50
PAGE_MAX = 200
EXPORT_MAX = 10_000
_SINGLE = frozenset({PROJECT_PARAM, LEVEL_PARAM, "from", "to", "person", "viewer", "cursor", "limit", "export"})
_PARAMETERS = _SINGLE | {"action"}
_USAGE_PARAMETERS = frozenset({PROJECT_PARAM, LEVEL_PARAM})
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FIRST_DAY, LAST_DAY = date(2000, 1, 1), date(2100, 12, 31)
_WHOLE = re.compile(r"[0-9]{1,6}")  # ASCII digits only: str.isdigit takes characters int() refuses ("²")
_TRUE, _FALSE = frozenset({"true", "1"}), frozenset({"false", "0"})


def _problem(name: str, message: str, kind: str = "value_error") -> dict[str, Any]:
    return {"loc": ["query", name], "msg": message, "type": kind}


def _given(request: Request, allowed: frozenset[str]) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """The query's values by name, empty values left out, and the problems with its names."""
    values: dict[str, list[str]] = {}
    problems: list[dict[str, Any]] = []
    for name, value in request.query_params.multi_items():
        if name not in allowed:
            if name not in values:
                problems.append(_problem(name, "is not a parameter of this endpoint", "unknown"))
            values.setdefault(name, [])
            continue
        values.setdefault(name, [])
        if value != "":
            values[name].append(value)
    for name, found in values.items():
        if name in allowed and name != "action" and len(found) > 1:
            problems.append(_problem(name, "may be given only once", "repeated"))
    return values, problems


def _level(values: dict[str, list[str]], problems: list[dict[str, Any]]) -> str:
    level = (values.get(LEVEL_PARAM) or ["team"])[0]
    if level not in LEVELS:
        problems.append(_problem(LEVEL_PARAM, "must be team or people"))
    return level


def _day(name: str, values: dict[str, list[str]], problems: list[dict[str, Any]]) -> date | None:
    found = values.get(name)
    if not found:
        return None
    try:
        if not _DAY.fullmatch(found[0]):
            raise ValueError(found[0])
        day = date.fromisoformat(found[0])
    except ValueError:
        problems.append(_problem(name, "must be a calendar day, YYYY-MM-DD"))
        return None
    if not FIRST_DAY <= day <= LAST_DAY:
        problems.append(_problem(name, f"must be a day from {FIRST_DAY.isoformat()} to {LAST_DAY.isoformat()}"))
        return None
    return day


@dataclass(frozen=True)
class AccessLogRequest:
    level: str
    export: bool
    query: LogQuery
    days: tuple[date | None, date | None]


def access_log_query(request: Request, ctx: ApiContext = Depends(get_ctx)) -> AccessLogRequest:
    """GET /access-log's parameters as the contract allows them (4.16); a 422 naming every problem otherwise."""
    values, problems = _given(request, _PARAMETERS)
    level = _level(values, problems)
    first, last = _day("from", values, problems), _day("to", values, problems)
    if first is not None and last is not None and first > last:
        problems.append(_problem("from", "must not be after to"))
    actions = tuple(dict.fromkeys(values.get("action", [])))
    if any(action not in ACTIONS for action in actions):
        problems.append(_problem("action", f"must be one of {', '.join(ACTIONS)}"))
    person = (values.get("person") or [None])[0]
    if person is not None and not PERSON_ID.fullmatch(person):
        problems.append(_problem("person", "must be a person id: p and 12 lower-case hex characters"))
    viewer = (values.get("viewer") or [None])[0]
    if viewer is not None and viewer != LOCAL_VIEWER and not VIEWER_HASH.fullmatch(viewer):
        problems.append(_problem("viewer", "must be a viewer hash: 32 lower-case hex characters, or local"))
    raw_export = (values.get("export") or ["false"])[0].lower()
    if raw_export not in _TRUE | _FALSE:
        problems.append(_problem("export", "must be true or false"))
    export = raw_export in _TRUE
    ceiling = EXPORT_MAX if export else PAGE_MAX
    raw_limit = (values.get("limit") or [str(EXPORT_MAX if export else PAGE_DEFAULT)])[0]
    limit = int(raw_limit) if _WHOLE.fullmatch(raw_limit) else 0
    if not 1 <= limit <= ceiling:
        problems.append(_problem("limit", f"must be a whole number from 1 to {ceiling}"))
    cursor = (values.get("cursor") or [None])[0]
    after = decode_cursor(cursor) if cursor is not None else None
    if cursor is not None and after is None:
        problems.append(_problem("cursor", "is not a cursor this service gave"))
    if problems:
        raise HTTPException(422, problems)
    zone = ctx.settings.report_zone
    query = LogQuery(
        start=to_iso(local_midnight(first, zone)) if first is not None else None,
        end=to_iso(local_midnight(last + timedelta(days=1), zone)) if last is not None else None,
        actions=actions,
        person=person,
        viewer=viewer,
        after=after,
        limit=limit,
    )
    return AccessLogRequest(level=level, export=export, query=query, days=(first, last))


@router.get("/access-log")
def access_log(
    request: Request,
    asked: AccessLogRequest = Depends(access_log_query),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(service_conn),
) -> dict[str, Any]:
    people_level_only(asked.level, "the access log and its export")
    viewer = need_viewer(request)
    rows, more = read_entries(conn, asked.query)
    people = everyone_served(conn, ctx.projects)
    entries = [entry_body(row, people) for row in rows]
    body = {
        "meta": service_meta(conn, ctx),
        "entries": entries,
        "next_cursor": encode_cursor(rows[-1]["at"], rows[-1]["id"]) if more and rows else None,
        "facets": read_facets(conn, people) if not asked.export and asked.query.after is None else None,
    }
    first, last = asked.days
    access: AccessLog = request.app.state.access_log
    access.record(
        viewer=viewer,
        level=asked.level,
        project=None,
        action=ACCESS_LOG_EXPORT if asked.export else ACCESS_LOG_VIEW,
        subjects=[asked.query.person] if asked.query.person else [],
        detail={
            "from": first.isoformat() if first else None,
            "to": last.isoformat() if last else None,
            "actions": list(asked.query.actions),
            "viewer": asked.query.viewer,
            "entries": len(entries),
        },
        once_within=None if asked.export else ACCESS_LOG_VIEW_ONCE_IN,
    )
    return body


def usage_query(request: Request) -> str:
    """GET /usage's level; a 422 for any other parameter, or a bad level."""
    values, problems = _given(request, _USAGE_PARAMETERS)
    level = _level(values, problems)
    if problems:
        raise HTTPException(422, problems)
    return level


@router.get("/usage")
def usage(
    level: str = Depends(usage_query),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(service_conn),
) -> dict[str, Any]:
    people_level_only(level, "usage figures")
    return {"meta": service_meta(conn, ctx), **usage_report(conn, ctx.settings, ctx.now())}
