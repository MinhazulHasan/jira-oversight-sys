"""The fixed catalogue of read-only, parameter-validated queries behind Ask and the MCP tools."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from oversight.config import ProcessConfig
from oversight.errors import ConfigError
from oversight.rules.catalog import Rule, RulesConfig
from oversight.stats import median, percentile, share
from oversight.timeutil import UTC, iso, to_iso

if TYPE_CHECKING:
    from oversight.ask.people import PeopleScope

ColumnKind = Literal["text", "number", "count", "hours", "share", "ratio", "date", "issue_key"]

# A key that cannot be a Jira key is refused before any query runs.
PROJECT_KEY_PATTERN = r"^[A-Z][A-Z0-9_]{0,19}$"
RULE_ID_PATTERN = r"^[RW]-[A-Z]+-\d{2,3}$"  # a custom rule's id too (R-CUS-01, W-CUS-104)
ISSUE_KEY_PATTERN = r"^[A-Z][A-Z0-9_]+-\d+$"
# Status and issue type names as Jira allows them ("Won't Fix", "Sub-task").
NAME_PATTERN = r"^[\w .,'()/&+-]{1,60}$"

Period = Literal["7d", "30d", "90d", "sprint", "all"]

# Keys quoted in oversight_count_issues' sample.
SAMPLE_KEYS = 20
_PERIOD_DAYS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}
_SPRINT_FALLBACK_DAYS = 14
_NO_EPIC = "(no epic)"
_NO_SPRINT = "(no sprint)"
_NO_TYPE = "(no type)"
# A subtask's epic is its parent's, so queries by epic join the parent (alias p).
_PARENT_JOIN = "LEFT JOIN issues AS p ON p.issue_key = i.parent_key"
_EPIC_OF_I = "COALESCE(i.epic_key, p.epic_key)"
# Fixed English month names: strftime("%b") follows the machine's locale.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: ColumnKind


@dataclass
class Facts:
    ref: str  # "F1", "F2", ... in plan order
    tool: str
    params: dict[str, Any]
    columns: list[Column]
    rows: list[dict[str, Any]]
    truncated: bool = False
    as_of: str | None = None
    note: str | None = None

    @property
    def empty(self) -> bool:
        return not self.rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "tool": self.tool,
            "params": dict(self.params),
            "columns": [asdict(column) for column in self.columns],
            "rows": [dict(row) for row in self.rows],
            "truncated": self.truncated,
            "as_of": self.as_of,
            "note": self.note,
        }


@dataclass
class CatalogContext:
    process: ProcessConfig
    rules: RulesConfig
    project: str
    now: datetime
    row_limit: int = 200
    last_synced_at: str | None = None
    # At people level only: the board's people, for the person tools (oversight/ask/people.py).
    people: PeopleScope | None = None


@dataclass(frozen=True)
class QuerySpec:
    name: str
    description: str
    params_model: type[BaseModel]
    run: Callable[[sqlite3.Connection, BaseModel, CatalogContext], Facts]
    needs_evidence: Callable[[BaseModel, RulesConfig], list[str]] = field(default=lambda p, r: [])


# --- parameter models -------------------------------------------------------------------


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ProjectParams(_Params):
    project: str | None = Field(
        default=None,
        pattern=PROJECT_KEY_PATTERN,
        description="Jira project key, e.g. AIL. Default: the project the question is about.",
    )

    @field_validator("project", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        # Keys are upper case in Jira; "ail" means AIL.
        return value.strip().upper() if isinstance(value, str) else value


class CountIssuesParams(_ProjectParams):
    status: str | None = Field(default=None, pattern=NAME_PATTERN, description="Status name, e.g. In Progress (any case).")
    issue_type: str | None = Field(default=None, pattern=NAME_PATTERN, description="Issue type name, e.g. Bug (any case).")
    period: Period = Field(default="all", description="Only issues created in the period.")
    has_estimate: bool | None = Field(default=None, description="True: only issues with an estimate; false: only without.")
    state: Literal["open", "in_progress", "done"] | None = Field(
        default=None,
        description="By status category, over the board's work (Epics left out), as the Overview's where work sits "
        "and the Portfolio count it: open (not done: open tickets), in_progress (in progress), done. Leave status out.",
    )


class TopIssuesParams(_ProjectParams):
    metric: Literal["cycle_time", "effort_ratio", "status_age", "reopens"]
    n: int = Field(default=10, ge=1, le=20)
    status_category: Literal["new", "indeterminate", "done"] | None = Field(
        default=None, description="new = not started, indeterminate = in progress, done = finished."
    )
    issue_type: str | None = Field(default=None, pattern=NAME_PATTERN)


class TimeInStatusParams(_ProjectParams):
    group_by: Literal["status", "issue_type", "epic"] = "status"
    period: Period = "90d"


class EffortVsEstimateParams(_ProjectParams):
    group_by: Literal["epic", "issue_type", "sprint"] = "epic"
    period: Period = "90d"


class ListFindingsParams(_ProjectParams):
    rule_id: str | None = Field(default=None, pattern=RULE_ID_PATTERN, description="A rule id such as R-EST-01.")
    severity: Literal["breach", "warning"] | None = None
    state: Literal["open", "resolved", "all"] = "open"
    period: Period = Field(default="all", description="Only findings first seen in the period.")
    n: int = Field(default=20, ge=1, le=50)


class IssueTimelineParams(_Params):
    issue_key: str = Field(pattern=ISSUE_KEY_PATTERN, description="An issue key such as AIL-552.")

    @field_validator("issue_key", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class TrendParams(_ProjectParams):
    metric: Literal[
        "median_cycle_time_h",
        "median_effort_ratio",
        "skipped_stage_share",
        "reopen_rate",
        "open_breaches",
        "open_warnings",
    ]
    last_n: int = Field(default=8, ge=1, le=12, description="How many weeks, ending with the current one.")


class CoverageParams(_ProjectParams):
    field: Literal["estimate", "story_points", "time_logged", "code_review", "test_evidence"]
    period: Period = Field(default="90d", description="Only issues created in the period.")


class HotSpotsParams(_ProjectParams):
    n: int = Field(default=5, ge=1, le=10)


class RuleStatusParams(_ProjectParams):
    pass


# --- shared helpers ----------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    """Run one parameterised statement; rows are addressable by column name whatever the connection's factory."""
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, tuple(params)).fetchall()


