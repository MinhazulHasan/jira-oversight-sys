"""POST /refresh starts 'sync now' for one board"""
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
