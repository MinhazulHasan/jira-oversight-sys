"""Weekly insights: candidates and ranking in code, a model-written narrative checked by the Ask validator."""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oversight.ask.catalog import Column, ColumnKind, Facts
from oversight.ask.prompts import INSIGHTS_SYSTEM, insights_user
from oversight.ask.validate import KEY, Check, validate
from oversight.config import ProcessConfig
from oversight.errors import ConfigError
from oversight.llm import audit as llm_audit
from oversight.llm.budget import SCRIPTED, budget_reached
from oversight.llm.provider import LLMError, LLMProvider, LLMRefusal, StructuredResult
from oversight.logs import get_logger
from oversight.rules.catalog import Rule, RulesConfig
from oversight.rules.settings import effective_rules, load_board_settings
from oversight.settings import Settings, validate_project_keys
from oversight.stats import median, percentile
from oversight.store.db import Store
from oversight.timeutil import to_iso, utcnow, zone

log = get_logger("insights")

Tone = Literal["blatantly_bad", "trending_bad", "neutral"]
CandidateKind = Literal["wow_change", "breach_hot_spot", "rising_rule", "outlier", "coverage"]
SEVERITY: dict[str, int] = {"breach": 3, "warning": 2, "info": 1}

TOP = 5
_WEEK = timedelta(days=7)
_MIN_INTERVALS = 5  # wow_change: closed intervals a week needs before its median means anything
_HOT_SPOTS = 3  # breach_hot_spot: how many epics
_MIN_OPEN_WORK = 10  # outlier: open issues (and known values per metric) a p90 needs
_OUTLIERS_LISTED = 5  # outlier: issues spelled out in detail
_COVERAGE_TARGET = 0.8
_COVERAGE_FALLBACK = timedelta(days=30)
_NEW_TREND = 2.0  # the trend when the comparison week had none: "new" weighs as doubling
_MAX_TOKENS = 1200
_KEYS_IN_TABLE = 5
_TONE_FOR: dict[str, Tone] = {"breach": "blatantly_bad", "warning": "trending_bad"}
_TONES: dict[str, Tone] = {"blatantly_bad": "blatantly_bad", "trending_bad": "trending_bad", "neutral": "neutral"}
_NOTHING = (
    "Nothing stood out this week."
)
# Kept here, not in prompts.py, because it speaks of <candidates>.
_STRICT_SUFFIX = """
The previous sentences failed verification: {problems}.
Write them again: every number and issue key must appear verbatim in <candidates>, and every sentence must list the id of the candidate it is about in candidate_ids."""


class NarrativeSentence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    tone: Tone
    candidate_ids: list[str] = Field(default_factory=list)


class Narrative(BaseModel):
    """What the model returns: one sentence per candidate, in order of importance."""

    model_config = ConfigDict(extra="forbid")

    sentences: list[NarrativeSentence] = Field(max_length=8)


Unit = Literal["count", "hours", "ratio", "share", "points", "days"]
_UNITS: frozenset[str] = frozenset({"count", "hours", "ratio", "share", "points", "days"})
# How the validator reads an entry with a unit: points and days are plain numbers to it.
_KIND_OF_UNIT: dict[str, ColumnKind] = {
    "count": "count", "hours": "hours", "ratio": "ratio", "share": "share", "points": "number", "days": "number",
}


@dataclass(frozen=True)
class DetailEntry:
    """One figure a candidate is about: a dashboard table row and a fact the narrative may quote."""

    key: str
    label: str
    value: int | float | str | None
    unit: Unit | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "value": self.value, "unit": self.unit}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DetailEntry:
        key = str(data.get("key") or "")
        unit = data.get("unit")
        return cls(
            key=key,
            label=str(data.get("label") or key.replace("_", " ")),
            value=_detail_value(data.get("value")),
            unit=unit if isinstance(unit, str) and unit in _UNITS else None,  # type: ignore[arg-type]
        )

    @property
    def per_issue(self) -> bool:
        return ":" in self.key


