"""The rule engine: evaluates the catalogue, opens and resolves findings."""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from oversight.config import FieldsConfig, ProcessConfig
from oversight.errors import ConfigError
from oversight.logs import get_logger
from oversight.rules import checks as _checks
from oversight.rules.catalog import Rule, RuleDefaults, RulesConfig
from oversight.rules.ignores import IGNORED, Ignores, active_ignores
from oversight.rules.settings import (
    BoardSettings,
    effective_rules,
    load_board_settings,
    recent_runs,
    showing,
    turned_off,
)
from oversight.settings import validate_project_keys
from oversight.store.db import Run, Store
from oversight.timeutil import parse_ts, to_iso, utcnow

log = get_logger("rules")

SPRINT_PREFIX = "sprint:"
# A preview names at most this many issue keys for the findings that would open, and as many for those that would close.
EXAMPLES = 5
_KEY = re.compile(r"(.*)-([0-9]+)")


@dataclass
class RuleStats:
    rule_id: str
    measurable: bool
    missing: list[str] = field(default_factory=list)
    checked: int = 0
    hits: int = 0
    opened: int = 0
    refreshed: int = 0
    note: str | None = None
    # Hits an admin ignored: recorded resolved as ignored, never counted as hits, opened or refreshed.
    ignored: int = 0


@dataclass
class RulesResult:
    run_id: str
    project: str
    rules_version: str
    stats: list[RuleStats]
    opened: int
    refreshed: int
    resolved: int
    superseded: int
    linked: int
    untouched: int
    elapsed_s: float
    # Findings resolved as "aged_out": their issue was finished before the look-back.
    aged_out: int = 0
    # Done issues the look-back left unchecked, and the look-back itself (None: off).
    out_of_scope: int = 0
    lookback_days: int | None = None
    settings_changed: int = 0
    settings_revision: int = 0
    customised: list[str] = field(default_factory=list)
    ignored: int = 0

    @property
    def not_measurable(self) -> list[RuleStats]:
        return [s for s in self.stats if not s.measurable]

    def render(self) -> str:
        lines = [
            f"rules {self.project} (catalogue {self.rules_version}): {self.opened} opened, "
            f"{self.refreshed} still open, {self.resolved} resolved, {self.superseded} superseded, "
            f"{self.linked} warnings linked to breaches ({self.elapsed_s:.1f} s)"
        ]
        width = max((len(s.rule_id) for s in self.stats), default=8)
        for s in self.stats:
            if not s.measurable:
                detail = f"not measurable: nothing in Jira shows {_checks.join_words(_checks.label(m) for m in s.missing)}"
            else:
                detail = f"{s.hits} hits ({s.opened} new) on {s.checked} checked"
                if s.ignored:
                    detail += f", {s.ignored} ignored"
                if s.note:
                    detail += f"; {s.note}"
            lines.append(f"  {s.rule_id:<{width}}  {detail}")
        if self.lookback_days is not None:
            lines.append(
                f"  look-back {self.lookback_days} days: {self.out_of_scope} issues finished before it not checked, "
                f"{self.aged_out} findings on them aged out"
            )
        if self.settings_revision or self.settings_changed:
            customised = ", ".join(self.customised) or "none"
            lines.append(
                f"  board rule settings revision {self.settings_revision} (customised: {customised}): "
                f"{self.settings_changed} findings closed because the settings changed"
            )
        if self.untouched:
            lines.append(f"  {self.untouched} findings of disabled or not-measurable rules left as they were")
        return "\n".join(lines)


def finding_id(rule_id: str, rule_version: str, issue_key: str, evidence_key: str) -> str:
    """Stable id: the first 32 hex characters of sha256 of the four parts joined by U+001F."""
    return hashlib.sha256("\x1f".join((rule_id, rule_version, issue_key, evidence_key)).encode()).hexdigest()[:32]


