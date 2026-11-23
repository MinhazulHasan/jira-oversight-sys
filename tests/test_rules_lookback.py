"""The rules' look-back (defaults.lookback_days) and the thresholds' type exclusion.

Look-back: a done issue finished (the later of issue_metrics.done_at and
resolved_at) before now - lookback_days is out of scope: no rule checks it, and
its open findings resolve as "aged_out", never "cleared". Open work is always
checked. The test catalogue (tests/fixtures/config/rules.yaml) has no look-back,
so each test turns it on with catalogue(lookback_days=...).

Thresholds: closed intervals of the excluded types (defaults.exclude_types:
Epic) never feed a percentile.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from oversight.config import FieldsConfig, ProcessConfig
from oversight.notify.digest import build_digest
from oversight.rules.catalog import RuleDefaults, RulesConfig
from oversight.rules.thresholds import stage_threshold, status_age_threshold
from oversight.settings import PROJECT_ROOT
from oversight.store.db import Store
from tests.conftest import CONFIG_FIXTURES
from tests.rules_helpers import NOW, add_history, add_issue, at, catalogue, context, derive, run_at

DAY = 24.0  # hours, for add_issue's hour offsets
LOOK_BACK = catalogue(lookback_days=90)


def _story(store: Store) -> None:
    # AIL-1: finished 150 days ago, straight from To Do to Done (an R-FLOW-02 skip).
    add_issue(store, "AIL-1", status="Done", created=-200 * DAY, changes=[(-150 * DAY, "To Do", "Done")])
    # AIL-2: the same skip, finished 30 days ago.
    add_issue(store, "AIL-2", status="Done", created=-60 * DAY, changes=[(-30 * DAY, "To Do", "Done")])
    # AIL-3: open work started 300 days ago with nobody assigned (R-HYG-01).
    add_issue(
        store, "AIL-3", status="In Progress", created=-310 * DAY, changes=[(-300 * DAY, "To Do", "In Progress")],
        assignee=None,
    )


def _open(store: Store) -> set[tuple[str, str]]:
    return {(r["rule_id"], r["issue_key"]) for r in store.query("SELECT * FROM findings WHERE resolved_at IS NULL")}


def _findings(store: Store, issue_key: str) -> list[dict[str, object]]:
    rows = store.query("SELECT * FROM findings WHERE issue_key = ? ORDER BY rule_id", (issue_key,))
    return [dict(row) for row in rows]


def _stats(result: object, rule_id: str) -> object:
    return next(s for s in result.stats if s.rule_id == rule_id)  # type: ignore[attr-defined]


def test_finished_work_older_than_the_look_back_is_not_checked(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store)
    derive(store, process)

    [result] = run_at(store, process, fields, LOOK_BACK)

    found = _open(store)
    assert ("R-FLOW-02", "AIL-2") in found  # finished 30 days ago: checked
    assert ("R-HYG-01", "AIL-3") in found  # open work, however old: always checked
    assert not [key for key in found if key[1] == "AIL-1"]  # finished 150 days ago: never checked
    assert _findings(store, "AIL-1") == []
    assert (result.out_of_scope, result.aged_out, result.lookback_days) == (1, 0, 90)
    assert _stats(result, "R-FLOW-02").checked == 2  # AIL-2 and AIL-3


def test_without_a_look_back_every_issue_is_checked(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    _story(store)
    derive(store, process)

    [result] = run_at(store, process, fields)  # the test catalogue sets no look-back

    assert ("R-FLOW-02", "AIL-1") in _open(store)
    assert _stats(result, "R-FLOW-02").checked == 3
    assert (result.out_of_scope, result.aged_out, result.lookback_days) == (0, 0, None)
    assert "look-back" not in result.render()


def test_open_findings_on_work_that_ages_out_resolve_as_aged_out(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    # AIL-2 finished 30 days ago: in scope today, out of scope 61 days from now.
    _story(store)
    derive(store, process)
    run_at(store, process, fields, LOOK_BACK)
    before = [row for row in _findings(store, "AIL-2") if row["resolved_at"] is None]
    assert [row["rule_id"] for row in before] == ["R-FLOW-02"]

    later = NOW + timedelta(days=61)
    [result] = run_at(store, process, fields, LOOK_BACK, now=later)

    after = _findings(store, "AIL-2")
    assert [(row["resolved_at"], row["resolved_why"]) for row in after] == [(at(0, later), "aged_out")]
    assert (result.aged_out, result.resolved, result.out_of_scope) == (1, 0, 2)  # AIL-1 and AIL-2 out
    assert ("R-HYG-01", "AIL-3") in _open(store)  # open work stays open


def test_the_later_of_done_at_and_resolved_at_decides(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    # Each skips from To Do straight to Done, so R-FLOW-02 flags every issue it checks.
    for key, done in (("AIL-4", -120), ("AIL-5", -10), ("AIL-6", -120), ("AIL-7", -120)):
        add_issue(store, key, status="Done", created=(done - 5) * DAY, changes=[(done * DAY, "To Do", "Done")])
    derive(store, process)
    with store.transaction():
        # AIL-4: done 120 days ago, but resolved 10 days ago (resolution set again): in scope.
        store.conn.execute("UPDATE issues SET resolved_at = ? WHERE issue_key = 'AIL-4'", (at(-10 * DAY),))
        # AIL-5: done 10 days ago, its resolution left from 120 days ago: in scope.
        store.conn.execute("UPDATE issues SET resolved_at = ? WHERE issue_key = 'AIL-5'", (at(-120 * DAY),))
        # AIL-6: both 120 days ago: out. AIL-7: done, but neither time is known: in scope.
        store.conn.execute("UPDATE issues SET resolved_at = NULL WHERE issue_key = 'AIL-7'")
        store.conn.execute("UPDATE issue_metrics SET done_at = NULL WHERE issue_key = 'AIL-7'")

    [result] = run_at(store, process, fields, LOOK_BACK)

    flagged = {key for rule, key in _open(store) if rule == "R-FLOW-02"}
    assert flagged == {"AIL-4", "AIL-5", "AIL-7"}
    assert result.out_of_scope == 1


def test_reopened_work_comes_back_into_scope(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    _story(store)
    derive(store, process)
    run_at(store, process, fields)  # no look-back: AIL-1's skip opens
    run_at(store, process, fields, LOOK_BACK)  # ... and ages out
    [aged] = [row for row in _findings(store, "AIL-1") if row["rule_id"] == "R-FLOW-02"]
    assert aged["resolved_why"] == "aged_out"

    # Reopened yesterday: open work again, so the rules check it and the same finding reopens.
    add_issue(
        store, "AIL-1", status="In Progress", created=-200 * DAY,
        changes=[(-150 * DAY, "To Do", "Done"), (-DAY, "Done", "In Progress")],
    )
    derive(store, process)
    [result] = run_at(store, process, fields, LOOK_BACK)

    [again] = [row for row in _findings(store, "AIL-1") if row["rule_id"] == "R-FLOW-02"]
    assert again["finding_id"] == aged["finding_id"]
    assert (again["resolved_at"], again["resolved_why"]) == (None, None)
    assert again["first_seen_at"] == aged["first_seen_at"]
    assert result.out_of_scope == 0 and _stats(result, "R-FLOW-02").opened == 1


def test_another_versions_findings_are_superseded_not_aged_out(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store)
    derive(store, process)
    run_at(store, process, fields, catalogue(version="0.0.9"))
    old = {row["finding_id"] for row in _findings(store, "AIL-1")}
    assert old

    [result] = run_at(store, process, fields, LOOK_BACK)

    assert {row["resolved_why"] for row in _findings(store, "AIL-1")} == {"superseded"}
    assert result.aged_out == 0 and result.superseded >= len(old)


def test_any_rules_findings_on_aged_out_work_age_out(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    # A disabled rule's findings are left as they are, unless their issue left scope.
    _story(store)
    derive(store, process)
    run_at(store, process, fields)

    [result] = run_at(store, process, fields, catalogue(lookback_days=90, enabled=["R-HYG-01"]))

    # R-FLOW-02 is off in this run: its finding on AIL-1 ages out, the one on AIL-2 stays as it was.
    assert {row["rule_id"]: row["resolved_why"] for row in _findings(store, "AIL-1")} == {"R-FLOW-02": "aged_out"}
    assert {row["rule_id"]: row["resolved_why"] for row in _findings(store, "AIL-2")} == {"R-FLOW-02": None}
    assert (result.aged_out, result.resolved, result.untouched) == (1, 0, 2)  # AIL-2's skip, AIL-3's R-EST-01


def test_the_digest_does_not_report_aged_out_findings(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store)
    derive(store, process)
    run_at(store, process, fields)
    with store.transaction():
        store.upsert(
            "digests",
            {"digest_id": "d1", "project_key": "AIL", "channel": "file", "sent_at": at(1), "since": None},
        )
    # Next day: AIL-3 gets an assignee (R-HYG-01 clears) and the look-back ages AIL-1 out.
    add_issue(
        store, "AIL-3", status="In Progress", created=-310 * DAY, changes=[(-300 * DAY, "To Do", "In Progress")]
    )
    derive(store, process)
    [result] = run_at(store, process, fields, LOOK_BACK, now=NOW + timedelta(days=1))
    assert (result.aged_out, result.resolved) == (1, 1)

    content = build_digest(store, process, LOOK_BACK, project="AIL", now=NOW + timedelta(days=2))

    assert [(item.rule_id, item.issue_key) for item in content.resolved] == [("R-HYG-01", "AIL-3")]


def test_render_and_the_run_record_report_the_look_back(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store)
    derive(store, process)
    run_at(store, process, fields)

    [result] = run_at(store, process, fields, LOOK_BACK)

    head, *body = result.render().splitlines()
    assert head.startswith("rules AIL (catalogue 0.1.0): 0 opened, ")
    assert "  look-back 90 days: 1 issues finished before it not checked, 1 findings on them aged out" in body
    run = store.query_one("SELECT stats_json FROM runs WHERE run_id = ?", (result.run_id,))
    assert run is not None
    stats = json.loads(run["stats_json"])["AIL"]
    assert (stats["aged_out"], stats["out_of_scope"], stats["lookback_days"], stats["resolved"]) == (1, 1, 90, 0)


def test_the_shipped_catalogue_looks_back_90_days_and_the_test_copy_does_not() -> None:
    assert RulesConfig.load(PROJECT_ROOT / "config" / "rules.yaml").defaults.lookback_days == 90
    assert RulesConfig.load(CONFIG_FIXTURES / "rules.yaml").defaults.lookback_days is None
    assert RuleDefaults().lookback_days is None
    with pytest.raises(ValidationError):
        RuleDefaults(lookback_days=0)


# --- thresholds: the excluded types never feed a percentile -------------------------------------


def _epics(store: Store, hours: list[float]) -> None:
    """One finished Epic per value, with one closed In Progress interval of that many hours."""
    for n, value in enumerate(hours):
        add_issue(
            store, f"AIL-{800 + n}", issue_type="Epic", status="Done", created=-DAY - value,
            changes=[(-DAY, "In Progress", "Done")], points=1,
        )


def test_thresholds_leave_out_the_excluded_types(store: Store, process: ProcessConfig) -> None:
    add_history(store, "In Progress", [10, 20, 30, 40])  # Tasks
    _epics(store, [900, 1000, 1100, 1200])  # epics stay open for weeks
    derive(store, process)
    options: dict[str, object] = {"min_history": 4, "history_days": 180, "now": NOW}

    pooled = status_age_threshold(store, "AIL", "In Progress", pct=90, **options)  # type: ignore[arg-type]
    work = status_age_threshold(store, "AIL", "In Progress", pct=90, exclude_types=["Epic"], **options)  # type: ignore[arg-type]
    shouted = status_age_threshold(store, "AIL", "In Progress", pct=90, exclude_types=["EPIC"], **options)  # type: ignore[arg-type]
    stage = stage_threshold(store, "AIL", "development", pct=75, exclude_types=["epic"], **options)  # type: ignore[arg-type]

    assert pooled is not None and pooled.n == 8
    # By hand, as in test_thresholds: 10, 20, 30, 40 -> p90 37, p75 32.5.
    assert work is not None and (work.n, work.hours) == (4, pytest.approx(37.0))
    assert shouted is not None and shouted.n == 4  # type names match ignoring case
    assert stage is not None and (stage.n, stage.hours) == (4, pytest.approx(32.5))
    # With the Epics left out there are too few intervals for a stricter min_history.
    strict = dict(options, min_history=5)
    assert status_age_threshold(store, "AIL", "In Progress", pct=90, exclude_types=["Epic"], **strict) is None  # type: ignore[arg-type]


def test_an_issue_without_a_type_still_counts(store: Store, process: ProcessConfig) -> None:
    add_history(store, "In Progress", [10, 20, 30])
    add_issue(store, "AIL-850", issue_type=None, status="Done", created=-DAY - 40,  # type: ignore[arg-type]
              changes=[(-DAY, "In Progress", "Done")], points=1)
    derive(store, process)

    found = status_age_threshold(store, "AIL", "In Progress", min_history=4, now=NOW, exclude_types=["Epic"])

    assert found is not None and found.n == 4


def test_the_rule_context_passes_the_catalogue_exclusions(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_history(store, "In Progress", [10, 20, 30, 40])
    _epics(store, [900, 1000, 1100, 1200])
    derive(store, process)

    rules_default = context(store, process, fields, catalogue(min_history=4))  # exclude_types: [Epic]
    rules_none = context(store, process, fields, catalogue(min_history=4, exclude_types=[]))

    found = rules_default.status_threshold("In Progress", 90)
    assert found is not None and (found.n, found.hours) == (4, pytest.approx(37.0))
    stage = rules_default.stage_threshold("development", 75)
    assert stage is not None and (stage.n, stage.hours) == (4, pytest.approx(32.5))
    everything = rules_none.status_threshold("In Progress", 90)
    assert everything is not None and everything.n == 8
