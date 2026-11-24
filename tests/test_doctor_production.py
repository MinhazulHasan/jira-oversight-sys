"""`python -m oversight doctor --production` (oversight/production.py)"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oversight import production
from oversight.backup import backup_name
from oversight.cli import EXIT_CHECKS_FAILED, EXIT_CONFIG, EXIT_OK, main
from oversight.doctor import Check, DoctorReport
from oversight.production import read_env_file, run_production_checks
from oversight.settings import Settings
from oversight.store.db import SCHEMA_VERSION, Store

NOW = datetime(2026, 11, 12, 14, 30, tzinfo=UTC)
KEY = "Qm7xT2vLp9sKd4RzW8nYb3HcJ6gFe1UaX5oIq0Zt"
SECRET = "7f3a9c1e5b2d8046a1c7e93b5d20f84c6a19e7d3b50c82f4916ad7e30b5c8f21"
ADDRESS = "https://oversight.example.com"
DASHBOARD = {
    "OVERSIGHT_SERVICE_KEY": KEY, "OVERSIGHT_JIRA_BASE_URL": "https://fake.atlassian.net", "APP_URL": ADDRESS,
    "ATLASSIAN_CLIENT_ID": "client-id-value", "ATLASSIAN_CLIENT_SECRET": "client-secret-value",
    "AUTH_SECRET": SECRET, "ALLOWED_EMAIL_DOMAINS": "example.com",
}
LOCAL_ORDER = [
    "service key", "jira", "jira account", "config", "model", "snapshot", "backups", "data folder", "logs folder",
    "api docs", "time zone", "log format", "web address",
]
DASHBOARD_ORDER = ["sign-in", "auth secret", "shared key", "atlassian app", "allowed domains", "admins"]
INFO_ORDER = ["people comparison", "ask rate limit", "model budget", "access log", "sync interval", "dashboard"]
ORDER = [*LOCAL_ORDER, *INFO_ORDER]


@pytest.fixture(autouse=True)
def app_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The folder the doctor treats as the project's"""
    folder = tmp_path / "install" / "backend"
    (folder / "logs").mkdir(parents=True)
    monkeypatch.setattr(production, "PROJECT_ROOT", folder)
    monkeypatch.delenv(production.DASHBOARD_ENV_VARIABLE, raising=False)
    return folder


@pytest.fixture
def ready(settings: Settings) -> Settings:
    """A deployment that passes"""
    Store.open(settings.db_file).close()
    backups = settings.backups_path
    backups.mkdir(parents=True)
    (backups / backup_name(NOW - timedelta(hours=1))).write_bytes(b"backup")
    return changed(settings, api_service_key=KEY, report_timezone="Europe/London", log_format="json",
                   cors_allow_origins=ADDRESS)


