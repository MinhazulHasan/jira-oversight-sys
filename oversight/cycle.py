"""`python -m oversight cycle`: one oversight cycle, or with --loop one every SYNC_INTERVAL_MINUTES. The loop is the
deployment's sync service (deploy/docker-compose.yml, docs/DEPLOYMENT.md).

A cycle is `discover` (with BOARD_DISCOVERY on, JO-17), then `sync --incremental`, `derive`, `rules` and `baseline
--missing`, each its own `python -m oversight` process, stopping at the first sync, derive or rules step that fails (the
exit code is that step's). Without --project, sync, derive and rules cover the kept boards: JIRA_PROJECTS and every
other board synced at least once (oversight/boards.py), so a board first synced from the dashboard ("Sync now") stays
fresh; `baseline --missing` writes the baseline report of each of them that has synced and has none yet, so every
synced board's Baseline page has one (18 Nov 2026). A discovery that fails is logged (cycle_discovery_failed) and the
cycle goes on: the boards stored before stay, and the sync never depends on it; a baseline step that fails is logged
(cycle_baseline_failed) and the cycle has still succeeded: the board's figures are in, and the next cycle tries again.
`--full` reads every issue instead (`sync --full`). Jira is only read, as everywhere.

The loop adds what a hosted deployment needs and the Windows tasks do separately:
- The first cycle of each UTC day, and the first after the service starts, syncs in full: only a full sync notices
  issues deleted or moved in Jira. A full sync that fails is tried again at the next cycle.
- After each cycle, `backup` when there is a snapshot and no backup was taken yet on the current UTC day
  (oversight/backup.py; BACKUP_KEEP of them are kept): once a UTC day, right after the day's first cycle, so it
  never drifts later day by day and never skips a day; a host's copy of it can run at a fixed time.
- Cycles start SYNC_INTERVAL_MINUTES apart, start to start; one that overruns is followed at once. A failed step is
  logged and the loop carries on: a Jira outage never stops the service, and GET /health reports the failed sync.
- SIGTERM or SIGINT (`docker stop`) interrupts the running step as Ctrl-C would (the sync then records a run that
  stopped part way, never one stuck at running, and GET /health says it was stopped, not that it failed), waits for
  it, and ends the loop with exit 0. A step interrupted by a stop is logged as cycle_stopped, never cycle_failed: a
  restart is not a failure. The signal handler never waits on the step runner's lock (the main thread may hold it
  when the signal arrives). A step that runs past STEP_TIMEOUT_S is interrupted the same way, and that is a failure.
Each step's JSON log lines go straight to this process's stderr; its report (stdout) is dropped, as "Sync now" drops
it. The loop's own lines carry step names, exit codes and seconds only.

Run one loop per snapshot: two would sync the same boards twice over.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from types import FrameType
from typing import Any

from oversight.backup import newest_backup
from oversight.logs import get_logger
from oversight.settings import PROJECT_ROOT, Settings
from oversight.timeutil import UTC, utcnow

# A full sync of several boards takes longer than "Sync now"'s incremental one (oversight/api/refresh.py).
STEP_TIMEOUT_S = 60 * 60
# How long an interrupted step gets to record its run before it is killed.
INTERRUPT_GRACE_S = 30
# The exit code of a step that ran out of time (as GNU timeout reports it).
TIMED_OUT = 124

# Runs one `python -m oversight <argv>` step and returns its exit code. Tests swap in a fake.
Runner = Callable[[Sequence[str]], int]


# Board discovery (JO-17): the cycle's first step when BOARD_DISCOVERY is on. Its failure never fails the cycle.
DISCOVER: tuple[str, ...] = ("discover",)
# The baseline report of each kept board that has none yet: the cycle's last step. Its failure never fails the cycle.
BASELINE: tuple[str, ...] = ("baseline", "--missing")
# The steps whose failure is logged (with this event) and never fails the cycle.
_OPTIONAL = {DISCOVER: "cycle_discovery_failed", BASELINE: "cycle_baseline_failed"}


def cycle_steps(*, full: bool, discover: bool = False) -> list[tuple[str, ...]]:
    steps = [("sync", "--full" if full else "--incremental"), ("derive",), ("rules",), BASELINE]
    return [DISCOVER, *steps] if discover else steps


def run_cycle(runner: Runner, *, full: bool, discover: bool = False, stop: threading.Event | None = None,
              clock: Callable[[], float] = time.monotonic) -> int:
    """The cycle's steps in order; the first non-zero exit code of sync, derive or rules ends it and is returned (0
    when all succeed); discovery's and the baseline's are logged and the cycle goes on. A stop asked for between steps
    ends it too, returning 0: nothing failed."""
    log = get_logger("cycle")
    started = clock()
    log.info("cycle_started", full=full)
    for argv in cycle_steps(full=full, discover=discover):
        if stop is not None and stop.is_set():
            log.info("cycle_stopped", before=argv[0])
            return 0
        step_started = clock()
        code = runner(argv)
        log.info("cycle_step_finished", step=argv[0], exit_code=code, seconds=round(clock() - step_started, 1))
        if code != 0 and stop is not None and stop.is_set():
            # Interrupted by a stop (docker stop, a restart): nothing failed.
            log.info("cycle_stopped", during=argv[0], exit_code=code, seconds=round(clock() - started, 1))
            return 0
        if code != 0 and tuple(argv) in _OPTIONAL:
            # Discovery: the boards stored before stay, and the boards the cycle keeps never depend on this one search.
            # The baseline: the boards' figures are in; the next cycle writes the reports still missing.
            log.warning(_OPTIONAL[tuple(argv)], exit_code=code)
            continue
        if code != 0:
            log.warning("cycle_failed", step=argv[0], exit_code=code, seconds=round(clock() - started, 1))
            return code
    log.info("cycle_finished", full=full, seconds=round(clock() - started, 1))
    return 0


def backup_due(settings: Settings, now: datetime) -> bool:
    """A snapshot exists and no backup was taken on `now`'s UTC day (backups are named by their UTC time): once a
    calendar day, so the time a backup lands never creeps later from day to day, and no day goes without one."""
    if not settings.db_file.exists():
        return False
    newest = newest_backup(settings.backups_path)
    return newest is None or newest.taken_at.astimezone(UTC).date() < now.astimezone(UTC).date()


def run_loop(
    settings: Settings,
    runner: Runner,
    *,
    stop: threading.Event,
    now: Callable[[], datetime] = utcnow,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Cycles every SYNC_INTERVAL_MINUTES until `stop` is set; see the module docstring. Returns 0."""
    log = get_logger("cycle")
    interval_s = settings.sync_interval_minutes * 60
    log.info("cycle_loop_started", interval_minutes=settings.sync_interval_minutes)
    full_on: date | None = None  # the UTC day of the last full sync that succeeded
    while not stop.is_set():
        started = clock()
        today = now().date()
        full = full_on != today
        if (run_cycle(runner, full=full, discover=settings.discovers_boards, stop=stop, clock=clock) == 0 and full
                and not stop.is_set()):
            full_on = today
        if not stop.is_set() and backup_due(settings, now()):
            code = runner(("backup",))
            (log.info if code == 0 else log.warning)("cycle_backup_finished", exit_code=code)
        wait_s = max(0.0, interval_s - (clock() - started))
        if stop.wait(wait_s):
            break
    log.info("cycle_loop_stopped")
    return 0


