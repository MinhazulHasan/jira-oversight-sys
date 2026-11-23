"""The dashboard API (Phase 8): FastAPI, read-only towards Jira, behind a service key.

Implements docs/API_CONTRACT.md in the jira-oversight-ui repository (its zod
schemas in src/lib/api/schemas.ts are the source of truth). Every route lives
under /api/v1 and needs the X-Service-Key header; one router module per
endpoint group sits in oversight/api/routes/. `python -m oversight api` serves
it with uvicorn on API_HOST:API_PORT.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from oversight.api.access_log import VIEWER_HEADER, VIEWER_NAME_HEADER, AccessLog, purge_access_log
from oversight.api.boards import BoardIndex, Discoverer, discovery_for
from oversight.api.deps import API_VERSION, SERVICE_KEY_HEADER, ApiContext, ApiError, require_key
from oversight.api.ratelimit import RateLimiter
from oversight.api.refresh import Refresher, board_synced, run_step
from oversight.api.routes import (
    ask,
    baseline,
    findings,
    governance,
    health,
    insights,
    issues,
    overview,
    people,
    person_card,
    portfolio,
    projects,
    refresh,
    rule_settings,
    rules,
    trends,
)
from oversight.config import FieldsConfig, ProcessConfig
from oversight.logs import configure_logging, get_logger
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings, get_settings
from oversight.store.db import Store
from oversight.timeutil import utcnow

log = get_logger("api")

ROUTERS = (
    health, projects, refresh, overview, findings, issues, people, person_card, ask, insights, trends, rule_settings,
    rules, baseline, portfolio, governance,
)


def prepare_snapshot(db_file: Path, *, access_log_days: int | None = None, now: Callable[..., Any] = utcnow) -> None:
    """Bring an existing snapshot to this version's schema before serving it, then (given ACCESS_LOG_DAYS) delete
    the access log entries past it: the retention purge at start.

    The routes read through read-only connections, which never migrate, so a server
    started on an older snapshot would answer 500 (no such table: people) until the
    next sync. Both ways of serving (`python -m oversight api` and run.py) call this
    first. A missing snapshot is left missing: the routes say so until a sync or
    `demo` makes one. A purge that fails is logged and left to the daily one: it never stops the API starting.
    """
    if not db_file.exists():
        return
    Store.open(db_file).close()
    if access_log_days is not None:
        try:
            purge_access_log(db_file, access_log_days, now())
        except sqlite3.Error as exc:
            log.warning("access_log_purge_failed", error_type=type(exc).__name__)


def route_template(request: Request) -> str:
    """The route a request matched as declared, its routers' prefix included ("/api/v1/people/{person_id}/avatar"),
    for a log line: never the path's values or the query. "unmatched" when no route matched. The matched route knows
    only its own part of the path (a router included under a prefix keeps its routes as they are), so the prefix is
    what the request's path holds before that part."""
    route = request.scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not isinstance(template, str) or not template:
        return "unmatched"
    try:
        concrete = template.format(**request.path_params)
    except (KeyError, IndexError, ValueError):
        return template
    path = request.url.path
    return path[: len(path) - len(concrete)] + template if path.endswith(concrete) else template


