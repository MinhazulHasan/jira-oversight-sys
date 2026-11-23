"""Custom rules (JO-20): the check registry (oversight/rules/registry.py) and how a rule is validated against it and the
board's options (a status in a category the rule never looks at among the refusals); ids, the limit and the titles (as
a reader sees them: nothing hidden, no look-alike); a board's rules with its custom rules (oversight/rules/settings.py);
how a change set adds, edits, deletes and restores one, with its history and undo, never naming the id a create in it
will be given, a new rule's problems named by its place in it; and what the engine does with them
(oversight/rules/engine.py): a rule added opens findings, narrowing it or raising its limit closes them as
settings_changed, a severity change gives new ids, a delete closes them as settings_changed and undoing it reopens the
same ids, first seen as before, and a release that moves the form's starting values renames none; and the preview of
an add, a delete, and a delete saved whose re-check has not run.

Small AIL stories built with tests/rules_helpers.py at fixed clocks, on a workflow that maps code review, saved as the
API saves them (save_board_settings, with the board's process), by the local viewer.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs

from oversight.ask.catalog import RULE_ID_PATTERN, ListFindingsParams
from oversight.config import FieldsConfig, ProcessConfig
from oversight.insights import weekly
from oversight.insights.weekly import Narrative, build_candidates, run_insights
from oversight.llm.scripted import ScriptedProvider
from oversight.rules import checks as check_module
from oversight.rules import settings as settings_module
from oversight.settings import Settings
from oversight.rules.catalog import Rule
from oversight.rules.engine import finding_id, preview
from oversight.rules.registry import (
    CHECK_SPECS,
    CUSTOM_LIMIT,
    EDITABLE,
    BoardOptions,
    board_options,
    check_definition,
    checks_payload,
)
from oversight.rules.settings import (
    BoardSettings,
    Change,
    CustomContext,
    FieldChange,
    RuleSetting,
    SavedChange,
    SettingsInvalid,
    effective_rules,
    load_board_settings,
    next_numbers,
    plan_changes,
    read_history,
    save_board_settings,
    settings_hash,
)
from oversight.store.db import Store
from tests.rules_helpers import FIVE_STAGES, NOW, add_issue, at, catalogue, derive, run_at, with_project

CATALOGUE = catalogue()
LOCAL = {"viewer_hash": "local", "viewer_name": "Local (sign-in off)"}
OPTIONS = BoardOptions(
    project="AIL",
    statuses=(("To Do", "new"), ("Code Review", "indeterminate"), ("In Progress", "indeterminate"), ("Done", "done")),
    issue_types=("Bug", "Story", "Task"),
    stages=("backlog", "development", "code_review", "done"),
    evidence=("code_review", "testing"),
    recorded=frozenset({"code_review"}),
)
BOARD = CustomContext(OPTIONS, {"R": 1, "W": 1}, "2026-11-23T09:00:00.000Z", "Local (sign-in off)")


def definition(**overrides: Any) -> dict[str, Any]:
    """Work in Code Review or To Do for more than 3 days: a breach rule, To do and In progress, every type."""
    rule: dict[str, Any] = {
        "title": "Stuck in review or to do",
        "check": "time_in_status_over",
        "params": {"statuses": ["Code Review", "To Do"], "hours": 72},
        "applies_to": {"status_category": ["new", "indeterminate"], "issue_type": None},
        "severity": "breach",
    }
    rule.update(overrides)
    return rule


def problems_of(*changes: Change, current: BoardSettings | None = None) -> list[tuple[str | None, str | None, str]]:
    with pytest.raises(SettingsInvalid) as refused:
        plan_changes(CATALOGUE, current or BoardSettings("AIL"), list(changes), BOARD)
    return [(problem.rule_id, problem.field, problem.message) for problem in refused.value.problems]


def added(current: BoardSettings | None = None, **overrides: Any) -> BoardSettings:
    """The board's settings with one more custom rule."""
    return plan_changes(CATALOGUE, current or BoardSettings("AIL"), [Change.adding(definition(**overrides))],
                        BOARD).settings


# --- the registry -------------------------------------------------------------------------------------------------


def test_the_registry_holds_every_check_the_engine_runs_and_nothing_else() -> None:
    assert set(CHECK_SPECS) == set(check_module.CHECKS) | set(check_module.SPRINT_CHECKS)
    for spec in CHECK_SPECS.values():
        # Two to five plain words, as the JO-20 spec asks (the dashboard's sample registry says them word for word).
        assert 2 <= len(spec.label.split()) <= 5 and spec.summary.endswith(".") and len(spec.summary) <= 90
        assert set(spec.default_categories) <= set(spec.categories) <= {"new", "indeterminate", "done"}
        assert (spec.scope == "sprint") == (spec.name in check_module.SPRINT_CHECKS) == (not spec.categories)
        assert len({param.name for param in spec.params}) == len(spec.params)
        for param in spec.params:
            assert (param.kind == "number") == (param.spec is not None)


def test_a_check_jo15_lets_admins_tune_uses_the_same_params_and_bounds() -> None:
    """One definition of a threshold for both kinds of rule: the registry's number params of the catalogue's checks are
    EDITABLE's ParamSpecs themselves."""
    for check, specs in EDITABLE.items():
        for spec in specs:
            param = CHECK_SPECS[check].param(spec.name)
            assert param is not None and param.spec is spec and param.label == spec.label
    assert CHECK_SPECS["effort_ratio_at_least"].param("threshold").default == 1.5  # the form starts at R-EFF-01's
    table = {(spec.name, param.name): (param.kind, param.spec.unit if param.spec else None,
                                       param.spec.min if param.spec else None, param.spec.max if param.spec else None)
             for spec in CHECK_SPECS.values() for param in spec.params}
    assert {key: table[key] for key in (("time_in_status_over", "statuses"), ("time_in_status_over", "hours"),
                                        ("open_longer_than", "days"), ("no_update_for", "days"),
                                        ("missing_field", "field"))} == {
        ("time_in_status_over", "statuses"): ("statuses", None, None, None),
        ("time_in_status_over", "hours"): ("number", "hours", 1.0, 8760.0),
        ("open_longer_than", "days"): ("number", "days", 1.0, 3650.0),
        ("no_update_for", "days"): ("number", "days", 1.0, 365.0),
        ("missing_field", "field"): ("field", None, None, None),
    }


def test_the_categories_each_check_may_apply_to_and_starts_with() -> None:
    found = {name: (spec.categories, spec.default_categories) for name, spec in CHECK_SPECS.items()}
    assert found["time_in_status_over"] == (("new", "indeterminate"), ("indeterminate",))
    assert found["open_longer_than"] == (("new", "indeterminate"), ("new", "indeterminate"))
    assert found["no_update_for"] == (("new", "indeterminate"), ("indeterminate",))
    assert found["missing_field"] == (("new", "indeterminate", "done"), ("indeterminate",))
    assert found["effort_ratio_at_least"] == (("indeterminate", "done"), ("done",))
    assert found["sprint_at_risk"] == ((), ())


