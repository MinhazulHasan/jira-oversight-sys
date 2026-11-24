"""What every dashboard API route needs: the context, a read-only connection, the key check."""
from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Query, Request, Security, status
from fastapi.security import APIKeyHeader

from oversight.api.boards import BoardIndex, Served
from oversight.config import FieldsConfig, ProcessConfig
from oversight.rules.catalog import RulesConfig
from oversight.rules.settings import board_rules
from oversight.settings import Settings
from oversight.store.db import open_readonly

API_VERSION = "0.1.0"
SERVICE_KEY_HEADER = "X-Service-Key"
# The board picker's query parameter: every route takes it (get_ctx reads it).
PROJECT_PARAM = "project"


@dataclass
class ApiContext:
    settings: Settings
    # process.yaml at start; for a request, once it has read its boards (served), every served board mapped.
    process: ProcessConfig
    # The rule catalogue, config/rules.yaml: every rule's definition and the defaults every board starts from.
    catalogue: RulesConfig
    fields: FieldsConfig
    db_path: Path
    # The LLM provider for /ask and nothing else (scripted in LLM_MODE=stub).
    provider: Any
    now: Callable[[], datetime]
    # The board this request reads (?project=KEY); None means the default board.
    selected: str | None = None
    boards: BoardIndex | None = None
    _rules: RulesConfig | None = field(default=None, init=False, repr=False, compare=False)
    # The boards this request sees once it has read them (served). Never copied, as _rules.
    _served: Served | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def projects(self) -> list[str]:
        """The boards the dashboard can switch between"""
        return self.served().keys

    def served(self, conn: sqlite3.Connection | None = None) -> Served:
        if self._served is None:
            index = self.boards or BoardIndex(self.settings.model_copy(update={"board_discovery": "off"}), self.process)
            self._served = index.read(self.db_path, conn)
            self.process = self._served.process
        return self._served

    @property
    def project(self) -> str:
        """The board this request reads: the one picked, else the first of JIRA_PROJECTS."""
        return self.selected or self.settings.jira_projects[0]

    @property
    def rules(self) -> RulesConfig:
        if self._rules is None:
            self._rules = board_rules(self.db_path, self.catalogue, self.project)
        return self._rules

    def jira_url(self, issue_key: str | None) -> str | None:
        """The issue's Jira link, or None: no base URL, a synthetic project, or no key."""
        base = self.settings.jira_base_url
        if not base or not issue_key or self.process.project(self.project).synthetic:
            return None
        return f"{base}/browse/{issue_key}"


class ApiError(Exception):

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        headers: dict[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})


LEVEL_PARAM = "level"
LEVELS = ("team", "people")
# The refusal of a people-level route at team level (contract 4.15 to 4.17).
PEOPLE_LEVEL_ONLY = "people_level_only"
UNKNOWN_BOARD = "unknown_board"
BOARD_NOT_SYNCED = "board_not_synced"


def people_level_only(level: str, what: str) -> None:
    """Least privilege first: a team-level request learns nothing from a people-level route."""
    if level != "people":
        raise ApiError(403, PEOPLE_LEVEL_ONLY, f"{what} are for people level only")


def get_ctx(
    request: Request,
    project: str | None = Query(
        None,
        max_length=20,
        description="The board to read (one the service serves: GET /projects lists them); default: the first of "
        "JIRA_PROJECTS",
    ),
) -> ApiContext:
    ctx: ApiContext = request.app.state.ctx
    if project is None:
        return replace(ctx)
    key = project.strip().upper()
    board = replace(ctx, selected=key)
    if key not in ctx.settings.jira_projects and board.served().board(key) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, UNKNOWN_BOARD, "that board is not one this service serves")
    return board


# Declared as a security scheme so /docs (API_DOCS=true) offers an Authorize button.
_service_key = APIKeyHeader(name=SERVICE_KEY_HEADER, auto_error=False, description="API_SERVICE_KEY from .env")


def require_key(given: str | None = Security(_service_key), ctx: ApiContext = Depends(get_ctx)) -> None:
    """401 unless X-Service-Key matches API_SERVICE_KEY (constant-time)."""
    expected = ctx.settings.api_service_key.get_secret_value()
    if not expected:
        if ctx.settings.api_allow_insecure:
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "the API has no service key configured")
    given = given or ""
    if not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or wrong service key")


def _open(ctx: ApiContext) -> sqlite3.Connection:
    """A read-only connection for one request, its boards read through it (ctx.served)"""
    if not ctx.db_path.exists():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no snapshot yet: run `python -m oversight sync --full` (or `demo`), then derive and rules",
        )
    conn = open_readonly(ctx.db_path, any_thread=True)
    try:
        ctx.served(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def read_conn(ctx: ApiContext = Depends(get_ctx)) -> Iterator[sqlite3.Connection]:
    """A board-scoped route's read-only connection"""
    conn = _open(ctx)
    try:
        board = ctx.served().board(ctx.project)
        if board is not None and not board.synced:
            name = ctx.process.project(board.key).name or board.key  # every served board is mapped (served)
            raise ApiError(status.HTTP_409_CONFLICT, BOARD_NOT_SYNCED, f"{name} hasn't been synced yet.")
        yield conn
    finally:
        conn.close()


def service_conn(ctx: ApiContext = Depends(get_ctx)) -> Iterator[sqlite3.Connection]:
    conn = _open(ctx)
    try:
        yield conn
    finally:
        conn.close()
