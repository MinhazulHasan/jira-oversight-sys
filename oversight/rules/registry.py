from __future__ import annotations

import math
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from oversight.config import EstimateConfig, ProcessConfig
from oversight.rules.checks import (
    DEFAULT_DAYS_LEFT,
    DEFAULT_GRACE_HOURS,
    DEFAULT_NOT_STARTED_SHARE,
    DEFAULT_STAGE_PCT,
    DEFAULT_STATUS_PCT,
    join_words,
    label,
)

Unit = Literal["ratio", "hours", "percentile", "days", "share"]
Kind = Literal["number", "statuses", "stage", "evidence", "field"]
DECIMALS = 4  # a value has at most this many decimal places
CATEGORIES: tuple[str, ...] = ("new", "indeterminate", "done")
# What missing_field can look for, and how each reads as a choice.
FIELDS: tuple[str, ...] = ("assignee", "estimate", "epic", "sprint")
FIELD_LABELS: dict[str, str] = {"assignee": "Assignee", "estimate": "Estimate", "epic": "Epic", "sprint": "Sprint"}
TITLE_MIN, TITLE_MAX = 3, 80  # characters
DESCRIPTION_MAX = 300  # characters
CUSTOM_LIMIT = 50  # custom rules (not deleted) on one board
# The registry's own starting values where a check reads a param it has no default for.
DEFAULT_HOURS_IN_STATUS = 72.0
DEFAULT_OPEN_DAYS = 30.0
DEFAULT_IDLE_DAYS = 7.0
DEFAULT_EFFORT_RATIO = 1.5  # R-EFF-01's in rules.yaml


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    unit: Unit
    min: float
    max: float
    step: float
    default: float | None

    def bounds(self) -> str:
        """The bounds as a reader says them: "0.1 to 10", "0 to 168 hours", "5% to 95%"."""
        if self.unit == "share":
            return f"{_num(self.min * 100)}% to {_num(self.max * 100)}%"
        suffix = {"hours": " hours", "days": " days"}.get(self.unit, "")
        return f"{_num(self.min)} to {_num(self.max)}{suffix}"

    def words(self, value: float) -> str:
        """A value in its unit, as the dashboard's field shows it: "1.5×", "1 h", "p90", "2 days", "30%"."""
        if self.unit == "ratio":
            return f"{_num(value)}×"
        if self.unit == "hours":
            return f"{_num(value)} h"
        if self.unit == "percentile":
            return f"p{_num(value)}"
        if self.unit == "days":
            return f"{_num(value)} {'day' if value == 1 else 'days'}"
        return f"{_num(round(value * 100, 2))}%"


EDITABLE: dict[str, tuple[ParamSpec, ...]] = {
    "estimate_after_start": (
        ParamSpec("grace_hours", "Grace after work started", "hours", 0.0, 168.0, 0.5, DEFAULT_GRACE_HOURS),
    ),
    "effort_ratio_at_least": (ParamSpec("threshold", "Effort ratio", "ratio", 0.1, 10.0, 0.05, None),),
    "stuck_in_status": (
        ParamSpec("pct", "Percentile of time in the status", "percentile", 50.0, 99.0, 1.0, DEFAULT_STATUS_PCT),
    ),
    "no_evidence_after_percentile": (
        ParamSpec("pct", "Percentile of time in the stage", "percentile", 50.0, 99.0, 1.0, DEFAULT_STAGE_PCT),
    ),
    "sprint_at_risk": (
        ParamSpec("days_left", "Days left in the sprint", "days", 0.0, 14.0, 0.5, DEFAULT_DAYS_LEFT),
        ParamSpec(
            "not_started_share", "Share of issues not started", "share", 0.05, 0.95, 0.05, DEFAULT_NOT_STARTED_SHARE
        ),
    ),
}


def _editable(check: str, name: str) -> ParamSpec:
    return next(spec for spec in EDITABLE[check] if spec.name == name)


# --- the checks ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckParam:
    name: str
    label: str
    kind: Kind
    spec: ParamSpec | None = None
    start: float | str | None = None

    @property
    def default(self) -> float | str | None:
        return self.start if self.start is not None else (self.spec.default if self.spec is not None else None)


@dataclass(frozen=True)
class CheckSpec:
    name: str
    label: str
    summary: str
    scope: Literal["issue", "sprint"]
    params: tuple[CheckParam, ...]
    categories: tuple[str, ...]
    default_categories: tuple[str, ...]

    def param(self, name: str) -> CheckParam | None:
        return next((param for param in self.params if param.name == name), None)


