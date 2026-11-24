"""`python -m oversight derive`: rebuild timelines and metrics from the snapshot."""
from __future__ import annotations

import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from oversight.config import ProcessConfig, ProjectProcess
from oversight.derive.metrics import compute_metrics
from oversight.derive.timeline import TimelineWarnings, build_intervals
from oversight.logs import get_logger
from oversight.store.db import Store
from oversight.timeutil import utcnow

log = get_logger("derive")

# Every warning a run counts, so run stats keep one shape from run to run.
_WARNING_NAMES: tuple[str, ...] = (
    "inconsistent_transitions",  # a change's from_status isn't the status the issue was in
    "negative_intervals",  # a change dated before created_at (its interval clamped to 0 h)
    "unmapped_statuses",  # distinct status names in timelines that process.yaml doesn't map
    "current_status_mismatch",  # history ends in a status other than the issue's current one
    "missing_created_at",  # issues skipped: nothing to start their timeline from
)

# Issue keys (or status names) quoted per warning in the log.
_LOG_SAMPLE = 5

_ISSUES_SQL = """
    SELECT issue_key, project_key, status, status_category, created_at,
           original_estimate_s, time_spent_s, story_points
    FROM issues WHERE project_key = ? ORDER BY issue_key
"""
_STATUS_CHANGES_SQL = """
    SELECT sc.issue_key, sc.changelog_id, sc.changed_at, sc.from_status, sc.to_status
    FROM status_changes AS sc JOIN issues AS i ON i.issue_key = sc.issue_key
    WHERE i.project_key = ?
"""
_FIELD_CHANGES_SQL = """
    SELECT fc.issue_key, fc.changelog_id, fc.field, fc.changed_at, fc.from_value, fc.to_value
    FROM field_changes AS fc JOIN issues AS i ON i.issue_key = fc.issue_key
    WHERE i.project_key = ? AND fc.field = ?
"""
_STATUSES_SQL = "SELECT name, category FROM statuses WHERE project_key = ? ORDER BY status_id"

