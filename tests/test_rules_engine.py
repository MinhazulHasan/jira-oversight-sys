from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.errors import ConfigError
from oversight.rules import checks
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import RulesResult, RuleStats, finding_id, run_rules
from oversight.store.db import Store
from tests.rules_helpers import FIVE_STAGES, NOW, add_issue, at, catalogue, derive, run_at, sprint, with_project

LATER = NOW + timedelta(hours=1)
STARTED = [(-48.0, "To Do", "In Progress")]

STORY_FINDINGS = {
    ("R-EST-01", "AIL-1"),
    ("R-HYG-01", "AIL-1"),
    ("R-FLOW-02", "AIL-2"),
    ("R-FLOW-03", "AIL-3"),
    ("W-EFF-01", "AIL-3"),
    ("W-EST-01", "AIL-4"),
    ("W-SPR-01", "sprint:4401"),
}


def _story(store: Store) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, assignee=None)
    add_issue(store, "AIL-2", status="Done", changes=[(-30, "To Do", "Done")], points=1)
    add_issue(store, "AIL-3", status="In Progress", points=2, spent_h=14,
              changes=[*STARTED, (-20, "In Progress", "Done"), (-10, "Done", "In Progress")])
    add_issue(store, "AIL-4", sprints=[sprint(4401)])


def _findings(store: Store, rule_id: str | None = None) -> list[sqlite3.Row]:
    where, params = (" WHERE rule_id = ?", (rule_id,)) if rule_id else ("", ())
    return store.query(f"SELECT * FROM findings{where} ORDER BY rule_version, rule_id, issue_key", params)


def _open(store: Store) -> set[tuple[str, str]]:
    return {(r["rule_id"], r["issue_key"]) for r in _findings(store) if r["resolved_at"] is None}


def _stat(result: RulesResult, rule_id: str) -> RuleStats:
    return next(s for s in result.stats if s.rule_id == rule_id)


def test_a_run_records_findings_rule_status_and_the_run(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store)
    derive(store, process)
    rules = catalogue()

    [result] = run_at(store, process, fields, rules)

    assert _open(store) == STORY_FINDINGS
    assert (result.opened, result.refreshed, result.resolved, result.superseded, result.linked) == (7, 0, 0, 0, 0)
    for row in _findings(store):
        rule = rules.by_id(row["rule_id"])
        assert row["finding_id"] == finding_id(rule.id, "0.1.0", row["issue_key"], row["evidence_key"])
        assert (row["kind"], row["severity"], row["project_key"]) == (rule.kind, rule.severity, "AIL")
        assert (row["first_seen_at"], row["last_seen_at"], row["run_id"]) == (at(0), at(0), result.run_id)
        assert isinstance(json.loads(row["evidence_json"]), dict) and row["message"]
    hygiene = store.query_one("SELECT * FROM rule_status WHERE run_id = ? AND rule_id = 'R-HYG-01'", (result.run_id,))
    assert hygiene is not None
    assert (hygiene["measurable"], hygiene["issues_checked"], hygiene["hits"]) == (1, 2, 1)
    run = store.query_one("SELECT * FROM runs WHERE run_id = ?", (result.run_id,))
    assert run is not None
    assert (run["kind"], run["status"], run["rules_version"]) == ("rules", "succeeded", "0.1.0")
    assert (run["started_at"], run["finished_at"]) == (at(0), at(0))
    assert json.loads(run["stats_json"])["AIL"]["opened"] == 7


def test_not_measurable_rules_are_recorded_with_the_missing_evidence_named(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store)
    derive(store, process)

    [result] = run_at(store, process, fields)

    rows = store.query("SELECT * FROM rule_status WHERE run_id = ?", (result.run_id,))
    assert len(rows) == len(catalogue().enabled_rules) == 12
    missing = {r["rule_id"]: json.loads(r["missing_json"]) for r in rows if r["measurable"] == 0}
    assert missing == {"R-FLOW-01": ["code_review", "testing"], "W-REV-01": ["code_review"]}
    assert [(s.rule_id, s.missing) for s in result.not_measurable] == list(missing.items())
    assert all(r["missing_json"] is None for r in rows if r["measurable"] == 1)
    assert not [r for r in _findings(store) if r["rule_id"] in missing]