def _number_param(spec: ParamSpec, start: float | None = None) -> CheckParam:
    return CheckParam(spec.name, spec.label, "number", spec, start)


_ANY = CATEGORIES
_OPEN = ("new", "indeterminate")
_STATUSES = CheckParam("statuses", "Statuses", "statuses")
_STAGE = CheckParam("stage", "Stage", "stage")
_EVIDENCE = CheckParam("evidence", "Evidence", "evidence")

CHECK_SPECS: dict[str, CheckSpec] = {spec.name: spec for spec in (
    CheckSpec(
        "time_in_status_over", "Time in a status", "Flags work that has sat in one of these statuses past the limit.",
        "issue", (_STATUSES, _number_param(ParamSpec(
            "hours", "Time in the status", "hours", 1.0, 8760.0, 1.0, DEFAULT_HOURS_IN_STATUS))),
        _OPEN, ("indeterminate",),
    ),
    CheckSpec(
        "open_longer_than", "Open too long", "Flags work still open this many days after it was created.", "issue",
        (_number_param(ParamSpec("days", "Days open", "days", 1.0, 3650.0, 1.0, DEFAULT_OPEN_DAYS)),),
        _OPEN, _OPEN,
    ),
    CheckSpec(
        "no_update_for", "No updates", "Flags open work with no change in Jira for this many days.", "issue",
        (_number_param(ParamSpec("days", "Days without a change", "days", 1.0, 365.0, 1.0, DEFAULT_IDLE_DAYS)),),
        _OPEN, ("indeterminate",),
    ),
    CheckSpec(
        "missing_field", "Missing field", "Flags work with no assignee, estimate, epic or sprint.", "issue",
        (CheckParam("field", "Field", "field", start="assignee"),),
        _ANY, ("indeterminate",),
    ),
    CheckSpec(
        "missing_estimate", "Started without an estimate", "Flags work that started with no estimate.", "issue", (),
        ("indeterminate", "done"), ("indeterminate", "done"),
    ),
    CheckSpec(
        "estimate_after_start", "Estimate set late", "Flags work whose estimate was first set after work started.",
        "issue", (_number_param(_editable("estimate_after_start", "grace_hours")),),
        ("indeterminate", "done"), ("indeterminate", "done"),
    ),
    CheckSpec(
        "effort_ratio_at_least", "Effort over estimate",
        "Flags work whose logged time is at least this many times its estimate.", "issue",
        (_number_param(_editable("effort_ratio_at_least", "threshold"), DEFAULT_EFFORT_RATIO),),
        ("indeterminate", "done"), ("done",),
    ),
    CheckSpec(
        "entered_stage_without_evidence", "Reached a stage without evidence",
        "Flags work that reached a stage with no evidence recorded before it.", "issue", (_STAGE, _EVIDENCE),
        _ANY, _ANY,
    ),
    CheckSpec(
        "skipped_stage", "Skipped a stage", "Flags a status change that jumped over a stage the workflow maps.",
        "issue", (), _ANY, _ANY,
    ),
    CheckSpec(
        "reopened", "Reopened after Done", "Flags work moved out of Done to a status that is not Done.", "issue", (),
        _ANY, _ANY,
    ),
    CheckSpec(
        "in_progress_unassigned", "In progress, no assignee", "Flags work in progress that nobody is assigned to.",
        "issue", (), ("indeterminate",), ("indeterminate",),
    ),
    CheckSpec(
        "stuck_in_status", "Unusually long in a status",
        "Flags work in its status longer than this board usually takes there.", "issue",
        (_number_param(_editable("stuck_in_status", "pct")),),
        _OPEN, _OPEN,
    ),
    CheckSpec(
        "no_evidence_after_percentile", "No evidence after usual time",
        "Flags work in a stage longer than usual with no evidence yet.", "issue",
        (_STAGE, _EVIDENCE, _number_param(_editable("no_evidence_after_percentile", "pct"))),
        ("indeterminate",), ("indeterminate",),
    ),
    CheckSpec(
        "unestimated_in_active_sprint", "Unestimated in the sprint",
        "Flags work in the active sprint that has no estimate.", "issue", (), _OPEN, _OPEN,
    ),
    CheckSpec(
        "sprint_at_risk", "Sprint at risk", "Flags an active sprint near its end with much of its work not started.",
        "sprint", tuple(_number_param(spec) for spec in EDITABLE["sprint_at_risk"]),
        (), (),
    ),
)}


