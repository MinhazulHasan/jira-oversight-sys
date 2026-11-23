"""Weekly series and headline totals shared by GET /overview and GET /trends.

The contract is docs/API_CONTRACT.md (sections 2, 4.2 and 4.8) in the
jira-oversight-ui repository; its zod schemas in src/lib/api/schemas.ts are the
source of truth. Two kinds of series:

- Weekly series (trend points `{t, value}`): weeks start on Monday 00:00 UTC,
  oldest first, and the last is the current week to date; `t` is the Monday.
  A week with nothing to measure is null, never 0.
- Window totals (the headline tiles): rolling 7-day windows ending now,
  now - 7 days, ..., oldest first, so a tile's sparkline and its delta agree.

Timestamps are compared as stored (UTC text, YYYY-MM-DDTHH:MM:SS.mmmZ: text
order is time order). A finding with no first_seen_at counts as seen from the
start, so an open finding is always counted as open.

New and resolved count what happened to the work, never what a change of rules
did to the findings (JO-15), as the weekly insights and the digest count them:
- new: a condition (a rule on a ticket, by its evidence key: what a finding
  names, which no board setting changes) first seen in the window, under
  whichever id. A severity changed in a board's rule settings, or a catalogue
  version bump, raises the same condition again under a new id, which is not
  new: once its first sighting is behind the window, it counts nowhere.
- resolved: findings the rules stopped finding in the window (resolved_why
  "cleared"). One closed because the board's rule settings changed
  (settings_changed), replaced under a new rule_version (superseded) or left
  behind by the look-back (aged_out) was not fixed, so it is never counted.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, NamedTuple

from oversight.api.deps import ApiContext
from oversight.api.meta import last_synced_at
from oversight.ask.catalog import _EPIC_OF_I, _PARENT_JOIN, CatalogContext, _has_skip, _type_exclusion, _week_starts
from oversight.stats import median, share
from oversight.timeutil import to_iso

TrendMetric = Literal[
    "median_cycle_time_h",
    "median_effort_ratio",
    "skipped_stage_share",
    "reopen_rate",
    "open_breaches",
    "open_warnings",
]
SERIES_WEEKS = 12  # trend cards, hot spot series, the default and the most GET /trends gives
WINDOWS = 12  # points in each window total
WINDOW = timedelta(days=7)
# The headline totals, in the contract's order.
TOTALS = ("open_breaches", "open_warnings", "new_7d", "resolved_7d", "issues_in_scope", "rules_not_measurable")


@dataclass(frozen=True)
class TrendDefinition:
    label: str
    unit: Literal["count", "hours", "ratio", "share"]
    direction_good: Literal["up", "down"]
    note: str


# Labels as the sample generator has them (src/server/sample/series.ts). "Finished in the week"
# counts every issue type, Epics included, as oversight_trend does (contract 4.8), and says so.
TREND_DEFINITIONS: dict[str, TrendDefinition] = {
    "median_cycle_time_h": TrendDefinition(
        "Median cycle time",
        "hours",
        "down",
        "Median hours from the start of development to Done, over issues finished in the week, Epics included.",
    ),
    "median_effort_ratio": TrendDefinition(
        "Median effort ratio",
        "ratio",
        "down",
        "Median logged time over estimate, over issues finished in the week that have both an estimate and logged"
        " time, Epics included.",
    ),
    "skipped_stage_share": TrendDefinition(
        "Skipped a stage",
        "share",
        "down",
        "Share of issues finished in the week that skipped a stage on the way, such as To Do straight to Done,"
        " Epics included.",
    ),
    "reopen_rate": TrendDefinition(
        "Reopen rate",
        "share",
        "down",
        "Share of issues finished in the week that had been reopened after Done, Epics included.",
    ),
    "open_breaches": TrendDefinition(
        "Open breaches",
        "count",
        "down",
        "Breach findings open at the end of the week; for the current week, open now.",
    ),
    "open_warnings": TrendDefinition(
        "Open warnings",
        "count",
        "down",
        "Warning findings open at the end of the week; for the current week, open now.",
    ),
}
TREND_METRICS: tuple[str, ...] = tuple(TREND_DEFINITIONS)
_OPEN_METRICS = {"open_breaches": "breach", "open_warnings": "warning"}
# The one way a finding resolves because the work changed: the rules no longer find it.
FIXED = "cleared"


class Finding(NamedTuple):
    severity: str
    first_seen_at: str  # "" when the engine recorded none: seen from the start
    resolved_at: str | None
    issue_key: str  # "sprint:<id>" for sprint findings
    epic_key: str | None  # the issue's epic (a subtask's is its parent's); None: no epic, or a sprint finding
    epic_title: str | None
    rule_id: str
    evidence_key: str
    resolved_why: str | None  # cleared | superseded | aged_out | settings_changed; None while open

    @property
    def condition(self) -> tuple[str, str, str]:
        """What the finding names, whichever id it carries: its rule, its ticket (or sprint) and its evidence key."""
        return self.rule_id, self.issue_key, self.evidence_key


# LEFT JOINs, so sprint findings (issue_key "sprint:<id>") stay in, with no epic (contract 8.1).
_FINDINGS_SQL = f"""
    SELECT f.severity, COALESCE(f.first_seen_at, '') AS first_seen_at, f.resolved_at, f.issue_key,
           {_EPIC_OF_I} AS epic_key, e.summary AS epic_title, f.rule_id, f.evidence_key, f.resolved_why
    FROM findings AS f
    LEFT JOIN issues AS i ON i.issue_key = f.issue_key
    {_PARENT_JOIN}
    LEFT JOIN issues AS e ON e.issue_key = {_EPIC_OF_I}
    WHERE f.project_key = ?