def test_rules_become_measurable_once_the_evidence_is_configured(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    reviewed = with_project(process, status_to_stage=FIVE_STAGES, evidence={"code_review": {"label": "code-reviewed"}})
    add_issue(store, "AIL-1", status="Testing", points=1, changes=[*STARTED, (-20, "In Progress", "Testing")])
    derive(store, reviewed)

    [result] = run_at(store, reviewed, fields)

    assert result.not_measurable == []
    assert [r["evidence_key"] for r in _findings(store, "R-FLOW-01")] == ["testing-2"]


def test_render_names_the_not_measurable_rules(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    _story(store)
    derive(store, process)

    [result] = run_at(store, process, fields)
    head, *body = result.render().splitlines()
    lines = {line.split()[0]: line for line in body}

    assert head.startswith(
        "rules AIL (catalogue 0.1.0): 7 opened, 0 still open, 0 resolved, 0 superseded, 0 warnings linked to breaches"
    )
    assert lines["R-FLOW-01"] == "  R-FLOW-01  not measurable: nothing in Jira shows code review and testing"
    assert lines["W-REV-01"] == "  W-REV-01   not measurable: nothing in Jira shows code review"
    assert lines["R-HYG-01"] == "  R-HYG-01   1 hits (1 new) on 2 checked"
    assert "fewer than 20 closed intervals in 180 days, no threshold" in lines["W-AGE-01"]


def test_a_second_run_with_no_change_opens_and_resolves_nothing(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store)
    derive(store, process)
    [first] = run_at(store, process, fields)
    before = _findings(store)

    [second] = run_at(store, process, fields, now=LATER)

    assert (second.opened, second.resolved, second.superseded, second.linked, second.untouched) == (0, 0, 0, 0, 0)
    assert second.refreshed == first.opened == 7
    after = _findings(store)
    assert [(r["finding_id"], r["first_seen_at"], r["resolved_at"]) for r in after] == [
        (r["finding_id"], at(0), None) for r in before
    ]
    assert {(r["last_seen_at"], r["run_id"]) for r in after} == {(at(1), second.run_id)}


def test_a_cleared_hit_resolves_and_a_reappearance_reopens_keeping_first_seen(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    def board(assignee: str | None) -> None:
        add_issue(store, "AIL-1", status="In Progress", changes=STARTED, points=1, assignee=assignee)

    board(None)
    derive(store, process)
    run_at(store, process, fields)
    [row] = _findings(store, "R-HYG-01")

    board("acct-team-2")
    [cleared] = run_at(store, process, fields, now=NOW + timedelta(hours=1))
    [resolved] = _findings(store, "R-HYG-01")
    assert (cleared.opened, cleared.resolved) == (0, 1)
    assert (resolved["resolved_at"], resolved["resolved_why"]) == (at(1), "cleared")

    board(None)
    [again] = run_at(store, process, fields, now=NOW + timedelta(hours=2))
    [reopened] = _findings(store, "R-HYG-01")
    assert (again.opened, again.resolved, _stat(again, "R-HYG-01").opened) == (1, 0, 1)
    assert reopened["finding_id"] == row["finding_id"]
    assert (reopened["first_seen_at"], reopened["last_seen_at"]) == (at(0), at(2))
    assert (reopened["resolved_at"], reopened["resolved_why"]) == (None, None)


def test_a_catalogue_version_bump_supersedes_the_old_findings(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store)
    derive(store, process)
    run_at(store, process, fields)

    [bumped] = run_at(store, process, fields, catalogue(version="0.2.0"), now=LATER)

    rows = _findings(store)
    old = [r for r in rows if r["rule_version"] == "0.1.0"]
    new = [r for r in rows if r["rule_version"] == "0.2.0"]
    assert (bumped.superseded, bumped.resolved, bumped.opened) == (7, 0, 7)
    assert {(r["resolved_at"], r["resolved_why"]) for r in old} == {(at(1), "superseded")}
    assert {r["resolved_at"] for r in new} == {None}
    assert {r["finding_id"] for r in old}.isdisjoint(r["finding_id"] for r in new)
    assert sorted((r["rule_id"], r["issue_key"], r["evidence_key"]) for r in old) == sorted(
        (r["rule_id"], r["issue_key"], r["evidence_key"]) for r in new
    )


def test_findings_of_disabled_rules_are_left_untouched(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store)
    derive(store, process)
    run_at(store, process, fields)
    all_but_hygiene = [rule.id for rule in catalogue().rules if rule.id != "R-HYG-01"]

    [result] = run_at(store, process, fields, catalogue(enabled=all_but_hygiene), now=LATER)

    assert (result.untouched, result.resolved, result.superseded) == (1, 0, 0)
    [row] = _findings(store, "R-HYG-01")
    assert (row["resolved_at"], row["last_seen_at"]) == (None, at(0))
    assert "  1 findings of disabled or not-measurable rules left as they were" in result.render().splitlines()


def test_epics_are_excluded_unless_a_rule_names_them(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", issue_type="Epic", status="In Progress", changes=STARTED, assignee=None)
    add_issue(store, "AIL-2", status="In Progress", changes=STARTED, assignee=None, points=1)
    derive(store, process)

    [result] = run_at(store, process, fields)

    assert _open(store) == {("R-HYG-01", "AIL-2")}
    assert _stat(result, "R-HYG-01").checked == 1

    data = catalogue().model_dump()
    [hygiene] = [rule for rule in data["rules"] if rule["id"] == "R-HYG-01"]
    hygiene["applies_to"]["issue_type"] = ["epic"]
    run_at(store, process, fields, RulesConfig.model_validate(data), now=LATER)

    assert _open(store) == {("R-HYG-01", "AIL-1")}


def test_a_warning_link_is_written_once_when_the_warning_preceded_the_breach(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, points=1, spent_h=7)
    add_issue(store, "AIL-2", status="Done", changes=[*STARTED, (-2, "In Progress", "Done")], points=1, spent_h=16)
    derive(store, process)
    [first] = run_at(store, process, fields)
    [warning] = _findings(store, "W-EFF-01")
    assert first.linked == 0

    add_issue(store, "AIL-1", status="Done", changes=[*STARTED, (1, "In Progress", "Done")], points=1, spent_h=16)
    derive(store, process, now=NOW + timedelta(hours=2))
    [second] = run_at(store, process, fields, now=NOW + timedelta(hours=2))
    [breach] = [r for r in _findings(store, "R-EFF-01") if r["issue_key"] == "AIL-1"]

    assert second.linked == 1
    links = store.query("SELECT * FROM warning_links")
    assert [(r["warning_finding_id"], r["breach_finding_id"], r["linked_at"]) for r in links] == [
        (warning["finding_id"], breach["finding_id"], at(2))
    ]
    [third] = run_at(store, process, fields, now=NOW + timedelta(hours=3))
    assert third.linked == 0
    assert store.count("warning_links") == 1


def test_a_link_keeps_its_time_and_never_crosses_catalogue_versions(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    def step(hours: float, status: str, spent_h: float, rules: RulesConfig | None = None) -> RulesResult:
        changes = STARTED if status == "In Progress" else [*STARTED, (1, "In Progress", "Done")]
        add_issue(store, "AIL-1", status=status, changes=changes, points=1, spent_h=spent_h)
        when = NOW + timedelta(hours=hours)
        derive(store, process, now=when)
        [result] = run_at(store, process, fields, rules, now=when)
        return result

    assert step(0, "In Progress", 7).linked == 0
    assert step(2, "Done", 16).linked == 1
    assert step(3, "Done", 4).resolved == 1
    assert step(4, "Done", 16).linked == 0
    bumped = step(5, "Done", 16, catalogue(version="0.2.0"))
    assert (bumped.opened, bumped.linked) == (1, 0)
    assert [r["linked_at"] for r in store.query("SELECT linked_at FROM warning_links")] == [at(2)]


def test_an_exception_in_a_check_rolls_the_project_back_and_fails_the_run(
    store: Store, process: ProcessConfig, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("AIL-1", "DEMO-1"):
        add_issue(store, key, status="In Progress", changes=STARTED, assignee=None)
    derive(store, process, ("AIL", "DEMO"))
    original = checks.CHECKS["reopened"]

    def fails_on_demo(issue: checks.Issue, ctx: checks.RuleContext, params: dict[str, Any]) -> checks.CheckResult:
        if ctx.project == "DEMO":
            raise RuntimeError("check exploded")
        return original(issue, ctx, params)

    monkeypatch.setitem(checks.CHECKS, "reopened", fails_on_demo)

    with pytest.raises(RuntimeError, match="check exploded"):
        run_at(store, process, fields, projects=("AIL", "DEMO"))

    assert {r["issue_key"] for r in _findings(store)} == {"AIL-1"}
    assert {r["project_key"] for r in store.query("SELECT project_key FROM rule_status")} == {"AIL"}
    run = store.query_one("SELECT * FROM runs WHERE kind = 'rules'")
    assert run is not None and (run["status"], run["finished_at"]) == ("failed", at(0))
    assert json.loads(run["stats_json"]) == {"error": "RuntimeError: check exploded", "projects_done": ["AIL"]}


def test_a_failure_to_record_the_failed_run_never_hides_the_original_error(
    store: Store, process: ProcessConfig, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, assignee=None)
    derive(store, process)

    def explode(issue: checks.Issue, ctx: checks.RuleContext, params: dict[str, Any]) -> checks.CheckResult:
        raise RuntimeError("check exploded")

    def broken_bookkeeping(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setitem(checks.CHECKS, "reopened", explode)
    monkeypatch.setattr(store, "finish_run", broken_bookkeeping)

    with pytest.raises(RuntimeError, match="check exploded"):
        run_at(store, process, fields)


def test_issues_without_metrics_are_skipped_with_a_note(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, assignee=None, points=1)
    derive(store, process)
    add_issue(store, "AIL-2", status="In Progress", changes=STARTED, assignee=None, points=1)

    [result] = run_at(store, process, fields)

    assert _open(store) == {("R-HYG-01", "AIL-1")}
    assert _stat(result, "R-HYG-01").note == "1 issues have no metrics: run derive"
    assert all("1 issues have no metrics: run derive" in (s.note or "") for s in result.stats if s.measurable)


def test_an_unknown_project_fails_before_anything_is_written(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    with pytest.raises(ConfigError, match="NOPE"):
        run_at(store, process, fields, projects=("NOPE",))

    assert store.count("runs", {"kind": "rules"}) == 0


def _people(store: Store) -> set[str]:
    people: set[str] = set()
    for row in store.query("SELECT assignee_id, raw_json FROM issues"):
        people.add(row["assignee_id"] or "")
        assignee = (json.loads(row["raw_json"] or "{}").get("fields") or {}).get("assignee")
        if isinstance(assignee, dict):
            people.update(str(assignee.get(k) or "") for k in ("accountId", "displayName"))
    return people - {""}


def test_on_the_seeded_demo_every_measurable_rule_finds_something(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    seed_demo(store, now=NOW, seed=20261104)
    derive(store, process, ("DEMO",))

    [result] = run_rules(store, process, catalogue(), fields, projects=["DEMO"], now=lambda: NOW)

    assert {s.rule_id for s in result.not_measurable} == {"R-FLOW-01", "W-REV-01"}
    assert [s.rule_id for s in result.stats if s.measurable and s.hits == 0] == []
    assert result.opened == store.count("findings", {"project_key": "DEMO"})
    people = _people(store)
    assert people, "the demo assigns work, so this check has something to look for"
    assert not [r["message"] for r in _findings(store) if any(p in r["message"] + r["evidence_json"] for p in people)]

    [again] = run_rules(store, process, catalogue(), fields, projects=["DEMO"], now=lambda: NOW)
    assert (again.opened, again.resolved, again.superseded) == (0, 0, 0)