def test_a_boards_options_come_from_its_statuses_issues_and_process(
    store: Store, process: ProcessConfig
) -> None:
    five = with_project(process, "AIL", status_to_stage=FIVE_STAGES, evidence={"code_review": {"status": "Code Review"},
                                                                               "testing": None})
    add_issue(store, "AIL-1", issue_type="Bug")
    add_issue(store, "AIL-2", issue_type="Story")
    add_issue(store, "AIL-3", issue_type="Epic")  # rules leave Epics out by default: so does the list
    options = board_options(store.conn, five, "AIL")
    assert options.statuses == (("To Do", "new"), ("Code Review", "indeterminate"), ("In Progress", "indeterminate"),
                                ("Testing", "indeterminate"), ("Done", "done"))
    assert options.issue_types == ("Bug", "Story")
    assert options.stages == ("backlog", "development", "code_review", "testing", "done")
    assert (options.evidence, options.recorded) == (("code_review", "testing"), frozenset({"code_review"}))
    # A board with no statuses synced lists the ones its workflow maps, with no category.
    assert board_options(store.conn, five, "OPS" if "OPS" in five.projects else "DEMO").statuses
    checks = {check["name"]: check for check in checks_payload(options)}
    review = checks["entered_stage_without_evidence"]
    assert (review["measurable"], review["missing_evidence"]) == (True, [])
    [stage, evidence] = review["params"]
    assert [option["value"] for option in stage["options"]] == list(options.stages)
    assert evidence["options"][0] == {"value": "code_review", "label": "Code review"}
    assert checks["time_in_status_over"]["params"][0]["options"][0] == {"value": "To Do", "label": "To Do",
                                                                         "category": "new"}
    # On AIL as configured, Jira records no evidence: those checks are not measurable, saying what is missing.
    bare = checks_payload(board_options(store.conn, process, "AIL"))
    flow = next(check for check in bare if check["name"] == "entered_stage_without_evidence")
    assert (flow["measurable"], flow["missing_evidence"]) == (False, ["code_review", "dev_signoff", "testing",
                                                                     "test_review", "test_signoff"])


# --- a custom rule's definition -------------------------------------------------------------------------------------


@pytest.mark.parametrize("check", list(CHECK_SPECS))
def test_every_check_takes_a_rule_built_from_its_starting_values_and_the_boards_first_options(check: str) -> None:
    """What the form starts with for each check (its params' defaults, the board's first status in one of the check's
    default categories, the first stage and evidence) is a rule a save takes, on the check's default categories."""
    spec = CHECK_SPECS[check]
    status = next(name for name, category in OPTIONS.statuses if category in spec.default_categories or not
                  spec.default_categories)
    first = {"statuses": [status], "stage": OPTIONS.stages[0], "evidence": OPTIONS.evidence[0]}
    params = {param.name: param.default if param.default is not None else first[param.kind] for param in spec.params}
    found, problems = check_definition({
        "title": spec.label, "check": check, "params": params, "severity": "warning",
        "applies_to": {"status_category": list(spec.default_categories), "issue_type": None},
    }, OPTIONS)
    assert problems == [] and found is not None and set(found.params) == {param.name for param in spec.params}


def test_a_definition_is_kept_as_the_board_writes_its_names() -> None:
    found, problems = check_definition(definition(
        title="  Bugs stuck  ", params={"statuses": ["to do", "CODE REVIEW", "To Do"], "hours": 72},
        applies_to={"status_category": ["indeterminate", "new"], "issue_type": ["bug", "TASK"]},
    ), OPTIONS)
    assert problems == [] and found is not None
    assert found.as_dict() == {
        "title": "Bugs stuck", "description": None, "check": "time_in_status_over",
        "params": {"statuses": ["To Do", "Code Review"], "hours": 72.0},
        "applies_to": {"status_category": ["new", "indeterminate"], "issue_type": ["Bug", "Task"]},
        "severity": "breach",
    }


@pytest.mark.parametrize("given", [
    definition(),
    definition(check="open_longer_than", params={"days": 30}),
    definition(check="no_update_for", params={"days": 7.5}, applies_to={"status_category": ["indeterminate"],
                                                                        "issue_type": None}),
    definition(check="missing_field", params={"field": "epic"},
               applies_to={"status_category": ["new", "indeterminate", "done"], "issue_type": ["Story"]}),
    definition(check="missing_estimate", params={}, applies_to={"status_category": ["done"], "issue_type": None}),
    definition(check="effort_ratio_at_least", params={"threshold": 2.25},
               applies_to={"status_category": ["done"], "issue_type": None}, severity="warning"),
    definition(check="entered_stage_without_evidence", params={"stage": "code_review", "evidence": "testing"},
               applies_to={"status_category": ["new", "indeterminate", "done"], "issue_type": None}),
    definition(check="no_evidence_after_percentile", params={"stage": "development", "evidence": "code_review",
                                                             "pct": 80}, applies_to={"status_category": ["indeterminate"],
                                                                                     "issue_type": None}),
    definition(check="sprint_at_risk", params={"days_left": 1, "not_started_share": 0.5},
               applies_to={"status_category": [], "issue_type": None}),
])
def test_a_definition_of_every_kind_of_check_is_taken(given: dict[str, Any]) -> None:
    found, problems = check_definition(given, OPTIONS)
    assert problems == [] and found is not None and found.check == given["check"]


