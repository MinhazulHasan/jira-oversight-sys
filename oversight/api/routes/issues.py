"""GET /issues/{key}/timeline (contract 4.5): the Issue page.

One issue of the dashboard's project: its facts (its current assignee by Jira
display name and picture version, points, time logged, the estimate's basis and
when it was set, the effort ratio against R-EFF-01's threshold, cycle time,
reopens, its Jira link), every status it sat in, the stages it jumped, its
changelog (assignees as Assigned / Unassigned), the hours logged on it week by
week (totals, never who), and every finding about it, open first, then newest
first. The key is upper-cased before it is looked up; 404 when the snapshot has
no such issue.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from oversight.api.deps import ApiContext, get_ctx, read_conn
from oversight.api.meta import build_meta
from oversight.api.people import person
from oversight.api.timelines import (
    CATEGORIES,
    ISSUE_KEY,
    Categories,
    FindingRecord,
    change_items,
    contract_stage,
    finding_payloads,
    interval_items,
    load_history,
    load_records,
    num,
    skipped_items,
    ts,
    weekly_worklogs,
)
from oversight.ask.catalog import _EPIC_OF_I, _PARENT_JOIN, _effort_threshold, _rows

router = APIRouter()

_ISSUE_SQL = f"""
    SELECT i.issue_key, i.summary, i.issue_type, i.status, i.status_category, i.created_at, i.updated_at,
           i.resolved_at, i.story_points, i.time_spent_s, {_EPIC_OF_I} AS epic_key, e.summary AS epic_title,
           m.started_at, m.cycle_time_h, m.reopen_count, m.skipped_stages, m.estimate_basis,
           m.estimate_set_at, m.effort_ratio, i.assignee_id, a.display_name AS assignee_name,
           a.avatar_hash AS assignee_avatar
    FROM issues AS i
    {_PARENT_JOIN}
    LEFT JOIN issues AS e ON e.issue_key = {_EPIC_OF_I}
    LEFT JOIN issue_metrics AS m ON m.issue_key = i.issue_key
    LEFT JOIN people AS a ON a.account_id = i.assignee_id
    WHERE i.issue_key = ? AND i.project_key = ?
"""
_ESTIMATE_BASES = frozenset(("points", "hours", "none"))


def _measure(value: object) -> float | None:
    """A non-negative figure to 4 decimals; None when unknown (or negative: a data error, not a zero)."""
    number = num(value)
    return number if number is not None and number >= 0 else None


def _open_then_newest(records: list[FindingRecord]) -> list[FindingRecord]:
    """Open first, then newest first, then finding_id (stable sorts, last key first)."""
    ordered = sorted(records, key=lambda record: record.finding_id)
    ordered.sort(key=lambda record: record.first_seen_at, reverse=True)
    ordered.sort(key=lambda record: record.state == "resolved")
    return ordered


def _facts(
    issue: sqlite3.Row,
    ctx: ApiContext,
    category: Callable[[str | None, str | None], str],
    first_entered: str | None,
) -> dict[str, Any]:
    key = issue["issue_key"]
    stage = contract_stage(ctx.process.stage_of(ctx.project, issue["status"]))
    stored_category = issue["status_category"]
    epic_key = issue["epic_key"]
    spent_s = num(issue["time_spent_s"])
    basis = issue["estimate_basis"]
    url = ctx.jira_url(key)
    return {
        "key": key,
        "summary": issue["summary"] or "",
        "type": issue["issue_type"] or "Unknown",
        "status": issue["status"] or "Unknown",
        "status_category": stored_category if stored_category in CATEGORIES else category(issue["status"], stage),
        "stage": stage,
        "epic": {"key": epic_key, "title": issue["epic_title"] or ""} if ISSUE_KEY.fullmatch(epic_key or "") else None,
        # The current assignee, by display name and picture version (the hashed id, never the account id); null: nobody.
        "assignee": person(issue["assignee_id"], issue["assignee_name"], issue["assignee_avatar"]),
        "created_at": ts(issue["created_at"]) or ts(issue["updated_at"]) or first_entered,
        "resolved_at": ts(issue["resolved_at"]),
        "story_points": _measure(issue["story_points"]),
        "time_spent_h": round(spent_s / 3600, 4) if spent_s is not None and spent_s > 0 else None,
        "estimate_basis": basis if basis in _ESTIMATE_BASES else "none",
        "estimate_set_at": ts(issue["estimate_set_at"]),
        "started_at": ts(issue["started_at"]),
        "effort_ratio": _measure(issue["effort_ratio"]),
        "effort_threshold": _effort_threshold(ctx.rules),
        "cycle_time_h": _measure(issue["cycle_time_h"]),
        "reopen_count": max(0, int(num(issue["reopen_count"]) or 0)),
        # The contract takes an https link or null; a synthetic project (the demo) links nowhere.
        "url": url if url and url.startswith("https://") else None,
    }


@router.get("/issues/{key}/timeline", response_model=None)
def issue_timeline(
    key: str,
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    key = key.strip().upper()
    rows = _rows(conn, _ISSUE_SQL, (key, ctx.project))
    if not rows:
        raise HTTPException(404, "no issue with that key in the synced data")
    issue = rows[0]
    history = load_history(conn, [key])
    category = Categories(conn, ctx).for_issue(issue)
    intervals = interval_items(history.intervals.get(key, []), category)
    findings = _open_then_newest(load_records(conn, ctx, " AND f.issue_key = ?", (key,)))
    return {
        "meta": build_meta(conn, ctx),
        "issue": _facts(issue, ctx, category, intervals[0]["entered_at"] if intervals else None),
        "intervals": intervals,
        "skipped": skipped_items(issue["skipped_stages"], history, key),
        "changes": change_items(history, key),
        "worklogs": weekly_worklogs(conn, key),
        "findings": finding_payloads(conn, ctx, findings, history),
    }
