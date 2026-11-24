from __future__ import annotations

from typing import Any

import pytest

from oversight.ask.router import CATALOG_NAMES, MAX_CALLS, PERSON_REASON, route
from oversight.llm.provider import Plan, PlanCall

NAMES = list(CATALOG_NAMES)
Expected = list[tuple[str, dict[str, Any]]]


def plan_for(question: str, *, project: str = "AIL", names: list[str] | None = None) -> Plan:
    return route(question, NAMES if names is None else names, project=project)


def calls_of(plan: Plan) -> Expected:
    return [(call.name, call.params) for call in plan.calls]


ROUTING_TABLE: list[tuple[str, Expected]] = [
    ("Where do tickets get stuck most?", [("oversight_time_in_status", {"project": "AIL", "group_by": "status", "period": "90d"})]),
    ("Which epics have the most open breaches?", [("oversight_hot_spots", {"project": "AIL"})]),
    (
        "How much work was estimated before it started?",
        [
            ("oversight_coverage", {"project": "AIL", "field": "estimate", "period": "90d"}),
            ("oversight_list_findings", {"project": "AIL", "rule_id": "R-EST-02", "state": "open"}),
        ],
    ),
    (
        "Which tickets went straight from To Do to Done this month?",
        [("oversight_list_findings", {"project": "AIL", "rule_id": "R-FLOW-02", "state": "all", "period": "30d"})],
    ),
    ("How is effort against estimate trending?", [("oversight_trend", {"project": "AIL", "metric": "median_effort_ratio", "last_n": 12})]),
    ("Which rules can't be measured in Jira today?", [("oversight_rule_status", {"project": "AIL"})]),
    ("What is the median cycle time this quarter?", [("oversight_trend", {"project": "AIL", "metric": "median_cycle_time_h", "last_n": 12})]),
    ("Which tickets have been reopened after Done?", [("oversight_list_findings", {"project": "AIL", "rule_id": "R-FLOW-03", "state": "open"})]),
    ("How much of the active sprint has an estimate?", [("oversight_coverage", {"project": "AIL", "field": "estimate", "period": "sprint"})]),
    ("Which open tickets have burned the most of their estimate?", [("oversight_top_issues", {"project": "AIL", "metric": "effort_ratio"})]),
    ("Which ticket types sit longest in In Progress?", [("oversight_time_in_status", {"project": "AIL", "group_by": "issue_type", "period": "90d"})]),
    (
        "Which tickets reached testing without code review last sprint?",
        [("oversight_list_findings", {"project": "AIL", "rule_id": "R-FLOW-01", "state": "all", "period": "sprint"})],
    ),
    ("What happened to AIL-552?", [("oversight_issue_timeline", {"issue_key": "AIL-552"})]),
    ("Timeline for DEMO-7 please", [("oversight_issue_timeline", {"issue_key": "DEMO-7"})]),
    ("Show the R-EST-01 findings in DEMO", [("oversight_list_findings", {"project": "DEMO", "rule_id": "R-EST-01", "state": "open"})]),
    ("Why is w-age-01 firing so often?", [("oversight_list_findings", {"project": "AIL", "rule_id": "W-AGE-01", "state": "open"})]),
    ("Which resolved findings for R-FLOW-02 do we have?", [("oversight_list_findings", {"project": "AIL", "rule_id": "R-FLOW-02", "state": "resolved"})]),
    ("How are breaches trending over the last 6 weeks?", [("oversight_trend", {"project": "AIL", "metric": "open_breaches", "last_n": 6})]),
    ("Is the reopen rate getting worse?", [("oversight_trend", {"project": "AIL", "metric": "reopen_rate", "last_n": 12})]),
    ("Top 3 hot spots", [("oversight_hot_spots", {"project": "AIL", "n": 3})]),
    ("Which rules can’t be measured?", [("oversight_rule_status", {"project": "AIL"})]),
    ("Which rules have the most open findings?", [("oversight_rule_status", {"project": "AIL"})]),
    ("Which tickets started without an estimate?", [("oversight_list_findings", {"project": "AIL", "rule_id": "R-EST-01", "state": "open"})]),
    (
        "Is there unestimated work in the active sprint?",
        [("oversight_list_findings", {"project": "AIL", "rule_id": "W-EST-01", "state": "all", "period": "sprint"})],
    ),
    ("How many tickets have no estimate?", [("oversight_count_issues", {"project": "AIL", "has_estimate": False})]),
    ("What share of finished work has time logged?", [("oversight_coverage", {"project": "AIL", "field": "time_logged", "period": "90d"})]),
    ("What percentage of tickets have code-review evidence?", [("oversight_coverage", {"project": "AIL", "field": "code_review", "period": "90d"})]),
    ("Top 5 tickets by cycle time", [("oversight_top_issues", {"project": "AIL", "metric": "cycle_time", "n": 5})]),
    ("Which tickets are stuck?", [("oversight_list_findings", {"project": "AIL", "rule_id": "W-AGE-01", "state": "open"})]),
    ("What are the oldest open tickets?", [("oversight_top_issues", {"project": "AIL", "metric": "status_age"})]),
    ("How long do bugs sit in each status this month?", [("oversight_time_in_status", {"project": "AIL", "group_by": "status", "period": "30d"})]),
    ("Which epics wait longest?", [("oversight_time_in_status", {"project": "AIL", "group_by": "epic", "period": "90d"})]),
    ("Which tickets skipped a stage?", [("oversight_list_findings", {"project": "AIL", "rule_id": "R-FLOW-02", "state": "open"})]),
    ("Which tickets were reopened the most?", [("oversight_top_issues", {"project": "AIL", "metric": "reopens"})]),
    ("Which tickets are in progress with nobody assigned?", [("oversight_list_findings", {"project": "AIL", "rule_id": "R-HYG-01", "state": "open"})]),
    ("Is the sprint at risk?", [("oversight_list_findings", {"project": "AIL", "rule_id": "W-SPR-01", "state": "all", "period": "sprint"})]),
    ("How does effort compare with estimates by epic?", [("oversight_effort_vs_estimate", {"project": "AIL", "group_by": "epic", "period": "90d"})]),
    ("Effort versus estimate per sprint", [("oversight_effort_vs_estimate", {"project": "AIL", "group_by": "sprint", "period": "90d"})]),
    ("Which tickets ran furthest over their estimates?", [("oversight_top_issues", {"project": "AIL", "metric": "effort_ratio"})]),
    ("What breaches are open?", [("oversight_list_findings", {"project": "AIL", "severity": "breach", "state": "open"})]),
    ("How many warnings were raised last week?", [("oversight_list_findings", {"project": "AIL", "severity": "warning", "state": "all", "period": "7d"})]),
    ("Show me the open findings", [("oversight_list_findings", {"project": "AIL", "state": "open"})]),
    ("How many bugs were done this month?", [("oversight_count_issues", {"project": "AIL", "status": "Done", "issue_type": "Bug", "period": "30d"})]),
    ("How many tickets are in progress?", [("oversight_count_issues", {"project": "AIL", "state": "in_progress"})]),
    ("Which tickets are in the sprint?", [("oversight_count_issues", {"project": "AIL", "period": "sprint"})]),
]


