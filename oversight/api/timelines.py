"""What GET /findings, GET /findings/{finding_id} and GET /issues/{key}/timeline share."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from oversight.api.deps import ApiContext
from oversight.api.people import UNASSIGNED, Person, person
from oversight.ask.catalog import _EPIC_OF_I, _PARENT_JOIN, _rows, _rule_title
from oversight.derive.run import _category_lookup, _status_categories
from oversight.rules.catalog import Rule
from oversight.rules.checks import label as stage_label
from oversight.rules.registry import days_words, hours_words
from oversight.timeutil import hours_between, iso, parse_ts

SPRINT_PREFIX = "sprint:"
# The stages the contract names (schemas.ts STAGES); any other stage goes out as null.
CONTRACT_STAGES = frozenset(
    ("backlog", "estimate", "development", "code_review", "dev_signoff", "testing", "test_review", "test_signoff", "done")
)
CATEGORIES = frozenset(("new", "indeterminate", "done"))
CHANGE_FIELDS = ("story_points", "original_estimate", "sprint", "assignee")
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,19}-[1-9][0-9]{0,8}")
RULE_ID = re.compile(r"[RW]-[A-Z]+-[0-9]{2,3}")
RESOLVED_WHY = frozenset(("cleared", "superseded", "settings_changed"))
FINDING_ID = re.compile(r"[0-9a-f]{32}")
_STORED_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_CHUNK = 500  # keys per IN (...) list, well under SQLite's variable limit
# Estimate basis -> (logical field, how it reads, unit).
_ESTIMATE: dict[str, tuple[str, str, str]] = {
    "points": ("story_points", "Story points", "points"),
    "hours": ("original_estimate", "Original estimate", "hours"),
}

Item = dict[str, Any]


# --- values -------------------------------------------------------------------------------


def ts(value: object) -> str | None:
    """A stored timestamp as the contract sends it (ISO 8601, UTC, Z); None when absent or unreadable."""
    if isinstance(value, str) and _STORED_TS.fullmatch(value):
        return value
    try:
        return iso(value)
    except (TypeError, ValueError, OverflowError):
        return None


def num(value: object, places: int = 4) -> float | None:
    """A finite number rounded to `places` decimals ("4 decimals is plenty"), or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(number, places) if math.isfinite(number) else None


def non_negative(value: object) -> float | None:
    number = num(value)
    return None if number is None else max(0.0, number)


def key_number(key: str | None) -> int | None:
    """DEMO-142 -> 142; None without a key."""
    if not key:
        return None
    tail = key.rpartition("-")[2]
    return int(tail) if tail.isdigit() else None


def contract_stage(stage: object) -> str | None:
    return stage if isinstance(stage, str) and stage in CONTRACT_STAGES else None


def estimate_text(field: str, value: object) -> str | None:
    """A story-point or original-estimate value as a reader sees it: "3", "8 h" (estimates are seconds)."""
    if value is None or value == "":
        return None
    number = num(value)
    if field == "original_estimate":
        return f"{number / 3600:g} h" if number is not None else str(value)
    if isinstance(value, str) or number is None:
        return str(value)
    return f"{number:g}"


def evidence_dict(text: object) -> dict[str, Any]:
    """A finding's evidence_json as a dict ({} when missing or unreadable)."""
    if not isinstance(text, str) or not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def find_rule(ctx: ApiContext, rule_id: str) -> Rule | None:
    """The rule a finding names on the board"""
    return ctx.rules.find(rule_id)


# --- status categories --------------------------------------------------------------------


class Categories:

    def __init__(self, conn: sqlite3.Connection, ctx: ApiContext) -> None:
        rows = _rows(conn, "SELECT name, category FROM statuses WHERE project_key = ?", (ctx.project,))
        self._table = _status_categories(rows)
        self._process = ctx.process
        self._project = ctx.process.project(ctx.project)

    def for_issue(self, issue: Mapping[str, Any] | sqlite3.Row | None) -> Callable[[str | None, str | None], str]:
        current = dict(issue) if issue is not None else {}
        lookup = _category_lookup(self._table, self._project, self._process.done_stage, current)

        def category(status: str | None, stage: str | None) -> str:
            found = lookup(status)
            return found if found in CATEGORIES else self._of_stage(stage)

        return category

    def _of_stage(self, stage: str | None) -> str:
        stages = self._process.stages
        if stage == self._process.done_stage:
            return "done"
        if stage in stages and stages.index(stage) < stages.index(self._process.start_stage):
            return "new"
        return "indeterminate"


