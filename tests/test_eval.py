"""The Q&A eval: runner scoring, the questions file, the shipped sets, the MCP eval XML and the CLI."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from oversight.ask.catalog import CATALOG, CatalogContext, Facts, run_query
from oversight.cli import EXIT_CHECKS_FAILED, EXIT_CONFIG, EXIT_OK, main as cli_main
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.errors import ConfigError
from oversight.eval import (
    MEDIAN_LATENCY_TARGET_S,
    OUTCOMES,
    PLAN_RATE_TARGET,
    EvalCase,
    EvalDataset,
    EvalReport,
    EvalResult,
    _has_key,
    _has_number,
    load_cases,
    load_dataset,
    run_qa_eval,
)
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.settings import PROJECT_ROOT, Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC

from tests.conftest import CONFIG_FIXTURES
from tests.fakes.fake_llm import FakeProvider, plan
from tests.test_catalog import build_snapshot, fixed_now

EVAL_DIR = PROJECT_ROOT / "tests" / "eval"
QUESTIONS = EVAL_DIR / "questions.yaml"
MCP_EVAL = EVAL_DIR / "mcp_eval.xml"
DEMO_PROJECT = "DEMO"
DEMO_SEED = 20261104
DEMO_ISSUES = 1150
DEMO_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

STUCK = "Where do tickets get stuck most?"
STUCK_ANSWER = "Tickets sit longest in In Progress: a median of 24.0 h over 15 intervals; To Do is 1.0 h at the median."
TIME_IN_STATUS = ("oversight_time_in_status", {"period": "all"})
PERSON_REASON = "Answers stay at team and process level."
FOUR_CASES: list[dict[str, Any]] = [
    {
        "id": "stuck",
        "question": STUCK,
        "expected_calls": ["oversight_time_in_status"],
        "expected_outcome": "answered",
        "expected_numbers": ["24.0", "15", "1.0"],
        "expected_keys": [],
    },
    {"id": "person", "question": "Who is the slowest developer?", "expected_calls": [], "expected_outcome": "not_understood"},
    {
        "id": "review",
        "question": "Which tickets reached testing without code review?",
        "expected_calls": ["oversight_list_findings"],
        "expected_outcome": "not_measurable",
    },
    {
        "id": "unassigned",
        "question": "Any unassigned work in progress?",
        "expected_calls": ["oversight_list_findings"],
        "expected_outcome": "no_data",
    },
]


def four_provider(answers: list[Any] | None = None) -> FakeProvider:
    """A provider scripted for FOUR_CASES in order: answered, not_understood, not_measurable, no_data."""
    return FakeProvider(
        plans=[
            plan(TIME_IN_STATUS),
            plan(understood=False, reason=PERSON_REASON),
            plan(("oversight_list_findings", {"rule_id": "R-FLOW-01"})),
            plan(("oversight_list_findings", {"rule_id": "R-HYG-01"})),
        ],
        answers=[STUCK_ANSWER] if answers is None else answers,
    )


def write_cases(path: Path, cases: Any) -> Path:
    path.write_text(yaml.safe_dump(cases, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def snapshot(store: Store, process: ProcessConfig) -> tuple[Store, ProcessConfig]:
    return store, build_snapshot(store, process)


@pytest.fixture
def rules() -> RulesConfig:
    return RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")


def run(
    snapshot: tuple[Store, ProcessConfig],
    rules: RulesConfig,
    settings: Settings,
    provider: Any,
    cases: Any,
    tmp_path: Path,
) -> EvalReport:
    store, process = snapshot
    return run_qa_eval(
        write_cases(tmp_path / "questions.yaml", cases),
        store=store,
        process=process,
        rules=rules,
        settings=settings,
        provider=provider,
        out_path=tmp_path / "reports" / "qa_eval.md",
        now=fixed_now,
    )


def test_run_qa_eval_scores_every_case_and_writes_the_report(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, tmp_path: Path
) -> None:
    store, _ = snapshot
    report = run(snapshot, rules, settings, four_provider(), FOUR_CASES, tmp_path)

    assert report.passed is True and report.plan_rate == 1.0 and report.outcome_rate == 1.0
    assert 0.0 <= report.median_latency_s <= MEDIAN_LATENCY_TARGET_S
    assert [r.id for r in report.results] == ["stuck", "person", "review", "unassigned"]
    assert [r.outcome for r in report.results] == ["answered", "not_understood", "not_measurable", "no_data"]
    assert all(r.plan_ok and r.outcome_ok and r.numbers_ok and r.keys_ok and r.ok for r in report.results)
    stuck, person = report.results[0], report.results[1]
    assert stuck.detail["planned"] == [{"name": "oversight_time_in_status", "params": {"period": "all"}}]
    assert stuck.detail["answer"] == STUCK_ANSWER and stuck.detail["missing_numbers"] == [] and stuck.detail["missing_keys"] == []
    assert stuck.detail["model"] == "fake-model" and len(stuck.detail["qa_id"]) == 32 and stuck.detail["asked_at"] == "2026-11-04T12:00:00.000Z"
    assert stuck.latency_s >= 0 and isinstance(stuck.latency_s, float)
    assert person.detail["message"] == PERSON_REASON and person.detail["answer"] is None and person.detail["planned"] == []
    assert report.report_path == tmp_path / "reports" / "qa_eval.md" and report.report_path.exists()
    text = report.report_path.read_text(encoding="utf-8")
    assert text == report.render() and text.startswith("# Q&A eval\n")
    assert "4 cases, 4/4 plans right (100%), 4/4 outcomes right (100%)" in text and "**PASSED**" in text
    assert "| 1 | stuck | Where do tickets get stuck most? | answered | answered | ok | ok | ok |" in text
    assert "| 2 | person | Who is the slowest developer? | not_understood | not_understood | ok | ok | ok |" in text
    assert "- Plan right: 4/4 (100.0%); target 90%." in text and "- Outcome right: 4/4 (100.0%)." in text
    assert "- Answered cases with every expected number and key: 1/1." in text and "- Result: PASSED." in text
    assert "## Misses" not in text and "with model fake-model" in text
    assert store.count("qa_log", {"asked_by": "eval"}) == 4


@pytest.mark.parametrize(
    ("text", "number", "found"),
    [
        ("a median of 24.0 h over 15 intervals", "24.0", True),
        ("a median of 24.0 h over 15 intervals", "24", False),  # a different precision is a different number
        ("a median of 24.0 h over 15 intervals", "4.0", False),  # inside 24.0
        ("a median of 24.0 h over 15 intervals", "15", True),
        ("150 intervals", "15", False),
        ("0.15 of the work", "15", False),
        ("There are 1,150 issues", "1150", True),
        ("There are 1150 issues", "1,150", True),
        ("16% of issues", "16", True),
        ("16% of issues", "16%", True),
        ("16.5% of issues", "16%", False),
        ("ratio 1.5x", "1.5", True),
        ("ratio 1.50", "1.5", False),
        ("", "1", False),
    ],
)
def test_numbers_match_as_whole_tokens(text: str, number: str, found: bool) -> None:
    assert _has_number(text, number) is found


@pytest.mark.parametrize(
    ("text", "key", "found"),
    [
        ("see [DEMO-3] and [DEMO-30]", "DEMO-3", True),
        ("see [DEMO-30]", "DEMO-3", False),
        ("see XDEMO-3", "DEMO-3", False),
        ("DEMO-3_1 is odd", "DEMO-3", False),
        ("plain DEMO-3.", "DEMO-3", True),
    ],
)
def test_keys_match_as_whole_tokens(text: str, key: str, found: bool) -> None:
    assert _has_key(text, key) is found


def test_numbers_and_keys_are_checked_in_the_answer_only(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, tmp_path: Path
) -> None:
    cases = [
        {
            "id": "cycle",
            "question": "Top 3 tickets by cycle time",
            "expected_calls": ["oversight_top_issues"],
            "expected_outcome": "answered",
            "expected_numbers": ["55.0", "55", "5.0"],
            "expected_keys": ["AIL-104", "AIL-10", "AIL-1"],
        }
    ]
    provider = FakeProvider(
        plans=[plan(("oversight_top_issues", {"metric": "cycle_time", "n": 3}))],
        answers=["AIL-104 has the longest cycle time at 55.0 h."],
    )
    report = run(snapshot, rules, settings, provider, cases, tmp_path)
    (result,) = report.results
    assert result.outcome == "answered" and result.plan_ok and result.outcome_ok
    assert result.numbers_ok is False and result.detail["missing_numbers"] == ["55", "5.0"]
    assert result.keys_ok is False and result.detail["missing_keys"] == ["AIL-10", "AIL-1"]
    assert report.passed is False
    text = report.report_path.read_text(encoding="utf-8")
    assert "1 answered case(s) missed a number or key" in text
    assert "- Numbers not found: 55, 5.0." in text and "- Keys not found: AIL-10, AIL-1." in text
    assert "- Answer: AIL-104 has the longest cycle time at 55.0 h." in text


def test_a_wrong_plan_counts_against_the_plan_rate(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, tmp_path: Path
) -> None:
    cases = [dict(FOUR_CASES[0], expected_calls=["oversight_hot_spots"]), *FOUR_CASES[1:]]
    report = run(snapshot, rules, settings, four_provider(), cases, tmp_path)
    assert report.plan_rate == 0.75 and report.outcome_rate == 1.0 and report.passed is False
    assert report.results[0].plan_ok is False and report.results[0].numbers_ok is True
    text = report.render()
    assert "**FAILED**" in text and f"plan rate 75.0% is below {PLAN_RATE_TARGET:.0%}" in text
    assert "## Misses" in text and "### stuck: Where do tickets get stuck most?" in text
    assert "- Plan: expected oversight_hot_spots; got oversight_time_in_status(period=all)." in text
    assert "| 1 | stuck | Where do tickets get stuck most? | answered | answered | MISS | ok | ok |" in text


def test_a_not_verified_answer_is_a_missed_outcome_but_not_a_shipped_number(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, tmp_path: Path
) -> None:
    provider = four_provider(answers=["The median is 999 hours.", "Still 999 hours."])
    report = run(snapshot, rules, settings, provider, FOUR_CASES, tmp_path)
    stuck = report.results[0]
    assert stuck.outcome == "not_verified" and stuck.outcome_ok is False and stuck.plan_ok is True
    assert stuck.numbers_ok is False and stuck.detail["answer"] is None
    assert report.outcome_rate == 0.75 and report.plan_rate == 1.0 and report.passed is True
    text = report.render()
    assert "- Outcome: expected answered; got not_verified." in text
    assert "- Message: The written answer did not match the facts, so only the facts are shown." in text
    assert "- Answer: (none)" in text and "- Answered cases with every expected number and key: 0/0." in text


def test_a_slow_median_fails_the_eval(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter(range(0, 10_000, 6))  # ask() reads the clock twice: every answer appears to take 6 s

    monkeypatch.setattr(time, "perf_counter", lambda: float(next(ticks)))
    report = run(snapshot, rules, settings, four_provider(), FOUR_CASES, tmp_path)
    assert report.median_latency_s == 6.0 and report.passed is False
    assert all(r.latency_s == 6.0 for r in report.results)
    assert f"median latency 6.00 s is over {MEDIAN_LATENCY_TARGET_S:.0f} s" in report.render()


def test_an_empty_report_cannot_pass() -> None:
    report = EvalReport(results=[], plan_rate=0.0, outcome_rate=0.0, median_latency_s=0.0, passed=False)
    text = report.render()
    assert "0 cases, 0/0 plans right (0%)" in text and "- Result: FAILED (no cases" in text
    minimal = EvalResult(id="x", plan_ok=True, outcome_ok=True, numbers_ok=True, keys_ok=True, outcome="answered", latency_s=0.5)
    assert "| 1 | x | x | ? | answered | ok | ok | ok | 0.50 |" in EvalReport([minimal], 1.0, 1.0, 0.5, True).render()


def test_load_cases_reads_a_list_or_a_mapping_with_cases(tmp_path: Path) -> None:
    as_list = load_cases(write_cases(tmp_path / "list.yaml", FOUR_CASES))
    assert [case.id for case in as_list] == ["stuck", "person", "review", "unassigned"]
    assert as_list[0] == EvalCase(
        id="stuck",
        question=STUCK,
        expected_calls=["oversight_time_in_status"],
        expected_outcome="answered",
        expected_numbers=["24.0", "15", "1.0"],
        expected_keys=[],
    )
    assert as_list[1].expected_numbers == [] and as_list[1].expected_keys == []
    as_mapping = load_cases(write_cases(tmp_path / "mapping.yaml", {"project": DEMO_PROJECT, "cases": FOUR_CASES}))
    assert as_mapping == as_list
    loose = [{"id": 7, "question": "  How many?  ", "expected_calls": [], "expected_outcome": "no_data", "expected_numbers": [736, 1.5, " 16% "]}]
    (case,) = load_cases(write_cases(tmp_path / "loose.yaml", loose))
    assert (case.id, case.question, case.expected_numbers) == ("7", "How many?", ["736", "1.5", "16%"])


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ({"not": "a list"}, "expected a non-empty list"),
        ([], "expected a non-empty list"),
        (["just a string"], "case 1: each case must be a mapping"),
        ([{"question": "q", "expected_calls": [], "expected_outcome": "answered"}], "case 1: missing id"),
        ([{"id": "a", "expected_calls": [], "expected_outcome": "answered"}], "missing question"),
        ([{"id": "", "question": "q", "expected_calls": [], "expected_outcome": "answered"}], "id must be a non-empty string"),
        ([{"id": "a", "question": "", "expected_calls": [], "expected_outcome": "answered"}], "question must be a non-empty string"),
        ([{"id": "a", "question": "q", "expected_calls": [], "expected_outcome": "answered", "expected_number": ["1"]}], "unknown keys expected_number"),
        ([{"id": "a", "question": "q", "expected_calls": [], "expected_outcome": "maybe"}], "expected_outcome must be one of answered"),
        ([{"id": "a", "question": "q", "expected_calls": ["oversight_nope"], "expected_outcome": "answered"}], "unknown tools oversight_nope"),
        ([{"id": "a", "question": "q", "expected_calls": "oversight_hot_spots", "expected_outcome": "answered"}], "expected_calls must be a list"),
        ([{"id": "a", "question": "q", "expected_calls": [], "expected_outcome": "answered", "expected_numbers": "1"}], "expected_numbers must be a list"),
        ([{"id": "a", "question": "q", "expected_calls": [], "expected_outcome": "answered", "expected_numbers": [True]}], "numbers or strings"),
        ([{"id": "a", "question": "q", "expected_calls": [], "expected_outcome": "answered", "expected_keys": ["demo-1"]}], "must be issue keys"),
        (
            [
                {"id": "a", "question": "q", "expected_calls": [], "expected_outcome": "answered"},
                {"id": "a", "question": "r", "expected_calls": [], "expected_outcome": "answered"},
            ],
            "case 2: duplicate id 'a'",
        ),
    ],
)
def test_load_cases_rejects_bad_files(tmp_path: Path, content: Any, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_cases(write_cases(tmp_path / "bad.yaml", content))


def test_load_cases_needs_the_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_cases(tmp_path / "missing.yaml")


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Store, Settings, ProcessConfig, RulesConfig]]:
    """The default demo seeded, derived and rules-run at DEMO_NOW: the data questions.yaml was computed on."""
    root = tmp_path_factory.mktemp("eval-demo")
    settings = Settings(
        jira_base_url="https://fake.atlassian.net",
        jira_email="dev@example.com",
        jira_api_token="test-token",
        jira_projects=DEMO_PROJECT,
        db_path=root / "data" / "oversight.db",
        config_dir=CONFIG_FIXTURES,
        reports_dir=root / "reports",
        log_format="console",
    )
    process = ProcessConfig.load(CONFIG_FIXTURES / "process.yaml")
    rules = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")
    fields = FieldsConfig.load(CONFIG_FIXTURES / "fields.yaml")
    store = Store.open(settings.db_file)
    seed_demo(store, now=DEMO_NOW, seed=DEMO_SEED, project=DEMO_PROJECT, issues=DEMO_ISSUES)
    run_derive(store, process, projects=[DEMO_PROJECT], now=lambda: DEMO_NOW)
    run_rules(store, process, rules, fields, projects=[DEMO_PROJECT], now=lambda: DEMO_NOW)
    yield store, settings, process, rules
    store.close()


def test_the_shipped_eval_set_is_well_formed() -> None:
    cases = load_cases(QUESTIONS)
    assert len(cases) == 30
    assert [case.id for case in cases] == [f"q{n:02d}" for n in range(1, 31)]
    assert {name for case in cases for name in case.expected_calls} == set(CATALOG)
    outcomes = {case.expected_outcome for case in cases}
    assert {"answered", "not_understood", "not_measurable", "no_data"} <= outcomes <= set(OUTCOMES)
    answered = [case for case in cases if case.expected_outcome == "answered"]
    with_ground_truth = [case for case in answered if case.expected_numbers or case.expected_keys]
    assert len(answered) >= 22 and len(with_ground_truth) >= len(answered) - 2
    header = QUESTIONS.read_text(encoding="utf-8").split("\n- id:")[0]
    for recorded in (str(DEMO_SEED), DEMO_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), DEMO_PROJECT, f"{DEMO_ISSUES:,}"):
        assert recorded in header, recorded


@pytest.fixture(scope="module")
def shipped_report(demo: tuple[Store, Settings, ProcessConfig, RulesConfig]) -> EvalReport:
    """The shipped set asked once on the demo; every test that reads its results or its audit rows uses this."""
    store, settings, process, rules = demo
    return run_qa_eval(
        QUESTIONS,
        store=store,
        process=process,
        rules=rules,
        settings=settings,
        provider=ScriptedProvider(),
        out_path=settings.reports_path / "qa_eval.md",
        now=lambda: DEMO_NOW,
    )


def test_the_shipped_eval_set_passes_on_the_demo(
    demo: tuple[Store, Settings, ProcessConfig, RulesConfig], shipped_report: EvalReport
) -> None:
    _, settings, _, _ = demo
    out_path = settings.reports_path / "qa_eval.md"
    report = shipped_report
    misses = [(r.id, r.outcome, r.detail["planned"], r.detail["missing_numbers"], r.detail["missing_keys"], r.detail["message"]) for r in report.results if not r.ok]
    assert misses == []
    assert report.passed is True and report.plan_rate == 1.0 and report.outcome_rate == 1.0
    assert len(report.results) == 30 and report.median_latency_s <= MEDIAN_LATENCY_TARGET_S
    assert all(r.numbers_ok and r.keys_ok for r in report.results if r.outcome == "answered")
    assert {r.outcome for r in report.results} == {"answered", "not_understood", "not_measurable", "no_data"}
    text = out_path.read_text(encoding="utf-8")
    assert "30 cases, 30/30 plans right (100%), 30/30 outcomes right (100%)" in text and "**PASSED**" in text
    assert text.count("\n| ") == 31
    assert "acc-" not in text and "app-tempo" not in text


def test_the_shipped_eval_set_records_every_question_in_the_audit_trail(
    demo: tuple[Store, Settings, ProcessConfig, RulesConfig], shipped_report: EvalReport
) -> None:
    # shipped_report asks the questions first, whichever of these tests runs (or runs alone).
    store, _, _, _ = demo
    assert len(shipped_report.results) == 30
    rows = store.query("SELECT question, outcome, model FROM qa_log WHERE asked_by = 'eval' ORDER BY asked_at, rowid")
    asked = [row["question"] for row in rows]
    assert len(asked) >= 30 and {case.question for case in load_cases(QUESTIONS)} <= set(asked)
    assert all(row["model"] in (None, "scripted") for row in rows)


def _facts(demo: tuple[Store, Settings, ProcessConfig, RulesConfig], name: str, params: dict[str, Any]) -> Facts:
    store, _, process, rules = demo
    conn = open_readonly(store.path)
    try:
        ctx = CatalogContext(process=process, rules=rules, project=DEMO_PROJECT, now=DEMO_NOW, row_limit=200)
        return run_query(conn, name, params, ctx, ref="F1")
    finally:
        conn.close()


def _percent(share: float) -> str:
    return f"{round(share * 100)}%"


MCP_ANSWERS: dict[str, Callable[[tuple[Store, Settings, ProcessConfig, RulesConfig]], str]] = {
    "How many DEMO issues are in status In Progress right now?": lambda demo: str(
        _facts(demo, "oversight_count_issues", {"status": "In Progress"}).rows[0]["count"]
    ),
    "How many DEMO bugs have no story-point estimate?": lambda demo: str(
        _facts(demo, "oversight_count_issues", {"issue_type": "Bug", "has_estimate": False}).rows[0]["count"]
    ),
    "Which DEMO epic has the most open breaches?": lambda demo: _facts(demo, "oversight_hot_spots", {"n": 1}).rows[0]["epic_key"],
    "How many of the rules in the catalogue cannot be measured in DEMO today?": lambda demo: str(
        sum(1 for row in _facts(demo, "oversight_rule_status", {}).rows if not row["measurable"])
    ),
    "Which rule has the most open findings in DEMO?": lambda demo: max(
        _facts(demo, "oversight_rule_status", {}).rows, key=lambda row: (row["open_findings"], row["rule_id"])
    )["rule_id"],
    "Which DEMO ticket has the highest ratio of logged effort to its estimate?": lambda demo: _facts(
        demo, "oversight_top_issues", {"metric": "effort_ratio", "n": 1}
    ).rows[0]["issue_key"],
    "What share of DEMO issues created in the last 90 days have story points? Answer as a whole percentage.": lambda demo: _percent(
        _facts(demo, "oversight_coverage", {"field": "story_points", "period": "90d"}).rows[0]["share"]
    ),
    "In which status have DEMO tickets spent the longest median time, over status intervals that ended in the last 90 days?": lambda demo: max(
        _facts(demo, "oversight_time_in_status", {"group_by": "status", "period": "90d"}).rows,
        key=lambda row: row["median_h"],
    )["group"],
    "How many status intervals does the timeline of DEMO-1013 have?": lambda demo: str(
        len(_facts(demo, "oversight_issue_timeline", {"issue_key": "DEMO-1013"}).rows)
    ),
    "What is the id of the active DEMO sprint that the 'Sprint at risk' warning (W-SPR-01) is open for?": lambda demo: _facts(
        demo, "oversight_list_findings", {"rule_id": "W-SPR-01"}
    ).rows[0]["issue_key"].removeprefix("sprint:"),
}


def test_the_mcp_eval_xml_is_well_formed() -> None:
    root = ET.parse(MCP_EVAL).getroot()
    assert root.tag == "evaluation"
    pairs = root.findall("qa_pair")
    assert len(pairs) == 10
    questions = [pair.findtext("question") for pair in pairs]
    answers = [pair.findtext("answer") for pair in pairs]
    assert all(q and q.strip() for q in questions) and all(a and a.strip() for a in answers)
    assert len(set(questions)) == 10
    assert all(set(pair.attrib) == set() and [child.tag for child in pair] == ["question", "answer"] for pair in pairs)
    assert [q.strip() for q in questions] == list(MCP_ANSWERS)
    for question in questions:
        assert "DEMO" in question


def test_the_mcp_eval_answers_hold_on_the_demo(demo: tuple[Store, Settings, ProcessConfig, RulesConfig]) -> None:
    pairs = ET.parse(MCP_EVAL).getroot().findall("qa_pair")
    for pair in pairs:
        question, answer = (pair.findtext("question") or "").strip(), (pair.findtext("answer") or "").strip()
        assert MCP_ANSWERS[question](demo) == answer, question


def test_the_cli_eval_command_runs_the_scripted_provider(
    process: ProcessConfig, settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store.open(settings.db_file) as store:
        build_snapshot(store, process)
        in_progress = store.conn.execute(
            "SELECT COUNT(*) FROM issues WHERE project_key = 'AIL' AND status_category = 'indeterminate'"
            " AND lower(issue_type) != 'epic'").fetchone()[0]
    (settings.config_dir / "rules.yaml").write_text((CONFIG_FIXTURES / "rules.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    cases = [
        {
            "id": "count",
            "question": "How many tickets are in progress?",
            "expected_calls": ["oversight_count_issues"],
            "expected_outcome": "answered",
            "expected_numbers": [str(in_progress)],
        },
        {"id": "person", "question": "Who is the slowest developer?", "expected_calls": [], "expected_outcome": "not_understood"},
        {
            "id": "review",
            "question": "Which tickets reached testing without code review?",
            "expected_calls": ["oversight_list_findings"],
            "expected_outcome": "not_measurable",
        },
    ]
    questions = write_cases(tmp_path / "questions.yaml", cases)
    capsys.readouterr()  # drop what building the snapshot logged under structlog's default (stdout) configuration

    assert cli_main(["eval", "--questions", str(questions)], settings=settings) == EXIT_OK
    out = capsys.readouterr().out
    assert "# Q&A eval" in out and "**PASSED**" in out and "3/3 plans right" in out
    report = settings.reports_path / "qa_eval.md"
    assert report.exists() and report.read_text(encoding="utf-8") == out.rstrip("\n") + "\n"

    failing = write_cases(tmp_path / "failing.yaml", [dict(cases[0], expected_calls=["oversight_hot_spots"]), *cases[1:]])
    assert cli_main(["eval", "--questions", str(failing)], settings=settings) == EXIT_CHECKS_FAILED
    assert "**FAILED**" in capsys.readouterr().out

    assert cli_main(["eval", "--questions", str(tmp_path / "missing.yaml")], settings=settings) == EXIT_CONFIG
    assert "config error" in capsys.readouterr().err


def test_the_shipped_set_names_its_dataset() -> None:
    assert load_dataset(QUESTIONS) == EvalDataset(project=DEMO_PROJECT, seed=DEMO_SEED, issues=DEMO_ISSUES, now=DEMO_NOW)


def test_a_plain_list_of_cases_has_no_dataset(tmp_path: Path) -> None:
    assert load_dataset(write_cases(tmp_path / "plain.yaml", FOUR_CASES)) is None
    assert len(load_cases(write_cases(tmp_path / "mapped.yaml", {"cases": FOUR_CASES}))) == 4


@pytest.mark.parametrize(
    ("header", "match"),
    [
        ("not a mapping", "dataset must be a mapping"),
        ({"project": "DEMO", "seed": 1, "issues": 10}, "missing now"),
        ({"project": "DEMO", "seed": 1, "issues": 10, "now": "2026-11-04T12:00:00Z", "extra": 1}, "unknown extra"),
        ({"project": "demo", "seed": 1, "issues": 10, "now": "2026-11-04T12:00:00Z"}, "project key"),
        ({"project": "DEMO", "seed": "x", "issues": 10, "now": "2026-11-04T12:00:00Z"}, "seed must be a whole number"),
        ({"project": "DEMO", "seed": 1, "issues": 0, "now": "2026-11-04T12:00:00Z"}, "issues must be a whole number"),
        ({"project": "DEMO", "seed": 1, "issues": 10, "now": "2026-11-04T12:00:00"}, "UTC timestamp"),
        ({"project": "DEMO", "seed": 1, "issues": 10, "now": "soon"}, "UTC timestamp"),
    ],
)
def test_a_bad_dataset_header_is_a_config_error(tmp_path: Path, header: Any, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_dataset(write_cases(tmp_path / "bad.yaml", {"dataset": header, "cases": FOUR_CASES}))


def test_the_cli_eval_rebuilds_the_dataset_and_never_touches_the_snapshot(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["eval"], settings=settings) == EXIT_OK
    out = capsys.readouterr().out
    assert "30 cases, 30/30 plans right (100%), 30/30 outcomes right (100%)" in out and "**PASSED**" in out
    assert not settings.db_file.exists()
    assert (settings.reports_path / "qa_eval.md").read_text(encoding="utf-8") == out.rstrip("\n") + "\n"


def test_the_cli_eval_needs_the_dataset_project_configured(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    header = {"project": "ZZZ", "seed": 1, "issues": 20, "now": "2026-11-04T12:00:00Z"}
    questions = write_cases(tmp_path / "zzz.yaml", {"dataset": header, "cases": FOUR_CASES})
    assert cli_main(["eval", "--questions", str(questions)], settings=settings) == EXIT_CONFIG
    assert "needs project ZZZ" in capsys.readouterr().err
