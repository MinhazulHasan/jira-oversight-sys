"""Rule settings for admins (JO-15; contract 4.20 to 4.22 in the jira-oversight-ui repository): what GET
/rules/settings, POST /rules/settings/preview and POST /rules/settings answer (oversight/api/routes/rule_settings.py),
over oversight/rules/settings.py, which holds every rule about what may change and how it is stored; and custom rules
(JO-20): the rules an admin adds to a board, its checks (oversight/rules/registry.py) and the AI draft
(oversight/api/rule_draft.py).

The board's settings (settings_body: GET, and a save's answer): {meta, project, revision, catalogue_version,
updated_at, updated_by, apply, rules, history, checks, custom_limit, custom_count, issue_types, ai_draft}.
- revision: the board's settings revision (0: never changed); updated_at and updated_by: the latest change's time and
  viewer name (null before any).
- rules: every catalogue rule, on or off, in catalogue order, then the board's custom rules in the order they were
  added: {rule_id, title, description, kind, check, scope, escalates_to, enabled, default_enabled, severity,
  default_severity, params, customised, measurable, missing_evidence, open, custom, check_label, applies_to,
  created_at, created_by}. params: a catalogue rule's editable params only (oversight.rules.settings.EDITABLE), each
  {name, label, kind ("number"), unit, value (the board's), default (rules.yaml's, else the check's), min, max, step,
  options (null)}, [] when none; a custom rule's, every param of its check (statuses, stage, evidence and field too),
  each {name, label, kind, unit, value, default, min, max, step, options}, default being its value (a custom rule has
  no catalogue value) and unit, min, max and step null for a param that is not a number. A custom rule's defaults are
  the rule as created: on, and its severity then (so customised says whether its on/off or severity changed since).
  measurable and missing_evidence as GET /rules says them for the board; open: its open findings on the board;
  check_label: the check's label; applies_to: {status_category: [...], issue_type: [...] | null}, a catalogue rule's
  as rules.yaml says it (every category where it names none; none for a sprint rule); created_at and created_by: when
  and by whom a custom rule was added (the viewer's name, never an email address), null for a catalogue rule.
- history: the board's changes, newest first, at most 50: {change_id, at, revision, by: {hash, name}, reason, undo_of,
  changes: [{rule_id, field, from, to}]}, from and to being the effective values; a custom rule's field `rule` holds
  its definition {title, description, check, params, applies_to, severity} (from null when added or restored, to null
  when deleted), its fields title, description and applies_to their values.
- checks: the registry for this board (registry.checks_payload): each {name, label, summary, scope, params: [{name,
  label, kind, unit, min, max, step, default, options}], categories, default_categories, measurable,
  missing_evidence}, options filled from the board: its statuses ({value, label, category}), the stages it maps and
  the evidence names ({value, label}), the fields; null for a number.
- custom_limit (50) and custom_count (the board's custom rules, not deleted); issue_types: the board's issue types,
  from its issues, Epic (rules.yaml's exclude_types) left out, for a custom rule's applies_to.
- ai_draft: {available, reason}: whether POST /rules/settings/draft can draft now. Available with LLM_MODE=live (a
  provider set up) and with stub (the scripted drafter); not while the board holds custom_limit custom rules (a draft
  could never be added: LIMIT_REASON), nor while the day's model budget is used up (LLM_DAILY_TOKEN_BUDGET), reason
  saying so in plain words (BUDGET_REASON).
- apply: whether the settings have reached the board's findings: {state, applied_revision, started_at, finished_at,
  message, result}. applied_revision is the settings revision the board's findings show: the highest settings_revision
  of its latest RECENT_RUNS succeeded rules runs (0 when none), from the runs table, so it is still right after the
  API restarts (revisions only grow, and each run reads them inside its write transaction for the board). Only those
  runs are read (oversight.rules.settings.recent_runs), so the answer costs the same however long the history. state:
  - running: this process's re-check of the board is queued or running (started_at: when it began, or was asked for);
  - applied: applied_revision >= revision. started_at, finished_at and result are the first succeeded rules run that
    applied this revision: result {opened, closed}, the findings it opened and every one it closed (cleared,
    settings_changed, superseded or aged out). All null for revision 0, and once every run read applied it (the first
    is older than they are: what it did is no longer known);
  - failed: this process's last re-check of the board failed and the settings have not reached the findings yet;
    message says so, and that the next sync applies them;
  - pending: not applied yet, and nothing running here: the next sync (or re-check) applies them, as message says.

The preview (preview_body): {meta, project, revision, impact, totals}. impact holds each rule whose settings the change
set changes, and each rule it leaves as saved whose saved settings have not reached the board's findings yet (a
re-check that failed, or has not run) when the re-check would open or close any of its findings, in catalogue order,
then the custom rules': {rule_id, measurable, open_now, would_open, would_close, would_stay, examples: {open, close},
note}, from oversight.rules.engine.preview over the board's data now, through the request's read-only connection:
nothing is written. So totals are what the re-check after the save does. A custom rule the change set adds is in it
under the id the save would give it now (the next of its prefix: another board adding one first moves it on), with
every finding it would raise now (would_open); one it deletes with its open findings closing (would_close). note says
when a rule left on is not measurable on the board (the change does nothing until it can run: NOT_MEASURABLE_NOTE),
or when its findings still reflect earlier settings, which the re-check brings up to date (PENDING_NOTE); null
otherwise. totals: {would_open, would_close}.

Each body is read in one read transaction on the request's read-only connection (snapshot): a save or a rules run
landing part way never shows as a revision without its change in the history, or an apply state that does not fit the
revision.

Bodies. A POST takes a JSON object of at most 16 KB (BODY_LIMIT): the preview's {changes, revision?} (the revision
not checked), a save's {revision, changes, reason?, undo_of?}; a change is {rule_id, enabled?, severity?, params?,
reset?} (JO-15's; on a custom rule, params edit its params, and reset is refused), or one of JO-20's, by its op:
{op: "create", rule: {title, description?, check, params, applies_to, severity}} (the id is given by the save),
{op: "edit", rule_id, title?, description?, params?, applies_to?} (a custom rule's; never its check), {op: "delete",
rule_id} and {op: "restore", rule_id} (a deleted custom rule of this board: what undo of a delete sends). At most one
change per rule, and a change other than a create names a rule the board holds already (never the id a create in the
same body would be given). Every problem found is in one 422 {"detail", "code": "rule_settings_invalid", "errors":
[{rule_id, field, message}]}: detail is every message once, in plain words, and errors each one with the rule and the
field it is about (null when none; the fields oversight.rules.settings.Problem names: a setting, params, reset, rule_id,
changes, revision, reason, undo_of, op, rule, title, description, check, applies_to and its parts, or the name of a key
the request does not take). A new rule's problems have no rule id (it has none yet): their field names the rule by its
place in the body, "changes.<index>.rule.<field>" (index from 0, as the body lists its changes: "changes.2.rule.title",
"changes.2.rule.params.days"), so a body that adds several rules says which one each problem is about. An undo
(undo_of) must undo the board's latest change exactly, or it is refused under undo_of. The reason, a custom rule's title
and its description are kept as the access log keeps a name (control characters as spaces, spaces collapsed; a title
and a description also as the registry keeps them: oversight.rules.registry.visible_text); the reason at most 500
characters; none of them may hold an email address or an Atlassian account id: they are shown to every admin.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oversight.api.access_log import Viewer, names_someone, plain_text
from oversight.api.deps import ApiContext, ApiError
from oversight.api.meta import build_meta
from oversight.api.refresh import Recheck
from oversight.ask.catalog import open_findings_by_rule, rule_missing
from oversight.llm.budget import SCRIPTED, budget_reached
from oversight.logs import get_logger
from oversight.rules.catalog import Rule
from oversight.rules.engine import Impact, preview
from oversight.rules.registry import (
    CATEGORIES,
    CHECK_SPECS,
    CUSTOM_LIMIT,
    BoardOptions,
    board_options,
    check_label,
    checks_payload,
    options_of,
)
from oversight.rules.settings import (
    REASON_MAX,
    RECENT_RUNS,
    SEVERITIES,
    AppliedRun,
    Change,
    CustomRule,
    ParamSpec,
    Problem,
    SavedChange,
    SettingsConflict,
    SettingsInvalid,
    custom_context,
    default_value,
    editable_params,
    effective_rules,
    load_board_settings,
    plan_changes,
    read_history,
    recent_runs,
    save_board_settings,
    showing,
)
from oversight.store.db import Store
from oversight.timeutil import to_iso

log = get_logger("api")

INVALID = "rule_settings_invalid"
CONFLICT = "rule_settings_conflict"
BUSY = "rule_settings_busy"
BODY_LIMIT = 16 * 1024  # bytes
MAX_CHANGES = 100  # far more than the catalogue's rules: a body with more is refused before any is read
WRITE_WAIT_MS = 10_000  # how long a save waits for the write lock a running rules step holds
CONFLICT_DETAIL = (
    "Someone else changed this board's rule settings while you were editing them. "
    "Reload to see their changes, then make yours again."
)
BUSY_DETAIL = (
    "The rules are running on this board right now, so the settings could not be saved. "
    "Nothing was changed: try again in a moment."
)
NOT_MEASURABLE_NOTE = "Not measurable on this board, so this change has no effect until it can run."
# A rule the changes leave as saved whose findings do not reflect the settings it runs with yet (an earlier change whose
# re-check failed, or has not run): the re-check after this save applies them too, so its figures are in the preview.
PENDING_NOTE = (
    "Its findings still reflect its earlier settings: the re-check after this save applies the current ones too."
)
# What pending means for the reader, beside the chip that already says when ("Will apply at the next sync").
PENDING_MESSAGE = "The board's findings still show its earlier settings until the next sync applies these."
# The AI draft's state while the day's model budget is used up (GET's ai_draft.reason, and the draft's note).
BUDGET_REASON = "Today's AI budget is used up. You can still add a rule by hand."
# ... and while the board holds as many custom rules as it can: a draft could never be added.
LIMIT_REASON = f"This board has {CUSTOM_LIMIT} custom rules, the most it can have. Delete one to add another."
_TOO_LARGE = "The request body is larger than 16 KB."
_NOT_AN_OBJECT = "The request body must be a JSON object."
_RULE_ID = re.compile(r"[RW]-[A-Z]+-[0-9]{2,3}")
_CHANGE_ID = re.compile(r"[0-9a-f]{32}")
_CHANGE_KEYS = frozenset({"rule_id", "enabled", "severity", "params", "reset"})
# JO-20's changes, by op: the keys each takes.
_OPS = ("create", "edit", "delete", "restore")
_OP_KEYS = {
    "create": frozenset({"op", "rule"}),
    "edit": frozenset({"op", "rule_id", "title", "description", "params", "applies_to"}),
    "delete": frozenset({"op", "rule_id"}),
    "restore": frozenset({"op", "rule_id"}),
}
_EDITABLE_FIELDS = ("title", "description", "params", "applies_to")
_PREVIEW_KEYS = frozenset({"changes", "revision"})
_SAVE_KEYS = frozenset({"changes", "revision", "reason", "undo_of"})
# The findings a run closed, whatever the reason: the result a save's re-check reports.
_CLOSED = ("resolved", "settings_changed", "superseded", "aged_out")


def invalid(problems: Sequence[Problem]) -> ApiError:
    """The 422 for a refused body or change set: every problem, in one detail (each naming its rule where its message
    does not already, a sentence two new rules share said once) and one by one."""
    detail = " ".join(dict.fromkeys(
        f"{problem.rule_id}: {problem.message}" if problem.rule_id and problem.rule_id not in problem.message
        else problem.message
        for problem in problems
    ))
    return ApiError(422, INVALID, detail, extra={"errors": [problem.as_dict() for problem in problems]})


def too_large() -> ApiError:
    return invalid([Problem(None, None, _TOO_LARGE)])


# --- reading a body ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SaveRequest:
    """A save's body, its shape checked: what it changes is checked against the board when it is written."""

    revision: int
    changes: list[Change]
    reason: str | None
    undo_of: str | None