@pytest.mark.parametrize(("question", "expected"), ROUTING_TABLE, ids=[q for q, _ in ROUTING_TABLE])
def test_routing_table(question: str, expected: Expected) -> None:
    plan = plan_for(question)
    assert plan.understood is True and plan.reason is None
    assert calls_of(plan) == expected
    assert all(isinstance(call, PlanCall) for call in plan.calls)
    assert len(plan.calls) <= MAX_CALLS == 3


def test_params_never_hold_nulls() -> None:
    for question, _expected in ROUTING_TABLE:
        for call in plan_for(question).calls:
            assert None not in call.params.values(), (question, call)


@pytest.mark.parametrize(
    "question",
    [
        "Who is the slowest developer?",
        "Which developer closed the most tickets?",
        "Who has the most open tickets?",
        "Which team member is best at estimating?",
        "Which engineer is the fastest?",
        "Rank people by productivity",
        "Show cycle time per person",
    ],
)
def test_per_person_questions_are_declined_with_the_reason(question: str) -> None:
    plan = plan_for(question)
    assert plan.understood is False
    assert plan.reason == PERSON_REASON == "Ask answers at team level. To see one person's findings, use the Person filter on Findings."
    assert plan.calls == []


@pytest.mark.parametrize("question", ["What's the weather like?", "Tell me a joke", "How are things trending?", "   ", ""])
def test_unmatched_questions_are_not_understood_without_a_reason(question: str) -> None:
    plan = plan_for(question)
    assert plan.understood is False and plan.reason is None and plan.calls == []


def test_the_context_project_is_the_default_and_a_blank_one_is_omitted() -> None:
    assert plan_for("Where do tickets get stuck most?", project="demo").calls[0].params["project"] == "DEMO"
    assert "project" not in plan_for("Where do tickets get stuck most?", project="").calls[0].params
    assert plan_for("What happened to AIL-552?", project="").calls[0].params == {"issue_key": "AIL-552"}


def test_a_project_key_in_the_question_wins() -> None:
    assert plan_for("Where do tickets get stuck most in DEMO?").calls[0].params["project"] == "DEMO"
    assert plan_for("In OPS_1, which epics have the most breaches?").calls[0].params["project"] == "OPS_1"