def dashboard_file(tmp_path: Path, **update: str) -> dict[str, str]:
    values = {key: value for key, value in {**DASHBOARD, **update}.items() if value != ""}
    path = tmp_path / "dashboard.env"
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("# the dashboard's settings\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return {production.DASHBOARD_ENV_VARIABLE: str(path)}


def check(settings: Settings, name: str, environ: dict[str, str] | None = None) -> Check:
    report = run_production_checks(settings, now=lambda: NOW, environ=environ or {})
    return next(item for item in report.checks if item.name == name)


def grade(item: Check) -> str:
    if item.ok:
        return "info" if item.info else "ok"
    return "warn" if item.warning else "fail"


def changed(settings: Settings, **update: Any) -> Settings:
    return Settings.model_validate({**settings.model_dump(), **update})


def test_a_ready_deployment_passes_with_every_check_in_order(ready: Settings) -> None:
    report = run_production_checks(ready, now=lambda: NOW, environ={})
    assert [item.name for item in report.checks] == ORDER
    assert report.ok
    assert {item.name: grade(item) for item in report.checks} == {
        "service key": "ok", "jira": "ok", "jira account": "info", "config": "ok", "model": "info",
        "snapshot": "ok", "backups": "ok", "data folder": "ok", "logs folder": "ok", "api docs": "ok",
        "time zone": "ok", "log format": "ok", "web address": "ok", "people comparison": "info",
        "ask rate limit": "info", "model budget": "info", "access log": "info", "sync interval": "info",
        "dashboard": "info",
    }
    rendered = report.render()
    assert rendered.splitlines()[-1] == "Verdict: PASS"
    assert "[INFO] dashboard: its settings are not read here" in rendered
    assert production.DASHBOARD_ENV_VARIABLE in rendered


def test_with_the_dashboards_settings_its_checks_join_in_order_and_pass(ready: Settings, tmp_path: Path) -> None:
    report = run_production_checks(ready, now=lambda: NOW, environ=dashboard_file(tmp_path))
    assert [item.name for item in report.checks] == [*LOCAL_ORDER, *DASHBOARD_ORDER, *INFO_ORDER]
    assert report.ok
    graded = {item.name: grade(item) for item in report.checks}
    assert [graded[name] for name in DASHBOARD_ORDER] == ["ok", "ok", "ok", "ok", "ok", "info"]
    assert graded["web address"] == "ok"
    dashboard = report.checks[-1]
    assert dashboard.name == "dashboard" and "were read from" in dashboard.detail


def test_a_blank_key_fails(ready: Settings) -> None:
    item = check(changed(ready, api_service_key=""), "service key")
    assert grade(item) == "fail" and "will not start" in item.detail


def test_a_blank_key_with_insecure_allowed_fails_as_open_to_anyone(ready: Settings) -> None:
    item = check(changed(ready, api_service_key="", api_allow_insecure=True), "service key")
    assert grade(item) == "fail" and "answers anyone" in item.detail


@pytest.mark.parametrize(("length", "expected"), [(31, "fail"), (32, "ok"), (64, "ok")])
def test_the_key_needs_32_characters(ready: Settings, length: int, expected: str) -> None:
    assert grade(check(changed(ready, api_service_key=(KEY + SECRET)[:length]), "service key")) == expected


@pytest.mark.parametrize("key", ["k" * 40, "ab" * 30, "01234567" * 5])
def test_a_repeated_key_fails_as_not_random(ready: Settings, key: str) -> None:
    item = check(changed(ready, api_service_key=key), "service key")
    assert grade(item) == "fail" and "repeated characters" in item.detail and key not in item.detail


def test_insecure_beside_a_key_is_a_warning(ready: Settings) -> None:
    item = check(changed(ready, api_allow_insecure=True), "service key")
    assert grade(item) == "warn" and "no effect" in item.detail


@pytest.mark.parametrize(
    ("update", "named"),
    [
        ({"jira_api_token": ""}, "JIRA_API_TOKEN"),
        ({"jira_email": ""}, "JIRA_EMAIL"),
        ({"jira_base_url": "http://fake.atlassian.net"}, "https://"),
    ],
)
def test_jira_settings_must_be_complete_and_https(ready: Settings, update: dict[str, Any], named: str) -> None:
    item = check(changed(ready, **update), "jira")
    assert grade(item) == "fail" and named in item.detail


def test_a_board_missing_from_process_yaml_is_a_warning(ready: Settings) -> None:
    item = check(changed(ready, jira_projects=["AIL", "NOPE"]), "config")
    assert grade(item) == "warn"
    assert "mapped from Jira's status categories: NOPE" in item.detail and "AIL" not in item.detail.split(": ")[-1]


def test_every_board_with_its_own_entry_is_ok(ready: Settings) -> None:
    item = check(changed(ready, jira_projects=["AIL"]), "config")
    assert grade(item) == "ok" and "own process.yaml entry" in item.detail


def test_a_missing_fields_yaml_fails(ready: Settings) -> None:
    ready.fields_file.unlink()
    item = check(ready, "config")
    assert grade(item) == "fail" and "python -m oversight doctor" in item.detail


def test_an_unreadable_rules_yaml_fails(ready: Settings) -> None:
    ready.rules_file.write_text("rules: [", encoding="utf-8")
    assert grade(check(ready, "config")) == "fail"


def test_a_live_model_needs_its_keys(ready: Settings) -> None:
    item = check(changed(ready, llm_mode="live"), "model")
    assert grade(item) == "fail" and "ANTHROPIC_API_KEY" in item.detail
    assert grade(check(changed(ready, llm_mode="live", anthropic_api_key="sk-test"), "model")) == "ok"
    bedrock = check(changed(ready, llm_mode="live", llm_provider="bedrock"), "model")
    assert grade(bedrock) == "fail" and "AWS_REGION" in bedrock.detail


def test_bedrock_names_its_region_and_warns_when_only_a_host_role_can_supply_credentials(ready: Settings) -> None:
    role_only = check(changed(ready, llm_mode="live", llm_provider="bedrock", aws_region="eu-west-2"), "model")
    assert grade(role_only) == "warn"
    assert "through bedrock in eu-west-2" in role_only.detail and "hop limit to 2" in role_only.detail
    keys = check(changed(ready, llm_mode="live", llm_provider="bedrock", aws_region="eu-west-2",
                         aws_access_key_id="AKIATEST", aws_secret_access_key="secret-test"), "model")
    assert grade(keys) == "ok" and "in eu-west-2" in keys.detail and "secret-test" not in keys.detail
    profile = check(changed(ready, llm_mode="live", llm_provider="bedrock", aws_region="us-east-1", aws_profile="x"),
                    "model")
    assert grade(profile) == "ok" and "in us-east-1" in profile.detail


def raw_snapshot(path: Path, version: int) -> None:
    path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE issues (key TEXT)")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


def test_no_snapshot_yet_is_a_warning(settings: Settings) -> None:
    item = check(settings, "snapshot")
    assert grade(item) == "warn" and "the first sync makes it" in item.detail
    assert not settings.db_file.exists()


def test_an_older_snapshot_is_reported_and_never_migrated(ready: Settings) -> None:
    raw_snapshot(ready.db_file, SCHEMA_VERSION - 1)
    item = check(ready, "snapshot")
    assert grade(item) == "info" and f"brings it to {SCHEMA_VERSION}" in item.detail
    conn = sqlite3.connect(ready.db_file)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION - 1
    finally:
        conn.close()


def test_a_newer_snapshot_fails(ready: Settings) -> None:
    raw_snapshot(ready.db_file, SCHEMA_VERSION + 1)
    item = check(ready, "snapshot")
    assert grade(item) == "fail" and "newer release" in item.detail


def test_an_unreadable_snapshot_fails(ready: Settings) -> None:
    for suffix in ("-wal", "-shm"):
        Path(str(ready.db_file) + suffix).unlink(missing_ok=True)
    ready.db_file.write_bytes(b"not a database, just text" * 100)
    item = check(ready, "snapshot")
    assert grade(item) == "fail" and "cannot be read" in item.detail


def test_no_backup_is_a_warning(settings: Settings) -> None:
    item = check(settings, "backups")
    assert grade(item) == "warn" and "python -m oversight backup" in item.detail


@pytest.mark.parametrize(("interval", "hours", "expected", "said"), [
    (60, 0.5, "ok", "under an hour old"), (60, 23, "ok", "23 hours old"), (60, 25.9, "ok", "25 hours old"),
    (60, 26.1, "warn", "26 hours old"), (60, 24 * 3, "warn", "3 days old"),
    (1440, 48.9, "ok", "2 days old"), (1440, 49.1, "warn", "2 days old"),
])
def test_a_backup_later_than_the_sync_service_leaves_it_is_a_warning(
    settings: Settings, interval: int, hours: float, expected: str, said: str
) -> None:
    configured = changed(settings, sync_interval_minutes=interval)
    configured.backups_path.mkdir(parents=True)
    (configured.backups_path / backup_name(NOW - timedelta(hours=hours))).write_bytes(b"backup")
    (configured.backups_path / "oversight-20261112-140000-by-hand.db").write_bytes(b"copy")
    item = check(configured, "backups")
    assert grade(item) == expected and said in item.detail


@pytest.mark.parametrize(("email", "expected"), [
    ("svc-oversight@client.com", "ok"), ("jira.readonly@client.com", "ok"), ("oversight-bot@client.com", "ok"),
    ("jane.smith@client.com", "warn"), ("john-doe@client.com", "warn"), ("someone@gmail.com", "warn"),
    ("dev@example.com", "info"), ("jsmith@client.com", "info"),
])
def test_the_jira_account_is_judged_by_its_address_and_never_printed(
    ready: Settings, email: str, expected: str
) -> None:
    item = check(changed(ready, jira_email=email), "jira account")
    assert grade(item) == expected
    assert email not in item.detail and email.split("@")[0] not in item.detail


def test_an_admins_address_is_a_personal_account_whatever_it_looks_like(ready: Settings, tmp_path: Path) -> None:
    environ = dashboard_file(tmp_path, ACCESS_MODE="admin-list", ADMIN_EMAILS="Svc-Sync@example.com,boss@example.com")
    item = check(changed(ready, jira_email="svc-sync@example.com"), "jira account", environ)
    assert grade(item) == "warn" and "admins" in item.detail and "svc-sync" not in item.detail.lower()


def test_a_blank_jira_email_has_no_account_check(ready: Settings) -> None:
    report = run_production_checks(changed(ready, jira_email=""), now=lambda: NOW, environ={})
    assert "jira account" not in [item.name for item in report.checks]
    assert grade(next(item for item in report.checks if item.name == "jira")) == "fail"


def test_a_missing_folder_is_a_warning(ready: Settings, app_folder: Path) -> None:
    (app_folder / "logs").rmdir()
    item = check(ready, "logs folder")
    assert grade(item) == "warn" and "does not exist" in item.detail


def test_a_folder_this_account_cannot_write_fails(ready: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(production.tempfile, "mkstemp", refused)
    for name in ("data folder", "logs folder"):
        item = check(ready, name)
        assert grade(item) == "fail" and "PermissionError" in item.detail and "DEPLOYMENT_IIS.md" in item.detail


def test_the_folder_check_leaves_nothing_behind(ready: Settings, app_folder: Path) -> None:
    assert grade(check(ready, "data folder")) == "ok" and grade(check(ready, "logs folder")) == "ok"
    assert list(ready.db_file.parent.glob(".doctor-*")) == []
    assert list((app_folder / "logs").iterdir()) == []


@pytest.mark.parametrize(("origins", "expected"), [
    ("", "warn"), ("http://localhost:3400", "warn"), ("http://oversight.example.com", "warn"),
    ("https://127.0.0.1:3400", "warn"), (ADDRESS, "ok"), (f"{ADDRESS},https://alt.example.com", "ok"),
])
def test_without_the_dashboards_file_the_cors_origins_stand_in_for_the_web_address(
    ready: Settings, origins: str, expected: str
) -> None:
    assert grade(check(changed(ready, cors_allow_origins=origins), "web address")) == expected


@pytest.mark.parametrize(("app_url", "expected"), [
    ("", "fail"), ("http://oversight.example.com", "fail"), ("https://localhost:3400", "fail"),
    ("https://oversight.example.com/", "fail"), ("https://oversight.example.com/app", "fail"),
    ("https://oversight.example.com?x=1", "fail"), ("https://oversight.example.com:port", "fail"),
    (ADDRESS, "ok"), ("https://oversight.example.com:8443", "ok"),
])
def test_app_url_must_be_an_https_origin_on_a_real_host(
    ready: Settings, tmp_path: Path, app_url: str, expected: str
) -> None:
    item = check(changed(ready, cors_allow_origins=app_url), "web address", dashboard_file(tmp_path, APP_URL=app_url))
    assert grade(item) == expected
    if expected == "ok":
        assert f"{app_url}/api/auth/callback" in item.detail


def test_app_url_and_cors_origins_must_agree_and_the_callback_is_named(ready: Settings, tmp_path: Path) -> None:
    item = check(ready, "web address", dashboard_file(tmp_path, APP_URL="https://other.example.com"))
    assert grade(item) == "warn" and "https://other.example.com/api/auth/callback" in item.detail


def test_the_dashboards_file_is_found_where_each_pack_keeps_it(ready: Settings, app_folder: Path) -> None:
    def names() -> list[str]:
        return [item.name for item in run_production_checks(ready, now=lambda: NOW, environ={}).checks]

    assert "sign-in" not in names()
    iis = app_folder.parent / "dashboard.env"
    iis.write_text("AUTH_MODE=atlassian\n", encoding="utf-8")  # the IIS pack's, above the backend's folder
    assert "sign-in" in names()
    iis.unlink()
    (app_folder / "deploy").mkdir()
    (app_folder / "deploy" / "dashboard.env").write_text("AUTH_MODE=atlassian\n", encoding="utf-8")  # the Docker pack's
    assert "sign-in" in names()


def test_a_named_file_that_is_missing_or_unreadable_is_a_warning_and_skips_the_dashboards_checks(
    ready: Settings, tmp_path: Path
) -> None:
    missing = check(ready, "dashboard", {production.DASHBOARD_ENV_VARIABLE: str(tmp_path / "nope.env")})
    assert grade(missing) == "warn" and "not a file" in missing.detail
    broken = tmp_path / "broken.env"
    broken.write_bytes(b"\x80\x81\x82 not utf-8")
    environ = {production.DASHBOARD_ENV_VARIABLE: str(broken)}
    unreadable = check(ready, "dashboard", environ)
    assert grade(unreadable) == "warn" and "cannot be read" in unreadable.detail
    report = run_production_checks(ready, now=lambda: NOW, environ=environ)
    assert "sign-in" not in [item.name for item in report.checks]


def test_the_env_file_is_read_as_node_reads_it(tmp_path: Path) -> None:
    path = tmp_path / "x.env"
    path.write_text(
        "# a comment\nPLAIN=value\nSPACED = value two  \nQUOTED=\"a # b\"\nSINGLE='x y'\nTRAILING=abc # note\n"
        "EMPTY=\nexport EXPORTED=yes\nNOEQUALS\n=nameless\nREPEATED=1\nREPEATED=2\n",
        encoding="utf-8",
    )
    assert read_env_file(path) == {
        "PLAIN": "value", "SPACED": "value two", "QUOTED": "a # b", "SINGLE": "x y", "TRAILING": "abc",
        "EMPTY": "", "EXPORTED": "yes", "REPEATED": "2",
    }


@pytest.mark.parametrize(("update", "expected"), [
    ({}, "ok"), ({"AUTH_MODE": "atlassian"}, "ok"), ({"AUTH_MODE": "off"}, "fail"),
    ({"ATLASSIAN_AUTH_URL": "http://localhost:4000"}, "fail"), ({"ATLASSIAN_API_URL": "http://localhost:4000"}, "fail"),
    ({"SAMPLE_NOW": "2026-11-04T14:30:00Z"}, "fail"), ({"OVERSIGHT_DATA": "sample"}, "fail"),
    ({"OVERSIGHT_DATA": "live"}, "ok"),
])
def test_sign_in_must_be_on_with_no_test_setting_left_in(
    ready: Settings, tmp_path: Path, update: dict[str, str], expected: str
) -> None:
    assert grade(check(ready, "sign-in", dashboard_file(tmp_path, **update))) == expected


@pytest.mark.parametrize(("secret", "expected", "said"), [
    ("", "fail", "blank"), ("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5", "fail", "shorter than 32"),
    ("ab" * 20, "fail", "repeated characters"), ("a" * 64, "fail", "repeated characters"),
    (KEY, "fail", "same as API_SERVICE_KEY"), (SECRET, "ok", "not the service key"),
])
def test_auth_secret_must_be_long_random_and_its_own(
    ready: Settings, tmp_path: Path, secret: str, expected: str, said: str
) -> None:
    item = check(ready, "auth secret", dashboard_file(tmp_path, AUTH_SECRET=secret))
    assert grade(item) == expected and said in item.detail
    assert not secret or secret not in item.detail


def test_the_dashboard_must_present_the_apis_key(ready: Settings, tmp_path: Path) -> None:
    assert grade(check(ready, "shared key", dashboard_file(tmp_path))) == "ok"
    blank = check(ready, "shared key", dashboard_file(tmp_path, OVERSIGHT_SERVICE_KEY=""))
    assert grade(blank) == "fail" and "blank" in blank.detail
    other = check(ready, "shared key", dashboard_file(tmp_path, OVERSIGHT_SERVICE_KEY=SECRET))
    assert grade(other) == "fail" and SECRET not in other.detail and KEY not in other.detail


@pytest.mark.parametrize(("update", "expected"), [
    ({}, "ok"), ({"ATLASSIAN_CLIENT_ID": ""}, "fail"), ({"ATLASSIAN_CLIENT_SECRET": ""}, "fail"),
    ({"OVERSIGHT_JIRA_BASE_URL": ""}, "fail"), ({"OVERSIGHT_JIRA_BASE_URL": "http://fake.atlassian.net"}, "fail"),
    ({"OVERSIGHT_JIRA_BASE_URL": "https://FAKE.atlassian.net/"}, "ok"),
    ({"OVERSIGHT_JIRA_BASE_URL": "https://other.atlassian.net"}, "warn"),
])
def test_the_atlassian_app_settings_must_be_complete(
    ready: Settings, tmp_path: Path, update: dict[str, str], expected: str
) -> None:
    assert grade(check(ready, "atlassian app", dashboard_file(tmp_path, **update))) == expected


@pytest.mark.parametrize(("domains", "expected"), [
    ("", "fail"), ("alice@example.com", "fail"), ("*.example.com", "fail"), ("example", "fail"),
    ("example.com", "ok"), ("example.com, Client.co.uk", "ok"),
])
def test_allowed_domains_must_be_set_and_be_domains(
    ready: Settings, tmp_path: Path, domains: str, expected: str
) -> None:
    item = check(ready, "allowed domains", dashboard_file(tmp_path, ALLOWED_EMAIL_DOMAINS=domains))
    assert grade(item) == expected and "alice" not in item.detail


def test_admins_are_reported_by_the_access_mode(ready: Settings, tmp_path: Path) -> None:
    everyone = check(ready, "admins", dashboard_file(tmp_path))
    assert grade(everyone) == "info" and "everyone who signs in is an admin" in everyone.detail
    assert grade(check(ready, "admins", dashboard_file(tmp_path, ACCESS_MODE="everyone-admin"))) == "info"
    none = check(ready, "admins", dashboard_file(tmp_path, ACCESS_MODE="admin-list"))
    assert grade(none) == "warn" and "ADMIN_EMAILS is blank" in none.detail
    outside = check(ready, "admins", dashboard_file(tmp_path, ACCESS_MODE="admin-list",
                                                   ADMIN_EMAILS="a@example.com,b@elsewhere.org"))
    assert grade(outside) == "warn" and "1 of 2" in outside.detail and "elsewhere" not in outside.detail
    listed = check(ready, "admins", dashboard_file(tmp_path, ACCESS_MODE="admin-list", ADMIN_EMAILS="a@example.com"))
    assert grade(listed) == "ok" and "1 admin" in listed.detail
    assert grade(check(ready, "admins", dashboard_file(tmp_path, ACCESS_MODE="nobody"))) == "warn"


def test_docs_a_blank_zone_and_console_logs_are_warnings(ready: Settings) -> None:
    report = run_production_checks(changed(ready, api_docs=True, report_timezone="", log_format="console"),
                                   now=lambda: NOW, environ={})
    graded = {item.name: grade(item) for item in report.checks}
    assert (graded["api docs"], graded["time zone"], graded["log format"]) == ("warn", "warn", "warn")
    assert report.ok  # warnings never fail the verdict
    assert report.render().splitlines()[-1] == "Verdict: PASS with 3 warnings"


def test_the_governance_settings_are_reported_as_set(ready: Settings) -> None:
    custom = changed(ready, people_comparison="off", ask_rate_limit="30/10m", llm_daily_token_budget=2_000_000,
                     access_log_days=90, sync_interval_minutes=30)
    details = {item.name: item.detail for item in run_production_checks(custom, now=lambda: NOW, environ={}).checks}
    assert details["people comparison"].startswith("off, so people are never compared")
    assert details["ask rate limit"].startswith("30 questions per viewer in any 10 minutes")
    assert details["model budget"].startswith("2,000,000 tokens per UTC day")
    assert details["access log"] == "entries kept 90 days (ACCESS_LOG_DAYS)"
    assert details["sync interval"].startswith("a cycle every 30 minutes")
    defaults = {item.name: item.detail for item in run_production_checks(ready, now=lambda: NOW, environ={}).checks}
    assert defaults["ask rate limit"].startswith("off, so no one is limited")
    assert defaults["people comparison"].startswith("on, so admins can compare")
    assert defaults["model budget"].startswith("no daily limit")
    for name in ("AUTH_MODE=atlassian", "APP_URL", "AUTH_SECRET", "ADMIN_EMAILS", "docs/DEPLOYMENT.md",
                 production.DASHBOARD_ENV_VARIABLE):
        assert name in defaults["dashboard"]


def no_jira(_settings: Settings) -> Any:
    raise AssertionError("doctor --production never calls Jira")


def test_the_command_passes_offline_and_names_no_secret(ready: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    live = changed(ready, llm_mode="live", anthropic_api_key="sk-ant-secret-value", smtp_password="smtp-secret")
    code = main(["doctor", "--production"], settings=live, client_factory=no_jira, now=lambda: NOW)
    out = capsys.readouterr()
    assert code == EXIT_OK
    for secret in (KEY, "test-token", "sk-ant-secret-value", "smtp-secret", "dev@example.com", "fake.atlassian.net"):
        assert secret not in out.out and secret not in out.err
    assert out.out.splitlines()[-1] == "Verdict: PASS"


def test_the_command_reads_the_dashboards_file_and_prints_none_of_its_secrets(
    ready: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in dashboard_file(tmp_path, ACCESS_MODE="admin-list", ADMIN_EMAILS="boss@example.com").items():
        monkeypatch.setenv(name, value)
    code = main(["doctor", "--production"], settings=ready, client_factory=no_jira, now=lambda: NOW)
    out = capsys.readouterr()
    assert code == EXIT_OK and out.out.splitlines()[-1] == "Verdict: PASS"
    for name in DASHBOARD_ORDER:
        assert f"] {name}:" in out.out
    for secret in (KEY, SECRET, "client-secret-value", "client-id-value", "boss@example.com", "fake.atlassian.net"):
        assert secret not in out.out and secret not in out.err


def test_the_command_fails_with_exit_1(ready: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["doctor", "--production"], settings=changed(ready, api_service_key="short"), client_factory=no_jira,
                now=lambda: NOW)
    printed = capsys.readouterr().out
    assert code == EXIT_CHECKS_FAILED
    assert "[FAIL] service key" in printed
    assert printed.splitlines()[-1].startswith(f"Verdict: FAIL (1 of {len(ORDER)} checks failed")


def test_the_command_takes_no_jira_options(ready: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    for extra in (["--project", "AIL"], ["--no-write-fields"]):
        assert main(["doctor", "--production", *extra], settings=ready, client_factory=no_jira) == EXIT_CONFIG
        assert "takes no --project" in capsys.readouterr().err


def test_info_lines_render_with_their_own_tag() -> None:
    report = DoctorReport(checks=[Check("a", True, "fine"), Check("b", True, "as set", info=True),
                                  Check("c", False, "look", warning=True)])
    assert report.render().splitlines() == ["[ OK ] a: fine", "[INFO] b: as set", "[WARN] c: look",
                                            "Verdict: PASS with 1 warning"]