def issue_category(issue: Mapping[str, Any] | sqlite3.Row, ctx: ApiContext, categories: Categories) -> str:
    """The status category the Issue page shows for an issue now"""
    stored = issue["status_category"]
    if stored in CATEGORIES:
        return str(stored)
    stage = contract_stage(ctx.process.stage_of(ctx.project, issue["status"]))
    return categories.for_issue(issue)(issue["status"], stage)


# --- history: timelines and changelogs for a set of issues --------------------------------


@dataclass
class History:
    """Some issues' timelines and changelogs, read in one query per table (no authors, no ids of people)."""

    issues: dict[str, sqlite3.Row]
    status_changes: dict[str, list[sqlite3.Row]]
    field_changes: dict[str, list[sqlite3.Row]]
    intervals: dict[str, list[sqlite3.Row]]


_ORDER_CHANGES = "ORDER BY issue_key, changed_at, CAST(changelog_id AS INTEGER), changelog_id"
_HISTORY_SQL = {
    "issues": "SELECT issue_key, status, status_category, created_at, story_points, original_estimate_s "
    "FROM issues WHERE issue_key IN ({marks})",
    "status_changes": "SELECT issue_key, changelog_id, changed_at, from_status, to_status FROM status_changes "
    "WHERE issue_key IN ({marks}) " + _ORDER_CHANGES,
    "field_changes": "SELECT issue_key, changelog_id, field, changed_at, from_value, to_value FROM field_changes "
    "WHERE issue_key IN ({marks}) AND field IN (" + ", ".join(f"'{name}'" for name in CHANGE_FIELDS) + ") "
    + _ORDER_CHANGES + ", field",
    "intervals": "SELECT issue_key, seq, status, stage, entered_at, left_at, hours FROM status_intervals "
    "WHERE issue_key IN ({marks}) ORDER BY issue_key, seq",
}