def run_rules(
    store: Store,
    process: ProcessConfig,
    rules: RulesConfig,
    fields: FieldsConfig,
    *,
    projects: list[str],
    now: Callable[[], datetime] = utcnow,
) -> list[RulesResult]:
    """Evaluate every rule each board has on, with its settings (`rules`: the catalogue)."""
    try:
        projects = validate_project_keys(projects)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    for project in projects:
        process.project(project)  # ConfigError before any write
    moment = now()
    run = store.start_run("rules", rules_version=rules.version, started_at=moment)
    results: list[RulesResult] = []
    try:
        for project in projects:
            clock = time.perf_counter()
            with store.transaction():
                result = _run_project(store, process, rules, fields, project, moment, run)
            result.elapsed_s = time.perf_counter() - clock
            results.append(result)
            log.info(
                "rules_project_done", project=project, opened=result.opened, refreshed=result.refreshed,
                resolved=result.resolved, superseded=result.superseded, aged_out=result.aged_out,
                settings_changed=result.settings_changed, settings_revision=result.settings_revision,
                out_of_scope=result.out_of_scope, linked=result.linked, ignored=result.ignored,
                not_measurable=[s.rule_id for s in result.not_measurable],
            )
    except BaseException as exc:
        try:
            store.finish_run(
                run,
                status="failed",
                stats={"error": f"{type(exc).__name__}: {exc}", "projects_done": [r.project for r in results]},
                finished_at=now(),
            )
        except Exception:  # never let the bookkeeping hide the original error (as run_derive does)
            log.exception("rules_run_not_recorded", run_id=run.run_id)
        raise
    store.finish_run(run, status="succeeded", stats={r.project: _stats(r) for r in results}, finished_at=now())
    return results


def _run_project(
    store: Store,
    process: ProcessConfig,
    catalogue: RulesConfig,
    fields: FieldsConfig,
    project: str,
    moment: datetime,
    run: Run,
) -> RulesResult:
    settings = load_board_settings(store.conn, project)  # inside the board's transaction: no save slips in
    rules = effective_rules(catalogue, settings)
    ignores = active_ignores(store.conn, project)  # likewise: no ignore or restore slips in
    board = board_for(store, process, rules, fields, project, moment)
    now_iso = board.ctx.now_iso

    produced: set[str] = set()
    opened_breaches: list[tuple[str, Rule, str, str]] = []  # (finding id, rule, issue key, first seen)
    stats: list[RuleStats] = []
    for rule in rules.enabled_rules:
        version = rules.rule_version(rule)
        evaluation = evaluate(rule, board)
        if not evaluation.measurable:
            stats.append(RuleStats(rule.id, measurable=False, missing=evaluation.missing))
            _record_status(store, run, project, rule, version, measurable=False, missing=evaluation.missing)
            continue
        rule_stats = RuleStats(rule.id, measurable=True, checked=evaluation.checked, note=evaluation.note)
        for issue_key, hit in evaluation.hits:
            fid = finding_id(rule.id, version, issue_key, hit.evidence_key)
            produced.add(fid)
            ignored = ignores.covers(issue_key, (rule.id,)) is not None
            first_seen, opened = _upsert_finding(store, fid, rule, version, project, issue_key, hit, now_iso, run,
                                                 ignored=ignored)
            if ignored:
                rule_stats.ignored += 1
                continue
            rule_stats.hits += 1
            if opened:
                rule_stats.opened += 1
                if rule.kind == "rule":
                    opened_breaches.append((fid, rule, issue_key, first_seen))
            else:
                rule_stats.refreshed += 1
        stats.append(rule_stats)
        _record_status(store, run, project, rule, version, measurable=True, stats=rule_stats)

    measured = {s.rule_id for s in stats if s.measurable}
    resolution = Resolution.of(catalogue, rules, measured, board.aged, turned_off(store.conn, project),
                               settings.deleted_custom(catalogue))
    tally = _resolve(store, project, resolution, produced, now_iso)
    tally.update(_settle_ignored(store, project, resolution, produced, ignores, now_iso))
    linked = _link(store, rules, opened_breaches, now_iso)
    return RulesResult(
        run_id=run.run_id,
        project=project,
        rules_version=rules.version,
        stats=stats,
        opened=sum(s.opened for s in stats),
        refreshed=sum(s.refreshed for s in stats),
        resolved=tally["cleared"],
        superseded=tally["superseded"],
        linked=linked,
        untouched=tally["untouched"],
        elapsed_s=0.0,
        aged_out=tally["aged_out"],
        out_of_scope=len(board.aged),
        lookback_days=rules.defaults.lookback_days,
        settings_changed=tally["settings_changed"],
        settings_revision=settings.revision,
        customised=[rule.id for rule in rules.rules if rule.customised],
        ignored=sum(s.ignored for s in stats),
    )


