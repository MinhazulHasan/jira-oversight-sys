"""Rule settings for admins (JO-15, oversight/rules/settings.py): what may change on a board and within which bounds,
the cross-rule rule, a board's rules with its settings applied, and how a change is validated and written, all or
nothing, with its audit trail.

The test catalogue (tests/fixtures/config/rules.yaml) and temporary stores only; the viewer is the local one.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from oversight.rules.catalog import RulesConfig
from oversight.rules.settings import (
    EDITABLE,
    BoardSettings,
    Change,
    Plan,
    RuleSetting,
    SavedChange,
    SettingsConflict,
    SettingsInvalid,
    board_rules,
    default_value,
    editable_params,
    effective_rules,
    load_board_settings,
    plan_changes,
    read_history,
    save_board_settings,
    settings_hash,
    turned_off,
)
from oversight.store.db import Store, open_readonly
from oversight.timeutil import to_iso
from tests.rules_helpers import NOW, catalogue
from tests.test_db import VERSION_8

CATALOGUE = catalogue()
LOCAL = {"viewer_hash": "local", "viewer_name": "Local (sign-in off)"}


def plan(*changes: Change, current: BoardSettings | None = None) -> Plan:
    return plan_changes(CATALOGUE, current or BoardSettings("AIL"), list(changes))


def problems(*changes: Change, current: BoardSettings | None = None) -> list[tuple[str | None, str | None, str]]:
    with pytest.raises(SettingsInvalid) as refused:
        plan(*changes, current=current)
    return [(problem.rule_id, problem.field, problem.message) for problem in refused.value.problems]


def save(store: Store, *changes: Change, revision: int | None = None, **options: Any) -> SavedChange:
    """Save as the API saves, from the board's revision unless told otherwise."""
    current = load_board_settings(store.conn, "AIL").revision if revision is None else revision
    return save_board_settings(store, CATALOGUE, "AIL", revision=current, changes=list(changes), now=NOW, **LOCAL,
                               **options)


# --- what may change, and within which bounds -------------------------------------------------------------------


def test_the_editable_params_and_their_bounds_are_the_specs() -> None:
    table = {(check, spec.name): (spec.unit, spec.min, spec.max, spec.step)
             for check, specs in EDITABLE.items() for spec in specs}
    assert table == {
        ("estimate_after_start", "grace_hours"): ("hours", 0, 168, 0.5),
        ("effort_ratio_at_least", "threshold"): ("ratio", 0.1, 10, 0.05),
        ("stuck_in_status", "pct"): ("percentile", 50, 99, 1),
        ("no_evidence_after_percentile", "pct"): ("percentile", 50, 99, 1),
        ("sprint_at_risk", "days_left"): ("days", 0, 14, 0.5),
        ("sprint_at_risk", "not_started_share"): ("share", 0.05, 0.95, 0.05),
    }
    assert all(spec.label and spec.label[0].isupper() for specs in EDITABLE.values() for spec in specs)


def test_each_rule_offers_its_numeric_params_and_never_a_structural_one() -> None:
    offered = {rule.id: [spec.name for spec in editable_params(rule)] for rule in CATALOGUE.rules}
    assert offered == {
        "R-EST-01": [], "R-EST-02": ["grace_hours"], "R-EFF-01": ["threshold"], "R-FLOW-01": [], "R-FLOW-02": [],
        "R-FLOW-03": [], "R-HYG-01": [], "W-EFF-01": ["threshold"], "W-AGE-01": ["pct"], "W-REV-01": ["pct"],
        "W-SPR-01": ["days_left", "not_started_share"], "W-EST-01": [],
    }


def test_a_param_rules_yaml_leaves_out_starts_from_the_checks_default() -> None:
    data = CATALOGUE.model_dump()
    for entry in data["rules"]:
        if entry["id"] in ("R-EST-02", "W-AGE-01", "W-REV-01", "W-SPR-01", "R-EFF-01"):
            entry["params"] = {key: value for key, value in entry["params"].items() if key in ("stage", "evidence")}
    bare = RulesConfig.model_validate(data)
    defaults = {rule.id: {spec.name: default_value(rule, spec) for spec in editable_params(rule)}
                for rule in bare.rules if editable_params(rule)}
    # R-EFF-01's check has no default threshold: with none in rules.yaml there is nothing to start from.
    assert defaults == {
        "R-EST-02": {"grace_hours": 1.0}, "W-EFF-01": {"threshold": 0.8}, "W-AGE-01": {"pct": 90.0},
        "W-REV-01": {"pct": 75.0}, "W-SPR-01": {"days_left": 2.0, "not_started_share": 0.3},
    }


