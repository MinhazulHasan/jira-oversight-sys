"""Sync now (JO-18): POST /refresh?project=KEY syncs that board alone (discover, sync, derive and rules for it, then
its baseline report when it has none), one job at a time, another board's request queued behind it; GET
/refresh?project=KEY reports that board. And the re-check of one board after its rule settings are saved (JO-15),
which shares the one worker, so it never runs beside a sync.

A fake step runner stands in for the `python -m oversight` processes, so nothing
here talks to Jira.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from oversight.api import refresh as refresh_module
from oversight.api.app import create_app
from oversight.api.refresh import Refresher, board_steps, board_synced
from oversight.cli import EXIT_CONFIG, EXIT_GUARD, EXIT_JIRA, EXIT_JIRA_AUTH
from oversight.llm.scripted import ScriptedProvider
from oversight.settings import PROJECT_ROOT, Settings
from oversight.store.db import Store
from oversight.timeutil import UTC
from tests.conftest import CONFIG_FIXTURES

KEY = "test-key"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
# One board's refresh once it has synced before, with discovery off (the default Refresher's).
AIL_STEPS = [("sync", "--incremental", "--project", "AIL"), ("derive", "--project", "AIL"),
             ("rules", "--project", "AIL"), ("baseline", "--missing", "--project", "AIL")]
STEPS = len(AIL_STEPS)
IDLE = {"project": "AIL", "state": "idle", "started_at": None, "finished_at": None, "message": None}


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        return self.at


class Steps:
    """Records each step; fails the one named in `fail`; can hold the first step until released."""

    def __init__(self, fail: str | None = None, hold: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail
        self.release = threading.Event()
        if not hold:
            self.release.set()

    def __call__(self, argv: Sequence[str]) -> int:
        self.release.wait(5)
        self.calls.append(tuple(argv))
        return 1 if argv[0] == self.fail else 0


def client_with(tmp_path: Path, refresher: Refresher, projects: str = "AIL") -> TestClient:
    settings = Settings(
        _env_file=None, jira_projects=projects, api_service_key=KEY, db_path=tmp_path / "oversight.db",
        config_dir=CONFIG_FIXTURES, reports_dir=tmp_path / "reports",
    )
    app = create_app(settings, provider=ScriptedProvider(), now=lambda: NOW, refresher=refresher)
    return TestClient(app, headers={"X-Service-Key": KEY})


def test_a_refresh_syncs_derives_and_checks_the_board_picked_and_no_other(tmp_path: Path) -> None:
    steps, clock = Steps(), Clock()
    refresher = Refresher(runner=steps, now=clock)
    client = client_with(tmp_path, refresher, "DEMO,AIL")

    assert client.get("/api/v1/refresh", params={"project": "AIL"}).json() == IDLE
    started = client.post("/api/v1/refresh", params={"project": "AIL"})
    assert started.status_code == 202
    assert started.json()["state"] == "running" and started.json()["project"] == "AIL"
    refresher.wait(5)

    body = client.get("/api/v1/refresh", params={"project": "ail"}).json()
    assert body["project"] == "AIL" and body["state"] == "succeeded" and body["message"] is None
    assert body["started_at"] == "2026-11-06T09:00:00.000Z" and body["finished_at"] == "2026-11-06T09:00:00.000Z"
    assert steps.calls == AIL_STEPS
    # The default board was not touched, and says so.
    assert client.get("/api/v1/refresh").json() == {**IDLE, "project": "DEMO"}


def test_without_a_pick_the_default_board_is_synced(tmp_path: Path) -> None:
    steps = Steps()
    refresher = Refresher(runner=steps, now=Clock())
    client = client_with(tmp_path, refresher, "DEMO,AIL")
    assert client.post("/api/v1/refresh").json()["project"] == "DEMO"
    refresher.wait(5)
    assert [call[-1] for call in steps.calls] == ["DEMO"] * STEPS


def test_a_board_the_service_does_not_serve_is_refused(tmp_path: Path) -> None:
    steps = Steps()
    client = client_with(tmp_path, Refresher(runner=steps, now=Clock()))
    for method in ("get", "post"):
        answer = getattr(client, method)("/api/v1/refresh", params={"project": "NOPE"})
        assert answer.status_code == 404 and answer.json()["code"] == "unknown_board"
    assert steps.calls == []


def test_the_steps_discover_first_and_read_a_boards_first_sync_in_full() -> None:
    assert board_steps("AIL", first=False, discover=False) == AIL_STEPS
    assert board_steps("NEW", first=True, discover=True) == [
        ("discover",), ("sync", "--full", "--project", "NEW"), ("derive", "--project", "NEW"),
        ("rules", "--project", "NEW"), ("baseline", "--missing", "--project", "NEW"),
    ]
    steps = Steps()
    refresher = Refresher(runner=steps, now=Clock(), synced=lambda project: project != "NEW", discover=True)
    refresher.start("NEW", background=False)
    refresher.start("AIL", background=False)
    assert steps.calls == [*board_steps("NEW", first=True, discover=True), *board_steps("AIL", first=False,
                                                                                         discover=True)]


def test_whether_a_board_synced_before_is_read_from_the_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "oversight.db"
    assert not board_synced(path, "AIL")  # no snapshot yet
    with Store.open(path) as store:
        store.set_cursor("AIL", last_synced_at="2026-11-06T08:00:00.000Z", run_id="r1", account_timezone=None)
    assert board_synced(path, "AIL") and not board_synced(path, "NEW")


def test_a_discovery_that_fails_never_stops_the_boards_sync() -> None:
    steps = Steps(fail="discover")
    refresher = Refresher(runner=steps, now=Clock(), discover=True)
    refresher.start("AIL", background=False)
    assert refresher.status("AIL")["state"] == "succeeded"
    assert steps.calls == [("discover",), *AIL_STEPS]


def test_a_boards_sync_writes_its_baseline_when_it_has_none_and_a_baseline_that_fails_never_fails_the_sync() -> None:
    """18 Nov 2026: a board synced from the dashboard never got a baseline report, so its Baseline page stayed empty
    and its "Check again" could never help. The refresh ends with `baseline --missing` for the board (a report only
    when it has none); the board's figures are in by then, so a report that fails is logged and the sync has
    succeeded (the next cycle writes it)."""
    steps = Steps(fail="baseline")
    refresher = Refresher(runner=steps, now=Clock(), synced=lambda _project: False)
    with capture_logs() as events:
        refresher.start("NEW", background=False)
    assert steps.calls[-1] == ("baseline", "--missing", "--project", "NEW")
    assert refresher.status("NEW")["state"] == "succeeded" and refresher.status("NEW")["message"] is None
    assert {"event": "api_refresh_baseline_failed", "project": "NEW", "logger": "api",
            "log_level": "warning"} in events


def test_another_boards_sync_is_queued_behind_the_one_running_never_refused_nor_run_beside_it(tmp_path: Path) -> None:
    steps = Steps(hold=True)
    refresher = Refresher(runner=steps, now=Clock())
    client = client_with(tmp_path, refresher, "DEMO,AIL")

    first = client.post("/api/v1/refresh", params={"project": "DEMO"}).json()
    second = client.post("/api/v1/refresh", params={"project": "AIL"}).json()
    assert (first["project"], first["state"]) == ("DEMO", "running")
    assert second == {"project": "AIL", "state": "queued", "started_at": None, "finished_at": None, "message": None}
    assert client.get("/api/v1/refresh", params={"project": "AIL"}).json()["state"] == "queued"
    again = client.post("/api/v1/refresh", params={"project": "AIL"}).json()  # its own press while it waits
    assert again == second
    steps.release.set()
    refresher.wait(5)
    assert [call[-1] for call in steps.calls] == ["DEMO"] * STEPS + ["AIL"] * STEPS  # one after the other, each once
    assert refresher.status("AIL")["state"] == refresher.status("DEMO")["state"] == "succeeded"


def test_a_second_press_while_running_starts_nothing(tmp_path: Path) -> None:
    steps = Steps(hold=True)
    refresher = Refresher(runner=steps, now=Clock())
    client = client_with(tmp_path, refresher)

    first = client.post("/api/v1/refresh").json()
    second = client.post("/api/v1/refresh").json()
    assert first["state"] == second["state"] == "running"
    assert first["started_at"] == second["started_at"]
    steps.release.set()
    refresher.wait(5)
    assert steps.calls.count(AIL_STEPS[0]) == 1


def test_the_cooldown_is_each_boards_own(tmp_path: Path) -> None:
    steps, clock = Steps(), Clock()
    refresher = Refresher(runner=steps, now=clock)
    client = client_with(tmp_path, refresher, "AIL,DEMO")
    client.post("/api/v1/refresh", params={"project": "AIL"})
    refresher.wait(5)

    clock.at = NOW + timedelta(seconds=10)
    assert client.post("/api/v1/refresh", params={"project": "AIL"}).json()["state"] == "succeeded"  # reported only
    assert len(steps.calls) == STEPS
    assert client.post("/api/v1/refresh", params={"project": "DEMO"}).json()["state"] == "running"  # another board
    refresher.wait(5)
    assert len(steps.calls) == 2 * STEPS

    clock.at = NOW + timedelta(seconds=31)
    assert client.post("/api/v1/refresh", params={"project": "AIL"}).json()["state"] == "running"
    refresher.wait(5)
    assert len(steps.calls) == 3 * STEPS


def test_a_failed_step_stops_the_refresh_and_says_what_to_do_in_one_line(tmp_path: Path) -> None:
    """18 Nov 2026: the dashboard leads with "The last sync didn't finish." and the message said it again ("The sync
    failed while copying from Jira. ..."): the message is what to do, and the step that failed goes to the log."""
    steps = Steps(fail="sync")
    refresher = Refresher(runner=steps, now=Clock())
    client = client_with(tmp_path, refresher)
    with capture_logs() as events:
        client.post("/api/v1/refresh")
        refresher.wait(5)

    body = client.get("/api/v1/refresh").json()
    assert body["state"] == "failed"
    assert body["message"] == "The figures on screen are from the last sync that worked."
    assert steps.calls == AIL_STEPS[:1]  # derive and rules never ran on a failed sync
    assert {"event": "api_refresh_finished", "project": "AIL", "state": "failed", "failed_step": "sync",
            "logger": "api", "log_level": "info"} in events


def test_a_failed_first_sync_says_to_try_again_rather_than_point_at_figures_there_are_none_of() -> None:
    refresher = Refresher(runner=Steps(fail="sync"), now=Clock(), synced=lambda _project: False)
    refresher.start("NEW", background=False)
    assert refresher.status("NEW")["message"] == "Try again in a few minutes."


@pytest.mark.parametrize(("code", "first", "message"), [
    # The service's settings (a JIRA_BASE_URL that is not https, say) or the read-only guard: trying again cannot help.
    (EXIT_CONFIG, True, "The service's settings need fixing. Ask your administrator."),
    (EXIT_GUARD, True, "The service's settings need fixing. Ask your administrator."),
    (EXIT_JIRA_AUTH, True, "Jira didn't accept the service's account. Ask your administrator."),
    (EXIT_CONFIG, False, "The service's settings need fixing. Ask your administrator. The figures on screen are from "
                         "the last sync that worked."),
    # Jira out of reach, or anything else: it may well work in a few minutes.
    (EXIT_JIRA, True, "Try again in a few minutes."),
    (1, True, "Try again in a few minutes."),
])
def test_a_failure_trying_again_cannot_mend_says_to_ask_an_administrator(code: int, first: bool, message: str) -> None:
    def fails(argv: Sequence[str]) -> int:
        return code

    refresher = Refresher(runner=fails, now=Clock(), synced=lambda _project: not first)
    refresher.start("EQNOP", background=False)
    assert (refresher.status("EQNOP")["state"], refresher.status("EQNOP")["message"]) == ("failed", message)


def test_a_step_that_hangs_is_reported_as_too_long(tmp_path: Path) -> None:
    def hangs(argv: Sequence[str]) -> int:
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=1)

    refresher = Refresher(runner=hangs, now=Clock())
    refresher.start("AIL", background=False)
    assert refresher.status("AIL")["state"] == "failed"
    assert refresher.status("AIL")["message"] == (
        "It took too long. The figures on screen are from the last sync that worked.")
    first = Refresher(runner=hangs, now=Clock(), synced=lambda _project: False)
    first.start("NEW", background=False)
    assert first.status("NEW")["message"] == "It took too long. Try again in a few minutes."


