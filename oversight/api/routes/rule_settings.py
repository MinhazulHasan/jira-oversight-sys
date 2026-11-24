from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from oversight.api.access_log import Viewer, need_viewer
from oversight.api.deps import (
    LEVEL_PARAM,
    LEVELS,
    PROJECT_PARAM,
    ApiContext,
    ApiError,
    get_ctx,
    people_level_only,
    read_conn,
)
from oversight.api.ratelimit import RATE_LIMITED, RateLimiter
from oversight.api.routes.refresh import refresher
from oversight.api.rule_draft import draft_body, parse_draft
from oversight.api.rule_settings import (
    BODY_LIMIT,
    parse_preview,
    parse_save,
    preview_body,
    save,
    settings_body,
    too_large,
)
from oversight.logs import get_logger

router = APIRouter()
log = get_logger("api")

_PARAMETERS = frozenset({PROJECT_PARAM, LEVEL_PARAM})


def admin(request: Request) -> Viewer:
    """What these routes check before anything is read"""
    counts: dict[str, int] = {}
    for name, _value in request.query_params.multi_items():
        counts[name] = counts.get(name, 0) + 1
    problems: list[dict[str, Any]] = []
    for name, count in counts.items():
        if name not in _PARAMETERS:
            problems.append({"loc": ["query", name], "msg": "is not a parameter of this endpoint", "type": "unknown"})
        elif count > 1:
            problems.append({"loc": ["query", name], "msg": "may be given only once", "type": "repeated"})
    level = request.query_params.get(LEVEL_PARAM) or "team"
    if level not in LEVELS:
        problems.append({"loc": ["query", LEVEL_PARAM], "msg": "must be team or people", "type": "value_error"})
    if problems:
        raise HTTPException(422, problems)
    people_level_only(level, "rule settings")
    return need_viewer(request)


async def json_body(request: Request) -> bytes:
    """The request's body, at most BODY_LIMIT bytes: a larger one is refused before it is read whole."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > BODY_LIMIT:
        raise too_large()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > BODY_LIMIT:
            raise too_large()
    return bytes(body)


@router.get("/rules/settings", dependencies=[Depends(admin)])
def get_rule_settings(
    request: Request,
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    return settings_body(conn, ctx, refresher(request).recheck_of(ctx.project))


@router.post("/rules/settings/preview")
def preview_rule_settings(
    viewer: Viewer = Depends(admin),
    raw: bytes = Depends(json_body),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    return preview_body(conn, ctx, parse_preview(raw), viewer)


@router.post("/rules/settings/draft")
def draft_custom_rule(
    request: Request,
    viewer: Viewer = Depends(admin),
    raw: bytes = Depends(json_body),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    text = parse_draft(raw)
    limiter: RateLimiter | None = request.app.state.ask_limiter
    wait = limiter.admit(viewer.hash, ctx.now().timestamp()) if limiter is not None else None
    if wait is not None:
        log.info("rule_draft_rate_limited", project=ctx.project, viewer=viewer.hash, retry_after_s=wait)
        raise ApiError(429, RATE_LIMITED, f"too many drafts in a short time: try again in {wait} seconds",
                       headers={"Retry-After": str(wait)})
    return draft_body(conn, ctx, text, viewer)


@router.post("/rules/settings")
def save_rule_settings(
    request: Request,
    viewer: Viewer = Depends(admin),
    raw: bytes = Depends(json_body),
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    saved = save(ctx, parse_save(raw), viewer)
    log.info(
        "rule_settings_saved", project=ctx.project, revision=saved.revision,
        rules=list(dict.fromkeys(change.rule_id for change in saved.changes)),
        fields=sorted({change.field for change in saved.changes}),
    )
    runner = refresher(request)
    runner.recheck(ctx.project, saved.revision, name=ctx.process.project(ctx.project).name or ctx.project)
    return settings_body(conn, ctx, runner.recheck_of(ctx.project))
