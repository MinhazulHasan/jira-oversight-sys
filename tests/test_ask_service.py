"""ask() end to end with a scripted provider: each of the five outcomes, the
audit rows, the degradations and the response shape (Planning.md Phase 6).
"""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from oversight.ask import service
from oversight.ask.service import MAX_QUESTION_CHARS, NOT_VERIFIED_MESSAGE, SUGGESTED_QUESTIONS, AskResponse, ask
from oversight.ask.prompts import COMPOSE_SYSTEM, CREATIVE_DECLINE, PLAN_SYSTEM
from oversight.ask.router import creative
from oversight.config import ProcessConfig
from oversight.llm.provider import LLMError, LLMRefusal, Usage
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store.db import Store

from tests.fakes.fake_llm import FakeProvider, audit_shim, cut_off, plan  # noqa: F401  (audit_shim is a fixture)
from tests.test_catalog import AS_OF, build_snapshot, fixed_now, rules  # noqa: F401  (rules is a fixture)

STUCK = "Where do tickets get stuck most?"
# An answer whose every number is in oversight_time_in_status(period=all) on the snapshot.
STUCK_ANSWER = "Tickets sit longest in In Progress: a median of 24.0 h over 15 intervals; To Do is 1.0 h at the median."
TIME_IN_STATUS = ("oversight_time_in_status", {"period": "all"})


@pytest.fixture
def snapshot(store: Store, process: ProcessConfig) -> tuple[Store, ProcessConfig]:
    return store, build_snapshot(store, process)


def _ask(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, provider: FakeProvider, question: str = STUCK, **kw: Any) -> AskResponse:
    store, process = snapshot
    return ask(question, store=store, process=process, rules=rules, settings=settings, provider=provider, now=fixed_now, **kw)


def _llm_calls(store: Store, qa_id: str) -> list[tuple[Any, ...]]:
    rows = store.query("SELECT purpose, outcome, provider, model, error FROM llm_calls WHERE qa_id = ? ORDER BY called_at, rowid", (qa_id,))
    return [tuple(row) for row in rows]


def _qa_row(store: Store, qa_id: str) -> dict[str, Any]:
    row = store.query_one("SELECT * FROM qa_log WHERE qa_id = ?", (qa_id,))
    assert row is not None
    return dict(row)


# --- the five outcomes ---------------------------------------------------------------------


@pytest.mark.usefixtures("audit_shim")
def test_answered_with_citations_and_audit_rows(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[STUCK_ANSWER])

    response = _ask(snapshot, rules, settings, provider, user="james")

    assert response.outcome == "answered" and response.answer == STUCK_ANSWER and response.message is None
    assert [(b.marker, b.text, b.fact_ref, b.row_index, b.column) for b in response.citations] == [
        (1, "24.0", "F1", 1, "median_h"),
        (2, "15", "F1", 1, "n"),
        (3, "1.0", "F1", 0, "median_h"),
    ]
    assert [c.name for c in response.plan] == ["oversight_time_in_status"]
    assert [f.ref for f in response.facts] == ["F1"] and response.facts[0].as_of == AS_OF
    assert response.project == "AIL" and response.asked_at == "2026-11-04T12:00:00.000Z" and response.model == "fake-model"
    assert response.missing_evidence == [] and response.suggestions == []
    # The provider saw the two documented calls, with the settings' model and efforts.
    assert [(c.kind, c.model, c.effort) for c in provider.calls] == [("structured", settings.llm_model_qa, "low"), ("compose", settings.llm_model_qa, "medium")]
    assert provider.compose_calls[0].max_tokens == 600 and provider.compose_calls[0].system == COMPOSE_SYSTEM
    assert provider.structured_calls[0].system == PLAN_SYSTEM
    # Audit: one llm_calls row per call, and the qa_log row.
    assert _llm_calls(store, response.qa_id) == [("plan", "ok", "fake", "fake-model", None), ("compose", "ok", "fake", "fake-model", None)]
    row = _qa_row(store, response.qa_id)
    assert (row["outcome"], row["asked_by"], row["project_key"], row["question"], row["answer"]) == ("answered", "james", "AIL", STUCK, STUCK_ANSWER)
    assert json.loads(row["plan_json"]) == {"understood": True, "reason": None, "calls": [{"name": "oversight_time_in_status", "params": {"period": "all"}}]}
    assert [f["ref"] for f in json.loads(row["facts_json"])] == ["F1"]
    assert [c["marker"] for c in json.loads(row["citations_json"])] == [1, 2, 3]
    assert row["missing_json"] is None and row["model"] == "fake-model" and len(row["prompt_hash"]) == 64
    assert isinstance(row["latency_ms"], int) and row["latency_ms"] == response.latency_ms >= 0


