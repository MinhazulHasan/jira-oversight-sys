"""The rule engine: evaluate the catalogue, open and resolve findings (Phase 3),
record what could not be measured, and link warnings to the breaches they
foreshadowed (Phase 4), each board as its rule settings say (JO-15).

run_rules(store, process, rules, fields, *, projects, now=utcnow) -> list[RulesResult]
`rules` is the catalogue (config/rules.yaml). One run record (kind "rules",
rules_version=rules.version, the catalogue's) for the whole call; each project
is processed inside one store.transaction(). Per project:

0. The board's rules: its rule settings are read from the store at the start of
   its transaction (oversight/rules/settings.py) and applied to the catalogue
   (effective_rules), so every caller (the CLI's rules, the cycle, "Sync now",
   the re-check after a save, the eval, the demo) runs each board as its admins
   set it, with no change of its own, the rules they added to it (custom rules,
   JO-20) after the catalogue's. A board with no settings runs the catalogue
   itself, exactly as before. Each rule's findings carry its rule_version (the
   catalogue's version, or "<version>+<severity>" where the board changed its
   severity; a custom rule's "c<revision that created it>", "+<severity>" the
   same way) and its settings_hash (None while its params are the catalogue's;
   a custom rule's covers its params and its scope).
1. The board (board_for): a RuleContext (checks.py) and the issues in scope.
   Issues come from `issues` LEFT JOIN `issue_metrics`; an issue with no
   metrics row is skipped, with one note on every rule ("<n> issues have no
   metrics: run derive").
   The look-back (rules defaults.lookback_days, when set): a done issue
   (status category done) finished before now - lookback_days is out of scope
   and no rule checks it. Finished is the later of issue_metrics.done_at and
   resolved_at; a done issue with neither stays in scope, and open work is
   always in scope. A condition on long-finished work can never clear, so
   without this its findings would stay open for ever.
2. For each rule on the board, evaluate(), which writes nothing: the one
   place a rule is evaluated, for a run and for a preview of new settings alike.
   - missing = evidence the rule needs that process.yaml leaves null, plus
     stages it needs that the project's workflow doesn't map. If any is
     missing, rule_status records the rule as not measurable (measurable 0,
     missing named) and the rule is skipped: "not measurable" is recorded,
     never silent.
   - Issues are selected by applies_to (status_category, issue_type, case-
     insensitive); defaults.exclude_types are excluded unless the rule's
     applies_to.issue_type names them. Sprint-scoped rules iterate the active
     sprints instead, under issue_key "sprint:<id>". Each is checked with the
     board's params; the check names what it finds (the evidence key) by the
     catalogue's (Rule.identity_params), so a ticket over both an old and a new
     threshold stays the same finding.
   Then each Hit upserts a finding (finding_id from rule id, the rule's
   rule_version, issue key and evidence key), keeping first_seen_at and
   setting last_seen_at, run_id, message, evidence, severity, settings_hash
   and an open state. A finding is "opened" when it had no row or its row was
   resolved, else "refreshed". rule_status records measurable 1, issues
   checked, hits, the rule's note and its settings_hash.
3. Resolve (Resolution.why): an open finding this run did not produce is
   resolved as
   - "superseded": its rule_version is not its rule's now (a catalogue version
     bump, or a severity changed on the board), so old and new never mix;
   - "aged_out": the look-back put its issue out of scope. Never "cleared":
     nothing was fixed, so the digest (which reports only cleared findings)
     leaves it out;
   - "settings_changed": its rule is off by the board's settings (rules.yaml has
     it on, or the board turned on a rule rules.yaml has off and then off again:
     settings.turned_off; a custom rule is on as created), its rule is a custom
     rule the board deleted (JO-20: whatever its rule_version, checked first),
     or its rule ran and the finding was last seen under other params (its
     settings_hash differs from the rule's now, NULL as NULL; a custom rule's
     covers its scope too). Never "cleared": when the data and the settings
     both changed, nothing is claimed as fixed;
   - "cleared": its rule ran, under the same params, and no longer finds it.
   Findings of rules off in rules.yaml itself that the board never turned on,
   or not measurable, are left as they are, and counted as untouched. Turning a
   rule back on, or a threshold back, reopens the same ids (the upsert keeps
   first_seen_at), and so does restoring a deleted custom rule (its id and its
   rule_version are the same).
4. Link: every breach opened in this run whose rule has warnings
   (rules.warnings_for) is linked to that issue's findings of those warning
   rules, each at its own rule_version, first seen no later than it ("warned
   on X, breached on Y"). A link is written once and keeps its linked_at.
5. The run is finished with per-project stats, among them settings_revision
   (the board's settings revision the run applied; 0: none) and
   settings_changed; on an exception it is marked failed and the exception
   re-raised (the project's transaction rolled back, so no partial findings
   remain).

preview(store, process, rules, fields, project, settings, *, now) -> list[Impact]
What a re-check with other settings would do to the findings of each rule
whose settings they change, on the board's data now: the rules evaluated as a
run evaluates them and the open findings resolved as a run would resolve them,
with nothing written (the API passes a store on a read-only connection). The
re-check applies every saved setting, so a rule whose saved settings have not
reached the findings yet (a re-check that failed, or has not run: what the run
the findings show recorded in rule_status differs) is in the preview too, when
the re-check would open or close anything of it, marked pending. A custom rule
the settings add (or restore) is in it with every finding it would raise now;
one they delete with every open finding it would close; and one a saved change
deleted whose findings are still open (its re-check failed, or has not run),
with those it would close, marked pending.
"""
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
    # Findings resolved as "settings_changed" (JO-15), the board's settings revision the run applied (0: none) and the
    # rules those settings change on it, in catalogue order.
    settings_changed: int = 0
    settings_revision: int = 0
    customised: list[str] = field(default_factory=list)

    @property
    def not_measurable(self) -> list[RuleStats]:
        return [s for s in self.stats if not s.measurable]

    def render(self) -> str:
        """Human summary: version, counts, one line per rule (hits, or 'not measurable: <missing>', or the note),
        then the look-back (when set), the board's rule settings (when it has any) and the findings left untouched."""
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
    """Evaluate every rule each board has on, with its settings (`rules`: the catalogue). See the module docstring."""
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
                out_of_scope=result.out_of_scope, linked=result.linked,
                not_measurable=[s.rule_id for s in result.not_measurable],
            )
    except BaseException as exc:
        # finished_at from the same clock as started_at (as every other run does), so a run
        # under an injected clock never "finishes" at the wall-clock time, before it started.
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
            rule_stats.hits += 1
            fid = finding_id(rule.id, version, issue_key, hit.evidence_key)
            produced.add(fid)
            first_seen, opened = _upsert_finding(store, fid, rule, version, project, issue_key, hit, now_iso, run)
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
    )