def test_a_sync_that_stops_unexpectedly_says_what_to_do_as_any_failure_does() -> None:
    def crashes(argv: Sequence[str]) -> int:
        raise RuntimeError("a bug")

    refresher = Refresher(runner=crashes, now=Clock())
    refresher.start("AIL", background=False)
    assert (refresher.status("AIL")["state"], refresher.status("AIL")["message"]) == (
        "failed", "The figures on screen are from the last sync that worked.")
    first = Refresher(runner=crashes, now=Clock(), synced=lambda _project: False)
    first.start("NEW", background=False)
    assert first.status("NEW")["message"] == "Try again in a few minutes."


def test_refresh_needs_the_service_key(tmp_path: Path) -> None:
    client = client_with(tmp_path, Refresher(runner=Steps(), now=Clock()))
    assert client.post("/api/v1/refresh", headers={"X-Service-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/refresh", headers={"X-Service-Key": "wrong"}).status_code == 401


def test_a_board_that_never_synced_can_be_synced_from_the_dashboard(tmp_path: Path) -> None:
    """The way a never-synced board gets its first sync: the refresh routes never answer board_not_synced."""
    with Store.open(tmp_path / "oversight.db"):
        pass  # a snapshot, with nothing of either board in it
    steps = Steps()
    refresher = Refresher(runner=steps, now=Clock(), synced=lambda _project: False)
    client = client_with(tmp_path, refresher, "DEMO,AIL")
    assert client.get("/api/v1/overview", params={"project": "AIL"}).status_code == 409
    assert client.post("/api/v1/refresh", params={"project": "AIL"}).status_code == 202
    refresher.wait(5)
    assert steps.calls[0] == ("sync", "--full", "--project", "AIL")


# --- the re-check after a board's rule settings are saved (JO-15) --------------------------------------------------


def test_a_recheck_runs_the_rules_for_its_board_alone_and_keeps_its_state_apart_from_sync_now() -> None:
    steps = Steps()
    refresher = Refresher(runner=steps, now=Clock())

    asked = refresher.recheck("AIL", 3, background=False)

    assert (asked.project, asked.revision, asked.requested_at, asked.state) == ("AIL", 3, NOW, "queued")
    assert steps.calls == [("rules", "--project", "AIL")]  # no sync: Jira is not read
    done = refresher.recheck_of("AIL")
    assert done is not None and (done.state, done.started_at, done.finished_at, done.message) == (
        "succeeded", NOW, NOW, None)
    assert refresher.recheck_of("DEMO") is None
    # "Sync now"'s own state is the sync's alone.
    assert refresher.status("AIL") == IDLE


def test_a_recheck_never_runs_beside_a_sync_whichever_was_asked_for_first() -> None:
    steps, clock = Steps(hold=True), Clock()
    refresher = Refresher(runner=steps, now=clock)
    refresher.start("AIL")  # held at its first step
    assert refresher.recheck("AIL", 1).state == "queued"
    assert refresher.status("AIL")["state"] == "running"
    steps.release.set()
    refresher.wait(5)
    assert steps.calls == [*AIL_STEPS, ("rules", "--project", "AIL")]

    steps.calls.clear()
    steps.release.clear()
    clock.at = NOW + timedelta(seconds=31)  # past the cooldown
    refresher.recheck("AIL", 2)  # held while it runs
    assert refresher.start("AIL")["state"] == "queued"  # the button says it waits its turn, at once
    steps.release.set()
    refresher.wait(5)
    assert steps.calls == [("rules", "--project", "AIL"), *AIL_STEPS]
    assert refresher.status("AIL")["state"] == "succeeded"


def test_a_board_asked_for_again_while_its_recheck_waits_keeps_the_one_waiting() -> None:
    steps = Steps(hold=True)
    refresher = Refresher(runner=steps, now=Clock())
    refresher.start("AIL")
    refresher.recheck("AIL", 1)
    again = refresher.recheck("AIL", 2)  # it reads the settings when it runs: the latest revision
    refresher.recheck("DEMO", 1)
    assert (again.revision, again.state) == (2, "queued")
    steps.release.set()
    refresher.wait(5)
    assert steps.calls[STEPS:] == [("rules", "--project", "AIL"), ("rules", "--project", "DEMO")]
    found = refresher.recheck_of("AIL")
    assert found is not None and (found.revision, found.state) == (2, "succeeded")


def _fails(argv: Sequence[str]) -> int:
    return 1


def _hangs(argv: Sequence[str]) -> int:
    raise subprocess.TimeoutExpired(cmd=list(argv), timeout=1)


def _cannot_start(argv: Sequence[str]) -> int:
    raise FileNotFoundError("python")


def _crashes(argv: Sequence[str]) -> int:
    raise RuntimeError("a bug")


@pytest.mark.parametrize(("runner", "problem"), [
    (_fails, "the rules run failed"),
    (_hangs, "it took too long"),
    (_cannot_start, "it could not start"),
    (_crashes, "it stopped unexpectedly"),
])
def test_a_recheck_that_fails_says_so_in_plain_words_and_the_next_job_still_runs(
    runner: Callable[[Sequence[str]], int], problem: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def steps(argv: Sequence[str]) -> int:
        calls.append(tuple(argv))
        return runner(argv) if len(calls) == 1 else 0  # the re-check fails; the sync after it does not

    refresher = Refresher(runner=steps, now=Clock())
    refresher.recheck("AIL", 1, name="AILED", background=False)

    found = refresher.recheck_of("AIL")
    assert found is not None and found.state == "failed" and found.finished_at == NOW
    # The board by its name, as the page names it (18 Nov 2026: it said "AIL's" beside a page that says AILED).
    assert found.message == (
        f"Couldn't re-check AILED's findings with the new settings: {problem}. They will apply at the next sync.")
    refresher.start("AIL", background=False)
    assert refresher.status("AIL")["state"] == "succeeded" and calls[1:] == AIL_STEPS


def test_a_recheck_asked_for_with_no_name_names_the_board_by_its_key() -> None:
    refresher = Refresher(runner=_fails, now=Clock())
    refresher.recheck("AIL", 1, background=False)
    found = refresher.recheck_of("AIL")
    assert found is not None and found.message is not None and found.message.startswith("Couldn't re-check AIL's ")


PICTURE_WARNING = (
    '{"person": "p0123456789ab", "reason": "HTTP 404", "event": "picture_not_fetched", "logger": "pictures", '
    '"log_level": "warning", "timestamp": "2026-11-06T09:00:01.000000Z"}'
)


def test_a_steps_own_log_goes_on_into_the_apis_log_whether_it_succeeds_or_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sync whose pictures fail still succeeds: its warnings (person id and reason) must reach the API's log all
    the same, as the scheduled cycle's log keeps them, rather than be read only when a step fails."""
    answers = iter([
        (0, f"{PICTURE_WARNING}\r\n\n".encode()),
        (1, b'{"event": "sync_failed", "log_level": "error"}\nTraceback (most recent call last):\n'),
    ])

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["stderr"] == subprocess.PIPE and kwargs["stdout"] == subprocess.DEVNULL
        code, stderr = next(answers)
        return subprocess.CompletedProcess(argv, code, b"", stderr)

    monkeypatch.setattr(refresh_module.subprocess, "run", fake_run)
    with capture_logs() as events:
        assert refresh_module.run_step(("sync", "--incremental")) == 0
        assert capsys.readouterr().err == PICTURE_WARNING + "\n"  # as written, blank lines left out
        assert events == []
        assert refresh_module.run_step(("derive",)) == 1
    assert capsys.readouterr().err == '{"event": "sync_failed", "log_level": "error"}\nTraceback (most recent call last):\n'
    assert events == [{"event": "api_refresh_step_failed", "step": "derive", "exit_code": 1, "logger": "api",
                       "log_level": "warning"}]


def test_a_step_stopped_for_taking_too_long_still_leaves_what_it_logged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1, stderr=f"{PICTURE_WARNING}\n".encode())

    monkeypatch.setattr(refresh_module.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        refresh_module.run_step(("sync", "--incremental"))
    assert capsys.readouterr().err == PICTURE_WARNING + "\n"


def test_the_relay_needs_no_stderr_to_write_to(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", None)  # pythonw, or a service with no console
    refresh_module.relay(PICTURE_WARNING.encode())
    refresh_module.relay(None)


def test_each_step_is_this_interpreter_running_the_cli_in_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen["argv"], seen["cwd"], seen["timeout"] = argv, kwargs["cwd"], kwargs["timeout"]
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(refresh_module.subprocess, "run", fake_run)
    assert refresh_module.run_step(("sync", "--incremental")) == 0
    assert seen["argv"] == [sys.executable, "-m", "oversight", "sync", "--incremental"]
    assert seen["cwd"] == PROJECT_ROOT
    assert seen["timeout"] == refresh_module.STEP_TIMEOUT_S
    # A board's first sync reads every issue: it gets the scheduled cycle's hour.
    assert refresh_module.run_step(("sync", "--full", "--project", "NEW")) == 0
    assert seen["timeout"] == refresh_module.FULL_STEP_TIMEOUT_S == 60 * 60
