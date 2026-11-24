from __future__ import annotations

import inspect
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import anthropic
import anthropic.types
import pydantic
import pytest
from structlog.testing import capture_logs

import oversight.llm.anthropic_adapter as adapter
from oversight.llm.anthropic_adapter import FALLBACK_BETA, AnthropicProvider
from oversight.llm.provider import LLMError, LLMRefusal, Plan, PlanCall, StructuredResult, TextResult, Usage
from oversight.logs import configure_logging
from oversight.settings import Settings

KEY = "sk-ant-api03-SECRET-KEY-VALUE"
SYSTEM = "You answer from facts only."
USER = "<facts>[]</facts>\n<question>SECRET PROMPT TEXT</question>"
MODEL = "claude-sonnet-5-5"

FORBIDDEN = ("temperature", "top_p", "top_k", "thinking", "tool_choice", "tools")


class _Messages:
    """messages.parse / messages.create with the SDK's keyword-only shape."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append({"method": "parse", **kwargs})
        return self._deliver()

    def create(self, **kwargs: Any) -> Any:
        self.calls.append({"method": "create", **kwargs})
        return self._deliver()

    def _deliver(self) -> Any:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeClient:
    def __init__(self, outcome: Any = None, *, beta_outcome: Any = None) -> None:
        self.messages = _Messages(outcome)
        self.beta = SimpleNamespace(messages=_Messages(outcome if beta_outcome is None else beta_outcome))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.messages.calls + self.beta.messages.calls


def text_response(
    *blocks: Any,
    stop_reason: str = "end_turn",
    model: str = MODEL,
    input_tokens: int = 120,
    output_tokens: int = 48,
    stop_details: Any = None,
) -> SimpleNamespace:
    content = list(blocks) or [SimpleNamespace(type="text", text="Tickets wait longest in To Do.")]
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_details=stop_details,
    )


def parsed_response(parsed: Any, **kwargs: Any) -> SimpleNamespace:
    response = text_response(SimpleNamespace(type="text", text="{}"), **kwargs)
    response.parsed_output = parsed
    return response


def sdk_status_error(cls: type[anthropic.APIStatusError], status: int, message: str = "boom") -> anthropic.APIStatusError:
    """An SDK error without an HTTP library: the constructor only reads these attributes."""
    response = SimpleNamespace(
        status_code=status,
        headers={"request-id": "req_test_123"},
        request=SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/messages"),
    )
    return cls(message, response=response, body={"error": {"type": "api_error", "message": message}})


def settings_with(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"llm_mode": "live", "anthropic_api_key": KEY, "llm_fallbacks": True}
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A perf counter that advances 250 ms per call, so latency is exact."""
    ticks = (index * 0.25 for index in range(100))
    monkeypatch.setattr(adapter, "_perf_counter", lambda: next(ticks))
    yield


def test_installed_sdk_exposes_the_documented_call_shapes() -> None:
    client = anthropic.Anthropic(api_key="sk-ant-offline")
    parse_params = inspect.signature(client.messages.parse).parameters
    assert {"output_format", "output_config", "system", "messages", "model", "max_tokens"} <= set(parse_params)
    beta_params = inspect.signature(client.beta.messages.create).parameters
    assert {"fallbacks", "betas", "output_config"} <= set(beta_params)
    assert anthropic.__version__.startswith("2.")
    for name in ("AuthenticationError", "PermissionDeniedError", "NotFoundError", "RateLimitError", "APIStatusError", "APIConnectionError"):
        assert hasattr(anthropic, name)


def test_the_real_client_is_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, Any] = {}

    def recorder(**kwargs: Any) -> str:
        built.update(kwargs)
        return "client"

    monkeypatch.setattr(anthropic, "Anthropic", recorder)
    provider = AnthropicProvider(settings_with(llm_timeout_s=42.5, llm_max_retries=5))
    assert built == {"api_key": KEY, "timeout": 42.5, "max_retries": 5}
    assert provider._client == "client"


def test_an_injected_client_is_used_as_is() -> None:
    client = FakeClient(text_response())
    provider = AnthropicProvider(settings_with(), client=client)
    assert provider._client is client
    assert provider.name == "anthropic"