@pytest.mark.usefixtures("audit_shim")
def test_not_understood_from_the_plan(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(understood=False, reason="Answers stay at team and process level.")])
    response = _ask(snapshot, rules, settings, provider, "Who is the slowest developer?")
    assert response.outcome == "not_understood" and response.answer is None
    assert response.message == "Answers stay at team and process level."
    assert response.suggestions == SUGGESTED_QUESTIONS and response.facts == [] and response.plan == []
    assert provider.compose_calls == []
    assert _llm_calls(store, response.qa_id) == [("plan", "ok", "fake", "fake-model", None)]
    assert _qa_row(store, response.qa_id)["outcome"] == "not_understood"


@pytest.mark.usefixtures("audit_shim")
def test_not_understood_when_the_plan_has_no_calls(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    response = _ask(snapshot, rules, settings, FakeProvider(plans=[plan(understood=True)]))
    assert response.outcome == "not_understood" and response.message and response.suggestions


@pytest.mark.usefixtures("audit_shim")
def test_not_understood_from_bad_params(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(("oversight_top_issues", {"metric": "cycle_time", "n": 99}))] * 2)
    response = _ask(snapshot, rules, settings, provider, "top 99 slowest tickets")
    assert response.outcome == "not_understood"
    assert "oversight_top_issues" in (response.message or "") and "n" in (response.message or "")
    assert response.facts == [] and provider.compose_calls == []
    assert _qa_row(store, response.qa_id)["outcome"] == "not_understood"
    unknown = _ask(snapshot, rules, settings, FakeProvider(plans=[plan("oversight_nope")] * 2))
    assert unknown.outcome == "not_understood" and "oversight_nope" in (unknown.message or "")
    scope = _ask(snapshot, rules, settings, FakeProvider(plans=[plan(("oversight_count_issues", {"project": "XYZ"}))]))
    assert scope.outcome == "not_understood" and "XYZ" in (scope.message or "")


@pytest.mark.usefixtures("audit_shim")
def test_no_data_keeps_the_empty_facts(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(("oversight_list_findings", {"rule_id": "R-HYG-01"}))])
    response = _ask(snapshot, rules, settings, provider, "Any unassigned work in progress?")
    assert response.outcome == "no_data" and response.answer is None
    assert "oversight_list_findings" in (response.message or "") and "R-HYG-01" in (response.message or "")
    assert [f.ref for f in response.facts] == ["F1"] and response.facts[0].empty
    assert provider.compose_calls == []
    row = _qa_row(store, response.qa_id)
    assert row["outcome"] == "no_data" and json.loads(row["facts_json"])[0]["rows"] == []


@pytest.mark.usefixtures("audit_shim")
def test_not_measurable_for_code_review_on_ail(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(("oversight_list_findings", {"rule_id": "R-FLOW-01"}))])
    response = _ask(snapshot, rules, settings, provider, "Which tickets reached testing without code review?")
    assert response.outcome == "not_measurable"
    assert response.missing_evidence == ["code_review"]
    assert "code_review" in (response.message or "") and "finding" in (response.message or "")
    assert response.facts == [] and response.answer is None  # nothing was fetched
    assert provider.compose_calls == []
    row = _qa_row(store, response.qa_id)
    assert row["outcome"] == "not_measurable" and json.loads(row["missing_json"]) == ["code_review"]
    coverage = _ask(snapshot, rules, settings, FakeProvider(plans=[plan(("oversight_coverage", {"field": "code_review"}))]))
    assert coverage.outcome == "not_measurable" and coverage.missing_evidence == ["code_review"]