def _grouped(conn: sqlite3.Connection, sql: str, keys: Sequence[str]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for start in range(0, len(keys), _CHUNK):
        chunk = keys[start : start + _CHUNK]
        for row in _rows(conn, sql.format(marks=", ".join("?" * len(chunk))), chunk):
            grouped[row["issue_key"]].append(row)
    return dict(grouped)


def load_history(conn: sqlite3.Connection, keys: Iterable[str | None]) -> History:
    """The history of these issues (sprint keys and None are skipped)."""
    wanted = sorted({key for key in keys if key and not key.startswith(SPRINT_PREFIX)})
    if not wanted:
        return History({}, {}, {}, {})
    return History(
        issues={key: rows[0] for key, rows in _grouped(conn, _HISTORY_SQL["issues"], wanted).items()},
        status_changes=_grouped(conn, _HISTORY_SQL["status_changes"], wanted),
        field_changes=_grouped(conn, _HISTORY_SQL["field_changes"], wanted),
        intervals=_grouped(conn, _HISTORY_SQL["intervals"], wanted),
    )


def interval_items(
    rows: Sequence[sqlite3.Row], category: Callable[[str | None, str | None], str]
) -> list[Item]:
    items: list[Item] = []
    for row in rows:
        entered = ts(row["entered_at"])
        if entered is None:
            continue  # unreadable: the contract needs a time for every interval
        left = ts(row["left_at"])
        hours: float | None = None
        if left is not None:
            hours = non_negative(row["hours"] if row["hours"] is not None else hours_between(entered, left))
        items.append(
            {
                "status": row["status"] or "Unknown",
                "stage": contract_stage(row["stage"]),
                "status_category": category(row["status"], row["stage"]),
                "entered_at": entered,
                "left_at": left,
                "hours": hours,
            }
        )
    return items


# --- evidence items -------------------------------------------------------------------------


def _shown(value: object) -> str | int | float | None:
    """An evidence value: text as it is, whole numbers as integers, other numbers to 4 decimals."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, int):
        return value
    return num(value)


def _item(
    kind: str,
    label: str,
    value: object = None,
    *,
    unit: str | None = None,
    src: object = None,
    dst: object = None,
    at: object = None,
    changelog_id: object = None,
) -> Item:
    return {
        "kind": kind,
        "label": label,
        "value": _shown(value),
        "unit": unit,
        "from": None if src in (None, "") else str(src),
        "to": None if dst in (None, "") else str(dst),
        "at": ts(at),
        "changelog_id": None if changelog_id in (None, "") else str(changelog_id),
    }


def metric(label: str, value: object, unit: str | None) -> Item:
    return _item("metric", label, value, unit=unit)


def status_item(label: str, src: object, dst: object, at: object, changelog_id: object) -> Item:
    """A status change as evidence: "To Do → In Progress", with its time and changelog entry."""
    value = " → ".join(str(part) for part in (src, dst) if part) or None
    return _item("status_change", label, value, src=src, dst=dst, at=at, changelog_id=changelog_id)


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _pct(value: object, default: object = None) -> str:
    number = num(value if value is not None else default)
    return f"{number:g}" if number is not None else "?"


@dataclass(frozen=True)
class _Subject:
    """One finding and the slice of its issue's history the evidence refers to."""

    kind: str  # rule | warning
    last_seen_at: str | None
    evidence: Mapping[str, Any]
    params: Mapping[str, Any]  # the rule's params on the board (ctx.rules; {} for a rule since removed)
    basis: str  # the project's estimate basis
    issue: sqlite3.Row | None
    changes: Sequence[sqlite3.Row]
    fields: Sequence[sqlite3.Row]
    intervals: Sequence[sqlite3.Row]

    @property
    def estimate(self) -> tuple[str, str, str]:
        basis = self.evidence.get("basis")
        return _ESTIMATE[basis if isinstance(basis, str) and basis in _ESTIMATE else self.basis]

    def change_at(self, at: object, status: object = None) -> sqlite3.Row | None:
        wanted = str(status or "").casefold()
        for change in self.changes:
            if change["changed_at"] == at and (not wanted or str(change["to_status"] or "").casefold() == wanted):
                return change
        return None

    def interval_at(self, at: str | None, status: object = None) -> sqlite3.Row | None:
        """The interval the issue sat in at `at`; with a status, the last such interval entered by then."""
        wanted = str(status or "").casefold()
        found = None
        for interval in self.intervals:
            if at is not None and interval["entered_at"] > at:
                continue
            if wanted and str(interval["status"] or "").casefold() != wanted:
                continue
            found = interval
        return found

    def entered(self, interval: sqlite3.Row | None, label: str | None = None, *, created: bool = True) -> Item | None:
        """The change that moved the issue into the interval or, for its first, its creation there."""
        if interval is None:
            return None
        status = interval["status"]
        change = self.change_at(interval["entered_at"], status)
        if change is not None:
            return status_item(
                label or f"Moved to {status}", change["from_status"], change["to_status"],
                change["changed_at"], change["changelog_id"],
            )
        if not created or interval["seq"] != 0:
            return None
        at = self.issue["created_at"] if self.issue is not None and self.issue["created_at"] else interval["entered_at"]
        return _item("status_change", label or f"Created in {status}", status, dst=status, at=at)

    def work_started(self, started_at: object) -> Item:
        interval = next((iv for iv in self.intervals if iv["entered_at"] == started_at), None)
        found = self.entered(interval, "Work started")
        if found is None and (change := self.change_at(started_at)) is not None:
            found = status_item(
                "Work started", change["from_status"], change["to_status"], change["changed_at"], change["changelog_id"]
            )
        return found or _item("status_change", "Work started", at=started_at)

    def estimate_set(self, set_at: object) -> Item:
        field, label, unit = _ESTIMATE[self.basis]
        for change in self.fields:
            if change["field"] == field and change["changed_at"] == set_at:
                value = estimate_text(field, change["to_value"])
                return _item(
                    "field_change", f"{label} set", value, unit=unit,
                    src=estimate_text(field, change["from_value"]), dst=value, at=set_at,
                    changelog_id=change["changelog_id"],
                )
        current = None  # set when the issue was created: no changelog entry
        if self.issue is not None:
            current = estimate_text(field, self.issue["story_points" if field == "story_points" else "original_estimate_s"])
        return _item("field_change", f"{label} set", current, unit=unit, dst=current, at=set_at)


def _missing_estimate(s: _Subject) -> list[Item]:
    _, label, unit = s.estimate
    return [s.work_started(s.evidence.get("started_at")), metric(label, None, unit)]


def _estimate_after_start(s: _Subject) -> list[Item]:
    started, set_at = s.evidence.get("started_at"), s.evidence.get("estimate_set_at")
    try:
        after: object = hours_between(started, set_at)
    except (TypeError, ValueError):
        after = s.evidence.get("hours_after")
    return [s.work_started(started), s.estimate_set(set_at), metric("Set after work started", after, "hours")]


def _effort_ratio(s: _Subject) -> list[Item]:
    items = [
        metric("Logged so far" if s.kind == "warning" else "Logged time", s.evidence.get("time_spent_h"), "hours"),
        metric("Estimate", s.evidence.get("estimate_h"), "hours"),
        metric("Effort ratio", s.evidence.get("effort_ratio"), "ratio"),
    ]
    moved = s.entered(s.interval_at(s.last_seen_at), created=False)
    return items + ([moved] if moved else [])


def _entered_stage_without_evidence(s: _Subject) -> list[Item]:
    ev = s.evidence
    interval = next(
        (iv for iv in s.intervals if iv["entered_at"] == ev.get("entered_at") and iv["stage"] == ev.get("stage")), None
    )
    entered = s.entered(interval) or _item(
        "status_change", f"Moved to {ev.get('status')}", ev.get("status"), dst=ev.get("status"), at=ev.get("entered_at")
    )
    return [entered, metric(f"{_sentence(stage_label(str(ev['evidence'])))} recorded", None, None)]


def _skipped_stage(s: _Subject) -> list[Item]:
    ev = s.evidence
    changelog_id = ev.get("changelog_id")
    at = ev.get("changed_at") or next(
        (change["changed_at"] for change in s.changes if str(change["changelog_id"]) == str(changelog_id)), None
    )
    stages = ", ".join(stage_label(str(stage)) for stage in ev.get("skipped") or [])
    return [status_item("Skipped", ev.get("from"), ev.get("to"), at, changelog_id), metric("Stages skipped", stages, None)]


def _reopened(s: _Subject) -> list[Item]:
    ev = s.evidence
    return [status_item("Reopened", ev.get("from"), ev.get("to"), ev.get("changed_at"), ev.get("changelog_id"))]


def _in_progress_unassigned(s: _Subject) -> list[Item]:
    entered = s.entered(s.interval_at(s.last_seen_at, s.evidence.get("status")))
    return ([entered] if entered else []) + [metric("Time in status", s.evidence.get("status_age_h"), "hours")]


def _stuck_in_status(s: _Subject) -> list[Item]:
    ev = s.evidence
    entered = s.entered(s.interval_at(s.last_seen_at, ev.get("status")))
    items = ([entered] if entered else []) + [
        metric("Time in status", ev.get("age_h"), "hours"),
        metric(f"Project p{_pct(ev.get('pct'), s.params.get('pct', 90))}", ev.get("threshold_h"), "hours"),
    ]
    if ev.get("history_n") is not None:
        items.append(metric("Closed intervals measured", ev.get("history_n"), "count"))
    return items


def _no_evidence_after_percentile(s: _Subject) -> list[Item]:
    ev = s.evidence
    entered = s.entered(s.interval_at(s.last_seen_at))
    return ([entered] if entered else []) + [
        metric(f"Time in {stage_label(str(ev['stage']))}", ev.get("hours_in_stage"), "hours"),
        metric(f"Project p{_pct(ev.get('pct'), s.params.get('pct', 75))}", ev.get("threshold_h"), "hours"),
        metric(f"{_sentence(stage_label(str(ev['evidence'])))} recorded", None, None),
    ]


def _time_in_status_over(s: _Subject) -> list[Item]:
    ev = s.evidence
    entered = s.entered(s.interval_at(s.last_seen_at, ev.get("status")))
    return ([entered] if entered else []) + [
        metric("Time in status", ev.get("age_h"), "hours"),
        metric("Limit", ev.get("threshold_h"), "hours"),
    ]


def _open_longer_than(s: _Subject) -> list[Item]:
    ev = s.evidence
    created = _item("status_change", "Created", at=ev.get("created_at"))
    return [created, metric("Open for", ev.get("open_days"), "days"), metric("Limit", ev.get("threshold_days"), "days")]


def _no_update_for(s: _Subject) -> list[Item]:
    ev = s.evidence
    return [
        _item("field_change", "Last changed in Jira", at=ev.get("updated_at")),
        metric("No change for", ev.get("idle_days"), "days"),
        metric("Limit", ev.get("threshold_days"), "days"),
    ]


_MISSING_LABELS = {"assignee": "Assignee", "estimate": "Estimate", "epic": "Epic", "sprint": "Sprint"}


def _missing_field(s: _Subject) -> list[Item]:
    name = str(s.evidence["field"])
    return [metric(_MISSING_LABELS.get(name, _sentence(name.replace("_", " "))), None, None)]


def _unestimated_in_active_sprint(s: _Subject) -> list[Item]:
    _, label, unit = s.estimate
    name = s.evidence.get("sprint_name") or f"Sprint {s.evidence['sprint_id']}"
    return [_item("sprint", "In the active sprint", name), metric(label, None, unit)]


def _sprint_at_risk(s: _Subject) -> list[Item]:
    ev = s.evidence
    name = ev.get("name") or f"Sprint {ev['sprint_id']}"
    if ev.get("overdue"):
        # Past its end with work not done (the check's evidence says so): what is left, not how much has started.
        return [
            _item("sprint", "Sprint ended", name, at=ev.get("end_date")),
            metric("Issues in the sprint", ev.get("issues"), "count"),
            metric("Not done", ev.get("not_done"), "count"),
        ]
    return [
        _item("sprint", "Sprint ends", name, at=ev.get("end_date")),
        metric("Days left", ev.get("days_left"), "days"),
        metric("Issues in the sprint", ev.get("issues"), "count"),
        metric("Not started", ev.get("not_started"), "count"),
        metric("Share not started", ev.get("share"), "share"),
    ]


# One formatter per check in oversight/rules/checks.py, keyed by check name.
_EVIDENCE: dict[str, Callable[[_Subject], list[Item]]] = {
    "missing_estimate": _missing_estimate,
    "estimate_after_start": _estimate_after_start,
    "effort_ratio_at_least": _effort_ratio,
    "entered_stage_without_evidence": _entered_stage_without_evidence,
    "skipped_stage": _skipped_stage,
    "reopened": _reopened,
    "in_progress_unassigned": _in_progress_unassigned,
    "stuck_in_status": _stuck_in_status,
    "no_evidence_after_percentile": _no_evidence_after_percentile,
    "unestimated_in_active_sprint": _unestimated_in_active_sprint,
    "sprint_at_risk": _sprint_at_risk,
    "time_in_status_over": _time_in_status_over,
    "open_longer_than": _open_longer_than,
    "no_update_for": _no_update_for,
    "missing_field": _missing_field,
}
# Keys a generic evidence line never shows, whatever a future check stores.
_PEOPLE = ("assignee", "author", "account", "user", "reporter", "creator")


def _generic(evidence: Mapping[str, Any]) -> list[Item]:
    """Evidence of a shape no formatter knows (a rule since removed, an older engine): each plain value."""
    items = []
    for key in sorted(evidence):
        value = evidence[key]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)) or any(p in key for p in _PEOPLE):
            continue
        unit = None
        if isinstance(value, (int, float)):
            unit = (
                "hours" if key.endswith("_h") else "share" if "share" in key else "ratio" if "ratio" in key
                else "days" if "days" in key else "count" if isinstance(value, int) else None
            )
        items.append(metric(_sentence(key.replace("_", " ")), value, unit))
    return items


