"""The checks (oversight/rules/checks.py), each called directly on hand-built issues.

Each test builds a few issues with tests/rules_helpers.py, derives them at the
fixed NOW, then calls one check through a RuleContext as the engine would, and
compares whole Hits (evidence key, evidence and message) for the hit cases.
"""
from __future__ import annotations

import pytest

from oversight.config import FieldsConfig, ProcessConfig
from oversight.rules.checks import CheckResult, Hit, RuleContext, check_sprint_at_risk, join_words, label
from oversight.store.db import Store
from tests.rules_helpers import (
    FIVE_STAGES,
    HOURS_BASIS,
    add_history,
    add_issue,
    at,
    catalogue,
    context,
    derive,
    hits,
    sprint,
    with_project,
)

STARTED = [(-48.0, "To Do", "In Progress")]  # entered development 48 h before NOW
FINISHED = [*STARTED, (-2.0, "In Progress", "Done")]


@pytest.fixture
def hours_process(process: ProcessConfig) -> ProcessConfig:
    """AIL plus OPS: the same three statuses, estimated in original-estimate hours."""
    return with_project(process, "OPS", estimate=HOURS_BASIS)


def _ops(store: Store, hours_process: ProcessConfig, fields: FieldsConfig) -> RuleContext:
    derive(store, hours_process, ("OPS",))
    return context(store, hours_process, fields, project="OPS")


# --- missing_estimate ---------------------------------------------------------------------


def test_missing_estimate_on_a_points_project(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED)
    add_issue(store, "AIL-2", status="In Progress", changes=STARTED, original_h=8)  # hours aren't AIL's basis
    add_issue(store, "AIL-3", status="In Progress", changes=STARTED, points=3)
    add_issue(store, "AIL-4")  # never started
    derive(store, process)
    ctx = context(store, process, fields)

    expected = Hit(
        "no-estimate", {"started_at": at(-48), "basis": "points"}, "Work started with no story-point estimate"
    )
    assert hits(ctx, "missing_estimate", "AIL-1") == [expected]
    assert hits(ctx, "missing_estimate", "AIL-2") == [expected]
    assert hits(ctx, "missing_estimate", "AIL-3") == []
    assert hits(ctx, "missing_estimate", "AIL-4") == []


def test_missing_estimate_on_an_hours_project(
    store: Store, hours_process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "OPS-1", status="Done", changes=FINISHED)
    add_issue(store, "OPS-2", status="In Progress", changes=STARTED, points=3)  # points aren't OPS's basis
    add_issue(store, "OPS-3", status="In Progress", changes=STARTED, original_h=6)
    ctx = _ops(store, hours_process, fields)

    expected = Hit("no-estimate", {"started_at": at(-48), "basis": "hours"}, "Work started with no original estimate")
    assert hits(ctx, "missing_estimate", "OPS-1") == [expected]
    assert hits(ctx, "missing_estimate", "OPS-2") == [expected]
    assert hits(ctx, "missing_estimate", "OPS-3") == []


# --- estimate_after_start -------------------------------------------------------------------