@pytest.mark.usefixtures("audit_shim")
def test_not_verified_after_a_failed_strict_retry(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=["The median is 999 hours.", "Still 999 hours."])
    response = _ask(snapshot, rules, settings, provider)
    assert response.outcome == "not_verified" and response.answer is None
    assert response.message == NOT_VERIFIED_MESSAGE and response.citations == []
    assert [f.ref for f in response.facts] == ["F1"] and len(response.facts[0].rows) == 3  # the facts are kept (To Do, In Progress, In Review)
    assert response.extra["problems"] == ["unverified number 999"]
    first, strict = provider.compose_calls
    assert "failed verification" not in first.user
    assert "failed verification: unverified number 999" in strict.user and strict.system == COMPOSE_SYSTEM
    assert _llm_calls(store, response.qa_id) == [
        ("plan", "ok", "fake", "fake-model", None),
        ("compose", "ok", "fake", "fake-model", None),
        ("compose_strict", "ok", "fake", "fake-model", None),
    ]
    row = _qa_row(store, response.qa_id)
    assert row["outcome"] == "not_verified" and row["answer"] is None and json.loads(row["citations_json"]) == []


@pytest.mark.usefixtures("audit_shim")
def test_the_strict_retry_can_succeed(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=["The median is 999 hours.", STUCK_ANSWER])
    response = _ask(snapshot, rules, settings, provider)
    assert response.outcome == "answered" and response.answer == STUCK_ANSWER and len(response.citations) == 3
    assert response.extra == {"retried": True}
    assert [c[0] for c in _llm_calls(store, response.qa_id)] == ["plan", "compose", "compose_strict"]


# --- no provider call for questions that cannot be questions ----------------------------------


@pytest.mark.usefixtures("audit_shim")
@pytest.mark.parametrize("question", ["", "   \n ", "x" * (MAX_QUESTION_CHARS + 1)])
def test_blank_and_over_long_questions_never_call_the_provider(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, question: str) -> None:
    store, _ = snapshot
    provider = FakeProvider()  # nothing scripted: any call would fail
    response = _ask(snapshot, rules, settings, provider, question)
    assert response.outcome == "not_understood" and response.suggestions == SUGGESTED_QUESTIONS
    assert response.message and response.plan == [] and response.model is None
    assert provider.calls == []
    assert _llm_calls(store, response.qa_id) == []
    assert _qa_row(store, response.qa_id)["outcome"] == "not_understood"


@pytest.mark.usefixtures("audit_shim")
@pytest.mark.parametrize("question", [
    "Write a short poem about our sprint.", "Tell me a joke about our open breaches", "make it rhyme",
    "Can you sum up the sprint in verse?", "Compose a haiku on cycle time",
])
def test_a_request_for_a_poem_song_or_joke_is_declined_before_any_model(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, question: str
) -> None:
    """18 Nov 2026, live: "Write a short poem about our sprint." got a 16-line rhyming poem marked "Numbers verified".
    Ask answers questions about the work: a creative piece is declined in code, with the suggestions."""
    store, _ = snapshot
    provider = FakeProvider()  # nothing scripted: any call would fail
    response = _ask(snapshot, rules, settings, provider, question)
    assert (response.outcome, response.message) == ("not_understood", CREATIVE_DECLINE)
    assert response.suggestions == SUGGESTED_QUESTIONS and response.answer is None
    assert provider.calls == [] and _llm_calls(store, response.qa_id) == []


