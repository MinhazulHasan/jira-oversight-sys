from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from oversight.errors import ConfigError
from oversight.settings import PROJECT_ROOT, Settings, validate_project_keys


def test_projects_split_from_a_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_PROJECTS", "ail, xyz ,")
    assert Settings().jira_projects == ["AIL", "XYZ"]


def test_project_keys_that_could_inject_jql_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_PROJECTS", 'AIL" OR project = "SECRET')
    with pytest.raises(ValidationError):
        Settings()


def test_validate_project_keys() -> None:
    assert validate_project_keys([" ail ", "X_1"]) == ["AIL", "X_1"]
    with pytest.raises(ValueError, match="1AB"):
        validate_project_keys(["1AB"])


def test_token_is_secret_in_repr() -> None:
    s = Settings(jira_api_token="super-secret-token")
    assert "super-secret-token" not in repr(s)
    assert s.jira_api_token.get_secret_value() == "super-secret-token"


def test_base_url_loses_trailing_slash() -> None:
    assert Settings(jira_base_url=" https://x.atlassian.net/ ").jira_base_url == "https://x.atlassian.net"


def test_relative_paths_resolve_against_the_project_folder() -> None:
    s = Settings(db_path=Path("data/x.db"))
    assert s.db_file == PROJECT_ROOT / "data" / "x.db"
    assert s.process_file == PROJECT_ROOT / "config" / "process.yaml"
    absolute = Path(PROJECT_ROOT.anchor) / "elsewhere" / "y.db"
    assert Settings(db_path=absolute).db_file == absolute


def test_require_jira_names_every_missing_variable() -> None:
    with pytest.raises(ConfigError) as err:
        Settings(jira_base_url="", jira_email="", jira_api_token="").require_jira()
    assert "JIRA_BASE_URL" in str(err.value)
    assert "JIRA_EMAIL" in str(err.value)
    assert "JIRA_API_TOKEN" in str(err.value)


def test_require_jira_passes_when_complete(settings: Settings) -> None:
    settings.require_jira()


@pytest.mark.parametrize("url", ["http://x.atlassian.net", "x.atlassian.net", "ftp://x.atlassian.net"])
def test_require_jira_wants_https(url: str) -> None:
    # Regression: an http:// site would have been sent the API token unencrypted.
    with pytest.raises(ConfigError, match="https://") as err:
        Settings(jira_base_url=url, jira_email="dev@example.com", jira_api_token="t").require_jira()
    assert "x.atlassian.net" not in str(err.value)
    secure = Settings(jira_base_url="HTTPS://x.atlassian.net", jira_email="dev@example.com", jira_api_token="t")
    secure.require_jira()


def test_defaults_match_the_plan() -> None:
    s = Settings()
    assert s.llm_model_qa == "claude-sonnet-5-5"
    assert s.llm_model_insights == "claude-opus-5-5"
    assert s.jira_max_attempts == 4


def test_report_timezone_is_an_iana_zone_and_utc_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (Settings().report_timezone, Settings().report_zone) == ("", "UTC")
    monkeypatch.setenv("REPORT_TIMEZONE", " America/Chicago ")
    assert (Settings().report_timezone, Settings().report_zone) == ("America/Chicago", "America/Chicago")
    for bad in ("Mars/Olympus", "America", "../etc/passwd"):
        monkeypatch.setenv("REPORT_TIMEZONE", bad)
        with pytest.raises(ValidationError, match="REPORT_TIMEZONE must be an IANA time zone name"):
            Settings()


def test_people_comparison_is_on_unless_an_organisation_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """PEOPLE_COMPARISON (JO-11): on by default; off only where an organisation does not want people compared."""
    assert Settings().people_comparison == "on"
    monkeypatch.setenv("PEOPLE_COMPARISON", "off")
    assert Settings().people_comparison == "off"
    for bad in ("OFF", "no", "false", "0", " on", ""):
        monkeypatch.setenv("PEOPLE_COMPARISON", bad)
        with pytest.raises(ValidationError, match="people_comparison"):
            Settings()


def test_board_discovery_is_on_unless_an_organisation_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOARD_DISCOVERY (JO-17): on by default, every board the Jira account can browse; off, JIRA_PROJECTS only."""
    assert Settings().board_discovery == "on" and Settings().discovers_boards
    monkeypatch.setenv("BOARD_DISCOVERY", "off")
    assert Settings().board_discovery == "off" and not Settings().discovers_boards
    for bad in ("OFF", "yes", "true", ""):
        monkeypatch.setenv("BOARD_DISCOVERY", bad)
        with pytest.raises(ValidationError, match="board_discovery"):
            Settings()


def test_jira_configured_says_whether_a_jira_call_can_be_made(settings: Settings) -> None:
    assert settings.jira_configured
    assert not settings.model_copy(update={"jira_base_url": ""}).jira_configured
    assert not settings.model_copy(update={"jira_base_url": "http://fake.atlassian.net"}).jira_configured


def test_a_bad_people_comparison_is_a_config_error_at_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from oversight.cli import EXIT_CONFIG, main
    from oversight.settings import get_settings

    monkeypatch.setenv("PEOPLE_COMPARISON", "maybe")
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_options: pytest.fail("the API was started"))
    get_settings.cache_clear()
    try:
        assert main(["api", "--port", "3301"]) == EXIT_CONFIG  # refused before anything starts or opens
    finally:
        get_settings.cache_clear()
    assert "config error: invalid settings" in capsys.readouterr().err
