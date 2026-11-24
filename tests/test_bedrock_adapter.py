"""BedrockProvider"""
from __future__ import annotations

from typing import Any

import anthropic
import pytest

from oversight.llm.bedrock_adapter import BedrockProvider, bedrock_client_options, bedrock_model_id
from oversight.llm.provider import LLMError, Plan, PlanCall, get_provider
from oversight.settings import ConfigError, Settings
from tests.test_anthropic_adapter import FakeClient, parsed_response, sdk_status_error, text_response

ACCESS_KEY = "AKIAEXAMPLEOFFLINE01"
SECRET_KEY = "offline-secret-value-never-logged"
MODEL = "claude-sonnet-5-5"


def bedrock_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_mode": "live",
        "llm_provider": "bedrock",
        "aws_region": "us-east-1",
        "aws_access_key_id": ACCESS_KEY,
        "aws_secret_access_key": SECRET_KEY,
        "llm_fallbacks": True,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("configured", "sent"),
    [
        ("claude-sonnet-5-5", "anthropic.claude-sonnet-5-5"),
        ("claude-opus-5-5", "anthropic.claude-opus-5-5"),
        ("anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5"),
        ("arn:aws:bedrock:us-east-1:123456789012:inference-profile/x", "arn:aws:bedrock:us-east-1:123456789012:inference-profile/x"),
    ],
)
def test_first_party_ids_get_the_bedrock_prefix_and_others_pass_through(configured: str, sent: str) -> None:
    assert bedrock_model_id(configured) == sent


def test_live_bedrock_needs_a_region() -> None:
    with pytest.raises(ConfigError, match="AWS_REGION"):
        bedrock_settings(aws_region="").require_llm()


def test_live_bedrock_needs_both_keys_or_neither() -> None:
    with pytest.raises(ConfigError, match="both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"):
        bedrock_settings(aws_secret_access_key="").require_llm()
    bedrock_settings(aws_access_key_id="", aws_secret_access_key="").require_llm()  # the AWS chain


def test_live_bedrock_does_not_need_an_anthropic_key() -> None:
    bedrock_settings(anthropic_api_key="").require_llm()


def test_the_anthropic_provider_still_needs_its_key() -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        Settings(llm_mode="live", llm_provider="anthropic", anthropic_api_key="").require_llm()


def test_aws_secrets_never_appear_in_the_settings_repr() -> None:
    shown = repr(bedrock_settings()) + str(bedrock_settings().model_dump())
    assert SECRET_KEY not in shown and ACCESS_KEY not in shown


def test_client_options_come_from_settings_and_blanks_are_left_to_the_aws_chain() -> None:
    options = bedrock_client_options(bedrock_settings(llm_timeout_s=42.5, llm_max_retries=5))
    assert options == {
        "aws_region": "us-east-1",
        "aws_access_key": ACCESS_KEY,
        "aws_secret_key": SECRET_KEY,
        "aws_session_token": None,
        "aws_profile": None,
        "timeout": 42.5,
        "max_retries": 5,
    }
    chain = bedrock_client_options(bedrock_settings(aws_access_key_id="", aws_secret_access_key=""))
    assert chain["aws_access_key"] is None and chain["aws_secret_key"] is None


def test_the_real_mantle_client_targets_the_regional_bedrock_endpoint() -> None:
    provider = BedrockProvider(bedrock_settings(llm_timeout_s=30.0, llm_max_retries=3))
    client = provider._client
    assert isinstance(client, anthropic.AnthropicBedrockMantle)
    assert str(client.base_url).startswith("https://bedrock-mantle.us-east-1.api.aws/anthropic")
    assert client.max_retries == 3 and client.timeout == 30.0


def test_get_provider_picks_bedrock_from_settings() -> None:
    assert isinstance(get_provider(bedrock_settings()), BedrockProvider)


def test_structured_sends_the_bedrock_model_id() -> None:
    plan = Plan(understood=True, calls=[PlanCall(name="oversight_hot_spots", params={})])
    client = FakeClient(parsed_response(plan, model="anthropic.claude-sonnet-5-5"))
    result = BedrockProvider(bedrock_settings(), client=client).structured("system", "user", Plan, model=MODEL)
    assert client.calls[0]["method"] == "parse"
    assert client.calls[0]["model"] == "anthropic.claude-sonnet-5-5"
    assert result.parsed == plan


def test_compose_never_sends_the_fallback_beta_on_bedrock() -> None:
    client = FakeClient(text_response())
    result = BedrockProvider(bedrock_settings(llm_fallbacks=True), client=client).compose("system", "user", model=MODEL)
    assert [call["method"] for call in client.calls] == ["create"]
    assert client.beta.messages.calls == []
    assert "betas" not in client.calls[0] and "fallbacks" not in client.calls[0]
    assert client.calls[0]["model"] == "anthropic.claude-sonnet-5-5"
    assert result.text


def test_errors_name_bedrock_and_what_to_check() -> None:
    client = FakeClient(sdk_status_error(anthropic.PermissionDeniedError, 403))
    with pytest.raises(LLMError) as raised:
        BedrockProvider(bedrock_settings(), client=client).compose("system", "user", model=MODEL)
    message = str(raised.value)
    assert message.startswith("Amazon Bedrock rejected the credentials (HTTP 403)")
    assert "AWS_ACCESS_KEY_ID" in message and "enabled" in message
    assert raised.value.retryable is False
    assert SECRET_KEY not in message


def test_throttling_is_retryable() -> None:
    client = FakeClient(sdk_status_error(anthropic.RateLimitError, 429))
    with pytest.raises(LLMError, match="Amazon Bedrock is rate limiting") as raised:
        BedrockProvider(bedrock_settings(), client=client).structured("system", "user", Plan, model=MODEL)
    assert raised.value.retryable is True
