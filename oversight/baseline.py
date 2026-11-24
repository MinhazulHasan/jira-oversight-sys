"""`python -m oversight baseline` (Phase 2): a board's baseline report, from code."""
from __future__ import annotations

import html
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from oversight.config import ProcessConfig, load_yaml
from oversight.errors import ConfigError
from oversight.logs import get_logger
from oversight.settings import validate_project_keys
from oversight.stats import median, percentile, share
from oversight.store.db import Store, SyncCursor
from oversight.timeutil import local_midnight, parse_ts, to_iso, utcnow, zone

log = get_logger("baseline")

# Hours per point within this fraction of process.yaml's hours_per_point count as near it.
_NEAR = 0.25
# Absorbs float rounding, so a value exactly on an edge (6 or 10 h at 8 h per point) counts.
_EDGE = 1e-9
# Labels for a missing value in counts by type, status, link title or host.
_NONE = "(none)"
_NO_HOST = "(no host)"
# Fixed English month names: strftime("%b") follows the machine's locale.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class BaselineExpected(BaseModel):
    """config/baseline_expected.yaml: the reference audit's figures to reproduce."""

    model_config = ConfigDict(extra="forbid")

    project: str
    as_of: date
    window_days: int
    timezone: str
    tempo_account_id: str | None = None
    expected: dict[str, float]


@dataclass(frozen=True)
class _Compared:
    """How one expected metric is checked against the regenerated figures."""

    path: tuple[str, ...]  # where the actual value sits in the figures
    label: str  # what it counts, for the report
    reason: str  # known reasons for a difference; {day} is as_of, e.g. "2 Nov"


_COMPARED: dict[str, _Compared] = {
    "total_issues": _Compared(
        ("snapshot", "created_before_window_end"),
        "issues created before the window end",
        "the reference audit counted during {day}; this window ends at midnight before it",
    ),
    "resolved_in_window": _Compared(
        ("resolved", "in_window"),
        "issues resolved in the window",
        "issues reopened since the reference audit lose their resolution date, and issues deleted "
        "or moved since drop out of the snapshot",
    ),
    "with_original_estimate": _Compared(
        ("resolved", "with_original_estimate", "count"),
        "resolved, with an original estimate",
        "original estimates set or cleared since the reference audit, or a different set of resolved issues",
    ),
    "with_story_points": _Compared(
        ("resolved", "with_story_points", "count"),
        "resolved, with story points",
        "story points set or cleared since the reference audit, or a different set of resolved issues",
    ),
    "with_logged_time": _Compared(
        ("resolved", "with_logged_time", "timespent", "count"),
        "resolved, with time spent",
        "the reference audit may have counted logged time another way",
    ),
    "straight_todo_to_done": _Compared(
        ("resolved", "straight_todo_to_done", "count"),
        "resolved, straight from the first stage to done",
        "status history changed since the reference audit (statuses renamed or issues moved), "
        "or a different set of resolved issues",
    ),
}
COMPARED_METRICS = frozenset(_COMPARED)


def load_expected(path: Path) -> BaselineExpected:
    """Load and validate baseline_expected.yaml (ConfigError when missing or invalid)."""
    path = Path(path)
    data = load_yaml(path)  # ConfigError when missing, unreadable, not UTF-8 or not YAML
    try:
        expected = BaselineExpected.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path.name} is invalid:\n{exc}") from exc
    problems = _expected_problems(expected)
    if problems:
        raise ConfigError(f"{path.name} is invalid: {'; '.join(problems)}")
    return expected


def _expected_problems(expected: BaselineExpected) -> list[str]:
    problems: list[str] = []
    try:
        keys = validate_project_keys([expected.project])
    except ValueError as exc:
        problems.append(str(exc))
    else:
        if keys != [expected.project]:
            problems.append(f"project {expected.project!r} is not an upper-case Jira project key")
    if expected.window_days < 1:
        problems.append("window_days must be at least 1")
    if not expected.timezone.strip():
        problems.append("timezone is empty: name the zone the reference audit used, e.g. America/Chicago")
    else:
        try:
            zone(expected.timezone)
        except ConfigError as exc:
            problems.append(str(exc))
    unknown = [metric for metric in expected.expected if metric not in _COMPARED]
    if unknown:
        problems.append(f"unknown metrics {', '.join(unknown)} (known: {', '.join(_COMPARED)})")
    return problems


@dataclass
class ComparisonRow:
    metric: str
    expected: float
    actual: float | None
    match: bool
    note: str = ""