@pytest.mark.parametrize(("rule_id", "name", "value"), [
    ("W-EFF-01", "threshold", 0.1), ("R-EFF-01", "threshold", 10), ("R-EFF-01", "threshold", 2.1234),
    ("R-EST-02", "grace_hours", 0), ("R-EST-02", "grace_hours", 168), ("W-AGE-01", "pct", 50),
    ("W-REV-01", "pct", 99), ("W-SPR-01", "days_left", 0), ("W-SPR-01", "days_left", 14),
    ("W-SPR-01", "not_started_share", 0.05), ("W-SPR-01", "not_started_share", 0.95),
])
def test_a_value_within_its_bounds_is_taken(rule_id: str, name: str, value: float) -> None:
    changed = plan(Change(rule_id, params={name: value}))
    assert [(change.rule_id, change.field, change.after) for change in changed.changes] == [
        (rule_id, f"params.{name}", float(value))]


@pytest.mark.parametrize(("rule_id", "name", "value", "message"), [
    ("R-EFF-01", "threshold", 10.05, "Effort ratio must be from 0.1 to 10."),
    ("W-EFF-01", "threshold", 0.05, "Effort ratio must be from 0.1 to 10."),
    ("R-EFF-01", "threshold", 10 ** 400, "Effort ratio must be from 0.1 to 10."),
    ("R-EST-02", "grace_hours", -0.5, "Grace after work started must be from 0 to 168 hours."),
    ("R-EST-02", "grace_hours", 168.5, "Grace after work started must be from 0 to 168 hours."),
    ("W-AGE-01", "pct", 49, "Percentile of time in the status must be from 50 to 99."),
    ("W-REV-01", "pct", 100, "Percentile of time in the stage must be from 50 to 99."),
    ("W-SPR-01", "days_left", 14.5, "Days left in the sprint must be from 0 to 14 days."),
    ("W-SPR-01", "not_started_share", 0.96, "Share of issues not started must be from 5% to 95%."),
    ("W-SPR-01", "not_started_share", 30, "Share of issues not started must be from 5% to 95%."),
    ("R-EFF-01", "threshold", "2", "Effort ratio must be a number."),
    ("R-EFF-01", "threshold", True, "Effort ratio must be a number."),
    ("R-EFF-01", "threshold", None, "Effort ratio must be a number."),
    ("R-EFF-01", "threshold", [2], "Effort ratio must be a number."),
    ("R-EFF-01", "threshold", math.nan, "Effort ratio must be a number."),
    ("R-EFF-01", "threshold", math.inf, "Effort ratio must be a number."),
    ("R-EFF-01", "threshold", 2.12345, "Effort ratio can have at most 4 decimal places."),
    ("R-EFF-01", "threshold", 0.00001, "Effort ratio must be from 0.1 to 10."),
])
def test_a_value_out_of_its_bounds_or_not_a_number_is_refused(rule_id: str, name: str, value: Any, message: str) -> None:
    assert problems(Change(rule_id, params={name: value})) == [(rule_id, f"params.{name}", message)]


def test_structural_params_and_params_a_rule_does_not_have_cannot_be_changed() -> None:
    assert problems(Change("W-REV-01", params={"stage": "testing"}), Change("R-FLOW-02", params={"threshold": 2})) == [
        ("W-REV-01", "params.stage", "W-REV-01 has no setting called stage that can be changed."),
        ("R-FLOW-02", "params.threshold", "R-FLOW-02 has no setting called threshold that can be changed."),
    ]