def _cap(rows: Sequence[Any], limit: int) -> tuple[list[Any], bool]:
    """The first `limit` rows and whether more existed (callers fetch limit + 1)."""
    return list(rows[:limit]), len(rows) > limit


def _clean(value: Any) -> Any:
    """Row values as the facts carry them: floats to 4 decimals (no float noise), everything else as stored."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 4)
    return value


def _row(**values: Any) -> dict[str, Any]:
    return {key: _clean(value) for key, value in values.items()}


def _day(iso_text: str | None) -> str | None:
    """'2026-10-28T09:00:00.000Z' -> '28 Oct 2026'."""
    if not iso_text or len(iso_text) < 10:
        return None
    try:
        year, month, day = int(iso_text[:4]), int(iso_text[5:7]), int(iso_text[8:10])
        return f"{day} {_MONTHS[month - 1]} {year}"
    except (ValueError, IndexError):
        return None


def _as_of(conn: sqlite3.Connection, ctx: CatalogContext, project: str) -> str | None:
    """When the data was last synced: the context's value, else the project's sync cursor, else the last sync run."""
    if ctx.last_synced_at:
        return ctx.last_synced_at
    cursor = _rows(conn, "SELECT last_synced_at FROM sync_state WHERE scope = ?", (project,))
    if cursor and cursor[0]["last_synced_at"]:
        return cursor[0]["last_synced_at"]
    run = _rows(
        conn,
        "SELECT started_at FROM runs WHERE kind = 'sync' AND status = 'succeeded' ORDER BY started_at DESC LIMIT 1",
    )
    return run[0]["started_at"] if run else None


@dataclass(frozen=True)
class _Window:
    since: str | None  # storage-format lower bound, inclusive; None = no bound
    phrase: str  # "the last 30 days", for notes
    note: str | None = None  # anything the reader must know (e.g. the sprint fallback)


_ACTIVE_SPRINT_SQL = """
    SELECT DISTINCT json_extract(t.value, '$.id') AS id, json_extract(t.value, '$.name') AS name,
           json_extract(t.value, '$.startDate') AS start_date, json_extract(t.value, '$.endDate') AS end_date
    FROM issues AS i, json_tree(i.raw_json) AS t
    WHERE i.project_key = ? AND i.raw_json IS NOT NULL AND instr(i.raw_json, 'active') > 0
      AND t.type = 'object' AND json_extract(t.value, '$.state') = 'active'
      AND json_extract(t.value, '$.startDate') IS NOT NULL
    ORDER BY start_date DESC, id DESC
