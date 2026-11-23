"""BedrockProvider: Claude on Amazon Bedrock through the official `anthropic` SDK.

Same requests as AnthropicProvider (structured outputs through
`messages.parse(output_format=...)`, plain text through `messages.create`),
sent to Bedrock's Messages API endpoint with the SDK's Mantle client:

    anthropic.AnthropicBedrockMantle(
        aws_access_key=..., aws_secret_key=..., aws_session_token=...,
        aws_region=..., aws_profile=..., timeout=..., max_retries=...)

Requests are signed with SigV4 (botocore, from `anthropic[bedrock]`). With no
keys and no profile in the settings, the standard AWS credential chain is used
(environment, shared config, SSO, instance or task role), which is what a
production host should rely on instead of long-lived keys.

Differences from the Claude API, checked against the installed SDK (2.4.0):
- Model ids carry an `anthropic.` prefix on Bedrock. Settings keep the
  first-party ids (claude-sonnet-5-5) and _model() adds the prefix; an id that
  already has one, or an ARN, is sent as given.
- Server-side refusal fallbacks are not available on Bedrock, so compose()
  never sends the fallback beta; a refusal raises LLMRefusal as usual.
- The SDK sends API version 2026-10-21, which Bedrock accepts for Claude 4.7
  and later only. Older Bedrock models (Opus 4.1, Sonnet 4) are retired and
  are not supported here.
"""
from __future__ import annotations

from typing import Any

import anthropic

from oversight.llm.anthropic_adapter import AnthropicProvider
from oversight.settings import Settings

BEDROCK_PREFIX = "anthropic."


def bedrock_model_id(model: str) -> str:
    """claude-opus-5-5 -> anthropic.claude-opus-5-5; prefixed ids and ARNs pass through."""
    return f"{BEDROCK_PREFIX}{model}" if model.startswith("claude-") else model


def bedrock_client_options(settings: Settings) -> dict[str, Any]:
    """Constructor arguments for the Mantle client; blank settings are left to the AWS chain."""

    def secret(value: Any) -> str | None:
        return value.get_secret_value().strip() or None

    return {
        "aws_region": settings.aws_region.strip() or None,
        "aws_access_key": secret(settings.aws_access_key_id),
        "aws_secret_key": secret(settings.aws_secret_access_key),
        "aws_session_token": secret(settings.aws_session_token),
        "aws_profile": settings.aws_profile.strip() or None,
        "timeout": settings.llm_timeout_s,
        "max_retries": settings.llm_max_retries,
    }


class BedrockProvider(AnthropicProvider):
    name = "bedrock"
    service = "Amazon Bedrock"
    credentials_hint = (
        "check AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (or AWS_PROFILE), that the IAM identity may "
        "call bedrock-mantle, and that the model is enabled for this account and region"
    )

    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        if client is None:
            client = anthropic.AnthropicBedrockMantle(**bedrock_client_options(settings))
        super().__init__(settings, client=client)
        # Not offered on Bedrock: sending the beta would fail every text call.
        self._fallbacks = False

    def _model(self, model: str) -> str:
        return bedrock_model_id(model)