def check_label(check: str) -> str:
    """The check's label ("Time in a status"); the name itself for a check the registry does not hold."""
    spec = CHECK_SPECS.get(check)
    return spec.label if spec is not None else check.replace("_", " ")


def reads(check: str, params: Mapping[str, Any], *, history_days: int, estimate: EstimateConfig,
          requires_evidence: Sequence[str] = (), requires_stages: Sequence[str] = ()) -> list[str]:
    """What a rule of this check reads from Jira, in plain words (GET /rules' `needs`)."""
    sized = "Estimate (story points)" if estimate.basis == "points" else "Estimate (original estimate)"
    history = f"Closed work from the last {history_days} days"
    plain: dict[str, list[str]] = {
        "missing_estimate": [sized, "Status history"],
        "estimate_after_start": [sized, "Field history"],
        "effort_ratio_at_least": [sized, "Logged time"],
        "skipped_stage": ["Mapped stages", "Status history"],
        "reopened": ["Status history"],
        "in_progress_unassigned": ["Assignee"],
        "stuck_in_status": ["Status history", history],
        "sprint_at_risk": ["Sprints"],
        "unestimated_in_active_sprint": ["Sprints", sized],
        "time_in_status_over": ["Status history"],
        "open_longer_than": ["Created date"],
        "no_update_for": ["Last updated date"],
    }
    if check in plain:
        return plain[check]
    if check == "missing_field" and params.get("field") in FIELDS:
        return {"assignee": ["Assignee"], "estimate": [sized], "epic": ["Epic"], "sprint": ["Sprints"]}[
            str(params["field"])]
    if check in ("entered_stage_without_evidence", "no_evidence_after_percentile") and params.get("evidence"):
        evidence = f"{_capital(label(str(params['evidence'])))} evidence"
        if check == "no_evidence_after_percentile":
            return [evidence, history]
        if params.get("stage"):
            return [evidence, f"A {label(str(params['stage']))} stage in the workflow"]
    return [f"{_capital(label(name))} evidence" for name in requires_evidence] + [
        f"A {label(name)} stage in the workflow" for name in requires_stages
    ]


def threshold_words(check: str, params: Mapping[str, Any]) -> str | None:
    try:
        if check == "estimate_after_start":
            return f"{_num(float(params.get('grace_hours', DEFAULT_GRACE_HOURS)))} h grace"
        if check == "effort_ratio_at_least":
            return f"{_num(float(params['threshold']))}×"
        if check == "stuck_in_status":
            return f"p{_num(float(params.get('pct', DEFAULT_STATUS_PCT)))} of time in the status"
        if check == "no_evidence_after_percentile":
            return f"p{_num(float(params.get('pct', DEFAULT_STAGE_PCT)))} of time in {label(str(params['stage']))}"
        if check == "sprint_at_risk":
            days = float(params.get("days_left", DEFAULT_DAYS_LEFT))
            share = float(params.get("not_started_share", DEFAULT_NOT_STARTED_SHARE))
            return f"≤ {_num(days)} {'day' if days == 1 else 'days'} left, > {_num(share * 100)}% not started"
        if check == "time_in_status_over":
            return f"{hours_words(float(params['hours']))} in {join_names(params['statuses'])}"
        if check == "open_longer_than":
            return f"{days_words(float(params['days']))} open"
        if check == "no_update_for":
            return f"{days_words(float(params['days']))} without a change"
    except (KeyError, TypeError, ValueError):
        return None
    return None


def hours_words(hours: float) -> str:
    if hours >= 48:
        return f"{_num(round(hours / 24, 1))} days"
    return f"{_num(round(hours, 1))} h"


def days_words(days: float) -> str:
    """"1 day", "30 days", "2.5 days"."""
    return f"{_num(days)} {'day' if days == 1 else 'days'}"


def join_names(names: object) -> str:
    items = [f"'{name}'" for name in names] if isinstance(names, (list, tuple)) else [f"'{names}'"]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]