def test_estimate_after_start_on_a_points_project(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    def pointed(key: str, set_at: float | None) -> None:
        history = [] if set_at is None else [(set_at, "story_points", None, "3")]
        add_issue(store, key, status="In Progress", changes=STARTED, points=3, field_changes=history)

    pointed("AIL-1", -40)  # 8 h after the start
    pointed("AIL-2", -47.5)  # inside the hour's grace
    pointed("AIL-3", -60)  # before the start
    pointed("AIL-4", None)  # set at creation, no history
    add_issue(store, "AIL-5", status="In Progress", changes=STARTED)  # never set
    derive(store, process)
    ctx = context(store, process, fields)

    # The evidence holds the grace it was found under, so a finding a later setting closed still says what it crossed.
    assert hits(ctx, "estimate_after_start", "AIL-1", grace_hours=1) == [
        Hit(
            "estimate-after-start",
            {"started_at": at(-48), "estimate_set_at": at(-40), "hours_after": 8.0, "grace_hours": 1.0},
            "Estimate first set 8.0 h after work started",
        )
    ]
    for key in ("AIL-2", "AIL-3", "AIL-4", "AIL-5"):
        assert hits(ctx, "estimate_after_start", key, grace_hours=1) == [], key
    [default] = hits(ctx, "estimate_after_start", "AIL-1")  # the grace defaults to 1 h
    assert default.evidence["grace_hours"] == 1.0
    assert hits(ctx, "estimate_after_start", "AIL-1", grace_hours=7.5)[0].evidence["grace_hours"] == 7.5
    assert hits(ctx, "estimate_after_start", "AIL-1", grace_hours=8) == []  # exactly the grace is fine


def test_estimate_after_start_on_an_hours_project(
    store: Store, hours_process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "OPS-1", status="In Progress", changes=STARTED, original_h=10,
              field_changes=[(0, "original_estimate", None, "36000")])  # 48 h after the start
    # Story-point history means nothing on an hours project: the original estimate was there from creation.
    add_issue(store, "OPS-2", status="In Progress", changes=STARTED, original_h=10, points=2,
              field_changes=[(-10, "story_points", None, "2")])
    ctx = _ops(store, hours_process, fields)

    assert hits(ctx, "estimate_after_start", "OPS-1") == [
        Hit(
            "estimate-after-start",
            {"started_at": at(-48), "estimate_set_at": at(0), "hours_after": 48.0, "grace_hours": 1.0},
            "Estimate first set 2.0 days after work started",
        )
    ]
    assert hits(ctx, "estimate_after_start", "OPS-2") == []


# --- effort_ratio_at_least --------------------------------------------------------------------


def test_effort_ratio_on_a_points_project(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    add_issue(store, "AIL-1", status="Done", changes=FINISHED, points=2, spent_h=32)  # 2.0× of 16 h
    add_issue(store, "AIL-2", status="Done", changes=FINISHED, points=2, spent_h=20)  # 1.25×
    add_issue(store, "AIL-3", status="Done", changes=FINISHED, original_h=4, spent_h=40)  # no points: no ratio
    add_issue(store, "AIL-4", status="Done", changes=FINISHED, points=1, worklogs=[(-30, 6), (-20, 6)])  # 1.5×
    derive(store, process)
    ctx = context(store, process, fields)

    assert hits(ctx, "effort_ratio_at_least", "AIL-1", threshold=1.5) == [
        Hit(
            "ratio-ge-1.5",
            {"effort_ratio": 2.0, "estimate_h": 16.0, "time_spent_h": 32.0, "threshold": 1.5},
            "Logged 32.0 h against an estimate of 16.0 h (2.00×; threshold 1.5×)",
        )
    ]
    assert hits(ctx, "effort_ratio_at_least", "AIL-2", threshold=1.5) == []
    assert hits(ctx, "effort_ratio_at_least", "AIL-3", threshold=1.5) == []
    # The threshold is inclusive; time spent is Jira's timespent, here the worklogs' sum.
    assert [h.message for h in hits(ctx, "effort_ratio_at_least", "AIL-4", threshold=1.5)] == [
        "Logged 12.0 h against an estimate of 8.0 h (1.50×; threshold 1.5×)"
    ]
    assert [h.evidence_key for h in hits(ctx, "effort_ratio_at_least", "AIL-2", threshold=0.8)] == ["ratio-ge-0.8"]


def test_effort_ratio_on_an_hours_project(store: Store, hours_process: ProcessConfig, fields: FieldsConfig) -> None:
    add_issue(store, "OPS-1", status="Done", changes=FINISHED, original_h=10, spent_h=15)  # exactly 1.5×
    add_issue(store, "OPS-2", status="Done", changes=FINISHED, original_h=10, spent_h=14)
    add_issue(store, "OPS-3", status="Done", changes=FINISHED, points=1, spent_h=40)  # no original estimate
    ctx = _ops(store, hours_process, fields)

    assert hits(ctx, "effort_ratio_at_least", "OPS-1", threshold=1.5) == [
        Hit(
            "ratio-ge-1.5",
            {"effort_ratio": 1.5, "estimate_h": 10.0, "time_spent_h": 15.0, "threshold": 1.5},
            "Logged 15.0 h against an estimate of 10.0 h (1.50×; threshold 1.5×)",
        )
    ]
    assert hits(ctx, "effort_ratio_at_least", "OPS-2", threshold=1.5) == []
    assert hits(ctx, "effort_ratio_at_least", "OPS-3", threshold=1.5) == []


# --- entered_stage_without_evidence, skipped_stage, reopened ------------------------------------


def test_entered_stage_without_evidence(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    reviewed = with_project(process, status_to_stage=FIVE_STAGES, evidence={"code_review": {"status": "Code Review"}})
    add_issue(store, "AIL-1", status="Testing", changes=[*STARTED, (-20, "In Progress", "Testing")])
    add_issue(store, "AIL-2", status="Testing",
              changes=[*STARTED, (-30, "In Progress", "Code Review"), (-20, "Code Review", "Testing")])
    add_issue(store, "AIL-3", status="Code Review", changes=[*STARTED, (-30, "In Progress", "Code Review")])
    # Reviewed only after first reaching testing: that first entry still had no review before it.
    add_issue(store, "AIL-4", status="Testing", changes=[
        *STARTED, (-30, "In Progress", "Testing"), (-20, "Testing", "Code Review"), (-10, "Code Review", "Testing"),
    ])
    derive(store, reviewed)
    ctx = context(store, reviewed, fields)
    params = {"stage": "testing", "evidence": "code_review"}

    assert hits(ctx, "entered_stage_without_evidence", "AIL-1", **params) == [
        Hit(
            "testing-2",  # the interval's seq: To Do 0, In Progress 1, Testing 2
            {"stage": "testing", "entered_at": at(-20), "evidence": "code_review", "status": "Testing"},
            "Entered testing (Testing) with no code review recorded before it",
        )
    ]
    assert hits(ctx, "entered_stage_without_evidence", "AIL-2", **params) == []
    assert hits(ctx, "entered_stage_without_evidence", "AIL-3", **params) == []
    assert [h.evidence_key for h in hits(ctx, "entered_stage_without_evidence", "AIL-4", **params)] == ["testing-2"]


def test_skipped_stage_one_hit_per_forward_jump(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    add_issue(store, "AIL-1", status="Done", changes=[(-10, "To Do", "Done")])
    add_issue(store, "AIL-2", status="Done", changes=FINISHED)
    add_issue(store, "AIL-3", status="Done",
              changes=[(-30, "To Do", "Done"), (-20, "Done", "To Do"), (-10, "To Do", "Done")])
    derive(store, process)
    ctx = context(store, process, fields)

    assert hits(ctx, "skipped_stage", "AIL-1") == [
        Hit(
            "skip-1",
            {"changelog_id": "1", "from": "To Do", "to": "Done", "skipped": ["development"], "changed_at": at(-10)},
            "To Do → Done skipped development",
        )
    ]
    assert hits(ctx, "skipped_stage", "AIL-2") == []
    assert [h.evidence_key for h in hits(ctx, "skipped_stage", "AIL-3")] == ["skip-1", "skip-3"]


def test_skipped_stage_names_every_stage_jumped(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    five = with_project(process, status_to_stage=FIVE_STAGES)
    add_issue(store, "AIL-1", status="Done", changes=FINISHED)
    derive(store, five)

    [hit] = hits(context(store, five, fields), "skipped_stage", "AIL-1")
    assert hit.evidence["skipped"] == ["code_review", "testing"]
    assert hit.message == "In Progress → Done skipped code review and testing"


def test_reopened_one_hit_per_reopen(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=[*FINISHED, (-1, "Done", "In Progress")])
    add_issue(store, "AIL-2", status="In Progress", changes=[
        *FINISHED, (-1.5, "Done", "To Do"), (-1, "To Do", "Done"), (-0.5, "Done", "In Progress"),
    ])
    add_issue(store, "AIL-3", status="Done", changes=FINISHED)
    derive(store, process)
    ctx = context(store, process, fields)

    assert hits(ctx, "reopened", "AIL-1") == [
        Hit(
            "reopen-3",
            {"changelog_id": "3", "changed_at": at(-1), "from": "Done", "to": "In Progress"},
            "Reopened: Done → In Progress",
        )
    ]
    assert [(h.evidence_key, h.message) for h in hits(ctx, "reopened", "AIL-2")] == [
        ("reopen-3", "Reopened: Done → To Do"),
        ("reopen-5", "Reopened: Done → In Progress"),
    ]
    assert hits(ctx, "reopened", "AIL-3") == []


def test_a_move_into_a_done_status_missing_from_the_statuses_table_is_not_a_reopen(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    # Closed was renamed or moved, so the statuses table lacks it and no stage maps it;
    # it is the issue's current status and Jira reports that as done. Derive's lookup
    # falls back to the issue's own category (reopen_count 0), and the rule must agree.
    add_issue(store, "AIL-1", status="Closed", changes=[*FINISHED, (-1, "Done", "Closed")])
    store.conn.execute("UPDATE issues SET status_category = 'done' WHERE issue_key = 'AIL-1'")
    # Moving out of Done into a status that is unknown and not the current one stays a reopen.
    add_issue(store, "AIL-2", status="In Progress", changes=[*FINISHED, (-1, "Done", "Parked"), (-0.5, "Parked", "In Progress")])
    derive(store, process)
    ctx = context(store, process, fields)

    assert store.query_one("SELECT reopen_count FROM issue_metrics WHERE issue_key = 'AIL-1'")["reopen_count"] == 0
    assert hits(ctx, "reopened", "AIL-1") == []
    assert [(h.evidence_key, h.message) for h in hits(ctx, "reopened", "AIL-2")] == [("reopen-3", "Reopened: Done → Parked")]


# --- in_progress_unassigned -----------------------------------------------------------------------


def test_in_progress_unassigned(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=[(-30, "To Do", "In Progress")], assignee=None)
    add_issue(store, "AIL-2", status="In Progress", changes=[(-30, "To Do", "In Progress")])
    add_issue(store, "AIL-3", assignee=None)  # unassigned, but not in progress
    add_issue(store, "AIL-4", status="In Progress", changes=[(-72, "To Do", "In Progress")], assignee=None)
    derive(store, process)
    ctx = context(store, process, fields)

    assert hits(ctx, "in_progress_unassigned", "AIL-1") == [
        Hit(
            "unassigned",
            {"status": "In Progress", "status_age_h": 30.0},
            "In 'In Progress' with nobody assigned for 30.0 h",
        )
    ]
    assert hits(ctx, "in_progress_unassigned", "AIL-2") == []
    assert hits(ctx, "in_progress_unassigned", "AIL-3") == []
    assert [h.message for h in hits(ctx, "in_progress_unassigned", "AIL-4")] == [
        "In 'In Progress' with nobody assigned for 3.0 days"
    ]


# --- stuck_in_status and no_evidence_after_percentile (thresholds from the project's history) ------


def test_stuck_in_status_against_the_projects_own_history(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_history(store, "In Progress", [10, 20, 30, 40])  # p90 by hand: 30 + 0.7 * 10 = 37 h
    add_issue(store, "AIL-1", status="In Progress", changes=[(-50, "To Do", "In Progress")])
    add_issue(store, "AIL-2", status="In Progress", changes=[(-20, "To Do", "In Progress")])
    derive(store, process)
    ctx = context(store, process, fields, catalogue(min_history=4), rule="W-AGE-01")

    assert hits(ctx, "stuck_in_status", "AIL-1", pct=90) == [
        Hit(
            "stuck-In Progress",
            {"status": "In Progress", "age_h": 50.0, "threshold_h": 37.0, "pct": 90.0, "history_n": 4},
            "In 'In Progress' for 2.1 days; the project's p90 for 'In Progress' is 37.0 h",
        )
    ]
    assert hits(ctx, "stuck_in_status", "AIL-2", pct=90) == []
    assert ctx.notes == {}


def test_stuck_in_status_is_silent_with_a_note_when_history_is_thin(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_history(store, "In Progress", [10, 20, 30])
    add_issue(store, "AIL-1", status="In Progress", created=-600, changes=[(-500, "To Do", "In Progress")])
    add_issue(store, "AIL-2", status="In Progress", created=-600, changes=[(-400, "To Do", "In Progress")])
    derive(store, process)
    ctx = context(store, process, fields, catalogue(min_history=4, history_days=90), rule="W-AGE-01")

    assert hits(ctx, "stuck_in_status", "AIL-1", pct=90) == []
    assert hits(ctx, "stuck_in_status", "AIL-2", pct=90) == []
    # One note for the rule, however many issues met the thin history.
    assert ctx.notes == {"W-AGE-01": "In Progress: fewer than 4 closed intervals in 90 days, no threshold"}


def _review_label_process(process: ProcessConfig) -> ProcessConfig:
    return with_project(process, status_to_stage=FIVE_STAGES, evidence={"code_review": {"label": "code-reviewed"}})


def test_no_evidence_after_percentile(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    labelled = _review_label_process(process)
    add_history(store, "In Progress", [10, 20, 30, 40])  # development p75 by hand: 30 + 0.25 * 10 = 32.5 h
    add_issue(store, "AIL-1", status="In Progress", changes=[(-40, "To Do", "In Progress")])
    add_issue(store, "AIL-2", status="In Progress", changes=[(-40, "To Do", "In Progress")], labels=["code-reviewed"])
    add_issue(store, "AIL-3", status="In Progress", changes=[(-20, "To Do", "In Progress")])
    add_issue(store, "AIL-4", status="Testing", created=-200)  # not in the stage at all
    derive(store, labelled)
    ctx = context(store, labelled, fields, catalogue(min_history=4), rule="W-REV-01")
    params = {"stage": "development", "evidence": "code_review", "pct": 75}

    assert hits(ctx, "no_evidence_after_percentile", "AIL-1", **params) == [
        Hit(
            "no-code_review-after-p75",
            {"stage": "development", "evidence": "code_review", "hours_in_stage": 40.0, "threshold_h": 32.5,
             "pct": 75.0},
            "In development for 40.0 h (the project's p75 is 32.5 h) with no code review recorded",
        )
    ]
    for key in ("AIL-2", "AIL-3", "AIL-4"):
        assert hits(ctx, "no_evidence_after_percentile", key, **params) == [], key
    assert ctx.notes == {}


def test_no_evidence_after_percentile_is_silent_with_a_note_when_history_is_thin(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    labelled = _review_label_process(process)
    add_history(store, "In Progress", [10, 20, 30, 40])
    add_issue(store, "AIL-1", status="In Progress", created=-500, changes=[(-400, "To Do", "In Progress")])
    derive(store, labelled)
    ctx = context(store, labelled, fields, catalogue(min_history=5), rule="W-REV-01")

    found = hits(ctx, "no_evidence_after_percentile", "AIL-1", stage="development", evidence="code_review", pct=75)
    assert found == []
    assert ctx.notes == {"W-REV-01": "development: fewer than 5 closed intervals in 180 days, no threshold"}


# --- unestimated_in_active_sprint ---------------------------------------------------------------


def test_unestimated_in_active_sprint(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    active, closed = sprint(4401), sprint(4400, state="closed", start=-480, end=-240)
    add_issue(store, "AIL-1", sprints=[closed, active])
    add_issue(store, "AIL-2", sprints=[active], points=3)
    add_issue(store, "AIL-3", sprints=[closed])  # only a finished sprint
    add_issue(store, "AIL-4")  # in no sprint
    derive(store, process)
    ctx = context(store, process, fields)

    assert hits(ctx, "unestimated_in_active_sprint", "AIL-1") == [
        Hit(
            "unestimated-sprint-4401",
            {"sprint_id": 4401, "sprint_name": "Sprint 4401", "basis": "points"},
            "In the active sprint Sprint 4401 with no story-point estimate",
        )
    ]
    for key in ("AIL-2", "AIL-3", "AIL-4"):
        assert hits(ctx, "unestimated_in_active_sprint", key) == [], key


def test_unestimated_in_active_sprint_on_an_hours_project(
    store: Store, hours_process: ProcessConfig, fields: FieldsConfig
) -> None:
    board = [sprint(4401, name="Ops 12")]
    add_issue(store, "OPS-1", sprints=board, points=3)  # points aren't the estimate on OPS
    add_issue(store, "OPS-2", sprints=board, original_h=6)
    ctx = _ops(store, hours_process, fields)

    assert [h.message for h in hits(ctx, "unestimated_in_active_sprint", "OPS-1")] == [
        "In the active sprint Ops 12 with no original estimate"
    ]
    assert hits(ctx, "unestimated_in_active_sprint", "OPS-2") == []


# --- sprint_at_risk -----------------------------------------------------------------------------


def _sprint_of_five(store: Store, *, not_started: int, end: float) -> None:
    """Five issues in active sprint 4401, ending `end` hours from now: `not_started` of
    them still in To Do, then issues in progress, and the fifth done."""
    board = [sprint(4401, end=end)]
    for n in range(1, 6):
        if n <= not_started:
            add_issue(store, f"AIL-{n}", sprints=board, points=1)
        elif n < 5:
            add_issue(store, f"AIL-{n}", status="In Progress", changes=STARTED, sprints=board, points=1)
        else:
            add_issue(store, f"AIL-{n}", status="Done", changes=FINISHED, sprints=board, points=1)


def _at_risk(store: Store, process: ProcessConfig, fields: FieldsConfig) -> CheckResult:
    derive(store, process)
    ctx = context(store, process, fields)
    [active] = ctx.active_sprints()
    return check_sprint_at_risk(active, ctx, {"days_left": 2, "not_started_share": 0.3})


def test_sprint_at_risk_at_40_percent_not_started_with_2_days_left(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _sprint_of_five(store, not_started=2, end=48)

    # The evidence holds the limits it was found under beside what was measured (days left, the share not started).
    assert _at_risk(store, process, fields) == Hit(
        "sprint-4401-at-risk",
        {"sprint_id": 4401, "name": "Sprint 4401", "end_date": at(48), "days_left": 2.0, "issues": 5,
         "not_started": 2, "not_done": 4, "overdue": False, "share": 0.4, "threshold_days_left": 2.0,
         "threshold_share": 0.3},
        "Sprint 4401 ends in 2.0 days with 2 of 5 issues not started (40%)",
    )


def test_sprint_not_at_risk_at_20_percent_not_started(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _sprint_of_five(store, not_started=1, end=48)

    assert _at_risk(store, process, fields) is None


def test_sprint_not_at_risk_with_5_days_left(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    _sprint_of_five(store, not_started=2, end=5 * 24)

    assert _at_risk(store, process, fields) is None


def test_sprint_past_its_end_is_at_risk_and_epics_are_not_counted(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _sprint_of_five(store, not_started=2, end=-24)
    add_issue(store, "AIL-6", issue_type="Epic", sprints=[sprint(4401, end=-24)])  # a container, never "started"

    result = _at_risk(store, process, fields)

    assert isinstance(result, Hit)
    assert result.message == "Sprint 4401 ended 1.0 days ago with 4 of 5 issues not done"
    assert (result.evidence["days_left"], result.evidence["issues"], result.evidence["not_done"]) == (-1.0, 5, 4)
    assert result.evidence["overdue"] is True


def test_a_sprint_past_its_end_with_work_not_done_is_at_risk_whatever_share_started(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    """18 Nov 2026: AILED's sprint ended on 16 Nov and is still open with 2 of 17 tickets in progress and none in To Do,
    and the Portfolio said "On track": a share of 0 not started is under any threshold. Past its end, work not done is
    the risk itself."""
    _sprint_of_five(store, not_started=0, end=-48)  # four in progress, one done, none in To Do

    result = _at_risk(store, process, fields)

    assert result == Hit(
        "sprint-4401-at-risk",  # the same finding as before the end: one evidence key
        {"sprint_id": 4401, "name": "Sprint 4401", "end_date": at(-48), "days_left": -2.0, "issues": 5,
         "not_started": 0, "not_done": 4, "overdue": True, "share": 0.0, "threshold_days_left": 2.0,
         "threshold_share": 0.3},
        "Sprint 4401 ended 2.0 days ago with 4 of 5 issues not done",
    )


def test_a_sprint_past_its_end_with_everything_done_is_not_at_risk(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    board = [sprint(4401, end=-48)]  # still open in Jira, but nothing is left to do
    for n in range(1, 4):
        add_issue(store, f"AIL-{n}", status="Done", changes=FINISHED, sprints=board, points=1)

    assert _at_risk(store, process, fields) is None


def test_sprint_at_risk_skips_issues_derive_has_not_reached(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    _sprint_of_five(store, not_started=1, end=48)
    derive(store, process)
    # Synced after derive: in progress, but with no metrics yet it can't be told started or not.
    add_issue(store, "AIL-6", status="In Progress", changes=STARTED, sprints=[sprint(4401, end=48)], points=1)
    ctx = context(store, process, fields)
    [active] = ctx.active_sprints()

    assert "AIL-6" in active.issue_keys
    # 1 of the 5 derived issues (20%), not 2 of 6 (33%, over the 30% share).
    assert check_sprint_at_risk(active, ctx, {"days_left": 2, "not_started_share": 0.3}) is None


# --- how names read -------------------------------------------------------------------------------


def test_stage_names_and_word_lists_read_as_prose() -> None:
    assert [label(n) for n in ("code_review", "dev_signoff", "test_signoff", "development")] == [
        "code review", "dev sign-off", "test sign-off", "development",
    ]
    assert [join_words(w) for w in ([], ["testing"], ["a", "b"], ["a", "b", "c"])] == [
        "", "testing", "a and b", "a, b and c",
    ]