"""
_DONE_SQL = """
    SELECT m.done_at, m.cycle_time_h, m.effort_ratio, m.skipped_stages, m.reopen_count, i.time_spent_s
    FROM issue_metrics AS m JOIN issues AS i ON i.issue_key = m.issue_key
    WHERE i.project_key = ? AND m.done_at >= ? AND m.done_at < ?
"""


def catalog_context(conn: sqlite3.Connection, ctx: ApiContext, now: datetime) -> CatalogContext:
    """The Ask catalogue's context, for reusing its queries at this request's `now`."""
    return CatalogContext(
        process=ctx.process,
        rules=ctx.rules,
        project=ctx.project,
        now=now,
        last_synced_at=last_synced_at(conn, ctx.project),
    )


def load_findings(conn: sqlite3.Connection, project: str) -> list[Finding]:
    """Every finding of the project, open and resolved, with its epic."""
    return [Finding(*row) for row in conn.execute(_FINDINGS_SQL, (project,)).fetchall()]


def rounded(value: float | int | None) -> float | int | None:
    """Floats to 4 decimals, as the catalogue's facts carry them; counts and None unchanged."""
    return round(value, 4) if isinstance(value, float) else value


# --- weekly series -------------------------------------------------------------------------


def weeks(now: datetime, last_n: int = SERIES_WEEKS) -> list[tuple[str, str, str]]:
    """(Monday as YYYY-MM-DD, start, end) per week in storage format, oldest first. The end is
    the next Monday, exclusive; for the current week it lies after now, so it reads "open now"."""
    return [
        (start.date().isoformat(), to_iso(start) or "", to_iso(start + timedelta(weeks=1)) or "")
        for start in _week_starts(now, last_n)
    ]


def open_at_week_end(findings: list[Finding], end: str) -> int:
    """Findings open at a week's end: first seen before it and not resolved before it."""
    return sum(1 for f in findings if f.first_seen_at < end and (f.resolved_at is None or f.resolved_at >= end))


def _done_value(metric: str, done: list[sqlite3.Row]) -> float | None:
    if metric == "median_cycle_time_h":
        return median(d["cycle_time_h"] for d in done if d["cycle_time_h"] is not None)
    if metric == "median_effort_ratio":
        # Contract 8.2: estimated work with nothing logged has a ratio of 0. That is
        # missing data, not effort, so only work with time logged counts.
        return median(
            d["effort_ratio"] for d in done if d["effort_ratio"] is not None and (d["time_spent_s"] or 0) > 0
        )
    if metric == "skipped_stage_share":
        return share(sum(1 for d in done if _has_skip(d["skipped_stages"])), len(done))
    if metric == "reopen_rate":
        return share(sum(1 for d in done if (d["reopen_count"] or 0) > 0), len(done))
    raise ValueError(f"not a trend metric: {metric!r}")