def test_a_change_set_is_refused_whole_with_every_problem_named() -> None:
    assert problems(
        Change("R-NOPE-01", enabled=False),
        Change("R-HYG-01", enabled=False),
        Change("R-HYG-01", severity="warning"),
        Change("R-EST-01", reset=True, enabled=False),
        Change("R-FLOW-02"),
        Change("R-FLOW-03", severity="warning"),  # already its severity
        Change("R-EFF-01", reset=True),  # nothing to reset
        Change("W-SPR-01", severity="critical", enabled="yes"),  # type: ignore[arg-type]
    ) == [
        ("R-NOPE-01", None, "R-NOPE-01 is not a rule in the catalogue."),
        ("R-HYG-01", None, "R-HYG-01 is named more than once: make one change per rule."),
        ("R-EST-01", "reset", "A reset cannot be combined with other changes to the same rule."),
        ("R-FLOW-02", None, "The change to R-FLOW-02 names nothing to change."),
        ("R-FLOW-03", None, "R-FLOW-03 already has these settings on this board."),
        ("R-EFF-01", None, "R-EFF-01 already has these settings on this board."),
        ("W-SPR-01", "enabled", "On or off must be true or false."),
        ("W-SPR-01", "severity", "Severity must be breach or warning."),
    ]
    with pytest.raises(SettingsInvalid, match="Name at least one change."):
        plan()


# --- the cross-rule rule ----------------------------------------------------------------------------------------


def test_a_warning_keeps_its_threshold_strictly_below_its_breach_rules() -> None:
    # Said on the field the change set moved, with the other rule's value in its unit, as the dashboard says it.
    assert problems(Change("W-EFF-01", params={"threshold": 1.5})) == [
        ("W-EFF-01", "params.threshold", "Must stay below R-EFF-01's 1.5× while both are on: the warning comes first.")]
    assert problems(Change("R-EFF-01", params={"threshold": 0.7})) == [
        ("R-EFF-01", "params.threshold", "Must stay above W-EFF-01's 0.8× while both are on.")]
    # Both changed: the warning is the one named.
    assert problems(Change("R-EFF-01", params={"threshold": 2}), Change("W-EFF-01", params={"threshold": 3})) == [
        ("W-EFF-01", "params.threshold", "Must stay below R-EFF-01's 2× while both are on: the warning comes first.")]
    # Moved together, or with the warning off (now or already): no conflict.
    assert plan(Change("R-EFF-01", params={"threshold": 0.7}), Change("W-EFF-01", params={"threshold": 0.5})).changes
    assert plan(Change("R-EFF-01", params={"threshold": 0.7}), Change("W-EFF-01", enabled=False)).changes
    off = plan(Change("W-EFF-01", enabled=False)).settings
    assert plan(Change("R-EFF-01", params={"threshold": 0.5}), current=off).changes
    # Turning the warning back on under a breach threshold below it is refused.
    lowered = plan(Change("R-EFF-01", params={"threshold": 0.5}), current=off).settings
    assert problems(Change("W-EFF-01", enabled=True), current=lowered) == [
        ("W-EFF-01", "params.threshold", "Must stay below R-EFF-01's 0.5× while both are on: the warning comes first.")]


@pytest.mark.parametrize(("check", "name", "value", "words"), [
    ("effort_ratio_at_least", "threshold", 1.25, "1.25×"),
    ("estimate_after_start", "grace_hours", 1.0, "1 h"),
    ("stuck_in_status", "pct", 90.0, "p90"),
    ("sprint_at_risk", "days_left", 1.0, "1 day"),
    ("sprint_at_risk", "days_left", 2.5, "2.5 days"),
    ("sprint_at_risk", "not_started_share", 0.35, "35%"),
])
def test_a_value_is_said_in_its_unit_as_the_dashboard_shows_it(check: str, name: str, value: float, words: str) -> None:
    [spec] = [spec for spec in EDITABLE[check] if spec.name == name]
    assert spec.words(value) == words


def test_an_unrelated_change_is_never_refused_for_a_pair_rules_yaml_got_wrong() -> None:
    data = CATALOGUE.model_dump()
    for entry in data["rules"]:
        if entry["id"] == "W-EFF-01":
            entry["params"]["threshold"] = 2.0  # above R-EFF-01's 1.5 in rules.yaml itself
    wrong = RulesConfig.model_validate(data)
    assert plan_changes(wrong, BoardSettings("AIL"), [Change("R-HYG-01", enabled=False)]).changes


# --- what a plan holds -------------------------------------------------------------------------------------------


