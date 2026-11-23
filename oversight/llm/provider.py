"""The LLMProvider interface and the models every provider returns.

CONTRACT (stub; the LLM build implements it). Two providers exist:
- ScriptedProvider (llm/scripted.py): offline, deterministic, no key. The
  default (LLM_MODE=stub), used by demos, tests and CI.
- AnthropicProvider (llm/anthropic_adapter.py): Claude through the Anthropic
  SDK (LLM_MODE=live).

Both expose the same two calls:
- structured(system, user, schema, *, model, effort, max_tokens) -> StructuredResult:
  the model returns an instance of `schema` (a pydantic model). Used for the
  plan (Plan) and for the weekly narrative (insights.Narrative).
- compose(system, user, *, model, effort, max_tokens) -> TextResult: free text.
Providers never see the database: callers pass fully rendered prompts, and
callers decide what to do with the result. Every call is audited through
llm.audit.record_llm_call by the caller (the provider returns usage and timing).

Errors: LLMError(retryable) for transport and API failures (the SDK already
retried); LLMRefusal when the model declined (stop_reason "refusal"); a
StructuredResult whose parsed output failed validation raises LLMError
(retryable=False) with the validation message. A failure that came with a
response (a refusal, a reply cut off at max_tokens or with no structured
output) carries the tokens it reported (`usage`) and its stop_reason: those
tokens are billed, so the caller records them and the daily budget counts
them (llm/budget.py). One with no response (a transport error) has none.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema

from oversight.errors import OversightError
from oversight.settings import Settings

T = TypeVar("T", bound=BaseModel)


def _params_from_json(value: Any) -> Any:
    """The model writes params as a JSON object inside a string; code passes a dict as is."""
    if not isinstance(value, str):
        return value
    parsed = json.loads(value.strip() or "{}")  # a JSONDecodeError is a ValueError: a validation error
    if not isinstance(parsed, dict):
        raise ValueError("params must be a JSON object")
    return parsed


# Structured outputs close every object schema (additionalProperties: false), so a
# free-form dict there could only ever be {}: the model could not pass a single
# parameter. In the schema the model sees, params is therefore a string holding a
# JSON object; the validator turns it back into the dict the rest of the code uses.
CallParams = Annotated[
    dict[str, Any],
    BeforeValidator(_params_from_json),
    WithJsonSchema(
        {
            "type": "string",
            "description": (
                'The call\'s parameters as a JSON object, for example "{\\"metric\\": \\"cycle_time\\", \\"n\\": 5}". '
                "Include every parameter the tool's input_schema lists as required; \"{}\" when it needs none."
            ),
        }
    ),
]


class PlanCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    params: CallParams = Field(default_factory=dict)


class Plan(BaseModel):
    """What the plan call returns: the catalogue calls that answer the question."""

    model_config = ConfigDict(extra="forbid")

    understood: bool
    calls: list[PlanCall] = Field(default_factory=list, max_length=3)
    # When not understood: one sentence the UI can show, no instructions followed from the question.
    reason: str | None = None


@dataclass(frozen=True)
class ConverseResult:
    """One turn of a tool-using conversation (the Ask analyst).

    `content` is the SDK's content blocks, to be appended to the conversation
    unchanged (thinking blocks included); `text` is its text blocks joined;
    `tool_uses` are its tool_use blocks (each with .id, .name and .input).
    """

    content: list[Any]
    text: str
    tool_uses: list[Any]
    model: str
    stop_reason: str
    usage: Usage
    latency_ms: int


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class TextResult:
    text: str
    model: str
    stop_reason: str
    usage: Usage
    latency_ms: int


@dataclass(frozen=True)
class StructuredResult:
    parsed: BaseModel
    model: str
    stop_reason: str
    usage: Usage
    latency_ms: int


class LLMError(OversightError):
    """A call that failed. usage and stop_reason: what its response reported, when one came back (see the module
    docstring); None when none did."""

    def __init__(
        self, message: str, *, retryable: bool = False, usage: Usage | None = None, stop_reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.usage = usage
        self.stop_reason = stop_reason


class LLMRefusal(LLMError):
    """The model declined (stop_reason "refusal"); callers degrade gracefully."""

    def __init__(self, message: str = "the model declined the request", *, usage: Usage | None = None) -> None:
        super().__init__(message, retryable=False, usage=usage, stop_reason="refusal")


class LLMProvider(Protocol):
    name: str

    def structured(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        model: str,
        effort: str = "low",
        max_tokens: int = 1024,
    ) -> StructuredResult: ...

    def compose(
        self,
        system: str,
        user: str,
        *,
        model: str,
        effort: str = "medium",
        max_tokens: int = 1024,
    ) -> TextResult: ...


def get_provider(settings: Settings) -> LLMProvider:
    """ScriptedProvider for LLM_MODE=stub; for live, the LLM_PROVIDER's adapter (settings.require_llm() first)."""
    # Imported here: both providers import this module for the result types.
    if settings.llm_mode == "stub":
        from oversight.llm.scripted import ScriptedProvider

        return ScriptedProvider()
    settings.require_llm()
    if settings.llm_provider == "bedrock":
        from oversight.llm.bedrock_adapter import BedrockProvider

        return BedrockProvider(settings)
    from oversight.llm.anthropic_adapter import AnthropicProvider

    return AnthropicProvider(settings)
