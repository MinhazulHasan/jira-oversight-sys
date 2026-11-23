"""JO-12, Ask's guardrails and the usage figures: ASK_RATE_LIMIT per viewer (429 ask_rate_limited, Retry-After,
nothing logged, no model), LLM_DAILY_TOKEN_BUDGET (Ask answers without the model and says so; insights skip; resets at
00:00 UTC), and GET /usage (contract 4.17, people level only). No network: the model is a stand-in that fails the test
if it is ever asked.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from structlog.testing import capture_logs

from oversight.api.app import create_app
from oversight.api.ratelimit import RateLimiter
from oversight.config import ProcessConfig
from oversight.insights.weekly import run_insights
from oversight.llm.budget import BUDGET_NOTE, budget_reached, spent_today, usage_report
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC
from tests.conftest import CONFIG_FIXTURES
from tests.test_api_ask_insights import ASK_FIELDS, _seed
from tests.test_api_overview import KEY, make_settings
from tests.test_governance import ADMIN, OTHER, Clock, headers

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
TODAY = "2026-11-04"


class NoModel:
    """A model that fails the test the moment anything asks it."""

    name = "anthropic"
    model = "no-model"

    def structured(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the model was asked")

    compose = converse = structured


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    settings = make_settings(tmp_path_factory.mktemp("guardrails"))
    _seed(settings, briefing=False)
    return settings


@pytest.fixture
def calls(demo: Settings) -> Settings:
    """The demo with no model calls recorded, for a test that sets today's spend."""
    conn = sqlite3.connect(demo.db_file)
    try:
        conn.execute("DELETE FROM llm_calls")
        conn.commit()
    finally:
        conn.close()
    return demo


def client_of(settings: Settings, *, provider: Any = None, clock: Clock | None = None, **sent: Any) -> TestClient:
    app = create_app(settings, provider=provider or ScriptedProvider(), now=clock or (lambda: NOW))
    return TestClient(app, headers=headers(**sent))


def count(settings: Settings, table: str) -> int:
    conn = open_readonly(settings.db_file)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def spend(settings: Settings, *calls: tuple[str, str, int, int]) -> None:
    """llm_calls rows: (called_at, provider, input tokens, output tokens)."""
    with Store.open(settings.db_file) as store:
        for n, (called_at, provider, given, written) in enumerate(calls):
            store.upsert("llm_calls", {"call_id": f"spent-{called_at}-{n}", "called_at": called_at, "purpose": "compose",
                                       "provider": provider, "input_tokens": given, "output_tokens": written,
                                       "outcome": "ok"})


# --- the rate limit ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "rule"), [("off", None), ("OFF", None), ("30/10m", (30, 600)), (" 5/1M ", (5, 60)),
                                              ("1/1440m", (1, 86400))])
def test_ask_rate_limit_is_off_or_a_count_per_minutes(value: str, rule: tuple[int, int] | None) -> None:
    assert Settings(_env_file=None, ask_rate_limit=value).ask_rate_limit_rule == rule


@pytest.mark.parametrize("value", ["", "on", "0/5m", "5/0m", "5/1441m", "5/m", "/5m", "5/5", "5/5h", "-1/5m",
                                   "5 per minute", "05/5m"])
def test_any_other_rate_limit_is_a_config_error_at_start(value: str) -> None:
    with pytest.raises(ValidationError, match="ASK_RATE_LIMIT must be off or <count>/<n>m"):
        Settings(_env_file=None, ask_rate_limit=value)


def test_the_limit_is_off_by_default() -> None:
    assert Settings(_env_file=None).ask_rate_limit_rule is None


def test_the_window_slides_and_a_refused_question_does_not_count() -> None:
    limiter = RateLimiter(2, 60)
    assert limiter.admit("a", 0) is None and limiter.admit("a", 10) is None
    assert limiter.admit("a", 30) == 30  # the oldest leaves the window at 60
    assert limiter.admit("a", 59.5) == 1  # rounded up, never 0
    assert limiter.admit("b", 59.5) is None  # each viewer has their own
    assert limiter.admit("a", 60) is None  # the first has left; the refused ones never counted
    assert limiter.admit("a", 61) == 9
    with pytest.raises(ValueError):
        RateLimiter(0, 60)


