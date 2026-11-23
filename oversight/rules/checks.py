"""One function per check type. Checks only read the database, through RuleContext.

An `Issue` is a sqlite3.Row of `issues` LEFT JOIN `issue_metrics` on issue_key
(every column of both tables, plus has_metrics). A check returns None (no
hit), one Hit, or a list of Hits (one per occurrence, e.g. one per skip). Hits
are stable: the same data gives the same evidence_key on every run, so the
findings lifecycle can tell "still there" from "new". An evidence key never
depends on a board's rule settings (JO-15): the two that hold a param
(effort_ratio_at_least's threshold, no_evidence_after_percentile's pct) take it
from the catalogue's params (keyed, RuleContext.identity_params) while the
comparison uses the board's, so a ticket over both an old and a new threshold
stays the same finding, its message and evidence refreshed with the new one.
A hit's evidence holds the thresholds it was found under (threshold, pct,
grace_hours, threshold_days_left and threshold_share, beside what was
measured), so a finding a later setting closed still says what it crossed.

Messages are short, at team and process level, and name no people. They carry
no dates (the evidence does, and the reader's screen shows them in the
reader's zone). Hours use one decimal and ratios two, with the "×" sign. A
time just past its limit never reads as the limit itself ("Open for 6 days;
the limit is 6 days"): it is said to a tenth, or as "more than" the limit.

Issue-scoped checks (CHECKS) and their params:
- missing_estimate: started and no estimate on the project's basis.
- estimate_after_start {grace_hours: 1}: the estimate was first set more than
  grace_hours after work started.
- effort_ratio_at_least {threshold}: effort_ratio >= threshold.
- entered_stage_without_evidence {stage, evidence}: the first entry into the
  stage had no evidence recorded at or before it.
- skipped_stage: one Hit per forward jump over a mapped stage.
- reopened: one Hit per change from a done-category status to one that is not.
- in_progress_unassigned: in progress with nobody assigned.
- stuck_in_status {pct}: longer in the current status than the project's own
  pct-th percentile for it (silent, with a note, when history is too thin).
- no_evidence_after_percentile {stage, evidence, pct}: in the stage longer than
  the project's pct-th percentile with no evidence yet.
- unestimated_in_active_sprint: in an active sprint with no estimate.
The general checks custom rules use (JO-20; oversight/rules/registry.py holds their params and bounds). Their evidence
keys hold no param, so a limit changed never renames a finding:
- time_in_status_over {statuses, hours}: the current status is one of `statuses` (ignoring case) and the time in it
  (issue_metrics.status_age_h) is above `hours`. Key "in-status-<status>". A status the board no longer has (renamed
  or removed in Jira: not in its statuses table, once that lists any) is noted on the rule's run line once, as it is:
  only the work the last sync still shows in it is checked. A rename in Jira leaves its tickets under the old name
  until each is next updated, and they are still in that status: their findings stay open rather than closing as
  fixed because a name changed. A status removed from the workflow moves its tickets on, so nothing is left in it.
- open_longer_than {days}: not done, and created more than `days` ago. Key "open-too-long".
- no_update_for {days}: not done, and Jira's updated time is more than `days` ago. Key "no-update".
- missing_field {field}: assignee (nobody assigned), estimate (no estimate on the board's basis:
  issue_metrics.estimate_basis "none"), epic (none on the issue nor, for a subtask, on its parent) or sprint (in no
  sprint). Key "missing-<field>".
Sprint-scoped checks (SPRINT_CHECKS) take a Sprint instead of an Issue:
- sprint_at_risk {days_left, not_started_share}: an active sprint ending within
  days_left days with more than not_started_share of its issues not started; or
  still open past its end date with any of its issues not done, whatever the
  share (18 Nov 2026: such a sprint read "On track"). The finding is the same one
  before and after the end (one evidence key); its evidence says which
  (overdue) and how many issues are not done. The engine stores these under
  issue_key "sprint:<id>".
The status_category filter in applies_to is applied by the engine, not here.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from oversight.config import EstimateConfig, FieldsConfig, ProcessConfig
from oversight.derive.timeline import sort_changes
from oversight.rules import evidence as _evidence
from oversight.rules.catalog import RulesConfig
from oversight.rules.thresholds import Threshold, stage_threshold, status_age_threshold
from oversight.store.db import Store
from oversight.timeutil import hours_between, iso

Issue = sqlite3.Row

# How stages and evidence names read in a sentence.
_LABELS: dict[str, str] = {
    "code_review": "code review",
    "dev_signoff": "dev sign-off",
    "test_review": "test review",
    "test_signoff": "test sign-off",
}
_BASIS_LABELS: dict[str, str] = {"points": "story-point", "hours": "original"}
# estimate_after_start's grace when its params leave it out: an estimate set within this many hours of the start
# counts as set before it (the person scorecards read it too, oversight/api/scorecard.py).
DEFAULT_GRACE_HOURS = 1.0
# The other params a check reads with a default when its params leave them out. A board's rule settings start from
# these too where rules.yaml sets nothing (oversight/rules/settings.py).
DEFAULT_STATUS_PCT = 90.0  # stuck_in_status
DEFAULT_STAGE_PCT = 75.0  # no_evidence_after_percentile
DEFAULT_DAYS_LEFT = 2.0  # sprint_at_risk
DEFAULT_NOT_STARTED_SHARE = 0.3  # sprint_at_risk
# A note on a rule never grows past this: it is one line in a run report.
_NOTE_LIMIT = 400

_ISSUES_SQL = """
SELECT i.*,
       m.issue_key IS NOT NULL AS has_metrics,
       m.started_at, m.done_at, m.cycle_time_h, m.reopen_count, m.skipped_stages,
       m.estimate_basis, m.estimate_set_at, m.effort_ratio, m.status_age_h, m.computed_at
