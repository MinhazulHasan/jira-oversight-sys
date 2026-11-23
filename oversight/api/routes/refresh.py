"""POST /refresh starts "sync now" for one board; GET /refresh says how it is going (oversight/api/refresh.py, JO-18).

Both are board-scoped: `?project=KEY` (the board picked in the top bar; the default board without it) names the board
to sync or report, and a board the service does not serve is 404 unknown_board, as everywhere. A board that has never
synced is no refusal here: this is how its first sync starts. The dashboard's Sync button posts, then polls the GET
until the state is neither "queued" nor "running", then reloads that board's figures. Jira is only read. POST takes no
body and answers 202 with the board's state: its refresh already queued or running, or one that finished moments ago,
is reported rather than started again; another board's refresh running puts this one in the queue ("queued"). The
body: {project, state, started_at, finished_at, message}.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from oversight.api.deps import ApiContext, get_ctx
from oversight.api.refresh import Refresher

router = APIRouter()


def refresher(request: Request) -> Refresher:
    runner: Refresher = request.app.state.refresher
    return runner


@router.get("/refresh")
def refresh_status(request: Request, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return refresher(request).status(ctx.project)


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
def refresh_start(request: Request, ctx: ApiContext = Depends(get_ctx)) -> dict[str, Any]:
    return refresher(request).start(ctx.project)