def test_many_viewers_are_swept_once_their_windows_empty() -> None:
    limiter = RateLimiter(1, 60)
    for n in range(1100):
        limiter.admit(f"viewer-{n}", n * 0.01)
    limiter.admit("late", 1000)
    assert len(limiter._asked) < 1100


def test_over_the_limit_a_question_is_refused_with_retry_after_and_nothing_else_happens(demo: Settings) -> None:
    clock = Clock(NOW)
    limited = demo.model_copy(update={"ask_rate_limit": "2/10m"})
    api = client_of(limited, clock=clock)
    question = {"question": "Where do tickets get stuck most?"}
    assert api.post("/api/v1/ask", json=question).status_code == 200
    clock.at = NOW + timedelta(minutes=4)
    assert api.post("/api/v1/ask", json={**question, "level": "people"}).status_code == 200
    asked, called, logged = count(demo, "qa_log"), count(demo, "llm_calls"), count(demo, "access_log")

    clock.at = NOW + timedelta(minutes=5)
    for body in (question, {"question": "How is @Ada Brooks doing?", "level": "people"}):
        refused = api.post("/api/v1/ask", json=body)
        assert refused.status_code == 429
        assert refused.json() == {"detail": "too many questions in a short time: try again in 300 seconds",
                                  "code": "ask_rate_limited"}
        assert refused.headers["retry-after"] == "300"  # until the first question leaves the window
        assert refused.headers["cache-control"] == "no-store"
    assert (count(demo, "qa_log"), count(demo, "llm_calls"), count(demo, "access_log")) == (asked, called, logged)

    # Another viewer has a limit of their own; and once the first question leaves the window, one more is let in.
    assert api.post("/api/v1/ask", json=question, headers={"X-Oversight-Viewer": OTHER}).status_code == 200
    clock.at = NOW + timedelta(minutes=10)
    assert api.post("/api/v1/ask", json=question).status_code == 200
    assert api.post("/api/v1/ask", json=question).status_code == 429


def test_with_a_limit_set_every_question_names_its_viewer(demo: Settings) -> None:
    limited = demo.model_copy(update={"ask_rate_limit": "5/1m"})
    unnamed = client_of(limited, viewer=None, name=None)
    response = unnamed.post("/api/v1/ask", json={"question": "Where do tickets get stuck most?"})
    assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["header", "X-Oversight-Viewer"]


def test_with_the_limit_off_no_one_is_ever_limited(demo: Settings) -> None:
    api = client_of(demo)
    for _ in range(40):
        assert api.post("/api/v1/ask", json={"question": "   "}).status_code == 200


# --- the daily model budget --------------------------------------------------------------------------------------


def test_the_budget_is_off_by_default_and_never_negative() -> None:
    assert Settings(_env_file=None).llm_daily_token_budget == 0
    with pytest.raises(ValidationError, match="LLM_DAILY_TOKEN_BUDGET must be 0"):
        Settings(_env_file=None, llm_daily_token_budget=-1)


def test_todays_spend_is_the_models_tokens_since_midnight_utc(calls: Settings) -> None:
    spend(calls, (f"{TODAY}T00:00:00.000Z", "anthropic", 600, 300), (f"{TODAY}T23:59:59.999Z", "bedrock", 50, 50),
          ("2026-11-03T23:59:59.999Z", "anthropic", 5000, 5000),  # yesterday
          (f"{TODAY}T09:00:00.000Z", "scripted", 9000, 9000))  # no model behind it
    conn = open_readonly(calls.db_file)
    try:
        assert spent_today(conn, NOW) == 1000
        assert budget_reached(conn, calls.model_copy(update={"llm_daily_token_budget": 1000}), NOW)
        assert not budget_reached(conn, calls.model_copy(update={"llm_daily_token_budget": 1001}), NOW)
        assert not budget_reached(conn, calls, NOW)  # 0: no limit
        # It resets at 00:00 UTC.
        midnight = datetime(2026, 11, 5, tzinfo=UTC)
        assert spent_today(conn, midnight - timedelta(milliseconds=1)) == 1000 and spent_today(conn, midnight) == 0
    finally:
        conn.close()