@dataclass
class BaselineResult:
    project: str
    as_of: date
    window_days: int
    timezone: str
    figures: dict[str, Any]
    comparison: list[ComparisonRow] = field(default_factory=list)
    markdown_path: Path | None = None
    json_path: Path | None = None

    def render(self) -> str:
        """Console summary: window, key figures, the comparison table, output paths."""
        window = _window(self.as_of, self.window_days, self.timezone)
        key_figures = ", ".join(
            f"{label} {_count_and_share(_dig(self.figures, 'resolved', *path))}"
            for label, path in (
                ("story points", ("with_story_points",)),
                ("original estimate", ("with_original_estimate",)),
                ("logged time", ("with_logged_time", "timespent")),
                ("straight to done", ("straight_todo_to_done",)),
            )
        )
        lines = [
            f"baseline {self.project} as of {self.as_of.isoformat()}: "
            f"{_days(self.window_days)} in {self.timezone}",
            f"  window: {to_iso(window.start)} to {to_iso(window.end)} (UTC, end excluded)",
            f"  issues: {_int(_dig(self.figures, 'snapshot', 'total_issues'))} in the snapshot, "
            f"{_int(_dig(self.figures, 'snapshot', 'created_before_window_end'))} created before the window end",
            f"  resolved in the window: {_int(_dig(self.figures, 'resolved', 'in_window'))} ({key_figures})",
        ]
        if self.comparison:
            matched = sum(row.match for row in self.comparison)
            lines.append(f"  comparison with the reference audit: {matched} of {len(self.comparison)} match")
            width = max(len(row.metric) for row in self.comparison)
            for row in self.comparison:
                line = (
                    f"    {row.metric:<{width}}  expected {_number(row.expected):>7}  "
                    f"actual {_number(row.actual):>7}  {'match' if row.match else 'DIFFERS'}"
                )
                lines.append(f"{line}  ({row.note})" if row.note else line)
        for label, path in (("report", self.markdown_path), ("data", self.json_path)):
            if path is not None:
                lines.append(f"  {label}: {path}")
        return "\n".join(lines)


def run_baseline(
    store: Store,
    process: ProcessConfig,
    *,
    project: str,
    as_of: date,
    window_days: int,
    tz_name: str,
    out_dir: Path,
    expected: BaselineExpected | None = None,
    now: Callable[[], datetime] = utcnow,
) -> BaselineResult:
    """Compute the figures, write the Markdown and JSON reports, return the result."""
    started = now()
    run = store.start_run("baseline", started_at=started)
    zone_name = tz_name or "UTC"
    log.info(
        "baseline_started",
        run_id=run.run_id,
        project=project,
        as_of=as_of.isoformat(),
        window_days=window_days,
        zone=zone_name,
    )
    try:
        _check_inputs(process, project, window_days, zone_name)
        window = _window(as_of, window_days, zone_name)
        snapshot = _read_snapshot(store, project)
        if snapshot.issues and not snapshot.metrics:
            raise ConfigError(f"no derived metrics for {project}: run `python -m oversight derive` first")
        tempo_account_id = expected.tempo_account_id if expected is not None else None
        figures = _figures(snapshot, window, process, project, tempo_account_id)
        status, note = _comparison_scope(expected, project, as_of, window_days, zone_name)
        comparison = _compare(expected, figures, as_of) if expected is not None and status == "compared" else []
        meta = _metadata(
            run_id=run.run_id,
            project=project,
            as_of=as_of,
            window_days=window_days,
            zone_name=zone_name,
            window=window,
            generated_at=started,
            process=process,
            snapshot=snapshot,
            comparison_status=status,
            comparison_note=note,
        )
        _log_data_warnings(project, meta)
        payload = {"metadata": meta, "figures": figures, "comparison": [asdict(row) for row in comparison]}
        markdown_path, json_path = _write_reports(
            Path(out_dir), project, as_of, payload, _render_markdown(meta, figures, comparison)
        )
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            store.finish_run(run, status="failed", stats={"project": project, "error": error}, finished_at=now())
        except Exception:  # never let the bookkeeping hide the original error
            log.exception("baseline_run_not_recorded", run_id=run.run_id)
        log.error("baseline_failed", run_id=run.run_id, error=error)
        raise

    stats = {
        "project": project,
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "zone": zone_name,
        "outputs": {"markdown": str(markdown_path), "json": str(json_path)},
        "comparison": {"status": status, "rows": len(comparison), "matched": sum(row.match for row in comparison)},
    }
    store.finish_run(run, status="succeeded", stats=stats, finished_at=now())
    log.info("baseline_finished", run_id=run.run_id, **stats)
    return BaselineResult(
        project=project,
        as_of=as_of,
        window_days=window_days,
        timezone=zone_name,
        figures=figures,
        comparison=comparison,
        markdown_path=markdown_path,
        json_path=json_path,
    )


# --- inputs and the snapshot ---------------------------------------------------


@dataclass(frozen=True)
class _Window:
    """[start, end): local midnights in the report zone, as aware UTC datetimes."""

    start: datetime
    end: datetime

    def includes(self, value: object) -> bool:
        moment = parse_ts(value)
        return moment is not None and self.start <= moment < self.end


def _window(as_of: date, window_days: int, zone_name: str) -> _Window:
    return _Window(
        start=local_midnight(as_of - timedelta(days=window_days), zone_name),
        end=local_midnight(as_of, zone_name),
    )


