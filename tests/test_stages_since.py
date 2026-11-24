from __future__ import annotations

from typing import Any

import pytest

from oversight.config import ProcessConfig
from oversight.derive.metrics import skipped_stages

STAGES = ["backlog", "estimate", "development", "code_review", "dev_signoff", "testing", "test_review",
          "test_signoff", "done"]


def process(**project: Any) -> ProcessConfig:
    base: dict[str, Any] = {
        "status_to_stage": {"To Do": "backlog", "In Progress": "development", "In Testing": "testing", "Done": "done"},
        "estimate": {"basis": "points", "field": "story_points", "hours_per_point": 8},
        "stages_since": {"testing": "2026-11-04"},
    }
    base.update(project)
    return ProcessConfig.model_validate({"process_version": "0.1.0", "stages": STAGES, "projects": {"P": base}})


def change(changelog_id: int, at: str, source: str, target: str) -> dict[str, Any]:
    return {"changelog_id": changelog_id, "changed_at": at, "from_status": source, "to_status": target}


def test_ordered_stages_leave_out_a_stage_before_its_day() -> None:
    config = process()
    assert config.ordered_stages("P") == ["backlog", "development", "testing", "done"]
    assert config.ordered_stages("P", at="2026-11-03T23:59:59.000Z") == ["backlog", "development", "done"]
    assert config.ordered_stages("P", at="2026-11-04T00:00:00.000Z") == ["backlog", "development", "testing", "done"]


def test_in_progress_to_done_skips_testing_only_once_testing_exists() -> None:
    changes = [
        change(1, "2026-10-20T10:00:00.000Z", "In Progress", "Done"),
        change(2, "2026-11-04T10:00:00.000Z", "In Progress", "Done"),
    ]
    skips = skipped_stages("P", changes, process())
    assert skips == [{"changelog_id": "2", "from": "In Progress", "to": "Done", "skipped": ["testing"]}]


def test_to_do_to_done_still_skips_development_before_and_after() -> None:
    changes = [
        change(1, "2026-10-20T10:00:00.000Z", "To Do", "Done"),
        change(2, "2026-11-05T10:00:00.000Z", "To Do", "Done"),
    ]
    skipped = [skip["skipped"] for skip in skipped_stages("P", changes, process())]
    assert skipped == [["development"], ["development", "testing"]]


def test_without_stages_since_every_change_is_judged_on_today_s_workflow() -> None:
    changes = [change(1, "2026-10-20T10:00:00.000Z", "In Progress", "Done")]
    assert skipped_stages("P", changes, process(stages_since={}))[0]["skipped"] == ["testing"]


def test_stages_since_must_name_a_mapped_stage() -> None:
    with pytest.raises(ValueError, match="no status maps to"):
        process(stages_since={"code_review": "2026-11-04"})