def test_a_plan_names_each_field_it_changes_with_its_effective_values_before_and_after() -> None:
    current = plan(Change("R-EFF-01", params={"threshold": 2.0}), Change("W-AGE-01", enabled=False)).settings
    changed = plan(
        Change("W-SPR-01", severity="breach", params={"days_left": 3, "not_started_share": 0.3}),
        Change("R-EFF-01", reset=True),
        Change("R-EST-02", params={"grace_hours": 4.5}),
        current=current,
    )
    # Catalogue order; not_started_share is already 0.3, so it is no change.
    assert [change.as_dict() for change in changed.changes] == [
        {"rule_id": "R-EST-02", "field": "params.grace_hours", "from": 1.0, "to": 4.5},
        {"rule_id": "R-EFF-01", "field": "params.threshold", "from": 2.0, "to": 1.5},
        {"rule_id": "W-SPR-01", "field": "severity", "from": "warning", "to": "breach"},
        {"rule_id": "W-SPR-01", "field": "params.days_left", "from": 2.0, "to": 3.0},
    ]
    assert changed.rule_ids == ["R-EST-02", "R-EFF-01", "W-SPR-01"]
    # Only what differs from rules.yaml is kept: R-EFF-01, reset, keeps nothing.
    assert changed.settings.rules == (
        RuleSetting("R-EST-02", params=(("grace_hours", 4.5),)),
        RuleSetting("W-AGE-01", enabled=False),
        RuleSetting("W-SPR-01", severity="breach", params=(("days_left", 3.0),)),
    )


def test_a_field_set_back_to_rules_yaml_is_kept_as_nothing() -> None:
    current = plan(Change("R-HYG-01", enabled=False, severity="warning")).settings
    back = plan(Change("R-HYG-01", enabled=True), current=current)
    assert back.settings.rules == (RuleSetting("R-HYG-01", severity="warning"),)
    gone = plan(Change("R-HYG-01", severity="breach"), current=back.settings)
    assert gone.settings.rules == ()
    assert [change.as_dict() for change in gone.changes] == [
        {"rule_id": "R-HYG-01", "field": "severity", "from": "warning", "to": "breach"}]


# --- a board's rules ----------------------------------------------------------------------------------------------


def test_with_no_settings_a_board_runs_the_catalogue_itself() -> None:
    assert effective_rules(CATALOGUE, BoardSettings("AIL")) is CATALOGUE
    for rule in CATALOGUE.rules:
        assert (CATALOGUE.rule_version(rule), rule.settings_hash, rule.customised) == ("0.1.0", None, False)
        assert rule.identity_params is rule.params


def test_a_boards_rules_carry_what_its_settings_made_of_each_rule() -> None:
    settings = plan(Change("R-EFF-01", params={"threshold": 2.0}), Change("R-HYG-01", severity="warning"),
                    Change("W-AGE-01", enabled=False)).settings
    board = effective_rules(CATALOGUE, settings)
    assert board.is_board_rules and not CATALOGUE.is_board_rules and board.version == "0.1.0"
    effort, hygiene, stuck = board.by_id("R-EFF-01"), board.by_id("R-HYG-01"), board.by_id("W-AGE-01")
    assert (effort.params, effort.identity_params, board.rule_version(effort)) == (
        {"threshold": 2.0}, {"threshold": 1.5}, "0.1.0")
    assert effort.settings_hash == settings_hash({"threshold": 2.0})
    assert re.fullmatch(r"[0-9a-f]{12}", effort.settings_hash or "")
    assert (hygiene.severity, board.rule_version(hygiene), hygiene.settings_hash) == ("warning", "0.1.0+warning", None)
    assert (stuck.enabled, board.rule_version(stuck), stuck.settings_hash) == (False, "0.1.0", None)
    assert [rule.id for rule in board.rules if rule.customised] == ["R-EFF-01", "R-HYG-01", "W-AGE-01"]
    assert [rule.id for rule in board.enabled_rules] == [rule.id for rule in CATALOGUE.rules if rule.id != "W-AGE-01"]
    assert CATALOGUE.by_id("R-EFF-01").params == {"threshold": 1.5}  # the catalogue as it was
    assert effective_rules(CATALOGUE, settings) is board  # worked out once for each set of rows
    with pytest.raises(ValueError, match="not one board's rules"):
        effective_rules(board, settings)


def test_the_params_fingerprint_is_canonical() -> None:
    assert settings_hash({"threshold": 2.0}) == hashlib.sha256(b'{"threshold":2.0}').hexdigest()[:12]
    assert settings_hash({"days_left": 3, "not_started_share": 0.3}) == settings_hash(
        {"not_started_share": 0.3, "days_left": 3.0})
    assert settings_hash({"threshold": 2.0}) != settings_hash({"threshold": 2.05})