# --- one board, one rule: what a run and a preview both evaluate ----------------------------------------------


@dataclass
class Board:
    ctx: _checks.RuleContext
    in_scope: list[Any]
    aged: set[str]
    without_metrics: int
    mapped: set[str]

    def missing(self, rule: Rule) -> list[str]:
        process, project = self.ctx.process, self.ctx.project
        missing = [e for e in rule.requires_evidence if not process.has_evidence(project, e)]
        missing += [s for s in rule.requires_stages if s not in self.mapped and s not in missing]
        return missing


def board_for(
    store: Store, process: ProcessConfig, rules: RulesConfig, fields: FieldsConfig, project: str, moment: datetime
) -> Board:
    """The board as `rules` (its own) are evaluated over it at `moment`: see Board."""
    now_iso = to_iso(moment)
    assert now_iso is not None
    ctx = _checks.RuleContext(
        store=store, process=process, fields=fields, rules=rules, project=project, now=moment, now_iso=now_iso
    )
    issues = ctx.issue_rows()
    aged = _out_of_scope(issues, rules.defaults.lookback_days, moment)
    with_metrics = [row for row in issues if row["has_metrics"]]
    return Board(
        ctx=ctx,
        in_scope=[row for row in with_metrics if row["issue_key"] not in aged],
        aged=aged,
        without_metrics=len(issues) - len(with_metrics),
        mapped=set(process.ordered_stages(project)),
    )


@dataclass
class Evaluation:
    rule: Rule
    missing: list[str]
    checked: int = 0
    hits: list[tuple[str, _checks.Hit]] = field(default_factory=list)
    note: str | None = None

    @property
    def measurable(self) -> bool:
        return not self.missing


def evaluate(rule: Rule, board: Board) -> Evaluation:
    missing = board.missing(rule)
    if missing:
        return Evaluation(rule, missing)
    ctx = board.ctx
    ctx.current_rule, ctx.identity_params = rule.id, rule.identity_params
    evaluation = Evaluation(rule, [])
    for issue_key, target in _targets(rule, ctx, board.in_scope, ctx.rules.defaults):
        evaluation.checked += 1
        evaluation.hits.extend((issue_key, hit) for hit in _hits(rule, target, ctx))
    if board.without_metrics:
        ctx.note(rule.id, f"{board.without_metrics} issues have no metrics: run derive")
    evaluation.note = ctx.notes.get(rule.id)
    return evaluation


def _out_of_scope(issues: list[Any], lookback_days: int | None, moment: datetime) -> set[str]:
    """Keys of the done issues finished before the look-back (none when it is off)."""
    if lookback_days is None:
        return set()
    cutoff = moment - timedelta(days=lookback_days)
    keys: set[str] = set()
    for row in issues:
        if row["status_category"] != "done":
            continue
        finished = [t for t in (_moment(row["done_at"]), _moment(row["resolved_at"])) if t is not None]
        if finished and max(finished) < cutoff:
            keys.add(row["issue_key"])
    return keys


def _moment(value: object) -> datetime | None:
    try:
        return parse_ts(value)
    except (TypeError, ValueError):  # unreadable: treated as unknown, which keeps the issue in scope
        return None


def _targets(
    rule: Rule, ctx: _checks.RuleContext, issues: list[Any], defaults: RuleDefaults
) -> list[tuple[str, Any]]:
    if rule.scope == "sprint":
        return [(f"{SPRINT_PREFIX}{sprint.sprint_id}", sprint) for sprint in ctx.active_sprints()]
    return [(row["issue_key"], row) for row in issues if _applies(rule, row, defaults)]


