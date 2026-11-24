"""Claude on Amazon Bedrock through the anthropic SDK's Mantle client."""
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
