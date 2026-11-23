"""What every dashboard API route needs: the context, a read-only connection, the key check.

The API serves every board the service serves (JO-17, oversight/boards.py): JIRA_PROJECTS, then, with BOARD_DISCOVERY
on, every board discovery found in Jira; one per request: `?project=KEY` picks one (the dashboard's board picker sends
it), and without it the first of JIRA_PROJECTS is served. A key the service does not serve is a 404 with the code
unknown_board, on every route. A board-scoped route (read_conn) on a served board that has never synced is a 409
{"detail", "code": "board_not_synced"}, so the dashboard can offer its first sync; the routes that belong to no one
board (the portfolio and the Governance routes) read through service_conn, which never refuses a board for that. Each
request's process maps every served board (process.yaml's entry, else one worked out from Jira's status categories,
oversight/api/boards.py). Every read goes through its own read-only SQLite connection, opened
per request and closed after it, so a sync can replace the database while the
API runs. A few routes write, never to Jira: POST /ask its own audit rows through
the ask service's Store, POST /refresh (sync now) the snapshot, through the
same CLI steps as the scheduled cycle, the access log its entries
(oversight/api/access_log.py), POST /rules/settings a board's rule settings,
the rules its admins add (JO-20) and their audit trail (JO-15,
oversight/api/rule_settings.py), and POST /rules/settings/draft its model
calls' audit rows (JO-20, oversight/api/rule_draft.py).

Each request's context holds the board's own rules (ApiContext.rules): the
catalogue (config/rules.yaml) with the board's rule settings applied and the
rules its admins added (JO-20), read from the snapshot the first time the
request needs them. So every board-scoped route
(the Process page, the scorecards, the issue timelines, Ask) reads the board's
thresholds, severities and which rules are on, and meta.rules_version stays the
catalogue's version.
"""
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
    # The boards the service serves and their mappings, shared by every request (JO-17); None: JIRA_PROJECTS only,
    # each as process.yaml maps it.
    boards: BoardIndex | None = None
    # The board's rules once this context has read them (rules). Never copied: replace() gives a new context, for
    # another board too, its own.
    _rules: RulesConfig | None = field(default=None, init=False, repr=False, compare=False)
    # The boards this request sees once it has read them (served). Never copied, as _rules.
    _served: Served | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def projects(self) -> list[str]:
        """The boards the dashboard can switch between: JIRA_PROJECTS, in order, then every board discovery found, by
        name (served)."""
        return self.served().keys

    def served(self, conn: sqlite3.Connection | None = None) -> Served:
        """The boards this request sees and the process with each mapped, read once (through `conn` when given) and
        kept; this context's process becomes that one."""
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
        """The rules the board runs with (JO-15): the catalogue with the board's rule settings applied
        (oversight/rules/settings.py), read from the snapshot the first time this context needs them, through a
        read-only connection of their own (three small queries), then kept: get_ctx gives every request a context of
        its own. The catalogue itself without a snapshot, or for a board nobody has changed."""
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
    """A refusal the contract names by a code: the API's usual error body, {"detail": "<plain words>"}, with the
    code beside it, {"detail": ..., "code": "people_level_only"} (app.py turns it into the response), any headers it
    needs (Retry-After on a 429), and any fields the body carries besides (rule_settings_invalid's field-level
    `errors`)."""

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


# The level a request is made at, set by the dashboard's server on every request that needs it and never by the
# browser: ?level=team|people on the GET routes (team when left out), "level" in POST /ask's body.
LEVEL_PARAM = "level"
LEVELS = ("team", "people")
# The refusal of a people-level route at team level (contract 4.15 to 4.17).
PEOPLE_LEVEL_ONLY = "people_level_only"
# A board this service does not serve (neither in JIRA_PROJECTS nor found by discovery): the 404's code, so a route's
# own 404 ("nobody by that id is on this board", JO-13) is never mistaken for it, nor it for one.
UNKNOWN_BOARD = "unknown_board"
# A board the service serves that has never synced (JO-17): a board-scoped route's 409, so the dashboard offers its
# first sync instead of an error.
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
    """The context for the board this request picked, the request's own (so the board's rules and boards it reads are
    never another request's); a board it does not serve is a 404 with the code unknown_board, whatever the route. A
    board in JIRA_PROJECTS is served without reading the snapshot; any other is looked up there (served)."""
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
    """401 unless X-Service-Key matches API_SERVICE_KEY (constant-time). A blank key
    is accepted only when API_ALLOW_INSECURE is true: an open API is a visible choice."""
    expected = ctx.settings.api_service_key.get_secret_value()
    if not expected:
        if ctx.settings.api_allow_insecure:
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "the API has no service key configured")
    given = given or ""
    if not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or wrong service key")


def _open(ctx: ApiContext) -> sqlite3.Connection:
    """A read-only connection for one request, its boards read through it (ctx.served); 503 when there is no snapshot
    yet."""
    if not ctx.db_path.exists():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no snapshot yet: run `python -m oversight sync --full` (or `demo`), then derive and rules",
        )
    # FastAPI runs this dependency and the route in different worker threads; the
    # request uses the connection one step at a time, so it may cross threads.
    conn = open_readonly(ctx.db_path, any_thread=True)
    try:
        ctx.served(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def read_conn(ctx: ApiContext = Depends(get_ctx)) -> Iterator[sqlite3.Connection]:
    """A board-scoped route's read-only connection; 503 when there is no snapshot yet, and 409 board_not_synced when
    the board picked has never synced (JO-17): nothing of it to read yet."""
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
    """The read-only connection of a route that belongs to no one board (the portfolio, the Governance routes): as
    read_conn, but a board never synced is no refusal here."""
    conn = _open(ctx)
    try:
        yield conn
    finally:
        conn.close()
