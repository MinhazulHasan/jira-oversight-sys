from __future__ import annotations

import re

import pytest

from oversight.config import FieldsConfig, ProcessConfig
from oversight.rules.checks import CHECKS, Hit, RuleContext
from oversight.store.db import Store
from tests.rules_helpers import FIVE_STAGES, HOURS_BASIS, add_issue, at, context, derive, hits, sprint, with_project

MINUTE = 1 / 60  # hours

IN_REVIEW = [(-100.0, "To Do", "In Progress"), (-80.0, "In Progress", "Code Review")]


@pytest.fixture
def five(process: ProcessConfig) -> ProcessConfig:
    return with_project(process, "AIL", status_to_stage=FIVE_STAGES)


def _ctx(store: Store, process: ProcessConfig, fields: FieldsConfig, project: str = "AIL") -> RuleContext:
    derive(store, process, (project,))
    return context(store, process, fields, project=project, rule="R-CUS-01")


def test_the_general_checks_are_registered() -> None:
    assert {"time_in_status_over", "open_longer_than", "no_update_for", "missing_field"} <= set(CHECKS)


def test_time_in_one_of_the_statuses_over_the_limit_is_a_hit(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="Code Review", changes=IN_REVIEW)
    add_issue(store, "AIL-2", status="Code Review", changes=[(-60.0, "To Do", "In Progress"),
                                                              (-50.0, "In Progress", "Code Review")])  # 50 h
    add_issue(store, "AIL-3", status="In Progress", changes=[(-200.0, "To Do", "In Progress")])  # not a status named
    ctx = _ctx(store, five, fields)

    assert hits(ctx, "time_in_status_over", "AIL-1", statuses=["Code Review"], hours=72.0) == [Hit(
        "in-status-Code Review", {"status": "Code Review", "age_h": 80.0, "threshold_h": 72.0},
        "In 'Code Review' for 3.3 days; the limit is 3 days",
    )]
    assert hits(ctx, "time_in_status_over", "AIL-2", statuses=["Code Review"], hours=72.0) == []
    assert hits(ctx, "time_in_status_over", "AIL-3", statuses=["Code Review", "Testing"], hours=72.0) == []
    assert hits(ctx, "time_in_status_over", "AIL-2", statuses=["code review"], hours=36.0) == [Hit(
        "in-status-Code Review", {"status": "Code Review", "age_h": 50.0, "threshold_h": 36.0},
        "In 'Code Review' for 2.1 days; the limit is 36 h",
    )]
    assert ctx.notes == {}


def test_the_finding_is_named_by_the_status_never_by_the_limit(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="Code Review", changes=IN_REVIEW)
    ctx = _ctx(store, five, fields)
    keys = {hit.evidence_key for hours in (24.0, 48.0, 72.0)
            for hit in hits(ctx, "time_in_status_over", "AIL-1", statuses=["Code Review", "Testing"], hours=hours)}
    assert keys == {"in-status-Code Review"}


def test_a_status_the_board_no_longer_has_is_said_on_the_run_line_as_what_happens(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="Code Review", changes=IN_REVIEW)
    add_issue(store, "AIL-2", status="Peer Review", changes=[(-100.0, "To Do", "Peer Review")])
    ctx = _ctx(store, five, fields)
    assert "peer review" not in ctx.status_names()
    assert hits(ctx, "time_in_status_over", "AIL-1", statuses=["Peer Review"], hours=1.0) == []
    [held] = hits(ctx, "time_in_status_over", "AIL-2", statuses=["Peer Review"], hours=1.0)
    assert held.evidence_key == "in-status-Peer Review"
    assert hits(ctx, "time_in_status_over", "AIL-1", statuses=["Peer Review", "Code Review"], hours=1.0)
    assert ctx.notes == {"R-CUS-01": "'Peer Review' is no longer a status on this board: only work the last sync still "
                                     "shows in it is checked"}


def test_a_board_with_no_statuses_synced_yet_checks_every_status_named(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="Code Review", changes=IN_REVIEW)
    store.conn.execute("DELETE FROM statuses")  # before the board's first sync of its statuses
    ctx = _ctx(store, five, fields)
    assert [hit.evidence_key for hit in hits(ctx, "time_in_status_over", "AIL-1", statuses=["Code Review"],
                                             hours=72.0)] == ["in-status-Code Review"]
    assert ctx.notes == {}