@pytest.mark.parametrize(("given", "expected"), [
    (definition(check="time_in_status_at_least"), [("check", "Pick one of the checks.")]),
    (definition(params={"statuses": ["Code Review"]}), [("params.hours", "Time in the status must be a number.")]),
    (definition(params={"statuses": ["Code Review"], "hours": 72, "days": 3}),
     [("params.days", "Time in a status has no setting called days.")]),
    (definition(params={"statuses": ["Code Review"], "hours": 0.5}),
     [("params.hours", "Time in the status must be from 1 to 8760 hours.")]),
    (definition(params={"statuses": ["Code Review"], "hours": 72.12345}),
     [("params.hours", "Time in the status can have at most 4 decimal places.")]),
    (definition(params={"statuses": ["Code Review"], "hours": "72"}),
     [("params.hours", "Time in the status must be a number.")]),
    (definition(params={"statuses": [], "hours": 72}), [("params.statuses", "Pick at least one status.")]),
    (definition(params={"statuses": ["Peer Review", "QA"], "hours": 72}),
     [("params.statuses", "Not a status on this board: Peer Review, QA.")]),
    (definition(check="entered_stage_without_evidence", params={"stage": "test_signoff", "evidence": "signoff"},
                applies_to={"status_category": ["done"], "issue_type": None}),
     [("params.stage", "Pick a stage this board's workflow maps."),
      ("params.evidence", "Pick evidence this board's process names.")]),
    (definition(check="missing_field", params={"field": "labels"}),
     [("params.field", "Pick assignee, estimate, epic or sprint.")]),
    (definition(applies_to={"status_category": [], "issue_type": None}),
     [("applies_to.status_category", "Pick at least one of To do, In progress and Done.")]),
    (definition(applies_to={"status_category": ["done"], "issue_type": None}),
     [("applies_to.status_category", "Time in a status applies to To do and In progress only.")]),
    # A status in a category the rule does not look at: the engine picks work by category first, so it never fires.
    (definition(params={"statuses": ["Done"], "hours": 24}, applies_to={"status_category": ["indeterminate"],
                                                                         "issue_type": None}),
     [("params.statuses", "'Done' is a Done status: Time in a status checks To do and In progress work only.")]),
    (definition(params={"statuses": ["To Do", "Code Review"], "hours": 24},
                applies_to={"status_category": ["indeterminate"], "issue_type": None}),
     [("params.statuses", "'To Do' is a To do status: tick To do under Applies to.")]),
    (definition(applies_to={"status_category": ["new"], "issue_type": ["Bug", "Incident"]}),
     [("applies_to.issue_type", "Not an issue type on this board: Incident.")]),
    (definition(applies_to={"status_category": ["new"], "issue_type": []}),
     [("applies_to.issue_type", "Pick at least one issue type, or all types.")]),
    (definition(check="sprint_at_risk", params={"days_left": 1, "not_started_share": 0.5},
                applies_to={"status_category": ["new"], "issue_type": ["Bug"]}),
     [("applies_to.status_category", "A sprint rule has no status filter."),
      ("applies_to.issue_type", "A sprint rule has no issue type filter.")]),
    (definition(applies_to=None), [("applies_to", "Say which work the rule applies to.")]),
    (definition(title="ab"), [("title", "The name must be 3 to 80 characters.")]),
    (definition(title="x" * 81), [("title", "The name must be 3 to 80 characters.")]),
    # Characters that show as nothing (zero-width spaces and joiners, soft hyphens, word joiners, a byte-order mark):
    # a name of them would read as blank in every list.
    (definition(title="​​​"), [("title", "The name must be 3 to 80 characters.")]),
    (definition(title="­⁠﻿‍"), [("title", "The name must be 3 to 80 characters.")]),
    (definition(title="!!!"), [("title", "The name needs letters or digits.")]),
    (definition(title=None), [("title", "Give the rule a name.")]),
    (definition(description="x" * 301), [("description", "The description can be at most 300 characters.")]),
    (definition(severity="critical"), [("severity", "Pick Breach or Warning.")]),
    ({**definition(), "kind": "rule"}, [("kind", "A new rule cannot set kind.")]),
])
def test_a_definition_that_cannot_be_one_is_refused_on_its_fields(
    given: dict[str, Any], expected: list[tuple[str, str]]
) -> None:
    found, problems = check_definition(given, OPTIONS)
    assert found is None and [(problem.field, problem.message) for problem in problems] == expected


def test_the_rule_id_pattern_has_room_for_three_digits() -> None:
    base = CATALOGUE.by_id("R-HYG-01").model_dump()
    for rule_id in ("R-CUS-01", "W-CUS-104", "R-CUS-999"):
        assert Rule.model_validate({**base, "id": rule_id}).id == rule_id
        assert ListFindingsParams(rule_id=rule_id).rule_id == rule_id
    for rule_id in ("R-CUS-1", "R-CUS-1000", "X-CUS-01"):
        with pytest.raises(ValueError):
            Rule.model_validate({**base, "id": rule_id})
    assert RULE_ID_PATTERN == r"^[RW]-[A-Z]+-\d{2,3}$"


# --- ids, the limit, the titles ----------------------------------------------------------------------------------


def test_a_new_rule_is_given_the_next_id_of_its_kind_across_every_board(store: Store, process: ProcessConfig) -> None:
    five = with_project(process, "AIL", status_to_stage=FIVE_STAGES)
    five = with_project(five, "OPS", status_to_stage=FIVE_STAGES)
    add_issue(store, "AIL-1")
    add_issue(store, "OPS-1")
    assert next_numbers(store.conn) == {"R": 1, "W": 1}
    first = _save(store, five, Change.adding(definition()))
    assert [(change.rule_id, change.field) for change in first.changes] == [("R-CUS-01", "rule")]
    warning = _save(store, five, Change.adding(definition(title="Early warning", severity="warning")), project="OPS")
    other = _save(store, five, Change.adding(definition(title="On another board")), project="OPS")
    assert [warning.changes[0].rule_id, other.changes[0].rule_id] == ["W-CUS-01", "R-CUS-02"]
    _save(store, five, Change.deleting("R-CUS-02"), project="OPS")
    again = _save(store, five, Change.adding(definition(title="Once more")), project="OPS")
    assert again.changes[0].rule_id == "R-CUS-03"  # a deleted rule's id is never given again
    assert next_numbers(store.conn) == {"R": 4, "W": 2}
    rows = store.query("SELECT rule_id, project_key, kind, severity, created_revision, deleted_at IS NOT NULL"
                       " FROM custom_rules ORDER BY rule_id")
    assert [tuple(row) for row in rows] == [("R-CUS-01", "AIL", "rule", "breach", 1, 0),
                                            ("R-CUS-02", "OPS", "rule", "breach", 2, 1),
                                            ("R-CUS-03", "OPS", "rule", "breach", 4, 0),
                                            ("W-CUS-01", "OPS", "warning", "warning", 1, 0)]


def test_a_board_holds_at_most_fifty_custom_rules() -> None:
    settings = BoardSettings("AIL")
    for n in range(CUSTOM_LIMIT):
        settings = added(settings, title=f"Rule number {n:02d}")
    assert len(settings.live_custom) == CUSTOM_LIMIT
    assert problems_of(Change.adding(definition(title="One too many")), current=settings) == [
        (None, None, "A board can have at most 50 custom rules: delete one first.")]
    # One out, one in: still fifty.
    swapped = plan_changes(CATALOGUE, settings, [Change.deleting("R-CUS-01"), Change.adding(definition(title="New"))],
                           BOARD).settings
    assert len(swapped.live_custom) == CUSTOM_LIMIT


def test_a_title_is_unique_on_the_board_ignoring_case() -> None:
    one = added()
    assert problems_of(Change.adding(definition(title="reopened AFTER done")), current=one) == [
        (None, "title", "Another rule on this board already has this name.")]  # R-FLOW-03's
    assert problems_of(Change.adding(definition(title="STUCK in review or to do")), current=one) == [
        (None, "title", "Another rule on this board already has this name.")]
    assert problems_of(Change.adding(definition(title="Twice")), Change.adding(definition(title="twice"))) == [
        (None, "title", "Another rule on this board already has this name."),
        (None, "title", "Another rule on this board already has this name.")]
    two = added(one, title="Second")
    assert problems_of(Change.editing("R-CUS-02", title="Stuck in review or TO DO"), current=two) == [
        ("R-CUS-02", "title", "Another rule on this board already has this name.")]
    # A deleted rule's title is free again; restoring it while another rule holds the title is refused.
    gone = plan_changes(CATALOGUE, two, [Change.deleting("R-CUS-01")], BOARD).settings
    taken = added(gone, title="stuck in review or to do")
    assert problems_of(Change.restoring("R-CUS-01"), current=taken) == [
        ("R-CUS-01", "title", "Another rule on this board already has this name.")]