"""


def _window(conn: sqlite3.Connection, ctx: CatalogContext, project: str, period: str) -> _Window:
    if period == "all":
        return _Window(None, "all time")
    if period in _PERIOD_DAYS:
        days = _PERIOD_DAYS[period]
        return _Window(to_iso(ctx.now - timedelta(days=days)), f"the last {days} days")
    # "sprint": the active sprint's start, from the sprint objects stored in raw_json.
    try:
        sprints = _rows(conn, _ACTIVE_SPRINT_SQL, (project,))
    except sqlite3.OperationalError:
        sprints = []  # a raw_json that is not valid JSON: treat as no sprint data
    for sprint in sprints:
        start = iso(sprint["start_date"])
        if start:
            name = sprint["name"] or f"sprint {sprint['id']}"
            return _Window(start, f"the active sprint ({name}, since {_day(start)})")
    since = to_iso(ctx.now - timedelta(days=_SPRINT_FALLBACK_DAYS))
    return _Window(
        since,
        f"the last {_SPRINT_FALLBACK_DAYS} days",
        note=f"No active sprint was found in the snapshot, so the last {_SPRINT_FALLBACK_DAYS} days stand in for it.",
    )


def _excluded_types(ctx: CatalogContext) -> list[str]:
    """Issue types the rules treat as containers, not work (defaults.exclude_types), lower-cased."""
    return [name.casefold() for name in ctx.rules.defaults.exclude_types]


def _type_exclusion(ctx: CatalogContext, alias: str) -> tuple[str, list[str]]:
    """SQL fragment that drops the excluded types; a NULL type is kept."""
    excluded = _excluded_types(ctx)
    if not excluded:
        return "", []
    marks = ", ".join("?" * len(excluded))
    return f" AND ({alias}.issue_type IS NULL OR lower({alias}.issue_type) NOT IN ({marks}))", excluded


def _spread(values: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    return median(values), percentile(values, 75), percentile(values, 90)


def _project_of(params: BaseModel, ctx: CatalogContext) -> str:
    return getattr(params, "project", None) or ctx.project


def board_of(params: BaseModel) -> str | None:
    """The board a call names, if any: its project, or the board of the issue key it reads."""
    project = getattr(params, "project", None)
    if project:
        return str(project)
    issue_key = getattr(params, "issue_key", None)
    return str(issue_key).rsplit("-", 1)[0] if issue_key else None


def _facts(tool: str, params: BaseModel, columns: list[Column], rows: list[dict[str, Any]], **extra: Any) -> Facts:
    return Facts(ref="", tool=tool, params=params.model_dump(mode="json", exclude_none=True), columns=columns, rows=rows, **extra)


# --- oversight_count_issues ----------------------------------------------------------------

_COUNT_COLUMNS = [Column("count", "Issues", "count"), Column("sample_keys", "Sample keys", "text")]


def run_count_issues(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, CountIssuesParams)
    project = _project_of(params, ctx)
    sql = "SELECT issue_key FROM issues WHERE project_key = ?"
    values: list[Any] = [project]
    notes: list[str] = []
    if params.status is not None:
        sql += " AND lower(status) = lower(?)"
        values.append(params.status)
    if params.issue_type is not None:
        sql += " AND lower(issue_type) = lower(?)"
        values.append(params.issue_type)
    if params.state is not None:
        # Open tickets means the board's work by status category, as the Overview and Portfolio count it.
        sql += {"open": " AND status_category != 'done'", "in_progress": " AND status_category = 'indeterminate'",
                "done": " AND status_category = 'done'"}[params.state]
        excluded = [name.casefold() for name in ctx.rules.defaults.exclude_types]
        if params.issue_type is None and excluded:
            sql += f" AND lower(issue_type) NOT IN ({', '.join('?' * len(excluded))})"
            values.extend(excluded)
        words = {"open": "open (status not done)", "in_progress": "in progress", "done": "done"}[params.state]
        left_out = f", {' and '.join(ctx.rules.defaults.exclude_types)} left out" if excluded and params.issue_type is None else ""
        notes.append(f"the board's work {words}{left_out}, as the Overview and the Portfolio count it")
    window = _window(conn, ctx, project, params.period)
    if window.since is not None:
        sql += " AND created_at >= ?"
        values.append(window.since)
        notes.append(f"issues created in {window.phrase}")
    if window.note:
        notes.append(window.note)
    if params.has_estimate is not None:
        estimate = ctx.process.estimate_for(project)
        column = "story_points" if estimate.basis == "points" else "original_estimate_s"
        # Column names come from code; 0 is "no estimate", as in derive.metrics.
        sql += f" AND {column} > 0" if params.has_estimate else f" AND ({column} IS NULL OR {column} <= 0)"
        notes.append(f"estimate basis: {estimate.basis}")
    sql += " ORDER BY created_at DESC, issue_key DESC"
    keys = [row["issue_key"] for row in _rows(conn, sql, values)]
    if len(keys) > SAMPLE_KEYS:
        notes.append(f"sample_keys lists the {SAMPLE_KEYS} most recent of {len(keys):,} issues")
    row = _row(count=len(keys), sample_keys=", ".join(keys[:SAMPLE_KEYS]))
    return _facts(
        "oversight_count_issues",
        params,
        _COUNT_COLUMNS,
        [row],
        as_of=_as_of(conn, ctx, project),
        note="; ".join(notes) or None,
    )


# --- oversight_top_issues ------------------------------------------------------------------

# metric -> (issue_metrics column, value kind, unit, description)
_METRICS: dict[str, tuple[str, ColumnKind, str, str]] = {
    "cycle_time": ("cycle_time_h", "hours", "h", "hours from the start of development to done"),
    "effort_ratio": ("effort_ratio", "ratio", "×", "logged time divided by the estimate"),
    "status_age": ("status_age_h", "hours", "h", "hours in the current status"),
    "reopens": ("reopen_count", "count", "reopens", "times reopened after done (at least once)"),
}


def run_top_issues(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, TopIssuesParams)
    project = _project_of(params, ctx)
    column, kind, unit, meaning = _METRICS[params.metric]
    sql = (
        f"SELECT i.issue_key, i.summary, i.issue_type, i.status, {_EPIC_OF_I} AS epic_key, m.{column} AS value"
        " FROM issues AS i JOIN issue_metrics AS m ON m.issue_key = i.issue_key"
        f" {_PARENT_JOIN}"
        f" WHERE i.project_key = ? AND m.{column} IS NOT NULL"
    )
    values: list[Any] = [project]
    if params.metric == "reopens":
        sql += f" AND m.{column} > 0"
    if params.status_category is not None:
        sql += " AND i.status_category = ?"
        values.append(params.status_category)
    if params.issue_type is not None:
        sql += " AND lower(i.issue_type) = lower(?)"
        values.append(params.issue_type)
    else:
        exclusion, excluded = _type_exclusion(ctx, "i")
        sql += exclusion
        values.extend(excluded)
    limit = min(params.n, ctx.row_limit)
    sql += " ORDER BY value DESC, i.issue_key LIMIT ?"
    values.append(limit + 1)
    fetched, truncated = _cap(_rows(conn, sql, values), limit)
    rows = [
        _row(
            issue_key=r["issue_key"],
            summary=r["summary"],
            issue_type=r["issue_type"],
            status=r["status"],
            epic_key=r["epic_key"],
            value=r["value"],
            unit=unit,
        )
        for r in fetched
    ]
    columns = [
        Column("issue_key", "Issue", "issue_key"),
        Column("summary", "Summary", "text"),
        Column("issue_type", "Type", "text"),
        Column("status", "Status", "text"),
        Column("epic_key", "Epic", "issue_key"),
        Column("value", params.metric.replace("_", " ").capitalize(), kind),
        Column("unit", "Unit", "text"),
    ]
    note = f"value = {meaning}, highest first"
    if params.issue_type is None and _excluded_types(ctx):
        note += f"; {', '.join(ctx.rules.defaults.exclude_types)} issues left out (containers, not work)"
    return _facts("oversight_top_issues", params, columns, rows, truncated=truncated, as_of=_as_of(conn, ctx, project), note=note)


# --- oversight_time_in_status --------------------------------------------------------------

_INTERVALS_SQL = f"""
    SELECT si.status, si.stage, si.hours, i.issue_type, {_EPIC_OF_I} AS epic_key
    FROM status_intervals AS si JOIN issues AS i ON i.issue_key = si.issue_key
    {_PARENT_JOIN}
    WHERE i.project_key = ? AND si.left_at IS NOT NULL AND si.hours IS NOT NULL
