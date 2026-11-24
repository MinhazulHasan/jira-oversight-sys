"""AnthropicProvider: Claude through the official anthropic SDK."""
from __future__ import annotations

import time
from typing import Any, TypeVar

import anthropic
import pydantic
from pydantic import BaseModel

from oversight.llm.provider import ConverseResult, LLMError, LLMRefusal, StructuredResult, TextResult, Usage
from oversight.logs import get_logger
from oversight.settings import Settings

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

log = get_logger("llm")
# A module attribute, so tests can replace the clock without touching `time`.
_perf_counter = time.perf_counter


class AnthropicProvider:
    name = "anthropic"
    # How errors name the service and what to check; BedrockProvider overrides both.
    service = "the Anthropic API"
    credentials_hint = "check ANTHROPIC_API_KEY"

    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        if client is None:
            client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                timeout=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,
            )
        self._client = client
        self._fallbacks = settings.llm_fallbacks

    def _model(self, model: str) -> str:
        """The model id as this platform names it (the Claude API uses the configured id as is)."""
        return model

    def _error(self, exc: anthropic.AnthropicError, model: str) -> LLMError:
        return _mapped(exc, model, service=self.service, credentials_hint=self.credentials_hint)

    def structured(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        model: str,
        effort: str = "low",
        max_tokens: int = 1024,
    ) -> StructuredResult:
        started = _perf_counter()
        try:
            response = self._client.messages.parse(
                model=self._model(model),
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                output_config={"effort": effort},
            )
        except pydantic.ValidationError as exc:
            # The SDK already validated the reply against the schema, so a mismatch is final, not transient.
            error = LLMError(
                f"the model's structured output did not match {schema.__name__}: {_validation_summary(exc)}",
                retryable=False,
            )
            self._log_failure("structured", model, exc, error)
            raise error from exc
        except anthropic.AnthropicError as exc:
            error = self._error(exc, model)
            self._log_failure("structured", model, exc, error)
            raise error from exc
        latency_ms = _elapsed_ms(started)
        self._log_call("structured", model, response, latency_ms)
        if response.stop_reason == "refusal":
            raise LLMRefusal(_refusal_message(response), usage=_usage(response))
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(
                f"the model returned no structured output (stop_reason {response.stop_reason})", retryable=False,
                usage=_usage(response), stop_reason=str(response.stop_reason),
            )
        if not isinstance(parsed, schema):
            parsed = _coerce(parsed, schema, response)
        return StructuredResult(
            parsed=parsed,
            model=str(response.model),
            stop_reason=str(response.stop_reason),
            usage=_usage(response),
            latency_ms=latency_ms,
        )

    def compose(
        self,
        system: str,
        user: str,
        *,
        model: str,
        effort: str = "medium",
        max_tokens: int = 1024,
    ) -> TextResult:
        request: dict[str, Any] = {
            "model": self._model(model),
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": effort},
        }
        started = _perf_counter()
        try:
            if self._fallbacks:
                response = self._client.beta.messages.create(**request, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                response = self._client.messages.create(**request)
        except anthropic.AnthropicError as exc:
            error = self._error(exc, model)
            self._log_failure("compose", model, exc, error)
            raise error from exc
        latency_ms = _elapsed_ms(started)
        self._log_call("compose", model, response, latency_ms)
        if response.stop_reason == "refusal":
            raise LLMRefusal(_refusal_message(response), usage=_usage(response))
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return TextResult(
            text=text,
            model=str(response.model),
            stop_reason=str(response.stop_reason or "end_turn"),
            usage=_usage(response),
            latency_ms=latency_ms,
        )

    def converse(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str,
        effort: str = "medium",
        max_tokens: int = 16000,
        tool_choice: dict[str, Any] | None = None,
    ) -> ConverseResult:
        """One turn with client tools; forced tool choice is never sent because current models reject it."""
        request: dict[str, Any] = {
            "model": self._model(model),
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": effort},
        }
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        started = _perf_counter()
        try:
            response = self._client.messages.create(**request)
        except anthropic.AnthropicError as exc:
            error = self._error(exc, model)
            self._log_failure("converse", model, exc, error)
            raise error from exc
        latency_ms = _elapsed_ms(started)
        self._log_call("converse", model, response, latency_ms)
        if response.stop_reason == "refusal":
            raise LLMRefusal(_refusal_message(response), usage=_usage(response))
        content = list(response.content)
        return ConverseResult(
            content=content,
            text="".join(block.text for block in content if getattr(block, "type", None) == "text"),
            tool_uses=[block for block in content if getattr(block, "type", None) == "tool_use"],
            model=str(response.model),
            stop_reason=str(response.stop_reason or "end_turn"),
            usage=_usage(response),
            latency_ms=latency_ms,
        )

    # --- logging: counts and names only, never prompt text or the key ---------------

    def _log_call(self, call: str, requested_model: str, response: Any, latency_ms: int) -> None:
        usage = _usage(response)
        log.info(
            "llm_call",
            provider=self.name,
            call=call,
            requested_model=requested_model,
            model=str(response.model),
            latency_ms=latency_ms,
            stop_reason=response.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    def _log_failure(self, call: str, model: str, exc: Exception, error: LLMError) -> None:
        log.warning(
            "llm_call_failed",
            provider=self.name,
            call=call,
            model=model,
            error=type(exc).__name__,
            status=getattr(exc, "status_code", None),
            request_id=getattr(exc, "request_id", None),
            retryable=error.retryable,
        )


def _mapped(
    exc: anthropic.AnthropicError,
    model: str,
    *,
    service: str = "the Anthropic API",
    credentials_hint: str = "check ANTHROPIC_API_KEY",
) -> LLMError:
    """The SDK's exception as an LLMError; its message is dropped because it can echo the request."""
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMError(
            f"{service} rejected the credentials (HTTP {exc.status_code}): {credentials_hint}",
            retryable=False,
        )
    if isinstance(exc, anthropic.NotFoundError):
        return LLMError(f"unknown model {model} (HTTP 404)", retryable=False)
    if isinstance(exc, anthropic.RateLimitError):
        return LLMError(f"{service} is rate limiting requests (HTTP 429)", retryable=True)
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        kind = f", {exc.type}" if getattr(exc, "type", None) else ""
        if status >= 500:
            return LLMError(f"{service} is unavailable (HTTP {status}{kind})", retryable=True)
        return LLMError(f"{service} rejected the request (HTTP {status}{kind})", retryable=False)
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError(f"could not reach {service} (connection error or timeout)", retryable=True)
    return LLMError(f"the Anthropic SDK failed ({type(exc).__name__})", retryable=False)


def _validation_summary(exc: pydantic.ValidationError) -> str:
    """The shape of a validation failure without the offending value."""
    errors = exc.errors(include_url=False, include_input=False)
    if not errors:
        return "invalid"
    first = errors[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "output"
    return f"{exc.error_count()} error(s), first at {where}: {first.get('msg', 'invalid')}"


def _coerce(parsed: Any, schema: type[T], response: Any) -> T:
    """A fake or an older SDK may hand back a dict instead of the model."""
    try:
        return schema.model_validate(parsed)
    except pydantic.ValidationError as exc:
        raise LLMError(
            f"the model's structured output did not match {schema.__name__}: {_validation_summary(exc)}",
            retryable=False, usage=_usage(response), stop_reason=str(response.stop_reason),
        ) from exc


def _refusal_message(response: Any) -> str:
    details = getattr(response, "stop_details", None)
    category = getattr(details, "category", None)
    if category:
        return f"the model declined the request ({category})"
    return "the model declined the request"


def _usage(response: Any) -> Usage:
    usage = response.usage
    return Usage(input_tokens=int(usage.input_tokens), output_tokens=int(usage.output_tokens))


def _elapsed_ms(started: float) -> int:
    return max(0, round((_perf_counter() - started) * 1000))