@dataclass
class Candidate:
    id: str
    kind: CandidateKind
    title: str
    severity: str  # breach | warning | info
    affected: int
    trend: float
    detail: list[DetailEntry]
    issue_keys: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.affected * SEVERITY.get(self.severity, 1) * self.trend

    def figure(self, key: str) -> Any:
        """The value of the detail entry `key`; None when the candidate has none."""
        return next((entry.value for entry in self.detail if entry.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        """What the model is shown and the insights row keeps; the score is rounded for reading."""
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "severity": self.severity,
            "affected": self.affected,
            "trend": self.trend,
            "score": round(self.score, 2),
            "detail": [entry.to_dict() for entry in self.detail],
            "issue_keys": list(self.issue_keys),
            "finding_ids": list(self.finding_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Candidate:
        """The inverse of to_dict; the score is derived, and a legacy dict detail is read as entries."""
        detail = data.get("detail")
        if isinstance(detail, Mapping):
            entries = _legacy_entries(detail)
        else:
            entries = [DetailEntry.from_dict(item) for item in detail or [] if isinstance(item, Mapping)]
        return cls(
            id=str(data["id"]),
            kind=data["kind"],
            title=str(data.get("title") or ""),
            severity=str(data.get("severity") or "info"),
            affected=int(data.get("affected") or 0),
            trend=float(data.get("trend", 1.0)),
            detail=entries,
            issue_keys=[str(key) for key in data.get("issue_keys") or []],
            finding_ids=[str(fid) for fid in data.get("finding_ids") or []],
        )


@dataclass
class Sentence:
    id: str
    text: str
    tone: Tone
    candidate_ids: list[str]
    issue_keys: list[str]
    finding_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Sentence:
        return cls(
            id=str(data.get("id") or ""),
            text=str(data.get("text") or ""),
            tone=_TONES.get(str(data.get("tone")), "neutral"),
            candidate_ids=[str(cid) for cid in data.get("candidate_ids") or []],
            issue_keys=[str(key) for key in data.get("issue_keys") or []],
            finding_ids=[str(fid) for fid in data.get("finding_ids") or []],
        )


@dataclass
class Insight:
    insight_id: str
    project: str
    week_of: str
    generated_at: str
    verified: bool
    narrative: str
    sentences: list[Sentence]
    candidates: list[Candidate]
    model: str | None
    prompt_hash: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "insight_id": self.insight_id,
            "project": self.project,
            "week_of": self.week_of,
            "generated_at": self.generated_at,
            "verified": self.verified,
            "narrative": self.narrative,
            "sentences": [sentence.to_dict() for sentence in self.sentences],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "model": self.model,
            "prompt_hash": self.prompt_hash,
        }

    def render(self) -> str:
        """Plain text for the terminal: the sentences, or the table when the narrative failed verification."""
        lines = [f"Weekly insights for {self.project}, week of {self.week_of} (generated {self.generated_at})"]
        if not self.verified:
            lines.append("The narrative did not pass verification, so the ranked table is shown instead:")
            lines.extend(self.narrative.splitlines())
        elif self.sentences:
            lines.extend(f"- {sentence.text}" for sentence in self.sentences)
        else:
            lines.append(self.narrative)
        return "\n".join(lines)


# --- candidates ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Weeks:
    """Week bounds as storage-format strings, which SQL and Python compare chronologically as text."""

    now: str
    this_start: str
    last_start: str

    @classmethod
    def ending(cls, now: datetime) -> _Weeks:
        return cls(_iso(now), _iso(now - _WEEK), _iso(now - 2 * _WEEK))

    def of(self, stamp: str | None) -> Literal["this", "last"] | None:
        if not stamp:
            return None
        if self.this_start < stamp <= self.now:
            return "this"
        if self.last_start < stamp <= self.this_start:
            return "last"
        return None


def build_candidates(
    store: Store, process: ProcessConfig, rules: RulesConfig, *, project: str, now: datetime
) -> list[Candidate]:
    """Every candidate for the project at `now`, in a fixed order; rank() orders them by score."""
    estimate = process.estimate_for(project)  # ConfigError for an unknown project, before any query
    rules = effective_rules(rules, load_board_settings(store.conn, project))
    weeks = _Weeks.ending(now)
    excluded = [name.casefold() for name in rules.defaults.exclude_types]
    cold = cold_start(store, project, now)
    candidates: list[Candidate] = []
    wow = _wow_change(store, project, weeks, excluded, process.done_stage)
    if wow is not None:
        candidates.append(wow)
    candidates.extend(_breach_hot_spots(store, rules, project, weeks, cold=cold))
    if not cold:  # every finding is new in a cold start: "this week vs last" would only count the first run
        candidates.extend(_rising_rules(store, rules, project, weeks))
    outlier = _outlier(store, project, excluded)
    if outlier is not None:
        candidates.append(outlier)
    coverage = _coverage(store, project, now, excluded, points=estimate.basis == "points")
    if coverage is not None:
        candidates.append(coverage)
    return candidates


# History starts at the first successful rules run, per the rule_status rows it wrote.
_FIRST_RULES_RUN_SQL = """
    SELECT MIN(r.started_at) FROM runs AS r
    WHERE r.kind = 'rules' AND r.status = 'succeeded'
      AND EXISTS (SELECT 1 FROM rule_status AS s WHERE s.run_id = r.run_id AND s.project_key = ?)
"""
_FIRST_FINDING_SQL = "SELECT MIN(first_seen_at) FROM findings WHERE project_key = ?"


def history_starts(store: Store, project: str) -> str | None:
    """Start of the project's findings history: its first rules run or first finding, whichever is earlier."""
    starts: list[str] = []
    for sql in (_FIRST_RULES_RUN_SQL, _FIRST_FINDING_SQL):
        row = store.query_one(sql, (project,))
        if row is not None and row[0]:
            starts.append(row[0])
    return min(starts) if starts else None


def cold_start(store: Store, project: str, now: datetime) -> bool:
    """True while history starts after last week began, when the weeks cannot be compared yet."""
    start = history_starts(store, project)
    return start is None or start > _Weeks.ending(now).last_start


# Time in the done stage is the wait before a reopen, so it is never a week-on-week candidate.
_INTERVALS_SQL = """
    SELECT si.issue_key, si.status, si.hours, si.left_at
    FROM status_intervals AS si JOIN issues AS i ON i.issue_key = si.issue_key
    WHERE i.project_key = ? AND si.hours IS NOT NULL AND si.left_at > ? AND si.left_at <= ?
      AND COALESCE(si.stage, '') <> ?{types}
"""


def _wow_change(
    store: Store, project: str, weeks: _Weeks, excluded: Sequence[str], done_stage: str
) -> Candidate | None:
    types, type_values = _type_filter(excluded)
    rows = store.query(
        _INTERVALS_SQL.format(types=types), [project, weeks.last_start, weeks.now, done_stage, *type_values]
    )
    hours: dict[str, dict[str, list[float]]] = {}
    longest: dict[str, dict[str, float]] = {}  # status -> issue -> its longest interval closed this week
    for row in rows:
        week = weeks.of(row["left_at"])
        if week is None:
            continue
        value = float(row["hours"])
        hours.setdefault(row["status"], {"this": [], "last": []})[week].append(value)
        if week == "this":
            issues = longest.setdefault(row["status"], {})
            issues[row["issue_key"]] = max(issues.get(row["issue_key"], 0.0), value)

    best: tuple[str, float, float, int, int] | None = None
    best_change = 0.0
    for status in sorted(hours):  # by name, so a tie goes to the same status every time
        this, last = hours[status]["this"], hours[status]["last"]
        if len(this) < _MIN_INTERVALS or len(last) < _MIN_INTERVALS:
            continue
        this_h, last_h = _median_of(this, 1), _median_of(last, 1)
        change = round(this_h - last_h, 1)
        if abs(change) > abs(best_change):
            best, best_change = (status, this_h, last_h, len(this), len(last)), change
    if best is None:
        return None  # too few intervals, or no change: nothing to say

    status, this_h, last_h, n_this, n_last = best
    longer = best_change > 0
    per_issue = longest.get(status, {})
    return Candidate(
        id="wow_change",
        kind="wow_change",
        title=f"Median time in {status} {'rose' if longer else 'fell'} week on week",
        severity="warning" if longer else "info",
        affected=n_this,
        trend=_ratio(this_h, last_h),
        detail=[
            DetailEntry("this_week_median_h", "Median this week", this_h, "hours"),
            DetailEntry("last_week_median_h", "Median last week", last_h, "hours"),
            # A size: the validator cannot check a minus sign, so the direction goes into words.
            DetailEntry("change_h", "Change in the median", abs(best_change), "hours"),
            DetailEntry("this_week_intervals", "Intervals closed this week", n_this, "count"),
            DetailEntry("last_week_intervals", "Intervals closed last week", n_last, "count"),
            DetailEntry("status", "Status", status),
            DetailEntry("direction", "Direction", "longer" if longer else "shorter"),
        ],
        # The longest waits first: the ones a reader clicks through to.
        issue_keys=sorted(per_issue, key=lambda key: (-per_issue[key], _key_order(key))),
    )


_BREACHES_SQL = """
    SELECT f.finding_id, f.issue_key, f.rule_id, f.first_seen_at, f.resolved_at,
           COALESCE(i.epic_key, p.epic_key) AS epic_key
    FROM findings AS f
    JOIN issues AS i ON i.issue_key = f.issue_key
    LEFT JOIN issues AS p ON p.issue_key = i.parent_key
    WHERE f.project_key = ? AND f.severity = 'breach' AND f.first_seen_at <= ?
      AND (f.resolved_at IS NULL OR f.resolved_at > ?) AND COALESCE(f.resolved_why, '') != 'ignored'
"""


def _breach_hot_spots(store: Store, rules: RulesConfig, project: str, weeks: _Weeks, *, cold: bool) -> list[Candidate]:
    """In a cold start the trend is 1.0 and the week-ago figure is left out."""
    # Every breach open now or 7 days ago; which of the two is decided per row below.
    rows = store.query(_BREACHES_SQL, (project, weeks.now, weeks.this_start))
    open_now: dict[str, list[sqlite3.Row]] = {}
    open_before: dict[str, int] = {}
    for row in rows:
        epic = row["epic_key"]
        if not epic:
            continue  # work outside any epic is not an epic
        if _open_at(row, weeks.now):
            open_now.setdefault(epic, []).append(row)
        if _open_at(row, weeks.this_start):
            open_before[epic] = open_before.get(epic, 0) + 1

    hottest = sorted(open_now, key=lambda epic: (-len(open_now[epic]), _key_order(epic)))[:_HOT_SPOTS]
    candidates: list[Candidate] = []
    for epic in hottest:
        found = open_now[epic]
        issues = {row["issue_key"] for row in found}
        detail = [DetailEntry("open_breaches", "Open breaches", len(found), "count")]
        if not cold:
            detail.append(DetailEntry("open_a_week_ago", "Open breaches a week ago", open_before.get(epic, 0), "count"))
        detail.append(DetailEntry("issues_with_breaches", "Issues with open breaches", len(issues), "count"))
        rule_id, rule_count = _most_common(row["rule_id"] for row in found)
        rule = _rule(rules, rule_id)
        if rule is not None:
            detail.append(DetailEntry("top_rule", "Most frequent rule", rule.title))
            detail.append(DetailEntry("top_rule_breaches", "Breaches of the most frequent rule", rule_count, "count"))
        detail.append(DetailEntry("epic_key", "Epic", epic))
        candidates.append(
            Candidate(
                id=f"breach_hot_spot:{epic}",
                kind="breach_hot_spot",
                title=f"Epic {epic} is a breach hot spot",
                severity="breach",
                affected=len(found),
                trend=1.0 if cold else max(1.0, _ratio(len(found), open_before.get(epic, 0))),
                detail=detail,
                issue_keys=[epic, *sorted(issues - {epic}, key=_key_order)],
                finding_ids=sorted(row["finding_id"] for row in found),
            )
        )
    return candidates


_NEW_FINDINGS_SQL = """
    SELECT f.finding_id, f.rule_id, f.issue_key, f.evidence_key, f.first_seen_at,
           (SELECT MIN(g.first_seen_at) FROM findings AS g
             WHERE g.project_key = f.project_key AND g.rule_id = f.rule_id
               AND g.issue_key = f.issue_key AND g.evidence_key = f.evidence_key
               AND COALESCE(g.resolved_why, '') != 'ignored') AS earliest
    FROM findings AS f
    WHERE f.project_key = ? AND f.first_seen_at > ? AND f.first_seen_at <= ?
      AND COALESCE(f.resolved_why, '') != 'ignored'
"""


def _rising_rules(store: Store, rules: RulesConfig, project: str, weeks: _Weeks) -> list[Candidate]:
    rows = store.query(_NEW_FINDINGS_SQL, (project, weeks.last_start, weeks.now))
    # rule -> week -> (issue, evidence) -> the newest row for it (the current catalogue version's finding)
    new: dict[str, dict[str, dict[tuple[str, str], sqlite3.Row]]] = {}
    for row in rows:
        week = weeks.of(row["earliest"])
        if week is None:
            continue  # first seen before last week, under an older catalogue version
        found = new.setdefault(row["rule_id"], {"this": {}, "last": {}})[week]
        what = (row["issue_key"], row["evidence_key"])
        kept = found.get(what)
        if kept is None or (row["first_seen_at"], row["finding_id"]) > (kept["first_seen_at"], kept["finding_id"]):
            found[what] = row

    candidates: list[Candidate] = []
    for rule in rules.enabled_rules:
        counts = new.get(rule.id)
        if counts is None or len(counts["this"]) <= len(counts["last"]):
            continue
        this, last = counts["this"], counts["last"]
        candidates.append(
            Candidate(
                id=f"rising_rule:{rule.id}",
                kind="rising_rule",
                title=f"More '{rule.title}' findings this week than last",
                severity=rule.severity,
                affected=len(this),
                trend=_ratio(len(this), len(last)),
                detail=[
                    DetailEntry("new_this_week", "First seen this week", len(this), "count"),
                    DetailEntry("new_last_week", "First seen last week", len(last), "count"),
                    DetailEntry("rule_id", "Rule", rule.id),
                ],
                # Sprint-scoped findings sit under "sprint:<id>", which is not an issue key.
                issue_keys=sorted({issue for issue, _ in this if KEY.fullmatch(issue)}, key=_key_order),
                finding_ids=sorted(row["finding_id"] for row in this.values()),
            )
        )
    return candidates


_OPEN_WORK_SQL = """
    SELECT i.issue_key, m.effort_ratio, m.status_age_h
    FROM issues AS i LEFT JOIN issue_metrics AS m ON m.issue_key = i.issue_key
    WHERE i.project_key = ? AND i.status_category IN ('new', 'indeterminate'){types}
"""


def _outlier(store: Store, project: str, excluded: Sequence[str]) -> Candidate | None:
    types, type_values = _type_filter(excluded)
    rows = store.query(_OPEN_WORK_SQL.format(types=types), [project, *type_values])
    if len(rows) < _MIN_OPEN_WORK:
        return None
    ratio_p90 = _p90([row["effort_ratio"] for row in rows], 2)
    age_p90 = _p90([row["status_age_h"] for row in rows], 1)
    if ratio_p90 is None and age_p90 is None:
        return None

    found: list[tuple[float, str, float | None, float | None]] = []
    for row in rows:
        ratio, age = _rounded(row["effort_ratio"], 2), _rounded(row["status_age_h"], 1)
        excess = max(_excess(ratio, ratio_p90), _excess(age, age_p90))
        if excess > 0:
            found.append((excess, row["issue_key"], ratio, age))
    if not found:
        return None
    found.sort(key=lambda item: (-item[0], _key_order(item[1])))  # the furthest beyond first

    detail = [
        DetailEntry("outliers", "Open issues beyond a p90", len(found), "count"),
        DetailEntry("open_issues", "Open issues", len(rows), "count"),
    ]
    if ratio_p90 is not None:
        detail.append(DetailEntry("p90_effort_ratio", "p90 effort ratio of open work", ratio_p90, "ratio"))
    if age_p90 is not None:
        detail.append(DetailEntry("p90_status_age_h", "p90 hours in the current status of open work", age_p90, "hours"))
    for _, key, ratio, age in found[:_OUTLIERS_LISTED]:  # the issues spelled out, each by its key
        if ratio is not None:
            detail.append(DetailEntry(f"{key}:effort_ratio", f"{key}, effort ratio", ratio, "ratio"))
        if age is not None:
            detail.append(DetailEntry(f"{key}:status_age_h", f"{key}, hours in its current status", age, "hours"))
    return Candidate(
        id="outlier",
        kind="outlier",
        title="Open issues far beyond the rest on effort or time in status",
        severity="warning",
        affected=len(found),
        trend=1.0,
        detail=detail,
        issue_keys=[item[1] for item in found],
    )


# The active sprint as the catalogue finds it: the sprint objects the sync stored in raw_json.
_ACTIVE_SPRINT_SQL = """
    SELECT DISTINCT json_extract(t.value, '$.id') AS id, json_extract(t.value, '$.name') AS name,
           json_extract(t.value, '$.startDate') AS start_date
    FROM issues AS i, json_tree(i.raw_json) AS t
    WHERE i.project_key = ? AND i.raw_json IS NOT NULL AND instr(i.raw_json, 'active') > 0
      AND t.type = 'object' AND json_extract(t.value, '$.state') = 'active'
      AND json_extract(t.value, '$.startDate') IS NOT NULL
    ORDER BY start_date DESC, id DESC
"""
_COVERAGE_SQL = (
    "SELECT i.issue_key, i.story_points, i.original_estimate_s, i.time_spent_s"
    " FROM issues AS i WHERE i.project_key = ?{scope}{types}"
)
_IN_SPRINT = " AND EXISTS (SELECT 1 FROM json_each(i.sprint_ids) AS s WHERE s.value = ?)"
_CREATED_IN = " AND i.created_at >= ? AND i.created_at <= ?"


def _coverage(
    store: Store, project: str, now: datetime, excluded: Sequence[str], *, points: bool
) -> Candidate | None:
    types, type_values = _type_filter(excluded)
    rows: list[sqlite3.Row] = []
    where: list[DetailEntry] = []
    sprint = _active_sprint(store, project)
    if sprint is not None:
        try:
            rows = store.query(_COVERAGE_SQL.format(scope=_IN_SPRINT, types=types), [project, sprint["id"], *type_values])
        except sqlite3.OperationalError:
            rows = []  # a sprint_ids value that is not JSON: no membership to read
        where = [DetailEntry("scope", "Scope", "the active sprint")]
        if sprint["name"]:
            where.append(DetailEntry("sprint", "Sprint", sprint["name"]))
    if not rows:
        since, until = _iso(now - _COVERAGE_FALLBACK), _iso(now)
        rows = store.query(_COVERAGE_SQL.format(scope=_CREATED_IN, types=types), [project, since, until, *type_values])
        where = [DetailEntry("scope", "Scope", "work created in the last thirty days")]
    if not rows:
        return None

    column = "story_points" if points else "original_estimate_s"
    estimated = {row["issue_key"] for row in rows if (row[column] or 0) > 0}
    logged = {row["issue_key"] for row in rows if (row["time_spent_s"] or 0) > 0}
    uncovered = sorted(
        (row["issue_key"] for row in rows if row["issue_key"] not in estimated or row["issue_key"] not in logged),
        key=_key_order,
    )
    # Shares as displayed (two places), so "below 80%" agrees with what the reader sees.
    estimate_share = round(len(estimated) / len(rows), 2)
    logged_share = round(len(logged) / len(rows), 2)
    below = estimate_share < _COVERAGE_TARGET or logged_share < _COVERAGE_TARGET
    return Candidate(
        id="coverage",
        kind="coverage",
        title=f"Estimate and logged-time coverage in {where[0].value}",
        severity="warning" if below else "info",
        affected=len(uncovered),
        trend=1.0,
        detail=[
            DetailEntry("estimate_share", "Share with an estimate", estimate_share, "share"),
            DetailEntry("time_logged_share", "Share with time logged", logged_share, "share"),
            DetailEntry("issues_in_scope", "Issues in scope", len(rows), "count"),
            DetailEntry("uncovered", "Issues missing an estimate or logged time", len(uncovered), "count"),
            DetailEntry("with_estimate", "With an estimate", len(estimated), "count"),
            DetailEntry("with_time_logged", "With time logged", len(logged), "count"),
            *where,
        ],
        issue_keys=uncovered,
    )


def _active_sprint(store: Store, project: str) -> sqlite3.Row | None:
    try:
        rows = store.query(_ACTIVE_SPRINT_SQL, (project,))
    except sqlite3.OperationalError:
        return None  # a raw_json that is not valid JSON: treat as no sprint data, as the catalogue does
    return next((row for row in rows if row["id"] is not None), None)


# --- ranking and the table ----------------------------------------------------------------------


def rank(candidates: list[Candidate], top: int = 5) -> list[Candidate]:
    """Highest score first; sorted() is stable, so equal scores keep the build order."""
    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)[: max(top, 0)]


def render_insights_table(candidates: list[Candidate]) -> str:
    """Plain text, one line per candidate, for when the narrative can't be verified."""
    if not candidates:
        return _NOTHING
    return "\n".join(f"{number}. {_table_line(candidate)}" for number, candidate in enumerate(candidates, start=1))


def _table_line(candidate: Candidate) -> str:
    """A candidate as one line of code-written facts: severity, size, trend, its scalar detail, its keys."""
    phrases = [f"{candidate.affected} affected", f"trend {_number_text(candidate.trend)}"]
    for entry in candidate.detail:
        # An entry about one of several issues (the outliers) is covered by the keys at the end of the line.
        phrase = None if entry.per_issue else _phrase(entry.key, entry.value)
        if phrase:
            phrases.append(phrase)
    line = f"{candidate.title} ({candidate.severity}): {', '.join(phrases)}"
    if candidate.issue_keys:
        shown = candidate.issue_keys[:_KEYS_IN_TABLE]
        more = len(candidate.issue_keys) - len(shown)
        line += "; issues " + ", ".join(shown) + (f" and {more} more" if more else "")
    return line + "."


def _phrase(key: str, value: Any) -> str | None:
    label = key.replace("_", " ")
    if value is None or isinstance(value, (list, dict)):
        return None  # lists (the outliers) are covered by the keys at the end of the line
    if isinstance(value, bool):
        return f"{label} {'yes' if value else 'no'}"
    if isinstance(value, (int, float)):
        if key.endswith("share") and 0 <= value <= 1:
            return f"{label} {value * 100:.0f}%"
        if key.endswith("_h"):
            return f"{label[:-2]} {_number_text(value)} h"
        return f"{label} {_number_text(value)}"
    return f"{label} {value}"


def _number_text(value: float) -> str:
    """12.0 -> '12', 1.25 -> '1.25': at most the two places the candidates are rounded to."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if math.isinf(value):
        return "infinite"
    return f"{value:.2f}".rstrip("0").rstrip(".")


# --- small helpers ------------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    """to_iso for a datetime that is never None (to_iso's type allows None)."""
    return to_iso(moment) or ""


def _type_filter(excluded: Sequence[str], alias: str = "i") -> tuple[str, list[str]]:
    """SQL that leaves out container types (defaults.exclude_types), as the catalogue does; a NULL type stays."""
    if not excluded:
        return "", []
    marks = ", ".join("?" * len(excluded))
    return f" AND ({alias}.issue_type IS NULL OR lower({alias}.issue_type) NOT IN ({marks}))", list(excluded)


def _key_order(key: str) -> tuple[str, int, str]:
    """Issue keys in human order: AIL-9 before AIL-10."""
    prefix, _, number = key.rpartition("-")
    return (prefix, int(number), key) if number.isdigit() else (key, -1, key)


def _median_of(values: Sequence[float], places: int) -> float:
    value = median(values)
    return round(value, places) if value is not None else 0.0


def _ratio(current: float, previous: float) -> float:
    """current / previous to two places; _NEW_TREND when there was nothing before and something now."""
    if previous > 0:
        return round(current / previous, 2)
    return _NEW_TREND if current > 0 else 1.0


def _open_at(row: sqlite3.Row, moment: str) -> bool:
    return bool(row["first_seen_at"] <= moment and (row["resolved_at"] is None or row["resolved_at"] > moment))


def _most_common(values: Iterable[str]) -> tuple[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    best = min(counts, key=lambda value: (-counts[value], value))  # ties: the first id, every time
    return best, counts[best]


def _rule(rules: RulesConfig, rule_id: str) -> Rule | None:
    """The rule a finding names on the board, deleted custom rules included; None if gone from the catalogue."""
    return rules.find(rule_id)


def _p90(values: Iterable[float | None], places: int) -> float | None:
    """The p90 of the known values, as displayed; None with fewer than _MIN_OPEN_WORK of them."""
    known = [float(value) for value in values if value is not None]
    if len(known) < _MIN_OPEN_WORK:
        return None
    value = percentile(known, 90)
    return round(value, places) if value is not None else None


def _excess(value: float | None, threshold: float | None) -> float:
    """How many times its threshold a value above it is; 0 when it is not above."""
    if value is None or threshold is None or value <= threshold:
        return 0.0
    return value / threshold if threshold > 0 else math.inf


def _rounded(value: Any, places: int) -> float | None:
    return round(float(value), places) if value is not None else None


def _detail_value(value: Any) -> int | float | str | None:
    """What a detail entry may hold (the contract's number | string | null): anything else is None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None  # JSON has no infinity
    return value


_UNIT_OF_KIND: dict[str, Unit] = {"count": "count", "hours": "hours", "ratio": "ratio", "share": "share"}


def _legacy_entries(detail: Mapping[str, Any]) -> list[DetailEntry]:
    """Entries from a legacy detail dict, as insights rows written before entries kept it."""
    entries: list[DetailEntry] = []
    for key, value in detail.items():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping) and isinstance(item.get("issue_key"), str):
                    issue = item["issue_key"]
                    for metric, number in item.items():
                        if metric != "issue_key":
                            entries.append(_legacy_entry(f"{issue}:{metric}", f"{issue}, {metric.replace('_', ' ')}", number))
        elif not isinstance(value, Mapping):
            label = str(key).replace("_", " ")
            entries.append(_legacy_entry(str(key), label[:1].upper() + label[1:], value))
    return entries


def _legacy_entry(key: str, label: str, value: Any) -> DetailEntry:
    kept = _detail_value(value)
    unit = _UNIT_OF_KIND.get(_column_kind(key.rpartition(":")[2], kept)) if isinstance(kept, (int, float)) else None
    return DetailEntry(key, label, kept, unit)


# --- verification -------------------------------------------------------------------------------

_HEAD_COLUMNS = [
    Column("candidate_id", "candidate id", "text"),
    Column("title", "title", "text"),
    Column("severity", "severity", "text"),
    Column("affected", "affected", "count"),
    Column("trend", "trend", "ratio"),
    Column("score", "score", "number"),
]


def _facts_for(candidate: Candidate, as_of: str) -> Facts:
    """The candidate as the validator's facts: its figures, a column per detail entry, and its keys."""
    row: dict[str, Any] = {
        "candidate_id": candidate.id,
        "title": candidate.title,
        "severity": candidate.severity,
        "affected": candidate.affected,
        "trend": candidate.trend,
        "score": round(candidate.score, 2),
    }
    columns = list(_HEAD_COLUMNS)
    for entry in candidate.detail:
        if entry.key in row:
            continue  # an entry never stands in for the candidate's own figures
        row[entry.key] = entry.value
        columns.append(Column(entry.key, entry.label, _entry_kind(entry)))
    if candidate.issue_keys:
        row["sample_keys"] = " ".join(candidate.issue_keys)
        columns.append(Column("sample_keys", "issue keys", "text"))
    return Facts(ref=candidate.id, tool=f"insights_{candidate.kind}", params={}, columns=columns, rows=[row], as_of=as_of)


def _entry_kind(entry: DetailEntry) -> ColumnKind:
    """The validator's column kind for an entry: its unit's, a key for a word that is an issue key."""
    if entry.unit is not None:
        return _KIND_OF_UNIT[entry.unit]
    if isinstance(entry.value, str):
        return "issue_key" if KEY.fullmatch(entry.value) else "text"
    return "text" if entry.value is None else "number"


def _column_kind(name: str, value: Any) -> ColumnKind:
    """A legacy detail value's kind from its name; only "share" changes what may be quoted (its percent forms)."""
    if name in ("issue_key", "epic_key"):
        return "issue_key"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "text"
    if name.endswith("_h"):
        return "hours"
    if name.endswith("share"):
        return "share"
    if "ratio" in name or name == "trend":
        return "ratio"
    return "count" if isinstance(value, int) else "number"


def _verify(narrative: Narrative, facts: list[Facts], known: set[str], rules: RulesConfig | None = None) -> Check:
    """validate() over the joined sentences, plus a check that each sentence names one of its candidates."""
    check = validate(" ".join(sentence.text for sentence in narrative.sentences), facts, None, rules=rules)
    problems = list(check.problems)
    if not narrative.sentences:
        problems.append("no sentences")
    for number, sentence in enumerate(narrative.sentences, start=1):
        if not known.intersection(sentence.candidate_ids):
            problems.append(f"sentence {number} names no candidate")
    return Check(ok=not problems, problems=problems, bindings=check.bindings)


# --- the narrative ------------------------------------------------------------------------------


@dataclass
class _Narration:
    narrative: Narrative | None  # None: the table ships instead
    model: str
    prompt_hash: str
    calls: int
    problems: list[str]


def _narrate(
    store: Store,
    provider: LLMProvider,
    settings: Settings,
    top: list[Candidate],
    facts: list[Facts],
    clock: Callable[[], datetime],
    rules: RulesConfig | None = None,
) -> _Narration:
    """One call and one strict retry when the check fails; a refusal or an error ends it at once."""
    known = {candidate.id for candidate in top}
    user = insights_user([candidate.to_dict() for candidate in top])
    model = settings.llm_model_insights
    problems: list[str] = []
    digest = ""
    for attempt, purpose in enumerate(("insights", "insights_strict"), start=1):
        prompt = user if attempt == 1 else user + _STRICT_SUFFIX.format(problems="; ".join(problems))
        digest = llm_audit.prompt_hash(INSIGHTS_SYSTEM, prompt)
        try:
            result = provider.structured(
                INSIGHTS_SYSTEM, prompt, Narrative, model=settings.llm_model_insights, effort="high", max_tokens=_MAX_TOKENS
            )
        except LLMError as exc:  # LLMRefusal is an LLMError
            refused = isinstance(exc, LLMRefusal)
            _audit(store, provider, purpose, model, digest, None, "refused" if refused else "error",
                   str(exc) or type(exc).__name__, clock, failure=exc)
            return _Narration(None, model, digest, attempt, [f"the model {'declined' if refused else 'failed'}: {exc}"])
        _audit(store, provider, purpose, result.model, digest, result, "ok", None, clock)
        model = result.model
        narrative = _as_narrative(result)
        if narrative is None:
            problems = ["the reply did not fit the sentences schema"]
            continue
        check = _verify(narrative, facts, known, rules)
        if check.ok:
            return _Narration(narrative, model, digest, attempt, [])
        problems = check.problems
    return _Narration(None, model, digest, 2, problems)


def _audit(
    store: Store,
    provider: LLMProvider,
    purpose: str,
    model: str,
    digest: str,
    result: StructuredResult | None,
    outcome: str,
    error: str | None,
    clock: Callable[[], datetime],
    *,
    failure: LLMError | None = None,
) -> None:
    """One llm_calls row; a failed call keeps its reported tokens so the day's budget counts what was billed."""
    if result is not None:
        usage, stop_reason = result.usage, result.stop_reason
    else:
        usage = failure.usage if failure is not None else None
        stop_reason = failure.stop_reason if failure is not None and failure.stop_reason else (
            "refusal" if outcome == "refused" else None)
    llm_audit.record_llm_call(
        store,
        purpose=purpose,
        provider=provider.name,
        model=model,
        prompt_hash=digest,
        usage=usage,
        latency_ms=result.latency_ms if result is not None else None,
        stop_reason=stop_reason,
        outcome=outcome,
        error=error,
        now=clock,
    )


def _as_narrative(result: StructuredResult) -> Narrative | None:
    parsed = result.parsed
    if isinstance(parsed, Narrative):
        return parsed
    try:  # a provider may hand back an equivalent model of its own
        return Narrative.model_validate(parsed.model_dump())
    except ValidationError:
        return None


def _linked(narrative: Narrative, top: list[Candidate]) -> list[Sentence]:
    """The model's sentences, each with the keys and finding ids of the candidates it names."""
    by_id = {candidate.id: candidate for candidate in top}
    sentences: list[Sentence] = []
    for number, sentence in enumerate(narrative.sentences, start=1):
        ids = [cid for cid in dict.fromkeys(sentence.candidate_ids) if cid in by_id]
        sentences.append(
            Sentence(
                id=f"s{number}",
                text=sentence.text.strip(),
                tone=sentence.tone,
                candidate_ids=ids,
                issue_keys=_union(by_id[cid].issue_keys for cid in ids),
                finding_ids=_union(by_id[cid].finding_ids for cid in ids),
            )
        )
    return sentences


def _table_sentences(top: list[Candidate]) -> list[Sentence]:
    """The fallback's sentences: one per table line, the tone from the severity."""
    return [
        Sentence(
            id=f"s{number}",
            text=_table_line(candidate),
            tone=_TONE_FOR.get(candidate.severity, "neutral"),
            candidate_ids=[candidate.id],
            issue_keys=list(candidate.issue_keys),
            finding_ids=list(candidate.finding_ids),
        )
        for number, candidate in enumerate(top, start=1)
    ]


def _union(groups: Iterable[list[str]]) -> list[str]:
    return list(dict.fromkeys(item for group in groups for item in group))


# --- the run ------------------------------------------------------------------------------------


def run_insights(
    store: Store,
    process: ProcessConfig,
    rules: RulesConfig,
    provider: LLMProvider,
    settings: Settings,
    *,
    projects: list[str],
    now: Callable[[], datetime] = utcnow,
    tz_name: str | None = None,
) -> list[Insight]:
    """One insights run: per project the candidates, the ranked top, the narrative or table, and the row."""
    try:
        wanted = list(dict.fromkeys(validate_project_keys(projects)))
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    for project in wanted:
        process.project(project)  # ConfigError before anything is written
    zone(tz_name)  # likewise for an unknown time zone

    started = now()
    run = store.start_run("insights", rules_version=rules.version, started_at=started)
    log.info("insights_started", run_id=run.run_id, projects=wanted)
    insights: list[Insight] = []
    stats: dict[str, Any] = {}
    try:
        for project in wanted:
            insight, stats[project] = _insight_for(
                store, process, rules, provider, settings, project=project, at=started, clock=now, tz_name=tz_name, run_id=run.run_id
            )
            if insight is not None:  # None: skipped, the day's model budget used up
                insights.append(insight)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            store.finish_run(run, status="failed", stats={"projects": stats, "error": error}, finished_at=now())
        except Exception:  # never let the bookkeeping hide the original error
            log.exception("insights_run_not_recorded", run_id=run.run_id)
        log.error("insights_failed", run_id=run.run_id, error=error)
        raise
    store.finish_run(run, status="succeeded", stats={"projects": stats}, finished_at=now())
    log.info("insights_finished", run_id=run.run_id, projects=stats)
    return insights


def _insight_for(
    store: Store,
    process: ProcessConfig,
    rules: RulesConfig,
    provider: LLMProvider,
    settings: Settings,
    *,
    project: str,
    at: datetime,
    clock: Callable[[], datetime],
    tz_name: str | None,
    run_id: str,
) -> tuple[Insight | None, dict[str, Any]]:
    candidates = build_candidates(store, process, rules, project=project, now=at)
    top = rank(candidates, TOP)
    model: str | None = None
    digest: str | None = None
    calls = 0
    sentences: list[Sentence] = []
    if top and getattr(provider, "name", None) != SCRIPTED and budget_reached(store.conn, settings, clock()):
        # The day's model budget is used up: no call, and no briefing for the board until it resets (00:00 UTC).
        log.info("insights_skipped_budget", project=project, budget=settings.llm_daily_token_budget)
        return None, {"candidates": len(candidates), "ranked": len(top), "skipped": "budget", "llm_calls": 0}
    if not top:
        verified, narrative = True, _NOTHING  # nothing to say, so nothing to ask the model
    else:
        as_of = _iso(at)
        board = effective_rules(rules, load_board_settings(store.conn, project))
        narration = _narrate(store, provider, settings, top, [_facts_for(c, as_of) for c in top], clock, board)
        model, digest, calls = narration.model, narration.prompt_hash, narration.calls
        if narration.narrative is not None:
            sentences = _linked(narration.narrative, top)
            verified, narrative = True, " ".join(sentence.text for sentence in sentences)
        else:
            sentences = _table_sentences(top)
            verified, narrative = False, render_insights_table(top)
            log.warning("insights_unverified", project=project, problems=narration.problems)

    insight = Insight(
        insight_id=uuid.uuid4().hex,
        project=project,
        week_of=week_of(at, tz_name),
        generated_at=_iso(clock()),
        verified=verified,
        narrative=narrative,
        sentences=sentences,
        candidates=top,
        model=model,
        prompt_hash=digest,
    )
    store.upsert(
        "insights",
        {
            "insight_id": insight.insight_id,
            "project_key": project,
            "week_of": insight.week_of,
            "generated_at": insight.generated_at,
            "verified": verified,
            "narrative": narrative,
            # Dumped here, not by the store, which sorts keys: detail keeps its reading order.
            "sentences_json": json.dumps([sentence.to_dict() for sentence in sentences], ensure_ascii=False),
            "candidates_json": json.dumps([candidate.to_dict() for candidate in top], ensure_ascii=False),
            "model": model,
            "prompt_hash": digest,
            "run_id": run_id,
        },
    )
    stats = {"candidates": len(candidates), "ranked": len(top), "verified": verified, "llm_calls": calls}
    log.info("insights_written", project=project, **stats)
    return insight, stats


def week_of(moment: datetime, tz_name: str | None = None) -> str:
    """The Monday (YYYY-MM-DD) of the week `moment` falls in, in the zone (UTC when unset)."""
    local = moment.astimezone(zone(tz_name))
    return (local.date() - timedelta(days=local.weekday())).isoformat()


_LATEST_SQL = "SELECT * FROM insights WHERE project_key = ? ORDER BY generated_at DESC, rowid DESC LIMIT 1"


def latest_insight(store: Store | sqlite3.Connection, project: str) -> Insight | None:
    """The newest insights row for the project, from a Store or a read-only connection; None when there is none."""
    conn = store.conn if isinstance(store, Store) else store
    cursor = conn.execute(_LATEST_SQL, (project,))
    row = cursor.fetchone()
    if row is None:
        return None
    data = dict(zip([column[0] for column in cursor.description], row, strict=True))
    return Insight(
        insight_id=data["insight_id"],
        project=data["project_key"],
        week_of=data["week_of"],
        generated_at=data["generated_at"],
        verified=bool(data["verified"]),
        narrative=data["narrative"] or "",
        sentences=[Sentence.from_dict(item) for item in _json_list(data["sentences_json"])],
        candidates=[Candidate.from_dict(item) for item in _json_list(data["candidates_json"])],
        model=data["model"],
        prompt_hash=data["prompt_hash"],
    )


def _json_list(text: str | None) -> list[dict[str, Any]]:
    if not text:
        return []
    value = json.loads(text)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