@pytest.mark.parametrize(("title", "kept"), [
    ("Stuck​ in review", "Stuck in review"),  # a zero-width space
    ("Ｓtuck in review", "Stuck in review"),  # a fullwidth S
    ("Bugs­ stuck  in\treview", "Bugs stuck in review"),  # a soft hyphen, spaces run together
])
def test_a_title_is_kept_as_a_reader_sees_it(title: str, kept: str) -> None:
    found, problems = check_definition(definition(title=title), OPTIONS)
    assert problems == [] and found is not None and found.title == kept


@pytest.mark.parametrize("title", ["Reopened​ after Done", "ＲEOPENED after done", "Reopened after⁠ Done"])
def test_a_title_that_looks_the_same_as_another_rules_is_taken(title: str) -> None:
    """Two rules that read the same in every list are one name: R-FLOW-03's here, whatever is hidden in the copy."""
    assert problems_of(Change.adding(definition(title=title))) == [
        (None, "title", "Another rule on this board already has this name.")]
    assert problems_of(Change.editing("R-CUS-01", title=title), current=added()) == [
        ("R-CUS-01", "title", "Another rule on this board already has this name.")]


@pytest.mark.parametrize("named", [
    Change("R-CUS-01", enabled=False),
    Change("R-CUS-01", severity="warning"),
    Change.editing("R-CUS-01", title="Renamed at once"),
    Change.deleting("R-CUS-01"),
    Change.restoring("R-CUS-01"),
], ids=["off", "severity", "edit", "delete", "restore"])
def test_a_change_never_names_the_id_a_create_in_the_same_change_set_will_be_given(named: Change) -> None:
    """R-CUS-01 is the id the create would be given, and no rule on the board yet: a change to it is refused, before or
    after the create, whatever it is (a rule added off, or added and deleted at once, would keep it out of the
    history)."""
    for changes in ([Change.adding(definition()), named], [named, Change.adding(definition())]):
        assert problems_of(*changes) == [("R-CUS-01", None, "R-CUS-01 is not a rule on this board.")]
    # A rule the board holds is still a change's to name beside a create (the next create is R-CUS-02).
    plan = plan_changes(CATALOGUE, added(), [Change.adding(definition(title="Another")), Change.deleting("R-CUS-01")],
                        BOARD)
    assert [(change.rule_id, change.field) for change in plan.changes] == [("R-CUS-01", "rule"), ("R-CUS-02", "rule")]


def test_a_change_set_of_creates_and_deletes_of_their_own_ids_changes_nothing() -> None:
    """50 rules added and deleted at once (each id one more than the last): refused whole, so no id is used up and no
    revision with nothing in its history is written."""
    creates = [Change.adding(definition(title=f"Rule number {n:02d}")) for n in range(CUSTOM_LIMIT)]
    deletes = [Change.deleting(f"R-CUS-{n:02d}") for n in range(1, CUSTOM_LIMIT + 1)]
    with pytest.raises(SettingsInvalid) as refused:
        plan_changes(CATALOGUE, BoardSettings("AIL"), [*creates, *deletes], BOARD)
    assert {problem.message for problem in refused.value.problems} == {
        f"R-CUS-{n:02d} is not a rule on this board." for n in range(1, CUSTOM_LIMIT + 1)}


def test_a_new_rules_problem_names_its_place_in_the_change_set() -> None:
    """Two rules added at once, the second refused: its problems name it by where its create stands (it has no id)."""
    with pytest.raises(SettingsInvalid) as refused:
        plan_changes(CATALOGUE, BoardSettings("AIL"), [
            Change("R-EFF-01", enabled=False),
            Change.adding(definition(title="Fine")),
            Change.adding(definition(title="fine", check="open_longer_than", params={"days": 0})),
        ], BOARD)
    assert [problem.as_dict() for problem in refused.value.problems] == [
        {"rule_id": None, "field": "changes.2.rule.params.days", "message": "Days open must be from 1 to 3650 days."}]
    with pytest.raises(SettingsInvalid) as twice:
        plan_changes(CATALOGUE, BoardSettings("AIL"), [Change.adding(definition(title="Same")),
                                                        Change.adding(definition(title="SAME"))], BOARD)
    assert [problem.as_dict()["field"] for problem in twice.value.problems] == [
        "changes.0.rule.title", "changes.1.rule.title"]


def test_an_edit_that_leaves_a_status_outside_the_rules_categories_is_refused() -> None:
    """The rule names Code Review (In progress) and To Do (To do), on both categories."""
    one = added()
    assert problems_of(Change.editing("R-CUS-01", applies_to={"status_category": ["indeterminate"]}), current=one) == [
        ("R-CUS-01", "params.statuses", "'To Do' is a To do status: tick To do under Applies to.")]
    assert problems_of(Change("R-CUS-01", params={"statuses": ["Done", "Code Review"]}), current=one) == [
        ("R-CUS-01", "params.statuses",
         "'Done' is a Done status: Time in a status checks To do and In progress work only.")]
    # Narrowed both at once, or a change of anything else: taken.
    narrowed = plan_changes(CATALOGUE, one, [Change.editing(
        "R-CUS-01", params={"statuses": ["Code Review"]}, applies_to={"status_category": ["indeterminate"]})], BOARD)
    assert [change.field for change in narrowed.changes] == ["applies_to", "params.statuses"]
    plan_changes(CATALOGUE, one, [Change.editing("R-CUS-01", title="Renamed")], BOARD)


@pytest.mark.parametrize(("change", "expected"), [
    (Change("R-CUS-01", reset=True), ("R-CUS-01", "reset", "A custom rule has no catalogue settings to reset to.")),
    (Change.editing("R-CUS-01", check="open_longer_than"),
     ("R-CUS-01", "check", "The check can't change: delete the rule and add it again.")),
    (Change.editing("R-CUS-01"), ("R-CUS-01", None, "The change to R-CUS-01 names nothing to change.")),
    (Change.editing("R-CUS-01", title="Stuck in review or to do"),
     ("R-CUS-01", None, "R-CUS-01 already has these settings on this board.")),
    (Change("R-CUS-01", enabled=True), ("R-CUS-01", None, "R-CUS-01 already has these settings on this board.")),
    (Change.restoring("R-CUS-01"), ("R-CUS-01", None, "R-CUS-01 is not deleted.")),
    (Change.editing("R-HYG-01", title="Mine now"),
     ("R-HYG-01", None, "R-HYG-01 is a catalogue rule: only a custom rule can be edited.")),
    (Change.deleting("R-EFF-01"), ("R-EFF-01", None, "R-EFF-01 is a catalogue rule: only a custom rule can be deleted.")),
    (Change.deleting("R-CUS-09"), ("R-CUS-09", None, "R-CUS-09 is not a rule on this board.")),
    (Change.editing("R-CUS-01", params={"hours": 0}),
     ("R-CUS-01", "params.hours", "Time in the status must be from 1 to 8760 hours.")),
    (Change.editing("R-CUS-01", applies_to={"status_category": ["done"]}),
     ("R-CUS-01", "applies_to.status_category", "Time in a status applies to To do and In progress only.")),
])
def test_a_change_to_a_custom_rule_that_cannot_be_made_is_refused(
    change: Change, expected: tuple[str | None, str | None, str]
) -> None:
    assert problems_of(change, current=added()) == [expected]


