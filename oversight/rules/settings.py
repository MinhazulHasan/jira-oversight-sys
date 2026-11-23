"""Rule settings for admins (JO-15): what one board changes of the rule catalogue, and the rules it runs with; and the
rules an admin adds to a board (JO-20, custom rules).

config/rules.yaml stays the catalogue: every rule's definition and the defaults every board starts from. An admin
changes, per board, from the dashboard (people level; oversight/api/routes/rule_settings.py):
- enabled: on or off (off: the rule is not run on the board);
- severity: breach or warning (how its findings are reported);
- thresholds: the numeric params in EDITABLE, by the rule's check, each within its bounds (min and max included,
  enforced here and mirrored by the dashboard, whose field moves by `step`). A param the rule's catalogue entry leaves
  out that the check reads with a default (grace_hours, pct) can be set too, from that default. Structural params
  (stage, evidence) never can, and nothing else of a rule, nor the catalogue's defaults (look-back, history days,
  excluded types), changes here: rules.yaml keeps them.
A value is a finite number (never a boolean or a string), within its bounds, with at most 4 decimal places. The
cross-rule rule: a warning that escalates_to a breach rule of the same check, both with a threshold (W-EFF-01 and
R-EFF-01, both effort_ratio_at_least), keeps its threshold strictly below the breach rule's while both are on. Its
refusal is said on the field the change set moved, with the other rule's value in its unit, as the dashboard says it:
"Must stay below R-EFF-01's 1.5× while both are on: the warning comes first." on the warning's, "Must stay above
W-EFF-01's 0.8× while both are on." on the breach rule's. EDITABLE and its ParamSpecs live in
oversight/rules/registry.py, beside the check registry that builds on them.

Custom rules (JO-20). An admin adds a rule to one board: one of the registry's checks (oversight/rules/registry.py)
with its params, the status categories and issue types it applies to, a title, a description and how serious it is
(Breach: kind rule and severity breach; Warning: kind warning and severity warning). Its id is R-CUS-nn for a breach
rule and W-CUS-nn for a warning, the next number of its prefix across every board (two or three digits), never
reused: a rule is never hard-deleted. custom_rules holds each one, its definition as created and last edited, the
board's settings revision that created it, who created it (the viewer's name, as the history keeps it) and, once
deleted, when. At most CUSTOM_LIMIT (50) live custom rules a board; a title of 3 to 80 characters and a description of
at most 300, plain text (the API cleans them as it cleans a reason; registry.check_title also takes out characters that
show as nothing and folds look-alike forms), the title unique among the board's rules, on or off, catalogue and
custom, as a reader sees it (registry.title_key: ignoring case). Afterwards its on/off and severity are set as any
rule's (rule_settings, relative to the rule as created: on, and its severity then); its kind stays.

A change set is a list of changes, each one rule's (Change), at most one per rule:
- set (JO-15's shape): enabled, severity and params, or reset. On a custom rule: enabled and severity as above, params
  edit its params (as an edit), and a reset is refused: a custom rule has no catalogue value to go back to;
- create: a new custom rule's definition; the id is given here;
- edit: a custom rule's title, description, params (some or all) or applies_to; never its check (delete it and add it
  again);
- delete: a custom rule, marked deleted; its findings close as settings_changed, as a rule turned off;
- restore: a deleted custom rule of this board (what undo of a delete sends), back with the same id.
Each is validated against the registry and the board's options (registry.check_definition and its parts), the limit
and the titles, all or nothing: SettingsInvalid names every problem, each on its field (a new rule's with no rule id:
it has none yet, so with its place in the change set instead). A change other than a create names a rule the board
holds already, never the id a create in the same change set will be given: that id is the save's to give, and a change
set that named it could hide what it does from the history (a rule added and deleted at once, or added off). A change
set that changes nothing is refused too.

Storage (schema version 9, custom_rules from version 11; nothing here writes to Jira). rule_settings holds one row
per board and rule that differs from its catalogue entry (a custom rule's: as created), each field NULL where the
board keeps that value: a field set back is stored as NULL, and a row with nothing left is deleted (a deleted custom
rule's row stays, so a restore brings back its severity too). rule_settings_changes keeps every change, never purged:
the audit trail and what undo restores. Each holds when, the board, the revision it made, the viewer (hash and name,
as the access log reads them: never an email address), the reason, each field it changed with its effective value
before and after ("1.5 -> 2"), and the change it undid. A custom rule's fields: rule (added: from null; deleted: to
null; restored: from null), with the rule's definition as {title, description, check, params, applies_to, severity};
title, description, applies_to and params.<name> for an edit; enabled and severity as any rule's. A board's revision
is its highest change's (0: never changed). save_board_settings checks the revision a save was made from, validates
the change set and writes the rows, the custom rules and the change in one write transaction (BEGIN IMMEDIATE): a save
made from another revision is refused (SettingsConflict), so two admins never overwrite each other, and a change set
with any problem changes nothing (SettingsInvalid, naming every problem in plain words). A save that names a change it
undoes (undo_of) must undo the board's latest change exactly, as the dashboard offers it: each field that change set
back to its value before it, and nothing else (an added rule deleted, a deleted one restored, an edit edited back). So
the history says a change was undone only when it no longer applies.

Where the board's settings stand. Each rules run records, per board, the settings revision it applied (its stats'
settings_revision) and, per rule it ran, the rule_version and settings_hash it ran with (rule_status). recent_runs
reads the board's latest RECENT_RUNS succeeded rules runs and showing picks the one whose settings the board's
findings show: GET /rules/settings says from them whether the settings have reached the findings, and a preview
compares a change with what they show (oversight/rules/engine.py). turned_off reads from the history the rules the
board itself turned off, rules.yaml's own "off" apart: their findings close as settings_changed.

The board's rules. effective_rules(catalogue, settings, custom) applies a board's overrides, appends its live custom
rules (settings.custom unless `custom` is given) and validates the result again as a RulesConfig (its models still
forbid anything rules.yaml cannot say), each Rule carrying what the board made of it (oversight/rules/catalog.py):
- its rule_version (RulesConfig.rule_version): the catalogue's version while its severity is the catalogue's, else
  "<version>+<severity>" ("0.1.0+breach"); a custom rule's is "c<created_revision>", with "+<severity>" while its
  severity differs from the one it was created with. Only a severity change gives a rule's findings new ids: turning
  it off and on again, deleting and restoring it, or changing a threshold or the scope, never does;
- settings_hash: None while its params are the catalogue's, else the first 12 hex characters of the SHA-256 of their
  canonical JSON (sorted keys, numbers as floats); a custom rule's always, of {params, applies_to}. The engine writes it
  on every finding it upserts and in rule_status, so an open finding the rule no longer produces resolves as
  "settings_changed", never as "cleared", when it was last seen under other params or another scope
  (oversight/rules/engine.py);
- customised: whether anything of it differs from rules.yaml on the board (a custom rule: its on/off or severity from
  the rule as created);
- identity_params: the catalogue's params, which name its findings (checks.keyed): a ticket over both an old and a
  new threshold stays the same finding. A custom rule's are CUSTOM_IDENTITY's, fixed values written once (never the
  registry's starting values, which a release may move), so neither editing its threshold nor a new release ever
  renames a finding;
- custom (JO-20): whether an admin added it. A custom rule's requires_evidence and requires_stages come from its
  params (registry.requirements), so "not measurable" works for it as for a catalogue rule.
The board's deleted custom rules are kept beside its rules (RulesConfig.find, title_of), so their findings keep their
titles. With no settings and no custom rules it is the catalogue itself, so a board nobody changed runs exactly as it
did before. A stored value that can no longer apply (a param no longer editable, or out of its bounds after an
upgrade; a custom rule whose check or params no longer fit the registry) is left out with a warning
(rule_setting_ignored), never an error. load_board_settings reads a board's settings through any connection, a
read-only one too, and finds none in a snapshot from before version 9 (no rule_settings table yet), and no custom rule
before version 11.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from oversight.config import ProcessConfig
from oversight.logs import get_logger
from oversight.rules.catalog import Rule, RulesConfig
# EDITABLE, ParamSpec, DECIMALS and Unit are registry.py's since JO-20, read from here as JO-15 always read them.
from oversight.rules.registry import (  # noqa: F401
    CHECK_SPECS,
    CUSTOM_LIMIT,
    DECIMALS,
    EDITABLE,
    FIELDS,
    BoardOptions,
    ParamSpec,
    Unit,
    board_options,
    check_applies,
    check_definition,
    check_description,
    check_params,
    check_title,
    engine_applies,
    requirements,
    status_problems,
    title_key,
)
from oversight.rules.registry import _num
from oversight.rules.registry import number as _number
from oversight.rules.registry import value_problem as _value_problem
from oversight.store.db import Store, open_readonly
from oversight.timeutil import to_iso

log = get_logger("rules")

SEVERITIES: tuple[str, ...] = ("breach", "warning")
REASON_MAX = 500  # characters
HISTORY_LIMIT = 50  # the changes GET /rules/settings lists, newest first
# A changed field's name: enabled, severity, or params.<name>; for a custom rule also rule, title, description and
# applies_to.
ENABLED, SEVERITY, PARAMS = "enabled", "severity", "params."
RULE, TITLE, DESCRIPTION, APPLIES = "rule", "title", "description", "applies_to"
# A custom rule's id: its prefix (R: a breach rule, W: a warning), CUS and its number, two digits or three. Numbers are
# never given twice, deleted rules' included, so a service can add at most CUSTOM_NUMBER_MAX breach rules and as many
# warnings in its life, across every board (docs/DEPLOYMENT.md).
CUSTOM_ID = re.compile(r"([RW])-CUS-([0-9]{2,3})")
CUSTOM_NUMBER_MAX = 999
ChangeOp = Literal["set", "create", "edit", "delete", "restore"]
# The params a custom rule's findings are named by (checks.keyed: only these checks name a finding by a param), by
# check. Fixed values, written once and never to change: not the registry's starting values, which the form starts a
# new rule from and a later release may move, so no release ever gives a custom rule's findings new ids (a finding
# renamed would close as cleared and open again as new). They are what those starting values were when JO-20 shipped.
CUSTOM_IDENTITY: dict[str, dict[str, float]] = {
    "effort_ratio_at_least": {"threshold": 1.5},
    "no_evidence_after_percentile": {"pct": 75.0},
}


def editable_params(rule: Rule) -> list[ParamSpec]:
    """The params of `rule` (as the catalogue has it) an admin may set, in EDITABLE's order: its check's, each with a
    value to start from (rules.yaml's, else the check's default) that is a number."""
    specs = EDITABLE.get(rule.check, ())
    return [spec for spec in specs if _number(rule.params.get(spec.name, spec.default)) is not None]


def default_value(rule: Rule, spec: ParamSpec) -> float:
    """The param's value in the catalogue: rules.yaml's, else the check's default. `rule` is the catalogue's."""
    value = _number(rule.params.get(spec.name, spec.default))
    if value is None:
        raise ValueError(f"{rule.id}: {spec.name} is not a setting this rule has")
    return value


# --- one board's settings, as stored -------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleSetting:
    """One rule's overrides on one board, as rule_settings holds them: None (or no param) wherever the board keeps
    rules.yaml's (a custom rule's: as it was created)."""

    rule_id: str
    enabled: bool | None = None
    severity: str | None = None
    params: tuple[tuple[str, float], ...] = ()  # (name, value), by name

    @property
    def empty(self) -> bool:
        return self.enabled is None and self.severity is None and not self.params


@dataclass(frozen=True)
class CustomRule:
    """A rule an admin added to one board (JO-20), as custom_rules holds it: its definition as created and last edited
    (its params and applies_to as canonical JSON, so a board's settings stay hashable), the board's settings revision
    that created it, who created it and when, and when it was deleted (None while it exists)."""

    rule_id: str
    title: str
    description: str | None
    kind: str
    severity: str  # as created
    check: str
    params_json: str
    applies_json: str
    created_at: str
    created_revision: int
    created_by: str
    updated_at: str
    deleted_at: str | None = None

    @property
    def live(self) -> bool:
        return self.deleted_at is None

    @property
    def params(self) -> dict[str, Any]:
        return _object(self.params_json)

    @property
    def applies_to(self) -> dict[str, Any]:
        found = _object(self.applies_json)
        return {"status_category": found.get("status_category") or [], "issue_type": found.get("issue_type")}

    def definition(self, severity: str | None = None) -> dict[str, Any]:
        """The rule as the history says it: {title, description, check, params, applies_to, severity}, the severity the
        board runs it with (`severity`) or as created."""
        return {"title": self.title, "description": self.description, "check": self.check, "params": self.params,
                "applies_to": self.applies_to, "severity": severity or self.severity}


@dataclass(frozen=True)
class BoardSettings:
    """One board's rule settings: its revision (its highest change; 0 when never changed), its rows, by rule id, and
    its custom rules, deleted ones too, in the order they were added."""

    project: str
    revision: int = 0
    rules: tuple[RuleSetting, ...] = ()
    custom: tuple[CustomRule, ...] = ()

    def of(self, rule_id: str) -> RuleSetting | None:
        return next((setting for setting in self.rules if setting.rule_id == rule_id), None)

    def custom_rule(self, rule_id: str) -> CustomRule | None:
        return next((rule for rule in self.custom if rule.rule_id == rule_id), None)

    @property
    def live_custom(self) -> list[CustomRule]:
        return [rule for rule in self.custom if rule.live]

    def deleted_custom(self, catalogue: RulesConfig) -> list[str]:
        """The custom rules the board deleted, by id (one whose row holds a catalogue rule's id, a hand edit, never)."""
        listed = {rule.id for rule in catalogue.rules}
        return [rule.rule_id for rule in self.custom if not rule.live and rule.rule_id not in listed]


_TABLES_SQL = (
    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ('rule_settings', 'rule_settings_changes')"
)
_CUSTOM_TABLE_SQL = "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'custom_rules'"
_REVISION_SQL = "SELECT MAX(revision) FROM rule_settings_changes WHERE project_key = ?"
_ROWS_SQL = "SELECT rule_id, enabled, severity, params_json FROM rule_settings WHERE project_key = ? ORDER BY rule_id"
_CUSTOM_SQL = (
    "SELECT rule_id, title, description, kind, severity, check_name, params_json, applies_json, created_at,"
    " created_revision, created_by, updated_at, deleted_at FROM custom_rules WHERE project_key = ?"
    " ORDER BY created_revision, rule_id"
)
_CUSTOM_IDS_SQL = "SELECT rule_id FROM custom_rules"
_HISTORY_SQL = (
    "SELECT change_id, at, revision, viewer_hash, viewer_name, reason, changes_json, undo_of"
    " FROM rule_settings_changes WHERE project_key = ? ORDER BY revision DESC LIMIT ?"
)
_CHANGES_SQL = "SELECT changes_json FROM rule_settings_changes WHERE project_key = ? ORDER BY revision"
_CHANGE_OF_BOARD_SQL = "SELECT revision, changes_json FROM rule_settings_changes WHERE change_id = ? AND project_key = ?"


def _has_tables(conn: sqlite3.Connection) -> bool:
    """Whether the snapshot has the rule settings tables (version 9 on): a read-only connection never migrates."""
    return int(conn.execute(_TABLES_SQL).fetchone()[0]) == 2


def _has_custom(conn: sqlite3.Connection) -> bool:
    """Whether the snapshot has custom_rules (version 11 on)."""
    return int(conn.execute(_CUSTOM_TABLE_SQL).fetchone()[0]) == 1


def load_board_settings(conn: sqlite3.Connection, project: str) -> BoardSettings:
    """The board's settings as the snapshot holds them, through any connection (a read-only one too): none in a
    snapshot without the tables yet. The revision is read before the rows, so a save landing between the two shows as
    a newer row under an older revision, which the next save (made from that revision) is refused for."""
    if not _has_tables(conn):
        return BoardSettings(project)
    revision = conn.execute(_REVISION_SQL, (project,)).fetchone()[0] or 0
    settings = (_setting(row) for row in conn.execute(_ROWS_SQL, (project,)).fetchall())
    custom = tuple(_custom(row) for row in conn.execute(_CUSTOM_SQL, (project,)).fetchall()) if _has_custom(conn) else ()
    return BoardSettings(project, int(revision), tuple(setting for setting in settings if not setting.empty), custom)


def _setting(row: Sequence[Any]) -> RuleSetting:
    """A rule_settings row as read (by position, whatever the connection's row factory): a value that cannot be one
    (a hand edit) reads as rules.yaml's."""
    rule_id, enabled, severity, params_json = row[0], row[1], row[2], row[3]
    try:
        stored = json.loads(params_json) if params_json else {}
    except ValueError:
        stored = {}
    numbers = ((name, _number(value)) for name, value in (stored.items() if isinstance(stored, dict) else ()))
    params = {name: number for name, number in numbers if isinstance(name, str) and number is not None}
    return RuleSetting(
        rule_id=str(rule_id),
        enabled=bool(enabled) if enabled in (0, 1) and not isinstance(enabled, str) else None,
        severity=severity if severity in SEVERITIES else None,
        params=tuple(sorted(params.items())),
    )


def _custom(row: Sequence[Any]) -> CustomRule:
    """A custom_rules row as read (by position): a JSON column that cannot be read (a hand edit) reads as {}, which the
    board's rules then leave out with a warning (effective_rules)."""
    values = tuple(row)
    return CustomRule(
        rule_id=str(values[0]), title=str(values[1] or ""), description=values[2] or None, kind=str(values[3]),
        severity=str(values[4]), check=str(values[5]), params_json=_canonical(_object(values[6])),
        applies_json=_canonical(_object(values[7])), created_at=str(values[8]), created_revision=int(values[9] or 0),
        created_by=str(values[10]), updated_at=str(values[11]), deleted_at=values[12] or None,
    )


def next_numbers(conn: sqlite3.Connection) -> dict[str, int]:
    """The next free number of each custom rule id prefix (R, W), across every board: one more than the highest ever
    given, deleted rules included, so an id is never reused."""
    highest = {"R": 0, "W": 0}
    if _has_custom(conn):
        for row in conn.execute(_CUSTOM_IDS_SQL).fetchall():
            found = CUSTOM_ID.fullmatch(str(row[0]))
            if found:
                highest[found.group(1)] = max(highest[found.group(1)], int(found.group(2)))
    return {prefix: number + 1 for prefix, number in highest.items()}


def read_history(conn: sqlite3.Connection, project: str, limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    """The board's changes, newest first, at most `limit`, as GET /rules/settings sends them: {change_id, at, revision,
    by: {hash, name}, reason, undo_of, changes: [{rule_id, field, from, to}]}; none before version 9."""
    if not _has_tables(conn):
        return []
    return [_history_entry(row) for row in conn.execute(_HISTORY_SQL, (project, limit)).fetchall()]


def _history_entry(row: Sequence[Any]) -> dict[str, Any]:
    change_id, at, revision, viewer_hash, viewer_name, reason, changes_json, undo_of = tuple(row)
    return {
        "change_id": change_id,
        "at": at,
        "revision": int(revision),
        "by": {"hash": viewer_hash, "name": viewer_name},
        "reason": reason or None,
        "undo_of": undo_of or None,
        "changes": _field_changes_of(changes_json),
    }


def _field_changes_of(changes_json: object) -> list[dict[str, Any]]:
    """A change's changes_json as [{rule_id, field, from, to}]: an item that cannot be read (a hand edit) is left out."""
    try:
        stored = json.loads(changes_json) if isinstance(changes_json, str) and changes_json else []
    except ValueError:
        stored = []
    return [
        {"rule_id": item["rule_id"], "field": item["field"], "from": item.get("from"), "to": item.get("to")}
        for item in (stored if isinstance(stored, list) else [])
        if isinstance(item, dict) and isinstance(item.get("rule_id"), str) and isinstance(item.get("field"), str)
    ]


def turned_off(conn: sqlite3.Connection, project: str) -> frozenset[str]:
    """The rules the board itself turned off: those its own latest change of `enabled` set to false, from its history
    (none before version 9), through any connection. A rule rules.yaml has off is one when the board turned it on and
    then off again: its findings were raised by the board's settings, so they close as settings_changed, as those of a
    rule rules.yaml has on do when the board turns it off. A rule rules.yaml has off that the board never turned on is
    not: its findings are left as they are."""
    if not _has_tables(conn):
        return frozenset()
    latest: dict[str, bool] = {}
    for row in conn.execute(_CHANGES_SQL, (project,)).fetchall():
        for change in _field_changes_of(row[0]):
            if change["field"] == ENABLED and isinstance(change["to"], bool):
                latest[change["rule_id"]] = change["to"]
    return frozenset(rule_id for rule_id, on in latest.items() if not on)


# --- where a board's settings stand -------------------------------------------------------------------------------

# The board's latest succeeded rules runs this many at most: runs that overlap are never that many, so the run whose
# settings the board's findings show is always among them, and reading them stays cheap however long the history grows
# (the runs index on kind and started_at, read newest first, stops after them).
RECENT_RUNS = 20
# Each succeeded rules run that holds stats for the board, newest first. The board key is a project key
# ([A-Z][A-Z0-9_]*), so it never needs escaping inside the JSON path.
_RECENT_RUNS_SQL = f"""
SELECT run_id, started_at, finished_at, stats_json
FROM runs
WHERE kind = 'rules' AND status = 'succeeded'
  AND CASE WHEN json_valid(stats_json) THEN json_type(stats_json, '$."' || ? || '"') END = 'object'
ORDER BY started_at DESC, run_id DESC
LIMIT {RECENT_RUNS}
"""


@dataclass(frozen=True)
class AppliedRun:
    """A succeeded rules run as it touched one board: when it started and finished (storage format), the board's
    settings revision it applied (0: none, or a run from before JO-15) and its stats for the board."""

    run_id: str
    started_at: str
    finished_at: str | None
    revision: int
    stats: Mapping[str, Any]


def recent_runs(conn: sqlite3.Connection, project: str) -> list[AppliedRun]:
    """The board's latest RECENT_RUNS succeeded rules runs, newest first, through any connection (a read-only one
    too)."""
    runs = []
    for row in conn.execute(_RECENT_RUNS_SQL, (project,)).fetchall():
        run_id, started_at, finished_at, stats_json = row[0], row[1], row[2], row[3]
        try:
            stats = json.loads(stats_json)
        except (TypeError, ValueError, RecursionError):  # what SQLite reads, Python may not: a run left out
            continue
        board = stats.get(project) if isinstance(stats, dict) else None
        if not isinstance(board, dict):
            continue
        revision = int(_number(board.get("settings_revision")) or 0)  # none before JO-15
        runs.append(AppliedRun(str(run_id), str(started_at or ""), finished_at, max(revision, 0), board))
    return runs


def showing(runs: Sequence[AppliedRun]) -> AppliedRun | None:
    """The run whose settings the board's findings show: the highest settings revision among the board's recent runs,
    the latest started of those (None before its first rules run). Revisions only grow, and a run reads the board's
    settings inside its write transaction for the board, which no save and no other run shares, so the highest is the
    one the findings show, whichever run started last (a re-check can start after a cycle that reaches the board later,
    or at the same moment)."""
    return max(runs, key=lambda run: (run.revision, run.started_at, run.run_id), default=None)


# --- the board's rules ------------------------------------------------------------------------------------------

# effective_rules' answers, by catalogue, board, rows and custom rules: a request reads its board's rules once, and a
# board's rules are worked out (and validated) once for each set of rows, not once for each request. A few dozen at most.
_MEMO: dict[tuple[int, str, tuple[RuleSetting, ...], tuple[CustomRule, ...]], tuple[RulesConfig, RulesConfig]] = {}
_MEMO_SIZE = 64
_MEMO_LOCK = threading.Lock()


def effective_rules(
    catalogue: RulesConfig, settings: BoardSettings, custom: Sequence[CustomRule] | None = None
) -> RulesConfig:
    """The rules a board runs with: the catalogue, the board's settings applied, its custom rules (`custom`, else the
    settings' own) appended, validated again (see the module docstring). With no settings and no custom rule, the
    catalogue itself. ValueError for a `catalogue` that is already a board's."""
    if catalogue.is_board_rules:
        raise ValueError("effective_rules takes the catalogue (rules.yaml), not one board's rules")
    customs = settings.custom if custom is None else tuple(custom)
    if not settings.rules and not customs:
        return catalogue
    key = (id(catalogue), settings.project, settings.rules, customs)
    with _MEMO_LOCK:
        found = _MEMO.get(key)
    if found is not None and found[0] is catalogue:
        return found[1]
    board = _board_rules(catalogue, settings, customs)
    with _MEMO_LOCK:
        while len(_MEMO) >= _MEMO_SIZE:
            _MEMO.pop(next(iter(_MEMO)))
        _MEMO[key] = (catalogue, board)  # the catalogue kept with it: its id names no other object while it is here
    return board


def _board_rules(catalogue: RulesConfig, settings: BoardSettings, customs: Sequence[CustomRule]) -> RulesConfig:
    listed_ids = {rule.id for rule in catalogue.rules}
    known = listed_ids | {rule.rule_id for rule in customs}
    for setting in settings.rules:
        if setting.rule_id not in known:  # a rule since removed from rules.yaml
            log.warning("rule_setting_ignored", project=settings.project, rule=setting.rule_id, setting="rule")
    data = catalogue.model_dump()
    for entry in data["rules"]:
        setting = settings.of(entry["id"])
        if setting is None:
            continue
        rule = catalogue.by_id(entry["id"])
        specs = {spec.name: spec for spec in editable_params(rule)}
        for name, value in setting.params:
            if name not in specs or _value_problem(specs[name], value) is not None:
                log.warning("rule_setting_ignored", project=settings.project, rule=rule.id, setting=f"{PARAMS}{name}")
        effective = _effective(rule, setting)
        entry["enabled"], entry["severity"] = effective.enabled, effective.severity
        entry["params"] = {
            **entry["params"],
            **{name: value for name, value in effective.params if value != default_value(rule, specs[name])},
        }
    entries = {rule.rule_id: _custom_entry(rule, settings, listed_ids) for rule in customs}
    live = {rule.rule_id: rule for rule in customs if rule.live and entries[rule.rule_id] is not None}
    data["rules"] += [entries[rule_id] for rule_id in live]
    board = RulesConfig.model_validate(data)
    board._board_rules = True
    for rule in board.rules:
        custom = live.get(rule.id)
        if custom is not None:
            _mark_custom(rule, custom)
            continue
        listed = catalogue.by_id(rule.id)
        rule._identity_params = dict(listed.params)
        if rule.severity != listed.severity:
            rule._board_version = f"{catalogue.version}+{rule.severity}"
        if rule.params != listed.params:
            rule._settings_hash = settings_hash(rule.params)
        listed_settings = (listed.enabled, listed.severity, listed.params)
        rule._customised = (rule.enabled, rule.severity, rule.params) != listed_settings
    for custom_rule in customs:
        entry = entries[custom_rule.rule_id]
        if custom_rule.live or entry is None:
            continue
        retired = Rule.model_validate(entry)
        _mark_custom(retired, custom_rule)
        board._retired[retired.id] = retired
    return board


def _custom_entry(rule: CustomRule, settings: BoardSettings, catalogue_ids: set[str]) -> dict[str, Any] | None:
    """A custom rule as a rules.yaml entry, its on/off and severity as the board set them; None, with a warning, for
    one that no longer fits (an id rules.yaml also holds, a check or params the registry no longer takes)."""
    spec = CHECK_SPECS.get(rule.check)
    params = rule.params
    if rule.rule_id in catalogue_ids or spec is None or not _params_fit(rule.check, params):
        log.warning("rule_setting_ignored", project=settings.project, rule=rule.rule_id, setting="rule")
        return None
    setting = settings.of(rule.rule_id)
    evidence, stages = requirements(rule.check, params)
    entry = {
        "id": rule.rule_id,
        "title": rule.title,
        "description": rule.description or "",
        "kind": rule.kind,
        "severity": setting.severity if setting is not None and setting.severity in SEVERITIES else rule.severity,
        "check": rule.check,
        "params": params,
        "applies_to": engine_applies(spec.scope, rule.applies_to),
        "requires_evidence": evidence,
        "requires_stages": stages,
        "escalates_to": None,
        "scope": spec.scope,
        "enabled": setting.enabled if setting is not None and setting.enabled is not None else True,
    }
    try:
        Rule.model_validate(entry)
    except ValidationError:
        log.warning("rule_setting_ignored", project=settings.project, rule=rule.rule_id, setting="rule")
        return None
    return entry


def _params_fit(check: str, params: Mapping[str, Any]) -> bool:
    """Whether stored params are what the check reads (a hand edit or an older registry may leave them otherwise):
    every param there, each of its kind and, for a number, within its bounds."""
    spec = CHECK_SPECS[check]
    if set(params) != {param.name for param in spec.params}:
        return False
    for param in spec.params:
        value = params[param.name]
        if param.kind == "number":
            if param.spec is None or _value_problem(param.spec, value) is not None:
                return False
        elif param.kind == "statuses":
            if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
                return False
        elif param.kind == "field":
            if value not in FIELDS:
                return False
        elif not isinstance(value, str) or not value:
            return False
    return True


def _mark_custom(rule: Rule, custom: CustomRule) -> None:
    """What a board makes of a custom rule: its version ("c<revision>", "+<severity>" while not as created), the
    fingerprint of its params and scope, CUSTOM_IDENTITY's values as the params its findings are named by, and whether
    its on/off or severity differ from the rule as created."""
    rule._custom = True
    rule._board_version = custom_version(custom, rule.severity)
    rule._settings_hash = settings_hash({"params": rule.params, "applies_to": custom.applies_to})
    rule._identity_params = dict(CUSTOM_IDENTITY.get(custom.check, {}))
    rule._customised = not rule.enabled or rule.severity != custom.severity


def custom_version(custom: CustomRule, severity: str) -> str:
    """A custom rule's rule_version: "c<created_revision>", with "+<severity>" while its severity differs from the one
    it was created with."""
    version = f"c{custom.created_revision}"
    return version if severity == custom.severity else f"{version}+{severity}"


def settings_hash(params: Mapping[str, Any]) -> str:
    """The first 12 hex characters of the SHA-256 of the params' canonical JSON: sorted keys, no spaces, every number
    as a float (so 2 and 2.0 in rules.yaml are one fingerprint)."""
    canonical = {name: _number(value) if _number(value) is not None else value for name, value in params.items()}
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def board_rules(db_path: Path, catalogue: RulesConfig, project: str) -> RulesConfig:
    """The rules `project` runs with, its settings read from the snapshot at db_path through a read-only connection of
    their own: the catalogue itself when there is no snapshot yet or the board has no settings."""
    if not Path(db_path).exists():
        return catalogue
    conn = open_readonly(db_path)
    try:
        settings = load_board_settings(conn, project)
    finally:
        conn.close()
    return effective_rules(catalogue, settings)


@dataclass(frozen=True)
class _Effective:
    """A rule's editable settings on a board: on or off, its severity, and every editable param, in EDITABLE's order."""

    enabled: bool
    severity: str
    params: tuple[tuple[str, float], ...]


def _effective(rule: Rule, setting: RuleSetting | None) -> _Effective:
    """What `rule` (the catalogue's) is on a board with `setting`, as effective_rules applies it: a stored value that
    cannot apply reads as the catalogue's."""
    stored = dict(setting.params) if setting is not None else {}
    params = []
    for spec in editable_params(rule):
        value = stored.get(spec.name)
        usable = value is not None and _value_problem(spec, value) is None
        params.append((spec.name, float(value) if usable and value is not None else default_value(rule, spec)))
    enabled = setting.enabled if setting is not None and setting.enabled is not None else rule.enabled
    severity = setting.severity if setting is not None and setting.severity in SEVERITIES else rule.severity
    return _Effective(enabled, str(severity), tuple(params))


def custom_state(rule: CustomRule, setting: RuleSetting | None) -> tuple[bool, str]:
    """A custom rule's on/off and severity on its board: as the board set them, else as created (on)."""
    enabled = setting.enabled if setting is not None and setting.enabled is not None else True
    severity = setting.severity if setting is not None and setting.severity in SEVERITIES else rule.severity
    return enabled, severity


# --- changing them ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One rule's change as a request names it (op "set", JO-15's): any of enabled, severity and params (some of its
    editable params; a custom rule's params of any kind), or reset, back to rules.yaml for that rule (with nothing else).
    JO-20's ops on custom rules: create (rule: the new rule's definition; no rule_id, the id is given here), edit (edit:
    the fields given, of title, description, params and applies_to), delete and restore."""

    rule_id: str
    enabled: bool | None = None
    severity: str | None = None
    params: Mapping[str, object] = field(default_factory=dict)
    reset: bool = False
    op: ChangeOp = "set"
    rule: Mapping[str, object] | None = None
    edit: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def adding(cls, rule: Mapping[str, object]) -> Change:
        return cls("", op="create", rule=dict(rule))

    @classmethod
    def editing(cls, rule_id: str, **fields: object) -> Change:
        return cls(rule_id, op="edit", edit=fields)

    @classmethod
    def deleting(cls, rule_id: str) -> Change:
        return cls(rule_id, op="delete")

    @classmethod
    def restoring(cls, rule_id: str) -> Change:
        return cls(rule_id, op="restore")


@dataclass(frozen=True)
class Problem:
    """Why a change set was refused, in plain words, with the rule and the field it is about when there is one. A new
    rule's problem names no rule (it has no id yet) but its place: the index of its create in the change set."""

    rule_id: str | None
    # enabled | severity | params.<name> | params (not an object of thresholds) | reset | rule_id (a change that names
    # no rule) | changes | revision | reason | undo_of, or the name of a key the request does not take (at most 40
    # characters); for a custom rule also op, rule, title, description, check, applies_to, applies_to.status_category,
    # applies_to.issue_type; None when the problem is with the request as a whole.
    field: str | None
    message: str
    # A new rule's: where its create stands in the change set, 0 first.
    index: int | None = None

    def as_dict(self) -> dict[str, str | None]:
        """The problem as the API's errors say it: a new rule's field by its place in the request, as
        "changes.<index>.rule.<field>" ("changes.1.rule.params.days"; "changes.1.rule" for the rule as a whole), so a
        request that adds several rules says which one each problem is about."""
        field = self.field
        if self.rule_id is None and self.index is not None:
            field = ".".join(part for part in (f"changes.{self.index}.rule", self.field) if part)
        return {"rule_id": self.rule_id, "field": field, "message": self.message}


class SettingsInvalid(ValueError):
    """A change set refused as a whole: nothing was written. `problems` names every problem found."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = list(problems)
        super().__init__(" ".join(problem.message for problem in self.problems))


class SettingsConflict(Exception):
    """A save made from another revision than the board's: someone else changed its rule settings first."""

    def __init__(self, revision: int) -> None:
        super().__init__(f"the board's rule settings are at revision {revision}")
        self.revision = revision


@dataclass(frozen=True)
class FieldChange:
    """One field a change set changes, with its effective value on the board before and after it (a custom rule's
    definition, or None, for its field `rule`)."""

    rule_id: str
    field: str  # enabled | severity | params.<name> | rule | title | description | applies_to
    before: Any
    after: Any

    def as_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "field": self.field, "from": self.before, "to": self.after}


@dataclass(frozen=True)
class Plan:
    """What a valid change set does: the board's settings after it (its revision unchanged) and every field it
    changes, in catalogue order (enabled, then severity, then the params in EDITABLE's order), then the custom rules in
    the order they were added."""

    settings: BoardSettings
    changes: tuple[FieldChange, ...]
    # The custom rules whose custom_rules row it adds or changes (added, edited, deleted, restored), in that order.
    custom_ids: tuple[str, ...] = ()

    @property
    def rule_ids(self) -> list[str]:
        """The rules whose settings on the board it changes, in catalogue order (then the custom rules')."""
        return list(dict.fromkeys(change.rule_id for change in self.changes))


@dataclass(frozen=True)
class CustomContext:
    """What a change to a board's custom rules is checked and made with, beyond its settings: the board's options (what
    a rule may name: registry.BoardOptions), the next free number of each id prefix across every board, and when and by
    whom the change is made (storage format, and the viewer's name as the history keeps it)."""

    options: BoardOptions
    numbers: Mapping[str, int] = field(default_factory=lambda: {"R": 1, "W": 1})
    at: str = ""
    by: str = ""


def custom_context(
    conn: sqlite3.Connection, process: ProcessConfig | None, catalogue: RulesConfig, project: str, *, at: str, by: str
) -> CustomContext:
    """The board's CustomContext through `conn` (a read-only one too): its options from the snapshot and process
    (none without a process), the next ids across every board, `at` and `by`."""
    options = (board_options(conn, process, project, exclude_types=catalogue.defaults.exclude_types)
               if process is not None else BoardOptions(project))
    return CustomContext(options, next_numbers(conn), at, by)


_VERB = {"edit": "edited", "delete": "deleted", "restore": "restored"}


def plan_changes(
    catalogue: RulesConfig, current: BoardSettings, changes: Sequence[Change], board: CustomContext | None = None
) -> Plan:
    """Check a change set against the board's settings, all or nothing: SettingsInvalid naming every problem (no
    change at all, an unknown rule, a rule named twice, a reset with anything else, a change that names or changes
    nothing, a param the rule has no setting for, a value that is not a number, out of its bounds or with more than 4
    decimal places, then the cross-rule rule; for custom rules, each definition against the registry and the board's
    options (`board`), then the limit of custom rules and the titles), else the Plan. A change other than a create
    names a rule the board holds already: the id a create in the same change set would be given is no rule on the
    board yet."""
    if not changes:
        raise SettingsInvalid([Problem(None, "changes", "Name at least one change.")])
    board = board or CustomContext(BoardOptions(current.project))
    problems: list[Problem] = []
    rows = {setting.rule_id: setting for setting in current.rules}
    customs = {rule.rule_id: rule for rule in current.custom}
    held = frozenset(customs)  # the board's custom rules before this change set: the only ones a change may name
    numbers = dict(board.numbers)
    for custom in current.custom:  # never an id the board holds, whatever the context says
        found_id = CUSTOM_ID.fullmatch(custom.rule_id)
        if found_id:
            prefix = found_id.group(1)
            numbers[prefix] = max(numbers.get(prefix, 1), int(found_id.group(2)) + 1)
    named: set[str] = set()
    touched: list[str] = []  # custom rules whose row changes, in the order the changes name them
    titled: list[str] = []  # custom rules whose title is new on the board: added, renamed or restored
    added: dict[str, int] = {}  # each rule the change set adds, by id: where its create stands in it
    for index, change in enumerate(changes):
        if change.op == "create":
            found, made = _create(change, board, numbers, current.revision, index)
            if made is None:
                problems.extend(found)
                continue
            customs[made.rule_id] = made
            touched.append(made.rule_id)
            titled.append(made.rule_id)
            added[made.rule_id] = index
            continue
        rule = next((rule for rule in catalogue.rules if rule.id == change.rule_id), None)
        custom = customs.get(change.rule_id) if rule is None and change.rule_id in held else None
        if rule is None and custom is None:
            problems.append(Problem(change.rule_id, None, _unknown(change.rule_id)))
            continue
        if change.rule_id in named:
            problems.append(Problem(
                change.rule_id, None, f"{change.rule_id} is named more than once: make one change per rule."))
            continue
        named.add(change.rule_id)
        if custom is not None:
            found, made, row, renamed = _custom_change(custom, rows.get(custom.rule_id), change, board)
            if found:
                problems.extend(found)
                continue
            if made != custom:
                customs[custom.rule_id] = made
                touched.append(custom.rule_id)
            if renamed:
                titled.append(custom.rule_id)
            if row is None:
                rows.pop(custom.rule_id, None)
            else:
                rows[custom.rule_id] = row
            continue
        assert rule is not None
        if change.op != "set":
            problems.append(Problem(rule.id, None, f"{rule.id} is a catalogue rule: only a custom rule can be "
                                                   f"{_VERB[change.op]}."))
            continue
        found, setting = _changed(rule, rows.get(rule.id), change)
        if found:
            problems.extend(found)
        elif _effective(rule, setting) == _effective(rule, rows.get(rule.id)):
            problems.append(Problem(rule.id, None, f"{rule.id} already has these settings on this board."))
        elif setting is None:
            rows.pop(rule.id, None)
        else:
            rows[rule.id] = setting
    if problems:
        raise SettingsInvalid(problems)
    problems = _custom_limits(catalogue, current, customs, titled, added)
    if problems:
        raise SettingsInvalid(problems)
    ordered = tuple(sorted(customs.values(), key=lambda rule: (rule.created_revision, _id_order(rule.rule_id))))
    proposed = BoardSettings(
        current.project, current.revision, tuple(rows[rule_id] for rule_id in sorted(rows)), ordered
    )
    problems = _cross_rule(catalogue, proposed, named)
    if problems:
        raise SettingsInvalid(problems)
    changed = (*_field_changes(catalogue, current, proposed), *_custom_field_changes(current, proposed))
    if not changed:  # every change above changes something: never a revision with nothing in its history
        raise SettingsInvalid([Problem(None, "changes", "These changes leave the board's settings as they are.")])
    return Plan(proposed, tuple(changed), tuple(dict.fromkeys(touched)))


def _unknown(rule_id: str) -> str:
    if CUSTOM_ID.fullmatch(rule_id):
        return f"{rule_id} is not a rule on this board."
    return f"{rule_id} is not a rule in the catalogue."


def _id_order(rule_id: str) -> tuple[str, int]:
    found = CUSTOM_ID.fullmatch(rule_id)
    return (found.group(1), int(found.group(2))) if found else (rule_id, 0)


def _changed(rule: Rule, row: RuleSetting | None, change: Change) -> tuple[list[Problem], RuleSetting | None]:
    """The rule's row after the change (None: nothing left to store), or the change's problems."""
    given = change.enabled is not None or change.severity is not None or bool(change.params)
    if change.reset:
        if given:
            return [Problem(rule.id, "reset", "A reset cannot be combined with other changes to the same rule.")], row
        return [], None
    if not given:
        return [Problem(rule.id, None, f"The change to {rule.id} names nothing to change.")], row
    problems: list[Problem] = []
    enabled = row.enabled if row is not None else None
    severity = row.severity if row is not None else None
    specs = {spec.name: spec for spec in editable_params(rule)}
    params = {
        name: value for name, value in (row.params if row is not None else ())
        if name in specs and _value_problem(specs[name], value) is None
    }
    if change.enabled is not None:
        if isinstance(change.enabled, bool):
            enabled = None if change.enabled == rule.enabled else change.enabled
        else:
            problems.append(Problem(rule.id, ENABLED, "On or off must be true or false."))
    if change.severity is not None:
        if change.severity in SEVERITIES:
            severity = None if change.severity == rule.severity else change.severity
        else:
            problems.append(Problem(rule.id, SEVERITY, "Severity must be breach or warning."))
    for name, value in change.params.items():
        spec = specs.get(name)
        if spec is None:
            problems.append(Problem(
                rule.id, f"{PARAMS}{name}", f"{rule.id} has no setting called {name} that can be changed."))
            continue
        problem = _value_problem(spec, value)
        if problem is not None:
            problems.append(Problem(rule.id, f"{PARAMS}{name}", problem))
            continue
        number = float(value)  # type: ignore[arg-type]
        if number == default_value(rule, spec):
            params.pop(name, None)
        else:
            params[name] = number
    if problems:
        return problems, row
    setting = RuleSetting(rule.id, enabled, severity, tuple(sorted(params.items())))
    return [], None if setting.empty else setting


# --- custom rules: create, edit, delete, restore ----------------------------------------------------------------


def _create(
    change: Change, board: CustomContext, numbers: dict[str, int], revision: int, index: int = 0
) -> tuple[list[Problem], CustomRule | None]:
    """The new rule, with the next id of its prefix, created at the revision the change will make; or its problems,
    each on its field (no rule id: it has none yet; `index`, the create's place in the change set, says which)."""
    definition, found = check_definition(change.rule, board.options)
    if definition is None:
        return [Problem(None, problem.field, problem.message, index) for problem in found], None
    prefix = "R" if definition.severity == "breach" else "W"
    number = numbers.get(prefix, 1)
    if number > CUSTOM_NUMBER_MAX:
        kind = "breach rules" if prefix == "R" else "warnings"
        return [Problem(None, None, f"All {CUSTOM_NUMBER_MAX} ids for custom {kind} have been used, so no more can be "
                                    "added.")], None
    numbers[prefix] = number + 1
    return [], CustomRule(
        rule_id=f"{prefix}-CUS-{number:02d}",
        title=definition.title,
        description=definition.description,
        kind="rule" if prefix == "R" else "warning",
        severity=definition.severity,
        check=definition.check,
        params_json=_canonical(definition.params),
        applies_json=_canonical(definition.applies_to),
        created_at=board.at,
        created_revision=revision + 1,
        created_by=board.by,
        updated_at=board.at,
    )


def _custom_change(
    rule: CustomRule, row: RuleSetting | None, change: Change, board: CustomContext
) -> tuple[list[Problem], CustomRule, RuleSetting | None, bool]:
    """A change to a custom rule: its problems, or the rule and its row after it, and whether its title is new."""
    rule_id = rule.rule_id
    if change.op == "delete":
        if not rule.live:
            return [Problem(rule_id, None, f"{rule_id} is already deleted.")], rule, row, False
        return [], replace(rule, deleted_at=board.at or rule.updated_at, updated_at=board.at or rule.updated_at), row, False
    if change.op == "restore":
        if rule.live:
            return [Problem(rule_id, None, f"{rule_id} is not deleted.")], rule, row, False
        return [], replace(rule, deleted_at=None, updated_at=board.at or rule.updated_at), row, True
    if not rule.live:
        return [Problem(rule_id, None, f"{rule_id} was deleted: restore it before changing it.")], rule, row, False
    if change.op == "set" and change.reset:
        return [Problem(rule_id, "reset", "A custom rule has no catalogue settings to reset to.")], rule, row, False
    fields = dict(change.edit) if change.op == "edit" else ({"params": dict(change.params)} if change.params else {})
    if change.op == "set" and change.enabled is None and change.severity is None and not change.params:
        return [Problem(rule_id, None, f"The change to {rule_id} names nothing to change.")], rule, row, False
    if change.op == "edit" and not fields:
        return [Problem(rule_id, None, f"The change to {rule_id} names nothing to change.")], rule, row, False
    problems, made = _edited(rule, fields, board)
    new_row = row
    if change.op == "set":
        found, new_row = _custom_row(rule, row, change)
        problems += found
    if problems:
        return problems, rule, row, False
    same_rule = made.definition() == rule.definition()
    if same_rule and custom_state(rule, new_row) == custom_state(rule, row):
        return [Problem(rule_id, None, f"{rule_id} already has these settings on this board.")], rule, row, False
    if same_rule:
        made = rule
    elif board.at:
        made = replace(made, updated_at=board.at)
    return [], made, new_row, title_key(made.title) != title_key(rule.title)


def _edited(rule: CustomRule, fields: Mapping[str, object], board: CustomContext) -> tuple[list[Problem], CustomRule]:
    """The rule with the given fields edited (title, description, params, applies_to), each validated against the
    registry and the board's options; the others kept. Never its check. An edit of its params or its scope also checks
    that each status it names is in a category it applies to (registry.status_problems): otherwise it could never
    flag anything there."""
    rule_id, spec = rule.rule_id, CHECK_SPECS.get(rule.check)
    problems: list[Problem] = []
    if spec is None:
        return [Problem(rule_id, "check", f"{rule_id}'s check is no longer one this service runs.")], rule
    extra = sorted(str(key) for key in fields if key not in (TITLE, DESCRIPTION, "params", APPLIES))
    for key in extra:
        message = ("The check can't change: delete the rule and add it again." if key == "check"
                   else f"A change cannot set {key[:40]}.")
        problems.append(Problem(rule_id, key[:40], message))
    made = rule
    if TITLE in fields:
        title, found = check_title(fields[TITLE])
        problems += [Problem(rule_id, problem.field, problem.message) for problem in found]
        if title is not None:
            made = replace(made, title=title)
    if DESCRIPTION in fields:
        description, found = check_description(fields[DESCRIPTION])
        problems += [Problem(rule_id, problem.field, problem.message) for problem in found]
        if not found:
            made = replace(made, description=description)
    if "params" in fields:
        given = fields["params"]
        names = list(given) if isinstance(given, Mapping) else []
        params, found = check_params(spec, given, board.options, only=names)
        problems += [Problem(rule_id, problem.field, problem.message) for problem in found]
        if not found:
            made = replace(made, params_json=_canonical({**made.params, **params}))
    if APPLIES in fields:
        given = fields[APPLIES]
        merged = {**made.applies_to, **given} if isinstance(given, Mapping) else given
        applies, found = check_applies(spec, merged, board.options)
        problems += [Problem(rule_id, problem.field, problem.message) for problem in found]
        if not found:
            made = replace(made, applies_json=_canonical(applies))
    if not problems and ("params" in fields or APPLIES in fields):
        found = status_problems(spec, made.params, made.applies_to, board.options)
        problems += [Problem(rule_id, problem.field, problem.message) for problem in found]
    return problems, made


def _custom_row(rule: CustomRule, row: RuleSetting | None, change: Change) -> tuple[list[Problem], RuleSetting | None]:
    """A custom rule's rule_settings row after a change of its on/off or severity, relative to the rule as created (on,
    and its severity then); None when nothing is left to store."""
    problems: list[Problem] = []
    enabled = row.enabled if row is not None else None
    severity = row.severity if row is not None else None
    if change.enabled is not None:
        if isinstance(change.enabled, bool):
            enabled = None if change.enabled else False
        else:
            problems.append(Problem(rule.rule_id, ENABLED, "On or off must be true or false."))
    if change.severity is not None:
        if change.severity in SEVERITIES:
            severity = None if change.severity == rule.severity else change.severity
        else:
            problems.append(Problem(rule.rule_id, SEVERITY, "Severity must be breach or warning."))
    setting = RuleSetting(rule.rule_id, enabled, severity)
    return problems, None if setting.empty else setting


def _custom_limits(
    catalogue: RulesConfig, current: BoardSettings, customs: Mapping[str, CustomRule], titled: Sequence[str],
    added: Mapping[str, int],
) -> list[Problem]:
    """At most CUSTOM_LIMIT live custom rules on the board (refused only for a change set that adds to them), and each
    title new on the board unique among its rules, catalogue and custom, as a reader sees it (registry.title_key:
    ignoring case, and compatibility forms such as a fullwidth letter). `added`: the rules the change set adds, each
    with its create's place in it."""
    problems: list[Problem] = []
    before = sum(1 for rule in current.custom if rule.live)
    after = sum(1 for rule in customs.values() if rule.live)
    if after > CUSTOM_LIMIT and after > before:
        problems.append(Problem(None, None, f"A board can have at most {CUSTOM_LIMIT} custom rules: delete one first."))
    titles: dict[str, list[str]] = {}
    for rule in catalogue.rules:
        titles.setdefault(title_key(rule.title), []).append(rule.id)
    for custom in customs.values():
        if custom.live:
            titles.setdefault(title_key(custom.title), []).append(custom.rule_id)
    for rule_id in dict.fromkeys(titled):
        custom = customs[rule_id]
        if custom.live and any(other != rule_id for other in titles.get(title_key(custom.title), [])):
            problems.append(Problem(None if rule_id in added else rule_id, TITLE,
                                    "Another rule on this board already has this name.", added.get(rule_id)))
    return problems


def _cross_rule(catalogue: RulesConfig, proposed: BoardSettings, named: set[str]) -> list[Problem]:
    """A warning that escalates to a breach rule of the same check, both with a threshold and both on, keeps its
    threshold strictly below the breach rule's: checked for every pair the change set touches, so an unrelated change
    is never refused for a pair rules.yaml itself got wrong. The problem is the warning's threshold when the change set
    names the warning, else the breach rule's, each said with the other rule's value in its unit, in the dashboard's
    words."""
    rules = effective_rules(catalogue, proposed)
    problems: list[Problem] = []
    for warning in rules.rules:
        if warning.escalates_to is None:
            continue
        breach = rules.by_id(warning.escalates_to)
        if warning.check != breach.check or not (warning.enabled and breach.enabled):
            continue
        if warning.id not in named and breach.id not in named:
            continue
        low, high = _number(warning.params.get("threshold")), _number(breach.params.get("threshold"))
        if low is None or high is None or low < high:
            continue
        spec = next((spec for spec in EDITABLE.get(warning.check, ()) if spec.name == "threshold"), None)
        if warning.id in named:
            problems.append(Problem(
                warning.id, f"{PARAMS}threshold",
                f"Must stay below {breach.id}'s {_words(spec, high)} while both are on: the warning comes first.",
            ))
        else:
            problems.append(Problem(
                breach.id, f"{PARAMS}threshold",
                f"Must stay above {warning.id}'s {_words(spec, low)} while both are on.",
            ))
    return problems


def _field_changes(catalogue: RulesConfig, current: BoardSettings, proposed: BoardSettings) -> Iterator[FieldChange]:
    for rule in catalogue.rules:
        before, after = _effective(rule, current.of(rule.id)), _effective(rule, proposed.of(rule.id))
        if before == after:
            continue
        if before.enabled != after.enabled:
            yield FieldChange(rule.id, ENABLED, before.enabled, after.enabled)
        if before.severity != after.severity:
            yield FieldChange(rule.id, SEVERITY, before.severity, after.severity)
        for (name, old), (_name, new) in zip(before.params, after.params, strict=True):
            if old != new:
                yield FieldChange(rule.id, f"{PARAMS}{name}", old, new)


def _custom_field_changes(current: BoardSettings, proposed: BoardSettings) -> Iterator[FieldChange]:
    """Each custom rule's changed fields, in the order the rules were added: rule (added, deleted, restored), else
    enabled, severity, title, description, applies_to and each param, as the board runs them before and after."""
    for rule in proposed.custom:
        before = current.custom_rule(rule.rule_id)
        enabled, severity = custom_state(rule, proposed.of(rule.rule_id))
        if before is None or not before.live:
            if rule.live:
                yield FieldChange(rule.rule_id, RULE, None, rule.definition(severity))
            continue
        was_enabled, was_severity = custom_state(before, current.of(rule.rule_id))
        if not rule.live:
            yield FieldChange(rule.rule_id, RULE, before.definition(was_severity), None)
            continue
        if was_enabled != enabled:
            yield FieldChange(rule.rule_id, ENABLED, was_enabled, enabled)
        if was_severity != severity:
            yield FieldChange(rule.rule_id, SEVERITY, was_severity, severity)
        for name, old, new in ((TITLE, before.title, rule.title), (DESCRIPTION, before.description, rule.description),
                               (APPLIES, before.applies_to, rule.applies_to)):
            if not _same(old, new):
                yield FieldChange(rule.rule_id, name, old, new)
        old_params, new_params = before.params, rule.params
        for name in dict.fromkeys([*old_params, *new_params]):
            if not _same(old_params.get(name), new_params.get(name)):
                yield FieldChange(rule.rule_id, f"{PARAMS}{name}", old_params.get(name), new_params.get(name))


@dataclass(frozen=True)
class SavedChange:
    """A change written: its id, the board's revision it made, when (storage format), and each field it changed."""

    change_id: str
    revision: int
    at: str
    changes: tuple[FieldChange, ...]


def save_board_settings(
    store: Store,
    catalogue: RulesConfig,
    project: str,
    *,
    revision: int,
    changes: Sequence[Change],
    viewer_hash: str,
    viewer_name: str,
    now: datetime,
    reason: str | None = None,
    undo_of: str | None = None,
    process: ProcessConfig | None = None,
) -> SavedChange:
    """Write one change to a board's rule settings, all or nothing, in one write transaction (BEGIN IMMEDIATE, waiting
    for the lock as the connection's busy timeout allows): the board's revision read again inside it
    (SettingsConflict unless it is `revision`), the change set validated against the settings as they are, and its
    custom rules against the board's options as they are (`process` maps the board; without it a custom rule can name
    no status, stage or evidence) and, with undo_of, checked to undo that change of this board, its latest, exactly
    (SettingsInvalid with every problem), then the board's rows upserted or deleted, its custom rules written and the
    change recorded with the next revision. The viewer is as the access log reads them; the reason is the caller's to
    clean."""
    at = to_iso(now)
    if at is None:
        raise ValueError("save_board_settings needs an aware datetime for now")
    with store.transaction():
        current = load_board_settings(store.conn, project)
        if revision != current.revision:
            raise SettingsConflict(current.revision)
        problems: list[Problem] = []
        undone = None if undo_of is None else store.query_one(_CHANGE_OF_BOARD_SQL, (undo_of, project))
        if undo_of is not None and undone is None:
            problems.append(Problem(None, "undo_of", "The change to undo is not one of this board's."))
        board = custom_context(store.conn, process, catalogue, project, at=at, by=viewer_name)
        try:
            plan = plan_changes(catalogue, current, changes, board)
        except SettingsInvalid as exc:
            raise SettingsInvalid([*problems, *exc.problems]) from None
        if undone is not None:
            problems.extend(_undo_problems(undone, current.revision, plan))
        if problems:
            raise SettingsInvalid(problems)
        for rule_id in dict.fromkeys([*(row.rule_id for row in current.rules), *(row.rule_id for row in plan.settings.rules)]):
            setting = plan.settings.of(rule_id)
            if setting == current.of(rule_id):
                continue
            if setting is None:
                store.delete_rows("rule_settings", {"project_key": project, "rule_id": rule_id})
                continue
            store.upsert("rule_settings", {
                "project_key": project,
                "rule_id": rule_id,
                "enabled": setting.enabled,
                "severity": setting.severity,
                "params_json": dict(setting.params) or None,
                "updated_at": at,
            })
        for rule_id in plan.custom_ids:
            custom = plan.settings.custom_rule(rule_id)
            assert custom is not None
            store.upsert("custom_rules", {
                "rule_id": custom.rule_id,
                "project_key": project,
                "title": custom.title,
                "description": custom.description,
                "kind": custom.kind,
                "severity": custom.severity,
                "check_name": custom.check,
                "params_json": custom.params,
                "applies_json": custom.applies_to,
                "created_at": custom.created_at,
                "created_revision": custom.created_revision,
                "created_by": custom.created_by,
                "updated_at": custom.updated_at,
                "deleted_at": custom.deleted_at,
            })
        change_id = uuid.uuid4().hex
        store.upsert("rule_settings_changes", {
            "change_id": change_id,
            "at": at,
            "project_key": project,
            "revision": current.revision + 1,
            "viewer_hash": viewer_hash,
            "viewer_name": viewer_name,
            "reason": reason,
            "changes_json": [change.as_dict() for change in plan.changes],
            "undo_of": undo_of,
        })
    return SavedChange(change_id, current.revision + 1, at, plan.changes)


def _undo_problems(undone: Sequence[Any], revision: int, plan: Plan) -> list[Problem]:
    """Why a valid change set named as the undo of a change (its revision and changes_json, as stored) is not one: only
    the board's latest change can be undone, as the dashboard offers it, and its undo sets each field that change set
    back to its value before it, and changes nothing else (a custom rule added is deleted, one deleted restored)."""
    if int(undone[0]) != revision:
        return [Problem(None, "undo_of", "Only the board's latest change can be undone.")]
    before = {(change["rule_id"], change["field"]): change["from"] for change in _field_changes_of(undone[1])}
    after = {(change.rule_id, change.field): change.after for change in plan.changes}
    if before.keys() != after.keys() or not all(_same(before[key], after[key]) for key in after):
        return [Problem(
            None, "undo_of",
            "This change does not undo that one: an undo sets each field it changed back to what it was, and nothing else.",
        )]
    return []


# --- values ---------------------------------------------------------------------------------------------------------


def _words(spec: ParamSpec | None, value: float) -> str:
    """A value in its param's unit ("1.5×"); the bare number for a param EDITABLE does not name."""
    return spec.words(value) if spec is not None else _num(value)


def _same(stored: object, value: object) -> bool:
    """Whether a value a change recorded (read back from JSON) is `value`: on or off and text as they are, a number by
    its value (2 and 2.0 alike), never a number for a boolean; None only for None; a list or an object item by item (a
    custom rule's definition, its scope, its statuses)."""
    if stored is None or value is None:
        return stored is None and value is None
    if isinstance(value, (bool, str)) or isinstance(stored, (bool, str)):
        return type(stored) is type(value) and stored == value
    if isinstance(value, Mapping) or isinstance(stored, Mapping):
        return (isinstance(stored, Mapping) and isinstance(value, Mapping) and stored.keys() == value.keys()
                and all(_same(stored[key], value[key]) for key in value))
    if isinstance(value, (list, tuple)) or isinstance(stored, (list, tuple)):
        return (isinstance(stored, (list, tuple)) and isinstance(value, (list, tuple)) and len(stored) == len(value)
                and all(_same(left, right) for left, right in zip(stored, value)))
    number = _number(stored)
    return number is not None and number == _number(value)


def _canonical(value: Mapping[str, Any]) -> str:
    """A JSON object as stored and compared: sorted keys, no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _object(text: object) -> dict[str, Any]:
    """A JSON object from its text (a dict as it is); {} for anything that is not one."""
    if isinstance(text, dict):
        return text
    try:
        found = json.loads(text) if isinstance(text, str) and text else {}
    except (ValueError, RecursionError):
        return {}
    return found if isinstance(found, dict) else {}
