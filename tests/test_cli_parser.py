from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path

import pytest

from oversight.cli import EXIT_CONFIG, EXIT_OK, build_parser, main
from oversight.settings import Settings, get_settings


def test_sync_needs_exactly_one_mode() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["sync"])
    with pytest.raises(SystemExit):
        parser.parse_args(["sync", "--full", "--incremental"])
    assert parser.parse_args(["sync", "--incremental"]).incremental


def test_baseline_options_parse() -> None:
    args = build_parser().parse_args(
        ["baseline", "--as-of", "2026-11-02", "--window-days", "90", "--tz", "America/Chicago", "--project", "ail"]
    )
    assert args.as_of == date(2026, 11, 2) and args.window_days == 90 and args.project == ["ail"]
    with pytest.raises(SystemExit):
        build_parser().parse_args(["baseline", "--as-of", "2 Nov"])


def test_missing_credentials_exit_with_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["doctor"], settings=Settings(jira_base_url="", jira_email="", jira_api_token=""))
    assert code == EXIT_CONFIG
    assert "JIRA_API_TOKEN" in capsys.readouterr().err


def test_bad_project_flag_is_a_config_error(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["derive", "--project", "A-B"], settings=settings)
    assert code == EXIT_CONFIG
    assert "invalid Jira project keys" in capsys.readouterr().err


def test_a_report_survives_a_console_that_cannot_encode_it(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: redirected output on Windows is cp1252 and strict, so a report
    # line holding any other character (a Jira name, or a path under a user folder
    # like this one) raised UnicodeEncodeError after the work was done.
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    reports = tmp_path / "reports \u2713 \u09b0\u09bf\u09aa\u09cb\u09b0\u09cd\u099f"
    settings = settings.model_copy(update={"reports_dir": reports})

    code = main(["baseline", "--as-of", "2026-11-02", "--tz", "America/Chicago"], settings=settings)

    console.flush()
    printed = console.buffer.getvalue().decode("cp1252")
    assert code == EXIT_OK
    assert "baseline AIL as of 2026-11-02" in printed
    assert r"reports \u2713 \u09b0" in printed  # escaped, not lost
    assert (reports / "baseline_AIL_2026-11-02.md").exists()


def test_a_utf16_env_file_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: Notepad's "Unicode" save of .env crashed every command with a traceback.
    env = tmp_path / ".env"
    env.write_text("JIRA_PROJECTS=AIL\n", encoding="utf-16")
    monkeypatch.setitem(Settings.model_config, "env_file", env)
    get_settings.cache_clear()
    try:
        assert main(["derive"]) == EXIT_CONFIG
    finally:
        get_settings.cache_clear()
    assert "config error: .env is not UTF-8 text (save it as UTF-8)" in capsys.readouterr().err