@pytest.mark.parametrize("level", ["team", "people"])
def test_once_the_budget_is_used_up_ask_answers_without_the_model_and_says_so(calls: Settings, level: str) -> None:
    spend(calls, (f"{TODAY}T08:00:00.000Z", "anthropic", 700, 300))
    api = client_of(calls.model_copy(update={"llm_daily_token_budget": 1000, "llm_mode": "live"}), provider=NoModel())
    response = api.post("/api/v1/ask", json={"question": "Where do tickets get stuck most?", "level": level})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ASK_FIELDS
    assert body["outcome"] == "answered" and body["answer"] and body["note"] == BUDGET_NOTE
    assert body["plan"] and body["facts"]  # the catalogue, planned and written in code
    conn = open_readonly(calls.db_file)
    try:
        providers = {row[0] for row in conn.execute("SELECT provider FROM llm_calls WHERE qa_id = ?", (body["qa_id"],))}
    finally:
        conn.close()
    assert providers <= {"scripted"}  # no model call; nothing taken from the budget


def test_under_the_budget_and_with_the_scripted_provider_there_is_no_note(calls: Settings) -> None:
    spend(calls, (f"{TODAY}T08:00:00.000Z", "anthropic", 700, 300))
    stub = client_of(calls.model_copy(update={"llm_daily_token_budget": 1000}))  # LLM_MODE=stub: no model anyway
    assert stub.post("/api/v1/ask", json={"question": "Where do tickets get stuck most?"}).json()["note"] is None
    roomy = client_of(calls.model_copy(update={"llm_daily_token_budget": 1001}))
    assert roomy.post("/api/v1/ask", json={"question": "Where do tickets get stuck most?"}).json()["note"] is None


def test_once_the_budget_is_used_up_the_insights_skip_with_a_log_line(calls: Settings) -> None:
    spend(calls, (f"{TODAY}T08:00:00.000Z", "anthropic", 1000, 0))
    settings = calls.model_copy(update={"llm_daily_token_budget": 1000, "llm_mode": "live"})
    process, rules = ProcessConfig.load(settings.process_file), RulesConfig.load(settings.rules_file)
    before = count(calls, "insights")
    with Store.open(settings.db_file) as store, capture_logs() as captured:
        written = run_insights(store, process, rules, NoModel(), settings, projects=["DEMO"], now=lambda: NOW)
        run = store.last_run("insights")
    assert written == [] and count(calls, "insights") == before
    skipped = [line for line in captured if line["event"] == "insights_skipped_budget"]
    assert [(line["project"], line["budget"]) for line in skipped] == [("DEMO", 1000)]
    assert run is not None and '"skipped": "budget"' in run["stats_json"]


# --- GET /usage --------------------------------------------------------------------------------------------------


def fresh(tmp_path: Path, **update: Any) -> Settings:
    settings = Settings(_env_file=None, jira_projects="DEMO", api_service_key=KEY, db_path=tmp_path / "oversight.db",
                        config_dir=CONFIG_FIXTURES, reports_dir=tmp_path / "reports", **update)
    Store.open(settings.db_file).close()
    return settings


def asked(settings: Settings, *times: str) -> None:
    with Store.open(settings.db_file) as store:
        for n, moment in enumerate(times):
            store.upsert("qa_log", {"qa_id": f"q{n}", "asked_at": moment, "outcome": "answered"})


