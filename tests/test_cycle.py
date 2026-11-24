"""`python -m oversight cycle` (oversight/cycle.py)"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from oversight import cycle
from oversight.backup import backup_name, list_backups
from oversight.cli import EXIT_CONFIG, main
from oversight.cycle import (
    TIMED_OUT,
    StepRunner,
    backup_due,
    cycle_steps,
    run_cycle,
    run_loop,
    stop_on_signals,
)
from oversight.settings import PROJECT_ROOT, Settings
from oversight.store.db import Store

START = datetime(2026, 11, 12, 22, 0, tzinfo=UTC)


class World:

    def __init__(self, settings: Settings, *, cycles: int, step_minutes: float = 1,
                 codes: dict[tuple[str, ...], list[int]] | None = None) -> None:
        self.settings = settings
        self.at = START
        self.step_minutes = step_minutes
        self.codes = codes or {}
        self.ran: list[tuple[datetime, tuple[str, ...]]] = []
        self.waits: list[float] = []
        self.stop = _Stop(self, cycles)

    def now(self) -> datetime:
        return self.at

    def clock(self) -> float:
        return (self.at - START).total_seconds()

    def runner(self, argv: Sequence[str]) -> int:
        step = tuple(argv)
        self.ran.append((self.at, step))
        if step == ("backup",):
            self.settings.backups_path.mkdir(parents=True, exist_ok=True)
            (self.settings.backups_path / backup_name(self.at)).write_bytes(b"backup")
        self.at += timedelta(minutes=self.step_minutes)
        queued = self.codes.get(step)
        return queued.pop(0) if queued else 0

    def syncs(self) -> list[str]:
        return [step[1] for _, step in self.ran if step[0] == "sync"]


class _Stop(threading.Event):
    def __init__(self, world: World, cycles: int) -> None:
        super().__init__()
        self.world = world
        self.left = cycles

    def wait(self, timeout: float | None = None) -> bool:
        self.world.waits.append(float(timeout or 0))
        self.world.at += timedelta(seconds=timeout or 0)
        self.left -= 1
        if self.left <= 0:
            self.set()
        return self.is_set()


def loop(world: World) -> int:
    return run_loop(world.settings, world.runner, stop=world.stop, now=world.now, clock=world.clock)


def test_a_cycle_is_sync_derive_rules_then_the_missing_baselines() -> None:
    assert cycle_steps(full=False) == [("sync", "--incremental"), ("derive",), ("rules",), ("baseline", "--missing")]
    assert cycle_steps(full=True) == [("sync", "--full"), ("derive",), ("rules",), ("baseline", "--missing")]


def test_with_board_discovery_on_a_cycle_discovers_first() -> None:
    assert cycle_steps(full=False, discover=True) == [
        ("discover",), ("sync", "--incremental"), ("derive",), ("rules",), ("baseline", "--missing")]


def test_a_baseline_that_fails_never_fails_the_cycle() -> None:
    from structlog.testing import capture_logs

    ran: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str]) -> int:
        ran.append(tuple(argv))
        return EXIT_CONFIG if argv[0] == "baseline" else 0

    with capture_logs() as captured:
        assert run_cycle(runner, full=False) == 0
    assert ran == cycle_steps(full=False)
    events = [line["event"] for line in captured]
    assert "cycle_baseline_failed" in events and "cycle_failed" not in events and "cycle_finished" in events


def test_a_discovery_that_fails_never_stops_the_cycle() -> None:
    from structlog.testing import capture_logs

    ran: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str]) -> int:
        ran.append(tuple(argv))
        return 4 if argv[0] == "discover" else 0

    with capture_logs() as captured:
        assert run_cycle(runner, full=False, discover=True) == 0
    assert ran == cycle_steps(full=False, discover=True)
    events = [line["event"] for line in captured]
    assert "cycle_discovery_failed" in events and "cycle_failed" not in events and "cycle_finished" in events


def test_with_board_discovery_off_the_loop_never_discovers(settings: Settings) -> None:
    world = World(settings.model_copy(update={"board_discovery": "off"}), cycles=2)
    loop(world)
    assert [step[0] for _, step in world.ran] == ["sync", "derive", "rules", "baseline"] * 2


def test_a_cycle_stops_at_the_first_failed_step_with_its_code() -> None:
    ran: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str]) -> int:
        ran.append(tuple(argv))
        return 4 if argv[0] == "sync" else 0

    assert run_cycle(runner, full=False) == 4
    assert ran == [("sync", "--incremental")]


def test_a_step_a_stop_interrupts_is_logged_as_stopped_never_as_failed() -> None:
    from structlog.testing import capture_logs

    stop = threading.Event()

    def interrupted(argv: Sequence[str]) -> int:
        stop.set()
        return 130  # the step's exit on Ctrl-C

    with capture_logs() as captured:
        assert run_cycle(interrupted, full=True, stop=stop) == 0
        assert run_cycle(lambda argv: 4, full=False) == 4  # a real failure is still one
    events = [(line["event"], line.get("log_level")) for line in captured]
    assert ("cycle_stopped", "info") in events
    assert [event for event in events if event[0] == "cycle_failed"] == [("cycle_failed", "warning")]
    stopped = next(line for line in captured if line["event"] == "cycle_stopped")
    assert (stopped["during"], stopped["exit_code"]) == ("sync", 130)


def test_a_stop_between_steps_ends_the_cycle_without_failing() -> None:
    stop = threading.Event()
    ran: list[str] = []

    def runner(argv: Sequence[str]) -> int:
        ran.append(argv[0])
        stop.set()
        return 0

    assert run_cycle(runner, full=False, stop=stop) == 0
    assert ran == ["sync"]


def test_the_first_cycle_is_full_then_incremental_until_the_next_utc_day(settings: Settings) -> None:
    world = World(settings, cycles=4)
    assert loop(world) == 0
    assert world.syncs() == ["--full", "--incremental", "--full", "--incremental"]
    starts = [at for at, step in world.ran if step[0] == "sync"]
    assert starts == [START + timedelta(hours=hours, minutes=1) for hours in range(4)]  # after each cycle's discovery


def test_cycles_start_an_interval_apart_and_an_overrun_is_followed_at_once(settings: Settings) -> None:
    world = World(settings.model_copy(update={"sync_interval_minutes": 30}), cycles=2, step_minutes=4)
    loop(world)
    assert world.waits[0] == (30 - 20) * 60  # five steps of 4 minutes: discover, sync, derive, rules, baseline
    slow = World(settings.model_copy(update={"sync_interval_minutes": 5}), cycles=2, step_minutes=4)
    loop(slow)
    assert slow.waits == [0.0, 0.0]


def test_a_failed_full_sync_is_tried_again_in_full(settings: Settings) -> None:
    world = World(settings, cycles=4, codes={("sync", "--full"): [4]})
    loop(world)
    assert world.syncs() == ["--full", "--full", "--full", "--incremental"]


def test_a_failed_step_never_stops_the_loop(settings: Settings) -> None:
    world = World(settings, cycles=3, codes={("derive",): [1, 1, 1]})
    loop(world)
    assert [step[0] for _, step in world.ran] == ["discover", "sync", "derive"] * 3


def test_a_backup_follows_the_first_cycle_and_then_the_first_cycle_of_each_utc_day(settings: Settings) -> None:
    Store.open(settings.db_file).close()
    world = World(settings, cycles=26)
    loop(world)
    backups = [at for at, step in world.ran if step == ("backup",)]
    assert backups == [START + timedelta(minutes=5), START + timedelta(hours=2, minutes=5)]
    assert len(list_backups(settings.backups_path)) == 2


class VariedWorld(World):
    """A day of real cycles"""

    def __init__(self, settings: Settings, *, cycles: int) -> None:
        super().__init__(settings, cycles=cycles)
        self.rng = __import__("random").Random(20261112)
        self.doctor_warned: list[datetime] = []

    def doctor(self, moment: datetime) -> None:
        from oversight.production import _backups

        if list_backups(self.settings.backups_path) and not _backups(self.settings, moment).ok:
            self.doctor_warned.append(moment)

    def runner(self, argv: Sequence[str]) -> int:
        if tuple(argv) == ("backup",):
            self.doctor(self.at)  # the newest backup's oldest moment: just before the next is written
        self.step_minutes = (self.rng.uniform(2.7, 4) if argv[:2] == ("sync", "--full")
                             else self.rng.uniform(0.3, 1.3) if argv[0] != "backup" else 0.02)
        return super().runner(argv)


@pytest.mark.parametrize("interval", [60, 240, 1440])
def test_over_sixty_days_one_backup_a_utc_day_and_the_doctor_never_warns(settings: Settings, interval: int) -> None:
    configured = settings.model_copy(update={"sync_interval_minutes": interval})
    Store.open(configured.db_file).close()
    days = 60
    world = VariedWorld(configured, cycles=days * 24 * 60 // interval)
    wait = world.stop.wait

    def sampled_wait(timeout: float | None = None) -> bool:  # the doctor, every hour of the wait too
        start = world.at
        for minute in range(0, int((timeout or 0) // 60) + 1, 60):
            world.doctor(start + timedelta(minutes=minute))
        return wait(timeout)

    world.stop.wait = sampled_wait  # type: ignore[method-assign]
    loop(world)
    backups = [at for at, step in world.ran if step == ("backup",)]
    by_day = [at.date() for at in backups]
    assert by_day == sorted(set(by_day))  # never two on one UTC day
    assert len(set(by_day)) == len({at.date() for at, _ in world.ran})  # every UTC day the loop ran has one
    gaps = [later - earlier for earlier, later in zip(backups, backups[1:])]
    assert max(gaps) <= timedelta(days=1, minutes=interval + 20)
    assert world.doctor_warned == []


def test_no_backup_without_a_snapshot(settings: Settings) -> None:
    world = World(settings, cycles=2)
    loop(world)
    assert ("backup",) not in [step for _, step in world.ran]


def test_a_backup_is_due_once_each_utc_day(settings: Settings) -> None:
    assert not backup_due(settings, START)  # no snapshot
    Store.open(settings.db_file).close()
    assert backup_due(settings, START)
    settings.backups_path.mkdir(parents=True)
    (settings.backups_path / backup_name(START)).write_bytes(b"backup")
    assert not backup_due(settings, START + timedelta(hours=1, minutes=59))  # 23:59, the same UTC day
    assert backup_due(settings, START + timedelta(hours=2))


def test_a_stop_during_a_cycle_ends_the_loop_with_no_backup(settings: Settings) -> None:
    Store.open(settings.db_file).close()
    world = World(settings, cycles=99)

    def runner(argv: Sequence[str]) -> int:
        code = world.runner(argv)
        world.stop.set()
        return code

    assert run_loop(settings, runner, stop=world.stop, now=world.now, clock=world.clock) == 0
    assert [step for _, step in world.ran] == [("discover",)]


class FakeChild:
    made: list[FakeChild] = []

    def __init__(self, args: list[str], **options: Any) -> None:
        self.args = args
        self.options = options
        self.signals: list[str] = []
        self.returncode: int | None = None
        self.hang = "hang" in args
        FakeChild.made.append(self)

    def wait(self, timeout: float | None = None) -> int:
        if self.hang and self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signal.Signals(signum).name)
        self.returncode = 130

    def terminate(self) -> None:
        self.signals.append("terminate")
        self.returncode = 1

    def kill(self) -> None:
        self.signals.append("kill")
        self.returncode = -9


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[FakeChild]:
    FakeChild.made = []
    monkeypatch.setattr(cycle.subprocess, "Popen", FakeChild)
    return FakeChild.made


def test_each_step_is_its_own_oversight_process_in_the_project_folder(children: list[FakeChild]) -> None:
    assert StepRunner()(("sync", "--incremental")) == 0
    (child,) = children
    assert child.args == [sys.executable, "-m", "oversight", "sync", "--incremental"]
    assert child.options == {"cwd": PROJECT_ROOT, "stdout": subprocess.DEVNULL}


def test_a_real_step_is_its_own_process_with_its_exit_code() -> None:
    runner = StepRunner(timeout_s=60)
    assert runner(("--version",)) == 0
    assert runner(("no-such-command",)) == 2


def test_a_step_past_its_time_is_interrupted_like_ctrl_c(children: list[FakeChild]) -> None:
    assert StepRunner(timeout_s=1, grace_s=1)(("sync", "hang")) == TIMED_OUT
    expected = "terminate" if sys.platform == "win32" else "SIGINT"
    assert children[0].signals == [expected]


def test_an_interrupt_stops_the_running_step_and_starts_no_other(children: list[FakeChild]) -> None:
    runner = StepRunner()
    running = FakeChild(["sync"])
    runner._child = running  # a step in progress
    runner.interrupt()
    assert running.signals == ["terminate" if sys.platform == "win32" else "SIGINT"]
    assert runner(("derive",)) == 0
    assert children == [running]  # nothing new was started


def test_sigterm_and_sigint_stop_the_loop(children: list[FakeChild]) -> None:
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    stop = threading.Event()
    runner = StepRunner()
    try:
        stop_on_signals(stop, runner)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and signal.getsignal(signal.SIGINT) is handler
        handler(signal.SIGTERM, None)
    finally:
        for signum, previous in saved.items():
            signal.signal(signum, previous)
    assert stop.is_set()
    assert runner(("sync", "--incremental")) == 0 and children == []


def test_health_says_a_sync_stopped_part_way_was_stopped_not_that_it_failed(tmp_path: Any) -> None:
    from fastapi.testclient import TestClient

    from oversight.api.app import create_app
    from oversight.api.routes.health import SYNC_FAILED, SYNC_STOPPED
    from oversight.demo import seed_demo
    from oversight.llm.scripted import ScriptedProvider
    from tests.test_api_overview import KEY, make_settings

    settings = make_settings(tmp_path)
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=START, seed=20261104, project="DEMO", issues=40)
    client = TestClient(create_app(settings, provider=ScriptedProvider()), headers={"X-Service-Key": KEY})
    assert client.get("/api/v1/health").json()["message"] is None

    for hours, error_type, said in ((1, "KeyboardInterrupt", SYNC_STOPPED), (2, "JiraError", SYNC_FAILED)):
        with Store.open(settings.db_file) as store:
            run = store.start_run("sync", started_at=START + timedelta(hours=hours))
            store.finish_run(run, status="failed", stats={"error_type": error_type}, finished_at=START + timedelta(hours=30))
        body = client.get("/api/v1/health").json()
        assert (body["status"], body["message"]) == ("degraded", said)


STOP_WITH_THE_LOCK_HELD = """
import signal, threading
from oversight.cycle import StepRunner, stop_on_signals
stop = threading.Event()
runner = StepRunner()
stop_on_signals(stop, runner)
with runner._lock:  # as the main thread holds it around Popen and in its finally
    signal.raise_signal(signal.SIGTERM)