def trend_series(
    conn: sqlite3.Connection,
    project: str,
    metric: str,
    now: datetime,
    last_n: int = SERIES_WEEKS,
    *,
    findings: list[Finding] | None = None,
) -> list[dict[str, Any]]:
    """One metric's weekly points `{t, value}`, oldest first, ending with the current week."""
    spans = weeks(now, last_n)
    values: list[float | int | None]
    if metric in _OPEN_METRICS:
        severity = _OPEN_METRICS[metric]
        pool = findings if findings is not None else load_findings(conn, project)
        rows = [f for f in pool if f.severity == severity]
        values = [open_at_week_end(rows, end) for _t, _start, end in spans]
    else:
        done = conn.execute(_DONE_SQL, (project, spans[0][1], spans[-1][2])).fetchall()
        values = [_done_value(metric, [d for d in done if start <= d["done_at"] < end]) for _t, start, end in spans]
    return [{"t": t, "value": rounded(value)} for (t, _start, _end), value in zip(spans, values, strict=True)]


def trend_card(
    conn: sqlite3.Connection, project: str, metric: str, now: datetime, *, findings: list[Finding] | None = None
) -> dict[str, Any]:
    """An Overview trend card: its points are GET /trends?metric=<metric>&last_n=12 exactly."""
    definition = TREND_DEFINITIONS[metric]
    points = trend_series(conn, project, metric, now, SERIES_WEEKS, findings=findings)
    return {
        "metric": metric,
        "label": definition.label,
        "unit": definition.unit,
        "current": points[-1]["value"],
        "previous": points[-2]["value"] if len(points) > 1 else None,
        "direction_good": definition.direction_good,
        "points": points,
    }


# --- window totals -------------------------------------------------------------------------


def window_ends(now: datetime, count: int = WINDOWS) -> list[datetime]:
    """Ends of the rolling 7-day windows, oldest first: now - (count - 1) weeks, ..., now."""
    return [now - WINDOW * (count - 1 - index) for index in range(count)]


def open_at(findings: list[Finding], end_at: str) -> int:
    """Findings open at a moment: first seen at or before it, not resolved by then."""
    return sum(
        1 for f in findings if f.first_seen_at <= end_at and (f.resolved_at is None or f.resolved_at > end_at)
    )


def first_sightings(findings: list[Finding]) -> list[str]:
    """When each condition the findings name was first seen, under whichever id ("" for one seen from the start): a
    condition raised again under a new id (a severity changed on the board, a catalogue version bump) was first seen
    when its first finding was."""
    earliest: dict[tuple[str, str, str], str] = {}
    for f in findings:
        seen = earliest.get(f.condition)
        if seen is None or f.first_seen_at < seen:
            earliest[f.condition] = f.first_seen_at
    return list(earliest.values())


def _total(points: list[dict[str, Any]]) -> dict[str, Any]:
    return {"current": points[-1]["value"], "previous": points[-2]["value"], "series": points}


def window_totals(
    conn: sqlite3.Connection,
    catalog: CatalogContext,
    findings: list[Finding],
    *,
    rules_not_measurable: int,
) -> dict[str, dict[str, Any]]:
    """The six headline totals over the rolling windows (contract 4.2, "Window total")."""
    exclusion, excluded = _type_exclusion(catalog, "i")
    created = [
        row[0]
        for row in conn.execute(
            f"SELECT i.created_at FROM issues AS i WHERE i.project_key = ?{exclusion}",
            (catalog.project, *excluded),
        )
    ]
    breaches = [f for f in findings if f.severity == "breach"]
    warnings = [f for f in findings if f.severity == "warning"]
    # New and resolved count what happened to the work (see the module docstring): conditions by their first sighting,
    # and the findings the rules stopped finding, never those a change of rules closed or raised again.
    sightings = [seen for seen in first_sightings(findings) if seen]
    fixed = [f.resolved_at for f in findings if f.resolved_at and f.resolved_why == FIXED]
    series: dict[str, list[dict[str, Any]]] = {name: [] for name in TOTALS}
    for end in window_ends(catalog.now):
        end_at, start_at = to_iso(end) or "", to_iso(end - WINDOW) or ""
        values = {
            "open_breaches": open_at(breaches, end_at),
            "open_warnings": open_at(warnings, end_at),
            # Both edges included, so the current window is the findings filter first_seen=7d, a finding raised again
            # under a new id apart.
            "new_7d": sum(1 for seen in sightings if start_at <= seen <= end_at),
            "resolved_7d": sum(1 for at in fixed if start_at <= at <= end_at),
            # Work with no created_at exists now: counted, so status_mix adds up to the current total.
            "issues_in_scope": sum(1 for created_at in created if created_at is None or created_at <= end_at),
            "rules_not_measurable": rules_not_measurable,
        }
        for name, value in values.items():
            series[name].append({"end_at": end_at, "value": value})
    return {name: _total(points) for name, points in series.items()}