"""
_SPREAD_COLUMNS = [
    Column("group", "Group", "text"),
    Column("n", "Intervals", "count"),
    Column("median_h", "Median (h)", "hours"),
    Column("p75_h", "p75 (h)", "hours"),
    Column("p90_h", "p90 (h)", "hours"),
]


def run_time_in_status(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, TimeInStatusParams)
    project = _project_of(params, ctx)
    window = _window(conn, ctx, project, params.period)
    sql = _INTERVALS_SQL
    values: list[Any] = [project]
    if window.since is not None:
        sql += " AND si.left_at >= ?"
        values.append(window.since)
    intervals = _rows(conn, sql, values)
    notes = [f"closed status intervals that ended in {window.phrase}"]
    if window.note:
        notes.append(window.note)

    hours: dict[str, list[float]] = defaultdict(list)
    stage_of: dict[str, str | None] = {}
    config = ctx.process.project(project) if project in ctx.process.projects else None
    if params.group_by == "status":
        # Time in the done stage is the wait before a reopen, not time in the process.
        done = ctx.process.done_stage
        for interval in intervals:
            if interval["stage"] == done:
                continue
            hours[interval["status"]].append(interval["hours"])
            stage_of.setdefault(interval["status"], interval["stage"])
        rank = {stage: index for index, stage in enumerate(ctx.process.stages)}
        # James's stage order, unmapped statuses last (as the baseline report does).
        ordered = sorted(hours, key=lambda status: (rank.get(stage_of[status] or "", len(rank)), status))
        done_statuses = [s for s, stage in config.status_to_stage.items() if stage == done] if config else []
        notes.append(f"time in the done stage ({', '.join(done_statuses) or done}) is left out")
    else:
        # By type or epic, only the work stages count: development up to, not including, done.
        work = set(ctx.process.work_stages())
        for interval in intervals:
            if interval["stage"] not in work:
                continue
            group = interval["issue_type"] or _NO_TYPE if params.group_by == "issue_type" else interval["epic_key"] or _NO_EPIC
            hours[group].append(interval["hours"])
        ordered = sorted(hours, key=lambda group: (-(median(hours[group]) or 0.0), group))
        statuses = [s for s, stage in config.status_to_stage.items() if stage in work] if config else []
        notes.append(f"hours in the work stages ({', '.join(statuses) or ', '.join(sorted(work))})")

    rows = []
    for group in ordered:
        med, p75, p90 = _spread(hours[group])
        rows.append(_row(group=group, n=len(hours[group]), median_h=med, p75_h=p75, p90_h=p90))
    rows, truncated = _cap(rows, ctx.row_limit)
    return _facts(
        "oversight_time_in_status",
        params,
        _SPREAD_COLUMNS,
        rows,
        truncated=truncated,
        as_of=_as_of(conn, ctx, project),
        note="; ".join(notes),
    )


# --- oversight_effort_vs_estimate ----------------------------------------------------------

# A ratio of 0 is an estimate with nothing logged, missing data that would drag medians down.
_DONE_RATIOS_SQL = f"""
    SELECT i.issue_key, i.issue_type, {_EPIC_OF_I} AS epic_key, e.summary AS epic_title, i.sprint_ids, m.effort_ratio
    FROM issues AS i JOIN issue_metrics AS m ON m.issue_key = i.issue_key
    {_PARENT_JOIN}
    LEFT JOIN issues AS e ON e.issue_key = {_EPIC_OF_I}
    WHERE i.project_key = ? AND m.done_at IS NOT NULL AND m.effort_ratio IS NOT NULL AND i.time_spent_s > 0
"""
_EFFORT_FIGURES = [
    Column("n", "Issues", "count"),
    Column("median_ratio", "Median ratio", "ratio"),
    Column("share_over_threshold", "Share over threshold", "share"),
    Column("threshold", "Threshold", "ratio"),
]
_EFFORT_COLUMNS = [Column("group", "Group", "text"), *_EFFORT_FIGURES]
# By epic the group is the epic's key, so an answer can name (and cite) the epic it is about.
_EFFORT_EPIC_COLUMNS = [Column("group", "Epic", "issue_key"), Column("epic_title", "Epic title", "text"), *_EFFORT_FIGURES]
_EFFORT_RULE = "R-EFF-01"
_DEFAULT_THRESHOLD = 1.5


def _effort_threshold(rules: RulesConfig) -> float:
    try:
        threshold = rules.by_id(_EFFORT_RULE).params.get("threshold")
    except ConfigError:
        threshold = None
    return float(threshold) if isinstance(threshold, (int, float)) and not isinstance(threshold, bool) else _DEFAULT_THRESHOLD


def _last_sprint(sprint_ids: str | None) -> str:
    """The sprint an issue finished in: the last id of its sprint list."""
    try:
        ids = json.loads(sprint_ids) if sprint_ids else []
    except ValueError:
        ids = []
    return f"Sprint {ids[-1]}" if isinstance(ids, list) and ids else _NO_SPRINT


def run_effort_vs_estimate(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, EffortVsEstimateParams)
    project = _project_of(params, ctx)
    window = _window(conn, ctx, project, params.period)
    sql = _DONE_RATIOS_SQL
    values: list[Any] = [project]
    if window.since is not None:
        sql += " AND m.done_at >= ?"
        values.append(window.since)
    threshold = _effort_threshold(ctx.rules)
    by_epic = params.group_by == "epic"
    ratios: dict[str, list[float]] = defaultdict(list)
    epic_titles: dict[str, str | None] = {_NO_EPIC: _NO_EPIC}
    for issue in _rows(conn, sql, values):
        if by_epic:
            group = issue["epic_key"] or _NO_EPIC
            epic_titles.setdefault(group, issue["epic_title"])
        elif params.group_by == "issue_type":
            group = issue["issue_type"] or _NO_TYPE
        else:
            group = _last_sprint(issue["sprint_ids"])
        ratios[group].append(issue["effort_ratio"])
    ordered = sorted(ratios, key=lambda group: (-(median(ratios[group]) or 0.0), group))
    rows = []
    for group in ordered:
        over = sum(1 for ratio in ratios[group] if ratio >= threshold)
        labels = {"group": group, "epic_title": epic_titles[group]} if by_epic else {"group": group}
        rows.append(
            _row(
                **labels,
                n=len(ratios[group]),
                median_ratio=median(ratios[group]),
                share_over_threshold=share(over, len(ratios[group])),
                threshold=threshold,
            )
        )
    rows, truncated = _cap(rows, ctx.row_limit)
    notes = [
        f"done work finished in {window.phrase} that has both an estimate and logged time",
        f"ratio = logged hours / estimated hours; threshold from {_EFFORT_RULE}",
    ]
    if window.note:
        notes.append(window.note)
    return _facts(
        "oversight_effort_vs_estimate",
        params,
        _EFFORT_EPIC_COLUMNS if by_epic else _EFFORT_COLUMNS,
        rows,
        truncated=truncated,
        as_of=_as_of(conn, ctx, project),
        note="; ".join(notes),
    )


# --- oversight_list_findings ---------------------------------------------------------------

_FINDINGS_SQL = f"""
    SELECT f.finding_id, f.rule_id, f.severity, f.issue_key, f.message, f.first_seen_at, f.resolved_at,
           i.summary, {_EPIC_OF_I} AS epic_key
    FROM findings AS f LEFT JOIN issues AS i ON i.issue_key = f.issue_key
    {_PARENT_JOIN}
    WHERE f.project_key = ?