def parse_preview(raw: bytes) -> list[Change]:
    """The preview's changes; 422 for anything else, or a revision that is not a whole number."""
    body = _object(raw)
    problems = _unknown(body, _PREVIEW_KEYS)
    if "revision" in body and body["revision"] is not None and not _whole(body["revision"]):
        problems.append(Problem(None, "revision", "The revision must be a whole number."))
    changes = _changes(body, problems)
    if problems:
        raise invalid(problems)
    return changes


def parse_save(raw: bytes) -> SaveRequest:
    """A save's body; 422 for anything else."""
    body = _object(raw)
    problems = _unknown(body, _SAVE_KEYS)
    revision = body.get("revision")
    if not _whole(revision):
        problems.append(Problem(None, "revision", "The revision the changes were made from must be a whole number."))
    changes = _changes(body, problems)
    reason = _reason(body.get("reason"), problems)
    undo_of = body.get("undo_of")
    if undo_of is not None and not (isinstance(undo_of, str) and _CHANGE_ID.fullmatch(undo_of)):
        problems.append(Problem(None, "undo_of", "The change to undo must be named by its change_id."))
    if problems:
        raise invalid(problems)
    return SaveRequest(int(revision), changes, reason, undo_of)  # type: ignore[arg-type]


def _object(raw: bytes) -> dict[str, Any]:
    if len(raw) > BODY_LIMIT:
        raise too_large()
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise invalid([Problem(None, None, _NOT_AN_OBJECT)]) from None
    if not isinstance(body, dict):
        raise invalid([Problem(None, None, _NOT_AN_OBJECT)])
    return body


