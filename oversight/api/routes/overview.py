"""GET /overview (contract 4.2): the Overview page's first screen.

Headline totals over rolling 7-day windows, hot spots, findings by rule,
coverage, trend cards, where work sits now and the latest briefing's first two
sentences. Every figure is computed from the same findings and issues the
other routes read: the series helpers live in oversight.api.aggregates, and
by_rule and coverage reuse the Ask catalogue's oversight_rule_status and
oversight_coverage, so the Overview, the Findings page and Ask agree.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from oversight.api.aggregates import (
    SERIES_WEEKS,
    Finding,
    catalog_context,
    load_findings,
    open_at_week_end,
    trend_card,
    weeks,
    window_totals,
)
from oversight.api.deps import ApiContext, get_ctx, read_conn
from oversight.api.meta import build_meta
from oversight.ask.catalog import (
    _COVERAGE_EVIDENCE,
    _NO_EPIC,
    CatalogContext,
    CoverageParams,
    RuleStatusParams,
    _type_exclusion,
    run_coverage,
    run_rule_status,
)
from oversight.timeutil import iso, to_iso

router = APIRouter()

HOT_SPOTS = 5
TREND_CARDS = ("median_cycle_time_h", "median_effort_ratio", "skipped_stage_share", "reopen_rate")
COVERAGE_FIELDS = ("estimate", "story_points", "time_logged", "code_review", "test_evidence")
COVERAGE_PERIOD = "90d"
STATUS_CATEGORIES = ("new", "indeterminate", "done")
TEASER_SENTENCES = 2
TONES = ("blatantly_bad", "trending_bad", "neutral")
# The contract's patterns (section 2): ids the interface can turn into links.
_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,19}-[1-9]\d{0,8}$")
_FINDING_ID = re.compile(r"^[0-9a-f]{32}$")
_SPRINT_PREFIX = "sprint:"


@router.get("/overview")
def get_overview(
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    now = ctx.now()
    catalog = catalog_context(conn, ctx, now)
    findings = load_findings(conn, ctx.project)
    by_rule = rule_summaries(conn, catalog)
    not_measurable = sum(1 for rule in by_rule if not rule["measurable"])
    return {
        "meta": build_meta(conn, ctx),
        "totals": window_totals(conn, catalog, findings, rules_not_measurable=not_measurable),
        "hot_spots": hot_spots(findings, now),
        "by_rule": by_rule,
        "coverage": coverage(conn, catalog),
        "trend_cards": {metric: trend_card(conn, ctx.project, metric, now) for metric in TREND_CARDS},
        "status_mix": status_mix(conn, catalog),
        "latest_insight": latest_insight(conn, ctx.project),
        "tracking_since": tracking_since(conn, ctx.project),
    }


def tracking_since(conn: sqlite3.Connection, project: str) -> str | None:
    """When the rules first saw this project: findings history starts here, so the
    interface does not read "everything is new" into the first week. None before any."""
    row = conn.execute("SELECT MIN(first_seen_at) FROM findings WHERE project_key = ?", (project,)).fetchone()
    return row[0] if row and row[0] else None


# --- hot spots -----------------------------------------------------------------------------


def hot_spots(findings: list[Finding], now: datetime, limit: int = HOT_SPOTS) -> list[dict[str, Any]]:
    """Epics by open breaches, then open warnings, then key; the no-epic group (work outside
    every epic, and sprint findings) after the epics, as oversight_hot_spots ranks them. Each
    group's open findings are exactly /findings?state=open&epic=<epic_key or none>."""
    every: dict[str | None, list[Finding]] = defaultdict(list)
    for finding in findings:
        every[finding.epic_key].append(finding)
    groups = []
    for epic, group in every.items():
        open_now = [f for f in group if f.resolved_at is None]
        if not open_now:
            continue
        breaches = sum(1 for f in open_now if f.severity == "breach")
        groups.append((epic, group, open_now, breaches, len(open_now) - breaches))
    groups.sort(key=lambda g: (g[0] is None, -g[3], -g[4], g[0] or ""))
    spans = weeks(now, SERIES_WEEKS)
    spots = []
    for epic, group, open_now, breaches, warnings in groups[:limit]:
        title = _NO_EPIC if epic is None else (open_now[0].epic_title or epic)
        spots.append(
            {
                "epic_key": epic,
                "epic_title": title,
                "open_breaches": breaches,
                "open_warnings": warnings,
                # Issues, so a sprint finding (no issue behind it) is not one.
                "issues": len({f.issue_key for f in open_now if not f.issue_key.startswith(_SPRINT_PREFIX)}),
                # Open at each week's end; the current week's end is after now, so its point is open now.
                "series": [{"t": t, "value": open_at_week_end(group, end)} for t, _start, end in spans],
            }
        )
    return spots


# --- findings by rule ----------------------------------------------------------------------


def rule_summaries(conn: sqlite3.Connection, catalog: CatalogContext) -> list[dict[str, Any]]:
    """Every enabled rule in catalogue order, as oversight_rule_status judges it."""
    stages = catalog.process.stages
    summaries = []
    for row in run_rule_status(conn, RuleStatusParams(), catalog).rows:
        missing = [name.strip() for name in (row["missing"] or "").split(",") if name.strip()]
        # Process order (stage order), whatever order the rule lists them in.
        missing.sort(key=lambda name: stages.index(name) if name in stages else len(stages))
        summaries.append(
            {
                "rule_id": row["rule_id"],
                "title": row["title"],
                "severity": row["severity"],
                "kind": row["kind"],
                "open": row["open_findings"],
                "measurable": bool(row["measurable"]),
                "missing_evidence": missing,
            }
        )
    return summaries