def test_the_time_is_the_current_stay_in_the_status_when_work_came_back_to_it(
    store: Store, five: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="Code Review", changes=[
        (-200.0, "To Do", "In Progress"), (-150.0, "In Progress", "Code Review"),
        (-60.0, "Code Review", "In Progress"), (-10.0, "In Progress", "Code Review")])
    ctx = _ctx(store, five, fields)
    assert hits(ctx, "time_in_status_over", "AIL-1", statuses=["Code Review"], hours=72.0) == []
    [hit] = hits(ctx, "time_in_status_over", "AIL-1", statuses=["Code Review"], hours=8.0)
    assert hit.evidence == {"status": "Code Review", "age_h": 10.0, "threshold_h": 8.0}


@pytest.mark.parametrize(("hours", "past", "message"), [
    (72.0, 0.0, None),  # at the limit exactly: not past it
    (72.0, MINUTE, "In 'Code Review' for more than 3 days; the limit is 3 days"),
    (72.0, 1.0, "In 'Code Review' for more than 3 days; the limit is 3 days"),  # 3.04 days: "3.0 days" read as 3
    (72.0, 2.5, "In 'Code Review' for 3.1 days; the limit is 3 days"),
    (29.0, MINUTE, "In 'Code Review' for more than 29 h; the limit is 29 h"),
    (29.0, 0.1, "In 'Code Review' for 29.1 h; the limit is 29 h"),
    (47.99, MINUTE, "In 'Code Review' for more than 48 h; the limit is 48 h"),  # "2.0 days" would read as 48 h
])
def test_time_in_a_status_at_and_just_past_the_limit(
    store: Store, five: ProcessConfig, fields: FieldsConfig, hours: float, past: float, message: str | None
) -> None:
    add_issue(store, "AIL-1", status="Code Review", changes=[(-300.0, "To Do", "Code Review")])
    derive(store, five)
    store.conn.execute("UPDATE issue_metrics SET status_age_h = ? WHERE issue_key = 'AIL-1'", (hours + past,))
    ctx = context(store, five, fields, rule="R-CUS-01")
    found = hits(ctx, "time_in_status_over", "AIL-1", statuses=["Code Review"], hours=hours)
    assert [hit.message for hit in found] == ([] if message is None else [message])


@pytest.mark.parametrize("check", ["open_longer_than", "no_update_for"])
@pytest.mark.parametrize(("past", "said"), [
    (0.0, None),  # at the limit exactly: not past it
    (MINUTE, "more than 6 days"),
    (2.0, "more than 6 days"),  # 6.08 days: no tenth gone by yet
    (3.0, "6.1 days"),
    (23.0, "6.9 days"),
    (24.0 + MINUTE, "7 days"),
])
def test_days_at_and_just_past_the_limit(
    store: Store, process: ProcessConfig, fields: FieldsConfig, check: str, past: float, said: str | None
) -> None:
    age = -(6 * 24.0 + past)
    add_issue(store, "AIL-1", status="In Progress", created=age, changes=[(age, "To Do", "In Progress")], updated=age)
    ctx = _ctx(store, process, fields)
    found = hits(ctx, check, "AIL-1", days=6.0)
    if said is None:
        assert found == []
        return
    [hit] = found
    lead = "Open for" if check == "open_longer_than" else "No change in Jira for"
    assert hit.message == f"{lead} {said}; the limit is 6 days"
    shown = re.search(r"for (?:more than )?([0-9.]+) days?;", hit.message)
    assert shown is not None and (hit.message.startswith(f"{lead} more than") or float(shown.group(1)) > 6.0)


def test_work_with_no_updated_time_is_never_called_idle(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=[(-400.0, "To Do", "In Progress")])
    store.conn.execute("UPDATE issues SET updated_at = NULL WHERE issue_key = 'AIL-1'")
    ctx = _ctx(store, process, fields)
    assert hits(ctx, "no_update_for", "AIL-1", days=1.0) == []