def test_a_deleted_rule_cannot_be_changed_until_it_is_restored() -> None:
    gone = plan_changes(CATALOGUE, added(), [Change.deleting("R-CUS-01")], BOARD).settings
    for change in (Change.editing("R-CUS-01", title="Back"), Change("R-CUS-01", enabled=False)):
        assert problems_of(change, current=gone) == [
            ("R-CUS-01", None, "R-CUS-01 was deleted: restore it before changing it.")]
    assert problems_of(Change.deleting("R-CUS-01"), current=gone) == [("R-CUS-01", None, "R-CUS-01 is already deleted.")]
    assert problems_of(Change.editing("R-CUS-01", title="One"), Change.deleting("R-CUS-01"), current=added()) == [
        ("R-CUS-01", None, "R-CUS-01 is named more than once: make one change per rule.")]


# --- a board's rules, its custom rules among them ----------------------------------------------------------------


def test_a_boards_custom_rules_follow_the_catalogues_validated_as_any_rule() -> None:
    settings = added(added(), title="No epic", check="missing_field", params={"field": "epic"},
                     applies_to={"status_category": ["new", "indeterminate", "done"], "issue_type": ["Story"]},
                     severity="warning")
    board = effective_rules(CATALOGUE, settings)
    assert [rule.id for rule in board.rules] == [rule.id for rule in CATALOGUE.rules] + ["R-CUS-01", "W-CUS-01"]
    stuck, epic = board.by_id("R-CUS-01"), board.by_id("W-CUS-01")
    assert (stuck.custom, epic.custom, CATALOGUE.rules[0].custom, board.by_id("R-EFF-01").custom) == (
        True, True, False, False)
    assert (stuck.kind, stuck.severity, epic.kind, epic.severity) == ("rule", "breach", "warning", "warning")
    assert (board.rule_version(stuck), board.rule_version(epic)) == ("c1", "c1")
    assert stuck.settings_hash == settings_hash({
        "params": {"statuses": ["To Do", "Code Review"], "hours": 72.0},
        "applies_to": {"status_category": ["new", "indeterminate"], "issue_type": None}})
    # Neither names a finding by a param (checks.keyed): their limits are never part of a finding's id.
    assert stuck.identity_params == {} and epic.identity_params == {}
    assert not stuck.customised and stuck.enabled
    # The engine reads its scope as a catalogue rule's: every category picked is no filter at all.
    assert stuck.applies_to is not None and stuck.applies_to.status_category == ["new", "indeterminate"]
    assert epic.applies_to is not None and (epic.applies_to.status_category, epic.applies_to.issue_type) == (
        None, ["Story"])
    review = effective_rules(CATALOGUE, added(title="Review", check="entered_stage_without_evidence",
                                              params={"stage": "code_review", "evidence": "testing"},
                                              applies_to={"status_category": ["done"], "issue_type": None}))
    assert (review.by_id("R-CUS-01").requires_evidence, review.by_id("R-CUS-01").requires_stages) == (
        ["testing"], ["code_review"])


def test_a_custom_rules_on_off_and_severity_are_the_boards_settings() -> None:
    settings = plan_changes(CATALOGUE, added(), [Change("R-CUS-01", enabled=False, severity="warning")],
                            BOARD).settings
    assert settings.of("R-CUS-01") == RuleSetting("R-CUS-01", enabled=False, severity="warning")
    rule = effective_rules(CATALOGUE, settings).by_id("R-CUS-01")
    assert (rule.enabled, rule.severity, rule.kind, rule.customised) == (False, "warning", "rule", True)
    assert effective_rules(CATALOGUE, settings).rule_version(rule) == "c1+warning"
    back = plan_changes(CATALOGUE, settings, [Change("R-CUS-01", enabled=True, severity="breach")], BOARD).settings
    assert back.rules == () and not effective_rules(CATALOGUE, back).by_id("R-CUS-01").customised


def test_a_deleted_rule_is_no_longer_the_boards_but_its_findings_keep_its_title() -> None:
    gone = plan_changes(CATALOGUE, added(), [Change.deleting("R-CUS-01")], BOARD).settings
    board = effective_rules(CATALOGUE, gone)
    assert "R-CUS-01" not in [rule.id for rule in board.rules]
    assert board.find("R-CUS-01") is not None and board.title_of("R-CUS-01") == "Stuck in review or to do"
    assert board.title_of("R-GONE-01") == "R-GONE-01" and board.find("R-GONE-01") is None
    assert board.title_of("R-EFF-01") == CATALOGUE.by_id("R-EFF-01").title


def test_a_custom_rule_that_no_longer_fits_is_left_out_with_a_warning() -> None:
    [rule] = added().custom
    broken = replace(rule, params_json=json.dumps({"statuses": ["To Do"]}))  # no hours: a hand edit
    clash = replace(rule, rule_id="R-EFF-01", title="Clash")  # an id rules.yaml holds
    with capture_logs() as events:
        board = effective_rules(CATALOGUE, BoardSettings("AIL", 1, (), (broken,)))
        effective_rules(CATALOGUE, BoardSettings("AIL", 1, (), (clash,)))
    assert [rule.id for rule in board.rules] == [rule.id for rule in CATALOGUE.rules]
    ignored = [(event["rule"], event["setting"]) for event in events if event["event"] == "rule_setting_ignored"]
    assert ignored == [("R-CUS-01", "rule"), ("R-EFF-01", "rule")]
    # Deleted, such a row never closes the catalogue rule's findings: it is no custom rule the board deleted.
    deleted = BoardSettings("AIL", 1, (), (replace(rule, deleted_at=at(0)), replace(clash, deleted_at=at(0))))
    assert deleted.deleted_custom(CATALOGUE) == ["R-CUS-01"]


# --- saving, the history and undo -------------------------------------------------------------------------------


def _save(store: Store, process: ProcessConfig, *changes: Change, project: str = "AIL", hours: float = 0.0,
          undo_of: str | None = None) -> SavedChange:
    revision = load_board_settings(store.conn, project).revision
    return save_board_settings(store, CATALOGUE, project, revision=revision, changes=list(changes),
                               now=NOW + timedelta(hours=hours), process=process, undo_of=undo_of, **LOCAL)


@pytest.fixture
def five(process: ProcessConfig) -> ProcessConfig:
    return with_project(process, "AIL", status_to_stage=FIVE_STAGES)


def _definition(rule: Mapping[str, Any]) -> dict[str, Any]:
    return {"title": rule["title"], "description": rule.get("description"), "check": rule["check"],
            "params": rule["params"], "applies_to": rule["applies_to"], "severity": rule["severity"]}


