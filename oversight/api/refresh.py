"""Sync now"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from oversight.cli import EXIT_CONFIG, EXIT_GUARD, EXIT_JIRA_AUTH
from oversight.logs import get_logger
from oversight.settings import PROJECT_ROOT
from oversight.store.db import open_readonly
from oversight.timeutil import to_iso, utcnow

log = get_logger("api")

STEP_TIMEOUT_S = 15 * 60
# A board's first sync reads every issue (sync --full): as long as the scheduled cycle's full sync gets (cycle.py).
FULL_STEP_TIMEOUT_S = 60 * 60
COOLDOWN_S = 30
DISCOVER = ("discover",)
BASELINE = "baseline"
# Steps whose failure never fails the sync, each with the warning it is logged as.
OPTIONAL_STEPS = {DISCOVER[0]: "api_refresh_discovery_failed", BASELINE: "api_refresh_baseline_failed"}
# A failed sync's message (see the module docstring): what to do, after the dashboard's own "didn't finish".
TRY_AGAIN = "Try again in a few minutes."
LAST_GOOD = "The figures on screen are from the last sync that worked."
TOO_LONG = "It took too long."
# Exit codes (oversight/cli.py) where trying again cannot help: someone has to change the service's set-up.
ASK_ADMIN = {
    EXIT_CONFIG: "The service's settings need fixing. Ask your administrator.",
    EXIT_GUARD: "The service's settings need fixing. Ask your administrator.",
    EXIT_JIRA_AUTH: "Jira didn't accept the service's account. Ask your administrator.",
}

State = Literal["idle", "queued", "running", "succeeded", "failed"]
RecheckState = Literal["queued", "running", "succeeded", "failed"]
# Runs one step's argv; returns its exit code. Tests swap in a fake.
StepRunner = Callable[[Sequence[str]], int]


def board_steps(project: str, *, first: bool, discover: bool) -> list[tuple[str, ...]]:
    """One board's refresh, in order (see the module docstring)."""
    scope = ("--project", project)
    steps = [("sync", "--full" if first else "--incremental", *scope), ("derive", *scope), ("rules", *scope),
             (BASELINE, "--missing", *scope)]
    return [DISCOVER, *steps] if discover else steps


def run_step(argv: Sequence[str], *, db_path: Path | None = None) -> int:
    """One `python -m oversight ...` step in the project folder, with this interpreter."""
    env = None if db_path is None else {**os.environ, "DB_PATH": str(Path(db_path).resolve())}
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed argv from board_steps or recheck, never user input
            [sys.executable, "-m", "oversight", *argv],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=FULL_STEP_TIMEOUT_S if "--full" in argv else STEP_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        relay(exc.stderr)  # what it logged before it was stopped
        raise
    relay(completed.stderr)
    if completed.returncode != 0:
        log.warning("api_refresh_step_failed", step=argv[0], exit_code=completed.returncode)
    return completed.returncode


def relay(output: bytes | str | None) -> None:
    stream = sys.stderr
    if not output or stream is None:
        return
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
    for line in text.splitlines():
        if line.strip():
            stream.write(line + "\n")
    stream.flush()