"""
_FINDING_COLUMNS = [
    Column("finding_id", "Finding", "text"),
    Column("rule_id", "Rule", "text"),
    Column("rule_title", "Rule title", "text"),
    Column("severity", "Severity", "text"),
    Column("issue_key", "Issue", "issue_key"),
    Column("summary", "Summary", "text"),
    Column("epic_key", "Epic", "issue_key"),
    Column("message", "Message", "text"),
    Column("first_seen_at", "First seen", "date"),
    Column("resolved_at", "Resolved", "date"),
]


def _rule_title(rules: RulesConfig, rule_id: str) -> str:
    """The rule's title as the board names it, deleted custom rules included; the id for an unknown rule."""
    return rules.title_of(rule_id)


def run_list_findings(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, ListFindingsParams)
    project = _project_of(params, ctx)
    sql = _FINDINGS_SQL
    values: list[Any] = [project]
    described = [params.state + " findings"]
    if params.rule_id is not None:
        sql += " AND f.rule_id = ?"
        values.append(params.rule_id)
        described.append(f"rule {params.rule_id} ({_rule_title(ctx.rules, params.rule_id)})")
    if params.severity is not None:
        sql += " AND f.severity = ?"
        values.append(params.severity)
    if params.state == "open":
        sql += " AND f.resolved_at IS NULL"
    elif params.state == "resolved":
        sql += " AND f.resolved_at IS NOT NULL"
    window = _window(conn, ctx, project, params.period)
    if window.since is not None:
        sql += " AND f.first_seen_at >= ?"
        values.append(window.since)
        described.append(f"first seen in {window.phrase}")
    limit = min(params.n, ctx.row_limit)
    sql += " ORDER BY (f.resolved_at IS NULL) DESC, f.first_seen_at DESC, f.finding_id LIMIT ?"
    values.append(limit + 1)
    fetched, truncated = _cap(_rows(conn, sql, values), limit)
    rows = [
        _row(
            finding_id=r["finding_id"],
            rule_id=r["rule_id"],
            rule_title=_rule_title(ctx.rules, r["rule_id"]),
            severity=r["severity"],
            issue_key=r["issue_key"],
            summary=r["summary"],
            epic_key=r["epic_key"],
            message=r["message"],
            first_seen_at=r["first_seen_at"],
            resolved_at=r["resolved_at"],
        )
        for r in fetched
    ]
    notes = [", ".join(described) + ", newest first"]
    if window.note:
        notes.append(window.note)
    return _facts(
        "oversight_list_findings",
        params,
        _FINDING_COLUMNS,
        rows,
        truncated=truncated,
        as_of=_as_of(conn, ctx, project),
        note="; ".join(notes),
    )


def _findings_evidence(params: BaseModel, rules: RulesConfig) -> list[str]:
    """Listing one rule's findings needs whatever evidence that rule needs."""
    rule_id = getattr(params, "rule_id", None)
    if not rule_id:
        return []
    try:
        return list(rules.by_id(rule_id).requires_evidence)
    except ConfigError:
        return []  # an unknown rule has no findings; the query returns none


# --- oversight_issue_timeline --------------------------------------------------------------

_TIMELINE_COLUMNS = [
    Column("seq", "Step", "count"),
    Column("status", "Status", "text"),
    Column("stage", "Stage", "text"),
    Column("entered_at", "Entered", "date"),
    Column("left_at", "Left", "date"),
    Column("hours", "Hours", "hours"),
]
_ISSUE_FINDINGS_SQL = """
    SELECT rule_id, resolved_at FROM findings WHERE issue_key = ?
    ORDER BY (resolved_at IS NULL) DESC, first_seen_at, finding_id
"""


def run_issue_timeline(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, IssueTimelineParams)
    key = params.issue_key
    project = key.rsplit("-", 1)[0]
    exists = bool(_rows(conn, "SELECT 1 FROM issues WHERE issue_key = ?", (key,)))
    intervals = _rows(
        conn,
        "SELECT seq, status, stage, entered_at, left_at, hours FROM status_intervals WHERE issue_key = ? ORDER BY seq",
        (key,),
    )
    rows = [_row(**dict(interval)) for interval in intervals]
    rows, truncated = _cap(rows, ctx.row_limit)
    if not exists:
        note = f"{key} is not in the snapshot"
    else:
        findings = _rows(conn, _ISSUE_FINDINGS_SQL, (key,))
        if findings:
            listed = "; ".join(
                f"{f['rule_id']} {_rule_title(ctx.rules, f['rule_id'])} ({'open' if f['resolved_at'] is None else 'resolved'})"
                for f in findings
            )
            note = f"Findings for {key}: {listed}"
        else:
            note = f"No findings for {key}"
        if not rows:
            note += "; no timeline yet (run derive)"
    return _facts(
        "oversight_issue_timeline",
        params,
        _TIMELINE_COLUMNS,
        rows,
        truncated=truncated,
        as_of=_as_of(conn, ctx, project),
        note=note,
    )


# --- oversight_trend -----------------------------------------------------------------------