def test_the_catalogue_still_refuses_what_rules_yaml_cannot_say() -> None:
    data = CATALOGUE.model_dump()
    data["rules"][0]["settings_hash"] = "0123456789ab"
    with pytest.raises(ValidationError, match="settings_hash"):
        RulesConfig.model_validate(data)


def test_a_stored_setting_that_can_no_longer_apply_is_left_out_with_a_warning() -> None:
    stale = BoardSettings("AIL", 3, (
        RuleSetting("R-EFF-01", params=(("stage", 1.0), ("threshold", 50.0))),
        RuleSetting("R-GONE-01", enabled=False),
        RuleSetting("R-HYG-01", enabled=False),
    ))
    with capture_logs() as events:
        board = effective_rules(CATALOGUE, stale)
    assert board.by_id("R-EFF-01").params == {"threshold": 1.5} and not board.by_id("R-EFF-01").customised
    assert board.by_id("R-HYG-01").enabled is False
    ignored = sorted((event["rule"], event["setting"]) for event in events if event["event"] == "rule_setting_ignored")
    assert ignored == [("R-EFF-01", "params.stage"), ("R-EFF-01", "params.threshold"), ("R-GONE-01", "rule")]


# --- reading a board's settings ---------------------------------------------------------------------------------


def test_a_boards_settings_are_read_through_a_read_only_connection(store: Store, tmp_path: Path) -> None:
    save(store, Change("R-EFF-01", params={"threshold": 2.0}), Change("W-AGE-01", enabled=False))
    save(store, Change("R-HYG-01", severity="warning"))
    assert store.path is not None
    conn = open_readonly(store.path)
    try:
        settings = load_board_settings(conn, "AIL")
        other = load_board_settings(conn, "DEMO")
    finally:
        conn.close()
    assert settings == BoardSettings("AIL", 2, (
        RuleSetting("R-EFF-01", params=(("threshold", 2.0),)),
        RuleSetting("R-HYG-01", severity="warning"),
        RuleSetting("W-AGE-01", enabled=False),
    ))
    assert other == BoardSettings("DEMO")
    assert board_rules(store.path, CATALOGUE, "AIL").by_id("W-AGE-01").enabled is False
    assert board_rules(store.path, CATALOGUE, "DEMO") is CATALOGUE
    assert board_rules(tmp_path / "none.db", CATALOGUE, "AIL") is CATALOGUE  # no snapshot yet
    assert not (tmp_path / "none.db").exists()


def test_a_snapshot_from_before_version_9_has_no_settings(tmp_path: Path) -> None:
    path = tmp_path / "v8.sqlite"
    with Store.open(path) as store:
        store.conn.executescript(VERSION_8)
    conn = open_readonly(path)  # read-only: it never migrates
    try:
        assert load_board_settings(conn, "AIL") == BoardSettings("AIL")
        assert read_history(conn, "AIL") == []
    finally:
        conn.close()


def test_a_row_edited_by_hand_reads_as_rules_yaml_wherever_it_cannot_be_read(store: Store) -> None:
    store.conn.executemany(
        "INSERT INTO rule_settings (project_key, rule_id, enabled, severity, params_json, updated_at)"
        " VALUES ('AIL', ?, ?, ?, ?, '2026-11-17T12:00:00.000Z')",
        [("R-EFF-01", 7, "critical", "not json"), ("W-AGE-01", 0, None, '{"pct": "high", "days": true, "x": 95}')],
    )
    assert load_board_settings(store.conn, "AIL") == BoardSettings(
        "AIL", 0, (RuleSetting("W-AGE-01", enabled=False, params=(("x", 95.0),)),))


# --- writing a change ---------------------------------------------------------------------------------------------


def _rows(store: Store, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in store.query(sql)]


