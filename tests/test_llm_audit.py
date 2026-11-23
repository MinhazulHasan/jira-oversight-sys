"""The llm_calls audit trail: hashes, counts and outcomes, never text."""
from __future__ import annotations

import hashlib
from datetime import datetime

import pytest

from oversight.llm.audit import OUTCOMES, prompt_hash, record_llm_call
from oversight.llm.provider import Usage
from oversight.store.db import Store
from oversight.timeutil import UTC

NOW = datetime(2026, 11, 4, 9, 30, 15, 250_000, tzinfo=UTC)


def test_prompt_hash_is_sha256_over_system_separator_user() -> None:
    expected = hashlib.sha256("sys\x1fuser".encode("utf-8")).hexdigest()
    assert prompt_hash("sys", "user") == expected
    assert len(expected) == 64


def test_prompt_hash_separates_the_two_parts() -> None:
    assert prompt_hash("ab", "c") != prompt_hash("a", "bc")
    assert prompt_hash("s", "u") == prompt_hash("s", "u")
    assert prompt_hash("s", "u") != prompt_hash("s", "u ")
    assert prompt_hash("", "") == hashlib.sha256(b"\x1f").hexdigest()


def test_record_llm_call_inserts_one_row_and_returns_its_id(store: Store) -> None:
    call_id = record_llm_call(
        store,
        purpose="plan",
        provider="anthropic",
        model="claude-sonnet-5-5",
        prompt_hash=prompt_hash("s", "u"),
        usage=Usage(input_tokens=321, output_tokens=55),
        latency_ms=812,
        stop_reason="end_turn",
        outcome="ok",
        qa_id="qa123",
        now=lambda: NOW,
    )
    assert len(call_id) == 32 and int(call_id, 16) >= 0
    (row,) = store.query("SELECT * FROM llm_calls")
    assert row["call_id"] == call_id
    assert row["called_at"] == "2026-11-04T09:30:15.250Z"
    assert (row["purpose"], row["provider"], row["model"]) == ("plan", "anthropic", "claude-sonnet-5-5")
    assert row["prompt_hash"] == prompt_hash("s", "u")
    assert (row["input_tokens"], row["output_tokens"], row["latency_ms"]) == (321, 55, 812)
    assert (row["stop_reason"], row["outcome"], row["error"], row["qa_id"]) == ("end_turn", "ok", None, "qa123")


def test_a_failed_call_is_recorded_without_usage(store: Store) -> None:
    record_llm_call(
        store,
        purpose="compose",
        provider="anthropic",
        model=None,
        prompt_hash="abc",
        usage=None,
        latency_ms=None,
        stop_reason=None,
        outcome="error",
        error="the Anthropic API is rate limiting requests (HTTP 429)",
        now=lambda: NOW,
    )
    (row,) = store.query("SELECT * FROM llm_calls")
    assert row["model"] is None and row["input_tokens"] is None and row["output_tokens"] is None
    assert row["latency_ms"] is None and row["stop_reason"] is None and row["qa_id"] is None
    assert row["outcome"] == "error"
    assert row["error"] == "the Anthropic API is rate limiting requests (HTTP 429)"


def test_a_refusal_is_its_own_outcome(store: Store) -> None:
    record_llm_call(
        store,
        purpose="insights",
        provider="anthropic",
        model="claude-opus-5-5",
        prompt_hash="abc",
        usage=Usage(input_tokens=10, output_tokens=0),
        latency_ms=5,
        stop_reason="refusal",
        outcome="refused",
        error="the model declined the request (cyber)",
        now=lambda: NOW,
    )
    (row,) = store.query("SELECT outcome, stop_reason, error FROM llm_calls")
    assert tuple(row) == ("refused", "refusal", "the model declined the request (cyber)")


def test_long_error_text_is_cut_short(store: Store) -> None:
    record_llm_call(
        store, purpose="plan", provider="scripted", model="scripted", prompt_hash="h", usage=None,
        latency_ms=1, stop_reason=None, outcome="error", error="x" * 2000, now=lambda: NOW,
    )
    (row,) = store.query("SELECT error FROM llm_calls")
    assert len(row["error"]) == 500


def test_an_unknown_outcome_is_refused_and_nothing_is_written(store: Store) -> None:
    assert OUTCOMES == ("ok", "refused", "error")
    with pytest.raises(ValueError, match="outcome"):
        record_llm_call(
            store, purpose="plan", provider="scripted", model=None, prompt_hash="h", usage=None,
            latency_ms=None, stop_reason=None, outcome="maybe", now=lambda: NOW,
        )
    assert store.count("llm_calls") == 0


def test_every_call_gets_its_own_row(store: Store) -> None:
    ids = {
        record_llm_call(
            store, purpose="compose", provider="scripted", model="scripted", prompt_hash="same", usage=None,
            latency_ms=1, stop_reason="end_turn", outcome="ok", qa_id="qa1", now=lambda: NOW,
        )
        for _ in range(3)
    }
    assert len(ids) == 3
    assert store.count("llm_calls", {"qa_id": "qa1"}) == 3


def test_the_default_clock_writes_a_storage_format_timestamp(store: Store) -> None:
    record_llm_call(
        store, purpose="plan", provider="scripted", model=None, prompt_hash="h", usage=None,
        latency_ms=None, stop_reason=None, outcome="ok",
    )
    (row,) = store.query("SELECT called_at FROM llm_calls")
    assert len(row["called_at"]) == 24 and row["called_at"].endswith("Z") and row["called_at"][10] == "T"