_TREND_KINDS: dict[str, ColumnKind] = {
    "median_cycle_time_h": "hours",
    "median_effort_ratio": "ratio",
    "skipped_stage_share": "share",
    "reopen_rate": "share",
    "open_breaches": "count",
    "open_warnings": "count",
}
_TREND_MEANING: dict[str, str] = {
    "median_cycle_time_h": "median cycle time (h) of work finished in the week",
    "median_effort_ratio": "median effort ratio of work finished in the week that has both an estimate and logged time",
    "skipped_stage_share": "share of work finished in the week that skipped a stage",
    "reopen_rate": "share of work finished in the week that had been reopened",
    "open_breaches": "breach findings open at the end of the week",
    "open_warnings": "warning findings open at the end of the week",
}
_DONE_IN_WEEKS_SQL = """
    SELECT m.done_at, m.cycle_time_h, m.effort_ratio, m.skipped_stages, m.reopen_count, i.time_spent_s
    FROM issue_metrics AS m JOIN issues AS i ON i.issue_key = m.issue_key
    WHERE i.project_key = ? AND m.done_at >= ? AND m.done_at < ?
"""
_FINDINGS_IN_WEEKS_SQL = """
    SELECT first_seen_at, resolved_at FROM findings
    WHERE project_key = ? AND severity = ? AND first_seen_at IS NOT NULL AND first_seen_at < ?
"""


def _week_starts(now: datetime, last_n: int) -> list[datetime]:
    """Mondays (UTC midnight) of the last_n weeks, oldest first, ending with the current week."""
    today = now.astimezone(UTC).date()
    current = datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time(), tzinfo=UTC)
    return [current - timedelta(weeks=last_n - 1 - index) for index in range(last_n)]


def _has_skip(skipped_stages: str | None) -> bool:
    try:
        return bool(json.loads(skipped_stages)) if skipped_stages else False
    except ValueError:
        return False


def run_trend(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, TrendParams)
    project = _project_of(params, ctx)
    weeks = _week_starts(ctx.now, params.last_n)
    bounds = [(to_iso(start), to_iso(start + timedelta(weeks=1))) for start in weeks]
    values: list[float | None] = []
    if params.metric in ("open_breaches", "open_warnings"):
        severity = "breach" if params.metric == "open_breaches" else "warning"
        findings = _rows(conn, _FINDINGS_IN_WEEKS_SQL, (project, severity, bounds[-1][1]))
        for _start, end in bounds:
            values.append(
                sum(
                    1
                    for f in findings
                    if f["first_seen_at"] < end and (f["resolved_at"] is None or f["resolved_at"] >= end)
                )
            )
    else:
        done = _rows(conn, _DONE_IN_WEEKS_SQL, (project, bounds[0][0], bounds[-1][1]))
        for start, end in bounds:
            in_week = [d for d in done if start <= d["done_at"] < end]
            if params.metric == "median_cycle_time_h":
                values.append(median([d["cycle_time_h"] for d in in_week if d["cycle_time_h"] is not None]))
            elif params.metric == "median_effort_ratio":
                # As oversight_effort_vs_estimate: an estimate with nothing logged is missing data, not a 0 ratio.
                values.append(
                    median([d["effort_ratio"] for d in in_week if d["effort_ratio"] is not None and (d["time_spent_s"] or 0) > 0])
                )
            elif params.metric == "skipped_stage_share":
                values.append(share(sum(1 for d in in_week if _has_skip(d["skipped_stages"])), len(in_week)))
            else:
                values.append(share(sum(1 for d in in_week if (d["reopen_count"] or 0) > 0), len(in_week)))
    rows = [_row(week_of=start.date().isoformat(), value=value) for start, value in zip(weeks, values, strict=True)]
    rows, truncated = _cap(rows, ctx.row_limit)
    columns = [Column("week_of", "Week of", "date"), Column("value", params.metric.replace("_", " "), _TREND_KINDS[params.metric])]
    note = f"{_TREND_MEANING[params.metric]}; weeks start on Monday (UTC), oldest first"
    if params.metric not in ("open_breaches", "open_warnings"):
        note += "; null = nothing finished that week"
    return _facts(
        "oversight_trend",
        params,
        columns,
        rows,
        truncated=truncated,
        as_of=_as_of(conn, ctx, project),
        note=note,
    )


# --- oversight_coverage --------------------------------------------------------------------

_COVERAGE_COLUMNS = [
    Column("covered", "Covered", "count"),
    Column("total", "Total", "count"),
    Column("share", "Share", "share"),
    Column("measurable", "Measurable", "text"),
    Column("note", "Note", "text"),
]
# field -> the evidence name in process.yaml it depends on, if any.
_COVERAGE_EVIDENCE: dict[str, str] = {"code_review": "code_review", "test_evidence": "testing"}


def _issues_with_evidence(conn: sqlite3.Connection, project: str, source: Mapping[str, Any]) -> set[str]:
    """Issue keys that carry the evidence, for each source shape process.yaml allows (rules/evidence.py)."""
    if "status" in source:
        sql = (
            "SELECT DISTINCT si.issue_key FROM status_intervals AS si JOIN issues AS i ON i.issue_key = si.issue_key"
            " WHERE i.project_key = ? AND lower(si.status) = lower(?)"
        )
        value = source["status"]
    elif "remote_link_contains" in source:
        sql = (
            "SELECT DISTINCT l.issue_key FROM links AS l JOIN issues AS i ON i.issue_key = l.issue_key"
            " WHERE i.project_key = ? AND l.kind = 'remote_link' AND instr(l.target, ?) > 0"
        )
        value = source["remote_link_contains"]
    elif "field" in source:
        sql = (
            "SELECT DISTINCT fc.issue_key FROM field_changes AS fc JOIN issues AS i ON i.issue_key = fc.issue_key"
            " WHERE i.project_key = ? AND fc.field = ? AND fc.to_value IS NOT NULL AND fc.to_value != ''"
        )
        value = source["field"]
    elif "label" in source:
        sql = (
            "SELECT DISTINCT i.issue_key FROM issues AS i, json_each(json_extract(i.raw_json, '$.fields.labels')) AS je"
            " WHERE i.project_key = ? AND i.raw_json IS NOT NULL AND je.value = ?"
        )
        value = source["label"]
    else:
        raise ConfigError(f"unknown evidence source {dict(source)!r} in process.yaml")
    return {row["issue_key"] for row in _rows(conn, sql, (project, str(value)))}