def test_work_open_longer_than_the_limit_is_a_hit(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    add_issue(store, "AIL-1", status="In Progress", created=-41 * 24.0, changes=[(-24.0, "To Do", "In Progress")])
    add_issue(store, "AIL-2", status="To Do", created=-20 * 24.0)
    add_issue(store, "AIL-3", status="Done", created=-90 * 24.0, changes=[(-24.0, "To Do", "Done")])
    add_issue(store, "AIL-4", status="To Do", created=-36.0)
    ctx = _ctx(store, process, fields)

    assert hits(ctx, "open_longer_than", "AIL-1", days=30.0) == [Hit(
        "open-too-long", {"created_at": at(-41 * 24.0), "open_days": 41.0, "threshold_days": 30.0},
        "Open for 41 days; the limit is 30 days",
    )]
    assert hits(ctx, "open_longer_than", "AIL-2", days=30.0) == []
    assert hits(ctx, "open_longer_than", "AIL-3", days=30.0) == []  # done work is never "open too long"
    assert hits(ctx, "open_longer_than", "AIL-4", days=1.0)[0].message == "Open for 1.5 days; the limit is 1 day"
    assert {hit.evidence_key for days in (1.0, 10.0) for hit in hits(ctx, "open_longer_than", "AIL-1", days=days)} == {
        "open-too-long"}


def test_open_work_jira_has_not_changed_for_too_long_is_a_hit(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=[(-400.0, "To Do", "In Progress")], updated=-12 * 24.0)
    add_issue(store, "AIL-2", status="In Progress", changes=[(-400.0, "To Do", "In Progress")], updated=-2 * 24.0)
    add_issue(store, "AIL-3", status="Done", changes=[(-400.0, "To Do", "Done")], updated=-300.0)
    ctx = _ctx(store, process, fields)

    assert hits(ctx, "no_update_for", "AIL-1", days=7.0) == [Hit(
        "no-update", {"updated_at": at(-12 * 24.0), "idle_days": 12.0, "threshold_days": 7.0},
        "No change in Jira for 12 days; the limit is 7 days",
    )]
    assert hits(ctx, "no_update_for", "AIL-2", days=7.0) == []
    assert hits(ctx, "no_update_for", "AIL-3", days=7.0) == []


def test_a_missing_assignee_estimate_epic_or_sprint_is_a_hit(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    started = [(-48.0, "To Do", "In Progress")]
    add_issue(store, "AIL-1", status="In Progress", changes=started, assignee=None)
    add_issue(store, "AIL-2", status="In Progress", changes=started, points=3, epic="AIL-100",
              sprints=[sprint(7)])
    add_issue(store, "AIL-3", status="In Progress", changes=started, parent="AIL-2")  # its parent's epic is its own
    add_issue(store, "AIL-4", status="In Progress", changes=started, parent="AIL-1")  # neither has an epic
    ctx = _ctx(store, process, fields)

    def found(key: str, field: str) -> list[Hit]:
        return hits(ctx, "missing_field", key, field=field)

    assert found("AIL-1", "assignee") == [Hit("missing-assignee", {"field": "assignee", "status": "In Progress"},
                                              "No assignee")]
    assert found("AIL-2", "assignee") == []
    assert found("AIL-1", "estimate") == [Hit("missing-estimate", {"field": "estimate", "status": "In Progress"},
                                              "No story-point estimate")]
    assert found("AIL-2", "estimate") == []
    assert found("AIL-1", "epic") == [Hit("missing-epic", {"field": "epic", "status": "In Progress"}, "No epic")]
    assert (found("AIL-2", "epic"), found("AIL-3", "epic")) == ([], [])
    assert found("AIL-4", "epic") == [Hit("missing-epic", {"field": "epic", "status": "In Progress"}, "No epic")]
    assert found("AIL-1", "sprint") == [Hit("missing-sprint", {"field": "sprint", "status": "In Progress"},
                                            "Not in a sprint")]
    assert found("AIL-2", "sprint") == []
    assert found("AIL-2", "colour") == []  # a field the registry does not offer: nothing to look for


def test_a_missing_estimate_is_on_the_boards_basis(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    hours = with_project(process, "OPS", estimate=HOURS_BASIS)
    add_issue(store, "OPS-1", status="In Progress", changes=[(-48.0, "To Do", "In Progress")], points=3)
    add_issue(store, "OPS-2", status="In Progress", changes=[(-48.0, "To Do", "In Progress")], original_h=8)
    ctx = _ctx(store, hours, fields, "OPS")
    assert [hit.message for hit in hits(ctx, "missing_field", "OPS-1", field="estimate")] == ["No original estimate"]
    assert hits(ctx, "missing_field", "OPS-2", field="estimate") == []