def _check_inputs(process: ProcessConfig, project: str, window_days: int, zone_name: str) -> None:
    """ConfigError for anything that would give a wrong report, before any figure."""
    try:
        # The key also names the output files, so nothing path-like gets through.
        validate_project_keys([project])
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    process.project(project)  # ConfigError when process.yaml doesn't configure it
    if window_days < 1:
        raise ConfigError("window_days must be at least 1")
    zone(zone_name)  # ConfigError for an unknown zone


_ISSUES_SQL = """
    SELECT issue_key, issue_type, status, status_category, created_at, resolved_at,
           original_estimate_s, time_spent_s, aggregate_time_spent_s, story_points
    FROM issues WHERE project_key = ? ORDER BY issue_key
"""
# The other reads join `issues`, so only this project's rows are read.
_METRICS_SQL = """
    SELECT m.issue_key, m.started_at, m.done_at, m.cycle_time_h, m.reopen_count,
           m.estimate_set_at, m.computed_at
    FROM issue_metrics AS m JOIN issues AS i ON i.issue_key = m.issue_key
    WHERE i.project_key = ?
"""
_STATUS_CHANGES_SQL = """
    SELECT sc.issue_key, sc.from_status, sc.to_status
    FROM status_changes AS sc JOIN issues AS i ON i.issue_key = sc.issue_key
    WHERE i.project_key = ?
"""
_WORKLOGS_SQL = """
    SELECT w.issue_key, w.seconds, w.author_id
    FROM worklogs AS w JOIN issues AS i ON i.issue_key = w.issue_key
    WHERE i.project_key = ? ORDER BY w.worklog_id
"""
_LINKS_SQL = """
    SELECT l.issue_key, l.kind, l.target, l.title
    FROM links AS l JOIN issues AS i ON i.issue_key = l.issue_key
    WHERE i.project_key = ?
"""
_CLOSED_INTERVALS_SQL = """
    SELECT si.status, si.stage, si.left_at, si.hours
    FROM status_intervals AS si JOIN issues AS i ON i.issue_key = si.issue_key
    WHERE i.project_key = ? AND si.left_at IS NOT NULL
"""


@dataclass
class _Snapshot:
    """One project's rows, read once; every figure is computed from these."""

    issues: list[dict[str, Any]]
    metrics: dict[str, dict[str, Any]]  # issue key -> its issue_metrics row
    changes: dict[str, list[dict[str, Any]]]  # issue key -> its status changes
    worklogs: dict[str, list[dict[str, Any]]]  # issue key -> its worklogs
    links: list[dict[str, Any]]
    closed_intervals: list[dict[str, Any]]
    cursor: SyncCursor | None


def _read_snapshot(store: Store, project: str) -> _Snapshot:
    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(row) for row in store.query(sql, (project,))]

    return _Snapshot(
        issues=rows(_ISSUES_SQL),
        metrics={row["issue_key"]: row for row in rows(_METRICS_SQL)},
        changes=_by_issue(rows(_STATUS_CHANGES_SQL)),
        worklogs=_by_issue(rows(_WORKLOGS_SQL)),
        links=rows(_LINKS_SQL),
        closed_intervals=rows(_CLOSED_INTERVALS_SQL),
        cursor=store.get_cursor(project),
    )


# --- figures -----------------------------------------------------------------------


def _figures(
    snapshot: _Snapshot,
    window: _Window,
    process: ProcessConfig,
    project: str,
    tempo_account_id: str | None,
) -> dict[str, Any]:
    resolved = [issue for issue in snapshot.issues if window.includes(issue["resolved_at"])]
    resolved_metrics = [snapshot.metrics[i["issue_key"]] for i in resolved if i["issue_key"] in snapshot.metrics]
    resolved_worklogs = [w for issue in resolved for w in snapshot.worklogs.get(issue["issue_key"], [])]
    reopened = sum(1 for metrics in resolved_metrics if (metrics["reopen_count"] or 0) > 0)
    return {
        "snapshot": _snapshot_figures(snapshot, window),
        "resolved": _resolved_figures(snapshot, resolved, window, process, project),
        "estimates": _estimate_figures(resolved, resolved_metrics, process.estimate_for(project).hours_per_point),
        "time_in_status": _time_in_status(snapshot.closed_intervals, window, process.stages),
        "cycle_time": _spread([m["cycle_time_h"] for m in resolved_metrics if m["cycle_time_h"] is not None]),
        "reopens": _count_share(reopened, len(resolved)),
        "worklogs": _worklog_figures(resolved_worklogs, tempo_account_id),
        "evidence": _evidence(process, project),
        "issue_links": _link_figures(snapshot.links),
    }