def test_usage_today_and_the_last_seven_days(tmp_path: Path) -> None:
    settings = fresh(tmp_path)
    asked(settings, f"{TODAY}T00:00:00.000Z", f"{TODAY}T14:00:00.000Z", "2026-11-03T23:59:59.999Z",
          "2026-10-29T10:00:00.000Z", "2026-10-28T23:59:59.999Z")  # the last is eight days ago
    spend(settings, (f"{TODAY}T09:00:00.000Z", "anthropic", 100, 50), (f"{TODAY}T10:00:00.000Z", "bedrock", 30, 20),
          (f"{TODAY}T11:00:00.000Z", "scripted", 999, 999), ("2026-11-01T12:00:00.000Z", "anthropic", 7, 3))
    response = client_of(settings).get("/api/v1/usage", params={"level": "people"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"meta", "today", "last_7_days", "ai_model"}
    assert body["today"] == {"questions": 2, "model_calls": 2, "input_tokens": 130, "output_tokens": 70,
                             "budget_tokens": None, "budget_used_pct": None}
    # Whether a model is set up at all, so a page of zero tokens can say why: stub here, so off.
    assert body["ai_model"] == "off"
    live = settings.model_copy(update={"llm_mode": "live"})
    conn = open_readonly(settings.db_file)
    try:
        assert usage_report(conn, live, NOW)["ai_model"] == "on"
    finally:
        conn.close()
    assert body["last_7_days"] == [
        {"date": "2026-10-29", "questions": 1, "model_calls": 0, "tokens": 0},
        {"date": "2026-10-30", "questions": 0, "model_calls": 0, "tokens": 0},
        {"date": "2026-10-31", "questions": 0, "model_calls": 0, "tokens": 0},
        {"date": "2026-11-01", "questions": 0, "model_calls": 1, "tokens": 10},
        {"date": "2026-11-02", "questions": 0, "model_calls": 0, "tokens": 0},
        {"date": "2026-11-03", "questions": 1, "model_calls": 0, "tokens": 0},
        {"date": TODAY, "questions": 2, "model_calls": 2, "tokens": 200},
    ]
    assert body["meta"]["projects"] == ["DEMO"]


def test_usage_against_a_budget(tmp_path: Path) -> None:
    settings = fresh(tmp_path, llm_daily_token_budget=400)
    spend(settings, (f"{TODAY}T09:00:00.000Z", "anthropic", 100, 33))
    today = client_of(settings).get("/api/v1/usage", params={"level": "people"}).json()["today"]
    assert (today["budget_tokens"], today["budget_used_pct"]) == (400, 33.2)
    spend(settings, (f"{TODAY}T10:00:00.000Z", "anthropic", 300, 100))  # the last call took the day past it
    conn = open_readonly(settings.db_file)
    try:
        assert usage_report(conn, settings, NOW)["today"]["budget_used_pct"] == 133.2
    finally:
        conn.close()


@pytest.mark.parametrize("params", [{"level": "people", "days": "30"}, [("level", "people"), ("level", "people")],
                                    {"level": "everyone"}])
def test_usage_takes_only_project_and_level(tmp_path: Path, params: Any) -> None:
    assert client_of(fresh(tmp_path)).get("/api/v1/usage", params=params).status_code == 422


def test_usage_needs_no_viewer_and_records_nothing(tmp_path: Path) -> None:
    settings = fresh(tmp_path)
    unnamed = client_of(settings, viewer=None, name=None)
    assert unnamed.get("/api/v1/usage", params={"level": "people"}).status_code == 200
    assert count(settings, "access_log") == 0


def test_the_viewer_hash_is_what_the_rate_limit_counts_and_a_restart_forgets(demo: Settings) -> None:
    """Two names on one hash are one viewer: the name is for people to read, the hash is who it is. The counts are
    kept in the process's memory, so a new process (a restart) starts with none."""
    limited = demo.model_copy(update={"ask_rate_limit": "1/1m"})
    api = client_of(limited, viewer=ADMIN, name="Morgan Reyes")
    assert api.post("/api/v1/ask", json={"question": "   "}).status_code == 200
    renamed = api.post("/api/v1/ask", json={"question": "   "}, headers={"X-Oversight-Viewer-Name": "Morgan%20R."})
    assert renamed.status_code == 429 and renamed.headers["retry-after"] == "60"
    assert client_of(limited, viewer=ADMIN).post("/api/v1/ask", json={"question": "   "}).status_code == 200
