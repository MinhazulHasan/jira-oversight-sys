"""The Ask analyst: the read-only query tool, and the conversation loop with a scripted model.

Nothing here calls a real model: FakeAnalyst hands back scripted turns with the
SDK's shapes (tool_use blocks with .id, .name and .input).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oversight.ask.analyst import QUERY_TOOL, QueryError, SnapshotQuery, analyst_system, column_kind, tidy_markdown
from oversight.ask.prompts import CREATIVE_DECLINE
from oversight.ask.service import ask
from oversight.config import ProcessConfig
from oversight.llm.provider import ConverseResult, LLMError, Usage
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import UTC
from tests.test_api_ask_insights import _seed
from tests.test_api_overview import make_settings

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    settings = make_settings(tmp_path_factory.mktemp("analyst"))
    _seed(settings, briefing=False)
    return settings


@pytest.fixture
def query(demo: Settings) -> SnapshotQuery:
    snapshot = SnapshotQuery(demo.db_file, project="DEMO", sprint_field=None, rules=RulesConfig.load(demo.rules_file))
    yield snapshot
    snapshot.close()


def epic_count(settings: Settings) -> int:
    conn = sqlite3.connect(settings.db_file)
    try:
        return conn.execute("SELECT COUNT(*) FROM issues WHERE project_key = 'DEMO' AND issue_type = 'Epic'").fetchone()[0]
    finally:
        conn.close()


# --- the query tool -------------------------------------------------------------------------


def test_views_answer_and_count_through_with_and_json(query: SnapshotQuery) -> None:
    columns, rows, more = query.run("SELECT issue_type, COUNT(*) AS n FROM issues GROUP BY 1 ORDER BY 2 DESC")
    assert columns == ["issue_type", "n"] and rows and not more
    assert query.run("WITH t AS (SELECT status FROM issues) SELECT COUNT(*) AS n FROM t")[1][0][0] > 0
    assert query.run("SELECT COUNT(*) AS n FROM rules")[1][0][0] == 12
    assert query.run("SELECT COUNT(*) AS n FROM issue_metrics, json_each(issue_metrics.skipped_stages)")[1]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM qa_log",
        "WITH issues AS (SELECT * FROM qa_log) SELECT * FROM issues",
        "WITH epics AS (SELECT name FROM statuses) SELECT * FROM epics",
        "SELECT sql FROM sqlite_temp_master",
        "SELECT * FROM pragma_database_list",
        "SELECT load_extension('x')",
    ],
)
def test_reads_outside_the_views_are_refused(query: SnapshotQuery, sql: str) -> None:
    with pytest.raises(QueryError, match="not allowed"):
        query.run(sql)


@pytest.mark.parametrize("sql", ["SELECT assignee_id FROM issues", "SELECT raw_json FROM issues", "SELECT author_id FROM worklogs"])
def test_person_identifiers_and_raw_jira_json_are_not_in_the_views(query: SnapshotQuery, sql: str) -> None:
    with pytest.raises(QueryError, match="no such column"):
        query.run(sql)


@pytest.mark.parametrize("sql", ["DELETE FROM rules", "PRAGMA table_info(issues)", "ATTACH DATABASE 'x.db' AS x", "DROP VIEW issues"])
def test_only_select_runs(query: SnapshotQuery, sql: str) -> None:
    with pytest.raises(QueryError, match="only a single SELECT"):
        query.run(sql)


def test_the_snapshot_schema_cannot_be_named(query: SnapshotQuery) -> None:
    with pytest.raises(QueryError, match="not allowed"):
        query.run(f"SELECT assignee_id FROM {query.schema}.issues")


def test_a_runaway_query_stops_at_the_time_limit(demo: Settings) -> None:
    quick = SnapshotQuery(demo.db_file, project="DEMO", sprint_field=None, rules=RulesConfig.load(demo.rules_file), time_limit_s=0.2)
    try:
        with pytest.raises(QueryError, match="ran over"):
            quick.run("WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT COUNT(*) FROM r")
    finally:
        quick.close()


def test_rows_are_capped_and_the_cap_is_said(demo: Settings) -> None:
    capped = SnapshotQuery(demo.db_file, project="DEMO", sprint_field=None, rules=RulesConfig.load(demo.rules_file), row_limit=5)
    try:
        _, rows, more = capped.run("SELECT issue_key FROM issues")
    finally:
        capped.close()
    assert len(rows) == 5 and more


def test_another_project_is_out_of_reach(query: SnapshotQuery) -> None:
    assert query.run("SELECT COUNT(*) FROM issues WHERE issue_key NOT LIKE 'DEMO-%'")[1][0][0] == 0


@pytest.mark.parametrize(
    ("name", "values", "kind"),
    [
        ("issue_key", ["AIL-1", "AIL-22"], "issue_key"),
        ("created_at", ["2026-11-04T10:00:00.000Z"], "date"),
        ("cycle_time_h", [12.5, 3.0], "hours"),
        ("n", [3, 4], "count"),
        ("done_share", [0.25, 0.5], "share"),
        ("effort_ratio", [1.2], "ratio"),
        ("story_points", [3, 5.5], "number"),
        ("summary", ["Build it"], "text"),
        ("empty", [None], "text"),
    ],
)
def test_column_kinds(name: str, values: list[Any], kind: str) -> None:
    assert column_kind(name, values) == kind


def test_tidy_markdown_keeps_markdown_but_not_links_brackets_or_list_numbers() -> None:
    text = "**Open epics**\n\n1. [AIL-1](https://x.atlassian.net/browse/AIL-1) first\n2. [AIL-2] second"
    assert tidy_markdown(text) == "**Open epics**\n\n- AIL-1 first\n- AIL-2 second"


# --- the conversation -----------------------------------------------------------------------


def use(name: str, arguments: dict[str, Any], ident: str = "tu1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=ident, name=name, input=arguments)


def turn(*, text: str = "", uses: list[SimpleNamespace] | None = None) -> ConverseResult:
    content: list[Any] = ([SimpleNamespace(type="text", text=text)] if text else []) + list(uses or [])
    return ConverseResult(content=content, text=text, tool_uses=list(uses or []), model="fake-opus",
                          stop_reason="tool_use" if uses else "end_turn", usage=Usage(100, 20), latency_ms=5)


class FakeAnalyst:
    """A live-looking provider: converse() returns the scripted turns in order."""

    name = "fake"

    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def converse(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> ConverseResult:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools, **kwargs})
        item = self.turns.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    # The catalogue path's calls: never reached when the analyst runs.
    def structured(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the catalogue planner ran instead of the analyst")

    compose = structured


def run(demo: Settings, provider: FakeAnalyst, question: str = "How many epics are there?") -> Any:
    process = ProcessConfig.load(demo.process_file)
    rules = RulesConfig.load(demo.rules_file)
    store = Store.open(demo.db_file)
    try:
        return ask(question, store=store, process=process, rules=rules, settings=demo, provider=provider, now=lambda: NOW), store
    finally:
        pass


def purposes(store: Store, qa_id: str) -> list[str]:
    return [row[0] for row in store.query("SELECT purpose FROM llm_calls WHERE qa_id = ? ORDER BY called_at, rowid", (qa_id,))]


def test_the_analyst_queries_then_answers_with_verified_citations(demo: Settings) -> None:
    n = epic_count(demo)
    provider = FakeAnalyst([
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"})]),
        turn(text=f"DEMO has **{n} epics**."),
    ])
    response, store = run(demo, provider)
    try:
        assert response.outcome == "answered" and response.answer == f"DEMO has **{n} epics**."
        assert [c.text for c in response.citations] == [str(n)]
        assert [(f.ref, f.tool) for f in response.facts] == [("F1", QUERY_TOOL)]
        assert response.facts[0].params == {"sql": "SELECT COUNT(*) AS n FROM epics"}
        assert [c.name for c in response.plan] == [QUERY_TOOL] and response.model == "fake-opus"
        assert purposes(store, response.qa_id) == ["analyst", "analyst"]
        # The model read the result back as a tool_result for its own tool_use id.
        result = provider.calls[1]["messages"][-1]["content"][0]
        assert result["type"] == "tool_result" and result["tool_use_id"] == "tu1" and f"[{n}]" in result["content"]
        assert "AIL" not in provider.calls[0]["system"] or "DEMO" in provider.calls[0]["system"]
        assert {tool["name"] for tool in provider.calls[0]["tools"]} >= {QUERY_TOOL, "oversight_hot_spots"}
    finally:
        store.close()


def test_a_bad_query_comes_back_as_a_tool_error_and_the_model_tries_again(demo: Settings) -> None:
    n = epic_count(demo)
    provider = FakeAnalyst([
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT assignee_id FROM issues"}, "a")]),
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"}, "b")]),
        turn(text=f"There are {n} epics."),
    ])
    response, store = run(demo, provider)
    store.close()
    error = provider.calls[1]["messages"][-1]["content"][0]
    assert error["is_error"] is True and "no such column" in error["content"]
    assert response.outcome == "answered" and [f.ref for f in response.facts] == ["F1"]


def test_an_unverifiable_answer_gets_one_rewrite_then_is_withheld(demo: Settings) -> None:
    provider = FakeAnalyst([
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"})]),
        turn(text="There are 987654 epics."),
        turn(text="There are still 987654 epics."),
    ])
    response, store = run(demo, provider)
    try:
        assert response.outcome == "not_verified" and response.answer is None and response.facts
        # The rewrite may still query: whatever it computed itself, it can now look up.
        assert provider.calls[2].get("tool_choice") is None
        assert "could not be verified" in provider.calls[2]["messages"][-1]["content"]
        assert "987654" in provider.calls[2]["messages"][-1]["content"]
        assert purposes(store, response.qa_id) == ["analyst", "analyst", "analyst_strict"]
    finally:
        store.close()


def test_the_rewrite_can_query_what_it_computed_itself(demo: Settings) -> None:
    n = epic_count(demo)
    provider = FakeAnalyst([
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT epic_key FROM epics"}, "a")]),
        turn(text="There are 987654 epics."),
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"}, "b")]),
        turn(text=f"There are {n} epics."),
    ])
    response, store = run(demo, provider)
    try:
        assert response.outcome == "answered" and [f.ref for f in response.facts] == ["F1", "F2"]
        assert purposes(store, response.qa_id) == ["analyst", "analyst", "analyst_strict", "analyst_strict"]
    finally:
        store.close()


def test_an_empty_answer_is_never_accepted(demo: Settings) -> None:
    provider = FakeAnalyst([
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"})]),
        turn(text=""),
        turn(text="   "),
    ])
    response, store = run(demo, provider)
    store.close()
    assert response.outcome == "not_verified" and response.answer is None
    assert "the answer was empty" in provider.calls[2]["messages"][-1]["content"]


def test_the_rewrite_can_fix_the_answer(demo: Settings) -> None:
    n = epic_count(demo)
    provider = FakeAnalyst([
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"})]),
        turn(text="There are 987654 epics."),
        turn(text=f"There are {n} epics."),
    ])
    response, store = run(demo, provider)
    store.close()
    assert response.outcome == "answered" and response.answer == f"There are {n} epics."


def test_a_count_written_in_words_is_checked_as_strictly_as_one_in_digits(demo: Settings) -> None:
    """18 Nov 2026: a poem wrote its counts in words ("Seventeen tickets ... nine breaches") and was marked "Numbers
    verified" with none of them checked. A count in words that no tool result holds is caught, and rewritten or
    withheld, as one in digits is."""
    provider = FakeAnalyst([
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"})]),
        turn(text="There are nine hundred and eighty-seven epics."),
        turn(text="There are still nine hundred and eighty-seven epics."),
    ])
    response, store = run(demo, provider)
    store.close()
    assert response.outcome == "not_verified" and response.answer is None
    assert "unverified number nine hundred and eighty-seven" in provider.calls[2]["messages"][-1]["content"]


def test_a_request_for_a_poem_is_declined_before_any_model(demo: Settings) -> None:
    provider = FakeAnalyst([])  # nothing scripted: any call would fail
    response, store = run(demo, provider, "Write a short poem about our sprint.")
    store.close()
    assert (response.outcome, response.message) == ("not_understood", CREATIVE_DECLINE)
    assert provider.calls == [] and response.facts == []


@pytest.mark.parametrize("level", ["team", "people"])
def test_the_analyst_is_told_to_answer_short_plain_and_with_facts_only(level: str) -> None:
    """18 Nov 2026, live: "Where do tickets get stuck most?" took 250 words, a five-column table and 39 figures, wrote
    "0.0006 h", stated an average as "each" and judged the work ("an abandoned backlog"); a count question added "None
    of them are sprint-level findings." and the assignee caveat where nobody was ranked. The writing rules say each of
    these, and the number check stays as it was."""
    system = analyst_system("AIL", "2026-11-18", 6, level)
    for rule in ("About 80 words or fewer", "no sentence about what the answer leaves out",
                 "A table only when the question asks for one", "never opinions", 'is not "each"',
                 "as digits (17, not seventeen)", "one decimal place", "never verse, a song, a joke or a story",
                 "Every number and issue key you write must appear in a tool result"):
        assert rule in system, rule
    if level == "people":
        assert "Only when you rank two or more people by their findings" in system


def test_a_question_that_needs_no_data_is_not_understood_with_the_model_s_words(demo: Settings) -> None:
    provider = FakeAnalyst([turn(text="This tool works at team and process level, so it does not rank people.")])
    response, store = run(demo, provider, "Who is the slowest developer?")
    store.close()
    assert response.outcome == "not_understood" and response.facts == []
    assert response.message == "This tool works at team and process level, so it does not rank people."


def test_the_last_turn_may_only_write(demo: Settings) -> None:
    demo_short = demo.model_copy(update={"ask_max_steps": 1})
    n = epic_count(demo)
    provider = FakeAnalyst([
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"})]),
        turn(text=f"{n} epics."),
    ])
    response, store = run(demo_short, provider)
    store.close()
    assert provider.calls[0].get("tool_choice") is None and provider.calls[1]["tool_choice"] == {"type": "none"}
    assert response.outcome == "answered"


def test_a_model_failure_after_some_facts_shows_the_facts(demo: Settings) -> None:
    provider = FakeAnalyst([
        turn(uses=[use(QUERY_TOOL, {"sql": "SELECT COUNT(*) AS n FROM epics"})]),
        LLMError("the model is unavailable", retryable=True),
    ])
    response, store = run(demo, provider)
    store.close()
    assert response.outcome == "not_verified" and response.facts


def test_catalogue_mode_and_in_memory_stores_keep_the_planner(demo: Settings) -> None:
    from oversight.ask.service import _analyst_available

    store = Store.open(demo.db_file)
    try:
        assert _analyst_available(demo, FakeAnalyst([]), store)
        assert not _analyst_available(demo.model_copy(update={"ask_mode": "catalog"}), FakeAnalyst([]), store)
        assert not _analyst_available(demo, SimpleNamespace(name="scripted"), store)
    finally:
        store.close()
    assert Path(demo.db_file).exists()