def _no_constant(name: str) -> Any:
    """NaN and Infinity are not JSON, whatever Python's reader allows."""
    raise ValueError(name)


def _unknown(body: dict[str, Any], allowed: frozenset[str]) -> list[Problem]:
    return [Problem(None, _cut(name), f"{_cut(name)} is not something this request takes.")
            for name in sorted(set(body) - allowed)]


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _changes(body: dict[str, Any], problems: list[Problem]) -> list[Change]:
    value = body.get("changes")
    if not isinstance(value, list):
        problems.append(Problem(None, "changes", "changes must be a list of changes, one for each rule."))
        return []
    if len(value) > MAX_CHANGES:
        problems.append(Problem(None, "changes", f"A request can change at most {MAX_CHANGES} rules."))
        return []
    changes = []
    for number, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            problems.append(Problem(None, "changes", f"Change {number} must be an object."))
            continue
        if "op" in item:
            change = _op_change(number, item, problems)
            if change is not None:
                changes.append(change)
            continue
        rule_id = item.get("rule_id")
        if not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
            problems.append(Problem(None, "rule_id", f"Change {number} must name its rule by id, such as R-EFF-01."))
            continue
        found = [Problem(rule_id, _cut(name), f"A change cannot set {_cut(name)}.")
                 for name in sorted(set(item) - _CHANGE_KEYS)]
        enabled, severity, params = item.get("enabled"), item.get("severity"), item.get("params")
        reset = item.get("reset")
        if enabled is not None and not isinstance(enabled, bool):
            found.append(Problem(rule_id, "enabled", "On or off must be true or false."))
        if severity is not None and severity not in SEVERITIES:
            found.append(Problem(rule_id, "severity", "Severity must be breach or warning."))
        if params is not None and not isinstance(params, dict):
            found.append(Problem(rule_id, "params", "Its thresholds must be an object of numbers, by name."))
        if reset is not None and not isinstance(reset, bool):
            found.append(Problem(rule_id, "reset", "Reset must be true or false."))
        problems.extend(found)
        if not found:
            changes.append(Change(rule_id, enabled, severity, dict(params or {}), bool(reset)))
    return changes


