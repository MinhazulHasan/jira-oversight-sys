"""History-based thresholds for early warnings (Phase 4).

Deterministic: thresholds come from the project's own closed intervals, never
from the AI.

- status_age_threshold: closed status_intervals (left_at set) of the project's
  issues whose status equals `status` (case-insensitive) and whose left_at is
  within the last history_days before `now`. Issues whose type is one of
  `exclude_types` (case-insensitive; the engine passes rules defaults.exclude_types)
  are left out: an Epic stays open for months, and pooling its intervals with
  the work's would inflate every percentile. An issue with no type is kept, as
  the rules keep it. Fewer than min_history intervals -> None (the caller stays
  silent and records why). Otherwise the pct-th percentile of their hours
  (oversight.stats.percentile) with n.
- stage_threshold: the same over intervals whose stage equals `stage`.
Hours are calendar hours, as everywhere in Phase 2.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from oversight.stats import percentile
from oversight.store.db import Store
from oversight.timeutil import to_iso

_CLOSED_INTERVALS_SQL = """
SELECT si.status, si.stage, si.hours, i.issue_type
FROM status_intervals si
JOIN issues i ON i.issue_key = si.issue_key
WHERE i.project_key = ?
  AND si.left_at IS NOT NULL AND si.hours IS NOT NULL
  AND si.left_at >= ? AND si.left_at <= ?
"""


@dataclass(frozen=True)
class Threshold:
    hours: float
    n: int
    pct: float


def status_age_threshold(
    store: Store,
    project: str,
    status: str,
    *,
    pct: float = 90,
    min_history: int = 20,
    history_days: int = 180,
    now: datetime,
    exclude_types: Iterable[str] = (),
) -> Threshold | None:
    return _threshold(
        store, project, "status", status, pct=pct, min_history=min_history, history_days=history_days, now=now,
        exclude_types=exclude_types,
    )


def stage_threshold(
    store: Store,
    project: str,
    stage: str,
    *,
    pct: float = 75,
    min_history: int = 20,
    history_days: int = 180,
    now: datetime,
    exclude_types: Iterable[str] = (),
) -> Threshold | None:
    return _threshold(
        store, project, "stage", stage, pct=pct, min_history=min_history, history_days=history_days, now=now,
        exclude_types=exclude_types,
    )


def _threshold(
    store: Store,
    project: str,
    column: str,
    value: str,
    *,
    pct: float,
    min_history: int,
    history_days: int,
    now: datetime,
    exclude_types: Iterable[str],
) -> Threshold | None:
    since = to_iso(now - timedelta(days=history_days))
    until = to_iso(now)
    wanted = value.casefold()
    excluded = {name.casefold() for name in exclude_types}
    # Matched in Python: SQLite's lower() only folds ASCII, and status and type names needn't be.
    hours = [
        row["hours"]
        for row in store.query(_CLOSED_INTERVALS_SQL, (project, since, until))
        if row[column] is not None
        and row[column].casefold() == wanted
        and (row["issue_type"] is None or row["issue_type"].casefold() not in excluded)
    ]
    if len(hours) < min_history:
        return None
    limit = percentile(hours, pct)
    if limit is None:  # unreachable with min_history >= 1; keeps the type honest
        return None
    return Threshold(hours=limit, n=len(hours), pct=pct)
