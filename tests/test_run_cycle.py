from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from oversight.settings import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "run_cycle.ps1"
MORNING = PROJECT_ROOT / "scripts" / "morning.ps1"
POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")

STUB = (
    "@echo off\r\n"
    "echo stub-python %*\r\n"
    'echo {"event": "stub_log", "command": "%3"} 1>&2\r\n'
    'if "%3"=="discover" exit /b %STUB_DISCOVER_EXIT%\r\n'
    'if "%3"=="sync" exit /b %STUB_SYNC_EXIT%\r\n'
    'if "%3"=="derive" exit /b %STUB_DERIVE_EXIT%\r\n'
    'if "%3"=="rules" exit /b %STUB_RULES_EXIT%\r\n'
    'if "%3"=="baseline" exit /b %STUB_BASELINE_EXIT%\r\n'
    'if "%3"=="digest" exit /b %STUB_DIGEST_EXIT%\r\n'
    "exit /b 0\r\n"
)


def cycle(
    tmp_path: Path,
    *options: str,
    discover_exit: int = 0,
    sync_exit: int = 0,
    derive_exit: int = 0,
    rules_exit: int = 0,
    baseline_exit: int = 0,
    digest_exit: int = 0,
    script: Path = SCRIPT,
) -> tuple[int, list[str]]:
    stub = tmp_path / "py with spaces" / "python.cmd"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(STUB, encoding="ascii", newline="")
    logs = tmp_path / "log dir"
    env = os.environ | {
        "STUB_DISCOVER_EXIT": str(discover_exit),
        "STUB_SYNC_EXIT": str(sync_exit),
        "STUB_DERIVE_EXIT": str(derive_exit),
        "STUB_RULES_EXIT": str(rules_exit),
        "STUB_BASELINE_EXIT": str(baseline_exit),
        "STUB_DIGEST_EXIT": str(digest_exit),
    }
    done = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-Python", str(stub), "-LogDir", str(logs), *options],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    (log,) = logs.glob("cycle_*.log")
    return done.returncode, log.read_text(encoding="utf-8").splitlines()


def commands(lines: list[str]) -> list[str]:
    return [line.removeprefix("stub-python -m oversight ") for line in lines if line.startswith("stub-python")]


def test_a_cycle_runs_sync_then_derive_and_logs_both(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path)
    assert code == 0
    printed = [line for line in lines if "[run_cycle]" not in line]
    assert printed == [
        "stub-python -m oversight discover",
        '{"event": "stub_log", "command": "discover"} ',
        "stub-python -m oversight sync --incremental",
        '{"event": "stub_log", "command": "sync"} ',
        "stub-python -m oversight derive",
        '{"event": "stub_log", "command": "derive"} ',
    ]
    assert lines[-1].endswith("[run_cycle] cycle finished: ok")


def test_a_failed_discovery_is_noted_and_the_cycle_goes_on(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, "-Rules", discover_exit=4)
    assert code == 0
    assert commands(lines) == ["discover", "sync --incremental", "derive", "rules", "baseline --missing"]
    assert any(line.endswith("[run_cycle] discover failed (exit 4); the boards stored before stay") for line in lines)
    assert lines[-1].endswith("[run_cycle] cycle finished: ok")


def test_a_failed_sync_stops_the_cycle_with_its_exit_code(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, sync_exit=4)
    assert code == 4
    assert not any("oversight derive" in line for line in lines)
    assert lines[-1].endswith("cycle FAILED at sync (exit 4); later steps were not run")


def test_a_full_cycle_and_a_failed_derive(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, "-Full", derive_exit=2)
    assert code == 2
    assert "stub-python -m oversight sync --full" in lines
    assert lines[-1].endswith("cycle FAILED at derive (exit 2); later steps were not run")


def test_rules_runs_after_derive_in_the_same_cycle_then_the_missing_baselines(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, "-Rules")
    assert code == 0
    assert commands(lines) == ["discover", "sync --incremental", "derive", "rules", "baseline --missing"]
    started = "[run_cycle] cycle started (discover, then sync --incremental, then derive, then rules, then baseline)"
    assert any(line.endswith(started) for line in lines)
    assert lines[-1].endswith("[run_cycle] cycle finished: ok")


def test_a_failed_baseline_is_noted_and_the_cycle_has_still_succeeded(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, "-Rules", baseline_exit=2)
    assert code == 0
    assert commands(lines) == ["discover", "sync --incremental", "derive", "rules", "baseline --missing"]
    assert any(line.endswith("[run_cycle] baseline failed (exit 2); the next cycle writes the reports still missing")
               for line in lines)
    assert lines[-1].endswith("[run_cycle] cycle finished: ok")


def test_rules_is_skipped_after_a_failed_derive(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, "-Rules", derive_exit=2)
    assert code == 2
    assert commands(lines) == ["discover", "sync --incremental", "derive"]
    assert lines[-1].endswith("cycle FAILED at derive (exit 2); later steps were not run")


def test_a_failed_rules_step_fails_the_cycle_with_its_exit_code(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, "-Full", "-Rules", rules_exit=1)
    assert code == 1
    assert commands(lines) == ["discover", "sync --full", "derive", "rules"]
    assert lines[-1].endswith("cycle FAILED at rules (exit 1); later steps were not run")


def test_the_morning_run_is_a_cycle_with_rules_then_the_digest(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, "-Channel", "file", script=MORNING)
    assert code == 0
    assert commands(lines) == ["discover", "sync --incremental", "derive", "rules", "baseline --missing",
                               "digest --channel file"]
    assert lines[-1].endswith("[run_cycle] cycle finished: ok")


def test_the_morning_digest_still_goes_out_after_a_failed_sync(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, script=MORNING, sync_exit=4)
    assert code == 4
    assert commands(lines) == ["discover", "sync --incremental", "digest"]
    assert lines[-1].endswith("cycle FAILED at sync (exit 4); later steps were not run, except the digest")


def test_a_failed_digest_fails_the_morning_run(tmp_path: Path) -> None:
    code, lines = cycle(tmp_path, script=MORNING, digest_exit=6)
    assert code == 6
    assert commands(lines) == ["discover", "sync --incremental", "derive", "rules", "baseline --missing", "digest"]
    assert lines[-1].endswith("cycle FAILED at digest (exit 6); later steps were not run")
