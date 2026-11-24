from __future__ import annotations

import pydantic
import pytest
from anthropic.lib._parse._transform import transform_schema

from oversight.llm.provider import Plan, PlanCall


def test_the_wire_schema_takes_params_as_a_string() -> None:
    params = transform_schema(Plan)["$defs"]["PlanCall"]["properties"]["params"]
    assert params["type"] == "string" and "required" in params["description"]


def test_a_json_string_from_the_model_becomes_the_params_dict() -> None:
    plan = Plan.model_validate_json(
        '{"understood": true, "calls": [{"name": "oversight_trend", "params": "{\\"metric\\": \\"reopen_rate\\", \\"last_n\\": 4}"}]}'
    )
    assert plan.calls[0].params == {"metric": "reopen_rate", "last_n": 4}


def test_code_still_builds_calls_with_a_dict_and_dumps_a_dict() -> None:
    call = PlanCall(name="oversight_hot_spots", params={"n": 5})
    assert call.model_dump(mode="json") == {"name": "oversight_hot_spots", "params": {"n": 5}}
    assert PlanCall(name="oversight_hot_spots", params="").params == {}


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", '"text"'])
def test_params_that_are_not_a_json_object_are_a_validation_error(bad: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        PlanCall(name="oversight_trend", params=bad)