def board_synced(db_path: Path, project: str) -> bool:
    if not Path(db_path).exists():
        return False
    try:
        conn = open_readonly(db_path)
        try:
            row = conn.execute("SELECT last_synced_at FROM sync_state WHERE scope = ?", (project,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("api_refresh_sync_state_unreadable", error_type=type(exc).__name__)
        return False
    return bool(row and row[0])


@dataclass
class BoardSync:
    """One board's 'Sync now' in this process"""

    project: str
    state: State = "idle"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None

    def body(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "state": self.state,
            "started_at": to_iso(self.started_at) if self.started_at else None,
            "finished_at": to_iso(self.finished_at) if self.finished_at else None,
            "message": self.message,
        }


@dataclass
class Recheck:

    project: str
    revision: int
    requested_at: datetime
    name: str | None = None
    state: RecheckState = "queued"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None

    @property
    def argv(self) -> tuple[str, ...]:
        return ("rules", "--project", self.project)


Job = BoardSync | Recheck


@dataclass
class Refresher:

    runner: StepRunner = run_step
    now: Callable[[], datetime] = utcnow
    cooldown: timedelta = timedelta(seconds=COOLDOWN_S)
    synced: Callable[[str], bool] = lambda _project: True
    discover: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _jobs: list[Job] = field(default_factory=list, repr=False)  # waiting, in the order asked for
    _working: bool = field(default=False, repr=False)  # a worker is running the jobs
    _syncs: dict[str, BoardSync] = field(default_factory=dict, repr=False)  # each board's latest refresh
    _rechecks: dict[str, Recheck] = field(default_factory=dict, repr=False)  # each board's latest re-check

    def status(self, project: str) -> dict[str, Any]:
        """The board's refresh as it stands: idle when none was asked for in this process."""
        with self._lock:
            return (self._syncs.get(project) or BoardSync(project)).body()

    def start(self, project: str, *, background: bool = True) -> dict[str, Any]:
        with self._lock:
            board = self._syncs.get(project)
            if board is not None and board.state in ("queued", "running"):
                return board.body()
            now = self.now()
            if board is not None and board.finished_at is not None and now - board.finished_at < self.cooldown:
                return board.body()
            queued = self._working
            board = BoardSync(project, state="queued" if queued else "running", started_at=None if queued else now)
            self._syncs[project] = board
            self._jobs.append(board)
            body = board.body()
            claimed = self._claim()
        log.info("api_refresh_started", project=project, queued=queued)
        if claimed:
            self._work(background)
        return body

    def recheck(self, project: str, revision: int, *, name: str | None = None, background: bool = True) -> Recheck:
        """Re-check one board's findings with its rule settings"""
        with self._lock:
            job = next((job for job in self._jobs if isinstance(job, Recheck) and job.project == project), None)
            if job is None:
                job = Recheck(project=project, revision=revision, requested_at=self.now(), name=name)
                self._jobs.append(job)
            else:
                job.revision = max(job.revision, revision)
                job.name = name or job.name
            self._rechecks[project] = job
            queued_behind = self._working
            claimed = self._claim()
            asked = replace(job)
        log.info("rule_settings_recheck_queued", project=project, revision=revision, behind_another_job=queued_behind)
        if claimed:
            self._work(background)
        return asked

    def recheck_of(self, project: str) -> Recheck | None:
        """A copy of the board's latest re-check in this process (queued, running or how it ended); None: none yet."""
        with self._lock:
            job = self._rechecks.get(project)
            return replace(job) if job is not None else None

    def wait(self, timeout: float | None = None) -> None:
        """For tests: wait for the running jobs (a refresh, the jobs queued with it) to end."""
        if self._thread is not None:
            self._thread.join(timeout)

    def _claim(self) -> bool:
        """Under the lock: whether this call starts the worker (none is running)."""
        if self._working:
            return False
        self._working = True
        return True

    def _work(self, background: bool) -> None:
        if background:
            self._thread = threading.Thread(target=self._drain, name="oversight-refresh", daemon=True)
            self._thread.start()
        else:
            self._drain()

    def _drain(self) -> None:
        """Run the jobs one at a time until none is left."""
        while True:
            with self._lock:
                if not self._jobs:
                    self._working = False
                    return
                job = self._jobs.pop(0)
                job.state = "running"
                job.started_at = job.started_at or self.now()
            try:
                if isinstance(job, Recheck):
                    self._recheck(job)
                else:
                    self._sync(job)
            except Exception:
                log.exception("api_refresh_job_crashed", job="recheck" if isinstance(job, Recheck) else "sync",
                              project=job.project)
                # Worked out before the lock is taken: whether the board synced before is read from the snapshot.
                message = (
                    _recheck_message(job.name or job.project, "it stopped unexpectedly")
                    if isinstance(job, Recheck) else sync_message(first=not self.synced(job.project))
                )
                with self._lock:
                    job.state, job.finished_at, job.message = "failed", self.now(), message

    def _sync(self, job: BoardSync) -> None:
        first = not self.synced(job.project)
        log.info("api_refresh_running", project=job.project, first=first)
        message: str | None = None
        failed: str | None = None
        for argv in board_steps(job.project, first=first, discover=self.discover):
            try:
                code = self.runner(argv)
                problem = sync_message(first=first, code=code) if code != 0 else None
            except subprocess.TimeoutExpired:
                problem = sync_message(first=first, timed_out=True)
            except OSError as exc:
                log.warning("api_refresh_step_error", step=argv[0], error_type=type(exc).__name__)
                problem = sync_message(first=first)
            if problem and argv[0] in OPTIONAL_STEPS:
                log.warning(OPTIONAL_STEPS[argv[0]], project=job.project)
                continue
            if problem:
                message, failed = problem, argv[0]
                break
        with self._lock:
            job.finished_at = self.now()
            job.state = "failed" if message else "succeeded"
            job.message = message
        log.info("api_refresh_finished", project=job.project, state=job.state, failed_step=failed)

    def _recheck(self, job: Recheck) -> None:
        log.info("rule_settings_recheck_started", project=job.project, revision=job.revision)
        problem: str | None = None
        try:
            if self.runner(job.argv) != 0:
                problem = "the rules run failed"
        except subprocess.TimeoutExpired:
            problem = "it took too long"
        except OSError as exc:
            log.warning("api_refresh_step_error", step="rules", error_type=type(exc).__name__)
            problem = "it could not start"
        with self._lock:
            job.finished_at = self.now()
            job.state = "failed" if problem else "succeeded"
            job.message = _recheck_message(job.name or job.project, problem) if problem else None
        log.info("rule_settings_recheck_finished", project=job.project, revision=job.revision, state=job.state)


def sync_message(*, first: bool, code: int | None = None, timed_out: bool = False) -> str:
    """A failed sync's message, one short line (see the module docstring)"""
    if code in ASK_ADMIN:
        return ASK_ADMIN[code] if first else f"{ASK_ADMIN[code]} {LAST_GOOD}"
    advice = TRY_AGAIN if first else LAST_GOOD
    return f"{TOO_LONG} {advice}" if timed_out else advice


def _recheck_message(board: str, problem: str) -> str:
    """board: the board's name as the dashboard shows it."""
    return (
        f"Couldn't re-check {board}'s findings with the new settings: {problem}. "
        "They will apply at the next sync."
    )