def _applies(rule: Rule, issue: Any, defaults: RuleDefaults) -> bool:
    issue_type = (issue["issue_type"] or "").casefold()
    applies = rule.applies_to
    if applies is not None and applies.issue_type is not None:
        if issue_type not in {t.casefold() for t in applies.issue_type}:
            return False
    elif issue_type in {t.casefold() for t in defaults.exclude_types}:
        return False
    if applies is not None and applies.status_category is not None:
        return issue["status_category"] in applies.status_category
    return True


def _hits(rule: Rule, target: Any, ctx: _checks.RuleContext) -> list[_checks.Hit]:
    # Looked up at call time, so tests can register checks of their own.
    registry: dict[str, Any] = _checks.SPRINT_CHECKS if rule.scope == "sprint" else _checks.CHECKS
    result = registry[rule.check](target, ctx, rule.params)
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


# --- writing what a run found ---------------------------------------------------------------------------------


def _upsert_finding(
    store: Store,
    fid: str,
    rule: Rule,
    version: str,
    project: str,
    issue_key: str,
    hit: _checks.Hit,
    now_iso: str,
    run: Run,
    *,
    ignored: bool = False,
) -> tuple[str, bool]:
    """Write the finding (resolved as ignored when an admin ignored it); its first_seen_at, and whether it opened."""
    existing = store.query_one(
        "SELECT first_seen_at, resolved_at, resolved_why FROM findings WHERE finding_id = ?", (fid,)
    )
    first_seen = existing["first_seen_at"] if existing is not None and existing["first_seen_at"] else now_iso
    resolved_at: str | None = None
    if ignored:
        kept = existing is not None and existing["resolved_why"] == IGNORED and existing["resolved_at"]
        resolved_at = existing["resolved_at"] if kept else now_iso
    store.upsert(
        "findings",
        {
            "finding_id": fid,
            "rule_id": rule.id,
            "rule_version": version,
            "issue_key": issue_key,
            "project_key": project,
            "kind": rule.kind,
            "severity": rule.severity,
            "evidence_key": hit.evidence_key,
            "message": hit.message,
            "evidence_json": hit.evidence,
            "first_seen_at": first_seen,
            "last_seen_at": now_iso,
            "resolved_at": resolved_at,
            "resolved_why": IGNORED if ignored else None,
            "run_id": run.run_id,
            "settings_hash": rule.settings_hash,
        },
    )
    opened = not ignored and (existing is None or existing["resolved_at"] is not None)
    return first_seen, opened


def _record_status(
    store: Store,
    run: Run,
    project: str,
    rule: Rule,
    version: str,
    *,
    measurable: bool,
    missing: list[str] | None = None,
    stats: RuleStats | None = None,
) -> None:
    store.upsert(
        "rule_status",
        {
            "run_id": run.run_id,
            "project_key": project,
            "rule_id": rule.id,
            "rule_version": version,
            "measurable": 1 if measurable else 0,
            "missing_json": missing or None,
            "issues_checked": stats.checked if stats else 0,
            "hits": stats.hits if stats else 0,
            "note": stats.note if stats else None,
            "settings_hash": rule.settings_hash,
        },
    )


