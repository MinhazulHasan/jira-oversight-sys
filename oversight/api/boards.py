from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from oversight.boards import Board, automatic_project, discover_boards, mapped_process, served_boards
from oversight.config import ProcessConfig, ProjectProcess
from oversight.logs import get_logger
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import parse_ts, utcnow

log = get_logger("api")

DISCOVERY_MAX_AGE = timedelta(minutes=30)
DISCOVERY_RETRY_AFTER = timedelta(minutes=5)


@dataclass(frozen=True)
class Served:
    """The boards one request sees, in the service's order, and the process with every one of them mapped."""

    boards: tuple[Board, ...]
    process: ProcessConfig

    @property
    def keys(self) -> list[str]:
        return [board.key for board in self.boards]

    def board(self, key: str) -> Board | None:
        return next((board for board in self.boards if board.key == key), None)


@dataclass
class BoardIndex:
    """What every request reads its boards through (see the module docstring)."""

    settings: Settings
    process: ProcessConfig  # process.yaml as loaded at start
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # key -> ((Jira's name, last sync's start), the mapping worked out then)
    _automatic: dict[str, tuple[tuple[str | None, str | None], ProjectProcess]] = field(default_factory=dict,
                                                                                         repr=False)

    def read(self, db_path: Path, conn: sqlite3.Connection | None = None) -> Served:
        if conn is not None:
            return self._read(conn)
        if not Path(db_path).exists():
            return self._read(None)
        try:
            own = open_readonly(db_path)
        except sqlite3.Error as exc:
            log.warning("api_boards_unreadable", error_type=type(exc).__name__)
            return self._read(None)
        try:
            return self._read(own)
        except sqlite3.Error as exc:
            log.warning("api_boards_unreadable", error_type=type(exc).__name__)
            return self._read(None)
        finally:
            own.close()

    def _read(self, conn: sqlite3.Connection | None) -> Served:
        boards = tuple(served_boards(conn, self.settings))
        process = mapped_process(self.process, conn, boards, automatic=lambda board: self._mapping(conn, board))
        return Served(boards=boards, process=process)

    def _mapping(self, conn: sqlite3.Connection | None, board: Board) -> ProjectProcess:
        """The board's automatic mapping: the one kept since its last sync, else worked out now and kept."""
        version = (board.name, board.last_synced_at)
        with self._lock:
            kept = self._automatic.get(board.key)
        if kept is not None and kept[0] == version:
            return kept[1]
        found = automatic_project(conn if board.synced else None, self.process, board.key, name=board.name)
        with self._lock:
            self._automatic[board.key] = (version, found)
        return found


class Discoverer:
    """Board discovery in the API's process, in the background (see the module docstring)."""

    def __init__(
        self,
        discover: Callable[[], object] | None,
        *,
        now: Callable[[], datetime] = utcnow,
        retry_after: timedelta = DISCOVERY_RETRY_AFTER,
    ) -> None:
        self._discover = discover
        self._now = now
        self._retry_after = retry_after
        self._lock = threading.Lock()
        self._running = False
        self._started: datetime | None = None
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._discover is not None

    def start(self) -> bool:
        """Start a discovery in the background unless one is running or one started here within retry_after"""
        if self._discover is None:
            return False
        with self._lock:
            now = self._now()
            if self._running or (self._started is not None and now - self._started < self._retry_after):
                return False
            self._running, self._started = True, now
        self._thread = threading.Thread(target=self._run, name="oversight-discovery", daemon=True)
        self._thread.start()
        return True

    def refresh_if_stale(self, last: str | None) -> bool:
        if self._discover is None:
            return False
        try:
            finished = parse_ts(last)
        except (TypeError, ValueError):
            finished = None
        if finished is not None and self._now() - finished <= DISCOVERY_MAX_AGE:
            return False
        return self.start()

    def wait(self, timeout: float | None = None) -> None:
        """For tests: wait for the discovery running in the background to end."""
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            self._discover()  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - logged; the last list stays and the API carries on
            log.warning("board_discovery_failed", error_type=type(exc).__name__,
                        status=getattr(exc, "status_code", None))
        finally:
            with self._lock:
                self._running = False


def discovery_for(settings: Settings, db_path: Path, now: Callable[[], datetime]) -> Callable[[], object] | None:
    """Discovery into the API's snapshot with this process's settings, or None"""
    if not settings.discovers_boards or not settings.jira_configured:
        return None

    def discover() -> object:
        from oversight.jira.client import JiraClient

        with Store.open(db_path) as store, JiraClient.from_settings(settings) as client:
            return discover_boards(client, store, now=now)

    return discover