def test_a_save_writes_only_what_differs_and_records_the_change(store: Store) -> None:
    saved = save(store, Change("R-EFF-01", params={"threshold": 2}), Change("W-AGE-01", enabled=False),
                 reason="Agreed with the team")
    at = to_iso(NOW)
    assert (saved.revision, saved.at) == (1, at) and re.fullmatch(r"[0-9a-f]{32}", saved.change_id)
    assert _rows(store, "SELECT * FROM rule_settings ORDER BY rule_id") == [
        ("AIL", "R-EFF-01", None, None, '{"threshold":2.0}', at),
        ("AIL", "W-AGE-01", 0, None, None, at),
    ]
    [change] = _rows(store, "SELECT * FROM rule_settings_changes")
    assert change[:7] == (saved.change_id, at, "AIL", 1, "local", "Local (sign-in off)", "Agreed with the team")
    assert json.loads(change[7]) == [
        {"field": "params.threshold", "from": 1.5, "rule_id": "R-EFF-01", "to": 2.0},
        {"field": "enabled", "from": True, "rule_id": "W-AGE-01", "to": False},
    ]
    assert change[8] is None
    assert read_history(store.conn, "AIL") == [{
        "change_id": saved.change_id, "at": at, "revision": 1, "by": {"hash": "local", "name": "Local (sign-in off)"},
        "reason": "Agreed with the team", "undo_of": None,
        "changes": [{"rule_id": "R-EFF-01", "field": "params.threshold", "from": 1.5, "to": 2.0},
                    {"rule_id": "W-AGE-01", "field": "enabled", "from": True, "to": False}],
    }]


def test_a_save_made_from_another_revision_is_a_conflict_and_writes_nothing(store: Store) -> None:
    save(store, Change("R-HYG-01", enabled=False))
    with pytest.raises(SettingsConflict) as conflict:
        save(store, Change("R-EFF-01", params={"threshold": 2.0}), revision=0)
    assert conflict.value.revision == 1
    assert (store.count("rule_settings"), store.count("rule_settings_changes")) == (1, 1)


def test_an_invalid_save_writes_nothing_and_names_every_problem(store: Store) -> None:
    with pytest.raises(SettingsInvalid) as refused:
        save(store, Change("R-EFF-01", params={"threshold": 2.0}), Change("W-EFF-01", params={"threshold": 3.0}),
             undo_of="f" * 32)
    assert [(problem.rule_id, problem.field) for problem in refused.value.problems] == [
        (None, "undo_of"), ("W-EFF-01", "params.threshold")]
    assert (store.count("rule_settings"), store.count("rule_settings_changes")) == (0, 0)