def test_structured_sends_the_documented_request_and_nothing_forbidden() -> None:
    client = FakeClient(parsed_response(Plan(understood=True)))
    provider = AnthropicProvider(settings_with(), client=client)
    provider.structured(SYSTEM, USER, Plan, model=MODEL, effort="low", max_tokens=800)

    (call,) = client.calls
    assert call["method"] == "parse"
    assert call["model"] == MODEL and call["max_tokens"] == 800 and call["system"] == SYSTEM
    assert call["messages"] == [{"role": "user", "content": USER}]  # one user turn, no prefill
    assert call["output_format"] is Plan
    assert call["output_config"] == {"effort": "low"}
    assert not any(name in call for name in FORBIDDEN)
    assert not client.beta.messages.calls


def test_structured_returns_the_parsed_instance_with_usage_and_latency(clock: None) -> None:
    plan = Plan(understood=True, calls=[PlanCall(name="oversight_hot_spots", params={"n": 3})])
    client = FakeClient(parsed_response(plan, input_tokens=321, output_tokens=55, model="claude-sonnet-5-5"))
    result = AnthropicProvider(settings_with(), client=client).structured(SYSTEM, USER, Plan, model=MODEL)

    assert isinstance(result, StructuredResult)
    assert result.parsed is plan
    assert result.model == "claude-sonnet-5-5" and result.stop_reason == "end_turn"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (321, 55)
    assert result.latency_ms == 250


def test_structured_coerces_a_plain_dict_into_the_schema() -> None:
    client = FakeClient(parsed_response({"understood": False, "reason": "not about Jira"}))
    result = AnthropicProvider(settings_with(), client=client).structured(SYSTEM, USER, Plan, model=MODEL)
    assert isinstance(result.parsed, Plan) and result.parsed.reason == "not about Jira"


def test_structured_refusal_raises_llm_refusal_with_the_category() -> None:
    details = SimpleNamespace(type="refusal", category="cyber", explanation="declined")
    client = FakeClient(parsed_response(None, stop_reason="refusal", stop_details=details))
    with pytest.raises(LLMRefusal, match="declined the request \\(cyber\\)") as err:
        AnthropicProvider(settings_with(), client=client).structured(SYSTEM, USER, Plan, model=MODEL)
    assert err.value.retryable is False


