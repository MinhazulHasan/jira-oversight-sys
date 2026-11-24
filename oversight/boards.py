from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from oversight.config import EstimateConfig, ProcessConfig, ProjectProcess
from oversight.logs import get_logger
from oversight.store.db import Store
from oversight.timeutil import utcnow

if TYPE_CHECKING:
    from oversight.jira.client import JiraClient
    from oversight.settings import Settings

# Hours a story point is worth on a board mapped automatically: what every board in process.yaml uses today.
AUTOMATIC_HOURS_PER_POINT = 8.0
# A key the sync can put into JQL (oversight.settings._PROJECT_KEY); Jira's own keys always are.
_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,19}")
# ProjectProcess.name's longest; Jira's own limit is 80 too.
_NAME_MAX = 80

_SERVED_SQL = "SELECT project_key, name, project_type FROM boards WHERE disappeared_at IS NULL"
_SYNCED_SQL = "SELECT scope, last_synced_at FROM sync_state WHERE last_synced_at IS NOT NULL AND last_synced_at != ''"
_LAST_DISCOVERY_SQL = "SELECT MAX(finished_at) FROM runs WHERE kind = 'discover' AND status = 'succeeded'"
_STATUSES_SQL = """
    SELECT name, category, 0 AS source, CAST(status_id AS INTEGER) AS number, status_id FROM statuses
    WHERE project_key = ?
    UNION ALL
    SELECT status, status_category, 1, 0, '' FROM issues
    WHERE project_key = ? AND status IS NOT NULL
    GROUP BY status, status_category
    ORDER BY source, number, status_id, name
"""
_ESTIMATES_SQL = """
    SELECT COALESCE(SUM(story_points > 0), 0), COALESCE(SUM(original_estimate_s > 0), 0)
    FROM issues WHERE project_key = ?
"""

log = get_logger("boards")


# --- served and kept boards --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Board:
    """A board the service serves."""

    key: str
    name: str | None  # Jira's name for it, from discovery; None when discovery has not listed it
    type: str | None  # Jira's project type (software, business, service_desk, ...); None when not listed
    last_synced_at: str | None  # the start of its last successful sync; None before the first

    @property
    def synced(self) -> bool:
        return self.last_synced_at is not None


def served_boards(conn: sqlite3.Connection | None, settings: Settings) -> list[Board]:
    synced: dict[str, str] = {}
    found: dict[str, tuple[str | None, str | None]] = {}
    if conn is not None:
        synced = {row[0]: row[1] for row in conn.execute(_SYNCED_SQL).fetchall()}
        if settings.discovers_boards:
            try:
                found = {row[0]: (row[1], row[2]) for row in conn.execute(_SERVED_SQL).fetchall()}
            except sqlite3.OperationalError:
                found = {}
    listed = list(settings.jira_projects)
    others = sorted((key for key in found if key not in listed),
                    key=lambda key: ((found[key][0] or key).casefold(), key))
    return [
        Board(key=key, name=found.get(key, (None, None))[0], type=found.get(key, (None, None))[1],
              last_synced_at=synced.get(key))
        for key in (*listed, *others)
    ]


def kept_boards(conn: sqlite3.Connection | None, settings: Settings) -> list[str]:
    return [board.key for board in served_boards(conn, settings)
            if board.key in settings.jira_projects or board.synced]


def last_discovery(conn: sqlite3.Connection) -> str | None:
    """When the last discovery that worked finished (storage format), or None: never."""
    row = conn.execute(_LAST_DISCOVERY_SQL).fetchone()
    return row[0] if row and row[0] else None


# --- the automatic process mapping ---------------------------------------------------------------------------------


def category_stages(process: ProcessConfig) -> dict[str, str]:
    """Jira's status category keys and the stage each maps to on a board mapped automatically."""
    before = process.stages[: process.stages.index(process.start_stage)]
    stages = {"indeterminate": process.start_stage, "done": process.done_stage}
    return {"new": before[0], **stages} if before else stages


def automatic_project(
    conn: sqlite3.Connection | None, process: ProcessConfig, key: str, *, name: str | None = None
) -> ProjectProcess:
    """The board's mapping worked out from the snapshot (see the module docstring)."""
    stages = category_stages(process)
    mapping: dict[str, str] = {}
    points = estimates = 0
    if conn is not None:
        folded: set[str] = set()
        for row in conn.execute(_STATUSES_SQL, (key, key)).fetchall():
            status, stage = row[0], stages.get(row[1] or "")
            if not isinstance(status, str) or not status.strip() or stage is None or status.casefold() in folded:
                continue
            folded.add(status.casefold())
            mapping[status] = stage
        points, estimates = (int(value or 0) for value in conn.execute(_ESTIMATES_SQL, (key,)).fetchone())
    estimate = (
        EstimateConfig(basis="points", field="story_points", hours_per_point=AUTOMATIC_HOURS_PER_POINT)
        if points and points >= estimates
        else EstimateConfig(basis="hours", field="original_estimate")
    )
    project = ProjectProcess(
        name=_name(name),
        status_to_stage=mapping,
        estimate=estimate,
        evidence={stage: None for stage in process.work_stages()[1:]},
    )
    project._automatic = True
    return project