_DELETE_INTERVALS_SQL = (
    "DELETE FROM status_intervals WHERE issue_key IN (SELECT issue_key FROM issues WHERE project_key = ?)"
)
_INSERT_INTERVAL_SQL = (
    "INSERT INTO status_intervals (issue_key, seq, status, stage, entered_at, left_at, hours) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
# NOT EXISTS rather than NOT IN: one NULL issue_key would make NOT IN match nothing.
_DELETE_ORPHANS_SQL: tuple[str, ...] = (
    "DELETE FROM status_intervals WHERE NOT EXISTS "
    "(SELECT 1 FROM issues WHERE issues.issue_key = status_intervals.issue_key)",
    "DELETE FROM issue_metrics WHERE NOT EXISTS "
    "(SELECT 1 FROM issues WHERE issues.issue_key = issue_metrics.issue_key)",
)


@dataclass
class DeriveResult:
    run_id: str
    # project -> issues processed
    issues: dict[str, int]
    intervals: int
    elapsed_s: float
    # warning name -> count (inconsistent_transitions, negative_intervals, unmapped_statuses, ...)
    warnings: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        """A short human summary: issues per project, intervals, elapsed time, warnings."""
        lines = [f"derive run {self.run_id}: {self.intervals:,} status intervals in {self.elapsed_s:.2f} s"]
        for project, count in self.issues.items():
            lines.append(f"  {project}: {count:,} issues")
        raised = [f"{name} {count:,}" for name, count in self.warnings.items() if count]
        if raised:
            lines.append(f"  warnings: {', '.join(raised)} (details in the log)")
        else:
            lines.append("  warnings: none")
        return "\n".join(lines)


def run_derive(
    store: Store,
    process: ProcessConfig,
    *,
    projects: list[str],
    now: Callable[[], datetime] = utcnow,
) -> DeriveResult:
    """Rebuild status_intervals and issue_metrics for the projects."""
    started = now()
    clock = time.perf_counter()
    run = store.start_run("derive", started_at=started)
    wanted = list(dict.fromkeys(projects))
    issues: dict[str, int] = {}
    intervals = 0
    warnings = dict.fromkeys(_WARNING_NAMES, 0)
    log.info("derive_started", run_id=run.run_id, projects=wanted)
    try:
        for project in wanted:
            process.project(project)  # ConfigError for an unknown project, before anything changes
        for project in wanted:
            with store.transaction():
                issues[project], project_intervals = _derive_project(store, process, project, started, warnings)
            intervals += project_intervals
        with store.transaction():
            orphans = sum(store.conn.execute(sql).rowcount for sql in _DELETE_ORPHANS_SQL)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        stats = _stats(issues, intervals, warnings, clock) | {"error": error}
        try:
            store.finish_run(run, status="failed", stats=stats, finished_at=now())
        except Exception:  # never let the bookkeeping hide the original error
            log.exception("derive_run_not_recorded", run_id=run.run_id)
        log.error("derive_failed", run_id=run.run_id, error=error)
        raise

    stats = _stats(issues, intervals, warnings, clock) | {"orphans_deleted": orphans}
    store.finish_run(run, status="succeeded", stats=stats, finished_at=now())
    log.info("derive_finished", run_id=run.run_id, **stats)
    return DeriveResult(
        run_id=run.run_id,
        issues=issues,
        intervals=intervals,
        elapsed_s=stats["elapsed_s"],
        warnings=dict(warnings),
    )


def _derive_project(
    store: Store, process: ProcessConfig, project: str, now: datetime, warnings: dict[str, int]
) -> tuple[int, int]:
    """Rebuild one project inside the caller's transaction; returns (issues, intervals)."""
    config = process.project(project)
    issues = [dict(row) for row in store.query(_ISSUES_SQL, (project,))]
    changes = _by_issue(store.query(_STATUS_CHANGES_SQL, (project,)))
    field_changes = _by_issue(store.query(_FIELD_CHANGES_SQL, (project, config.estimate.field)))
    categories = _status_categories(store.query(_STATUSES_SQL, (project,)))

    interval_rows: list[tuple[Any, ...]] = []
    metric_rows: list[dict[str, Any]] = []
    unmapped: Counter[str] = Counter()  # status name -> intervals spent in it
    flagged: dict[str, list[str]] = defaultdict(list)  # warning name -> issue keys
    for issue in issues:
        key = issue["issue_key"]
        if not issue["created_at"]:
            flagged["missing_created_at"].append(key)
            continue
        issue_changes = changes.get(key, [])
        timeline_warnings = TimelineWarnings()
        try:
            timeline = build_intervals(
                issue["created_at"], issue["status"], issue_changes, config.stage_of, timeline_warnings
            )
            category_of = _category_lookup(categories, config, process.done_stage, issue)
            metric_rows.append(
                compute_metrics(
                    issue, timeline, issue_changes, field_changes.get(key, []), process, category_of, now
                )
            )
        except (ValueError, TypeError) as exc:
            # Malformed rows (e.g. an unreadable timestamp): name the issue to fix.
            raise ValueError(f"cannot derive {key}: {exc}") from exc

        if timeline_warnings.inconsistent_transitions:
            warnings["inconsistent_transitions"] += timeline_warnings.inconsistent_transitions
            flagged["inconsistent_transitions"].append(key)
        if timeline_warnings.negative_intervals:
            warnings["negative_intervals"] += timeline_warnings.negative_intervals
            flagged["negative_intervals"].append(key)
        if issue_changes and timeline[-1].status.casefold() != (issue["status"] or "").casefold():
            flagged["current_status_mismatch"].append(key)
        for seq, interval in enumerate(timeline):
            if interval.stage is None:
                unmapped[interval.status] += 1
            interval_rows.append(
                (key, seq, interval.status, interval.stage, interval.entered_at, interval.left_at, interval.hours)
            )

    store.conn.execute(_DELETE_INTERVALS_SQL, (project,))
    store.conn.executemany(_INSERT_INTERVAL_SQL, interval_rows)
    store.upsert_many("issue_metrics", metric_rows)
    for key in flagged["missing_created_at"]:
        store.delete_rows("issue_metrics", {"issue_key": key})

    warnings["current_status_mismatch"] += len(flagged["current_status_mismatch"])
    warnings["missing_created_at"] += len(flagged["missing_created_at"])
    warnings["unmapped_statuses"] += len({name.casefold() for name in unmapped})
    _log_warnings(project, flagged, unmapped)
    log.info("derive_project", project=project, issues=len(metric_rows), intervals=len(interval_rows))
    return len(metric_rows), len(interval_rows)


def _category_lookup(
    categories: Mapping[str, str],
    project: ProjectProcess,
    done_stage: str,
    issue: Mapping[str, Any],
) -> Callable[[str | None], str | None]:
    """category_of for one issue: the statuses table first, then the fallbacks."""
    current = (issue.get("status") or "").casefold()
    current_category = issue.get("status_category")

    def category_of(name: str | None) -> str | None:
        if name is None:
            return None
        folded = name.casefold()
        if folded in categories:
            return categories[folded]
        if project.stage_of(name) == done_stage:
            return "done"
        if current and folded == current:
            return current_category
        return None

    return category_of


def _status_categories(rows: Iterable[Mapping[str, Any] | sqlite3.Row]) -> dict[str, str]:
    """Case-folded status name -> category, from `statuses` rows (blank categories left out)."""
    return {row["name"].casefold(): row["category"] for row in rows if row["name"] and row["category"]}


def _by_issue(rows: Iterable[sqlite3.Row]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["issue_key"]].append(dict(row))
    return grouped


def _log_warnings(project: str, flagged: Mapping[str, list[str]], unmapped: Mapping[str, int]) -> None:
    if unmapped:
        log.warning(
            "unmapped_statuses",
            project=project,
            intervals_by_status=dict(sorted(unmapped.items())),
            hint="map these statuses to stages in config/process.yaml",
        )
    for name, keys in flagged.items():
        if keys:
            log.warning(name, project=project, issues=len(keys), sample=keys[:_LOG_SAMPLE])


def _stats(issues: dict[str, int], intervals: int, warnings: dict[str, int], clock: float) -> dict[str, Any]:
    return {
        "issues": dict(issues),
        "intervals": intervals,
        "warnings": dict(warnings),
        "elapsed_s": round(time.perf_counter() - clock, 3),
    }