# --- the board's options --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardOptions:
    project: str
    statuses: tuple[tuple[str, str | None], ...] = ()
    issue_types: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    recorded: frozenset[str] = frozenset()
    estimate_basis: str = "points"

    def status(self, name: str) -> str | None:
        """The board's own spelling of a status name (ignoring case); None when it has no such status."""
        wanted = name.casefold()
        return next((status for status, _category in self.statuses if status.casefold() == wanted), None)

    def category_of(self, name: str) -> str | None:
        wanted = name.casefold()
        return next((category for status, category in self.statuses if status.casefold() == wanted), None)

    def issue_type(self, name: str) -> str | None:
        wanted = name.casefold()
        return next((found for found in self.issue_types if found.casefold() == wanted), None)


_STATUSES_SQL = "SELECT name, category FROM statuses WHERE project_key = ?"
_ISSUE_TYPES_SQL = "SELECT DISTINCT issue_type FROM issues WHERE project_key = ? AND issue_type IS NOT NULL"
_CATEGORY_ORDER = {category: n for n, category in enumerate(CATEGORIES)}


def board_options(
    conn: sqlite3.Connection, process: ProcessConfig, project: str, *, exclude_types: Iterable[str] = ("Epic",)
) -> BoardOptions:
    """The board's options (BoardOptions), through any connection (a read-only one too)."""
    config = process.project(project)
    statuses: dict[str, tuple[str, str | None]] = {}
    for row in conn.execute(_STATUSES_SQL, (project,)).fetchall():
        name, category = row[0], row[1]
        if isinstance(name, str) and name.strip() and name.casefold() not in statuses:
            statuses[name.casefold()] = (name, category if category in CATEGORIES else None)
    if not statuses:
        for name in config.status_to_stage:
            statuses.setdefault(name.casefold(), (name, None))
    ordered = sorted(statuses.values(), key=lambda item: (_CATEGORY_ORDER.get(item[1] or "", 3), item[0].casefold()))
    excluded = {name.casefold() for name in exclude_types}
    types = {
        str(row[0]).casefold(): str(row[0])
        for row in conn.execute(_ISSUE_TYPES_SQL, (project,)).fetchall()
        if str(row[0]).strip() and str(row[0]).casefold() not in excluded
    }
    evidence = [stage for stage in process.stages if stage in config.evidence]
    return BoardOptions(
        project=project,
        statuses=tuple(ordered),
        issue_types=tuple(sorted(types.values(), key=str.casefold)),
        stages=tuple(process.ordered_stages(project)),
        evidence=tuple(evidence),
        recorded=frozenset(name for name in evidence if process.has_evidence(project, name)),
        estimate_basis=config.estimate.basis,
    )


def options_of(param: CheckParam, options: BoardOptions) -> list[dict[str, Any]] | None:
    if param.kind == "statuses":
        return [{"value": name, "label": name, "category": category} for name, category in options.statuses]
    if param.kind == "stage":
        return [{"value": stage, "label": _capital(label(stage))} for stage in options.stages]
    if param.kind == "evidence":
        return [{"value": name, "label": _capital(label(name))} for name in options.evidence]
    if param.kind == "field":
        return [{"value": name, "label": FIELD_LABELS[name]} for name in FIELDS]
    return None


def param_payload(param: CheckParam, options: BoardOptions) -> dict[str, Any]:
    spec = param.spec
    return {
        "name": param.name,
        "label": param.label,
        "kind": param.kind,
        "unit": spec.unit if spec is not None else None,
        "min": spec.min if spec is not None else None,
        "max": spec.max if spec is not None else None,
        "step": spec.step if spec is not None else None,
        "default": param.default,
        "options": options_of(param, options),
    }


def missing_for(spec: CheckSpec, options: BoardOptions) -> list[str]:
    missing: list[str] = []
    if spec.param("evidence") is not None and not options.recorded:
        missing.extend(options.evidence or ("evidence",))
    if spec.param("stage") is not None and not options.stages:
        missing.append("stage")
    return missing


def checks_payload(options: BoardOptions) -> list[dict[str, Any]]:
    found = []
    for spec in CHECK_SPECS.values():
        missing = missing_for(spec, options)
        found.append({
            "name": spec.name,
            "label": spec.label,
            "summary": spec.summary,
            "scope": spec.scope,
            "params": [param_payload(param, options) for param in spec.params],
            "categories": list(spec.categories),
            "default_categories": list(spec.default_categories),
            "measurable": not missing,
            "missing_evidence": missing,
        })
    return found


# --- a custom rule's definition ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    field: str
    message: str