def serve_app() -> FastAPI:
    """run.py's factory. uvicorn calls it in each worker it starts, a reloaded one too (API_RELOAD), so the
    snapshot is brought to this version's schema whenever new code starts serving it, not only when the server
    itself starts: code reloaded onto an older snapshot would otherwise answer 500 until the next sync. Each worker
    logs as run.py does (LOG_FORMAT): a reloaded one is a process of its own, which run.py's set-up never reached."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    prepare_snapshot(settings.db_file, access_log_days=settings.access_log_days)
    return create_app(settings)


def create_app(
    settings: Settings | None = None,
    *,
    store_path: Path | None = None,
    provider: Any = None,
    now: Callable[..., Any] = utcnow,
    refresher: Refresher | None = None,
    discoverer: Discoverer | None = None,
) -> FastAPI:
    """Build the app. Tests inject settings, a database path, a provider, a clock, the refresher and the discoverer.

    A board process.yaml does not map never stops it starting (JO-17): each request maps every board it serves
    (oversight/api/boards.py). When it starts serving (its lifespan: uvicorn's start, never a bare TestClient), it
    starts a board discovery in the background (with BOARD_DISCOVERY on and the Jira settings in place)."""
    settings = settings or get_settings()
    process = ProcessConfig.load(settings.process_file)
    rules_config = RulesConfig.load(settings.rules_file)
    fields = FieldsConfig.load(settings.fields_file) if settings.fields_file.exists() else FieldsConfig()
    if provider is None:
        from oversight.llm.provider import get_provider

        provider = get_provider(settings)
    db_path = Path(store_path) if store_path is not None else settings.db_file

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Board discovery at start, in the background: serving never waits on Jira (JO-17).
        app.state.discoverer.start()
        yield

    app = FastAPI(
        title="Jira Oversight API",
        version=API_VERSION,
        # The interface is the only client: interactive docs only when API_DOCS asks for them.
        docs_url="/docs" if settings.api_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.api_docs else None,
        lifespan=_lifespan,
    )
    app.state.ctx = ApiContext(
        settings=settings,
        process=process,
        catalogue=rules_config,
        fields=fields,
        db_path=db_path,
        provider=provider,
        now=now,
        boards=BoardIndex(settings, process),
    )
    # Board discovery in this process (JO-17): at start and when GET /projects finds the list old.
    app.state.discoverer = discoverer or Discoverer(discovery_for(settings, db_path, now), now=now)
    # "Sync now" for one board (POST /refresh, JO-18) and the re-check after a board's rule settings are saved (JO-15):
    # one job at a time for this process, each step told to use the snapshot this app serves.
    app.state.refresher = refresher or Refresher(
        runner=partial(run_step, db_path=db_path),
        synced=partial(board_synced, db_path),
        discover=settings.discovers_boards,
    )
    # Who viewed whose person-level data (JO-12), and its retention purge, once a day.
    app.state.access_log = AccessLog(app.state.ctx.db_path, days=settings.access_log_days, now=now)
    # Ask's rate limit per viewer (ASK_RATE_LIMIT), in this process's memory; None when off.
    rule = settings.ask_rate_limit_rule
    app.state.ask_limiter = RateLimiter(*rule) if rule is not None else None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=[SERVICE_KEY_HEADER, VIEWER_HEADER, VIEWER_NAME_HEADER, "Content-Type", "Accept"],
    )

    @app.middleware("http")
    async def _headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # The access log's daily retention purge rides on whatever request comes first that day (the dashboard
        # polls /health every minute); it never fails one.
        access: AccessLog = app.state.access_log
        if access.due():
            await run_in_threadpool(access.purge_if_due)
        started = time.perf_counter()
        response = await call_next(request)
        # Nothing is cached, except where a route says otherwise: a person's picture (contract 4.13).
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        # One structured line a request, in place of the web server's plain access line: the route as declared
        # (/api/v1/people/{person_id}/avatar), never the path's values or the query string (a search as typed).
        log.info("api_request", method=request.method, route=route_template(request), status=response.status_code,
                 ms=round((time.perf_counter() - started) * 1000))
        return response

    @app.exception_handler(ApiError)
    async def _refused(request: Request, exc: ApiError) -> JSONResponse:
        # The usual body, with the code the contract names (unknown_board, board_not_synced, people_level_only,
        # comparison_off, ask_rate_limited, access_log_unavailable, rule_settings_conflict, rule_settings_invalid with
        # its field-level errors), and its headers (Retry-After).
        return JSONResponse({"detail": exc.detail, "code": exc.code, **exc.extra}, status_code=exc.status_code,
                            headers=exc.headers or None)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Logged with its traceback; the response names nothing internal.
        log.exception("api_unhandled_error", path=request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    @app.get("/", include_in_schema=False)
    def _root() -> dict[str, Any]:
        """What this is, for someone who opens it in a browser. Nothing here needs the key."""
        return {
            "service": "Jira Oversight API",
            "version": API_VERSION,
            "api": "/api/v1 (send the X-Service-Key header)",
            "docs": "/docs" if settings.api_docs else "off (set API_DOCS=true)",
            "dashboard": "http://localhost:3400",
        }

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_key)])
    for module in ROUTERS:
        api.include_router(module.router)
    app.include_router(api)
    return app


def context(app: FastAPI) -> ApiContext:
    """The app's context (for tests)."""
    ctx: ApiContext = app.state.ctx
    return ctx
