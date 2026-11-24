"""The query catalogue"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import pytest

from oversight.ask.catalog import CATALOG, CatalogContext, Column, Facts, catalog_schema, run_query, validate_params
from oversight.ask.validate import validate
from oversight.config import ProcessConfig
from oversight.derive.run import run_derive
from oversight.rules.catalog import RulesConfig
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC

from tests.conftest import CONFIG_FIXTURES
from tests.fixtures.derive import CASES, load_into, with_ops

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
AS_OF = "2026-11-04T11:00:00.000Z"
EPIC = "AIL-100"
MISSING_EPIC = "AIL-200"
EPIC_OF = {"AIL-103": EPIC, "AIL-109": EPIC, "AIL-104": MISSING_EPIC, "AIL-111": MISSING_EPIC}
SPEC_NAMES = [
    "oversight_count_issues",
    "oversight_top_issues",
    "oversight_time_in_status",
    "oversight_effort_vs_estimate",
    "oversight_list_findings",
    "oversight_issue_timeline",
    "oversight_trend",
    "oversight_coverage",
    "oversight_hot_spots",
    "oversight_rule_status",
]
DEFAULT_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("oversight_count_issues", {}),
    ("oversight_top_issues", {"metric": "cycle_time"}),
    ("oversight_time_in_status", {}),
    ("oversight_effort_vs_estimate", {}),
    ("oversight_list_findings", {}),
    ("oversight_issue_timeline", {"issue_key": "AIL-104"}),
    ("oversight_trend", {"metric": "median_cycle_time_h"}),
    ("oversight_coverage", {"field": "estimate"}),
    ("oversight_hot_spots", {}),
    ("oversight_rule_status", {}),
]


def _finding(
    finding_id: str,
    rule_id: str,
    issue_key: str,
    severity: str,
    evidence_key: str,
    first_seen_at: str,
    *,
    message: str | None = None,
    resolved_at: str | None = None,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "rule_id": rule_id,
        "rule_version": "0.1.0",
        "issue_key": issue_key,
        "project_key": "AIL",
        "kind": "rule",
        "severity": severity,
        "evidence_key": evidence_key,
        "message": message,
        "evidence_json": {},
        "first_seen_at": first_seen_at,
        "last_seen_at": "2026-11-04T06:00:00.000Z",
        "resolved_at": resolved_at,
        "resolved_why": "cleared" if resolved_at else None,
        "run_id": "rules-1",
    }


FINDINGS: list[dict[str, Any]] = [
    _finding("f-1", "R-FLOW-02", "AIL-103", "breach", "skip-1031", "2026-11-01T06:00:00.000Z", message="To Do → Done skipped development"),
    _finding("f-2", "R-FLOW-03", "AIL-104", "warning", "reopen-1043", "2026-10-20T06:00:00.000Z"),
    _finding("f-3", "R-EST-01", "AIL-109", "breach", "no-estimate", "2026-10-22T06:00:00.000Z", message="Started on 21 Oct 2026 with no points estimate"),
    _finding(
        "f-4",
        "R-EFF-01",
        "AIL-111",
        "breach",
        "ratio-ge-1.5",
        "2026-11-03T06:00:00.000Z",
        message="Logged 24.0 h against an estimate of 16.0 h (1.50×, threshold 1.5×)",
    ),
    _finding("f-5", "R-EST-01", "AIL-117", "breach", "no-estimate", "2026-10-10T06:00:00.000Z", resolved_at="2026-11-03T09:00:00.000Z"),
    _finding("f-6", "R-EST-02", "AIL-106", "warning", "estimate-after-start", "2026-10-16T06:00:00.000Z"),
]


def fixed_now() -> datetime:
    return NOW


def build_snapshot(store: Store, process: ProcessConfig, *, summaries: Mapping[str, str] | None = None) -> ProcessConfig:
    load_into(store)
    store.upsert(
        "issues",
        {
            "issue_key": EPIC,
            "issue_id": "90100",
            "project_key": "AIL",
            "issue_type": "Epic",
            "summary": "Billing",
            "status": "In Progress",
            "status_category": "indeterminate",
            "created_at": "2026-09-01T09:00:00.000Z",
        },
    )
    for key, epic in EPIC_OF.items():
        store.upsert("issues", CASES[key]["issue"] | {"epic_key": epic})
    for key, summary in (summaries or {}).items():
        store.upsert("issues", CASES[key]["issue"] | {"epic_key": EPIC_OF.get(key), "summary": summary})
    full_process = with_ops(process)
    run_derive(store, full_process, projects=["AIL", "OPS"], now=fixed_now)
    store.upsert_many("findings", FINDINGS)
    store.set_cursor("AIL", last_synced_at=AS_OF, run_id="sync-1", account_timezone="Europe/London")
    return full_process


def context(process: ProcessConfig, rules: RulesConfig, **overrides: Any) -> CatalogContext:
    values: dict[str, Any] = {"process": process, "rules": rules, "project": "AIL", "now": NOW, "row_limit": 200}
    return CatalogContext(**(values | overrides))


@pytest.fixture
def rules() -> RulesConfig:
    return RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")


@pytest.fixture
def snapshot(store: Store, process: ProcessConfig) -> tuple[Store, ProcessConfig]:
    return store, build_snapshot(store, process)


def _run(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, name: str, params: dict[str, Any], **ctx: Any) -> Facts:
    store, process = snapshot
    return run_query(store.conn, name, params, context(process, rules, **ctx), ref="F1")


def _kinds(facts: Facts) -> dict[str, str]:
    return {column.key: column.kind for column in facts.columns}


def test_the_ten_specs_in_order() -> None:
    assert list(CATALOG) == SPEC_NAMES
    for spec in CATALOG.values():
        assert spec.name.startswith("oversight_")
        assert spec.description.strip()


def test_catalog_schema_describes_every_spec() -> None:
    schema = catalog_schema()
    assert [entry["name"] for entry in schema] == SPEC_NAMES
    for entry in schema:
        assert entry["description"] == CATALOG[entry["name"]].description
        assert entry["input_schema"]["additionalProperties"] is False  # extra="forbid"
        assert "properties" in entry["input_schema"]
    json.dumps(schema)  # it goes into the plan prompt as JSON


class _NoSQL:
    """A connection stand-in"""

    def cursor(self) -> Any:
        raise AssertionError("SQL ran before the parameters were validated")

    def execute(self, *_args: Any) -> Any:
        raise AssertionError("SQL ran before the parameters were validated")


BAD_PARAMS: list[tuple[str, dict[str, Any], str]] = [
    ("oversight_count_issues", {"project": "AIL; DROP TABLE issues"}, "project"),
    ("oversight_count_issues", {"project": "ail-1"}, "project"),
    ("oversight_count_issues", {"period": "1y"}, "period"),
    ("oversight_count_issues", {"has_estimate": "maybe"}, "has_estimate"),
    ("oversight_count_issues", {"status": "Done' OR '1'='1"}, "status"),
    ("oversight_count_issues", {"issue_type": "Bug; --"}, "issue_type"),
    ("oversight_count_issues", {"unknown": 1}, "unknown parameter unknown"),
    ("oversight_top_issues", {"metric": "velocity"}, "metric"),
    ("oversight_top_issues", {"metric": "cycle_time", "n": 21}, "n"),
    ("oversight_top_issues", {"metric": "cycle_time", "n": 0}, "n"),
    ("oversight_top_issues", {}, "metric is required"),
    ("oversight_top_issues", {"metric": "cycle_time", "status_category": "closed"}, "status_category"),
    ("oversight_time_in_status", {"group_by": "assignee"}, "group_by"),
    ("oversight_time_in_status", {"period": "30"}, "period"),
    ("oversight_effort_vs_estimate", {"group_by": "person"}, "group_by"),
    ("oversight_list_findings", {"rule_id": "R-EST-1"}, "rule_id"),
    ("oversight_list_findings", {"rule_id": "R-EST-01; DROP"}, "rule_id"),
    ("oversight_list_findings", {"severity": "critical"}, "severity"),
    ("oversight_list_findings", {"state": "closed"}, "state"),
    ("oversight_list_findings", {"n": 51}, "n"),
    ("oversight_issue_timeline", {}, "issue_key is required"),
    ("oversight_issue_timeline", {"issue_key": "ail-1' OR 1=1"}, "issue_key"),
    ("oversight_issue_timeline", {"issue_key": "AIL"}, "issue_key"),
    ("oversight_trend", {"metric": "velocity"}, "metric"),
    ("oversight_trend", {"metric": "reopen_rate", "last_n": 13}, "last_n"),
    ("oversight_trend", {"metric": "reopen_rate", "last_n": "many"}, "last_n"),
    ("oversight_coverage", {"field": "assignee"}, "field"),
    ("oversight_coverage", {}, "field is required"),
    ("oversight_hot_spots", {"n": 11}, "n"),
    ("oversight_rule_status", {"n": 1}, "unknown parameter n"),
    ("oversight_rule_status", {"project": "1AIL"}, "project"),
]


@pytest.mark.parametrize(("name", "params", "problem"), BAD_PARAMS, ids=[f"{n}:{p}" for n, _, p in BAD_PARAMS])
def test_bad_params_are_refused_before_sql(
    name: str, params: dict[str, Any], problem: str, process: ProcessConfig, rules: RulesConfig
) -> None:
    with pytest.raises(ValueError, match=problem):
        run_query(_NoSQL(), name, params, context(process, rules), ref="F1")  # type: ignore[arg-type]


def test_unknown_query_is_refused_before_sql(process: ProcessConfig, rules: RulesConfig) -> None:
    with pytest.raises(ValueError, match="unknown query 'oversight_nope'"):
        run_query(_NoSQL(), "oversight_nope", {}, context(process, rules), ref="F1")  # type: ignore[arg-type]


def test_keys_are_normalised_and_defaults_apply() -> None:
    count = validate_params("oversight_count_issues", {"project": " ail ", "status": "In Progress"})
    assert (count.project, count.status, count.period, count.has_estimate) == ("AIL", "In Progress", "all", None)  # type: ignore[attr-defined]
    timeline = validate_params("oversight_issue_timeline", {"issue_key": "ail-104"})
    assert timeline.issue_key == "AIL-104"  # type: ignore[attr-defined]
    findings = validate_params("oversight_list_findings", {})
    assert (findings.state, findings.n, findings.rule_id) == ("open", 20, None)  # type: ignore[attr-defined]
    assert validate_params("oversight_top_issues", {"metric": "reopens", "n": "5"}).n == 5  # type: ignore[attr-defined]


def test_every_spec_runs_on_a_read_only_connection(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    store, process = snapshot
    conn = open_readonly(store.path)  # type: ignore[arg-type]
    try:
        for index, (name, params) in enumerate(DEFAULT_CALLS, start=1):
            facts = run_query(conn, name, params, context(process, rules), ref=f"F{index}")
            assert facts.ref == f"F{index}" and facts.tool == name
            assert facts.as_of == AS_OF
            assert [column.key for column in facts.columns]
            assert all(set(row) == {column.key for column in facts.columns} for row in facts.rows)
            json.dumps(facts.to_dict())
    finally:
        conn.close()


def test_facts_to_dict_shape(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_hot_spots", {"n": 2})
    payload = facts.to_dict()
    assert set(payload) == {"ref", "tool", "params", "columns", "rows", "truncated", "as_of", "note"}
    assert payload["params"] == {"n": 2}
    assert payload["columns"][0] == {"key": "epic_key", "label": "Epic", "kind": "issue_key"}
    assert payload["truncated"] is True and len(payload["rows"]) == 2
    assert not Facts(ref="F1", tool="t", params={}, columns=[Column("a", "A", "text")], rows=[{"a": 1}]).empty


def test_count_issues_counts_and_samples(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_count_issues", {})
    assert _kinds(facts) == {"count": "count", "sample_keys": "text"}
    assert len(facts.rows) == 1
    (row,) = facts.rows
    assert row["count"] == 19  # 18 fixture issues plus the Epic; counts never leave types out
    keys = row["sample_keys"].split(", ")
    assert len(keys) == 19 and keys[0] == "AIL-116" and keys[-1] == EPIC  # newest first
    assert facts.as_of == AS_OF and facts.truncated is False


def test_count_issues_filters(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    assert _run(snapshot, rules, "oversight_count_issues", {"status": "done"}).rows[0]["count"] == 14  # any case
    bug = _run(snapshot, rules, "oversight_count_issues", {"issue_type": "bug"}).rows[0]
    assert (bug["count"], bug["sample_keys"]) == (1, "AIL-104")
    without = _run(snapshot, rules, "oversight_count_issues", {"has_estimate": False}).rows[0]
    assert without["count"] == 4 and set(without["sample_keys"].split(", ")) == {"AIL-109", "AIL-113", "AIL-117", EPIC}
    with_estimate = _run(snapshot, rules, "oversight_count_issues", {"has_estimate": True, "issue_type": "Task"}).rows[0]
    assert with_estimate["count"] == 13  # 16 tasks less AIL-109, AIL-113 and AIL-117
    recent = _run(snapshot, rules, "oversight_count_issues", {"period": "7d"})
    assert recent.rows[0]["count"] == 7 and "last 7 days" in (recent.note or "")
    assert _run(snapshot, rules, "oversight_count_issues", {"project": "OPS"}).rows[0]["count"] == 3


def test_count_issues_zero_is_still_a_row(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    facts = run_query(store.conn, "oversight_count_issues", {}, context(process, rules), ref="F1")
    assert facts.rows == [{"count": 0, "sample_keys": ""}]
    assert facts.as_of is None  # nothing has ever synced


def test_as_of_falls_back_to_the_last_sync_run(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    run = store.start_run("sync", started_at=datetime(2026, 11, 4, 10, 30, tzinfo=UTC))
    store.finish_run(run, status="succeeded")
    facts = run_query(store.conn, "oversight_count_issues", {}, context(process, rules), ref="F1")
    assert facts.as_of == "2026-11-04T10:30:00.000Z"
    given = run_query(store.conn, "oversight_count_issues", {}, context(process, rules, last_synced_at="2026-11-04T11:11:00.000Z"), ref="F1")
    assert given.as_of == "2026-11-04T11:11:00.000Z"


def test_top_issues_by_cycle_time(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_top_issues", {"metric": "cycle_time", "n": 3})
    assert [column.key for column in facts.columns] == ["issue_key", "summary", "issue_type", "status", "epic_key", "value", "unit"]
    assert _kinds(facts)["value"] == "hours" and _kinds(facts)["issue_key"] == "issue_key" and _kinds(facts)["epic_key"] == "issue_key"
    assert [(r["issue_key"], r["value"], r["unit"]) for r in facts.rows] == [("AIL-104", 55.0, "h"), ("AIL-101", 54.0, "h"), ("AIL-106", 32.0, "h")]
    assert facts.rows[0]["epic_key"] == MISSING_EPIC and facts.rows[0]["issue_type"] == "Bug"
    assert facts.truncated is True


def test_top_issues_other_metrics(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    ratio = _run(snapshot, rules, "oversight_top_issues", {"metric": "effort_ratio", "n": 2})
    assert [(r["issue_key"], r["value"]) for r in ratio.rows] == [("AIL-111", 1.5), ("AIL-101", 1.25)]  # the tie at 1.25 breaks by key
    assert _kinds(ratio)["value"] == "ratio" and ratio.rows[0]["unit"] == "×"
    reopens = _run(snapshot, rules, "oversight_top_issues", {"metric": "reopens", "n": 20})
    assert [(r["issue_key"], r["value"]) for r in reopens.rows] == [("AIL-104", 1), ("AIL-117", 1)]  # never-reopened work is left out
    assert _kinds(reopens)["value"] == "count" and reopens.truncated is False
    in_progress = _run(snapshot, rules, "oversight_top_issues", {"metric": "status_age", "status_category": "indeterminate"})
    assert [(r["issue_key"], r["value"]) for r in in_progress.rows] == [("AIL-110", 28.0), ("AIL-117", 26.0)]


def test_top_issues_leave_epics_out_unless_asked(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    oldest = _run(snapshot, rules, "oversight_top_issues", {"metric": "status_age", "n": 1})
    assert [(r["issue_key"], r["value"]) for r in oldest.rows] == [("AIL-101", 764.0)]
    assert "Epic" in (oldest.note or "")
    epics = _run(snapshot, rules, "oversight_top_issues", {"metric": "status_age", "issue_type": "epic"})
    assert [r["issue_key"] for r in epics.rows] == [EPIC]


def test_time_in_status_matches_the_labels(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_time_in_status", {"period": "all"})
    assert _kinds(facts) == {"group": "text", "n": "count", "median_h": "hours", "p75_h": "hours", "p90_h": "hours"}
    assert [r["group"] for r in facts.rows] == ["To Do", "In Progress", "In Review"]
    assert "time in the done stage (Done) is left out" in (facts.note or "")
    by_group = {r["group"]: r for r in facts.rows}
    assert (by_group["To Do"]["n"], by_group["To Do"]["median_h"], by_group["To Do"]["p75_h"], by_group["To Do"]["p90_h"]) == (17, 1.0, 2.0, 5.6)
    assert (by_group["In Progress"]["n"], by_group["In Progress"]["median_h"], by_group["In Progress"]["p75_h"]) == (15, 24.0, 24.0)
    assert by_group["In Progress"]["p90_h"] == pytest.approx(30.8)
    assert by_group["In Review"]["n"] == 1


def test_time_in_status_periods_and_groups(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    week = _run(snapshot, rules, "oversight_time_in_status", {"period": "7d"})
    by_group = {r["group"]: r for r in week.rows}
    assert (by_group["In Progress"]["n"], by_group["In Progress"]["median_h"]) == (5, 8.0)
    assert "last 7 days" in (week.note or "")
    by_type = _run(snapshot, rules, "oversight_time_in_status", {"group_by": "issue_type", "period": "all"})
    assert [(r["group"], r["n"], r["median_h"]) for r in by_type.rows] == [("Task", 12, 24.0), ("Bug", 2, 16.0), ("Subtask", 1, 0.0)]
    assert "In Progress" in (by_type.note or "")  # which statuses count as work
    by_epic = _run(snapshot, rules, "oversight_time_in_status", {"group_by": "epic", "period": "all"})
    assert {r["group"] for r in by_epic.rows} == {"(no epic)", EPIC, MISSING_EPIC, "AIL-1138"}  # AIL-1140's real epic


def test_time_in_status_sprint_period_without_sprint_data(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_time_in_status", {"period": "sprint"})
    assert "No active sprint" in (facts.note or "") and "14 days" in (facts.note or "")
    assert {r["group"]: r["n"] for r in facts.rows}["In Progress"] == 8


def test_time_in_status_sprint_period_from_the_active_sprint(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    store, process = snapshot
    raw = {"fields": {"customfield_10020": [
        {"id": 4426, "name": "Sprint 11", "state": "closed", "startDate": "2026-10-14T09:00:00.000Z", "endDate": "2026-10-28T09:00:00.000Z"},
        {"id": 4427, "name": "Sprint 12", "state": "active", "startDate": "2026-10-30T09:00:00.000Z", "endDate": "2026-11-13T09:00:00.000Z"},
    ]}}
    store.upsert("issues", CASES["AIL-1140"]["issue"] | {"raw_json": raw})
    facts = _run(snapshot, rules, "oversight_time_in_status", {"period": "sprint"})
    assert "Sprint 12" in (facts.note or "") and "30 Oct 2026" in (facts.note or "")
    assert {r["group"]: r["n"] for r in facts.rows} == {"To Do": 5, "In Progress": 3}  # Done's 2 left out


def test_sprint_period_survives_malformed_raw_json(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    store, _ = snapshot
    store.conn.execute("UPDATE issues SET raw_json = ? WHERE issue_key = ?", ("{not json: active", "AIL-110"))
    facts = _run(snapshot, rules, "oversight_time_in_status", {"period": "sprint"})
    assert "No active sprint" in (facts.note or "") and not facts.empty


def test_row_cap_sets_truncated(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_time_in_status", {"period": "all"}, row_limit=2)
    assert len(facts.rows) == 2 and facts.truncated is True
    findings = _run(snapshot, rules, "oversight_list_findings", {"n": 50}, row_limit=3)
    assert len(findings.rows) == 3 and findings.truncated is True
    exact = _run(snapshot, rules, "oversight_list_findings", {"n": 5})
    assert len(exact.rows) == 5 and exact.truncated is False


def test_effort_vs_estimate_by_type(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_effort_vs_estimate", {"group_by": "issue_type", "period": "all"})
    assert _kinds(facts) == {"group": "text", "n": "count", "median_ratio": "ratio", "share_over_threshold": "share", "threshold": "ratio"}
    assert [(r["group"], r["n"], r["median_ratio"]) for r in facts.rows] == [("Bug", 1, 1.0), ("Task", 11, 0.75), ("Subtask", 1, 0.125)]
    task = facts.rows[1]
    assert task["share_over_threshold"] == pytest.approx(1 / 11, abs=1e-4) and task["threshold"] == 1.5
    assert facts.rows[0]["share_over_threshold"] == 0.0


def test_effort_vs_estimate_by_sprint_and_period(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    by_sprint = _run(snapshot, rules, "oversight_effort_vs_estimate", {"group_by": "sprint", "period": "all"})
    assert [(r["group"], r["n"]) for r in by_sprint.rows] == [("Sprint 4427", 1), ("(no sprint)", 12)]
    recent = _run(snapshot, rules, "oversight_effort_vs_estimate", {"group_by": "epic", "period": "7d"})
    assert [(r["group"], r["n"], r["median_ratio"]) for r in recent.rows] == [("AIL-1138", 1, 1.0), ("(no epic)", 2, 0.3125)]
    assert "R-EFF-01" in (recent.note or "")


def test_effort_threshold_defaults_when_the_rule_is_absent(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    trimmed = RulesConfig.model_validate(rules.model_dump() | {"rules": [r.model_dump() for r in rules.rules if r.id not in ("R-EFF-01", "W-EFF-01")]})
    facts = _run(snapshot, trimmed, "oversight_effort_vs_estimate", {"group_by": "issue_type", "period": "all"})
    assert facts.rows[0]["threshold"] == 1.5


def test_effort_by_epic_names_the_epic_so_an_answer_can_cite_it(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_effort_vs_estimate", {"group_by": "epic", "period": "all"})
    assert [(c.key, c.label, c.kind) for c in facts.columns[:2]] == [("group", "Epic", "issue_key"), ("epic_title", "Epic title", "text")]
    assert _kinds(facts) == {
        "group": "issue_key", "epic_title": "text", "n": "count", "median_ratio": "ratio", "share_over_threshold": "share", "threshold": "ratio",
    }
    assert [(r["group"], r["epic_title"], r["n"]) for r in facts.rows] == [
        (MISSING_EPIC, None, 2), ("AIL-1138", None, 1), ("(no epic)", "(no epic)", 9), (EPIC, "Billing", 1),
    ]
    check = validate("Epic AIL-100 ran at a median of 0.375 of its estimate.", [facts], None)
    assert check.ok, check.problems
    assert [(b.text, b.row_index, b.column) for b in check.bindings] == [("AIL-100", 3, "group"), ("0.375", 3, "median_ratio")]
    assert "epic_title" not in _kinds(_run(snapshot, rules, "oversight_effort_vs_estimate", {"group_by": "sprint", "period": "all"}))


def test_effort_medians_leave_out_estimated_work_with_nothing_logged(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    store, _ = snapshot
    store.conn.execute("UPDATE issues SET time_spent_s = NULL WHERE issue_key = 'AIL-115'")
    store.conn.execute("UPDATE issue_metrics SET effort_ratio = 0.0 WHERE issue_key = 'AIL-115'")
    by_epic = _run(snapshot, rules, "oversight_effort_vs_estimate", {"group_by": "epic", "period": "all"})
    no_epic = next(r for r in by_epic.rows if r["group"] == "(no epic)")
    assert (no_epic["n"], no_epic["median_ratio"]) == (8, 0.625)
    assert "both an estimate and logged time" in (by_epic.note or "")
    trend = _run(snapshot, rules, "oversight_trend", {"metric": "median_effort_ratio", "last_n": 4})
    assert [r["value"] for r in trend.rows] == [0.5, 1.125, 0.6875, 1.0]
    assert "both an estimate and logged time" in (trend.note or "")


def test_list_findings_open_by_default(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_list_findings", {})
    assert [column.key for column in facts.columns] == [
        "finding_id", "rule_id", "rule_title", "severity", "issue_key", "summary", "epic_key", "message", "first_seen_at", "resolved_at",
    ]
    assert _kinds(facts)["first_seen_at"] == "date" and _kinds(facts)["issue_key"] == "issue_key"
    assert [r["finding_id"] for r in facts.rows] == ["f-4", "f-1", "f-3", "f-2", "f-6"]  # newest first
    skip = facts.rows[1]
    assert (skip["rule_title"], skip["issue_key"], skip["epic_key"], skip["severity"]) == ("Skipped a stage", "AIL-103", EPIC, "breach")
    assert skip["message"] == "To Do → Done skipped development" and skip["resolved_at"] is None
    assert all(r["resolved_at"] is None for r in facts.rows)


def test_list_findings_filters(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    breaches = _run(snapshot, rules, "oversight_list_findings", {"severity": "breach"})
    assert [r["finding_id"] for r in breaches.rows] == ["f-4", "f-1", "f-3"]
    estimate = _run(snapshot, rules, "oversight_list_findings", {"rule_id": "R-EST-01", "state": "all"})
    assert [(r["finding_id"], r["resolved_at"]) for r in estimate.rows] == [("f-3", None), ("f-5", "2026-11-03T09:00:00.000Z")]
    assert "Work started without an estimate" in (estimate.note or "")
    resolved = _run(snapshot, rules, "oversight_list_findings", {"state": "resolved"})
    assert [r["finding_id"] for r in resolved.rows] == ["f-5"]
    recent = _run(snapshot, rules, "oversight_list_findings", {"period": "7d"})
    assert [r["finding_id"] for r in recent.rows] == ["f-4", "f-1"]
    capped = _run(snapshot, rules, "oversight_list_findings", {"n": 2})
    assert [r["finding_id"] for r in capped.rows] == ["f-4", "f-1"] and capped.truncated is True
    assert _run(snapshot, rules, "oversight_list_findings", {"rule_id": "R-HYG-01"}).empty


def test_list_findings_needs_the_rules_evidence(rules: RulesConfig) -> None:
    spec = CATALOG["oversight_list_findings"]
    assert spec.needs_evidence(validate_params(spec.name, {"rule_id": "R-FLOW-01"}), rules) == ["code_review"]
    assert spec.needs_evidence(validate_params(spec.name, {"rule_id": "W-REV-01"}), rules) == ["code_review"]
    assert spec.needs_evidence(validate_params(spec.name, {"rule_id": "R-EST-01"}), rules) == []
    assert spec.needs_evidence(validate_params(spec.name, {}), rules) == []
    assert spec.needs_evidence(validate_params(spec.name, {"rule_id": "R-ZZZ-99"}), rules) == []
    assert CATALOG["oversight_count_issues"].needs_evidence(validate_params("oversight_count_issues", {}), rules) == []


def test_issue_timeline_rows_and_findings_note(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_issue_timeline", {"issue_key": "AIL-104"})
    assert _kinds(facts) == {"seq": "count", "status": "text", "stage": "text", "entered_at": "date", "left_at": "date", "hours": "hours"}
    assert [r["seq"] for r in facts.rows] == [0, 1, 2, 3, 4]
    assert [r["status"] for r in facts.rows] == ["To Do", "In Progress", "Done", "In Progress", "Done"]
    assert [r["hours"] for r in facts.rows] == [1.0, 24.0, 23.0, 8.0, None]
    assert facts.rows[1]["stage"] == "development" and facts.rows[4]["left_at"] is None
    assert facts.note == "Findings for AIL-104: R-FLOW-03 Reopened after Done (open)"
    assert facts.as_of == AS_OF


def test_issue_timeline_for_unknown_and_quiet_issues(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    missing = _run(snapshot, rules, "oversight_issue_timeline", {"issue_key": "AIL-999"})
    assert missing.empty and missing.note == "AIL-999 is not in the snapshot"
    quiet = _run(snapshot, rules, "oversight_issue_timeline", {"issue_key": "AIL-116"})
    assert len(quiet.rows) == 1 and quiet.note == "No findings for AIL-116"
    both = _run(snapshot, rules, "oversight_issue_timeline", {"issue_key": "AIL-117"})
    assert both.note == "Findings for AIL-117: R-EST-01 Work started without an estimate (resolved)"


def test_trend_weeks_start_on_monday(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_trend", {"metric": "median_cycle_time_h", "last_n": 4})
    assert _kinds(facts) == {"week_of": "date", "value": "hours"}
    assert [r["week_of"] for r in facts.rows] == ["2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02"]
    assert [r["value"] for r in facts.rows[:3]] == [29.0, 16.0, 7.0]
    assert facts.rows[3]["value"] == pytest.approx(23.7449, abs=1e-4)


def test_trend_metrics(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    def values(metric: str, last_n: int) -> list[Any]:
        facts = _run(snapshot, rules, "oversight_trend", {"metric": metric, "last_n": last_n})
        return [r["value"] for r in facts.rows]

    assert values("open_breaches", 4) == [1, 2, 3, 3]
    assert values("open_warnings", 2) == [2, 2]
    assert values("skipped_stage_share", 5) == pytest.approx([1 / 3, 0.0, 0.0, 0.0, 0.0], abs=1e-4)
    assert values("reopen_rate", 5) == pytest.approx([1 / 3, 0.0, 0.0, 0.0, 0.0], abs=1e-4)
    assert values("median_effort_ratio", 4) == [0.5, 1.125, 0.5, 1.0]
    twelve = _run(snapshot, rules, "oversight_trend", {"metric": "median_cycle_time_h", "last_n": 12})
    assert len(twelve.rows) == 12 and twelve.rows[0] == {"week_of": "2026-08-17", "value": None}
    assert _kinds(_run(snapshot, rules, "oversight_trend", {"metric": "open_breaches"}))["value"] == "count"
    assert _kinds(_run(snapshot, rules, "oversight_trend", {"metric": "reopen_rate"}))["value"] == "share"


def test_coverage_of_estimates_and_time(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    estimate = _run(snapshot, rules, "oversight_coverage", {"field": "estimate", "period": "all"})
    assert _kinds(estimate) == {"covered": "count", "total": "count", "share": "share", "measurable": "text", "note": "text"}
    (row,) = estimate.rows
    assert (row["covered"], row["total"], row["measurable"]) == (15, 18, True)  # the Epic is left out
    assert row["share"] == pytest.approx(15 / 18, abs=1e-4)
    time_logged = _run(snapshot, rules, "oversight_coverage", {"field": "time_logged", "period": "all"}).rows[0]
    assert (time_logged["covered"], time_logged["total"]) == (17, 18)  # AIL-116 has no time
    points = _run(snapshot, rules, "oversight_coverage", {"field": "story_points", "period": "30d"}).rows[0]
    assert (points["covered"], points["total"]) == (13, 16)
    assert "last 30 days" in points["note"]


def test_coverage_without_evidence_is_not_measurable(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_coverage", {"field": "code_review", "period": "all"})
    (row,) = facts.rows
    assert (row["covered"], row["share"], row["measurable"], row["total"]) == (None, None, False, 18)
    assert "code_review" in row["note"] and "process.yaml" in row["note"]
    spec = CATALOG["oversight_coverage"]
    assert spec.needs_evidence(validate_params(spec.name, {"field": "code_review"}), rules) == ["code_review"]
    assert spec.needs_evidence(validate_params(spec.name, {"field": "test_evidence"}), rules) == ["testing"]
    assert spec.needs_evidence(validate_params(spec.name, {"field": "estimate"}), rules) == []


def test_coverage_with_a_status_evidence_source(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    store, process = snapshot
    data = process.model_dump()
    data["projects"]["AIL"]["evidence"]["code_review"] = {"status": "In Review"}
    with_review = ProcessConfig.model_validate(data)
    facts = run_query(store.conn, "oversight_coverage", {"field": "code_review", "period": "all"}, context(with_review, rules), ref="F1")
    (row,) = facts.rows
    assert (row["covered"], row["total"], row["measurable"]) == (1, 18, True)
    assert "status: In Review" in row["note"]


def test_hot_spots_rank_epics_by_open_findings(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_hot_spots", {})
    assert _kinds(facts) == {"epic_key": "issue_key", "epic_title": "text", "open_breaches": "count", "open_warnings": "count", "issues": "count"}
    assert [(r["epic_key"], r["epic_title"], r["open_breaches"], r["open_warnings"], r["issues"]) for r in facts.rows] == [
        (EPIC, "Billing", 2, 0, 2),
        (MISSING_EPIC, None, 1, 1, 2),
        (None, "(no epic)", 0, 1, 1),
    ]
    assert facts.truncated is False
    top = _run(snapshot, rules, "oversight_hot_spots", {"n": 1})
    assert [r["epic_key"] for r in top.rows] == [EPIC] and top.truncated is True


def test_work_outside_any_epic_is_listed_after_the_epics_however_many_findings_it_holds(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig
) -> None:
    store, _ = snapshot
    store.upsert_many("findings", [
        _finding(f"f-x{n}", "R-HYG-01", "AIL-106", "breach", f"extra-{n}", "2026-11-02T06:00:00.000Z") for n in range(3)
    ])
    facts = _run(snapshot, rules, "oversight_hot_spots", {})
    assert [(r["epic_key"], r["open_breaches"], r["open_warnings"], r["issues"]) for r in facts.rows] == [
        (EPIC, 2, 0, 2),
        (MISSING_EPIC, 1, 1, 2),
        (None, 3, 1, 1),
    ]
    assert _run(snapshot, rules, "oversight_hot_spots", {"n": 1}).rows[0]["epic_key"] == EPIC


def test_sprint_findings_are_counted_under_no_epic_but_never_as_issues(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig
) -> None:
    store, _ = snapshot
    store.upsert("findings", _finding("f-s1", "W-SPR-01", "sprint:4427", "warning", "sprint-4427", "2026-11-02T06:00:00.000Z"))
    facts = _run(snapshot, rules, "oversight_hot_spots", {})
    assert [(r["epic_key"], r["open_breaches"], r["open_warnings"], r["issues"]) for r in facts.rows] == [
        (EPIC, 2, 0, 2),
        (MISSING_EPIC, 1, 1, 2),
        (None, 0, 2, 1),  # f-6 on AIL-106 and the sprint's warning; one issue
    ]
    assert "sprint findings included" in (facts.note or "")
    store.conn.execute("UPDATE findings SET resolved_at = '2026-11-03T09:00:00.000Z' WHERE finding_id = 'f-6'")
    rows = _run(snapshot, rules, "oversight_hot_spots", {}).rows
    assert (rows[-1]["epic_key"], rows[-1]["epic_title"], rows[-1]["open_warnings"], rows[-1]["issues"]) == (None, "(no epic)", 1, 0)


def test_rule_status_names_what_cannot_be_measured(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig) -> None:
    facts = _run(snapshot, rules, "oversight_rule_status", {})
    assert [column.key for column in facts.columns] == ["rule_id", "title", "kind", "severity", "measurable", "missing", "open_findings", "version"]
    rows = {r["rule_id"]: r for r in facts.rows}
    assert len(rows) == 12 and all(r["version"] == "0.1.0" for r in rows.values())
    assert (rows["R-FLOW-01"]["measurable"], rows["R-FLOW-01"]["missing"]) == (False, "code_review, testing")
    assert (rows["W-REV-01"]["measurable"], rows["W-REV-01"]["missing"]) == (False, "code_review")
    assert sum(1 for r in rows.values() if r["measurable"]) == 10
    assert {k: r["open_findings"] for k, r in rows.items() if r["open_findings"]} == {
        "R-EST-01": 1, "R-EST-02": 1, "R-EFF-01": 1, "R-FLOW-02": 1, "R-FLOW-03": 1,
    }
    assert (rows["R-EST-01"]["kind"], rows["R-EST-01"]["severity"], rows["R-EST-01"]["title"]) == ("rule", "breach", "Work started without an estimate")
