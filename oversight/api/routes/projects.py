from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request

from oversight.api.boards import Discoverer
from oversight.api.deps import ApiContext, get_ctx
from oversight.boards import Board, last_discovery
from oversight.logs import get_logger
from oversight.store.db import open_readonly

router = APIRouter()
log = get_logger("api")

_COUNTS_SQL = "SELECT project_key, COUNT(*) FROM issues GROUP BY project_key"


@router.get("/projects")
def list_projects(request: Request, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    counts: dict[str, int] = {}
    discovered: str | None = None
    if ctx.db_path.exists():
        try:
            conn = open_readonly(ctx.db_path)
            try:
                counts = {row[0]: int(row[1]) for row in conn.execute(_COUNTS_SQL).fetchall()}
                discovered = last_discovery(conn)
                ctx.served(conn)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("api_projects_unreadable", error_type=type(exc).__name__)
            counts, discovered = {}, None
    served = ctx.served()  # read above; else JIRA_PROJECTS alone (no snapshot, or one that cannot be read)
    discoverer: Discoverer = request.app.state.discoverer
    discoverer.refresh_if_stale(discovered)
    default = ctx.settings.jira_projects[0]

    def name(board: Board) -> str:
        return ctx.process.project(board.key).name or board.key

    def order(board: Board) -> tuple[int, int, str, str]:
        return (board.key != default, not board.synced, name(board).casefold(), board.key)

    return {
        "default": default,
        "projects": [
            {
                "key": board.key,
                "name": name(board),
                "issues": counts.get(board.key, 0),
                "last_synced_at": board.last_synced_at,
                "synced": board.synced,
                "type": board.type,
            }
            for board in sorted(served.boards, key=order)
        ],
    }
