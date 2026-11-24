from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from oversight.api.aggregates import catalog_context, load_findings, open_at_week_end, weeks, window_totals
from oversight.api.deps import LEVEL_PARAM, LEVELS, PROJECT_PARAM, ApiContext, get_ctx, service_conn
from oversight.api.meta import last_synced_at, service_meta
from oversight.api.routes.overview import rule_summaries, status_mix, tracking_since
from oversight.api.timelines import key_number
from oversight.ask.catalog import _ACTIVE_SPRINT_SQL
from oversight.boards import Board
from oversight.timeutil import is_stale, parse_ts, to_iso

router = APIRouter()

ATTENTION = 10
TREND_WEEKS = 8
UNESTIMATED_RULE = "W-EST-01"
SPRINT_RULE = "W-SPR-01"
BOARD_PARAM = "board"
_PARAMETERS = frozenset({PROJECT_PARAM, LEVEL_PARAM, BOARD_PARAM})
_BOARD_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,19}")  # a Jira project key, as the dashboard checks one
_SPRINT_PREFIX = "sprint:"
_OLDEST_BREACHES_SQL = f"""
    SELECT issue_key, rule_id, MIN(first_seen_at) AS opened, MAX(last_seen_at) AS seen, COUNT(*) AS findings
    FROM findings
    WHERE project_key = ? AND severity = 'breach' AND issue_key NOT LIKE '{_SPRINT_PREFIX}%'
      AND first_seen_at IS NOT NULL AND first_seen_at <= ? AND (resolved_at IS NULL OR resolved_at > ?)
    GROUP BY issue_key, rule_id
    ORDER BY opened, CAST(substr(issue_key, instr(issue_key, '-') + 1) AS INTEGER), issue_key, rule_id
    LIMIT ?
"""
# The sprint rule's open findings ("open now" as above): their evidence says when a sprint past its end was found.
_OPEN_SPRINT_FINDINGS_SQL = f"""
    SELECT evidence_json FROM findings
    WHERE project_key = ? AND rule_id = ? AND issue_key LIKE '{_SPRINT_PREFIX}%'
      AND first_seen_at IS NOT NULL AND first_seen_at <= ? AND (resolved_at IS NULL OR resolved_at > ?)
"""


def _known_parameters(request: Request) -> None:
    problems = []
    for name in dict.fromkeys(request.query_params.keys()):
        values = request.query_params.getlist(name)
        if name not in _PARAMETERS:
            problems.append({"loc": ["query", name], "msg": "is not a parameter of this endpoint", "type": "unknown"})
        elif name == BOARD_PARAM:
            keys = [value.strip().upper() for value in values]
            if not all(_BOARD_KEY.fullmatch(key) for key in keys):
                problems.append({"loc": ["query", name], "msg": "must be a board key", "type": "value_error"})
            elif len(set(keys)) != len(keys):
                problems.append({"loc": ["query", name], "msg": "names a board twice", "type": "repeated"})
        elif len(values) > 1:
            problems.append({"loc": ["query", name], "msg": "may be given only once", "type": "repeated"})
        elif name == LEVEL_PARAM and values[0] not in ("", *LEVELS):
            problems.append({"loc": ["query", name], "msg": "must be team or people", "type": "value_error"})
    if problems:
        raise HTTPException(422, problems)


def _listed(request: Request, ctx: ApiContext) -> list[Board]:
    """The boards to list, in the service's order"""
    asked = {value.strip().upper() for value in request.query_params.getlist(BOARD_PARAM)}
    boards = ctx.served().boards
    return [board for board in boards if board.key in asked] if asked else list(boards)


def _has_active_sprint(conn: sqlite3.Connection, project: str) -> bool:
    try:
        return conn.execute(_ACTIVE_SPRINT_SQL, (project,)).fetchone() is not None
    except sqlite3.OperationalError:  # a raw_json that is not valid JSON: no sprint data, as Ask reads it
        return False


def _rule(by_rule: list[dict[str, Any]], rule_id: str) -> dict[str, Any] | None:
    return next((summary for summary in by_rule if summary["rule_id"] == rule_id), None)


