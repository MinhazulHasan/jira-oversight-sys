"""POST /ask (contract 4.6) and GET /insights/latest (4.7) against a seeded DEMO store.

Shapes are checked against the zod schemas in the interface's
src/lib/api/schemas.ts (askResponseSchema, insightsSchema); the scripted
provider answers, so nothing leaves the machine.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oversight.api.routes.insights import method_text
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.insights.weekly import latest_insight, run_insights
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC
from tests.test_api_overview import make_client, make_settings

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
FACT_REF = re.compile(r"^F\d+$")
TOOL = re.compile(r"^oversight_[a-z_]+$")
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,19}-[1-9]\d{0,8}$")
OUTCOMES = {"answered", "not_understood", "no_data", "not_measurable", "not_verified"}
ASK_FIELDS = {"meta", "qa_id", "question", "outcome", "answer", "citations", "facts", "plan", "missing_evidence",
              "message", "suggestions", "latency_ms", "model", "asked_at", "people", "note"}
INSIGHT_FIELDS = {"meta", "week_of", "generated_at", "verified", "sentences", "candidates", "method", "model"}


def _seed(settings: Settings, *, briefing: bool) -> None:
    process = ProcessConfig.load(settings.process_file)
    rules = RulesConfig.load(settings.rules_file)
    fields = FieldsConfig.load(settings.fields_file)
    store = Store.open(settings.db_file)
    try:
        seed_demo(store, now=NOW, seed=20261105)
        run_derive(store, process, projects=["DEMO"], now=lambda: NOW)
        run_rules(store, process, rules, fields, projects=["DEMO"], now=lambda: NOW)
        if briefing:
            run_insights(store, process, rules, ScriptedProvider(), settings, projects=["DEMO"], now=lambda: NOW)
    finally:
        store.close()


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    settings = make_settings(tmp_path_factory.mktemp("ask_insights"))
    _seed(settings, briefing=True)
    return settings


@pytest.fixture(scope="module")
def client(demo: Settings) -> TestClient:
    return make_client(demo, NOW)


def ask(client: TestClient, question: str) -> dict[str, Any]:
    response = client.post("/api/v1/ask", json={"question": question})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ASK_FIELDS
    assert HEX32.match(body["qa_id"]) and body["outcome"] in OUTCOMES and ISO_Z.match(body["asked_at"])
    assert body["meta"]["projects"] == ["DEMO"] and body["meta"]["data_mode"] == "live"
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    for step in body["plan"]:
        assert set(step) == {"tool", "params"} and TOOL.match(step["tool"])
    for facts in body["facts"]:
        assert FACT_REF.match(facts["ref"]) and TOOL.match(facts["tool"])
        assert {"params", "columns", "rows", "truncated", "as_of", "note"} <= set(facts)
    return body


# --- POST /ask ------------------------------------------------------------------------------


def test_an_answer_carries_its_plan_facts_and_citations(client: TestClient) -> None:
    body = ask(client, "Where do tickets get stuck most?")
    assert body["outcome"] == "answered"
    assert body["answer"] and body["message"] is None
    assert body["plan"][0]["tool"] == "oversight_time_in_status"
    assert body["facts"] and body["citations"]
    for citation in body["citations"]:
        assert set(citation) == {"marker", "text", "fact_ref", "row_index", "column"}
        assert citation["text"] in body["answer"] and FACT_REF.match(citation["fact_ref"])
    assert [citation["marker"] for citation in body["citations"]] == list(range(1, len(body["citations"]) + 1))


def test_a_question_about_a_person_is_declined_at_team_level(client: TestClient) -> None:
    body = ask(client, "Who is the slowest developer?")
    assert body["outcome"] == "not_understood"
    assert body["answer"] is None and body["citations"] == [] and body["people"] == []
    assert body["message"] == "Ask answers at team level. To see one person's findings, use the Person filter on Findings."


def test_a_blank_question_is_not_understood_and_suggests_some(client: TestClient) -> None:
    body = ask(client, "   ")
    assert body["outcome"] == "not_understood" and body["suggestions"]


def test_the_body_takes_only_a_question_of_at_most_2000_characters(client: TestClient) -> None:
    assert client.post("/api/v1/ask", json={"question": "x", "user": "someone"}).status_code == 422
    assert client.post("/api/v1/ask", json={"question": "x" * 2001}).status_code == 422
    assert client.post("/api/v1/ask", json={}).status_code == 422


def test_every_question_is_recorded_in_qa_log(client: TestClient, demo: Settings) -> None:
    body = ask(client, "Which epics have the most open breaches?")
    conn = open_readonly(demo.db_file)
    try:
        row = conn.execute("SELECT asked_by FROM qa_log WHERE qa_id = ?", (body["qa_id"],)).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "dashboard"


def test_ask_needs_the_service_key(demo: Settings) -> None:
    anonymous = TestClient(make_client(demo, NOW).app)
    assert anonymous.post("/api/v1/ask", json={"question": "Where do tickets get stuck most?"}).status_code == 401


# --- GET /insights/latest -------------------------------------------------------------------


def test_the_latest_briefing_matches_the_contract(client: TestClient, demo: Settings) -> None:
    response = client.get("/api/v1/insights/latest")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == INSIGHT_FIELDS
    assert body["week_of"] == "2026-11-02" and ISO_Z.match(body["generated_at"])
    assert body["verified"] is True and body["sentences"] and body["candidates"]
    for sentence in body["sentences"]:
        assert set(sentence) == {"id", "text", "tone", "candidate_ids", "issue_keys", "finding_ids"}
        assert sentence["tone"] in {"blatantly_bad", "trending_bad", "neutral"}
        assert all(ISSUE_KEY.match(key) for key in sentence["issue_keys"])
    ids = {candidate["id"] for candidate in body["candidates"]}
    assert all(set(sentence["candidate_ids"]) <= ids for sentence in body["sentences"])
    for candidate in body["candidates"]:
        assert {"id", "kind", "title", "score", "affected", "severity", "trend", "detail", "issue_keys",
                "finding_ids"} <= set(candidate)
        assert candidate["score"] >= 0 and candidate["trend"] >= 0
    scores = [candidate["score"] for candidate in body["candidates"]]
    assert scores == sorted(scores, reverse=True)
    assert "A script (LLM_MODE=stub)" in body["method"] and body["model"] == "scripted"


def test_no_briefing_yet_is_a_404(tmp_path_factory: pytest.TempPathFactory) -> None:
    settings = make_settings(tmp_path_factory.mktemp("no_briefing"))
    _seed(settings, briefing=False)
    response = make_client(settings, NOW).get("/api/v1/insights/latest")
    assert response.status_code == 404
    assert "python -m oversight insights" in response.json()["detail"]


def test_the_method_names_the_model_and_says_when_the_check_failed(demo: Settings) -> None:
    conn = open_readonly(demo.db_file)
    try:
        insight = latest_insight(conn, "DEMO")
    finally:
        conn.close()
    assert insight is not None
    insight.model = "claude-opus-5-5"
    assert "The model claude-opus-5-5 wrote the briefing" in method_text(insight)
    insight.verified = False
    assert "did not pass the check" in method_text(insight)