def test_structured_without_parsed_output_is_a_final_error() -> None:
    client = FakeClient(parsed_response(None, stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="no structured output .*max_tokens") as err:
        AnthropicProvider(settings_with(), client=client).structured(SYSTEM, USER, Plan, model=MODEL)
    assert err.value.retryable is False


def test_structured_validation_failure_is_final_and_hides_the_value() -> None:
    try:
        Plan.model_validate_json('{"understood": "nope-SECRET-VALUE"}')
    except pydantic.ValidationError as exc:
        validation_error = exc
    client = FakeClient(validation_error)
    with pytest.raises(LLMError, match="did not match Plan") as err:
        AnthropicProvider(settings_with(), client=client).structured(SYSTEM, USER, Plan, model=MODEL)
    assert err.value.retryable is False
    assert "understood" in str(err.value)
    assert "SECRET" not in str(err.value)


def test_structured_coercion_failure_is_final() -> None:
    client = FakeClient(parsed_response({"understood": "not a bool"}))
    with pytest.raises(LLMError, match="did not match Plan") as err:
        AnthropicProvider(settings_with(), client=client).structured(SYSTEM, USER, Plan, model=MODEL)
    assert err.value.retryable is False


@pytest.mark.parametrize(("response", "stop_reason"), [
    (parsed_response(None, stop_reason="refusal", stop_details=None, input_tokens=4800, output_tokens=35), "refusal"),
    (parsed_response(None, stop_reason="max_tokens", input_tokens=5100, output_tokens=4096), "max_tokens"),
    (parsed_response({"understood": "not a bool"}, input_tokens=900, output_tokens=60), "end_turn"),
], ids=["refusal", "cut-off", "not-the-schema"])
def test_a_failed_structured_call_carries_the_tokens_its_response_reported(response: Any, stop_reason: str) -> None:
    with pytest.raises(LLMError) as err:
        AnthropicProvider(settings_with(), client=FakeClient(response)).structured(SYSTEM, USER, Plan, model=MODEL)
    assert err.value.usage == Usage(response.usage.input_tokens, response.usage.output_tokens)
    assert err.value.stop_reason == stop_reason


def test_a_failed_call_with_no_response_carries_no_tokens() -> None:
    client = FakeClient(anthropic.APIConnectionError(request=SimpleNamespace(method="POST", url="https://x")))
    with pytest.raises(LLMError) as err:
        AnthropicProvider(settings_with(), client=client).structured(SYSTEM, USER, Plan, model=MODEL)
    assert (err.value.usage, err.value.stop_reason) == (None, None)


def test_a_refused_composition_or_turn_carries_its_tokens() -> None:
    response = text_response(stop_reason="refusal", stop_details=None, input_tokens=700, output_tokens=12)
    provider = AnthropicProvider(settings_with(llm_fallbacks=False), client=FakeClient(response))
    with pytest.raises(LLMRefusal) as composed:
        provider.compose(SYSTEM, USER, model=MODEL)
    with pytest.raises(LLMRefusal) as turned:
        provider.converse(SYSTEM, [{"role": "user", "content": USER}], [], model=MODEL)
    assert composed.value.usage == turned.value.usage == Usage(700, 12)


def test_compose_uses_the_beta_endpoint_with_default_fallbacks() -> None:
    client = FakeClient(text_response())
    provider = AnthropicProvider(settings_with(llm_fallbacks=True), client=client)
    provider.compose(SYSTEM, USER, model=MODEL, effort="medium", max_tokens=600)

    assert not client.messages.calls
    (call,) = client.beta.messages.calls
    assert call["method"] == "create"
    assert call["betas"] == [FALLBACK_BETA] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["model"] == MODEL and call["max_tokens"] == 600 and call["system"] == SYSTEM
    assert call["messages"] == [{"role": "user", "content": USER}]
    assert call["output_config"] == {"effort": "medium"}
    assert not any(name in call for name in FORBIDDEN)


def test_compose_uses_the_plain_endpoint_without_fallbacks() -> None:
    client = FakeClient(text_response())
    provider = AnthropicProvider(settings_with(llm_fallbacks=False), client=client)
    provider.compose(SYSTEM, USER, model=MODEL)

    assert not client.beta.messages.calls
    (call,) = client.messages.calls
    assert call["method"] == "create"
    assert "betas" not in call and "fallbacks" not in call
    assert call["output_config"] == {"effort": "medium"}
    assert not any(name in call for name in FORBIDDEN)


def test_compose_joins_text_blocks_and_skips_thinking(clock: None) -> None:
    response = text_response(
        SimpleNamespace(type="thinking", thinking="private"),
        SimpleNamespace(type="text", text="Tickets wait longest "),
        SimpleNamespace(type="text", text="in To Do."),
        input_tokens=77,
        output_tokens=9,
    )
    result = AnthropicProvider(settings_with(), client=FakeClient(response)).compose(SYSTEM, USER, model=MODEL)

    assert isinstance(result, TextResult)
    assert result.text == "Tickets wait longest in To Do."
    assert "private" not in result.text
    assert result.stop_reason == "end_turn" and result.model == MODEL
    assert (result.usage.input_tokens, result.usage.output_tokens) == (77, 9)
    assert result.latency_ms == 250


def test_compose_reads_a_real_sdk_message_object() -> None:
    message = anthropic.types.Message(
        id="msg_01",
        content=[anthropic.types.TextBlock(type="text", text="Four breaches are open.")],
        model="claude-opus-4-8",  # the fallback model answered
        role="assistant",
        stop_reason="end_turn",
        stop_sequence=None,
        type="message",
        usage=anthropic.types.Usage(input_tokens=11, output_tokens=7),
    )
    result = AnthropicProvider(settings_with(), client=FakeClient(message)).compose(SYSTEM, USER, model=MODEL)
    assert result.text == "Four breaches are open."
    assert result.model == "claude-opus-4-8"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 7)


def test_compose_refusal_raises_llm_refusal() -> None:
    response = text_response(stop_reason="refusal", stop_details=SimpleNamespace(category=None))
    with pytest.raises(LLMRefusal, match="declined the request$"):
        AnthropicProvider(settings_with(), client=FakeClient(response)).compose(SYSTEM, USER, model=MODEL)


def test_compose_passes_max_tokens_through_with_the_partial_text() -> None:
    response = text_response(SimpleNamespace(type="text", text="Tickets wait longest in"), stop_reason="max_tokens")
    result = AnthropicProvider(settings_with(), client=FakeClient(response)).compose(SYSTEM, USER, model=MODEL)
    assert result.stop_reason == "max_tokens"
    assert result.text == "Tickets wait longest in"