class StepRunner:
    """Runs each step as `python -m oversight <argv>` in the project folder, one at a time, and can interrupt the
    one running (Ctrl-C's signal, so it records a failed run before it ends)."""

    def __init__(self, *, timeout_s: float = STEP_TIMEOUT_S, grace_s: float = INTERRUPT_GRACE_S) -> None:
        self.timeout_s = timeout_s
        self.grace_s = grace_s
        # Re-entrant: the stop signal's handler runs on the main thread, which may be holding it already (around
        # Popen, or in the finally below); a plain lock would leave the handler waiting on itself for good.
        self._lock = threading.RLock()
        self._child: subprocess.Popen[bytes] | None = None
        self._signalled: subprocess.Popen[bytes] | None = None
        self._stopping = False

    def __call__(self, argv: Sequence[str]) -> int:
        with self._lock:
            if self._stopping:
                return 0
            # A fixed argv from cycle_steps or "backup", never user input; stderr (the JSON log) is inherited.
            child = subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "oversight", *argv],
                cwd=PROJECT_ROOT,
                stdout=subprocess.DEVNULL,
            )
            self._child = child
            # A stop that arrived while the step was starting found no step to interrupt: interrupt it now.
            stopping = self._stopping
        if stopping:
            self._interrupt(child)
        try:
            try:
                return child.wait(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                get_logger("cycle").warning("cycle_step_timed_out", step=argv[0], timeout_s=self.timeout_s)
                self._interrupt(child)
                self._reap(child)
                return TIMED_OUT
        finally:
            with self._lock:
                self._child = None

    def interrupt(self) -> None:
        """No new step starts, and the running one (if any) is interrupted."""
        with self._lock:
            self._stopping = True
            child = self._child
        if child is not None:
            self._interrupt(child)

    def _interrupt(self, child: subprocess.Popen[bytes]) -> None:
        # Once a step: a second Ctrl-C would cut short the run it is recording.
        if child.poll() is not None or child is self._signalled:
            return
        self._signalled = child
        try:
            if sys.platform == "win32":  # no SIGINT for a child on Windows
                child.terminate()
            else:
                child.send_signal(signal.SIGINT)
        except OSError:
            pass

    def _reap(self, child: subprocess.Popen[bytes]) -> None:
        try:
            child.wait(timeout=self.grace_s)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def stop_on_signals(stop: threading.Event, runner: StepRunner) -> None:
    """SIGTERM (docker stop) and SIGINT end the loop: the running step is interrupted and no other starts."""

    def handle(signum: int, _frame: FrameType | None) -> None:
        get_logger("cycle").info("cycle_stop_requested", signal=signal.Signals(signum).name)
        stop.set()
        runner.interrupt()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, handle)


def loop_forever(settings: Settings, *, now: Callable[[], datetime] = utcnow, **options: Any) -> int:
    """The sync service: real steps, stopped by SIGTERM or SIGINT."""
    stop = threading.Event()
    runner = StepRunner(**options)
    stop_on_signals(stop, runner)
    return run_loop(settings, runner, stop=stop, now=now)
