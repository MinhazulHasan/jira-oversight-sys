from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from oversight.config import FieldsConfig, ProcessConfig
from oversight.rules.engine import Impact, RulesResult, RuleStats, finding_id, preview
from oversight.rules.settings import (
    BoardSettings,
    Change,
    SavedChange,
    load_board_settings,
    plan_changes,
    save_board_settings,
    settings_hash,
)
from oversight.rules.catalog import RulesConfig
from oversight.store.db import Store, open_readonly
from tests.rules_helpers import (
    NOW,
    add_history,
    add_issue,
    at,
    catalogue,
    context,
    derive,
    hits,
    run_at,
    sprint,
    with_project,
)

LATER = NOW + timedelta(hours=1)
STARTED = [(-48.0, "To Do", "In Progress")]
FINISHED = [*STARTED, (-2.0, "In Progress", "Done")]


def _set(store: Store, *changes: Change, project: str = "AIL", rules: RulesConfig | None = None) -> SavedChange:
    revision = load_board_settings(store.conn, project).revision
    return save_board_settings(store, rules or catalogue(), project, revision=revision, changes=list(changes), now=NOW,
                               viewer_hash="local", viewer_name="Local (sign-in off)")


def _findings(store: Store, rule_id: str) -> list[sqlite3.Row]:
    return store.query("SELECT * FROM findings WHERE rule_id = ? ORDER BY issue_key, rule_version", (rule_id,))


def _by_issue(store: Store, rule_id: str) -> dict[str, sqlite3.Row]:
    return {row["issue_key"]: row for row in _findings(store, rule_id)}


def _stat(result: RulesResult, rule_id: str) -> RuleStats:
    return next(s for s in result.stats if s.rule_id == rule_id)


def _efforts(store: Store, spent: dict[str, float]) -> None:
    for key, hours in spent.items():
        add_issue(store, key, status="Done", changes=FINISHED, points=1, spent_h=hours)


def _stats(store: Store, result: RulesResult) -> dict[str, Any]:
    row = store.query_one("SELECT stats_json FROM runs WHERE run_id = ?", (result.run_id,))
    assert row is not None
    stats: dict[str, Any] = json.loads(row["stats_json"])["AIL"]
    return stats


