"""The live adapter (LLM_PROVIDER) against the real service: opt-in, and only with credentials.

Runs only with `pytest -m live`, and only when the environment holds
ANTHROPIC_API_KEY, or LLM_PROVIDER=bedrock with AWS_REGION and AWS credentials
(tests never read .env). One structured call and one compose
call, both tiny, so a run costs a fraction of a cent.
"""
from __future__ import annotations

import os

import pytest

from oversight.ask.prompts import COMPOSE_SYSTEM, PLAN_SYSTEM, compose_user, plan_user
from oversight.llm.provider import Plan, get_provider
from oversight.settings import Settings

MINI_CATALOG = [
    {
        "name": "oversight_time_in_status",
        "description": "Median, p75 and p90 hours per status, issue type or epic over a period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "group_by": {"type": "string", "enum": ["status", "issue_type", "epic"]},
                "period": {"type": "string", "enum": ["7d", "30d", "90d", "sprint", "all"]},
            },
        },
    },
    {
        "name": "oversight_hot_spots",
        "description": "Epics with the most open breaches and warnings.",
        "input_schema": {"type": "object", "properties": {"project": {"type": "string"}, "n": {"type": "integer"}}},
    },
]

FACTS = [
    {
        "ref": "F1",
        "tool": "oversight_time_in_status",
        "params": {"project": "AIL", "group_by": "status", "period": "90d"},
        "columns": [
            {"key": "group", "label": "Status", "kind": "text"},
            {"key": "n", "label": "Intervals", "kind": "count"},
            {"key": "median_h", "label": "Median h", "kind": "hours"},
            {"key": "p90_h", "label": "p90 h", "kind": "hours"},
        ],
        "rows": [
            {"group": "In Progress", "n": 14, "median_h": 37.5, "p90_h": 120.0},
            {"group": "To Do", "n": 20, "median_h": 5.25, "p90_h": 21.0},
        ],
        "truncated": False,
        "as_of": "2026-11-04T09:00:00.000Z",
        "note": None,
    }
]


def _configured() -> bool:
    if os.environ.get("LLM_PROVIDER", "").strip().lower() == "bedrock":
        return bool(os.environ.get("AWS_REGION", "").strip())
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


@pytest.mark.live
@pytest.mark.skipif(not _configured(), reason="needs ANTHROPIC_API_KEY, or LLM_PROVIDER=bedrock and AWS_REGION, in the environment")
def test_structured_and_compose_against_the_real_api() -> None:
    settings = Settings(llm_mode="live", llm_timeout_s=120.0)
    provider = get_provider(settings)

    plan = provider.structured(
        PLAN_SYSTEM,
        plan_user("Where do tickets get stuck most?", MINI_CATALOG),
        Plan,
        model=settings.llm_model_qa,
        effort="low",
        max_tokens=800,
    )
    assert isinstance(plan.parsed, Plan)
    assert plan.usage.input_tokens > 0 and plan.usage.output_tokens > 0
    assert plan.latency_ms > 0
    if plan.parsed.understood:
        assert plan.parsed.calls, "an understood plan names at least one call"
        assert all(call.name in {entry["name"] for entry in MINI_CATALOG} for call in plan.parsed.calls)

    answer = provider.compose(
        COMPOSE_SYSTEM,
        compose_user("Where do tickets get stuck most?", FACTS),
        model=settings.llm_model_qa,
        effort="low",
        max_tokens=300,
    )
    assert answer.text.strip()
    assert answer.stop_reason in {"end_turn", "max_tokens"}
    assert answer.usage.output_tokens > 0 and answer.latency_ms > 0
    print(plan.parsed.model_dump_json(), answer.model, answer.text)