def evidence_items(check: str | None, subject: _Subject) -> list[Item]:
    formatter = _EVIDENCE.get(check or "")
    if formatter is not None:
        try:
            return formatter(subject)
        except (KeyError, TypeError, ValueError):
            pass  # evidence written by another engine version: show what it holds instead
    return _generic(subject.evidence)


def threshold_text(check: str | None, subject: _Subject) -> str | None:
    """The rule's threshold as a reader says it, or None for a rule without one"""
    ev, params = subject.evidence, subject.params
    if check == "effort_ratio_at_least":
        ratio = num(_found_under(ev, "threshold", params.get("threshold")))
        return f"{ratio:g}×" if ratio is not None else None
    if check == "estimate_after_start":
        grace = num(_found_under(ev, "grace_hours", params.get("grace_hours", 1)))
        return f"{grace:g} h grace" if grace is not None else None
    if check in ("stuck_in_status", "no_evidence_after_percentile"):
        what = ev.get("status") if check == "stuck_in_status" else stage_label(str(ev.get("stage") or ""))
        hours = num(ev.get("threshold_h"))
        if not what or hours is None:
            return None
        default_pct = params.get("pct", 90 if check == "stuck_in_status" else 75)
        return f"p{_pct(ev.get('pct'), default_pct)} of {what}: {hours:.1f} h"
    if check == "sprint_at_risk":
        if ev.get("overdue"):
            return "past its end with work not done"
        days = num(_found_under(ev, "threshold_days_left", params.get("days_left", 2)))
        share = num(_found_under(ev, "threshold_share", params.get("not_started_share", 0.3)))
        if days is None or share is None:
            return None
        return f"≤ {days:g} days left, > {round(share * 100):g}% not started"
    if check == "time_in_status_over":
        hours = num(_found_under(ev, "threshold_h", params.get("hours")))
        return f"{hours_words(hours)} in '{ev.get('status')}'" if hours is not None and ev.get("status") else None
    if check in ("open_longer_than", "no_update_for"):
        days = num(_found_under(ev, "threshold_days", params.get("days")))
        if days is None:
            return None
        return f"{days_words(days)} open" if check == "open_longer_than" else f"{days_words(days)} without a change"
    return None