def test_a_save_that_fails_part_way_leaves_nothing_behind(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    real = store.upsert

    def fails_on_the_change(table: str, row: Any) -> Any:
        if table == "rule_settings_changes":
            raise RuntimeError("disk full")
        return real(table, row)

    monkeypatch.setattr(store, "upsert", fails_on_the_change)
    with pytest.raises(RuntimeError, match="disk full"):
        save(store, Change("R-EFF-01", params={"threshold": 2.0}))
    assert (store.count("rule_settings"), store.count("rule_settings_changes")) == (0, 0)


def test_an_undo_restores_rules_yaml_and_names_the_change_it_undid(store: Store) -> None:
    first = save(store, Change("R-EFF-01", params={"threshold": 2.0}))
    undone = save(store, Change("R-EFF-01", params={"threshold": 1.5}), reason="Undo of revision 1",
                  undo_of=first.change_id)
    assert undone.revision == 2 and store.count("rule_settings") == 0
    assert [(entry["revision"], entry["undo_of"], entry["reason"]) for entry in read_history(store.conn, "AIL")] == [
        (2, first.change_id, "Undo of revision 1"), (1, None, None)]
    other = save_board_settings(store, CATALOGUE, "DEMO", revision=0, changes=[Change("R-HYG-01", enabled=False)],
                                now=NOW, **LOCAL)
    with pytest.raises(SettingsInvalid, match="The change to undo is not one of this board's."):
        save(store, Change("R-HYG-01", enabled=False), undo_of=other.change_id)


def test_an_undo_must_put_back_exactly_what_the_boards_latest_change_set(store: Store) -> None:
    """A save that names the change it undoes is refused unless it undoes the board's latest change exactly (each field
    it set, back to its value before it, and nothing else): the history then says a change was undone only when it no
    longer applies."""
    first = save(store, Change("R-HYG-01", enabled=False))
    second = save(store, Change("W-SPR-01", severity="breach", params={"days_left": 3}),
                  Change("R-EFF-01", params={"threshold": 2.5}))
    undoes = {"reason": "Undo of revision 2", "undo_of": second.change_id}

    def refused(*changes: Change, undo_of: str = second.change_id) -> list[tuple[str | None, str | None, str]]:
        with pytest.raises(SettingsInvalid) as refusal:
            save(store, *changes, reason="Undo", undo_of=undo_of)
        return [(problem.rule_id, problem.field, problem.message) for problem in refusal.value.problems]

    not_an_undo = (None, "undo_of", "This change does not undo that one: an undo sets each field it changed back to"
                                    " what it was, and nothing else.")
    # Something else altogether, named as the undo of revision 2.
    assert refused(Change("W-AGE-01", params={"pct": 80})) == [not_an_undo]
    # Part of it, too much of it, or another value than the one before.
    assert refused(Change("W-SPR-01", severity="warning", params={"days_left": 2})) == [not_an_undo]
    assert refused(Change("W-SPR-01", severity="warning", params={"days_left": 2}),
                   Change("R-EFF-01", params={"threshold": 1.5}), Change("R-HYG-01", enabled=True)) == [not_an_undo]
    assert refused(Change("W-SPR-01", severity="warning", params={"days_left": 2}),
                   Change("R-EFF-01", params={"threshold": 1.8})) == [not_an_undo]
    # Only the latest change can be undone, as the page offers it.
    assert refused(Change("R-HYG-01", enabled=True), undo_of=first.change_id) == [
        (None, "undo_of", "Only the board's latest change can be undone.")]
    # The change set's own problems come with it.
    assert refused(Change("R-EFF-01", params={"threshold": 11})) == [
        ("R-EFF-01", "params.threshold", "Effort ratio must be from 0.1 to 10.")]
    assert (store.count("rule_settings_changes"), load_board_settings(store.conn, "AIL").revision) == (2, 2)

    # The exact inverse is taken, whatever order the change set names the rules in.
    undone = save(store, Change("W-SPR-01", severity="warning", params={"days_left": 2}),
                  Change("R-EFF-01", params={"threshold": 1.5}), **undoes)
    assert [(entry["revision"], entry["undo_of"]) for entry in read_history(store.conn, "AIL")][:1] == [
        (3, second.change_id)]
    # An undo of an undo puts the change back.
    again = save(store, Change("W-SPR-01", severity="breach", params={"days_left": 3}),
                 Change("R-EFF-01", params={"threshold": 2.5}), undo_of=undone.change_id)
    assert again.revision == 4 and load_board_settings(store.conn, "AIL").of("R-EFF-01") == RuleSetting(
        "R-EFF-01", params=(("threshold", 2.5),))


def test_the_rules_a_board_turned_off_are_read_from_its_history(store: Store, tmp_path: Path) -> None:
    assert turned_off(store.conn, "AIL") == frozenset()
    save(store, Change("R-HYG-01", enabled=False), Change("W-AGE-01", enabled=False))
    save(store, Change("W-AGE-01", enabled=True))  # back on: no longer off
    save(store, Change("R-EFF-01", params={"threshold": 2.0}))  # no change of on or off
    save(store, Change("W-REV-01", enabled=False))
    save(store, Change("W-REV-01", reset=True))  # a reset that turns it back on says so too
    assert turned_off(store.conn, "AIL") == frozenset({"R-HYG-01"})
    assert turned_off(store.conn, "DEMO") == frozenset()
    # A rule rules.yaml has off that the board turned on and off again is one of them.
    stingy = catalogue(enabled=[rule.id for rule in CATALOGUE.rules if rule.id != "W-SPR-01"])
    for enabled in (True, False):
        revision = load_board_settings(store.conn, "AIL").revision
        save_board_settings(store, stingy, "AIL", revision=revision, changes=[Change("W-SPR-01", enabled=enabled)],
                            now=NOW, **LOCAL)
    assert turned_off(store.conn, "AIL") == frozenset({"R-HYG-01", "W-SPR-01"})
    # A snapshot from before version 9 has no history: nothing was turned off.
    path = tmp_path / "v8.sqlite"
    with Store.open(path) as old:
        old.conn.executescript(VERSION_8)
    conn = open_readonly(path)
    try:
        assert turned_off(conn, "AIL") == frozenset()
    finally:
        conn.close()


def test_each_board_has_its_own_revisions_and_history_newest_first_at_most_fifty(store: Store) -> None:
    for n in range(52):
        save(store, Change("R-EST-02", params={"grace_hours": float(2 + n % 2)}))
    save_board_settings(store, CATALOGUE, "DEMO", revision=0, changes=[Change("R-HYG-01", enabled=False)], now=NOW,
                        **LOCAL)
    history = read_history(store.conn, "AIL")
    assert [entry["revision"] for entry in history] == list(range(52, 2, -1))
    assert load_board_settings(store.conn, "AIL").revision == 52
    assert [entry["revision"] for entry in read_history(store.conn, "DEMO")] == [1]
