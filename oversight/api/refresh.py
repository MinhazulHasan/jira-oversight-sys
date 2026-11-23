"""Sync now: the dashboard's Sync button syncs the board it is on (JO-18); and the re-check of one board after an admin
saves its rule settings (JO-15).

One board's refresh runs, in order: `discover` (with BOARD_DISCOVERY on: every board the Jira account can browse,
JO-17), then `sync --incremental --project KEY` (`sync --full --project KEY` when the board has never synced: its first
sync reads every issue), `derive --project KEY`, `rules --project KEY` and `baseline --missing --project KEY` (the
board's baseline report when it has none yet, so a board's first sync fills its Baseline page too: 18 Nov 2026). Jira
is only read. Each step runs as its own `python -m oversight` process (the CLI the scheduled cycle uses), one after
another, on a background thread, so the request that starts it answers at once and the API keeps serving while it
runs. A discovery that fails is logged (api_refresh_discovery_failed) and the sync goes on: the board was served when
it was asked for; a baseline that fails is logged (api_refresh_baseline_failed) and the sync has still succeeded: the
next cycle writes it. Whether a board has synced is read from the snapshot when its job starts, so a board the
scheduled cycle synced meanwhile is synced incrementally.

One job runs at a time in this API process, on one worker thread, in the order asked for: a board's refresh asked for
while another job runs (another board's refresh, or a re-check) is queued behind it, in its own state "queued", never
refused and never run beside it. A board's own repeat press while its refresh is queued or running reports that state,
and a new one starts no sooner than COOLDOWN_S after that board's last one finished; the cooldown is the board's own,
so another board can be synced at once. The state is kept in memory, a board at a time; it is about this API process
only, and the snapshot (sync_state) stays the record of what synced. Its body (GET and POST /refresh): {project,
state, started_at, finished_at, message}: project the board it is about, state idle | queued | running | succeeded |
failed, started_at when it started (null while queued, or before any), finished_at when it ended, and message, for a
failed one, what to do in one short line (null otherwise). The dashboard leads with its own words ("The last sync
didn't finish.", "<Board> didn't sync."), so the message never says the sync failed again (18 Nov 2026): "Try again in
a few minutes." for a first sync, "The figures on screen are from the last sync that worked." after one; and where
trying again cannot help, because a step stopped on the service's settings (exit 2, or 5: the read-only guard) or on
Jira refusing its account (exit 3), it says to ask an administrator instead (sync_message). Which step failed, and how,
goes to the log.

A re-check (recheck) is the rules step for one board alone, `rules --project KEY`, with no Jira call, run the same way,
in the same queue: a re-check asked for while a sync (or another re-check) runs waits for it (the sync's own rules step
may have applied the new settings already: running them again changes nothing), and a sync asked for during a re-check
waits for it the same way, so a re-check never overlaps a sync in this process. A board asked for again while its
re-check is still queued keeps the one queued, which reads the settings when it runs. Each board's latest re-check is
kept apart (recheck_of), for GET /rules/settings' apply (oversight/api/rule_settings.py). A re-check that fails or runs
out of time is said so in plain words, naming the board as the dashboard does (its name, AILED, never its key AIL);
the next sync's rules step applies the settings anyway, and so does the scheduled cycle, another process, which reads
them from the snapshot.

Each step's own log (its JSON lines on stderr, in the format the settings give both processes) goes on into this
process's log as it was written, whether the step succeeds, fails or runs out of time: a picture the sync could not
fetch, say, is a warning there with the person id and the reason, as it is in the scheduled cycle's log (run_cycle.ps1
keeps the same lines).
"""
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
    """One `python -m oversight ...` step in the project folder, with this interpreter. With db_path (the API's own
    snapshot), the step is told to use it (DB_PATH), so it changes the snapshot the API serves and no other. Its log
    goes on into this process's log (relay), never to the browser. A full sync gets FULL_STEP_TIMEOUT_S, any other
    step STEP_TIMEOUT_S."""
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
    """A step's log lines, as it wrote them, into this process's log (its stderr), line by line, empty lines left
    out. Nothing when this process has no stderr to write to."""
    stream = sys.stderr
    if not output or stream is None:
        return
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
    for line in text.splitlines():
        if line.strip():
            stream.write(line + "\n")
    stream.flush()


def board_synced(db_path: Path, project: str) -> bool:
    """Whether the board has synced successfully at least once (sync_state), read from the snapshot read-only. False
    without a snapshot, or one that cannot be read: its first sync then reads every issue, which is always safe."""
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
    """One board's "Sync now" in this process: queued, running, or how its latest ended. Times are this process's
    clock."""

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
    """One board's re-check after its rule settings were saved (JO-15): the rules step for that board alone, at the
    settings revision it was asked for (or a later one: it reads them when it runs). Times are this process's clock.
    name: the board's name, as its message says it (the key when none is given)."""

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
    """Each board's refresh in this API process (queued, running or how its latest ended), and each board's latest
    re-check. One worker thread runs the jobs, one at a time, in the order asked for. `synced` says whether a board has
    synced before (its first sync is a full one); `discover` runs discovery first in each refresh."""

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
        """Refresh the board unless its refresh is queued or running, or its last one finished within the cooldown;
        answer its state either way. Running at once when no other job is; else queued behind them."""
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
        """Re-check one board's findings with its rule settings: at once, or queued behind the sync or re-check that
        is running. A board already queued keeps its place. Returns a copy of the board's re-check as it stands.
        name: the board's name, for its message."""
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
        """Run the jobs one at a time until none is left. A job that fails in a way no step reports (a bug) is logged
        and ended as failed; the next one runs all the same."""
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
                # Discovery: the board was served when it was asked for, and its sync never depends on this one search.
                # The baseline: the board's figures are in; the next cycle writes its report.
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
    """A failed sync's message, one short line (see the module docstring): ask an administrator where a step stopped
    on the service's settings or on Jira refusing its account (`code`, the step's exit code); otherwise try again (a
    first sync) or the figures on screen are the last good ones, after "It took too long." for a step out of time."""
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