@pytest.mark.parametrize(
    ("error", "retryable", "fragment"),
    [
        (sdk_status_error(anthropic.AuthenticationError, 401), False, "ANTHROPIC_API_KEY"),
        (sdk_status_error(anthropic.PermissionDeniedError, 403), False, "ANTHROPIC_API_KEY"),
        (sdk_status_error(anthropic.NotFoundError, 404), False, f"unknown model {MODEL}"),
        (sdk_status_error(anthropic.RateLimitError, 429), True, "429"),
        (sdk_status_error(anthropic.BadRequestError, 400), False, "400"),
        (sdk_status_error(anthropic.UnprocessableEntityError, 422), False, "422"),
        (sdk_status_error(anthropic.InternalServerError, 500), True, "500"),
        (sdk_status_error(anthropic.ServiceUnavailableError, 503), True, "503"),
        (sdk_status_error(anthropic.OverloadedError, 529), True, "529"),
        (anthropic.APIConnectionError(request=SimpleNamespace()), True, "reach"),
        (anthropic.APITimeoutError(request=SimpleNamespace()), True, "timeout"),
        (anthropic.AnthropicError("something else"), False, "AnthropicError"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else str(value),
)
@pytest.mark.parametrize("call", ["structured", "compose"])
def test_sdk_errors_map_to_llm_errors(error: BaseException, retryable: bool, fragment: str, call: str) -> None:
    provider = AnthropicProvider(settings_with(), client=FakeClient(error))
    with pytest.raises(LLMError) as err:
        if call == "structured":
            provider.structured(SYSTEM, USER, Plan, model=MODEL)
        else:
            provider.compose(SYSTEM, USER, model=MODEL)
    assert not isinstance(err.value, LLMRefusal)
    assert err.value.retryable is retryable
    assert fragment in str(err.value)
    assert err.value.__cause__ is error


def test_exception_text_never_includes_the_key_or_the_prompt() -> None:
    leaky = sdk_status_error(anthropic.AuthenticationError, 401, message=f"invalid x-api-key {KEY} for prompt {USER}")
    provider = AnthropicProvider(settings_with(), client=FakeClient(leaky))
    for call in ("structured", "compose"):
        with pytest.raises(LLMError) as err:
            if call == "structured":
                provider.structured(SYSTEM, USER, Plan, model=MODEL)
            else:
                provider.compose(SYSTEM, USER, model=MODEL)
        text = str(err.value)
        assert KEY not in text and "SECRET" not in text and "sk-ant" not in text
        assert "SECRET PROMPT TEXT" not in text and USER not in text


def _flat_values(event: dict[str, Any]) -> str:
    return " ".join(str(value) for value in event.values())


def test_each_call_logs_one_line_without_prompt_text_or_the_key() -> None:
    configure_logging("INFO", "json")
    response = text_response(stop_reason="end_turn", model="claude-opus-4-8", input_tokens=200, output_tokens=40)
    provider = AnthropicProvider(settings_with(), client=FakeClient(response))
    with capture_logs() as captured:
        provider.compose(SYSTEM, USER, model=MODEL)
    (event,) = [e for e in captured if e["event"] == "llm_call"]
    assert event["provider"] == "anthropic" and event["call"] == "compose"
    assert event["requested_model"] == MODEL and event["model"] == "claude-opus-4-8"
    assert event["stop_reason"] == "end_turn"
    assert (event["input_tokens"], event["output_tokens"]) == (200, 40)
    assert isinstance(event["latency_ms"], int)
    assert KEY not in _flat_values(event) and "SECRET PROMPT" not in _flat_values(event)


def test_a_failed_call_logs_the_error_type_not_its_message() -> None:
    configure_logging("INFO", "json")
    error = sdk_status_error(anthropic.RateLimitError, 429, message=f"slow down {KEY} SECRET PROMPT TEXT")
    provider = AnthropicProvider(settings_with(), client=FakeClient(error))
    with capture_logs() as captured, pytest.raises(LLMError):
        provider.structured(SYSTEM, USER, Plan, model=MODEL)
    (event,) = [e for e in captured if e["event"] == "llm_call_failed"]
    assert event["error"] == "RateLimitError" and event["status"] == 429 and event["retryable"] is True
    assert event["request_id"] == "req_test_123" and event["call"] == "structured"
    assert KEY not in _flat_values(event) and "SECRET" not in _flat_values(event)