def test_a_threshold_change_keeps_the_findings_still_over_it_and_closes_the_rest_as_settings_changed(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _efforts(store, {"AIL-1": 16, "AIL-2": 13, "AIL-3": 24})
    derive(store, process)
    run_at(store, process, fields)
    before = _by_issue(store, "R-EFF-01")
    assert sorted(before) == ["AIL-1", "AIL-2", "AIL-3"]
    assert {row["evidence_key"] for row in before.values()} == {"ratio-ge-1.5"}

    _set(store, Change("R-EFF-01", params={"threshold": 1.8}))
    [result] = run_at(store, process, fields, now=LATER)

    after = _by_issue(store, "R-EFF-01")
    assert len(_findings(store, "R-EFF-01")) == 3
    fingerprint = settings_hash({"threshold": 1.8})
    for key in ("AIL-1", "AIL-3"):
        row = after[key]
        assert (row["finding_id"], row["first_seen_at"], row["last_seen_at"], row["resolved_at"]) == (
            before[key]["finding_id"], at(0), at(1), None)
        assert row["settings_hash"] == fingerprint
        assert json.loads(row["evidence_json"])["threshold"] == 1.8 and "threshold 1.8×" in row["message"]
    assert (after["AIL-2"]["resolved_at"], after["AIL-2"]["resolved_why"]) == (at(1), "settings_changed")
    assert (result.settings_changed, result.resolved, result.settings_revision, result.customised) == (
        1, 0, 1, ["R-EFF-01"])
    assert (_stat(result, "R-EFF-01").opened, _stat(result, "R-EFF-01").refreshed) == (0, 2)
    stats = _stats(store, result)
    assert (stats["settings_revision"], stats["settings_changed"], stats["resolved"]) == (1, 1, 0)
    status = store.query_one(
        "SELECT rule_version, settings_hash FROM rule_status WHERE run_id = ? AND rule_id = 'R-EFF-01'", (result.run_id,))
    assert status is not None and tuple(status) == ("0.1.0", fingerprint)
    assert ("  board rule settings revision 1 (customised: R-EFF-01): 1 findings closed because the settings changed"
            in result.render().splitlines())

    _set(store, Change("R-EFF-01", reset=True))
    [back] = run_at(store, process, fields, now=NOW + timedelta(hours=2))
    reopened = _by_issue(store, "R-EFF-01")
    assert reopened["AIL-2"]["finding_id"] == before["AIL-2"]["finding_id"]
    assert (reopened["AIL-2"]["first_seen_at"], reopened["AIL-2"]["resolved_at"], reopened["AIL-2"]["resolved_why"]) == (
        at(0), None, None)
    assert {row["settings_hash"] for row in reopened.values()} == {None}
    assert (_stat(back, "R-EFF-01").opened, back.settings_changed, back.settings_revision, back.customised) == (
        1, 0, 2, [])


def test_when_the_data_and_the_settings_both_change_nothing_is_claimed_as_fixed(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _efforts(store, {"AIL-1": 16, "AIL-2": 13})
    derive(store, process)
    run_at(store, process, fields)

    _set(store, Change("R-EFF-01", params={"threshold": 1.8}))
    _efforts(store, {"AIL-2": 6})
    derive(store, process)
    [both] = run_at(store, process, fields, now=LATER)
    assert _by_issue(store, "R-EFF-01")["AIL-2"]["resolved_why"] == "settings_changed"
    assert (both.settings_changed, both.resolved) == (1, 0)

    _efforts(store, {"AIL-1": 8})
    derive(store, process)
    [data] = run_at(store, process, fields, now=NOW + timedelta(hours=2))
    assert _by_issue(store, "R-EFF-01")["AIL-1"]["resolved_why"] == "cleared"
    assert (data.settings_changed, data.resolved) == (0, 1)


def test_a_finding_is_named_by_the_catalogues_threshold_and_compared_with_the_boards(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="Done", changes=FINISHED, points=1, spent_h=24)
    derive(store, process)
    ctx = context(store, process, fields)
    ctx.identity_params = {"threshold": 1.5}

    [hit] = hits(ctx, "effort_ratio_at_least", "AIL-1", threshold=2.0)
    assert (hit.evidence_key, hit.evidence["threshold"]) == ("ratio-ge-1.5", 2.0)
    assert "threshold 2×" in hit.message
    assert hits(ctx, "effort_ratio_at_least", "AIL-1", threshold=3.5) == []
    ctx.identity_params = None
    assert [h.evidence_key for h in hits(ctx, "effort_ratio_at_least", "AIL-1", threshold=2.0)] == ["ratio-ge-2"]


def test_a_no_evidence_finding_is_named_by_the_catalogues_percentile(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    reviewed = with_project(process, evidence={"code_review": {"label": "code-reviewed"}})
    add_history(store, "In Progress", [10.0] * 20)
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, points=1)
    derive(store, reviewed)
    ctx = context(store, reviewed, fields)
    ctx.identity_params = {"stage": "development", "evidence": "code_review", "pct": 75}

    [hit] = hits(ctx, "no_evidence_after_percentile", "AIL-1", stage="development", evidence="code_review", pct=90)
    assert (hit.evidence_key, hit.evidence["pct"]) == ("no-code_review-after-p75", 90.0)


def test_turning_a_rule_off_closes_its_findings_and_turning_it_on_again_reopens_them(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, points=1, assignee=None)
    derive(store, process)
    run_at(store, process, fields)
    [first] = _findings(store, "R-HYG-01")

    _set(store, Change("R-HYG-01", enabled=False))
    [off] = run_at(store, process, fields, now=LATER)
    [closed] = _findings(store, "R-HYG-01")
    assert (closed["finding_id"], closed["resolved_at"], closed["resolved_why"]) == (
        first["finding_id"], at(1), "settings_changed")
    assert (off.settings_changed, off.untouched) == (1, 0)
    assert "R-HYG-01" not in {s.rule_id for s in off.stats}
    assert store.query_one("SELECT 1 FROM rule_status WHERE run_id = ? AND rule_id = 'R-HYG-01'", (off.run_id,)) is None

    _set(store, Change("R-HYG-01", enabled=True))
    [on] = run_at(store, process, fields, now=NOW + timedelta(hours=2))
    [again] = _findings(store, "R-HYG-01")
    assert (again["finding_id"], again["first_seen_at"], again["resolved_at"], again["resolved_why"]) == (
        first["finding_id"], at(0), None, None)
    assert _stat(on, "R-HYG-01").opened == 1


def test_a_rule_rules_yaml_has_off_closes_its_findings_once_the_board_turns_it_on_and_off_again(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, points=1, assignee=None)
    derive(store, process)
    run_at(store, process, fields)
    [first] = _findings(store, "R-HYG-01")
    stingy = catalogue(enabled=[rule.id for rule in catalogue().rules if rule.id != "R-HYG-01"])

    [quiet] = run_at(store, process, fields, stingy, now=LATER)
    [kept] = _findings(store, "R-HYG-01")
    assert (kept["resolved_at"], quiet.untouched, quiet.settings_changed) == (None, 1, 0)

    _set(store, Change("R-HYG-01", enabled=True), rules=stingy)
    [on] = run_at(store, process, fields, stingy, now=NOW + timedelta(hours=2))
    [refreshed] = _findings(store, "R-HYG-01")
    assert (refreshed["finding_id"], refreshed["last_seen_at"], refreshed["resolved_at"]) == (
        first["finding_id"], at(2), None)
    assert on.customised == ["R-HYG-01"]

    _set(store, Change("R-HYG-01", enabled=False), rules=stingy)
    assert store.count("rule_settings") == 0
    [off] = run_at(store, process, fields, stingy, now=NOW + timedelta(hours=3))
    [closed] = _findings(store, "R-HYG-01")
    assert (closed["finding_id"], closed["resolved_at"], closed["resolved_why"]) == (
        first["finding_id"], at(3), "settings_changed")
    assert (off.settings_changed, off.untouched, off.customised) == (1, 0, [])
    proposed = plan_changes(stingy, load_board_settings(store.conn, "AIL"), [Change("R-HYG-01", enabled=True)]).settings
    [back] = preview(store, process, stingy, fields, "AIL", proposed, now=NOW + timedelta(hours=4))
    assert (back.rule_id, back.open_now, back.would_open, back.open_examples) == ("R-HYG-01", 0, 1, ("AIL-1",))


def test_a_severity_change_supersedes_the_findings_with_new_ids_and_changing_it_back_reopens_the_originals(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, points=1, assignee=None)
    derive(store, process)
    run_at(store, process, fields)
    [original] = _findings(store, "R-HYG-01")
    assert (original["rule_version"], original["severity"]) == ("0.1.0", "breach")

    _set(store, Change("R-HYG-01", severity="warning"))
    [changed] = run_at(store, process, fields, now=LATER)
    old, new = _findings(store, "R-HYG-01")
    assert (old["finding_id"], old["resolved_at"], old["resolved_why"]) == (original["finding_id"], at(1), "superseded")
    assert new["finding_id"] == finding_id("R-HYG-01", "0.1.0+warning", "AIL-1", "unassigned") != original["finding_id"]
    assert (new["rule_version"], new["severity"], new["kind"], new["first_seen_at"], new["resolved_at"]) == (
        "0.1.0+warning", "warning", "rule", at(1), None)
    assert (changed.superseded, _stat(changed, "R-HYG-01").opened, changed.settings_changed) == (1, 1, 0)
    status = store.query_one("SELECT rule_version FROM rule_status WHERE run_id = ? AND rule_id = 'R-HYG-01'",
                             (changed.run_id,))
    assert status is not None and status["rule_version"] == "0.1.0+warning"

    _set(store, Change("R-HYG-01", reset=True))
    [back] = run_at(store, process, fields, now=NOW + timedelta(hours=2))
    old, new = _findings(store, "R-HYG-01")
    assert (old["finding_id"], old["first_seen_at"], old["resolved_at"], old["severity"]) == (
        original["finding_id"], at(0), None, "breach")
    assert (new["resolved_at"], new["resolved_why"]) == (at(2), "superseded")
    assert (back.superseded, _stat(back, "R-HYG-01").opened) == (1, 1)


def test_a_warning_is_linked_to_its_breach_at_the_warnings_own_rule_version(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _set(store, Change("W-EFF-01", severity="breach"))
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, points=1, spent_h=7)
    derive(store, process)
    run_at(store, process, fields)
    [warning] = _findings(store, "W-EFF-01")
    assert (warning["rule_version"], warning["severity"], warning["kind"]) == ("0.1.0+breach", "breach", "warning")

    add_issue(store, "AIL-1", status="Done", changes=[*STARTED, (1, "In Progress", "Done")], points=1, spent_h=16)
    derive(store, process, now=NOW + timedelta(hours=2))
    [breached] = run_at(store, process, fields, now=NOW + timedelta(hours=2))
    [breach] = _findings(store, "R-EFF-01")
    assert breached.linked == 1
    assert [tuple(row) for row in store.query("SELECT warning_finding_id, breach_finding_id FROM warning_links")] == [
        (warning["finding_id"], breach["finding_id"])]


def test_a_board_with_no_settings_runs_exactly_as_before_whatever_another_board_has(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _set(store, Change("R-HYG-01", enabled=False), Change("R-EFF-01", params={"threshold": 3}), project="DEMO")
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, assignee=None)
    _efforts(store, {"AIL-2": 16})
    derive(store, process)

    [result] = run_at(store, process, fields)
    [again] = run_at(store, process, fields, now=LATER)

    rows = store.query("SELECT * FROM findings")
    assert {row["rule_id"] for row in rows} >= {"R-HYG-01", "R-EFF-01"}
    for row in rows:
        assert row["finding_id"] == finding_id(row["rule_id"], "0.1.0", row["issue_key"], row["evidence_key"])
        assert (row["rule_version"], row["settings_hash"], row["resolved_at"]) == ("0.1.0", None, None)
    assert {row["settings_hash"] for row in store.query("SELECT settings_hash FROM rule_status")} == {None}
    assert (result.settings_revision, result.settings_changed, result.customised) == (0, 0, [])
    assert (again.opened, again.resolved, again.superseded, again.settings_changed) == (0, 0, 0, 0)
    assert "board rule settings" not in result.render()


def test_a_preview_says_what_the_recheck_then_does_and_writes_nothing(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _efforts(store, {"AIL-1": 16, "AIL-2": 13, "AIL-3": 24})
    add_issue(store, "AIL-4", status="In Progress", changes=STARTED, points=1, assignee=None)
    add_issue(store, "AIL-5", status="In Progress", changes=STARTED, points=1, spent_h=7)
    derive(store, process)
    run_at(store, process, fields)
    changes = [Change("R-EFF-01", params={"threshold": 1.8}), Change("R-HYG-01", severity="warning"),
               Change("W-EFF-01", enabled=False), Change("R-FLOW-01", severity="warning")]
    proposed = plan_changes(catalogue(), load_board_settings(store.conn, "AIL"), changes).settings
    before = [tuple(row) for row in store.query("SELECT * FROM findings ORDER BY finding_id")]

    assert store.path is not None
    conn = open_readonly(store.path)
    try:
        impacts = preview(Store(conn), process, catalogue(), fields, "AIL", proposed, now=LATER)
    finally:
        conn.close()

    assert impacts == [
        Impact("R-EFF-01", measurable=True, enabled=True, open_now=3, would_open=0, would_close=1, would_stay=2,
               open_examples=(), close_examples=("AIL-2",)),
        Impact("R-FLOW-01", measurable=False, enabled=True, open_now=0, would_open=0, would_close=0, would_stay=0,
               open_examples=(), close_examples=()),
        Impact("R-HYG-01", measurable=True, enabled=True, open_now=1, would_open=1, would_close=1, would_stay=0,
               open_examples=("AIL-4",), close_examples=("AIL-4",)),
        Impact("W-EFF-01", measurable=True, enabled=False, open_now=1, would_open=0, would_close=1, would_stay=0,
               open_examples=(), close_examples=("AIL-5",)),
    ]
    assert [tuple(row) for row in store.query("SELECT * FROM findings ORDER BY finding_id")] == before
    assert store.count("rule_settings") == 0

    _set(store, *changes)
    [result] = run_at(store, process, fields, now=LATER)
    assert (result.settings_changed, result.superseded, _stat(result, "R-HYG-01").opened) == (2, 1, 1)
    assert _stat(result, "R-EFF-01").opened == 0


def test_a_preview_of_settings_that_change_nothing_is_empty(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _set(store, Change("R-EFF-01", params={"threshold": 1.8}))
    current = load_board_settings(store.conn, "AIL")
    assert preview(store, process, catalogue(), fields, "AIL", current, now=NOW) == []
    assert preview(store, process, catalogue(), fields, "AIL", BoardSettings("AIL"), now=NOW)[0].rule_id == "R-EFF-01"


def test_a_preview_counts_saved_settings_the_findings_do_not_show_yet(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _efforts(store, {"AIL-1": 16, "AIL-2": 13, "AIL-3": 24})
    add_issue(store, "AIL-4", status="In Progress", changes=STARTED, points=1, assignee=None)
    derive(store, process)
    run_at(store, process, fields)
    _set(store, Change("R-EFF-01", params={"threshold": 1.8}))
    _set(store, Change("W-AGE-01", params={"pct": 80}))
    changes = [Change("R-HYG-01", enabled=False)]
    proposed = plan_changes(catalogue(), load_board_settings(store.conn, "AIL"), changes).settings

    impacts = preview(store, process, catalogue(), fields, "AIL", proposed, now=LATER)

    assert impacts == [
        Impact("R-EFF-01", measurable=True, enabled=True, open_now=3, would_open=0, would_close=1, would_stay=2,
               open_examples=(), close_examples=("AIL-2",), pending=True),
        Impact("R-HYG-01", measurable=True, enabled=False, open_now=1, would_open=0, would_close=1, would_stay=0,
               open_examples=(), close_examples=("AIL-4",)),
    ]
    _set(store, *changes)
    [result] = run_at(store, process, fields, now=LATER)
    assert result.settings_changed == sum(impact.would_close for impact in impacts) == 2
    later = plan_changes(catalogue(), load_board_settings(store.conn, "AIL"), [Change("W-SPR-01", enabled=False)])
    assert [impact.rule_id for impact in preview(store, process, catalogue(), fields, "AIL", later.settings,
                                                 now=LATER)] == ["W-SPR-01"]


def test_a_preview_names_sprints_by_their_key_in_number_order(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    for number, sprint_id in ((1, 12), (2, 7)):
        add_issue(store, f"AIL-{number}", sprints=[sprint(sprint_id, end=24.0)], points=1)
    derive(store, process)
    run_at(store, process, fields)
    assert sorted(row["issue_key"] for row in _findings(store, "W-SPR-01")) == ["sprint:12", "sprint:7"]
    proposed = plan_changes(catalogue(), load_board_settings(store.conn, "AIL"),
                            [Change("W-SPR-01", params={"days_left": 0.5})]).settings

    [risk] = preview(store, process, catalogue(), fields, "AIL", proposed, now=LATER)

    assert (risk.rule_id, risk.would_close, risk.close_examples) == ("W-SPR-01", 2, ("sprint:7", "sprint:12"))
    _set(store, Change("W-SPR-01", params={"days_left": 0.5}))
    run_at(store, process, fields, now=LATER)
    for row in _findings(store, "W-SPR-01"):
        evidence = json.loads(row["evidence_json"])
        assert (row["resolved_why"], evidence["threshold_days_left"], evidence["threshold_share"]) == (
            "settings_changed", 2.0, 0.3)