# --- resolving what a run did not see -----------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    catalogue_version: str
    versions: Mapping[str, str]
    hashes: Mapping[str, str | None]
    measured: frozenset[str]
    switched_off: frozenset[str]
    aged: frozenset[str]
    deleted: frozenset[str] = frozenset()

    @classmethod
    def of(
        cls,
        catalogue: RulesConfig,
        rules: RulesConfig,
        measured: Iterable[str],
        aged: Iterable[str],
        board_off: Collection[str] = (),
        deleted: Iterable[str] = (),
    ) -> Resolution:
        """For a board's rules (`rules`, from `catalogue`) after the rules in `measured` ran."""
        on_by_default = {rule.id for rule in catalogue.rules if rule.enabled} | {
            rule.id for rule in rules.rules if rule.custom
        }
        return cls(
            catalogue_version=catalogue.version,
            versions={rule.id: rules.rule_version(rule) for rule in rules.rules},
            hashes={rule.id: rule.settings_hash for rule in rules.rules},
            measured=frozenset(measured),
            switched_off=frozenset(
                rule.id for rule in rules.rules
                if not rule.enabled and (rule.id in on_by_default or rule.id in board_off)
            ),
            aged=frozenset(aged),
            deleted=frozenset(deleted),
        )

    def outdated(self, row: Mapping[str, Any]) -> str | None:
        """Why a finding no longer stands for its rule as the board runs it: the rule deleted, or another version."""
        rule_id = row["rule_id"]
        if rule_id in self.deleted:
            return "settings_changed"
        if row["rule_version"] != self.versions.get(rule_id, self.catalogue_version):
            return "superseded"
        return None

    def why(self, row: Mapping[str, Any], produced: Collection[str]) -> str | None:
        """Why an open finding resolves."""
        rule_id = row["rule_id"]
        outdated = self.outdated(row)
        if outdated is not None:
            return outdated
        if row["finding_id"] in produced:
            return None
        if row["issue_key"] in self.aged:
            return "aged_out"
        if rule_id in self.switched_off:
            return "settings_changed"
        if rule_id in self.measured:
            return "settings_changed" if row["settings_hash"] != self.hashes.get(rule_id) else "cleared"
        return None


_OPEN_SQL = (
    "SELECT finding_id, rule_id, rule_version, issue_key, settings_hash FROM findings "
    "WHERE project_key = ? AND resolved_at IS NULL"
)


def _resolve(
    store: Store, project: str, resolution: Resolution, produced: set[str], now_iso: str
) -> Counter[str]:
    """Resolve what this run did not see; the count of each reason, and of the findings left untouched."""
    tally: Counter[str] = Counter({"cleared": 0, "superseded": 0, "aged_out": 0, "settings_changed": 0, "untouched": 0})
    for row in store.query(_OPEN_SQL, (project,)):
        why = resolution.why(row, produced)
        if why is None:
            if row["finding_id"] not in produced:
                tally["untouched"] += 1
            continue
        tally[why] += 1
        store.conn.execute(
            "UPDATE findings SET resolved_at = ?, resolved_why = ? WHERE finding_id = ?",
            (now_iso, why, row["finding_id"]),
        )
    return tally


_IGNORED_SQL = (
    "SELECT finding_id, rule_id, rule_version, issue_key, settings_hash FROM findings "
    "WHERE project_key = ? AND resolved_why = 'ignored'"
)


def _settle_ignored(
    store: Store, project: str, resolution: Resolution, produced: set[str], ignores: Ignores, now_iso: str
) -> Counter[str]:
    """Ignored findings: kept while an ignore holds them and their rule's version stands; else settled as open ones."""
    tally: Counter[str] = Counter()
    for row in store.query(_IGNORED_SQL, (project,)):
        if ignores.covers(row["issue_key"], (row["rule_id"],)) is not None:
            why = resolution.outdated(row)
            if why is None:
                continue
        else:
            # Its ignore was restored and the rules did not find it again (those found again reopened above).
            why = resolution.why(row, produced)
            if why is None:  # its rule did not run: open again, as it was before the ignore
                tally["untouched"] += 1
                store.conn.execute(
                    "UPDATE findings SET resolved_at = NULL, resolved_why = NULL WHERE finding_id = ?",
                    (row["finding_id"],),
                )
                continue
        tally[why] += 1
        store.conn.execute(
            "UPDATE findings SET resolved_at = ?, resolved_why = ? WHERE finding_id = ?",
            (now_iso, why, row["finding_id"]),
        )
    return tally