def requirements(check: str, params: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    evidence = [str(params["evidence"])] if isinstance(params.get("evidence"), str) else []
    stages = [str(params["stage"])] if isinstance(params.get("stage"), str) else []
    return evidence, stages


_INVISIBLE = frozenset({"Cf", "Cs", "Co", "Cn"})


def visible_text(text: str) -> str:
    kept = []
    for char in unicodedata.normalize("NFKC", text):
        category = unicodedata.category(char)
        if category in _INVISIBLE:
            continue
        kept.append(" " if category == "Cc" else char)
    return " ".join("".join(kept).split())


def title_key(title: str) -> str:
    return visible_text(title).casefold()


def check_title(value: object) -> tuple[str | None, list[Problem]]:
    if not isinstance(value, str):
        return None, [Problem("title", "Give the rule a name.")]
    text = visible_text(value)
    if not TITLE_MIN <= len(text) <= TITLE_MAX:
        return None, [Problem("title", f"The name must be {TITLE_MIN} to {TITLE_MAX} characters.")]
    if not any(char.isalnum() for char in text):
        return None, [Problem("title", "The name needs letters or digits.")]
    return text, []


def check_description(value: object) -> tuple[str | None, list[Problem]]:
    """The description as kept (None when blank), or a problem when it is not valid text."""
    if value is None:
        return None, []
    if not isinstance(value, str):
        return None, [Problem("description", "The description must be text.")]
    text = visible_text(value)
    if len(text) > DESCRIPTION_MAX:
        return None, [Problem("description", f"The description can be at most {DESCRIPTION_MAX} characters.")]
    return text or None, []


def check_params(
    spec: CheckSpec, given: object, options: BoardOptions, *, only: Iterable[str] | None = None
) -> tuple[dict[str, Any], list[Problem]]:
    if not isinstance(given, Mapping):
        return {}, [Problem("params", "The settings must be an object, by name.")]
    problems: list[Problem] = []
    names = {param.name for param in spec.params}
    for name in sorted(str(key) for key in given if str(key) not in names):
        problems.append(Problem(f"params.{name[:30]}", f"{spec.label} has no setting called {name[:30]}."))
    wanted = set(names) if only is None else {name for name in only if name in names}
    params: dict[str, Any] = {}
    for param in spec.params:
        if param.name not in wanted:
            continue
        value, problem = _param_value(param, given.get(param.name), options)  # a missing one is told what it takes
        if problem is not None:
            problems.append(Problem(f"params.{param.name}", problem))
        else:
            params[param.name] = value
    return params, problems


def _param_value(param: CheckParam, value: object, options: BoardOptions) -> tuple[Any, str | None]:
    if param.kind == "number":
        assert param.spec is not None
        problem = value_problem(param.spec, value)
        return (None, problem) if problem is not None else (float(value), None)  # type: ignore[arg-type]
    if param.kind == "statuses":
        if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
            return None, "Pick at least one status."
        unknown = [item for item in value if options.status(item) is None]
        if unknown:
            return None, f"Not a status on this board: {', '.join(unknown[:5])}."
        named = {(options.status(item) or item).casefold() for item in value}
        return [name for name, _category in options.statuses if name.casefold() in named], None
    if param.kind == "stage":
        if not isinstance(value, str) or value not in options.stages:
            return None, "Pick a stage this board's workflow maps."
        return value, None
    if param.kind == "evidence":
        if not isinstance(value, str) or value not in options.evidence:
            return None, "Pick evidence this board's process names."
        return value, None
    if not isinstance(value, str) or value not in FIELDS:
        return None, "Pick assignee, estimate, epic or sprint."
    return value, None


def check_applies(spec: CheckSpec, given: object, options: BoardOptions) -> tuple[dict[str, Any], list[Problem]]:
    if not isinstance(given, Mapping):
        return {}, [Problem("applies_to", "Say which work the rule applies to.")]
    problems: list[Problem] = []
    extra = sorted(str(key) for key in given if key not in ("status_category", "issue_type"))
    if extra:
        problems.append(Problem("applies_to", f"applies_to has no {extra[0][:30]}."))
    categories = given.get("status_category")
    chosen: list[str] = []
    if spec.scope == "sprint":
        if categories:
            problems.append(Problem("applies_to.status_category", "A sprint rule has no status filter."))
    elif not isinstance(categories, list) or not categories:
        problems.append(Problem("applies_to.status_category", "Pick at least one of To do, In progress and Done."))
    elif not all(isinstance(item, str) and item in spec.categories for item in categories):
        allowed = " and ".join(_CATEGORY_WORDS[category] for category in spec.categories)
        problems.append(Problem("applies_to.status_category", f"{spec.label} applies to {allowed} only."))
    else:
        chosen = [category for category in CATEGORIES if category in categories]
    types = given.get("issue_type")
    kept: list[str] | None = None
    if types is not None:
        if spec.scope == "sprint":
            problems.append(Problem("applies_to.issue_type", "A sprint rule has no issue type filter."))
        elif not isinstance(types, list) or not types or not all(isinstance(item, str) for item in types):
            problems.append(Problem("applies_to.issue_type", "Pick at least one issue type, or all types."))
        else:
            unknown = [item for item in types if options.issue_type(item) is None]
            if unknown:
                problems.append(Problem("applies_to.issue_type",
                                        f"Not an issue type on this board: {', '.join(unknown[:5])}."))
            else:
                named = {item.casefold() for item in types}
                kept = [name for name in options.issue_types if name.casefold() in named]
    return {"status_category": chosen, "issue_type": kept}, problems


_CATEGORY_WORDS = {"new": "To do", "indeterminate": "In progress", "done": "Done"}


def status_problems(
    spec: CheckSpec, params: Mapping[str, Any], applies: Mapping[str, Any], options: BoardOptions
) -> list[Problem]:
    if spec.param("statuses") is None:
        return []
    names = params.get("statuses")
    categories = applies.get("status_category") or []
    problems: list[Problem] = []
    for name in names if isinstance(names, list) else []:
        category = options.category_of(name) if isinstance(name, str) else None
        if category is None or category in categories:
            continue
        words = _CATEGORY_WORDS[category]
        if category in spec.categories:
            message = f"'{name}' is a {words} status: tick {words} under Applies to."
        else:
            allowed = " and ".join(_CATEGORY_WORDS[each] for each in spec.categories)
            message = f"'{name}' is a {words} status: {spec.label} checks {allowed} work only."
        problems.append(Problem("params.statuses", message))
    return problems


@dataclass(frozen=True)
class Definition:
    """A custom rule as an admin defines it, validated and kept as the board writes its names."""

    title: str
    description: str | None
    check: str
    params: dict[str, Any]
    applies_to: dict[str, Any]
    severity: str

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "description": self.description, "check": self.check, "params": self.params,
                "applies_to": self.applies_to, "severity": self.severity}


