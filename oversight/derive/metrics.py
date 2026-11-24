"""Per-issue metrics: the facts rules and AI use without touching Jira."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from oversight.config import EstimateConfig, ProcessConfig
from oversight.derive.timeline import Interval, sort_changes
from oversight.timeutil import hours_between, iso, to_iso

# process.yaml estimate field -> the `issues` column holding its current value.
_ESTIMATE_COLUMN: dict[str, str] = {
    "story_points": "story_points",
    "original_estimate": "original_estimate_s",
}


def has_estimate_value(value: object) -> bool:
    """True for a number (or numeric string) other than 0. None, "" and "0" are no estimate."""
    number = _number(value)
    return number is not None and number != 0


def skipped_stages(
    project: str,
    changes: Sequence[Mapping[str, Any]],
    process: ProcessConfig,
) -> list[dict[str, Any]]:
    """Forward jumps over mapped stages: [{"changelog_id", "from", "to", "skipped": [stage, ...]}]."""
    stage_of = process.project(project).stage_of
    skips: list[dict[str, Any]] = []
    for change in sort_changes(changes):
        # Judged against the workflow as it was on the day of the change (stages_since).
        order = process.ordered_stages(project, at=change.get("changed_at"))
        position = {stage: index for index, stage in enumerate(order)}
        a = position.get(stage_of(change.get("from_status")))
        b = position.get(stage_of(change.get("to_status")))
        if a is None or b is None or b - a <= 1:
            continue
        skips.append(
            {
                "changelog_id": str(change.get("changelog_id")),
                "from": change.get("from_status"),
                "to": change.get("to_status"),
                "skipped": order[a + 1 : b],
            }
        )
    return skips


def reopen_count(
    changes: Sequence[Mapping[str, Any]], category_of: Callable[[str | None], str | None]
) -> int:
    """Changes from a done-category status to a status that is not done."""
    return sum(
        1
        for change in changes
        if category_of(change.get("from_status")) == "done"
        and category_of(change.get("to_status")) != "done"
    )


def estimate_set_at(
    issue: Mapping[str, Any],
    field_changes: Sequence[Mapping[str, Any]],
    estimate: EstimateConfig,
) -> str | None:
    """When the estimate was first set."""
    relevant = [change for change in field_changes if change.get("field") == estimate.field]
    for change in sort_changes(relevant):
        if has_estimate_value(change.get("to_value")):
            if has_estimate_value(change.get("from_value")):
                return iso(issue.get("created_at"))
            return iso(change.get("changed_at"))
    if has_estimate_value(_current_estimate(issue, estimate)):
        return iso(issue.get("created_at"))
    return None


def effort_ratio(issue: Mapping[str, Any], estimate: EstimateConfig) -> float | None:
    """actual / estimate. actual = time_spent_s / 3600 (0 when null)."""
    actual_h = (_number(issue.get("time_spent_s")) or 0.0) / 3600
    if estimate.basis == "points":
        points = _number(issue.get("story_points"))
        if points is not None and points > 0 and estimate.hours_per_point:
            return actual_h / (points * estimate.hours_per_point)
        return None
    seconds = _number(issue.get("original_estimate_s"))
    if seconds is not None and seconds > 0:
        return actual_h / (seconds / 3600)
    return None


def compute_metrics(
    issue: Mapping[str, Any],
    intervals: Sequence[Interval],
    changes: Sequence[Mapping[str, Any]],
    field_changes: Sequence[Mapping[str, Any]],
    process: ProcessConfig,
    category_of: Callable[[str | None], str | None],
    now: datetime,
) -> dict[str, Any]:
    """One `issue_metrics` row (column names exactly as in schema.sql)."""
    computed_at = to_iso(now)  # first, so a naive `now` fails before any work
    project = issue["project_key"]
    estimate = process.estimate_for(project)
    work_stages = set(process.work_stages())

    started_at = next((i.entered_at for i in intervals if i.stage in work_stages), None)
    done_at = _done_at(intervals, process.done_stage, category_of)
    cycle_time_h = None
    if started_at is not None and done_at is not None:
        span = hours_between(started_at, done_at)
        cycle_time_h = span if span >= 0 else None
    status_age_h = max(0.0, hours_between(intervals[-1].entered_at, now)) if intervals else None

    return {
        "issue_key": issue["issue_key"],
        "started_at": started_at,
        "done_at": done_at,
        "cycle_time_h": cycle_time_h,
        "reopen_count": reopen_count(changes, category_of),
        "skipped_stages": skipped_stages(project, changes, process),
        "estimate_basis": estimate.basis if has_estimate_value(_current_estimate(issue, estimate)) else "none",
        "estimate_set_at": estimate_set_at(issue, field_changes, estimate),
        "effort_ratio": effort_ratio(issue, estimate),
        "status_age_h": status_age_h,
        "computed_at": computed_at,
    }


def _done_at(
    intervals: Sequence[Interval], done_stage: str, category_of: Callable[[str | None], str | None]
) -> str | None:
    done_at = None
    for interval in reversed(intervals):
        if interval.stage is not None:
            is_done = interval.stage == done_stage
        else:
            is_done = category_of(interval.status) == "done"
        if not is_done:
            break
        done_at = interval.entered_at
    return done_at


def _current_estimate(issue: Mapping[str, Any], estimate: EstimateConfig) -> object:
    return issue.get(_ESTIMATE_COLUMN[estimate.field])


def _number(value: object) -> float | None:
    """A finite float from a number or a numeric string; None for anything else."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None