def run_coverage(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, CoverageParams)
    project = _project_of(params, ctx)
    window = _window(conn, ctx, project, params.period)
    sql = "SELECT issue_key, story_points, original_estimate_s, time_spent_s FROM issues AS i WHERE i.project_key = ?"
    values: list[Any] = [project]
    if window.since is not None:
        sql += " AND i.created_at >= ?"
        values.append(window.since)
    exclusion, excluded = _type_exclusion(ctx, "i")
    sql += exclusion
    values.extend(excluded)
    issues = _rows(conn, sql, values)
    total = len(issues)
    scope = f"issues created in {window.phrase}"
    if excluded:
        scope += f", {', '.join(ctx.rules.defaults.exclude_types)} left out"

    covered: int | None
    measurable = True
    if params.field in _COVERAGE_EVIDENCE:
        evidence = _COVERAGE_EVIDENCE[params.field]
        source = ctx.process.project(project).evidence.get(evidence) if project in ctx.process.projects else None
        if source is None:
            measurable = False
            covered = None
            note = f"Jira holds no {evidence} evidence for {project} (process.yaml: evidence.{evidence} is null); {scope}"
        else:
            with_evidence = _issues_with_evidence(conn, project, source)
            covered = sum(1 for issue in issues if issue["issue_key"] in with_evidence)
            note = f"issues with {evidence} evidence ({', '.join(f'{k}: {v}' for k, v in source.items())}); {scope}"
    else:
        if params.field == "estimate":
            estimate = ctx.process.estimate_for(project)
            column = "story_points" if estimate.basis == "points" else "original_estimate_s"
            what = f"an estimate ({estimate.basis})"
        elif params.field == "story_points":
            column, what = "story_points", "story points"
        else:
            column, what = "time_spent_s", "logged time"
        covered = sum(1 for issue in issues if (issue[column] or 0) > 0)
        note = f"issues with {what}; {scope}"
    if window.note:
        note += f"; {window.note}"
    row = _row(covered=covered, total=total, share=share(covered, total) if covered is not None else None, measurable=measurable, note=note)
    return _facts("oversight_coverage", params, _COVERAGE_COLUMNS, [row], as_of=_as_of(conn, ctx, project), note=note)


def _coverage_evidence(params: BaseModel, rules: RulesConfig) -> list[str]:
    evidence = _COVERAGE_EVIDENCE.get(getattr(params, "field", ""))
    return [evidence] if evidence else []


# --- oversight_hot_spots -------------------------------------------------------------------

# A LEFT JOIN: a sprint finding has no issue row, so it lands in the no-epic group.
_OPEN_BY_EPIC_SQL = f"""
    SELECT {_EPIC_OF_I} AS epic_key, e.summary AS epic_title, f.severity, f.issue_key
    FROM findings AS f
    LEFT JOIN issues AS i ON i.issue_key = f.issue_key
    {_PARENT_JOIN}
    LEFT JOIN issues AS e ON e.issue_key = {_EPIC_OF_I}
    WHERE f.project_key = ? AND f.resolved_at IS NULL
"""
_SPRINT_SCOPED = "sprint:"
_HOT_SPOT_COLUMNS = [
    Column("epic_key", "Epic", "issue_key"),
    Column("epic_title", "Epic title", "text"),
    Column("open_breaches", "Open breaches", "count"),
    Column("open_warnings", "Open warnings", "count"),
    Column("issues", "Issues with findings", "count"),
]


def run_hot_spots(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, HotSpotsParams)
    project = _project_of(params, ctx)
    breaches: dict[str | None, int] = defaultdict(int)
    warnings: dict[str | None, int] = defaultdict(int)
    issues: dict[str | None, set[str]] = defaultdict(set)
    titles: dict[str | None, str | None] = {}
    for finding in _rows(conn, _OPEN_BY_EPIC_SQL, (project,)):
        epic = finding["epic_key"]
        titles.setdefault(epic, finding["epic_title"])
        if finding["severity"] == "breach":
            breaches[epic] += 1
        else:
            warnings[epic] += 1
        # Every group with a finding is listed, even one holding sprint findings only; a sprint is not an issue.
        group = issues.setdefault(epic, set())
        if not finding["issue_key"].startswith(_SPRINT_SCOPED):
            group.add(finding["issue_key"])
    # Work outside any epic comes after the epics, so the top hot spot always names an epic.
    ordered = sorted(issues, key=lambda epic: (epic is None, -breaches[epic], -warnings[epic], epic or ""))
    limit = min(params.n, ctx.row_limit)
    rows = [
        _row(
            epic_key=epic,
            epic_title=titles[epic] if epic is not None else _NO_EPIC,
            open_breaches=breaches[epic],
            open_warnings=warnings[epic],
            issues=len(issues[epic]),
        )
        for epic in ordered
    ]
    rows, truncated = _cap(rows, limit)
    return _facts(
        "oversight_hot_spots",
        params,
        _HOT_SPOT_COLUMNS,
        rows,
        truncated=truncated,
        as_of=_as_of(conn, ctx, project),
        note=(
            "epics ranked by open breaches, then open warnings; findings outside any epic are grouped as (no epic), "
            "after the epics, sprint findings included (they count as findings, not as issues)"
        ),
    )


# --- oversight_rule_status -----------------------------------------------------------------

_RULE_STATUS_COLUMNS = [
    Column("rule_id", "Rule", "text"),
    Column("title", "Title", "text"),
    Column("kind", "Kind", "text"),
    Column("severity", "Severity", "text"),
    Column("measurable", "Measurable", "text"),
    Column("missing", "Missing", "text"),
    Column("open_findings", "Open findings", "count"),
    Column("version", "Version", "text"),
]