_DEFINITION_KEYS = ("title", "description", "check", "params", "applies_to", "severity")


def check_definition(given: object, options: BoardOptions) -> tuple[Definition | None, list[Problem]]:
    if not isinstance(given, Mapping):
        return None, [Problem("rule", "The new rule must be an object.")]
    problems: list[Problem] = []
    extra = sorted(str(key) for key in given if key not in _DEFINITION_KEYS)
    problems.extend(Problem(key[:40], f"A new rule cannot set {key[:40]}.") for key in extra)
    title, found = check_title(given.get("title"))
    problems += found
    description, found = check_description(given.get("description"))
    problems += found
    severity = given.get("severity")
    if severity not in ("breach", "warning"):
        problems.append(Problem("severity", "Pick Breach or Warning."))
    check = given.get("check")
    spec = CHECK_SPECS.get(check) if isinstance(check, str) else None
    if spec is None:
        problems.append(Problem("check", "Pick one of the checks."))
        return None, problems
    params, found_params = check_params(spec, given.get("params", {}), options)
    applies, found_applies = check_applies(spec, given.get("applies_to"), options)
    problems += found_params + found_applies
    if not found_params and not found_applies:
        problems += status_problems(spec, params, applies, options)
    if problems or title is None:
        return None, problems
    return Definition(title, description, spec.name, params, applies, str(severity)), []


def engine_applies(scope: str, applies: Mapping[str, Any]) -> dict[str, Any] | None:
    if scope == "sprint":
        return None
    categories = applies.get("status_category")
    covers = isinstance(categories, list) and set(categories) >= set(CATEGORIES)
    return {"status_category": None if covers else categories, "issue_type": applies.get("issue_type")}


# --- what a rule flags, in words --------------------------------------------------------------------------------------