def _op_change(number: int, item: dict[str, Any], problems: list[Problem]) -> Change | None:
    """A JO-20 change (create, edit, delete or restore), its shape checked and its texts cleaned; what it names is
    checked against the board when it is planned. None, with its problems, when it is not one."""
    op = item.get("op")
    if op not in _OPS:
        problems.append(Problem(None, "op", f"Change {number}'s op must be create, edit, delete or restore."))
        return None
    if op == "create":
        found = [Problem(None, _cut(name), f"A new rule's change cannot set {_cut(name)}.")
                 for name in sorted(set(item) - _OP_KEYS["create"])]
        rule = item.get("rule")
        if not isinstance(rule, dict):
            found.append(Problem(None, "rule", f"Change {number} must hold the new rule as an object."))
        else:
            rule = {**rule, **_texts(rule, None, found, index=number - 1)}
        problems.extend(found)
        return None if found else Change.adding(rule)
    rule_id = item.get("rule_id")
    if not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
        problems.append(Problem(None, "rule_id", f"Change {number} must name its rule by id, such as R-EFF-01."))
        return None
    found = []
    for name in sorted(set(item) - _OP_KEYS[op]):
        message = ("The check can't change: delete the rule and add it again." if op == "edit" and name == "check"
                   else f"A change cannot set {_cut(name)}.")
        found.append(Problem(rule_id, _cut(name), message))
    if op != "edit":
        problems.extend(found)
        return None if found else (Change.deleting(rule_id) if op == "delete" else Change.restoring(rule_id))
    fields = {name: item[name] for name in _EDITABLE_FIELDS if name in item}
    fields.update(_texts(fields, rule_id, found))
    for name, kind in (("params", "Its settings must be an object, by name."),
                       ("applies_to", "What it applies to must be an object.")):
        if name in fields and not isinstance(fields[name], dict):
            found.append(Problem(rule_id, name, kind))
    problems.extend(found)
    return None if found else Change.editing(rule_id, **fields)