# --- coverage ------------------------------------------------------------------------------


def coverage(conn: sqlite3.Connection, catalog: CatalogContext) -> dict[str, dict[str, Any]]:
    """The five entries as oversight_coverage counts them, over work created in the last 90 days."""
    scope = _coverage_scope(catalog)
    entries = {}
    for field in COVERAGE_FIELDS:
        row = run_coverage(conn, CoverageParams(field=field, period=COVERAGE_PERIOD), catalog).rows[0]
        measurable = bool(row["measurable"])
        entries[field] = {
            "covered": row["covered"],
            "total": row["total"],
            "share": row["share"] if measurable else None,
            "measurable": measurable,
            "note": _coverage_note(field, measurable, catalog, scope),
        }
    return entries


def _coverage_scope(catalog: CatalogContext) -> str:
    scope = f"issues created in the last {int(COVERAGE_PERIOD.rstrip('d'))} days"
    excluded = [name if name.endswith("s") else f"{name}s" for name in catalog.rules.defaults.exclude_types]
    if excluded:
        names = excluded[0] if len(excluded) == 1 else f"{', '.join(excluded[:-1])} and {excluded[-1]}"
        scope += f", {names} left out"
    return scope


def _coverage_note(field: str, measurable: bool, catalog: CatalogContext, scope: str) -> str:
    """What was counted, over which issues, in plain words."""
    if field in _COVERAGE_EVIDENCE:
        what = _COVERAGE_EVIDENCE[field].replace("_", " ")  # "code review", "testing"
        if not measurable:
            return f"Jira records no {what} for {catalog.project}, so it cannot be counted; {scope}."
        return f"Issues with {what} evidence, of the {scope}."
    if field == "estimate":
        basis = catalog.process.estimate_for(catalog.project).basis
        what = "an estimate (story points)" if basis == "points" else "an estimate (original estimate)"
    elif field == "story_points":
        what = "story points"
    else:
        what = "logged time"
    return f"Issues with {what}, of the {scope}."


# --- status mix ----------------------------------------------------------------------------


def status_mix(conn: sqlite3.Connection, catalog: CatalogContext) -> list[dict[str, Any]]:
    """Work (Epics left out) by its status now, in process order. The scope is the
    issues_in_scope total's, so the counts add up to totals.issues_in_scope.current."""
    exclusion, excluded = _type_exclusion(catalog, "i")
    rows = conn.execute(
        f"""
        SELECT i.status AS status, COUNT(*) AS n,
               MAX(CASE WHEN i.status_category IN ('new', 'indeterminate', 'done')
                        THEN i.status_category END) AS category
        FROM issues AS i
        WHERE i.project_key = ? AND (i.created_at IS NULL OR i.created_at <= ?){exclusion}
        GROUP BY i.status
        """,
        (catalog.project, to_iso(catalog.now), *excluded),
    ).fetchall()
    process = catalog.process
    mix = []
    for row in rows:
        stage = process.stage_of(catalog.project, row["status"])
        mix.append(
            {
                "status": row["status"] or "(no status)",
                "stage": stage,
                "status_category": row["category"] or _category_of(stage, catalog),
                "count": row["n"],
            }
        )
    order = process.stages
    mix.sort(
        key=lambda s: (
            order.index(s["stage"]) if s["stage"] in order else len(order),
            STATUS_CATEGORIES.index(s["status_category"]),
            s["status"].casefold(),
        )
    )
    return mix


def _category_of(stage: str | None, catalog: CatalogContext) -> str:
    """A category for a status Jira gave none, from its stage."""
    process = catalog.process
    if stage == process.done_stage:
        return "done"
    if stage in process.stages and process.stages.index(stage) < process.stages.index(process.start_stage):
        return "new"
    return "indeterminate"


# --- the latest briefing -------------------------------------------------------------------


def latest_insight(conn: sqlite3.Connection, project: str) -> dict[str, Any] | None:
    """The newest briefing's first two sentences, or None before the first."""
    row = conn.execute(
        "SELECT week_of, generated_at, verified, sentences_json FROM insights WHERE project_key = ?"
        " ORDER BY generated_at DESC, rowid DESC LIMIT 1",
        (project,),
    ).fetchone()
    if row is None:
        return None
    return {
        "week_of": row["week_of"],
        "generated_at": iso(row["generated_at"]),
        "verified": bool(row["verified"]),
        "sentences": _sentences(row["sentences_json"]),
    }


def _sentences(text: str | None) -> list[dict[str, Any]]:
    """The first two sentences as weekly.py stored them, with only well-formed keys and ids."""
    try:
        stored = json.loads(text) if text else []
    except ValueError:
        stored = []
    sentences: list[dict[str, Any]] = []
    for index, item in enumerate(stored if isinstance(stored, list) else [], start=1):
        if len(sentences) == TEASER_SENTENCES:
            break
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        sentences.append(
            {
                "id": str(item.get("id") or f"s{index}"),
                "text": str(item["text"]),
                "tone": item.get("tone") if item.get("tone") in TONES else "neutral",
                "candidate_ids": [str(c) for c in _list(item.get("candidate_ids"))],
                "issue_keys": [k for k in _list(item.get("issue_keys")) if _matches(_ISSUE_KEY, k)],
                "finding_ids": [f for f in _list(item.get("finding_ids")) if _matches(_FINDING_ID, f)],
            }
        )
    return sentences


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None