@pytest.mark.parametrize("question", [
    STUCK, "Which stories took longest?", "Which tickets mention songs?", "How many jokes did QA log?",
    "Who wrote the most tickets?", "Which tickets went straight from To Do to Done this month?",
])
def test_a_question_that_only_mentions_such_a_piece_is_no_request_for_one(question: str) -> None:
    assert not creative(question)


@pytest.mark.usefixtures("audit_shim")
def test_a_question_of_exactly_the_limit_is_asked(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    provider = FakeProvider(plans=[plan(understood=False, reason="no")])
    response = _ask(snapshot, rules, settings, provider, "y" * MAX_QUESTION_CHARS)
    assert len(provider.structured_calls) == 1 and response.message == "no"


# --- the model fails ------------------------------------------------------------------------


@pytest.mark.usefixtures("audit_shim")
def test_an_llm_error_at_plan_time_degrades_to_not_understood(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[LLMError("connection reset", retryable=True)])
    response = _ask(snapshot, rules, settings, provider)
    assert response.outcome == "not_understood" and response.suggestions == SUGGESTED_QUESTIONS
    assert "connection reset" not in (response.message or "")  # logged, not shown
    assert _llm_calls(store, response.qa_id) == [("plan", "error", "fake", settings.llm_model_qa, "LLMError: connection reset")]
    assert _qa_row(store, response.qa_id)["outcome"] == "not_understood"


@pytest.mark.usefixtures("audit_shim")
def test_an_llm_error_at_compose_time_degrades_to_not_verified_with_facts(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[LLMError("rate limited", retryable=True)])
    response = _ask(snapshot, rules, settings, provider)
    assert response.outcome == "not_verified" and response.answer is None
    assert len(response.facts) == 1 and not response.facts[0].empty
    assert response.message and "rate limited" not in response.message
    assert _llm_calls(store, response.qa_id)[1] == ("compose", "error", "fake", settings.llm_model_qa, "LLMError: rate limited")
    assert len(provider.compose_calls) == 1  # the SDK already retried; the service does not


@pytest.mark.usefixtures("audit_shim")
def test_a_refusal_is_a_failed_composition(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    twice = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[LLMRefusal(), LLMRefusal()])
    response = _ask(snapshot, rules, settings, twice)
    assert response.outcome == "not_verified" and len(response.facts) == 1
    assert [(c[0], c[1]) for c in _llm_calls(store, response.qa_id)] == [("plan", "ok"), ("compose", "refused"), ("compose_strict", "refused")]
    once = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[LLMRefusal(), STUCK_ANSWER])
    assert _ask(snapshot, rules, settings, once).outcome == "answered"


@pytest.mark.usefixtures("audit_shim")
def test_a_refused_call_is_recorded_with_the_tokens_it_was_billed_for(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings
) -> None:
    """A refusal comes with a response: its tokens count toward the day's budget, as an answer's do."""
    store, _ = snapshot
    billed = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[LLMRefusal(usage=Usage(2400, 30)), STUCK_ANSWER])
    response = _ask(snapshot, rules, settings, billed)
    rows = store.query("SELECT purpose, outcome, stop_reason, input_tokens, output_tokens FROM llm_calls"
                       " WHERE qa_id = ? ORDER BY called_at, rowid", (response.qa_id,))
    assert [tuple(row) for row in rows][1] == ("compose", "refused", "refusal", 2400, 30)


@pytest.mark.usefixtures("audit_shim")
def test_a_cut_off_answer_is_a_failed_composition(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    provider = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[cut_off(STUCK_ANSWER), STUCK_ANSWER])
    response = _ask(snapshot, rules, settings, provider)
    assert response.outcome == "answered" and response.extra == {"retried": True}
    assert "cut off" in provider.compose_calls[1].user
    only_cut = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[cut_off(STUCK_ANSWER), cut_off(STUCK_ANSWER)])
    assert _ask(snapshot, rules, settings, only_cut).outcome == "not_verified"


# --- settings, stores and the response shape ------------------------------------------------


