"""The provider interface: result types, errors and provider selection."""
from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from oversight.errors import ConfigError, OversightError
from oversight.llm.anthropic_adapter import AnthropicProvider
from oversight.llm.provider import (
    LLMError,
    LLMProvider,
    LLMRefusal,
    Plan,
    PlanCall,
    StructuredResult,
    TextResult,
    Usage,
    get_provider,
)
from oversight.llm.scripted import ScriptedProvider
from oversight.settings import Settings


def test_stub_mode_gives_the_scripted_provider() -> None:
    provider = get_provider(Settings(llm_mode="stub", anthropic_api_key=""))
    assert isinstance(provider, ScriptedProvider)
    assert provider.name == "scripted"


def test_stub_mode_ignores_a_key_that_happens_to_be_set() -> None:
    assert isinstance(get_provider(Settings(llm_mode="stub", anthropic_api_key="sk-ant-x")), ScriptedProvider)


def test_live_mode_gives_the_anthropic_provider() -> None:
    provider = get_provider(Settings(llm_mode="live", anthropic_api_key="sk-ant-offline-test"))
    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "anthropic"


def test_live_mode_without_a_key_fails_before_any_call() -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        get_provider(Settings(llm_mode="live", anthropic_api_key=""))


@pytest.mark.parametrize("provider", [ScriptedProvider(), AnthropicProvider(Settings(anthropic_api_key="sk-ant-x"))])
def test_both_providers_have_the_interface(provider: LLMProvider) -> None:
    assert isinstance(provider.name, str) and provider.name
    assert callable(provider.structured) and callable(provider.compose)


def test_llm_error_carries_retryable() -> None:
    assert LLMError("x").retryable is False
    assert LLMError("x", retryable=True).retryable is True
    assert isinstance(LLMError("x"), OversightError)


def test_refusal_is_a_final_llm_error() -> None:
    refusal = LLMRefusal()
    assert isinstance(refusal, LLMError) and refusal.retryable is False
    assert str(refusal) == "the model declined the request"
    assert str(LLMRefusal("declined (cyber)")) == "declined (cyber)"


def test_plan_allows_at_most_three_calls_and_no_extra_fields() -> None:
    calls = [PlanCall(name=f"oversight_{i}") for i in range(3)]
    assert len(Plan(understood=True, calls=calls).calls) == 3
    with pytest.raises(ValidationError):
        Plan(understood=True, calls=calls + [PlanCall(name="oversight_4")])
    with pytest.raises(ValidationError):
        Plan(understood=True, sql="DROP TABLE issues")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PlanCall(name="oversight_x", where="1=1")  # type: ignore[call-arg]
    assert PlanCall(name="oversight_x").params == {}
    assert Plan(understood=False).reason is None


def test_results_are_immutable_records() -> None:
    usage = Usage(input_tokens=1, output_tokens=2)
    text = TextResult(text="t", model="m", stop_reason="end_turn", usage=usage, latency_ms=3)
    structured = StructuredResult(parsed=Plan(understood=True), model="m", stop_reason="end_turn", usage=usage, latency_ms=3)
    for record in (usage, text, structured):
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.latency_ms = 9  # type: ignore[misc]