def test_adding_editing_deleting_and_restoring_are_written_with_their_history(
    store: Store, five: ProcessConfig
) -> None:
    add_issue(store, "AIL-1", issue_type="Bug")
    rule = definition(description=" Flags work stuck in review. ",
                      applies_to={"status_category": ["new", "indeterminate"], "issue_type": ["bug"]})
    made = _save(store, five, Change.adding(rule))
    stored = {"title": "Stuck in review or to do", "description": "Flags work stuck in review.",
              "check": "time_in_status_over", "params": {"statuses": ["To Do", "Code Review"], "hours": 72.0},
              "applies_to": {"status_category": ["new", "indeterminate"], "issue_type": ["Bug"]}, "severity": "breach"}
    assert made.changes == (FieldChange("R-CUS-01", "rule", None, stored),)
    row = store.query_one("SELECT * FROM custom_rules")
    assert row is not None and (row["created_by"], row["created_at"], row["created_revision"], row["deleted_at"]) == (
        "Local (sign-in off)", at(0), 1, None)
    assert json.loads(row["params_json"]) == stored["params"] and json.loads(row["applies_json"]) == stored["applies_to"]

    edited = _save(store, five, Change.editing("R-CUS-01", title="Bugs stuck in review", params={"hours": 96},
                                               applies_to={"issue_type": None}, description=None), hours=1)
    assert [change.as_dict() for change in edited.changes] == [
        {"rule_id": "R-CUS-01", "field": "title", "from": "Stuck in review or to do", "to": "Bugs stuck in review"},
        {"rule_id": "R-CUS-01", "field": "description", "from": "Flags work stuck in review.", "to": None},
        {"rule_id": "R-CUS-01", "field": "applies_to",
         "from": {"status_category": ["new", "indeterminate"], "issue_type": ["Bug"]},
         "to": {"status_category": ["new", "indeterminate"], "issue_type": None}},
        {"rule_id": "R-CUS-01", "field": "params.hours", "from": 72.0, "to": 96.0},
    ]
    # The threshold control a catalogue rule has works on a custom rule too, with on/off and severity.
    tuned = _save(store, five, Change("R-CUS-01", enabled=False, severity="warning", params={"hours": 100}), hours=2)
    assert [change.as_dict() for change in tuned.changes] == [
        {"rule_id": "R-CUS-01", "field": "enabled", "from": True, "to": False},
        {"rule_id": "R-CUS-01", "field": "severity", "from": "breach", "to": "warning"},
        {"rule_id": "R-CUS-01", "field": "params.hours", "from": 96.0, "to": 100.0},
    ]
    deleted = _save(store, five, Change.deleting("R-CUS-01"), hours=3)
    now_stored = {**stored, "title": "Bugs stuck in review", "description": None, "params": {**stored["params"],
                  "hours": 100.0}, "applies_to": {"status_category": ["new", "indeterminate"], "issue_type": None},
                  "severity": "warning"}
    assert deleted.changes == (FieldChange("R-CUS-01", "rule", now_stored, None),)
    assert store.query_one("SELECT deleted_at FROM custom_rules")[0] == at(3)
    # The deleted rule's settings stay, so a restore brings back the same rule (and the same findings).
    assert load_board_settings(store.conn, "AIL").of("R-CUS-01") == RuleSetting("R-CUS-01", False, "warning")
    restored = _save(store, five, Change.restoring("R-CUS-01"), hours=4, undo_of=deleted.change_id)
    assert restored.changes == (FieldChange("R-CUS-01", "rule", None, now_stored),)
    assert [(entry["revision"], entry["undo_of"], [change["field"] for change in entry["changes"]])
            for entry in read_history(store.conn, "AIL")] == [
        (5, deleted.change_id, ["rule"]), (4, None, ["rule"]), (3, None, ["enabled", "severity", "params.hours"]),
        (2, None, ["title", "description", "applies_to", "params.hours"]), (1, None, ["rule"])]


def test_undo_deletes_what_was_added_restores_what_was_deleted_and_edits_back(
    store: Store, five: ProcessConfig
) -> None:
    add_issue(store, "AIL-1")
    made = _save(store, five, Change.adding(definition()))
    # The undo of an add is its delete, and only that.
    with pytest.raises(SettingsInvalid, match="does not undo that one"):
        _save(store, five, Change.editing("R-CUS-01", title="Something else"), undo_of=made.change_id)
    undone = _save(store, five, Change.deleting("R-CUS-01"), undo_of=made.change_id)
    assert load_board_settings(store.conn, "AIL").live_custom == []
    # The undo of that delete is the restore, and the undo of the restore a delete again.
    with pytest.raises(SettingsInvalid, match="does not undo that one"):
        _save(store, five, Change.adding(definition(title="Not a restore")), undo_of=undone.change_id)
    restored = _save(store, five, Change.restoring("R-CUS-01"), undo_of=undone.change_id)
    again = _save(store, five, Change.deleting("R-CUS-01"), undo_of=restored.change_id)
    assert [change.field for change in again.changes] == ["rule"] and not load_board_settings(store.conn, "AIL").live_custom
    _save(store, five, Change.restoring("R-CUS-01"), undo_of=again.change_id)
    edited = _save(store, five, Change.editing("R-CUS-01", params={"statuses": ["Code Review"]}))
    with pytest.raises(SettingsInvalid, match="does not undo that one"):
        _save(store, five, Change.editing("R-CUS-01", params={"statuses": ["To Do"]}), undo_of=edited.change_id)
    back = _save(store, five, Change.editing("R-CUS-01", params={"statuses": ["To Do", "Code Review"]}),
                 undo_of=edited.change_id)
    assert [change.as_dict() for change in back.changes] == [{
        "rule_id": "R-CUS-01", "field": "params.statuses", "from": ["Code Review"], "to": ["To Do", "Code Review"]}]


def test_a_save_without_the_boards_process_still_turns_a_custom_rule_off_or_deletes_it(
    store: Store, five: ProcessConfig
) -> None:
    add_issue(store, "AIL-1")
    _save(store, five, Change.adding(definition()))
    revision = load_board_settings(store.conn, "AIL").revision
    save_board_settings(store, CATALOGUE, "AIL", revision=revision, changes=[Change("R-CUS-01", enabled=False)],
                        now=NOW, **LOCAL)
    assert effective_rules(CATALOGUE, load_board_settings(store.conn, "AIL")).by_id("R-CUS-01").enabled is False
    with pytest.raises(SettingsInvalid, match="Not a status on this board"):  # no process: no statuses to name
        save_board_settings(store, CATALOGUE, "AIL", revision=revision + 1, now=NOW, **LOCAL,
                            changes=[Change.adding(definition(title="Another"))])


# --- what the engine does with them ------------------------------------------------------------------------------