def _sprint(conn: sqlite3.Connection, project: str, by_rule: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    risk, unestimated = _rule(by_rule, SPRINT_RULE), _rule(by_rule, UNESTIMATED_RULE)
    # An open finding of either sprint rule is an active sprint, whatever the sprint objects say.
    active = any(rule is not None and rule["open"] > 0 for rule in (risk, unestimated)) or _has_active_sprint(conn, project)

    def measured(rule: dict[str, Any] | None) -> bool:
        return active and rule is not None and bool(rule["measurable"])

    at_risk = risk["open"] > 0 if risk is not None and measured(risk) else None
    return {
        "active_sprint": active,
        "unestimated_in_active_sprint": unestimated["open"] if unestimated is not None and measured(unestimated) else None,
        "sprint_at_risk": at_risk,
        "sprint_overdue": _overdue(conn, project, now) if at_risk else None,
    }


def _overdue(conn: sqlite3.Connection, project: str, now: datetime) -> dict[str, Any] | None:
    at = to_iso(now)
    found = []
    for (raw,) in conn.execute(_OPEN_SPRINT_FINDINGS_SQL, (project, SPRINT_RULE, at, at)).fetchall():
        try:
            evidence = json.loads(raw) if raw else {}
            ended = parse_ts(evidence["end_date"]) if isinstance(evidence, dict) and evidence.get("overdue") else None
        except (KeyError, TypeError, ValueError, OverflowError):
            continue  # evidence written by another engine version: nothing to say of it
        not_done = evidence.get("not_done") if ended is not None else None
        if ended is not None and isinstance(not_done, int) and not isinstance(not_done, bool) and not_done > 0:
            found.append((ended, not_done))
    if not found:
        return None
    ended, not_done = min(found)
    return {"ended_at": to_iso(ended), "not_done": not_done}


def board_row(conn: sqlite3.Connection, ctx: ApiContext, now: datetime) -> dict[str, Any]:
    """One board's row, every figure as its GET /overview works it out (ctx: the board picked)."""
    project = ctx.project
    catalog = catalog_context(conn, ctx, now)
    findings = load_findings(conn, project)
    by_rule = rule_summaries(conn, catalog)
    not_measurable = sum(1 for summary in by_rule if not summary["measurable"])
    totals = window_totals(conn, catalog, findings, rules_not_measurable=not_measurable)
    mix = status_mix(conn, catalog)
    opened = [summary for summary in by_rule if summary["open"] > 0]
    top = max(opened, key=lambda summary: summary["open"]) if opened else None  # the first of equals: catalogue order
    synced = last_synced_at(conn, project)
    return {
        "project": {"key": project, "name": ctx.process.project(project).name or project},
        "last_sync_at": synced,
        "open_tickets": sum(status["count"] for status in mix if status["status_category"] != "done"),
        "in_progress": sum(status["count"] for status in mix if status["status_category"] == "indeterminate"),
        "open_breaches": totals["open_breaches"]["current"],
        "open_warnings": totals["open_warnings"]["current"],
        "new_findings_7d": totals["new_7d"]["current"],
        "resolved_findings_7d": totals["resolved_7d"]["current"],
        "open_findings_trend": [
            *(open_at_week_end(findings, end) for _t, _start, end in weeks(now, TREND_WEEKS)[:-1]),
            totals["open_breaches"]["current"] + totals["open_warnings"]["current"],  # this week to date: open now
        ],
        "tracking_since": tracking_since(conn, project),
        **_sprint(conn, project, by_rule, now),
        "top_rule": {"id": top["rule_id"], "title": top["title"], "open": top["open"]} if top else None,
        "stale": is_stale(synced, now),
    }


def oldest_breaches(conn: sqlite3.Connection, ctx: ApiContext, project: str, now: datetime) -> list[dict[str, Any]]:
    at = to_iso(now)
    rules = ctx.rules
    stale = is_stale(last_synced_at(conn, project), now)
    found = []
    for row in conn.execute(_OLDEST_BREACHES_SQL, (project, at, at, ATTENTION)).fetchall():
        opened = parse_ts(row["opened"])
        seen = parse_ts(row["seen"]) or opened
        until = seen if stale and seen is not None else now
        found.append({
            "project": project,
            "issue_key": row["issue_key"],
            "rule_id": row["rule_id"],
            "title": rules.title_of(row["rule_id"]),
            "severity": "breach",
            "opened_at": to_iso(opened),
            "last_seen_at": to_iso(seen),
            "age_days": max(0, (until - opened).days) if opened is not None else 0,
            "findings": row["findings"],
        })
    return found


def attention_order(breaches: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    turns: dict[tuple[int, str], int] = {}
    keyed = []
    for order, breach in breaches:
        moment = (order, breach["opened_at"])
        turn = turns.get(moment, 0)
        turns[moment] = turn + 1
        keyed.append(((breach["opened_at"], turn, order, key_number(breach["issue_key"]) or 0, breach["issue_key"],
                       breach["rule_id"]), breach))
    keyed.sort(key=lambda item: item[0])
    return [breach for _key, breach in keyed]


@router.get("/portfolio", dependencies=[Depends(_known_parameters)])
def portfolio(
    request: Request,
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(service_conn),
) -> dict[str, Any]:
    now = ctx.now()
    listed = _listed(request, ctx)
    synced = [board.key for board in listed if board.synced]
    boards, breaches = [], []
    for order, key in enumerate(synced):
        board = replace(ctx, selected=key)
        boards.append(board_row(conn, board, now))
        breaches.extend((order, breach) for breach in oldest_breaches(conn, board, key, now))
    not_synced = len(listed) - len(synced)
    return {
        "meta": service_meta(conn, ctx),
        "boards": boards,
        "attention": attention_order(breaches)[:ATTENTION],
        "boards_not_synced": not_synced,
        "not_synced": not_synced,
    }