@pytest.mark.usefixtures("audit_shim")
def test_only_ask_max_calls_calls_run_and_row_limit_caps_rows(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    capped = settings.model_copy(update={"ask_max_calls": 1, "ask_row_limit": 2})
    provider = FakeProvider(plans=[plan(TIME_IN_STATUS, ("oversight_hot_spots", {}))], answers=[STUCK_ANSWER])
    response = _ask(snapshot, rules, capped, provider)
    assert [c.name for c in response.plan] == ["oversight_time_in_status"]
    assert [f.ref for f in response.facts] == ["F1"]
    assert len(response.facts[0].rows) == 2 and response.facts[0].truncated is True
    assert response.outcome == "answered"  # 24.0 and 15 are in the two rows kept


@pytest.mark.usefixtures("audit_shim")
def test_several_calls_become_f1_f2_in_plan_order(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    provider = FakeProvider(
        plans=[plan(("oversight_hot_spots", {"n": 2}), TIME_IN_STATUS)],
        answers=["Epic AIL-100 (Billing) has 2 open breaches; In Progress has a median of 24.0 h."],
    )
    response = _ask(snapshot, rules, settings, provider)
    assert response.outcome == "answered"
    assert [(f.ref, f.tool) for f in response.facts] == [("F1", "oversight_hot_spots"), ("F2", "oversight_time_in_status")]
    assert [(b.text, b.fact_ref, b.row_index, b.column) for b in response.citations] == [
        ("AIL-100", "F1", 0, "epic_key"),
        ("2", "F1", 0, "open_breaches"),
        ("24.0", "F2", 1, "median_h"),
    ]
    assert "<facts>" in provider.compose_calls[0].user and '"ref": "F2"' in provider.compose_calls[0].user


@pytest.mark.usefixtures("audit_shim")
def test_a_file_store_is_read_through_a_read_only_connection(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = snapshot
    opened: list[Path] = []
    real = service.open_readonly

    def spy(path: Path) -> Any:
        opened.append(Path(path))
        return real(path)

    monkeypatch.setattr(service, "open_readonly", spy)
    response = _ask(snapshot, rules, settings, FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[STUCK_ANSWER]))
    # Twice, read-only both times: the board's names for the check of the question's own words, then the facts.
    assert response.outcome == "answered" and opened == [store.path, store.path]


@pytest.mark.usefixtures("audit_shim")
def test_an_in_memory_store_uses_its_own_connection(process: ProcessConfig, rules: RulesConfig, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "open_readonly", lambda path: pytest.fail("open_readonly called for an in-memory store"))
    with Store.open(":memory:") as memory:
        full = build_snapshot(memory, process)
        response = ask(STUCK, store=memory, process=full, rules=rules, settings=settings, provider=FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[STUCK_ANSWER]), now=fixed_now)
        assert response.outcome == "answered"
        assert memory.count("qa_log") == 1 and memory.count("llm_calls") == 2


@pytest.mark.usefixtures("audit_shim")
def test_project_defaults_to_the_first_configured_project(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    both = settings.model_copy(update={"jira_projects": ["DEMO", "AIL"]})
    provider = FakeProvider(plans=[plan(("oversight_count_issues", {}))], answers=["There are 0 issues."])
    response = _ask(snapshot, rules, both, provider)
    assert response.project == "DEMO" and response.facts[0].rows[0]["count"] == 0
    explicit = _ask(snapshot, rules, both, FakeProvider(plans=[plan(("oversight_count_issues", {}))], answers=["19 issues."]), project="AIL")
    assert explicit.project == "AIL" and explicit.outcome == "answered"


@pytest.mark.usefixtures("audit_shim")
def test_to_dict_has_every_field_and_no_person_data(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    provider = FakeProvider(
        plans=[plan(("oversight_top_issues", {"metric": "cycle_time", "n": 3}), ("oversight_list_findings", {}))],
        answers=["AIL-104 has the longest cycle time at 55.0 h; AIL-111 breached R-EFF-01."],
    )
    response = _ask(snapshot, rules, settings, provider)
    assert response.outcome == "answered"
    payload = response.to_dict()
    assert set(payload) == {f.name for f in fields(AskResponse)}
    assert payload["citations"][0] == {"marker": 1, "text": "AIL-104", "fact_ref": "F1", "row_index": 0, "column": "issue_key"}
    assert payload["plan"] == [{"name": "oversight_top_issues", "params": {"metric": "cycle_time", "n": 3}}, {"name": "oversight_list_findings", "params": {}}]
    assert payload["facts"][0]["columns"][0] == {"key": "issue_key", "label": "Issue", "kind": "issue_key"}
    text = json.dumps(payload)
    for forbidden in ("assignee", "author", "acc-dev", "acc-"):
        assert forbidden not in text
    json.loads(text)  # round-trips as JSON


@pytest.mark.usefixtures("audit_shim")
def test_render_shows_the_answer_then_the_tables(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    provider = FakeProvider(plans=[plan(TIME_IN_STATUS, ("oversight_list_findings", {"rule_id": "R-HYG-01"}))], answers=[STUCK_ANSWER])
    text = _ask(snapshot, rules, settings, provider).render()
    assert text.startswith(STUCK_ANSWER)
    assert "F1 oversight_time_in_status(group_by=status, period=all)" in text and f"as of {AS_OF}" in text
    assert "In Progress" in text and "Median (h)" in text and "24.0" in text
    assert "F2 oversight_list_findings(rule_id=R-HYG-01" in text and "(no rows)" in text
    assert "acc-" not in text


@pytest.mark.usefixtures("audit_shim")
def test_render_of_the_other_outcomes(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    measurable = _ask(snapshot, rules, settings, FakeProvider(plans=[plan(("oversight_list_findings", {"rule_id": "R-FLOW-01"}))])).render()
    assert "Missing evidence: code_review" in measurable
    understood = _ask(snapshot, rules, settings, FakeProvider(), "").render()
    assert "Try one of these:" in understood and SUGGESTED_QUESTIONS[0] in understood
    unverified = _ask(snapshot, rules, settings, FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=["999", "999"])).render()
    assert unverified.startswith(NOT_VERIFIED_MESSAGE) and "In Progress" in unverified


@pytest.mark.usefixtures("audit_shim")
def test_latency_is_measured_for_every_outcome(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    for provider, question in (
        (FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[STUCK_ANSWER]), STUCK),
        (FakeProvider(), ""),
        (FakeProvider(plans=[plan(("oversight_list_findings", {"rule_id": "R-FLOW-01"}))]), STUCK),
    ):
        response = _ask(snapshot, rules, settings, provider, question)
        assert isinstance(response.latency_ms, int) and response.latency_ms >= 0
        assert response.qa_id and len(response.qa_id) == 32


def test_the_audit_shim_stands_in_while_the_real_audit_is_a_stub(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback path of tests/fakes/fake_llm.py: when oversight.llm.audit raises
    NotImplementedError the shim records the same llm_calls rows, so these tests
    stay green whichever lands first."""
    from oversight.llm import audit as llm_audit

    from tests.fakes import fake_llm

    def not_yet(*_args: Any, **_kwargs: Any) -> str:
        raise NotImplementedError

    monkeypatch.setattr(llm_audit, "prompt_hash", not_yet)
    monkeypatch.setattr(llm_audit, "record_llm_call", not_yet)
    assert fake_llm.install_fake_audit(monkeypatch) is True
    assert llm_audit.prompt_hash is fake_llm.fake_prompt_hash and llm_audit.record_llm_call is fake_llm.fake_record_llm_call
    store, _ = snapshot
    response = _ask(snapshot, rules, settings, FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=[STUCK_ANSWER]))
    assert response.outcome == "answered"
    assert _llm_calls(store, response.qa_id) == [("plan", "ok", "fake", "fake-model", None), ("compose", "ok", "fake", "fake-model", None)]
    assert _qa_row(store, response.qa_id)["prompt_hash"] == fake_llm.fake_prompt_hash(COMPOSE_SYSTEM, provider_prompt(response, store))


def provider_prompt(response: AskResponse, store: Store) -> str:
    """The compose prompt ask() built for this response, rebuilt the same way, so the recorded hash can be checked."""
    from oversight.ask.prompts import compose_user

    return compose_user(response.question, [facts.to_dict() for facts in response.facts])


@pytest.mark.usefixtures("audit_shim")
def test_the_question_is_stripped_and_the_plan_prompt_carries_it(snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(understood=False)])
    response = _ask(snapshot, rules, settings, provider, "  Where do tickets get stuck most?  ")
    assert response.question == STUCK and _qa_row(store, response.qa_id)["question"] == STUCK
    prompt = provider.structured_calls[0].user
    assert prompt.endswith(f"<question>{STUCK}</question>") and prompt.startswith("<catalog>")
    assert "oversight_rule_status" in prompt and "input_schema" in prompt
    assert provider.structured_calls[0].schema.__name__ == "Plan"


# --- a plan with a bad parameter gets one more try ------------------------------------------


@pytest.mark.usefixtures("audit_shim")
def test_a_plan_missing_a_required_parameter_is_retried_once_with_the_problem(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings
) -> None:
    store, _ = snapshot
    provider = FakeProvider(plans=[plan(("oversight_coverage", {})), plan(TIME_IN_STATUS)], answers=[STUCK_ANSWER])

    response = _ask(snapshot, rules, settings, provider)

    assert response.outcome == "answered" and [c.name for c in response.plan] == ["oversight_time_in_status"]
    first, second = provider.structured_calls
    assert "<problem>" not in first.user
    assert "<problem>The previous plan could not be used: oversight_coverage" in second.user
    assert [row[0] for row in _llm_calls(store, response.qa_id)] == ["plan", "plan_retry", "compose"]


@pytest.mark.usefixtures("audit_shim")
def test_a_plan_that_is_still_invalid_after_the_retry_is_not_understood(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings
) -> None:
    provider = FakeProvider(plans=[plan(("oversight_coverage", {})), plan(("oversight_coverage", {}))])

    response = _ask(snapshot, rules, settings, provider)

    assert response.outcome == "not_understood"
    assert response.message is not None and response.message.startswith("The plan could not be used: oversight_coverage")
    assert len(provider.structured_calls) == 2 and provider.compose_calls == []


# --- the answer is plain prose ---------------------------------------------------------------


def test_plain_prose_takes_out_markdown_and_key_brackets() -> None:
    text = "## Summary\n**Effort is flat.** See [AIL-575] and __[AIL-62]__.\n\n- R-FLOW-01 needs review\n2. W-REV-01 too"
    assert service.plain_prose(text) == (
        "Summary\nEffort is flat. See AIL-575 and AIL-62.\n\nR-FLOW-01 needs review\nW-REV-01 too"
    )
    # Numbers, ranges and ordinary brackets are left alone.
    assert service.plain_prose("12 of 65 (49.2%) [see facts] 3-4 h") == "12 of 65 (49.2%) [see facts] 3-4 h"


@pytest.mark.usefixtures("audit_shim")
def test_a_markdown_answer_is_shown_and_cited_as_plain_prose(
    snapshot: tuple[Store, ProcessConfig], rules: RulesConfig, settings: Settings
) -> None:
    provider = FakeProvider(plans=[plan(TIME_IN_STATUS)], answers=["**Tickets sit longest in In Progress**: a median of 24.0 h over 15 intervals."])

    response = _ask(snapshot, rules, settings, provider)

    assert response.outcome == "answered"
    assert response.answer == "Tickets sit longest in In Progress: a median of 24.0 h over 15 intervals."
    assert all(binding.text in response.answer for binding in response.citations)