FROM issues i
LEFT JOIN issue_metrics m ON m.issue_key = i.issue_key
WHERE i.project_key = ?
ORDER BY i.issue_key
"""
_INTERVALS_SQL = """
SELECT si.* FROM status_intervals si JOIN issues i ON i.issue_key = si.issue_key
WHERE i.project_key = ? ORDER BY si.issue_key, si.seq
"""
_STATUS_CHANGES_SQL = """
SELECT sc.* FROM status_changes sc JOIN issues i ON i.issue_key = sc.issue_key WHERE i.project_key = ?
"""
_FIELD_CHANGES_SQL = """
SELECT fc.* FROM field_changes fc JOIN issues i ON i.issue_key = fc.issue_key WHERE i.project_key = ?
"""
_REMOTE_LINK_SQL = "SELECT 1 FROM links WHERE issue_key = ? AND kind = 'remote_link' AND instr(target, ?) > 0 LIMIT 1"


def label(name: str) -> str:
    """A stage or evidence name as it reads in a sentence ("code_review" -> "code review")."""
    return _LABELS.get(name, name.replace("_", " "))


@dataclass(frozen=True)
class Hit:
    evidence_key: str
    evidence: dict[str, Any]
    message: str


@dataclass(frozen=True)
class Sprint:
    sprint_id: int
    name: str
    state: str  # active | closed | future
    start_date: str | None  # storage format
    end_date: str | None
    issue_keys: tuple[str, ...]


@dataclass
class RuleContext:
    """Everything a check may read, for one project in one run.

    Built by the engine. The project's issues, timelines and changes are read in
    a few bulk queries the first time they are needed, then served from memory.
    """

    store: Store
    process: ProcessConfig
    fields: FieldsConfig
    rules: RulesConfig
    project: str
    now: datetime
    now_iso: str
    notes: dict[str, str] = field(default_factory=dict)
    # The rule being evaluated, so a check can leave a note on it (set by the engine).
    current_rule: str | None = None
    # The params that rule's findings are named by (Rule.identity_params, the catalogue's: set by the engine with
    # current_rule), so an evidence key never depends on a board's rule settings (keyed). None: the params given.
    identity_params: Mapping[str, Any] | None = None
    _issues: dict[str, sqlite3.Row] | None = field(default=None, init=False, repr=False)
    _intervals: dict[str, list[sqlite3.Row]] | None = field(default=None, init=False, repr=False)
    _changes: dict[str, list[dict[str, Any]]] | None = field(default=None, init=False, repr=False)
    _field_changes: dict[str, list[dict[str, Any]]] | None = field(default=None, init=False, repr=False)
    _raw: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _categories: dict[str, str] | None = field(default=None, init=False, repr=False)
    _thresholds: dict[tuple[str, str, float], Threshold | None] = field(default_factory=dict, init=False, repr=False)
    _sprints: list[Sprint] | None = field(default=None, init=False, repr=False)
    _status_names: frozenset[str] | None = field(default=None, init=False, repr=False)
    _epics: dict[str, str | None] = field(default_factory=dict, init=False, repr=False)

    @property
    def estimate(self) -> EstimateConfig:
        return self.process.estimate_for(self.project)

    # --- issues and their history ---------------------------------------------------

    def issue_rows(self) -> list[sqlite3.Row]:
        """Every issue of the project, joined to its metrics (has_metrics 0 when derive hasn't run)."""
        return list(self._load_issues().values())

    def issue(self, issue_key: str) -> sqlite3.Row | None:
        return self._load_issues().get(issue_key)

    def metrics(self, issue_key: str) -> sqlite3.Row | None:
        row = self.issue(issue_key)
        return row if row is not None and row["has_metrics"] else None

    def intervals(self, issue_key: str) -> list[sqlite3.Row]:
        if self._intervals is None:
            self._intervals = _group(self.store.query(_INTERVALS_SQL, (self.project,)))
        return self._intervals.get(issue_key, [])

    def status_changes(self, issue_key: str) -> list[dict[str, Any]]:
        """The issue's status changes as dicts, sorted by (changed_at, numeric changelog id)."""
        if self._changes is None:
            self._changes = _sorted_history(self.store.query(_STATUS_CHANGES_SQL, (self.project,)))
        return self._changes.get(issue_key, [])

    def field_changes(self, issue_key: str, field_name: str | None = None) -> list[dict[str, Any]]:
        """The issue's tracked field changes as dicts, in the same order; optionally one logical field."""
        if self._field_changes is None:
            self._field_changes = _sorted_history(self.store.query(_FIELD_CHANGES_SQL, (self.project,)))
        rows = self._field_changes.get(issue_key, [])
        return rows if field_name is None else [row for row in rows if row["field"] == field_name]

    def raw(self, issue_key: str) -> dict[str, Any]:
        if issue_key not in self._raw:
            row = self.issue(issue_key)
            parsed: Any = None
            if row is not None and row["raw_json"]:
                try:
                    parsed = json.loads(row["raw_json"])
                except ValueError:
                    parsed = None
            self._raw[issue_key] = parsed if isinstance(parsed, dict) else {}
        return self._raw[issue_key]

    def has_remote_link(self, issue_key: str, text: str) -> bool:
        return self.store.query_one(_REMOTE_LINK_SQL, (issue_key, text)) is not None

    # --- vocabulary -------------------------------------------------------------------

    def stage_of(self, status: str | None) -> str | None:
        return self.process.stage_of(self.project, status)

    def category_of(self, status: str | None, issue: Issue | None = None) -> str | None:
        """new | indeterminate | done, from the statuses table; a status mapped to done_stage counts as done.

        With `issue`, a status missing from the table (renamed, removed, or from
        the project the issue moved from) that is the issue's current status
        takes the issue's own status_category: derive's lookup
        (derive/run.py _category_lookup) does the same, so a rule and the
        metrics never disagree about whether a change was a reopen.
        """
        if status is None:
            return None
        if self._categories is None:
            rows = self.store.query("SELECT name, category FROM statuses WHERE project_key = ?", (self.project,))
            self._categories = {r["name"].casefold(): r["category"] for r in rows if r["name"] and r["category"]}
        found = self._categories.get(status.casefold())
        if found:
            return found
        if self.stage_of(status) == self.process.done_stage:
            return "done"
        if issue is not None and issue["status"] and status.casefold() == str(issue["status"]).casefold():
            return issue["status_category"] or None
        return None

    def status_names(self) -> frozenset[str]:
        """The board's status names as the statuses table lists them, folded for case; empty before its first sync."""
        if self._status_names is None:
            rows = self.store.query("SELECT name FROM statuses WHERE project_key = ?", (self.project,))
            self._status_names = frozenset(row["name"].casefold() for row in rows if row["name"])
        return self._status_names

    def epic_of(self, issue: Issue) -> str | None:
        """The issue's epic, or for a subtask its parent's (as the catalogue reads it: COALESCE of the two)."""
        if issue["epic_key"]:
            return str(issue["epic_key"])
        parent = issue["parent_key"]
        if not parent:
            return None
        if parent not in self._epics:
            row = self.issue(parent) or self.store.query_one("SELECT epic_key FROM issues WHERE issue_key = ?", (parent,))
            self._epics[parent] = row["epic_key"] if row is not None and row["epic_key"] else None
        return self._epics[parent]

    # --- thresholds, sprints, evidence ------------------------------------------------

    def status_threshold(self, status: str, pct: float) -> Threshold | None:
        key = ("status", status.casefold(), pct)
        if key not in self._thresholds:
            d = self.rules.defaults
            self._thresholds[key] = status_age_threshold(
                self.store, self.project, status, pct=pct, min_history=d.min_history,
                history_days=d.history_days, now=self.now, exclude_types=d.exclude_types,
            )
        return self._thresholds[key]

    def stage_threshold(self, stage: str, pct: float) -> Threshold | None:
        key = ("stage", stage.casefold(), pct)
        if key not in self._thresholds:
            d = self.rules.defaults
            self._thresholds[key] = stage_threshold(
                self.store, self.project, stage, pct=pct, min_history=d.min_history,
                history_days=d.history_days, now=self.now, exclude_types=d.exclude_types,
            )
        return self._thresholds[key]

    def active_sprints(self) -> list[Sprint]:
        """Active sprints named in the issues' sprint fields, with the issues that list them."""
        if self._sprints is None:
            field_id = self.fields.id_of("sprint")
            meta: dict[int, dict[str, Any]] = {}
            members: dict[int, set[str]] = defaultdict(set)
            for key in self._load_issues():
                for sprint in _sprint_objects(self.raw(key), field_id):
                    sprint_id = _int(sprint.get("id"))
                    if sprint_id is None:
                        continue
                    members[sprint_id].add(key)
                    if sprint.get("state") == "active":
                        meta.setdefault(sprint_id, sprint)
            self._sprints = [
                Sprint(
                    sprint_id=sprint_id,
                    name=str(sprint.get("name") or f"Sprint {sprint_id}"),
                    state="active",
                    start_date=_iso_or_none(sprint.get("startDate")),
                    end_date=_iso_or_none(sprint.get("endDate")),
                    issue_keys=tuple(sorted(members[sprint_id])),
                )
                for sprint_id, sprint in sorted(meta.items())
            ]
        return self._sprints

    def evidence_time(self, issue_key: str, name: str) -> str | None:
        return _evidence.evidence_time(self, issue_key, name)

    def note(self, rule_id: str, text: str) -> None:
        """Add a note to the rule's run report line, once per distinct text."""
        current = self.notes.get(rule_id)
        if current is None:
            self.notes[rule_id] = text
        elif text not in current and len(current) < _NOTE_LIMIT:
            self.notes[rule_id] = f"{current}; {text}"

    def _load_issues(self) -> dict[str, sqlite3.Row]:
        if self._issues is None:
            self._issues = {row["issue_key"]: row for row in self.store.query(_ISSUES_SQL, (self.project,))}
        return self._issues


CheckResult = Hit | list[Hit] | None
CheckFn = Callable[[Issue, RuleContext, dict[str, Any]], CheckResult]
SprintCheckFn = Callable[[Sprint, RuleContext, dict[str, Any]], CheckResult]


def keyed(ctx: RuleContext, params: Mapping[str, Any], name: str, default: Any) -> Any:
    """A param a check names its findings by (it is part of the evidence key): the catalogue's value while the engine
    runs the rule with a board's settings (ctx.identity_params), so changing a threshold on one board never gives a
    finding a new id; otherwise the param as given. `default`: the value when the params read leave it out."""
    source = params if ctx.identity_params is None else ctx.identity_params
    return source.get(name, default)


# --- the checks ---------------------------------------------------------------------------


def check_missing_estimate(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    if not issue["started_at"] or issue["estimate_basis"] != "none":
        return None
    basis = ctx.estimate.basis
    return Hit(
        "no-estimate",
        {"started_at": issue["started_at"], "basis": basis},
        f"Work started with no {_BASIS_LABELS[basis]} estimate",
    )


def check_estimate_after_start(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    started, set_at = issue["started_at"], issue["estimate_set_at"]
    if not started or not set_at:
        return None
    after = hours_between(started, set_at)
    grace = float(params.get("grace_hours", DEFAULT_GRACE_HOURS))
    if after <= grace:
        return None
    return Hit(
        "estimate-after-start",
        {"started_at": started, "estimate_set_at": set_at, "hours_after": round(after, 1), "grace_hours": grace},
        f"Estimate first set {_duration(after)} after work started",
    )


def check_effort_ratio_at_least(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    ratio = issue["effort_ratio"]
    threshold = float(params["threshold"])
    if ratio is None or ratio < threshold:
        return None
    estimate = ctx.estimate
    if estimate.basis == "points":
        estimate_h = (issue["story_points"] or 0) * (estimate.hours_per_point or 0)
    else:
        estimate_h = (issue["original_estimate_s"] or 0) / 3600
    spent_h = (issue["time_spent_s"] or 0) / 3600
    named = float(keyed(ctx, params, "threshold", threshold))  # the catalogue's threshold names the finding
    return Hit(
        f"ratio-ge-{named:g}",
        {
            "effort_ratio": round(ratio, 2),
            "estimate_h": round(estimate_h, 1),
            "time_spent_h": round(spent_h, 1),
            "threshold": threshold,
        },
        f"Logged {spent_h:.1f} h against an estimate of {estimate_h:.1f} h ({ratio:.2f}×; threshold {threshold:g}×)",
    )


def check_entered_stage_without_evidence(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    stage, evidence = params["stage"], params["evidence"]
    key = issue["issue_key"]
    entered = next((iv for iv in ctx.intervals(key) if iv["stage"] == stage), None)
    if entered is None or _evidence.has_evidence_before(ctx, key, evidence, entered["entered_at"]):
        return None
    return Hit(
        f"{stage}-{entered['seq']}",
        {"stage": stage, "entered_at": entered["entered_at"], "evidence": evidence, "status": entered["status"]},
        f"Entered {label(stage)} ({entered['status']}) with no {label(evidence)} recorded before it",
    )


def check_skipped_stage(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    try:
        skips = json.loads(issue["skipped_stages"] or "[]")
    except ValueError:
        return None
    changed_at = {str(ch["changelog_id"]): ch["changed_at"] for ch in ctx.status_changes(issue["issue_key"])}
    hits = []
    for skip in skips if isinstance(skips, list) else []:
        changelog_id = str(skip.get("changelog_id"))
        stages = [str(s) for s in skip.get("skipped") or []]
        source, target = skip.get("from"), skip.get("to")
        hits.append(
            Hit(
                f"skip-{changelog_id}",
                {"changelog_id": changelog_id, "from": source, "to": target, "skipped": stages,
                 "changed_at": changed_at.get(changelog_id)},
                f"{source} → {target} skipped {_join(label(s) for s in stages)}",
            )
        )
    return hits or None


def check_reopened(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    hits = []
    for change in ctx.status_changes(issue["issue_key"]):
        before, after = ctx.category_of(change["from_status"], issue), ctx.category_of(change["to_status"], issue)
        if before == "done" and after != "done":
            hits.append(
                Hit(
                    f"reopen-{change['changelog_id']}",
                    {"changelog_id": str(change["changelog_id"]), "changed_at": change["changed_at"],
                     "from": change["from_status"], "to": change["to_status"]},
                    f"Reopened: {change['from_status']} → {change['to_status']}",
                )
            )
    return hits or None


def check_in_progress_unassigned(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    if issue["status_category"] != "indeterminate" or issue["assignee_id"]:
        return None
    age = issue["status_age_h"]
    # Status names are quoted: "In In Progress" reads as a typo in the digest.
    message = f"In '{issue['status']}' with nobody assigned"
    if age is not None:
        message += f" for {_duration(age)}"
    return Hit("unassigned", {"status": issue["status"], "status_age_h": None if age is None else round(age, 1)}, message)


def check_stuck_in_status(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    status, age = issue["status"], issue["status_age_h"]
    if not status or age is None:
        return None
    pct = float(params.get("pct", DEFAULT_STATUS_PCT))
    limit = ctx.status_threshold(status, pct)
    if limit is None:
        _note_thin_history(ctx, status)
        return None
    if age <= limit.hours:
        return None
    return Hit(
        f"stuck-{status}",
        {"status": status, "age_h": round(age, 1), "threshold_h": round(limit.hours, 1), "pct": pct,
         "history_n": limit.n},
        f"In '{status}' for {_duration(age)}; the project's p{pct:g} for '{status}' is {_duration(limit.hours)}",
    )


def check_no_evidence_after_percentile(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    stage, evidence = params["stage"], params["evidence"]
    pct = float(params.get("pct", DEFAULT_STAGE_PCT))
    timeline = ctx.intervals(issue["issue_key"])
    if not timeline or timeline[-1]["left_at"] is not None or timeline[-1]["stage"] != stage:
        return None
    limit = ctx.stage_threshold(stage, pct)
    if limit is None:
        _note_thin_history(ctx, label(stage))
        return None
    hours_in = max(0.0, hours_between(timeline[-1]["entered_at"], ctx.now))
    if hours_in <= limit.hours or ctx.evidence_time(issue["issue_key"], evidence) is not None:
        return None
    named = float(keyed(ctx, params, "pct", DEFAULT_STAGE_PCT))  # the catalogue's percentile names the finding
    return Hit(
        f"no-{evidence}-after-p{named:g}",
        {"stage": stage, "evidence": evidence, "hours_in_stage": round(hours_in, 1),
         "threshold_h": round(limit.hours, 1), "pct": pct},
        f"In {label(stage)} for {_duration(hours_in)} (the project's p{pct:g} is {_duration(limit.hours)}) "
        f"with no {label(evidence)} recorded",
    )


def check_unestimated_in_active_sprint(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    if issue["estimate_basis"] != "none":
        return None
    basis = ctx.estimate.basis
    hits = [
        Hit(
            f"unestimated-sprint-{sprint.sprint_id}",
            {"sprint_id": sprint.sprint_id, "sprint_name": sprint.name, "basis": basis},
            f"In the active sprint {sprint.name} with no {_BASIS_LABELS[basis]} estimate",
        )
        for sprint in ctx.active_sprints()
        if issue["issue_key"] in sprint.issue_keys
    ]
    return hits or None


def check_sprint_at_risk(sprint: Sprint, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    if not sprint.end_date:
        return None
    days_left = hours_between(ctx.now, sprint.end_date) / 24
    within = float(params.get("days_left", DEFAULT_DAYS_LEFT))
    if days_left > within:
        return None
    excluded = {t.casefold() for t in ctx.rules.defaults.exclude_types}
    # Issues derive hasn't reached are skipped, as the engine skips them for every rule (and
    # notes it): with no metrics, started_at is unknown, not "not started".
    members = [row for row in (ctx.metrics(k) for k in sprint.issue_keys) if row is not None]
    members = [row for row in members if (row["issue_type"] or "").casefold() not in excluded]
    if not members:
        return None
    not_done = [row for row in members if row["status_category"] != "done"]
    not_started = [row for row in not_done if not row["started_at"]]
    share = len(not_started) / len(members)
    over = float(params.get("not_started_share", DEFAULT_NOT_STARTED_SHARE))
    # Past its end with work not done, the sprint has overrun: at risk whatever share has started.
    overdue = days_left < 0 and bool(not_done)
    if share <= over and not overdue:
        return None
    if overdue:
        message = f"{sprint.name} ended {-days_left:.1f} days ago with {len(not_done)} of {len(members)} issues not done"
    else:
        message = (f"{sprint.name} ends in {days_left:.1f} days with {len(not_started)} of {len(members)} issues "
                   f"not started ({share:.0%})")
    return Hit(
        f"sprint-{sprint.sprint_id}-at-risk",
        {"sprint_id": sprint.sprint_id, "name": sprint.name, "end_date": sprint.end_date,
         "days_left": round(days_left, 1), "issues": len(members), "not_started": len(not_started),
         "not_done": len(not_done), "overdue": overdue, "share": round(share, 3), "threshold_days_left": within,
         "threshold_share": over},
        message,
    )


def check_time_in_status_over(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    statuses = [str(name) for name in params["statuses"]]
    limit = float(params["hours"])
    known = ctx.status_names()
    for name in statuses:
        if known and name.casefold() not in known:
            ctx.note(ctx.current_rule or "", f"'{name}' is no longer a status on this board: only work the last sync "
                                             "still shows in it is checked")
    status, age = issue["status"], issue["status_age_h"]
    if not status or age is None or status.casefold() not in {name.casefold() for name in statuses}:
        return None
    if age <= limit:
        return None
    return Hit(
        f"in-status-{status}",
        {"status": status, "age_h": round(age, 1), "threshold_h": limit},
        f"In '{status}' for {_time_past(age, limit)}; the limit is {_limit_hours(limit)}",
    )


def check_open_longer_than(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    created = issue["created_at"]
    if issue["status_category"] == "done" or not created:
        return None
    days, limit = hours_between(created, ctx.now) / 24, float(params["days"])
    if days <= limit:
        return None
    return Hit(
        "open-too-long",
        {"created_at": created, "open_days": round(days, 1), "threshold_days": limit},
        f"Open for {_days_past(days, limit)}; the limit is {_days(limit)}",
    )


def check_no_update_for(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    updated = issue["updated_at"]
    if issue["status_category"] == "done" or not updated:
        return None
    days, limit = hours_between(updated, ctx.now) / 24, float(params["days"])
    if days <= limit:
        return None
    return Hit(
        "no-update",
        {"updated_at": updated, "idle_days": round(days, 1), "threshold_days": limit},
        f"No change in Jira for {_days_past(days, limit)}; the limit is {_days(limit)}",
    )


def check_missing_field(issue: Issue, ctx: RuleContext, params: dict[str, Any]) -> CheckResult:
    field_name = str(params["field"])
    if field_name == "assignee":
        missing, message = not issue["assignee_id"], "No assignee"
    elif field_name == "estimate":
        missing, message = issue["estimate_basis"] == "none", f"No {_BASIS_LABELS[ctx.estimate.basis]} estimate"
    elif field_name == "epic":
        missing, message = ctx.epic_of(issue) is None, "No epic"
    elif field_name == "sprint":
        missing, message = not _sprint_ids(issue["sprint_ids"]), "Not in a sprint"
    else:  # a field the registry does not offer: nothing to look for
        return None
    if not missing:
        return None
    return Hit(f"missing-{field_name}", {"field": field_name, "status": issue["status"]}, message)


CHECKS: dict[str, CheckFn] = {
    "missing_estimate": check_missing_estimate,
    "estimate_after_start": check_estimate_after_start,
    "effort_ratio_at_least": check_effort_ratio_at_least,
    "entered_stage_without_evidence": check_entered_stage_without_evidence,
    "skipped_stage": check_skipped_stage,
    "reopened": check_reopened,
    "in_progress_unassigned": check_in_progress_unassigned,
    "stuck_in_status": check_stuck_in_status,
    "no_evidence_after_percentile": check_no_evidence_after_percentile,
    "unestimated_in_active_sprint": check_unestimated_in_active_sprint,
    "time_in_status_over": check_time_in_status_over,
    "open_longer_than": check_open_longer_than,
    "no_update_for": check_no_update_for,
    "missing_field": check_missing_field,
}

SPRINT_CHECKS: dict[str, SprintCheckFn] = {
    "sprint_at_risk": check_sprint_at_risk,
}

CHECK_NAMES = frozenset(CHECKS)
SPRINT_CHECK_NAMES = frozenset(SPRINT_CHECKS)


# --- helpers ------------------------------------------------------------------------------


def _duration(hours: float) -> str:
    return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.1f} h"


def _limit_hours(hours: float) -> str:
    """A limit in hours as _duration reads a time (days from 48 hours on), without the trailing ".0": "3 days"."""
    return f"{round(hours / 24, 1):g} days" if hours >= 48 else f"{round(hours, 1):g} h"


def _shown_hours(hours: float) -> float:
    """A time as _duration and _limit_hours show it, to a tenth of its unit (days from 48 hours on), in hours again."""
    return round(hours / 24, 1) * 24 if hours >= 48 else round(hours, 1)


def _time_past(hours: float, limit: float) -> str:
    """A time over a limit in hours, as _duration says it ("3.4 days"), unless that would read as no more than the
    limit as _limit_hours says it ("29.0 h" past a limit of "29 h"): then "more than" the limit."""
    return _duration(hours) if _shown_hours(hours) > _shown_hours(limit) else f"more than {_limit_hours(limit)}"


def _days(days: float) -> str:
    return f"{days:g} {'day' if days == 1 else 'days'}"


def _days_past(days: float, limit: float) -> str:
    """A time over a limit in days, never reading as the limit itself: the whole days gone by while they are over it
    ("41 days"), else the tenths ("6.4 days"), else "more than" the limit ("more than 6 days")."""
    whole = float(math.floor(days))
    if whole > limit:
        return _days(whole)
    tenths = math.floor(days * 10) / 10
    return f"{tenths:.1f} days" if tenths > limit else f"more than {_days(limit)}"


def _sprint_ids(value: object) -> list[Any]:
    """An issue's sprint_ids (a JSON list as stored); [] when there are none or they cannot be read."""
    if not isinstance(value, str) or not value:
        return []
    try:
        ids = json.loads(value)
    except ValueError:
        return []
    return ids if isinstance(ids, list) else []


def join_words(items: Iterable[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    words = list(items)
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


_join = join_words


def _note_thin_history(ctx: RuleContext, what: str) -> None:
    d = ctx.rules.defaults
    ctx.note(
        ctx.current_rule or "",
        f"{what}: fewer than {d.min_history} closed intervals in {d.history_days} days, no threshold",
    )


def _group(rows: Iterable[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[row["issue_key"]].append(row)
    return dict(grouped)


def _sorted_history(rows: Iterable[sqlite3.Row]) -> dict[str, list[dict[str, Any]]]:
    # sort_changes reads rows with .get(), which sqlite3.Row lacks: hand it dicts.
    return {key: sort_changes([dict(row) for row in group]) for key, group in _group(rows).items()}  # type: ignore[misc]


def _sprint_objects(raw: dict[str, Any], field_id: str | None) -> list[dict[str, Any]]:
    """Sprint objects in an issue payload: under the sprint field, or in any field that holds them."""
    fields = raw.get("fields") or {}
    if not isinstance(fields, dict):
        return []
    candidates = [fields.get(field_id)] if field_id else list(fields.values())
    found = []
    for value in candidates:
        if isinstance(value, list):
            found.extend(item for item in value if isinstance(item, dict) and "id" in item and "state" in item)
    return found


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _iso_or_none(value: object) -> str | None:
    try:
        return iso(value)
    except (TypeError, ValueError):
        return None