def rule_missing(rule: Rule, process: ProcessConfig, project: str) -> list[str]:
    """What a rule needs that the board does not record; any of it makes the rule not measurable."""
    configured = project in process.projects
    mapped = set(process.ordered_stages(project)) if configured else set()
    missing = [name for name in rule.requires_evidence if not (configured and process.has_evidence(project, name))]
    missing += [stage for stage in rule.requires_stages if stage not in mapped and stage not in missing]
    return missing


def open_findings_by_rule(conn: sqlite3.Connection, project: str) -> dict[str, int]:
    """Each rule's open findings on the board, of every rule_version (as the findings page counts them)."""
    return {
        row["rule_id"]: int(row["n"])
        for row in _rows(
            conn,
            "SELECT rule_id, COUNT(*) AS n FROM findings WHERE project_key = ? AND resolved_at IS NULL GROUP BY rule_id",
            (project,),
        )
    }


def run_rule_status(conn: sqlite3.Connection, params: BaseModel, ctx: CatalogContext) -> Facts:
    assert isinstance(params, RuleStatusParams)
    project = _project_of(params, ctx)
    open_counts = open_findings_by_rule(conn, project)
    configured = project in ctx.process.projects
    rows = []
    for rule in ctx.rules.enabled_rules:
        missing = rule_missing(rule, ctx.process, project)
        rows.append(
            _row(
                rule_id=rule.id,
                title=rule.title,
                kind=rule.kind,
                severity=rule.severity,
                measurable=not missing,
                missing=", ".join(missing) or None,
                open_findings=open_counts.get(rule.id, 0),
                version=ctx.rules.version,
            )
        )
    rows, truncated = _cap(rows, ctx.row_limit)
    note = "enabled rules; a rule is not measurable when Jira records no evidence for a stage it needs"
    if not configured:
        note += f"; {project} is not configured in process.yaml"
    return _facts(
        "oversight_rule_status",
        params,
        _RULE_STATUS_COLUMNS,
        rows,
        truncated=truncated,
        as_of=_as_of(conn, ctx, project),
        note=note,
    )


# --- the catalogue --------------------------------------------------------------------------

CATALOG: dict[str, QuerySpec] = {
    spec.name: spec
    for spec in (
        QuerySpec(
            "oversight_count_issues",
            "How many issues match a status, type, creation period or estimate presence, with up to 20 sample keys.",
            CountIssuesParams,
            run_count_issues,
        ),
        QuerySpec(
            "oversight_top_issues",
            "The issues ranked highest on one metric: cycle_time (hours), effort_ratio (logged / estimate), "
            "status_age (hours in the current status) or reopens.",
            TopIssuesParams,
            run_top_issues,
        ),
        QuerySpec(
            "oversight_time_in_status",
            "Median, p75 and p90 hours spent in each status (or per issue type or epic) over intervals that ended in the period. "
            "Use it for where work gets stuck or sits longest.",
            TimeInStatusParams,
            run_time_in_status,
        ),
        QuerySpec(
            "oversight_effort_vs_estimate",
            "Logged effort against the estimate for work finished in the period, per epic, issue type or sprint: "
            "median ratio and the share at or over the breach threshold.",
            EffortVsEstimateParams,
            run_effort_vs_estimate,
        ),
        QuerySpec(
            "oversight_list_findings",
            "Rule findings (breaches and warnings) with their issues and messages, filtered by rule, severity, state and period.",
            ListFindingsParams,
            run_list_findings,
            _findings_evidence,
        ),
        QuerySpec(
            "oversight_issue_timeline",
            "One issue's status timeline (each status, its stage, when it was entered and left, hours) and its findings.",
            IssueTimelineParams,
            run_issue_timeline,
        ),
        QuerySpec(
            "oversight_trend",
            "A weekly series (weeks start on Monday) of one metric: median cycle time, median effort ratio, "
            "skipped-stage share, reopen rate, open breaches or open warnings.",
            TrendParams,
            run_trend,
        ),
        QuerySpec(
            "oversight_coverage",
            "The share of issues created in the period that have an estimate, story points, logged time, "
            "code review evidence or test evidence; says when Jira can't measure it.",
            CoverageParams,
            run_coverage,
            _coverage_evidence,
        ),
        QuerySpec(
            "oversight_hot_spots",
            "The epics with the most open breaches and warnings.",
            HotSpotsParams,
            run_hot_spots,
        ),
        QuerySpec(
            "oversight_rule_status",
            "Every rule in the catalogue: whether Jira can measure it, what evidence is missing, and how many findings are open.",
            RuleStatusParams,
            run_rule_status,
        ),
    )
}


def catalog_schema() -> list[dict[str, Any]]:
    """[{name, description, input_schema}] for every spec, in CATALOG order."""
    return [
        {"name": spec.name, "description": spec.description, "input_schema": spec.params_model.model_json_schema()}
        for spec in CATALOG.values()
    ]


def validate_params(name: str, params: Mapping[str, Any] | Any, catalog: Mapping[str, QuerySpec] = CATALOG) -> BaseModel:
    """The spec's validated parameter model; ValueError in plain words for an unknown spec or bad parameters."""
    spec = catalog.get(name)
    if spec is None:
        raise ValueError(f"unknown query {name!r}")
    try:
        return spec.params_model.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"{name}: {_plain_problems(exc)}") from None


def run_query(conn: sqlite3.Connection, name: str, params: dict[str, Any], ctx: CatalogContext, *, ref: str) -> Facts:
    """Validate params with the spec's model (ValueError on failure) and run the spec."""
    model = validate_params(name, params)
    facts = CATALOG[name].run(conn, model, ctx)
    facts.ref = ref
    return facts


def _plain_problems(exc: ValidationError) -> str:
    """Pydantic's errors as one readable sentence: 'n must be <= 20; unknown parameter foo'."""
    parts = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ())) or "parameters"
        if error.get("type") == "extra_forbidden":
            parts.append(f"unknown parameter {where}")
        elif error.get("type") == "missing":
            parts.append(f"{where} is required")
        else:
            parts.append(f"{where}: {error.get('msg', 'invalid value')}")
    return "; ".join(parts)