def _snapshot_figures(snapshot: _Snapshot, window: _Window) -> dict[str, Any]:
    created_before_end = 0
    for issue in snapshot.issues:
        created = parse_ts(issue["created_at"])
        if created is not None and created < window.end:
            created_before_end += 1
    return {
        "last_synced_at": snapshot.cursor.last_synced_at if snapshot.cursor else None,
        "total_issues": len(snapshot.issues),
        "created_before_window_end": created_before_end,
        "by_type": _counts(issue["issue_type"] for issue in snapshot.issues),
        "by_status": _counts(issue["status"] for issue in snapshot.issues),
        "by_status_category": _counts(issue["status_category"] for issue in snapshot.issues),
    }


def _resolved_figures(
    snapshot: _Snapshot,
    resolved: list[dict[str, Any]],
    window: _Window,
    process: ProcessConfig,
    project: str,
) -> dict[str, Any]:
    stage_of = process.project(project).stage_of
    from_statuses, to_statuses = _straight_statuses(process, project)
    from_names = {status.casefold() for status in from_statuses}
    to_names = {status.casefold() for status in to_statuses}

    def counted(test: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        return _count_share(sum(1 for issue in resolved if test(issue)), len(resolved))

    def went_straight(issue: dict[str, Any]) -> bool:
        return any(
            (change["from_status"] or "").casefold() in from_names
            and (change["to_status"] or "").casefold() in to_names
            for change in snapshot.changes.get(issue["issue_key"], [])
        )

    def created_in_done(issue: dict[str, Any]) -> bool:
        if snapshot.changes.get(issue["issue_key"]):
            return False
        stage = stage_of(issue["status"])
        if stage is not None:
            return stage == process.done_stage
        return issue["status_category"] == "done"

    return {
        "in_window": len(resolved),
        "with_original_estimate": counted(lambda issue: _positive(issue["original_estimate_s"])),
        "with_story_points": counted(lambda issue: _positive(issue["story_points"])),
        "with_logged_time": {
            "timespent": counted(lambda issue: _positive(issue["time_spent_s"])),
            "aggregate": counted(lambda issue: _positive(issue["aggregate_time_spent_s"])),
            "worklog_rows": counted(lambda issue: bool(snapshot.worklogs.get(issue["issue_key"]))),
        },
        "straight_todo_to_done": counted(went_straight)
        | {"from_statuses": from_statuses, "to_statuses": to_statuses},
        "created_in_done": counted(created_in_done),
        "done_entered_in_window": sum(1 for m in snapshot.metrics.values() if window.includes(m["done_at"])),
    }


def _straight_statuses(process: ProcessConfig, project: str) -> tuple[list[str], list[str]]:
    """Statuses of the first ordered stage and of the done stage (AIL: To Do and Done)."""
    order = process.ordered_stages(project)
    # A project that maps only done statuses has no "before" to jump from.
    first = order[0] if order and order[0] != process.done_stage else None
    mapping = process.project(project).status_to_stage
    return (
        [status for status, stage in mapping.items() if first is not None and stage == first],
        [status for status, stage in mapping.items() if stage == process.done_stage],
    )


def _estimate_figures(
    resolved: list[dict[str, Any]],
    resolved_metrics: list[dict[str, Any]],
    hours_per_point: float | None,
) -> dict[str, Any]:
    ratios = [
        issue["time_spent_s"] / 3600 / issue["story_points"]
        for issue in resolved
        if _positive(issue["time_spent_s"]) and _positive(issue["story_points"])
    ]
    within = None
    if hours_per_point:
        near = sum(1 for ratio in ratios if abs(ratio / hours_per_point - 1) <= _NEAR + _EDGE)
        within = {"hours_per_point": hours_per_point, "count": near, "share": share(near, len(ratios))}
    timed = [m for m in resolved_metrics if m["estimate_set_at"] and m["started_at"]]
    late = sum(1 for m in timed if _later(m["estimate_set_at"], m["started_at"]))
    return {
        "hours_per_point": {
            "n": len(ratios),
            "median": median(ratios),
            "p25": percentile(ratios, 25),
            "p75": percentile(ratios, 75),
        },
        "within_25pct": within,
        "estimate_set_after_start": {"count": late, "of": len(timed), "share": share(late, len(timed))},
    }


def _time_in_status(
    intervals: Iterable[Mapping[str, Any]], window: _Window, stages: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Per status (James's stage order, unmapped last): n, median, p75 and p90 hours."""
    hours: dict[str, list[float]] = defaultdict(list)
    stage_of: dict[str, str | None] = {}
    for interval in intervals:
        if interval["hours"] is None or not window.includes(interval["left_at"]):
            continue
        hours[interval["status"]].append(interval["hours"])
        stage_of.setdefault(interval["status"], interval["stage"])
    rank = {stage: index for index, stage in enumerate(stages)}
    ordered = sorted(hours, key=lambda status: (rank.get(stage_of[status] or "", len(rank)), status))
    return {status: {"stage": stage_of[status]} | _spread(hours[status]) for status in ordered}


def _worklog_figures(worklogs: Sequence[Mapping[str, Any]], tempo_account_id: str | None) -> dict[str, Any]:
    """Counts and shares only: account ids group the worklogs and never leave this function."""
    total_seconds = sum(worklog["seconds"] or 0 for worklog in worklogs)
    rows: Counter[str] = Counter()
    seconds: Counter[str] = Counter()
    for worklog in worklogs:
        author = worklog["author_id"]
        if author:
            rows[author] += 1
            seconds[author] += worklog["seconds"] or 0

    def share_of(author: str) -> dict[str, float | None]:
        return {"seconds": share(seconds[author], total_seconds), "count": share(rows[author], len(worklogs))}

    # Tied accounts have equal counts and seconds, so either gives the same shares.
    top = max(rows, key=lambda author: (rows[author], seconds[author]), default=None)
    figures: dict[str, Any] = {
        "count": len(worklogs),
        "total_hours": total_seconds / 3600,
        "authors": len(rows),
        "top_author_share": share_of(top) if top is not None else {"seconds": None, "count": None},
    }
    if tempo_account_id:
        figures["tempo_share"] = share_of(tempo_account_id)
    return figures


def _evidence(process: ProcessConfig, project: str) -> dict[str, list[str]]:
    sources = process.project(project).evidence
    return {
        "not_measurable": process.missing_evidence(project),
        "configured": [name for name, source in sources.items() if source is not None],
    }


def _link_figures(links: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    issue_links = [link for link in links if link["kind"] == "issue_link"]
    remote_links = [link for link in links if link["kind"] == "remote_link"]
    return {
        "issues_with_issue_links": len({link["issue_key"] for link in issue_links}),
        "issue_links": len(issue_links),
        "issue_links_by_title": _counts(title for link in issue_links for title in _link_titles(link["title"])),
        "remote_links": len(remote_links),
        "remote_links_by_host": _counts(_host(link["target"]) for link in remote_links),
    }


def _link_titles(title: str | None) -> list[str]:
    parts = [part.strip() for part in (title or "").split(";")]
    return [part for part in parts if part] or [_NONE]


def _host(url: str | None) -> str:
    """A remote link's domain, lower-cased: never its path, query or any credentials in it."""
    try:
        host = urlsplit(url or "").hostname
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        host = None
    return host or _NO_HOST


# --- comparison and metadata ------------------------------------------------------


def _comparison_scope(
    expected: BaselineExpected | None, project: str, as_of: date, window_days: int, zone_name: str
) -> tuple[str, str]:
    if expected is None:
        return "not requested", ""
    if (expected.project, expected.as_of, expected.window_days) != (project, as_of, window_days):
        return "skipped", (
            f"the expected figures are for {expected.project} as of {expected.as_of.isoformat()} over "
            f"{_days(expected.window_days)}, and this report is for {project} as of {as_of.isoformat()} "
            f"over {_days(window_days)}"
        )
    if expected.timezone != zone_name:
        return "compared", (
            f"the reference audit used {expected.timezone} and this report uses {zone_name}, so the window edges differ"
        )
    return "compared", ""


def _compare(expected: BaselineExpected, figures: Mapping[str, Any], as_of: date) -> list[ComparisonRow]:
    rows: list[ComparisonRow] = []
    for metric, wanted in expected.expected.items():
        spec = _COMPARED.get(metric)
        actual = _dig(figures, *spec.path) if spec is not None else None
        match = actual is not None and actual == wanted
        note = comparison_note(metric, figures, as_of, match=match)
        rows.append(ComparisonRow(metric=metric, expected=wanted, actual=actual, match=match, note=note))
    return rows


def comparison_note(metric: str, figures: Mapping[str, Any], as_of: date, *, match: bool) -> str:
    spec = _COMPARED.get(metric)
    if spec is None:
        return "not a baseline figure"
    notes = [] if match else [spec.reason.format(day=f"{as_of.day} {_MONTHS[as_of.month - 1]}")]
    if metric == "with_logged_time":
        variants = _dig(figures, "resolved", "with_logged_time") or {}
        counts = [(name, _dig(variants, name, "count")) for name in ("aggregate", "worklog_rows")]
        if all(isinstance(count, int) for _name, count in counts):
            notes.append("other variants: " + ", ".join(f"{name} {count:,}" for name, count in counts))
    return "; ".join(notes)


def _metadata(
    *,
    run_id: str,
    project: str,
    as_of: date,
    window_days: int,
    zone_name: str,
    window: _Window,
    generated_at: datetime,
    process: ProcessConfig,
    snapshot: _Snapshot,
    comparison_status: str,
    comparison_note: str,
) -> dict[str, Any]:
    local = zone(zone_name)
    last_synced_at = snapshot.cursor.last_synced_at if snapshot.cursor else None
    # The oldest row: one left behind by an earlier derive would make the figures stale.
    computed = [m["computed_at"] for m in snapshot.metrics.values() if m["computed_at"]]
    metrics_computed_at = min(computed) if computed else None
    return {
        "project": project,
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "zone": zone_name,
        "window": {
            "start": to_iso(window.start),
            "end": to_iso(window.end),
            "start_local": window.start.astimezone(local).isoformat(),
            "end_local": window.end.astimezone(local).isoformat(),
        },
        "generated_at": to_iso(generated_at),
        "process_version": process.process_version,
        "last_synced_at": last_synced_at,
        "metrics_computed_at": metrics_computed_at,
        "metrics_stale": _later(last_synced_at, metrics_computed_at),
        "issues_without_metrics": sum(1 for issue in snapshot.issues if issue["issue_key"] not in snapshot.metrics),
        "comparison_status": comparison_status,
        "comparison_note": comparison_note,
        "run_id": run_id,
    }


def _log_data_warnings(project: str, meta: Mapping[str, Any]) -> None:
    if meta["issues_without_metrics"]:
        log.warning(
            "baseline_issues_without_metrics",
            project=project,
            issues=meta["issues_without_metrics"],
            hint="figures from derived rows leave them out; run `python -m oversight derive` if they are new",
        )
    if meta["metrics_stale"]:
        log.warning(
            "baseline_metrics_stale",
            project=project,
            last_synced_at=meta["last_synced_at"],
            metrics_computed_at=meta["metrics_computed_at"],
            hint="run `python -m oversight derive`, then the baseline again",
        )


def report_paths(out_dir: Path, project: str) -> list[Path]:
    pattern = re.compile(rf"baseline_{re.escape(project)}_\d{{4}}-\d{{2}}-\d{{2}}\.json")
    folder = Path(out_dir)
    try:
        return sorted(path for path in folder.iterdir() if pattern.fullmatch(path.name)) if folder.is_dir() else []
    except OSError:
        return []


def _write_reports(
    out_dir: Path, project: str, as_of: date, payload: Mapping[str, Any], markdown: str
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"baseline_{project}_{as_of.isoformat()}"
    markdown_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"
    # newline="\n": the same bytes on Windows as elsewhere, so reports diff cleanly.
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
    return markdown_path, json_path


# --- Markdown -------------------------------------------------------------------------


def _render_markdown(
    meta: Mapping[str, Any], figures: Mapping[str, Any], comparison: Sequence[ComparisonRow]
) -> str:
    """The report for Tazbir and James: plain headings and tables, team and process level only."""
    sections = (
        _md_header(meta, figures["snapshot"]),
        _md_comparison(meta, comparison),
        _md_snapshot(figures["snapshot"]),
        _md_resolved(figures["resolved"]),
        _md_estimates(figures["estimates"]),
        _md_time_in_status(figures["time_in_status"]),
        _md_cycle_time(figures["cycle_time"]),
        _md_reopens(figures["reopens"]),
        _md_worklogs(figures["worklogs"]),
        _md_evidence(figures["evidence"]),
        _md_issue_links(figures["issue_links"]),
    )
    return "\n".join(line for section in sections for line in section).rstrip("\n") + "\n"


def _md_header(meta: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[str]:
    window = meta["window"]
    lines = [
        f"# Baseline report: {_text(meta['project'])}, as of {meta['as_of']}",
        "",
        "Computed by code from the local snapshot of Jira (`python -m oversight baseline`). "
        "Figures are at team and process level: no person or account is named.",
        "",
        *_table(
            ["Item", "Value"],
            [
                [
                    "Window",
                    f"{_local_minute(window['start_local'])} to {_local_minute(window['end_local'])} "
                    f"{meta['zone']} ({_days(meta['window_days'])}; the end is excluded)",
                ],
                ["Window in UTC", f"{window['start']} to {window['end']}"],
                ["Last sync", meta["last_synced_at"] or "never"],
                ["Metrics derived", meta["metrics_computed_at"] or "never"],
                ["Process version", meta["process_version"]],
                ["Generated", meta["generated_at"]],
            ],
        ),
    ]
    warnings = []
    if not snapshot["total_issues"]:
        warnings.append(
            f"The snapshot holds no {_text(meta['project'])} issues: run `python -m oversight sync --full` first."
        )
    if meta["issues_without_metrics"]:
        warnings.append(
            f"{_int(meta['issues_without_metrics'])} of {_int(snapshot['total_issues'])} issues have no "
            "derived metrics, so the figures built on derived data leave them out."
        )
    if meta["metrics_stale"]:
        warnings.append(
            "The last sync is newer than the derived metrics: run `python -m oversight derive`, "
            "then regenerate this report."
        )
    if warnings:
        lines += [f"- {warning}" for warning in warnings] + [""]
    return lines


def _md_comparison(meta: Mapping[str, Any], comparison: Sequence[ComparisonRow]) -> list[str]:
    lines = ["## Comparison with the reference audit", ""]
    status = meta["comparison_status"]
    if status == "not requested":
        return lines + [
            "Not requested: `python -m oversight baseline --compare` regenerates the reference audit's window and "
            "compares.",
            "",
        ]
    if status == "skipped":
        return lines + [f"Skipped: {_text(meta['comparison_note'])}.", ""]
    rows = [
        [
            row.metric,
            _COMPARED[row.metric].label if row.metric in _COMPARED else "",
            _number(row.expected),
            _number(row.actual),
            "yes" if row.match else "no",
            row.note,
        ]
        for row in comparison
    ]
    lines += _table(
        ["Metric", "What is counted", "Reference audit", "Regenerated", "Match", "Note"], rows, right={2, 3}
    )
    matched = sum(row.match for row in comparison)
    lines += [
        f"{matched} of {len(comparison)} figures match. Each difference has a note; "
        "where the data and the reference audit disagree, the data wins.",
        "",
    ]
    if meta["comparison_note"]:
        lines += [f"Note: {_text(meta['comparison_note'])}.", ""]
    return lines


def _md_snapshot(snapshot: Mapping[str, Any]) -> list[str]:
    lines = ["## Snapshot", ""]
    lines += _table(
        ["Figure", "Issues"],
        [
            ["In the snapshot", _int(snapshot["total_issues"])],
            ["Created before the window end", _int(snapshot["created_before_window_end"])],
        ],
        right={1},
    )
    lines += _count_table("Issue type", "Issues", snapshot["by_type"])
    lines += _count_table("Status", "Issues", snapshot["by_status"])
    lines += _count_table("Status category", "Issues", snapshot["by_status_category"])
    return lines


def _md_resolved(resolved: Mapping[str, Any]) -> list[str]:
    logged = resolved["with_logged_time"]
    straight = resolved["straight_todo_to_done"]
    rows = [
        ("With an original estimate", resolved["with_original_estimate"]),
        ("With story points", resolved["with_story_points"]),
        ("With logged time: time spent", logged["timespent"]),
        ("With logged time: time spent including subtasks", logged["aggregate"]),
        ("With logged time: at least one worklog", logged["worklog_rows"]),
        (
            f"Straight from {_names(straight['from_statuses'], 'or')} to {_names(straight['to_statuses'], 'or')}",
            straight,
        ),
        ("Created in a done status and never moved", resolved["created_in_done"]),
    ]
    lines = [
        "## Resolved in the window",
        "",
        f"Issues whose resolution date is in the window: {_int(resolved['in_window'])}.",
        "",
    ]
    lines += _table(
        ["Figure", "Issues", "Share"],
        [[label, _int(figure["count"]), _pct(figure["share"])] for label, figure in rows],
        right={1, 2},
    )
    lines += [
        "Issues that entered a done status in the window, from the status history "
        f"(an alternative to the resolution date): {_int(resolved['done_entered_in_window'])}.",
        "",
    ]
    return lines


def _md_estimates(estimates: Mapping[str, Any]) -> list[str]:
    per_point = estimates["hours_per_point"]
    within = estimates["within_25pct"]
    late = estimates["estimate_set_after_start"]
    rows = [
        ["Hours per point, median", _hours(per_point["median"])],
        ["Hours per point, p25", _hours(per_point["p25"])],
        ["Hours per point, p75", _hours(per_point["p75"])],
    ]
    if within is not None:
        rows.append(
            [
                f"Within ±25% of {_number(within['hours_per_point'])} hours per point",
                _of(within["count"], per_point["n"], within["share"]),
            ]
        )
    rows.append(["Estimate first set after work started", _of(late["count"], late["of"], late["share"])])
    lines = [
        "## Estimates",
        "",
        "Issues resolved in the window. Hours per point is time spent divided by story points, "
        f"over the {_int(per_point['n'])} issues that have both.",
        "",
    ]
    lines += _table(["Figure", "Value"], rows, right={1})
    lines += ["The last row previews the R-EST-02 pattern; it is not a finding.", ""]
    return lines


def _md_time_in_status(time_in_status: Mapping[str, Mapping[str, Any]]) -> list[str]:
    lines = ["## Time in status", "", "Calendar hours, over the status intervals that ended in the window.", ""]
    if not time_in_status:
        return lines + ["No status interval ended in the window.", ""]
    rows = [
        [status, row["stage"] or "(unmapped)", _int(row["n"]), *_spread_cells(row)]
        for status, row in time_in_status.items()
    ]
    lines += _table(["Status", "Stage", "Intervals", "Median h", "p75 h", "p90 h"], rows, right={2, 3, 4, 5})
    return lines


def _md_cycle_time(cycle_time: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Cycle time",
        "",
        "Calendar hours from the first start of work to done, for the issues resolved in the window "
        "that have both.",
        "",
    ]
    lines += _table(
        ["Issues", "Median h", "p75 h", "p90 h"],
        [[_int(cycle_time["n"]), *_spread_cells(cycle_time)]],
        right={0, 1, 2, 3},
    )
    return lines


def _md_reopens(reopens: Mapping[str, Any]) -> list[str]:
    return [
        "## Reopens",
        "",
        f"Issues resolved in the window and reopened at least once: {_count_and_share(reopens)}.",
        "",
    ]


def _md_worklogs(worklogs: Mapping[str, Any]) -> list[str]:
    top = worklogs["top_author_share"]
    rows = [
        ["Worklogs", _int(worklogs["count"])],
        ["Hours logged", _hours(worklogs["total_hours"])],
        ["Accounts that logged time", _int(worklogs["authors"])],
        ["Most common account: share of hours", _pct(top["seconds"])],
        ["Most common account: share of worklogs", _pct(top["count"])],
    ]
    tempo = worklogs.get("tempo_share")
    if tempo is not None:
        rows += [
            ["Tempo app account: share of hours", _pct(tempo["seconds"])],
            ["Tempo app account: share of worklogs", _pct(tempo["count"])],
        ]
    lines = ["## Worklogs", "", "On the issues resolved in the window. Accounts are counted, never named.", ""]
    lines += _table(["Figure", "Value"], rows, right={1})
    return lines


def _md_evidence(evidence: Mapping[str, list[str]]) -> list[str]:
    missing, configured = evidence["not_measurable"], evidence["configured"]
    lines = ["## Evidence", ""]
    if missing:
        lines.append(
            f"Not measurable: process.yaml has no evidence source for {_names(missing, 'and')}, "
            'so the rules that need them will report "not measurable".'
        )
    if configured:
        lines.append(f"Evidence sources configured: {_names(configured, 'and')}.")
    if not missing and not configured:
        lines.append("process.yaml names no evidence for this project.")
    return lines + [""]


def _md_issue_links(links: Mapping[str, Any]) -> list[str]:
    lines = ["## Issue links", ""]
    lines += _table(
        ["Figure", "Value"],
        [
            ["Issues with at least one issue link", _int(links["issues_with_issue_links"])],
            ["Issue links", _int(links["issue_links"])],
            ["Remote links", _int(links["remote_links"])],
        ],
        right={1},
    )
    lines += _count_table("Link direction", "Links", links["issue_links_by_title"])
    lines += _count_table("Remote link host", "Links", links["remote_links_by_host"])
    return lines


def _table(headers: Sequence[str], rows: Iterable[Sequence[object]], right: Iterable[int] = ()) -> list[str]:
    """A Markdown table, then a blank line; columns in `right` (numbers) are right-aligned."""
    right = set(right)
    lines = [
        "| " + " | ".join(_cell(header) for header in headers) + " |",
        "|" + "|".join("---:" if index in right else "---" for index in range(len(headers))) + "|",
    ]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return lines + [""]


def _count_table(label: str, unit: str, counts: Mapping[str, int]) -> list[str]:
    if not counts:
        return []
    return _table([label, unit], [[name, _int(count)] for name, count in counts.items()], right={1})


# --- small helpers ---------------------------------------------------------------------


def _text(value: object) -> str:
    """Data shown in the report, on one line and HTML-escaped: Jira data is untrusted."""
    return html.escape(" ".join(str(value).split()), quote=False)


def _cell(value: object) -> str:
    """_text for a table cell: a pipe would end the cell."""
    return _text(value).replace("|", "\\|")


def _int(value: int | None) -> str:
    return "n/a" if value is None else f"{value:,}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _hours(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.1f}"


def _spread_cells(spread: Mapping[str, Any]) -> list[str]:
    return [_hours(spread["median"]), _hours(spread["p75"]), _hours(spread["p90"])]


def _number(value: float | None) -> str:
    """A count without decimals, anything else with two."""
    if value is None:
        return "n/a"
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.2f}"


def _of(count: int, whole: int, part: float | None) -> str:
    return f"{count:,} of {whole:,} ({_pct(part)})"


def _count_and_share(figure: Mapping[str, Any] | None) -> str:
    if not figure:
        return "n/a"
    return f"{_int(figure['count'])} ({_pct(figure['share'])})"


def _days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


def _names(names: Sequence[str], joiner: str) -> str:
    if not names:
        return "(none)"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} {joiner} {names[-1]}"


def _local_minute(iso_text: str) -> str:
    """'2026-08-04T00:00:00-05:00' -> '2026-08-04 00:00'."""
    return iso_text[:16].replace("T", " ")


def _count_share(part: int, whole: int) -> dict[str, Any]:
    return {"count": part, "share": share(part, whole)}


def _spread(values: Sequence[float]) -> dict[str, Any]:
    """n, median, p75 and p90, interpolated as oversight.stats does (None when empty)."""
    return {"n": len(values), "median": median(values), "p75": percentile(values, 75), "p90": percentile(values, 90)}


def _counts(values: Iterable[str | None]) -> dict[str, int]:
    counter = Counter(value if value else _NONE for value in values)
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _positive(value: object) -> bool:
    """A number above 0."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _later(first: object, second: object) -> bool:
    """True when both timestamps are set and `first` comes after `second`."""
    a, b = parse_ts(first), parse_ts(second)
    return a is not None and b is not None and a > b


def _by_issue(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["issue_key"]].append(row)
    return dict(grouped)


def _dig(data: Any, *path: str) -> Any:
    """data[path[0]][path[1]]..., or None as soon as a step is missing."""
    for key in path:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data