def _link(store: Store, rules: RulesConfig, opened: list[tuple[str, Rule, str, str]], now_iso: str) -> int:
    linked = 0
    for breach_id, rule, issue_key, first_seen in opened:
        warnings = rules.warnings_for(rule.id)
        if not warnings:
            continue
        either = " OR ".join("(rule_id = ? AND rule_version = ?)" for _ in warnings)
        found = store.query(
            f"SELECT finding_id FROM findings WHERE issue_key = ? AND first_seen_at <= ? AND ({either})",
            (issue_key, first_seen, *(value for w in warnings for value in (w.id, rules.rule_version(w)))),
        )
        for warning in found:
            pair = {"warning_finding_id": warning["finding_id"], "breach_finding_id": breach_id}
            # A breach that clears and reappears keeps its link, and the time the link was made.
            if store.count("warning_links", pair):
                continue
            store.upsert("warning_links", {**pair, "linked_at": now_iso})
            linked += 1
    return linked


def _stats(result: RulesResult) -> dict[str, Any]:
    return {
        "opened": result.opened,
        "refreshed": result.refreshed,
        "resolved": result.resolved,
        "superseded": result.superseded,
        "aged_out": result.aged_out,
        "settings_changed": result.settings_changed,
        "settings_revision": result.settings_revision,
        "out_of_scope": result.out_of_scope,
        "lookback_days": result.lookback_days,
        "linked": result.linked,
        "untouched": result.untouched,
        "ignored": result.ignored,
        "not_measurable": {s.rule_id: s.missing for s in result.not_measurable},
        "elapsed_s": round(result.elapsed_s, 3),
    }


# --- previewing other settings ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Impact:
    rule_id: str
    measurable: bool
    enabled: bool
    open_now: int
    would_open: int
    would_close: int
    would_stay: int
    open_examples: tuple[str, ...]
    close_examples: tuple[str, ...]
    pending: bool = False


_RULE_OPEN_SQL = (
    "SELECT finding_id, rule_id, rule_version, issue_key, settings_hash FROM findings "
    "WHERE project_key = ? AND rule_id = ? AND resolved_at IS NULL"
)
_RULE_HAS_OPEN_SQL = "SELECT 1 FROM findings WHERE project_key = ? AND resolved_at IS NULL AND rule_id = ? LIMIT 1"
_RULE_STATUS_SQL = "SELECT rule_id, rule_version, settings_hash FROM rule_status WHERE run_id = ? AND project_key = ?"


def preview(
    store: Store,
    process: ProcessConfig,
    rules: RulesConfig,
    fields: FieldsConfig,
    project: str,
    settings: BoardSettings,
    *,
    now: datetime,
) -> list[Impact]:
    saved = load_board_settings(store.conn, project)
    current = effective_rules(rules, saved)
    proposed = effective_rules(rules, settings)
    shown = _shown(store, project)
    changed: dict[str, tuple[Rule, bool]] = {}  # rule id -> (rule, pending)
    for rule in proposed.rules:
        before = _live(current, rule.id)
        if before is None or _settings_of(rule, proposed) != _settings_of(before, current):
            changed[rule.id] = (rule, False)
        elif shown is not None and _ran_as(rule, proposed) != shown.get(rule.id):
            changed[rule.id] = (rule, True)
    kept = {rule.id for rule in proposed.rules}
    gone = {rule.id: rule for rule in current.rules if rule.id not in kept}  # custom rules these settings delete
    still_deleted = set(settings.deleted_custom(rules))
    unclosed = {  # deleted before these settings (and still), findings still open: the re-check closes them
        rule_id for rule_id in saved.deleted_custom(rules)
        if rule_id in still_deleted and store.query_one(_RULE_HAS_OPEN_SQL, (project, rule_id)) is not None
    }
    if not changed and not gone and not unclosed:
        return []
    board = board_for(store, process, proposed, fields, project, now)
    ignores = active_ignores(store.conn, project)
    measured = {rule.id for rule in proposed.enabled_rules if not board.missing(rule)}
    # The rules the board would have turned off: those it turned off before, and those these settings turn off.
    board_off = turned_off(store.conn, project) | {
        rule.id for rule in proposed.rules
        if not rule.enabled and ((before := _live(current, rule.id)) is None or before.enabled)
    }
    resolution = Resolution.of(rules, proposed, measured, board.aged, board_off, settings.deleted_custom(rules))
    impacts = []
    # The catalogue's rules, then the board's custom rules in the order they were added (deleted ones too).
    order = dict.fromkeys([*(rule.id for rule in rules.rules), *(custom.rule_id for custom in settings.custom)])
    for rule_id in order:
        if rule_id in gone:
            impacts.append(_deleted_impact(store, project, gone[rule_id], board, resolution))
            continue
        if rule_id in unclosed:
            impact = _deleted_impact(store, project, current.find(rule_id), board, resolution, rule_id=rule_id,
                                     pending=True)
            if impact.would_close:
                impacts.append(impact)
            continue
        if rule_id not in changed:
            continue
        rule, pending = changed[rule_id]
        version = proposed.rule_version(rule)
        produced: dict[str, str] = {}  # finding id -> issue key
        if rule.id in measured:
            for issue_key, hit in evaluate(rule, board).hits:
                if ignores.covers(issue_key, (rule.id,)) is None:  # an ignored one is recorded ignored, never opened
                    produced[finding_id(rule.id, version, issue_key, hit.evidence_key)] = issue_key
        open_rows = store.query(_RULE_OPEN_SQL, (project, rule.id))
        closing = [row["issue_key"] for row in open_rows if resolution.why(row, produced) is not None]
        open_ids = {row["finding_id"] for row in open_rows}
        opening = [key for fid, key in produced.items() if fid not in open_ids]
        if pending and not (opening or closing):
            continue  # applying it changes no finding: nothing to say
        impacts.append(Impact(
            rule_id=rule.id,
            measurable=not board.missing(rule),
            enabled=rule.enabled,
            open_now=len(open_rows),
            would_open=len(opening),
            would_close=len(closing),
            would_stay=len(open_rows) - len(closing),
            open_examples=_examples(opening),
            close_examples=_examples(closing),
            pending=pending,
        ))
    return impacts