def test_shouted_words_and_rule_ids_are_not_project_keys() -> None:
    assert plan_for("How many tickets are TO DO?").calls[0].params == {"project": "AIL", "status": "To Do"}
    assert plan_for("Show me R-FLOW-02 findings").calls[0].params["project"] == "AIL"
    assert plan_for("What is the P90 time in status?").calls[0].params["project"] == "AIL"


@pytest.mark.parametrize(
    ("question", "n"),
    [
        ("Top 5 tickets by cycle time", 5),
        ("top five tickets by cycle time", 5),
        ("Top 99 tickets by cycle time", 20),
        ("Top 0 tickets by cycle time", 1),
    ],
)
def test_top_n_is_parsed_and_capped_for_top_issues(question: str, n: int) -> None:
    assert plan_for(question).calls[0].params["n"] == n


def test_top_n_caps_follow_each_tool() -> None:
    assert plan_for("Top 99 hot spots").calls[0].params["n"] == 10
    assert plan_for("Top 99 breaches").calls[0].params["n"] == 50
    assert "n" not in plan_for("Which epics have the most open breaches?").calls[0].params


@pytest.mark.parametrize(
    ("phrase", "period"),
    [
        ("last sprint", "sprint"),
        ("this sprint", "sprint"),
        ("in the current sprint", "sprint"),
        ("this week", "7d"),
        ("in the last 7 days", "7d"),
        ("over the past seven days", "7d"),
        ("this month", "30d"),
        ("in the last 30 days", "30d"),
        ("in the past month", "30d"),
        ("this quarter", "90d"),
        ("in the last 90 days", "90d"),
        ("in the last three months", "90d"),
        ("ever", "all"),
        ("overall", "all"),
    ],
)
def test_periods_are_parsed_from_the_question(phrase: str, period: str) -> None:
    (call,) = plan_for(f"Which tickets skipped a stage {phrase}?").calls
    assert call.name == "oversight_list_findings"
    assert call.params["period"] == period
    assert call.params["state"] == "all"


def test_by_sprint_is_a_grouping_not_a_period() -> None:
    (call,) = plan_for("Effort against estimate by sprint").calls
    assert call.params == {"project": "AIL", "group_by": "sprint", "period": "90d"}


def test_findings_state_follows_the_words() -> None:
    assert plan_for("Which breaches were resolved last week?").calls[0].params["state"] == "resolved"
    assert plan_for("Which breaches are open this week?").calls[0].params["state"] == "open"
    assert plan_for("Which breaches were raised this week?").calls[0].params["state"] == "all"
    assert plan_for("Which breaches do we have?").calls[0].params["state"] == "open"


def test_trend_weeks_follow_the_period_or_the_question() -> None:
    assert plan_for("How is effort against estimate trending this month?").calls[0].params["last_n"] == 5
    assert plan_for("How is effort against estimate trending this week?").calls[0].params["last_n"] == 2
    assert plan_for("How are breaches trending over the last 30 weeks?").calls[0].params["last_n"] == 12
    assert plan_for("How are breaches trending over the last 4 weeks?").calls[0].params["last_n"] == 4


def test_counts_parse_status_type_and_estimate() -> None:
    assert plan_for("How many stories are to do?").calls[0].params == {"project": "AIL", "status": "To Do", "issue_type": "Story"}
    assert plan_for("How many epics are there?").calls[0].params == {"project": "AIL", "issue_type": "Epic"}
    assert plan_for("How many subtasks were done ever?").calls[0].params == {
        "project": "AIL", "status": "Done", "issue_type": "Subtask", "period": "all",
    }
    assert plan_for("How many tasks have no estimate this sprint?").calls[0].params == {
        "project": "AIL", "issue_type": "Task", "period": "sprint", "has_estimate": False,
    }


def test_only_tools_in_the_catalogue_are_planned() -> None:
    without = [name for name in NAMES if name != "oversight_time_in_status"]
    plan = plan_for("Where do tickets get stuck most?", names=without)
    assert plan.understood is False and plan.reason is None and plan.calls == []

    only_coverage = ["oversight_coverage"]
    plan = plan_for("How much work was estimated before it started?", names=only_coverage)
    assert [call.name for call in plan.calls] == ["oversight_coverage"]


def test_an_empty_catalogue_plans_nothing() -> None:
    plan = plan_for("Where do tickets get stuck most?", names=[])
    assert plan.understood is False and plan.calls == []
    assert plan_for("Who is slowest?", names=[]).reason == PERSON_REASON


def test_plans_validate_against_the_plan_model() -> None:
    for question, _expected in ROUTING_TABLE:
        plan = plan_for(question)
        assert Plan.model_validate(plan.model_dump()) == plan
