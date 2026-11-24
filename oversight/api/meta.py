from __future__ import annotations

import sqlite3
from typing import Any

from oversight.api.deps import ApiContext
from oversight.timeutil import to_iso


def last_synced_at(conn: sqlite3.Connection, project: str) -> str | None:
    """Start of the project's last successful sync (sync_state), or None before the first."""
    row = conn.execute("SELECT last_synced_at FROM sync_state WHERE scope = ?", (project,)).fetchone()
    return row[0] if row and row[0] else None


def service_meta(conn: sqlite3.Connection, ctx: ApiContext) -> dict[str, Any]:
    served = ctx.served(conn)
    synced = [board.last_synced_at for board in served.boards if board.last_synced_at]
    return {**build_meta(conn, ctx), "projects": served.keys, "last_synced_at": max(synced) if synced else None}


def build_meta(conn: sqlite3.Connection, ctx: ApiContext) -> dict[str, Any]:
    project = ctx.project
    return {
        "data_mode": "live",
        "last_synced_at": last_synced_at(conn, project),
        "projects": [project],
        "rules_version": ctx.catalogue.version,
        "process_version": ctx.process.process_version,
        "generated_at": to_iso(ctx.now()),
        "report_timezone": ctx.settings.report_zone,
    }