print("returned", stop.is_set(), runner._stopping)
"""


def test_a_stop_signal_never_waits_on_the_runners_lock() -> None:
    done = subprocess.run([sys.executable, "-c", STOP_WITH_THE_LOCK_HELD], cwd=PROJECT_ROOT, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "returned True True"


def test_a_stop_while_a_step_starts_still_interrupts_it_once(children: list[FakeChild]) -> None:
    runner = StepRunner()

    class StoppedAsItStarts(FakeChild):
        def __init__(self, args: list[str], **options: Any) -> None:
            super().__init__(args, **options)
            runner.interrupt()  # the signal lands while Popen runs: no step to interrupt yet

    cycle.subprocess.Popen = StoppedAsItStarts  # type: ignore[assignment,misc]
    assert runner(("sync", "--incremental")) == (1 if sys.platform == "win32" else 130)
    (child,) = children
    assert child.signals == ["terminate" if sys.platform == "win32" else "SIGINT"]  # once, not twice


def test_the_command_needs_jira_settings_before_any_step(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cycle, "StepRunner", lambda: pytest.fail("no step may run"))
    code = main(["cycle", "--loop"], settings=settings.model_copy(update={"jira_email": ""}))
    assert code == EXIT_CONFIG
    assert "JIRA_EMAIL" in capsys.readouterr().err


def test_the_command_runs_one_cycle_and_returns_its_code(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[tuple[str, ...]] = []

    def fake_runner() -> Any:
        def run(argv: Sequence[str]) -> int:
            ran.append(tuple(argv))
            return 3 if argv[0] == "derive" else 0

        return run

    monkeypatch.setattr(cycle, "StepRunner", fake_runner)
    assert main(["cycle", "--full"], settings=settings) == 3
    assert ran == [("discover",), ("sync", "--full"), ("derive",)]


def test_the_loop_option_starts_the_service(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Settings] = []
    monkeypatch.setattr(cycle, "loop_forever", lambda given, now: started.append(given) or 0)
    assert main(["cycle", "--loop"], settings=settings) == 0
    assert started == [settings]


def test_full_and_loop_do_not_mix() -> None:
    with pytest.raises(SystemExit):
        main(["cycle", "--full", "--loop"])