def _story(store: Store, process: ProcessConfig) -> None:
    """Three tickets past 3 days in a status the rule names (two in Code Review, one in To Do), one under it."""
    add_issue(store, "AIL-1", issue_type="Bug", status="Code Review",
              changes=[(-100.0, "To Do", "In Progress"), (-80.0, "In Progress", "Code Review")])
    add_issue(store, "AIL-2", issue_type="Story", status="Code Review",
              changes=[(-120.0, "To Do", "In Progress"), (-100.0, "In Progress", "Code Review")])
    add_issue(store, "AIL-3", issue_type="Bug", status="To Do", created=-200.0)
    add_issue(store, "AIL-4", issue_type="Bug", status="Code Review",
              changes=[(-30.0, "To Do", "In Progress"), (-20.0, "In Progress", "Code Review")])
    derive(store, process)


def _open(store: Store) -> dict[str, Any]:
    rows = store.query("SELECT * FROM findings WHERE rule_id = 'R-CUS-01' AND resolved_at IS NULL")
    return {row["issue_key"]: row for row in rows}


def _closed(store: Store) -> dict[str, str]:
    rows = store.query("SELECT issue_key, resolved_why FROM findings WHERE rule_id = 'R-CUS-01'"
                       " AND resolved_at IS NOT NULL")
    return {row["issue_key"]: row["resolved_why"] for row in rows}


def test_a_custom_rule_lives_its_whole_life_on_the_board(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store, five)
    _save(store, five, Change.adding(definition()))
    [result] = run_at(store, five, fields)

    opened = _open(store)
    assert sorted(opened) == ["AIL-1", "AIL-2", "AIL-3"]
    assert {(row["rule_version"], row["kind"], row["severity"], row["first_seen_at"]) for row in opened.values()} == {
        ("c1", "rule", "breach", at(0))}
    assert {row["evidence_key"] for row in opened.values()} == {"in-status-Code Review", "in-status-To Do"}
    assert opened["AIL-1"]["message"] == "In 'Code Review' for 3.3 days; the limit is 3 days"
    first = {key: row["finding_id"] for key, row in opened.items()}
    assert first["AIL-1"] == finding_id("R-CUS-01", "c1", "AIL-1", "in-status-Code Review")
    assert next(stat for stat in result.stats if stat.rule_id == "R-CUS-01").opened == 3

    # Narrowed to work in progress in Code Review: the ticket in To Do closes as settings_changed, never cleared.
    _save(store, five, Change.editing("R-CUS-01", params={"statuses": ["Code Review"]},
                                      applies_to={"status_category": ["indeterminate"]}))
    run_at(store, five, fields, now=NOW + timedelta(hours=1))
    assert sorted(_open(store)) == ["AIL-1", "AIL-2"] and _closed(store) == {"AIL-3": "settings_changed"}

    # The limit raised past 80 hours: AIL-1 closes as settings_changed; AIL-2, over it still, keeps its finding.
    _save(store, five, Change.editing("R-CUS-01", params={"hours": 90}))
    run_at(store, five, fields, now=NOW + timedelta(hours=2))
    still = _open(store)
    assert sorted(still) == ["AIL-2"] and still["AIL-2"]["finding_id"] == first["AIL-2"]
    assert json.loads(still["AIL-2"]["evidence_json"])["threshold_h"] == 90.0
    assert _closed(store) == {"AIL-1": "settings_changed", "AIL-3": "settings_changed"}

    # A severity change gives new ids: the open finding is superseded and raised again as a warning.
    _save(store, five, Change("R-CUS-01", severity="warning"))
    run_at(store, five, fields, now=NOW + timedelta(hours=3))
    warned = _open(store)["AIL-2"]
    assert (warned["rule_version"], warned["severity"], warned["kind"]) == ("c1+warning", "warning", "rule")
    assert store.query_one("SELECT resolved_why FROM findings WHERE finding_id = ?", (first["AIL-2"],))[0] == "superseded"
    _save(store, five, Change("R-CUS-01", severity="breach"))
    run_at(store, five, fields, now=NOW + timedelta(hours=4))
    assert _open(store)["AIL-2"]["finding_id"] == first["AIL-2"]  # back: the original reopens

    # Deleted: its open finding closes as settings_changed, as a rule turned off.
    deleted = _save(store, five, Change.deleting("R-CUS-01"))
    [gone] = run_at(store, five, fields, now=NOW + timedelta(hours=5))
    assert _open(store) == {} and gone.settings_changed == 1
    assert store.query_one("SELECT resolved_why FROM findings WHERE finding_id = ?", (first["AIL-2"],))[0] == (
        "settings_changed")
    assert "R-CUS-01" not in {stat.rule_id for stat in gone.stats}

    # Undoing the delete brings it back: the same id and version, so the same finding reopens, first seen as before.
    _save(store, five, Change.restoring("R-CUS-01"), undo_of=deleted.change_id)
    run_at(store, five, fields, now=NOW + timedelta(hours=6))
    back = _open(store)
    assert sorted(back) == ["AIL-2"]
    assert (back["AIL-2"]["finding_id"], back["AIL-2"]["first_seen_at"]) == (first["AIL-2"], at(0))


def test_a_custom_rule_never_looks_at_an_epic(store: Store, five: ProcessConfig, fields: FieldsConfig) -> None:
    """Epics are left out as for every rule (rules.yaml's exclude_types, applied by the engine, not the check), and a
    custom rule cannot name them back: the board offers no Epic among its issue types."""
    add_issue(store, "AIL-1", issue_type="Task", status="To Do", created=-500.0)
    add_issue(store, "AIL-9", issue_type="Epic", status="To Do", created=-500.0)
    derive(store, five)
    old = definition(title="Open over a day", check="open_longer_than", params={"days": 1})
    _save(store, five, Change.adding(old))
    run_at(store, five, fields)
    assert sorted(_open(store)) == ["AIL-1"]
    with pytest.raises(SettingsInvalid, match="Not an issue type on this board: Epic."):
        _save(store, five, Change.editing("R-CUS-01", applies_to={"issue_type": ["Epic"]}))


def test_turning_a_custom_rule_off_closes_its_findings_and_on_again_reopens_them(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store, five)
    _save(store, five, Change.adding(definition()))
    run_at(store, five, fields)
    _save(store, five, Change("R-CUS-01", enabled=False))
    [off] = run_at(store, five, fields, now=NOW + timedelta(hours=1))
    assert _open(store) == {} and set(_closed(store).values()) == {"settings_changed"} and off.settings_changed == 3
    _save(store, five, Change("R-CUS-01", enabled=True))
    run_at(store, five, fields, now=NOW + timedelta(hours=2))
    assert {row["first_seen_at"] for row in _open(store).values()} == {at(0)} and len(_open(store)) == 3


def test_the_weekly_insights_name_a_rising_custom_rule_by_its_title(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store, five)
    run_at(store, five, fields, now=NOW - timedelta(days=15))  # the board's history starts a fortnight before
    _save(store, five, Change.adding(definition(title="Bugs stuck 3 days")))
    run_at(store, five, fields)
    candidates = {candidate.id: candidate for candidate in
                  build_candidates(store, five, CATALOGUE, project="AIL", now=NOW + timedelta(hours=1))}
    rising = candidates["rising_rule:R-CUS-01"]
    assert (rising.title, rising.severity, rising.affected) == (
        "More 'Bugs stuck 3 days' findings this week than last", "breach", 3)