# --- one board, one rule: what a run and a preview both evaluate ----------------------------------------------


@dataclass
class Board:
    """One board as its rules are evaluated over it, read once for every rule: the checks' context (its rules are the
    board's), the issues in scope (with metrics, the look-back applied), the keys of those the look-back left out, how
    many issues have no metrics yet, and the stages its workflow maps."""

    ctx: _checks.RuleContext
    in_scope: list[Any]
    aged: set[str]
    without_metrics: int
    mapped: set[str]

    def missing(self, rule: Rule) -> list[str]:
        """What the rule needs that the board does not record: evidence process.yaml leaves null, then stages its
        workflow does not map. Any of it makes the rule not measurable (oversight.ask.catalog.rule_missing too)."""
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
    """What one rule finds on one board: the targets it checked and its hits, by issue key ("sprint:<id>" for a
    sprint), and its note; nothing written. Not measurable when it misses evidence or stages, with them named."""

    rule: Rule
    missing: list[str]
    checked: int = 0
    hits: list[tuple[str, _checks.Hit]] = field(default_factory=list)
    note: str | None = None

    @property
    def measurable(self) -> bool:
        return not self.missing


def evaluate(rule: Rule, board: Board) -> Evaluation:
    """One rule over one board, writing nothing: the one place a rule is evaluated, by a run and by a preview of new
    settings alike. A rule that is not measurable checks nothing. Otherwise every target in scope (the issues
    applies_to and the excluded types select, or the active sprints) is checked with the rule's params, its findings
    named by its identity params (the catalogue's), and the rule's note gathered (with the issues that have no metrics
    yet)."""
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
    """Keys of the done issues finished before the look-back (none when it is off).

    Finished is the later of done_at and resolved_at, so work finished recently by
    either account stays in scope; a done issue with neither (or with timestamps
    that do not parse) stays in scope, and so does all open work.
    """
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
) -> tuple[str, bool]:
    """Write the finding; return its first_seen_at and whether this run opened it."""
    existing = store.query_one("SELECT first_seen_at, resolved_at FROM findings WHERE finding_id = ?", (fid,))
    first_seen = existing["first_seen_at"] if existing is not None and existing["first_seen_at"] else now_iso
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
            "resolved_at": None,
            "resolved_why": None,
            "run_id": run.run_id,
            "settings_hash": rule.settings_hash,
        },
    )
    opened = existing is None or existing["resolved_at"] is not None
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
    """How a board's rules stand after a run (or would after a re-check), to say why an open finding the run did not
    produce resolves: each rule's rule_version and settings_hash now, the rules that ran and could measure, the rules
    the board's settings turn off (rules.yaml has them on, or the board turned them on and off again; a custom rule is
    on as created), the custom rules the board deleted, and the issues the look-back put out of scope."""

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
        """For a board's rules (`rules`, from `catalogue`) after the rules in `measured` ran. `board_off`: the rules the
        board itself turned off (settings.turned_off), which a rule rules.yaml has off is only once the board turned it
        on. `deleted`: the custom rules the board deleted (JO-20)."""
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

    def why(self, row: Mapping[str, Any], produced: Collection[str]) -> str | None:
        """Why an open finding (its finding_id, rule_id, rule_version, issue_key and settings_hash) resolves:
        superseded, aged_out, settings_changed or cleared (see the module docstring); None when it stays open, as
        produced again or untouched."""
        rule_id = row["rule_id"]
        if rule_id in self.deleted:
            return "settings_changed"
        if row["rule_version"] != self.versions.get(rule_id, self.catalogue_version):
            return "superseded"
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