def mapped_process(
    process: ProcessConfig,
    conn: sqlite3.Connection | None,
    boards: Iterable[Board],
    *,
    automatic: Callable[[Board], ProjectProcess] | None = None,
) -> ProcessConfig:
    def work_out(board: Board) -> ProjectProcess:
        return automatic_project(conn if board.synced else None, process, board.key, name=board.name)

    extra: dict[str, ProjectProcess] = {}
    for board in boards:
        explicit = process.projects.get(board.key)
        if explicit is None:
            extra[board.key] = (automatic or work_out)(board)
        elif explicit.name is None and _name(board.name) is not None:
            extra[board.key] = explicit.model_copy(update={"name": _name(board.name)})
    return process.with_projects(extra)


def board_process(process: ProcessConfig, conn: sqlite3.Connection | None, settings: Settings) -> ProcessConfig:
    return mapped_process(process, conn, served_boards(conn, settings))


def _name(name: str | None) -> str | None:
    text = " ".join((name or "").split())[:_NAME_MAX].strip()
    return text or None


# --- discovery -----------------------------------------------------------------------------------------------------


@dataclass
class DiscoveryResult:
    run_id: str
    listed: int  # boards the search listed (with a usable key)
    added: int  # of them, new to the snapshot
    back: int  # of them, listed again after they had disappeared
    disappeared: int  # stored boards the search no longer lists
    skipped: int  # entries without a usable key
    requests: int
    empty: bool = False  # the search listed nothing: the stored list was left as it was
    keys: list[str] = field(default_factory=list)

    def render(self) -> str:
        if self.empty:
            return f"discover run {self.run_id}: Jira listed no board this account can browse; the stored list stays"
        lines = [
            f"discover run {self.run_id}: {self.listed:,} boards this account can browse",
            f"  new: {self.added:,}; back: {self.back:,}; disappeared from Jira: {self.disappeared:,}",
            f"  requests: {self.requests:,}",
        ]
        if self.skipped:
            lines.append(f"  skipped: {self.skipped:,} without a usable key")
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        return {"listed": self.listed, "added": self.added, "back": self.back, "disappeared": self.disappeared,
                "skipped": self.skipped, "requests": self.requests, "empty": self.empty}


def discover_boards(client: JiraClient, store: Store, *, now: Callable[[], datetime] = utcnow) -> DiscoveryResult:
    """Every board the account can browse, into the boards table (see the module docstring)."""
    started = now()
    run = store.start_run("discover", started_at=started)
    requests_before = client.request_count
    result = DiscoveryResult(run_id=run.run_id, listed=0, added=0, back=0, disappeared=0, skipped=0, requests=0)
    try:
        boards, result.skipped = _boards(client.project_search())
        result.requests = client.request_count - requests_before
        if not boards:
            result.empty = True
            log.warning("board_discovery_empty", skipped=result.skipped)
        else:
            _store(store, boards, run.started_at, result)
    except BaseException as exc:
        # BaseException: even Ctrl-C leaves a failed run, not one stuck at "running".
        result.requests = client.request_count - requests_before
        stats = {**result.stats(), "error_type": type(exc).__name__,
                 "status": getattr(exc, "status_code", None)}
        store.finish_run(run, status="failed", stats=stats, finished_at=now())
        log.error("board_discovery_failed", run_id=run.run_id, error_type=type(exc).__name__,
                  status=getattr(exc, "status_code", None))
        raise
    store.finish_run(run, status="succeeded", stats=result.stats(), finished_at=now())
    log.info("board_discovery_finished", run_id=run.run_id, **result.stats())
    return result


def _boards(projects: Sequence[Any]) -> tuple[dict[str, dict[str, Any]], int]:
    boards: dict[str, dict[str, Any]] = {}
    skipped = 0
    for project in projects:
        if not isinstance(project, dict) or project.get("archived") is True or project.get("deleted") is True:
            continue
        key = project.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            skipped += 1
            continue
        name = project.get("name")
        kind = project.get("projectTypeKey")
        project_id = project.get("id")
        boards[key] = {
            "project_key": key,
            "project_id": str(project_id) if isinstance(project_id, (str, int)) and not isinstance(project_id, bool)
            else None,
            "name": _name(name if isinstance(name, str) else None) or key,
            "project_type": kind if isinstance(kind, str) and kind.strip() else None,
        }
    return boards, skipped


def _store(store: Store, boards: dict[str, dict[str, Any]], seen_at: str, result: DiscoveryResult) -> None:
    with store.transaction():
        stored = {row["project_key"]: row["disappeared_at"]
                  for row in store.query("SELECT project_key, disappeared_at FROM boards")}
        for key, row in boards.items():
            if key not in stored:
                result.added += 1
            elif stored[key] is not None:
                result.back += 1
            store.conn.execute(
                """
                INSERT INTO boards (project_key, project_id, name, project_type, discovered_at, last_seen_at,
                                    disappeared_at)
                VALUES (:project_key, :project_id, :name, :project_type, :seen_at, :seen_at, NULL)
                ON CONFLICT (project_key) DO UPDATE SET
                  project_id = excluded.project_id, name = excluded.name, project_type = excluded.project_type,
                  last_seen_at = excluded.last_seen_at, disappeared_at = NULL
                """,
                {**row, "seen_at": seen_at},
            )
        gone = [key for key, disappeared in stored.items() if key not in boards and disappeared is None]
        for key in gone:
            store.conn.execute("UPDATE boards SET disappeared_at = ? WHERE project_key = ?", (seen_at, key))
    result.listed = len(boards)
    result.disappeared = len(gone)
    result.keys = sorted(boards)
