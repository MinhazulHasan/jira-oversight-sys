"""Status intervals from status history (Phase 2).

An issue's timeline is an unbroken chain of intervals: from created_at to its
first status change, from each change to the next, and an open interval for
the status it is in now. Hours are calendar hours.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from oversight.timeutil import parse_ts, to_iso

# status_intervals.status is NOT NULL and a timeline has no gaps, so a change
# whose status name is missing still opens an interval, under this name.
UNKNOWN_STATUS = "(unknown)"


@dataclass(frozen=True)
class Interval:
    status: str
    stage: str | None  # James's stage from process.yaml; None if the status is unmapped
    entered_at: str  # storage-format UTC timestamp
    left_at: str | None  # None: still in this status
    hours: float | None  # None while still in this status


@dataclass
class TimelineWarnings:
    # A change whose from_status differs (case-insensitively) from the status the
    # issue was in: history was edited, or a status was renamed.
    inconsistent_transitions: int = 0
    # A change dated before created_at; its interval is clamped to 0 hours.
    negative_intervals: int = 0


def sort_changes(changes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Order status changes by (changed_at, numeric changelog_id). Non-numeric ids sort after numeric ones."""
    return sorted(changes, key=_change_order)


def build_intervals(
    created_at: str,
    current_status: str,
    changes: Sequence[Mapping[str, Any]],
    stage_of: Callable[[str | None], str | None],
    warnings: TimelineWarnings | None = None,
) -> list[Interval]:
    """Rebuild one issue's status intervals (Planning.md Phase 2).

    `changes` are status_changes rows (from_status, to_status, changed_at,
    changelog_id), sorted here with sort_changes. The first interval starts at
    created_at, in the first change's from_status, or in current_status if there
    are no changes. Each change closes the current interval at changed_at and
    opens one in to_status. The last interval is open (left_at None, hours None).
    hours = max(0, left - entered) in hours. Counts anomalies into `warnings`.
    An issue created directly in Done gives a single open Done interval.

    Timestamps may be in any form oversight.timeutil.parse_ts accepts; the
    intervals carry them in the storage format. A missing status name becomes
    UNKNOWN_STATUS (and so has no stage).
    """
    if warnings is None:
        warnings = TimelineWarnings()
    ordered = sort_changes(changes)
    status = _status_name(ordered[0].get("from_status") if ordered else current_status)
    entered = _timestamp(created_at, "created_at")
    intervals: list[Interval] = []
    for change in ordered:
        if _status_name(change.get("from_status")).casefold() != status.casefold():
            warnings.inconsistent_transitions += 1
        left = _timestamp(change.get("changed_at"), "changed_at")  # already checked by the sort
        hours = (left - entered).total_seconds() / 3600
        if hours < 0:
            # Only the first interval can go negative: the changes are sorted.
            warnings.negative_intervals += 1
            hours = 0.0
        intervals.append(
            Interval(
                status=status,
                stage=stage_of(status),
                entered_at=_storage(entered),
                left_at=_storage(left),
                hours=hours,
            )
        )
        status, entered = _status_name(change.get("to_status")), left
    intervals.append(
        Interval(status=status, stage=stage_of(status), entered_at=_storage(entered), left_at=None, hours=None)
    )
    return intervals


def _change_order(change: Mapping[str, Any]) -> tuple[datetime, int, int, str]:
    # Changelog ids grow over time, so they break ties between changes made in
    # the same millisecond; compared as text, "10" would sort before "9".
    raw_id = change.get("changelog_id")
    text = "" if raw_id is None else str(raw_id)
    when = _timestamp(change.get("changed_at"), f"changed_at of change {text or '(no id)'}")
    if text.isascii() and text.isdigit():
        return (when, 0, int(text), "")
    return (when, 1, 0, text)


def _timestamp(value: object, what: str) -> datetime:
    moment = parse_ts(value)
    if moment is None:
        raise ValueError(f"{what} is missing")
    return moment


def _storage(moment: datetime) -> str:
    # to_iso only returns None for None; the cast records that for type checkers.
    return cast(str, to_iso(moment))


def _status_name(value: object) -> str:
    if value is None or not str(value).strip():
        return UNKNOWN_STATUS
    return str(value)