def _found_under(evidence: Mapping[str, Any], key: str, board: object) -> object:
    """A threshold the finding's evidence records under `key`, else the board's (`board`)."""
    value = evidence.get(key)
    return value if value is not None else board


# --- findings ---------------------------------------------------------------------------------

FINDING_SQL = f"""
    SELECT f.finding_id, f.rule_id, f.rule_version, f.issue_key, f.kind, f.severity, f.message, f.evidence_json,
           f.first_seen_at, f.last_seen_at, f.resolved_at, f.resolved_why,
           i.issue_key AS joined_key, i.summary AS issue_summary, i.issue_type, {_EPIC_OF_I} AS epic_key,
           e.summary AS epic_title, i.assignee_id, a.display_name AS assignee_name, a.avatar_hash AS assignee_avatar
    FROM findings AS f
    LEFT JOIN issues AS i ON i.issue_key = f.issue_key
    {_PARENT_JOIN}
    LEFT JOIN issues AS e ON e.issue_key = {_EPIC_OF_I}
    LEFT JOIN people AS a ON a.account_id = i.assignee_id
    WHERE f.project_key = ?
"""


def _sprint_of(stored_key: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    """{sprint_id, name} for a finding stored under "sprint:<id>" (the name is in its evidence)."""
    sprint_id = evidence.get("sprint_id")
    if not isinstance(sprint_id, int) or isinstance(sprint_id, bool):
        raw = stored_key[len(SPRINT_PREFIX) :]
        sprint_id = int(raw) if raw.isdigit() else 0
    name = evidence.get("name") or evidence.get("sprint_name") or f"Sprint {sprint_id}"
    return {"sprint_id": sprint_id, "name": str(name)}


@dataclass(frozen=True)
class FindingRecord:

    row: sqlite3.Row
    finding_id: str
    rule_id: str
    severity: str
    state: str  # open | resolved
    issue_key: str | None
    summary: str | None
    epic_key: str | None
    sprint: dict[str, Any] | None
    first_seen_at: str
    last_seen_at: str
    resolved_at: str | None
    assignee: Person | None  # the issue's current assignee; None when unassigned, for a sprint finding and a gone issue
    person: str | None  # what the person filter matches: the assignee's id or "unassigned"; None for no one's finding

    @classmethod
    def of(cls, row: sqlite3.Row) -> FindingRecord:
        stored_key = row["issue_key"] or ""
        sprint = _sprint_of(stored_key, evidence_dict(row["evidence_json"])) if stored_key.startswith(SPRINT_PREFIX) else None
        epic = row["epic_key"] if sprint is None and ISSUE_KEY.fullmatch(row["epic_key"] or "") else None
        resolved_at = ts(row["resolved_at"])
        nobodys = sprint is not None or row["joined_key"] is None
        assignee = None if nobodys else person(row["assignee_id"], row["assignee_name"], row["assignee_avatar"])
        return cls(
            row=row,
            finding_id=row["finding_id"],
            rule_id=row["rule_id"],
            severity=row["severity"],
            state="resolved" if resolved_at else "open",
            issue_key=None if sprint is not None else stored_key,
            summary=None if sprint is not None else row["issue_summary"],
            epic_key=epic,
            sprint=sprint,
            first_seen_at=ts(row["first_seen_at"]) or "",
            last_seen_at=ts(row["last_seen_at"]) or "",
            resolved_at=resolved_at,
            assignee=assignee,
            person=None if nobodys else assignee["id"] if assignee is not None else UNASSIGNED,
        )


def load_records(
    conn: sqlite3.Connection, ctx: ApiContext, where: str = "", params: Sequence[Any] = ()
) -> list[FindingRecord]:
    """The dashboard project's findings matching `where` (an " AND ..." clause on FINDING_SQL)."""
    return [FindingRecord.of(row) for row in _rows(conn, FINDING_SQL + where, (ctx.project, *params))]


_WARNED_SQL = """
    SELECT wl.breach_finding_id AS breach_id, w.finding_id, w.rule_id, w.first_seen_at, wl.linked_at
    FROM warning_links AS wl JOIN findings AS w ON w.finding_id = wl.warning_finding_id
    WHERE wl.breach_finding_id IN ({marks})
    ORDER BY wl.breach_finding_id, w.first_seen_at, w.finding_id
"""


def warned_before(conn: sqlite3.Connection, finding_ids: Iterable[str]) -> dict[str, dict[str, str]]:
    """"Warned on X, breached on Y": the earliest warning linked to each finding (warning_links)."""
    ids = list(dict.fromkeys(finding_ids))
    found: dict[str, dict[str, str]] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        for row in _rows(conn, _WARNED_SQL.format(marks=", ".join("?" * len(chunk))), chunk):
            warned_at = ts(row["first_seen_at"]) or ts(row["linked_at"])
            if row["breach_id"] in found or warned_at is None:
                continue
            if RULE_ID.fullmatch(row["rule_id"] or "") and FINDING_ID.fullmatch(row["finding_id"] or ""):
                found[row["breach_id"]] = {
                    "warning_rule_id": row["rule_id"],
                    "warning_finding_id": row["finding_id"],
                    "warned_at": warned_at,
                }
    return found


def finding_payloads(
    conn: sqlite3.Connection, ctx: ApiContext, records: Sequence[FindingRecord], history: History | None = None
) -> list[Item]:
    """The contract's Finding objects for these records, in their order."""
    if not records:
        return []
    if history is None:
        history = load_history(conn, (record.issue_key for record in records))
    warned = warned_before(conn, (record.finding_id for record in records))
    basis = ctx.process.project(ctx.project).estimate.basis
    return [_payload(record, ctx, history, warned.get(record.finding_id), basis) for record in records]


def _payload(
    record: FindingRecord, ctx: ApiContext, history: History, warned: dict[str, str] | None, basis: str
) -> Item:
    row, key = record.row, record.issue_key
    rule = find_rule(ctx, record.rule_id)
    check = rule.check if rule is not None else None
    subject = _Subject(
        kind=row["kind"],
        last_seen_at=record.last_seen_at or None,
        evidence=evidence_dict(row["evidence_json"]),
        params=rule.params if rule is not None else {},
        basis=basis,
        issue=history.issues.get(key) if key else None,
        changes=history.status_changes.get(key, []) if key else [],
        fields=history.field_changes.get(key, []) if key else [],
        intervals=history.intervals.get(key, []) if key else [],
    )
    title = _rule_title(ctx.rules, record.rule_id)
    resolved = record.resolved_at is not None
    return {
        "finding_id": record.finding_id,
        "rule_id": record.rule_id,
        "rule_title": title,
        "rule_version": row["rule_version"],
        "kind": row["kind"],
        "severity": record.severity,
        "state": record.state,
        "message": row["message"] or title,
        "scope": "sprint" if record.sprint is not None else "issue",
        "issue_key": key,
        "issue_summary": record.summary,
        "issue_type": row["issue_type"] if key else None,
        "epic_key": record.epic_key,
        "epic_title": row["epic_title"] if record.epic_key else None,
        "assignee": dict(record.assignee) if record.assignee is not None else None,
        "sprint": record.sprint,
        "first_seen_at": record.first_seen_at,
        "last_seen_at": record.last_seen_at,
        "resolved_at": record.resolved_at,
        "resolved_why": row["resolved_why"] if resolved and row["resolved_why"] in RESOLVED_WHY else None,
        "threshold": threshold_text(check, subject),
        "evidence": evidence_items(check, subject),
        "warned_before": warned,
    }


# --- the changelog, skipped stages and worklogs of one issue ----------------------------------


def _id_order(changelog_id: object) -> tuple[int, int, str]:
    text = str(changelog_id)
    return (0, int(text), text) if text.isdigit() else (1, 0, text)


def _field_value(field: str, value: object) -> str | None:
    if field == "assignee":
        return "Assigned" if value not in (None, "") else "Unassigned"
    if field in ("story_points", "original_estimate"):
        return estimate_text(field, value)
    return None if value in (None, "") else str(value)


def change_items(history: History, key: str) -> list[Item]:
    ordered: list[tuple[tuple[Any, ...], Item]] = []
    for change in history.status_changes.get(key, []):
        at = ts(change["changed_at"])
        if at is None or change["changelog_id"] in (None, ""):
            continue
        ordered.append(
            (
                (at, _id_order(change["changelog_id"]), 0, "status"),
                {"changelog_id": str(change["changelog_id"]), "changed_at": at, "kind": "status", "field": "status",
                 "from": change["from_status"], "to": change["to_status"]},
            )
        )
    for change in history.field_changes.get(key, []):
        at, field = ts(change["changed_at"]), change["field"]
        if at is None or change["changelog_id"] in (None, "") or field not in CHANGE_FIELDS:
            continue
        ordered.append(
            (
                (at, _id_order(change["changelog_id"]), 1, field),
                {"changelog_id": str(change["changelog_id"]), "changed_at": at, "kind": "field", "field": field,
                 "from": _field_value(field, change["from_value"]), "to": _field_value(field, change["to_value"])},
            )
        )
    ordered.sort(key=lambda pair: pair[0])
    return [item for _, item in ordered]


def skipped_items(skipped_json: object, history: History, key: str) -> list[Item]:
    """Forward jumps over mapped stages (issue_metrics.skipped_stages), with when they happened."""
    try:
        skips = json.loads(skipped_json) if isinstance(skipped_json, str) and skipped_json else []
    except ValueError:
        skips = []
    when = {str(change["changelog_id"]): change["changed_at"] for change in history.status_changes.get(key, [])}
    items: list[Item] = []
    for skip in skips if isinstance(skips, list) else []:
        if not isinstance(skip, dict):
            continue
        changelog_id = str(skip.get("changelog_id") or "")
        at = ts(skip.get("changed_at") or when.get(changelog_id))
        stages = [stage for stage in skip.get("skipped") or [] if stage in CONTRACT_STAGES]
        source, target = skip.get("from"), skip.get("to")
        if changelog_id and at and stages and source and target:
            items.append(
                {"changelog_id": changelog_id, "changed_at": at, "from": str(source), "to": str(target), "skipped": stages}
            )
    items.sort(key=lambda item: (item["changed_at"], _id_order(item["changelog_id"])))
    return items


def weekly_worklogs(conn: sqlite3.Connection, key: str) -> list[Item]:
    """Logged hours per week (the Monday it starts, UTC), oldest first: totals only, never who logged them."""
    totals: dict[date, float] = defaultdict(float)
    for row in _rows(conn, "SELECT started_at, seconds FROM worklogs WHERE issue_key = ?", (key,)):
        try:
            started = parse_ts(row["started_at"])
        except (TypeError, ValueError, OverflowError):
            continue
        if started is None:
            continue
        day = started.date()
        totals[day - timedelta(days=day.weekday())] += max(0.0, num(row["seconds"]) or 0.0)
    return [{"week": week.isoformat(), "hours": round(seconds / 3600, 4)} for week, seconds in sorted(totals.items())]