def _link(store: Store, rules: RulesConfig, opened: list[tuple[str, Rule, str, str]], now_iso: str) -> int:
    linked = 0
    for breach_id, rule, issue_key, first_seen in opened:
        warnings = rules.warnings_for(rule.id)
        if not warnings:
            continue
        # Each warning rule at its own rule_version on the board: versions never mix, and after a version bump (or a
        # severity change) every reopened breach would otherwise be linked again to its old warnings.
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
        "not_measurable": {s.rule_id: s.missing for s in result.not_measurable},
        "elapsed_s": round(result.elapsed_s, 3),
    }


# --- previewing other settings ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Impact:
    """What a re-check with other settings would do to one rule's open findings on a board: how many are open now,
    how many would open (new ids, or resolved ones reopening), close (settings_changed, superseded by a severity
    change, or cleared or aged out by the data since the last run) and stay open (the same ids), with up to EXAMPLES
    issue keys ("sprint:<id>" for a sprint) of those that would open and close. Measurable: the rule's evidence and
    stages are all there on the board. Pending: the settings leave the rule as saved, but its findings do not reflect
    the settings it runs with yet (an earlier change whose re-check failed or never ran), so the re-check applies them
    too."""

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
    """What a re-check of the board with `settings` would do, rule by rule (catalogue order, then the custom rules in
    the order they were added), against the board's data now: for each rule whose settings they change (a custom rule
    they add, restore or delete too), each rule they leave as saved whose saved settings have not reached its findings
    yet (pending) and that the re-check would open or close findings of, and each custom rule a saved change deleted
    whose findings are still open (pending: the re-check closes them, as it closes every finding of a deleted rule).
    evaluate() as a run evaluates, Resolution.why as a run resolves, nothing written. `rules` is the catalogue; `store`
    may sit on a read-only connection."""
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
    """A custom rule the settings delete (pending: one a saved change deleted, its findings not closed yet): every open
    finding of it closes (settings_changed), nothing opens. `rule` is None for a deleted rule whose row no longer fits
    the registry (it is named by `rule_id`)."""
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
    """One of the board's rules by id; None for a rule it does not run (one a change adds, or one it deleted)."""
    return next((rule for rule in rules.rules if rule.id == rule_id), None)


def _settings_of(rule: Rule, rules: RulesConfig) -> tuple[bool, str, str | None]:
    """What a board's settings decide of a rule: on or off, its rule_version (its severity) and its params."""
    return rule.enabled, rules.rule_version(rule), rule.settings_hash


def _ran_as(rule: Rule, rules: RulesConfig) -> tuple[str, str | None] | None:
    """How a run of the board runs the rule, as rule_status records it: its rule_version and settings_hash; None when
    it is off, so not run."""
    return (rules.rule_version(rule), rule.settings_hash) if rule.enabled else None


def _shown(store: Store, project: str) -> dict[str, tuple[str, str | None]] | None:
    """Each rule as the board's findings show it: the rule_version and settings_hash the run whose settings they show
    (settings.showing) ran it with, by rule id, a rule it did not run left out; None before the board's first rules
    run, when there is nothing for saved settings to have reached."""
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