def _texts(
    given: dict[str, Any], rule_id: str | None, problems: list[Problem], *, index: int | None = None
) -> dict[str, Any]:
    """A custom rule's title and description as kept (as the access log keeps a name), each refused when it holds an
    email address or an account id; a value that is not text is left for the registry to refuse. `index`: a new
    rule's place in the body, which its problems name it by."""
    cleaned: dict[str, Any] = {}
    for name, words in (("title", "The name"), ("description", "The description")):
        value = given.get(name)
        if not isinstance(value, str):
            continue
        text = plain_text(value)
        if names_someone(text):
            problems.append(Problem(rule_id, name, f"{words} cannot hold an email address or an account id.", index))
        cleaned[name] = text
    return cleaned


def _reason(value: object, problems: list[Problem]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        problems.append(Problem(None, "reason", "The reason must be text."))
        return None
    text = plain_text(value)
    if len(text) > REASON_MAX:
        problems.append(Problem(None, "reason", f"The reason can be at most {REASON_MAX} characters."))
        return None
    if names_someone(text):
        problems.append(Problem(None, "reason", "The reason cannot hold an email address or an account id."))
        return None
    return text or None


def _cut(name: object) -> str:
    """A name from the body, short enough to say back."""
    return str(name)[:40]


# --- the board's settings -----------------------------------------------------------------------------------------


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Every read inside it in one read transaction: the snapshot as it stood at its first read (the database is in
    WAL mode, so no writer waits for it). Inside a transaction already, it joins it."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("ROLLBACK")  # it only read


def settings_body(conn: sqlite3.Connection, ctx: ApiContext, job: Recheck | None) -> dict[str, Any]:
    """GET /rules/settings' body (and a save's answer), read in one snapshot: see the module docstring. `job`: this
    process's latest re-check of the board."""
    project = ctx.project
    with snapshot(conn):
        settings = load_board_settings(conn, project)
        history = read_history(conn, project)
        latest = history[0] if history else None
        open_counts = open_findings_by_rule(conn, project)
        apply = apply_state(conn, project, settings.revision, job, saved_at=latest["at"] if latest else None)
        options = board_options(conn, ctx.process, project, exclude_types=ctx.catalogue.defaults.exclude_types)
        draft = ai_draft_state(conn, ctx, len(settings.live_custom))
        meta = build_meta(conn, ctx)
    rules = effective_rules(ctx.catalogue, settings)
    customs = {rule.rule_id: rule for rule in settings.custom}
    return {
        "meta": meta,
        "project": project,
        "revision": settings.revision,
        "catalogue_version": ctx.catalogue.version,
        "updated_at": latest["at"] if latest else None,
        "updated_by": latest["by"]["name"] if latest else None,
        "apply": apply,
        "rules": [_rule(rule, ctx, open_counts, customs.get(rule.id) if rule.custom else None, options)
                  for rule in rules.rules],
        "history": history,
        "checks": checks_payload(options),
        "custom_limit": CUSTOM_LIMIT,
        "custom_count": len(settings.live_custom),
        "issue_types": list(options.issue_types),
        "ai_draft": draft,
    }


def ai_draft_state(conn: sqlite3.Connection, ctx: ApiContext, custom_count: int) -> dict[str, Any]:
    """{available, reason}: whether a draft can be asked for now on a board holding `custom_count` custom rules (see
    the module docstring). The scripted drafter calls no model, so the budget never stops it; the limit stops every
    drafter, as no draft could be added."""
    reason = unavailable(conn, ctx, custom_count)
    return {"available": reason is None, "reason": reason}


def unavailable(conn: sqlite3.Connection, ctx: ApiContext, custom_count: int) -> str | None:
    """Why no draft can be asked for now, in plain words: the board holds as many custom rules as it can
    (LIMIT_REASON), or the day's model budget is used up (BUDGET_REASON, never for the scripted drafter); None when
    one can."""
    if custom_count >= CUSTOM_LIMIT:
        return LIMIT_REASON
    if getattr(ctx.provider, "name", None) != SCRIPTED and budget_reached(conn, ctx.settings, ctx.now()):
        return BUDGET_REASON
    return None


def _rule(
    rule: Rule, ctx: ApiContext, open_counts: dict[str, int], custom: CustomRule | None, options: BoardOptions
) -> dict[str, Any]:
    """One rule as the board has it (`rule`), beside the catalogue's (a custom rule's: as it was created)."""
    missing = rule_missing(rule, ctx.process, ctx.project)
    if custom is None:
        listed = ctx.catalogue.by_id(rule.id)
        defaults = (listed.enabled, listed.severity)
        params = [_param(rule, listed, spec) for spec in editable_params(listed)]
    else:
        defaults = (True, custom.severity)
        params = _custom_params(rule, custom, options)
    return {
        "rule_id": rule.id,
        "title": rule.title,
        "description": rule.description,
        "kind": rule.kind,
        "check": rule.check,
        "scope": rule.scope,
        "escalates_to": rule.escalates_to,
        "enabled": rule.enabled,
        "default_enabled": defaults[0],
        "severity": rule.severity,
        "default_severity": defaults[1],
        "params": params,
        "customised": rule.customised,
        "measurable": not missing,
        "missing_evidence": missing,
        "open": open_counts.get(rule.id, 0),
        "custom": custom is not None,
        "check_label": check_label(rule.check),
        "applies_to": custom.applies_to if custom is not None else _applies_of(rule),
        "created_at": custom.created_at if custom is not None else None,
        "created_by": custom.created_by if custom is not None else None,
    }


def _param(rule: Rule, listed: Rule, spec: ParamSpec) -> dict[str, Any]:
    default = default_value(listed, spec)
    value = rule.params.get(spec.name, default)
    return {
        "name": spec.name,
        "label": spec.label,
        "kind": "number",
        "unit": spec.unit,
        "value": float(value),
        "default": default,
        "min": spec.min,
        "max": spec.max,
        "step": spec.step,
        "options": None,
    }


def _custom_params(rule: Rule, custom: CustomRule, options: BoardOptions) -> list[dict[str, Any]]:
    """Every param of a custom rule's check, its value the rule's (and so its default: it has no catalogue value)."""
    found = []
    for param in CHECK_SPECS[custom.check].params:
        spec, value = param.spec, rule.params.get(param.name)
        found.append({
            "name": param.name,
            "label": param.label,
            "kind": param.kind,
            "unit": spec.unit if spec is not None else None,
            "value": value,
            "default": value,
            "min": spec.min if spec is not None else None,
            "max": spec.max if spec is not None else None,
            "step": spec.step if spec is not None else None,
            "options": options_of(param, options),
        })
    return found


def _applies_of(rule: Rule) -> dict[str, Any]:
    """A catalogue rule's applies_to as the dashboard reads a custom rule's: every category where rules.yaml names
    none, none for a sprint rule; null issue types for every type."""
    applies = rule.applies_to
    if rule.scope == "sprint":
        categories: list[str] = []
    elif applies is not None and applies.status_category is not None:
        categories = [category for category in CATEGORIES if category in applies.status_category]
    else:
        categories = list(CATEGORIES)
    return {"status_category": categories, "issue_type": list(applies.issue_type) if applies and applies.issue_type else None}


def apply_state(
    conn: sqlite3.Connection, project: str, revision: int, job: Recheck | None, *, saved_at: str | None = None
) -> dict[str, Any]:
    """Whether the board's settings at `revision`, saved at `saved_at` (storage format), have reached its findings,
    from the board's latest succeeded rules runs alone (oversight.rules.settings.recent_runs: a bounded read, however
    long the history): see the module docstring."""
    runs = recent_runs(conn, project)
    shown = showing(runs)
    applied = shown.revision if shown is not None else 0
    body: dict[str, Any] = {"state": "pending", "applied_revision": applied, "started_at": None, "finished_at": None,
                            "message": None, "result": None}
    if job is not None and job.state in ("queued", "running"):
        return body | {"state": "running", "started_at": to_iso(job.started_at or job.requested_at)}
    if applied >= revision:
        run = _applying(runs, revision, saved_at or "") if revision > 0 else None
        if run is None:
            return body | {"state": "applied"}
        return body | {"state": "applied", "started_at": run.started_at, "finished_at": run.finished_at,
                       "result": _result(run.stats)}
    if job is not None and job.state == "failed":
        return body | {"state": "failed", "started_at": to_iso(job.started_at), "finished_at": to_iso(job.finished_at),
                       "message": job.message}
    return body | {"message": PENDING_MESSAGE}


def _applying(runs: Sequence[AppliedRun], revision: int, saved_at: str) -> AppliedRun | None:
    """The first of the board's recent runs (newest first) to apply its settings at `revision` or a later one: the
    earliest started of those that did and finished after the revision was saved, as only those can have read it.
    None when that is the oldest of RECENT_RUNS runs: every run read applied it, so the first may be older still, and
    what it did is no longer known."""
    found = [run for run in runs if run.revision >= revision and (run.finished_at or "") >= saved_at]
    first = min(found, key=lambda run: (run.started_at, run.run_id), default=None)
    if first is None or (len(runs) >= RECENT_RUNS and first.run_id == runs[-1].run_id):
        return None
    return first


def _result(stats: Mapping[str, Any]) -> dict[str, int] | None:
    """{opened, closed} for the board, from a rules run's stats for it; None when they do not say."""
    counts = [stats.get(key) for key in ("opened", *_CLOSED)]
    if not all(isinstance(count, int) or count is None for count in counts):
        return None
    return {"opened": int(stats.get("opened") or 0), "closed": sum(int(stats.get(key) or 0) for key in _CLOSED)}


# --- the preview -------------------------------------------------------------------------------------------------


def preview_body(
    conn: sqlite3.Connection, ctx: ApiContext, changes: Sequence[Change], viewer: Viewer | None = None
) -> dict[str, Any]:
    """POST /rules/settings/preview's body, read in one snapshot: the change set validated as a save validates it (422
    otherwise; a custom rule against the board's options as they are), then what it would do (see the module
    docstring). Nothing is written: `conn` is read-only."""
    with snapshot(conn):
        current = load_board_settings(conn, ctx.project)
        board = custom_context(conn, ctx.process, ctx.catalogue, ctx.project, at=to_iso(ctx.now()) or "",
                               by=viewer.name if viewer is not None else "")
        try:
            plan = plan_changes(ctx.catalogue, current, changes, board)
        except SettingsInvalid as exc:
            raise invalid(exc.problems) from None
        store = Store(conn)
        impacts = preview(store, ctx.process, ctx.catalogue, ctx.fields, ctx.project, plan.settings, now=ctx.now())
        meta = build_meta(conn, ctx)
    return {
        "meta": meta,
        "project": ctx.project,
        "revision": current.revision,
        "impact": [_impact(impact) for impact in impacts],
        "totals": {
            "would_open": sum(impact.would_open for impact in impacts),
            "would_close": sum(impact.would_close for impact in impacts),
        },
    }


def _impact(impact: Impact) -> dict[str, Any]:
    idle = impact.enabled and not impact.measurable and impact.would_close == 0
    return {
        "rule_id": impact.rule_id,
        "measurable": impact.measurable,
        "open_now": impact.open_now,
        "would_open": impact.would_open,
        "would_close": impact.would_close,
        "would_stay": impact.would_stay,
        "examples": {"open": list(impact.open_examples), "close": list(impact.close_examples)},
        "note": NOT_MEASURABLE_NOTE if idle else PENDING_NOTE if impact.pending else None,
    }


# --- saving ---------------------------------------------------------------------------------------------------------


def save(ctx: ApiContext, asked: SaveRequest, viewer: Viewer) -> SavedChange:
    """Write a save to the board's settings (oversight.rules.settings.save_board_settings): 409 when it was made from
    another revision, 422 for any problem with it, 503 when a rules run holds the write lock past WRITE_WAIT_MS;
    nothing is written in any of them."""
    store = _writer(ctx.db_path)
    try:
        return save_board_settings(
            store, ctx.catalogue, ctx.project, revision=asked.revision, changes=asked.changes,
            viewer_hash=viewer.hash, viewer_name=viewer.name, now=ctx.now(), reason=asked.reason,
            undo_of=asked.undo_of, process=ctx.process,
        )
    except SettingsConflict:
        raise ApiError(409, CONFLICT, CONFLICT_DETAIL) from None
    except SettingsInvalid as exc:
        raise invalid(exc.problems) from None
    except sqlite3.OperationalError as exc:
        if str(getattr(exc, "sqlite_errorname", "")).startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
            log.warning("rule_settings_busy", project=ctx.project)
            raise ApiError(503, BUSY, BUSY_DETAIL) from None
        raise
    finally:
        store.close()


def _writer(db_path: Path) -> Store:
    """A Store on the snapshot that exists, to write a board's settings: read-write, never making a new file and never
    migrating (the API brought the snapshot to this version when it started), waiting up to WRITE_WAIT_MS for the
    write lock."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {WRITE_WAIT_MS}")
    return Store(conn, Path(db_path))