def explain(check: str, params: Mapping[str, Any], applies: Mapping[str, Any]) -> str | None:
    who, many = _who(applies.get("issue_type"))
    its, was = ("their", "they were") if many else ("its", "it was")
    try:
        if check == "time_in_status_over":
            limit = _time_words(float(params["hours"]))
            return f"Flags {who} in {join_names(params['statuses'])} for more than {limit}."
        if check == "open_longer_than":
            return f"Flags {who} still open more than {days_words(float(params['days']))} after {was} created."
        if check == "no_update_for":
            return f"Flags open {who} with no change in Jira for more than {days_words(float(params['days']))}."
        if check == "missing_field":
            field = str(params["field"])
            if field not in FIELDS:
                return None
            return f"Flags {who} not in a sprint." if field == "sprint" else f"Flags {who} with no {field}."
        if check == "missing_estimate":
            return f"Flags {who} that started with no estimate."
        if check == "estimate_after_start":
            grace = float(params.get("grace_hours", DEFAULT_GRACE_HOURS))
            after = f"more than {_time_words(grace)} after" if grace > 0 else "after"
            return f"Flags {who} whose estimate was first set {after} work started."
        if check == "effort_ratio_at_least":
            return f"Flags {who} whose logged time is at least {_num(float(params['threshold']))} times {its} estimate."
        if check == "entered_stage_without_evidence":
            stage, evidence = label(str(params["stage"])), label(str(params["evidence"]))
            return f"Flags {who} that reached {stage} with no {evidence} recorded before it."
        if check == "skipped_stage":
            return f"Flags {who} whose status jumped over a stage of the workflow."
        if check == "reopened":
            return f"Flags {who} moved out of Done to a status that is not Done."
        if check == "in_progress_unassigned":
            return f"Flags {who} in progress that nobody is assigned to."
        if check == "stuck_in_status":
            pct = _num(float(params.get("pct", DEFAULT_STATUS_PCT)))
            return f"Flags {who} in {its} status longer than this board's p{pct} there."
        if check == "no_evidence_after_percentile":
            stage, evidence = label(str(params["stage"])), label(str(params["evidence"]))
            pct = _num(float(params.get("pct", DEFAULT_STAGE_PCT)))
            return f"Flags {who} in {stage} longer than this board's p{pct} there, with no {evidence} recorded yet."
        if check == "unestimated_in_active_sprint":
            return f"Flags {who} in the active sprint with no estimate."
        if check == "sprint_at_risk":
            days = days_words(float(params.get("days_left", DEFAULT_DAYS_LEFT)))
            share = _num(round(float(params.get("not_started_share", DEFAULT_NOT_STARTED_SHARE)) * 100, 2))
            return (f"Flags an active sprint with {days} or less left and more than {share}% of its work not started, "
                    "or past its end with work not done.")
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _who(types: object) -> tuple[str, bool]:
    if not isinstance(types, list) or not types or not all(isinstance(name, str) for name in types):
        return "work", False
    return join_words(_lower_first(_plural(name)) for name in types), True


def _plural(name: str) -> str:
    if name.lower().endswith("s"):
        return name
    if len(name) > 1 and name.lower().endswith("y") and name[-2].lower() not in "aeiou":
        return name[:-1] + "ies"
    return name + "s"


def _lower_first(text: str) -> str:
    """"Bugs" -> "bugs"; a name whose second letter is a capital ("QA tasks") keeps its case."""
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


def _time_words(hours: float) -> str:
    if hours >= 48:
        return f"{_num(round(hours / 24, 1))} days"
    return f"{_num(round(hours, 1))} {'hour' if hours == 1 else 'hours'}"


# --- values -------------------------------------------------------------------------------------------------------


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        found = float(value)
    except OverflowError:
        return None
    return found if math.isfinite(found) else None


def value_problem(spec: ParamSpec, value: object) -> str | None:
    """Why `value` cannot be the param's, as a sentence; None when it can."""
    found = number(value)
    if found is None:
        too_large = isinstance(value, int) and not isinstance(value, bool)
        return f"{spec.label} must be from {spec.bounds()}." if too_large else f"{spec.label} must be a number."
    if not spec.min <= found <= spec.max:
        return f"{spec.label} must be from {spec.bounds()}."
    exponent = Decimal(repr(found)).as_tuple().exponent
    if not isinstance(exponent, int) or -exponent > DECIMALS:
        return f"{spec.label} can have at most {DECIMALS} decimal places."
    return None


def _num(value: float) -> str:
    """1.0 -> "1", 1.5 -> "1.5", 5.000000000000001 -> "5"."""
    return f"{value:g}"


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]
