"""GET /health (contract 4.1): is the service up, what it serves, when it last synced.

Polled every minute by the interface's shell, so it stays cheap: one read-only
connection and a few one-row queries. It opens that connection itself rather
than through read_conn, because health must answer (degraded, with a plain
message) even when there is no database at all.

Degraded when:
- there is no snapshot (no database file, or one that cannot be read);
- no sync of the board has succeeded yet (the project has no last_synced_at in sync_state): health still answers 200
  for such a board, never board_not_synced's 409 (JO-17), so the shell keeps its status;
- the newest finished sync run that covered the board failed after its last successful sync. A run covers the boards
  its stats name (`projects`; JO-18 syncs one board at a time); a run from before that names none and counts for
  every board. A run that was stopped part way (Ctrl-C, or the sync service stopped or restarted: its stats say
  KeyboardInterrupt) is said as stopped, not as failed: a routine restart is no fault, though the figures are still
  the sync's before it.
A sync still running decides nothing. `meta` is sent whenever the database can
be read; without it the key is left out (the schema allows that here only, and
null would not validate).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from oversight.api.deps import API_VERSION, ApiContext, get_ctx
from oversight.api.meta import build_meta
from oversight.logs import get_logger
from oversight.store.db import open_readonly
from oversight.timeutil import parse_ts

router = APIRouter()
log = get_logger("api")

NO_SNAPSHOT = "No snapshot of Jira yet: the first sync has not run."
UNREADABLE = "The snapshot is missing or cannot be read."
NEVER_SYNCED = "This board hasn't been synced yet."
NEVER_SYNCED_FAILED = "This board's first sync failed."
SYNC_FAILED = "The last Jira sync failed; the figures are from the sync before it."
NEVER_SYNCED_STOPPED = "This board's first sync was stopped before it finished."
SYNC_STOPPED = "The last Jira sync was stopped before it finished; the figures are from the sync before it."
# How a sync run stopped part way records its end (oversight/ingest/sync.py records a failed run on BaseException).
_STOPPED = "KeyboardInterrupt"

# The newest finished sync run that covered the board: one whose stats name it among their projects, or one from before
# the stats named any (it counts for every board).
_LAST_FINISHED_SYNC_SQL = """
SELECT status, started_at, stats_json FROM runs
WHERE kind = 'sync' AND status IN ('succeeded', 'failed')
  AND CASE WHEN json_valid(stats_json) AND json_type(stats_json, '$.projects') = 'array'
           THEN EXISTS (SELECT 1 FROM json_each(stats_json, '$.projects') WHERE value = ?)
           ELSE 1 END
ORDER BY started_at DESC LIMIT 1
"""


@router.get("/health")
def health(ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    if not ctx.db_path.exists():
        return _body(ctx, None, NO_SNAPSHOT)
    try:
        conn = open_readonly(ctx.db_path)
        try:
            meta = build_meta(conn, ctx)
            message = _sync_problem(conn, ctx.project, meta["last_synced_at"])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("api_health_unreadable", error_type=type(exc).__name__)
        return _body(ctx, None, UNREADABLE)
    return _body(ctx, meta, message)


def _body(ctx: ApiContext, meta: dict[str, Any] | None, message: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {} if meta is None else {"meta": meta}
    body.update(
        status="ok" if message is None else "degraded",
        message=message,
        data_mode="live",
        read_only=True,
        api_version=API_VERSION,
        # Unknown before the first sync: null, as the contract's rule for unknown values says.
        last_synced_at=meta["last_synced_at"] if meta else None,
        projects=[ctx.project],
        report_timezone=ctx.settings.report_zone,
    )
    return body


def _sync_problem(conn: sqlite3.Connection, project: str, last_synced_at: str | None) -> str | None:
    """Why the board's data needs attention, in plain words; None when it does not."""
    row = conn.execute(_LAST_FINISHED_SYNC_SQL, (project,)).fetchone()
    failed = row is not None and row["status"] == "failed"
    stopped = failed and _error_type(row["stats_json"]) == _STOPPED
    if last_synced_at is None:
        return (NEVER_SYNCED_STOPPED if stopped else NEVER_SYNCED_FAILED) if failed else NEVER_SYNCED
    # A failure no later than the last success (a later sync, or a demo seeding) is history.
    if failed and not _no_later(row["started_at"], last_synced_at):
        return SYNC_STOPPED if stopped else SYNC_FAILED
    return None


def _error_type(stats_json: object) -> str | None:
    """The error type a failed run's stats record; None when they record none or cannot be read."""
    try:
        stats = json.loads(stats_json) if isinstance(stats_json, str) else None
    except ValueError:
        return None
    found = stats.get("error_type") if isinstance(stats, dict) else None
    return found if isinstance(found, str) else None


def _no_later(first: object, second: object) -> bool:
    """True when both times parse and `first` is not after `second`; unknown order counts as later."""
    try:
        a, b = parse_ts(first), parse_ts(second)
    except (TypeError, ValueError):
        return False
    return isinstance(a, datetime) and isinstance(b, datetime) and a <= b