def _deleted_impact(
    store: Store, project: str, rule: Rule | None, board: Board, resolution: Resolution, *, rule_id: str | None = None,
    pending: bool = False,
) -> Impact:
    named = rule.id if rule is not None else str(rule_id)
    open_rows = store.query(_RULE_OPEN_SQL, (project, named))
    closing = [row["issue_key"] for row in open_rows if resolution.why(row, ()) is not None]
    return Impact(
        rule_id=named,
        measurable=rule is None or not board.missing(rule),
        enabled=False,
        open_now=len(open_rows),
        would_open=0,
        would_close=len(closing),
        would_stay=len(open_rows) - len(closing),
        open_examples=(),
        close_examples=_examples(closing),
        pending=pending,
    )


def _live(rules: RulesConfig, rule_id: str) -> Rule | None:
    return next((rule for rule in rules.rules if rule.id == rule_id), None)


def _settings_of(rule: Rule, rules: RulesConfig) -> tuple[bool, str, str | None]:
    """What a board's settings decide of a rule: on or off, its rule_version (its severity) and its params."""
    return rule.enabled, rules.rule_version(rule), rule.settings_hash


def _ran_as(rule: Rule, rules: RulesConfig) -> tuple[str, str | None] | None:
    return (rules.rule_version(rule), rule.settings_hash) if rule.enabled else None


def _shown(store: Store, project: str) -> dict[str, tuple[str, str | None]] | None:
    run = showing(recent_runs(store.conn, project))
    if run is None:
        return None
    return {row[0]: (row[1], row[2]) for row in store.query(_RULE_STATUS_SQL, (run.run_id, project))}


def _examples(keys: Iterable[str]) -> tuple[str, ...]:
    """Up to EXAMPLES distinct keys: issues by project and number, then sprints by id."""
    return tuple(sorted(set(keys), key=_key_order)[:EXAMPLES])


def _key_order(key: str) -> tuple[int, str, int, str]:
    if key.startswith(SPRINT_PREFIX):
        number = key[len(SPRINT_PREFIX):]
        return 1, "", int(number) if number.isdigit() else 0, key
    found = _KEY.fullmatch(key)
    return (0, found.group(1), int(found.group(2)), key) if found else (0, key, 0, key)
