"""GET /rules/settings, POST /rules/settings/preview and POST /rules/settings (contract 4.20 to 4.22): an admin's rule
settings for one board (JO-15), custom rules among them (JO-20); and POST /rules/settings/draft (contract 4.23), a
custom rule drafted from the admin's words. The bodies, the apply state and the reading of a body are
oversight/api/rule_settings.py's (the draft's, oversight/api/rule_draft.py's); what may change and how it is stored,
oversight/rules/settings.py's.

All four are people level only: the dashboard's server sends ?level=people for an admin; team (or no level) is 403
{"detail", "code": "people_level_only"}, as the Governance routes answer, and a level that is neither team nor people
is a 422, as a parameter that is not one. They are board-scoped (?project=KEY, the
default board without it; one the service does not serve is 404 unknown_board, checked with the service key, as
everywhere), take no other parameter (any other, or one given twice, is a 422) and must name their viewer
(X-Oversight-Viewer and its -Name, read exactly as the access log reads them: none, or a hash that is not one, is a
422). After the key and the board, the parameters, the level and the viewer are checked in that order (admin), all
before a body is read. A body larger than 16 KB is refused (422 rule_settings_invalid) before it is read whole.
No-store, as every JSON route.

POST /rules/settings writes, never to Jira: the board's rows and the change, in one write transaction, then it starts
the board's re-check (oversight/api/refresh.py: `rules --project KEY`, queued behind a sync that is running) and
answers 200 with the board's settings, as GET answers, apply running. A save made from another revision is 409
{"detail", "code": "rule_settings_conflict"}; a bad body or change set is 422 rule_settings_invalid; a write lock a
rules run holds past rule_settings.WRITE_WAIT_MS is 503 {"detail", "code": "rule_settings_busy"}: nothing is written
in any of them. The app log has one line for each save (rule_settings_saved: the board, the revision, the rule ids and
the fields changed; no name, no value).

POST /rules/settings/draft writes no setting, only its model calls' audit rows. While ASK_RATE_LIMIT is set, the
viewer's draft counts against the same limit as their questions (one bucket per viewer, oversight/api/ratelimit.py):
over it, 429 {"detail", "code": "ask_rate_limited"} with Retry-After, before anything is asked of the model; a body it
refuses (422) is not counted. The app log has one line for each draft, whatever its outcome (rule_draft: the board,
the viewer's hash, the outcome, how many problems and the llm_calls ids of its model calls; never the words: written
by oversight/api/rule_draft.draft_body), and one for each draft the limit stops (rule_draft_rate_limited, with the
viewer's hash).
"""
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
    """What these routes check before anything is read: no parameter but project and level, each at most once, and a
    level that is one (422 otherwise); people level (403); then the viewer (422 when none is named)."""
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
