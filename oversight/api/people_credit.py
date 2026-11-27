from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from oversight.api.access_log import Viewer
from oversight.api.credit import (
    SOURCES,
    Choice,
    StaleRevision,
    board_setting,
    coverage,
    field_named,
    save_setting,
    step_one,
)
from oversight.api.deps import LEVEL_PARAM, LEVELS, PROJECT_PARAM, ApiContext, ApiError, people_level_only
from oversight.api.meta import build_meta
from oversight.api.rule_settings import _writer, snapshot
from oversight.api.scorecard import DEFAULT_DAYS, PERIODS, load_board
from oversight.logs import get_logger
from oversight.timeutil import to_iso

log = get_logger("api")

STALE = "stale_revision"
INVALID = "credit_invalid"
BUSY = "credit_busy"
BODY_LIMIT = 4 * 1024  # bytes: a choice takes a few dozen
STALE_DETAIL = (
    "Someone else changed how this board's finished work is counted. Reload to see their choice, then choose again."
)
BUSY_DETAIL = "The board is being updated right now, so the choice could not be saved. Try again in a moment."
_PARAMETERS = frozenset({PROJECT_PARAM, LEVEL_PARAM, "days"})
_KEYS = frozenset({"source", "field", "revision"})


@dataclass(frozen=True)
class CreditQuery:
    """GET and PUT /people/credit's parameters, checked."""

    level: str
    days: int


@dataclass(frozen=True)
class CreditRequest:
    choice: Choice
    revision: int


def _problem(name: str, message: str, kind: str = "value_error") -> dict[str, Any]:
    return {"loc": ["query", name], "msg": message, "type": kind}


def credit_query(request: Request) -> CreditQuery:
    """The parameters as the contract allows them (4.29): a 422 naming every problem, then 403 below people level."""
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
    # Least privilege first: below people level nothing is read, the body included.
    people_level_only(level, "credit settings")
    return CreditQuery(level=level, days=int(days))


def invalid(problems: list[tuple[str, str]]) -> ApiError:
    """The 422 for a refused choice"""
    return ApiError(422, INVALID, " ".join(dict.fromkeys(message for _name, message in problems)),
                    extra={"errors": [{"field": name, "message": message} for name, message in problems]})


async def credit_json(request: Request) -> bytes:
    """The request's body, at most BODY_LIMIT bytes: a larger one is refused before it is read whole."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > BODY_LIMIT:
        raise invalid([("body", "The request body is larger than 4 KB.")])
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > BODY_LIMIT:
            raise invalid([("body", "The request body is larger than 4 KB.")])
    return bytes(body)


def parse_credit(raw: bytes) -> CreditRequest:
    """A choice's body, its shape checked: whether its field is one the board offers is checked when it is saved."""
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise invalid([("body", "The request body must be a JSON object.")])
    problems = [(str(name)[:40], f"{str(name)[:40]} is not something this request takes.")
                for name in sorted(set(body) - _KEYS)]
    source, field, revision = body.get("source"), body.get("field"), body.get("revision")
    if source not in SOURCES:
        problems.append(("source", "The source must be auto, assignee or field."))
    if field is not None and not isinstance(field, str):
        problems.append(("field", "The field must be a people field's key, or null."))
    elif source == "field" and not field:
        problems.append(("field", "Counting by a field needs the field."))
    elif source in ("auto", "assignee") and field is not None:
        problems.append(("field", "Only counting by a field takes a field."))
    if not (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0):
        problems.append(("revision", "The revision the choice was made from must be a whole number."))
    if problems:
        raise invalid(problems)
    return CreditRequest(Choice(source, field if source == "field" else None), revision)


def _no_constant(name: str) -> Any:
    """NaN and Infinity are not JSON, whatever Python's reader allows."""
    raise ValueError(name)


def _excluded(ctx: ApiContext) -> frozenset[str]:
    """The issue types that are no one's work, folded for case (Epic)."""
    return frozenset(name.casefold() for name in ctx.rules.defaults.exclude_types)


def credit_body(conn: sqlite3.Connection, ctx: ApiContext, days: int) -> dict[str, Any]:
    """GET /people/credit's body (and a save's answer), read in one snapshot"""
    with snapshot(conn):
        board = load_board(conn, ctx)
        period = board.period(days)
        setting = board_setting(conn, ctx.process, ctx.project)
        finished = [ticket.issue_key for ticket in board.work if period.holds(ticket.done_at)]
        _eligible, options = coverage(conn, ctx.fields, ctx.project, excluded=_excluded(ctx), finished=finished)
        meta = build_meta(conn, ctx)
    field = step_one(setting.choice, ctx.fields)
    return {
        "meta": meta,
        "period": {"days": period.days, "from": period.since, "to": period.until},
        "setting": {
            "source": setting.choice.source,
            "field": field_named(ctx.fields, setting.choice.field),
            "revision": setting.revision,
            "changed_by": setting.changed_by,
            "changed_at": setting.changed_at,
        },
        "effective_field": {"key": field.key, "name": field.name} if field is not None else None,
        "finished": len(finished),
        "options": [
            {"source": option.choice.source, "field": field_named(ctx.fields, option.choice.field),
             "source_filled": option.source_filled, "credited": option.credited}
            for option in options
        ],
    }


def save_credit(conn: sqlite3.Connection, ctx: ApiContext, asked: CreditRequest, viewer: Viewer) -> None:
    """Save an admin's choice for the board (credit.save_setting): 422 for a field it does not offer, 409 when stale"""
    # The choice that applies stays choosable, even once its field names no one.
    if asked.choice.source == "field" and asked.choice != board_setting(conn, ctx.process, ctx.project).choice:
        eligible, _options = coverage(conn, ctx.fields, ctx.project, excluded=_excluded(ctx), finished=())
        if asked.choice.field not in {one.key for one in eligible}:
            raise invalid([("field", "That field names no one on this board's tickets, so it cannot count work.")])
    store = _writer(ctx.db_path)
    try:
        saved = save_setting(store, ctx.process, ctx.project, asked.choice, revision=asked.revision, by=viewer.name,
                             at=to_iso(ctx.now()) or "")
    except StaleRevision:
        raise ApiError(409, STALE, STALE_DETAIL) from None
    except sqlite3.OperationalError as exc:
        if str(getattr(exc, "sqlite_errorname", "")).startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
            log.warning("people_credit_busy", project=ctx.project)
            raise ApiError(503, BUSY, BUSY_DETAIL) from None
        raise
    finally:
        store.close()
    log.info("people_credit_saved", project=ctx.project, revision=saved.revision, source=saved.choice.source,
             field=saved.choice.field)