def test_a_custom_rules_title_in_the_weekly_briefing_is_words_never_a_figure(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    """The briefing quotes a rising rule's title: its 9 is a name, so the scripted sentence verifies, while a sentence
    that takes the 9 for a count of anything is refused."""
    _story(store, five)
    run_at(store, five, fields, now=NOW - timedelta(days=15))
    _save(store, five, Change.adding(definition(title="Bugs stuck 9 days")))
    run_at(store, five, fields)
    later = NOW + timedelta(hours=1)
    [insight] = run_insights(store, five, CATALOGUE, ScriptedProvider(), Settings(_env_file=None), projects=["AIL"],
                             now=lambda: later)
    rising = next(candidate for candidate in insight.candidates if candidate.id == "rising_rule:R-CUS-01")
    assert insight.verified and "'Bugs stuck 9 days' findings this week than last" in insight.narrative
    board = effective_rules(CATALOGUE, load_board_settings(store.conn, "AIL"))
    facts = [weekly._facts_for(rising, at(1))]
    invented = Narrative(sentences=[{"text": "Blatantly bad: 9 bugs are stuck.", "tone": "blatantly_bad",
                                     "candidate_ids": [rising.id]}])
    assert weekly._verify(invented, facts, {rising.id}, board).problems == ["unverified number 9"]


def test_a_preview_of_an_added_rule_opens_what_it_would_raise_and_of_a_delete_closes_what_it_holds(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    _story(store, five)
    board = CustomContext(board_options(store.conn, five, "AIL"), next_numbers(store.conn), at(0), "Local")
    plan = plan_changes(CATALOGUE, load_board_settings(store.conn, "AIL"), [Change.adding(definition())], board)
    [adding] = preview(store, five, CATALOGUE, fields, "AIL", plan.settings, now=NOW)
    assert (adding.rule_id, adding.open_now, adding.would_open, adding.would_close, adding.would_stay) == (
        "R-CUS-01", 0, 3, 0, 0)
    assert adding.open_examples == ("AIL-1", "AIL-2", "AIL-3") and adding.measurable and adding.enabled
    assert store.count("findings", {"rule_id": "R-CUS-01"}) == 0  # nothing written

    _save(store, five, Change.adding(definition()))
    run_at(store, five, fields)
    plan = plan_changes(CATALOGUE, load_board_settings(store.conn, "AIL"), [Change.deleting("R-CUS-01")], board)
    [deleting] = preview(store, five, CATALOGUE, fields, "AIL", plan.settings, now=NOW + timedelta(hours=1))
    assert (deleting.rule_id, deleting.open_now, deleting.would_open, deleting.would_close, deleting.would_stay) == (
        "R-CUS-01", 3, 0, 3, 0)
    assert deleting.close_examples == ("AIL-1", "AIL-2", "AIL-3") and not deleting.enabled
    assert len(_open(store)) == 3


def test_a_preview_says_what_the_recheck_closes_of_a_rule_deleted_before_it_ran(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    """A delete saved whose re-check failed or never ran: its findings are still open, and the re-check after the next
    save closes them. A preview of that save, of an unrelated rule, says so (pending): its totals are the re-check's."""
    _story(store, five)
    _save(store, five, Change.adding(definition()))
    run_at(store, five, fields)
    _save(store, five, Change.deleting("R-CUS-01"), hours=1)  # no re-check after it
    assert len(_open(store)) == 3
    unrelated = Change("R-EFF-01", params={"threshold": 2.0})
    board = CustomContext(board_options(store.conn, five, "AIL"), next_numbers(store.conn), at(2), "Local")
    plan = plan_changes(CATALOGUE, load_board_settings(store.conn, "AIL"), [unrelated], board)

    impacts = {impact.rule_id: impact for impact in preview(store, five, CATALOGUE, fields, "AIL", plan.settings,
                                                            now=NOW)}

    gone = impacts["R-CUS-01"]
    assert (gone.pending, gone.enabled, gone.open_now, gone.would_open, gone.would_close, gone.would_stay) == (
        True, False, 3, 0, 3, 0)
    assert gone.close_examples == ("AIL-1", "AIL-2", "AIL-3") and not impacts["R-EFF-01"].pending
    would_open = sum(impact.would_open for impact in impacts.values())
    would_close = sum(impact.would_close for impact in impacts.values())
    assert len(_open(store)) == 3  # nothing written

    _save(store, five, unrelated, hours=2)
    [result] = run_at(store, five, fields)
    closed = result.resolved + result.superseded + result.aged_out + result.settings_changed
    assert (result.opened, closed, result.settings_changed) == (would_open, would_close, 3)
    assert _open(store) == {}
    # Once its findings are closed, nothing of it is left to say.
    again = plan_changes(CATALOGUE, load_board_settings(store.conn, "AIL"), [Change("R-EFF-01", params={
        "threshold": 2.5})], board)
    assert "R-CUS-01" not in {impact.rule_id for impact in preview(store, five, CATALOGUE, fields, "AIL",
                                                                   again.settings, now=NOW)}


def test_a_custom_rules_finding_ids_never_follow_the_registrys_starting_values(
    store: Store, process: ProcessConfig, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom effort rule names its findings by CUSTOM_IDENTITY's 1.5, never by the form's starting value: a later
    release that starts the form elsewhere leaves every finding as it was (none cleared, none opened again)."""
    add_issue(store, "AIL-1", status="Done", changes=[(-100.0, "To Do", "In Progress"), (-10.0, "In Progress", "Done")],
              points=1, spent_h=20)  # 20 h against 8: 2.5×
    derive(store, process)
    effort = definition(title="Done at twice the estimate", check="effort_ratio_at_least", params={"threshold": 2},
                        applies_to={"status_category": ["done"], "issue_type": None})
    _save(store, process, Change.adding(effort))
    run_at(store, process, fields)
    [first] = store.query("SELECT finding_id, evidence_key FROM findings WHERE rule_id = 'R-CUS-01'")
    assert first["evidence_key"] == "ratio-ge-1.5"

    spec = CHECK_SPECS["effort_ratio_at_least"]
    moved = replace(spec, params=(replace(spec.params[0], start=1.6),))
    monkeypatch.setitem(CHECK_SPECS, "effort_ratio_at_least", moved)  # the form now starts at 1.6
    monkeypatch.setattr(settings_module, "_MEMO", {})
    assert CHECK_SPECS["effort_ratio_at_least"].param("threshold").default == 1.6
    rule = effective_rules(CATALOGUE, load_board_settings(store.conn, "AIL")).by_id("R-CUS-01")
    assert rule.identity_params == {"threshold": 1.5}

    [again] = run_at(store, process, fields, now=NOW + timedelta(hours=1))
    assert (again.resolved, next(stat for stat in again.stats if stat.rule_id == "R-CUS-01").opened) == (0, 0)
    assert [tuple(row) for row in store.query(
        "SELECT finding_id, evidence_key FROM findings WHERE rule_id = 'R-CUS-01' AND resolved_at IS NULL")] == [
        (first["finding_id"], "ratio-ge-1.5")]
