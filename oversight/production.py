"""`python -m oversight doctor --production`: is this deployment ready to serve the client?"""
from __future__ import annotations

import hmac
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from oversight.backup import list_backups
from oversight.config import FieldsConfig, ProcessConfig
from oversight.doctor import Check, DoctorReport
from oversight.errors import ConfigError
from oversight.logs import get_logger
from oversight.rules.catalog import RulesConfig
from oversight.settings import PROJECT_ROOT, Settings
from oversight.store.db import SCHEMA_VERSION, open_readonly
from oversight.timeutil import utcnow

# The same floor as the dashboard's AUTH_SECRET.
MIN_KEY_LENGTH = 32
MIN_DISTINCT_CHARACTERS = 10
# A full sync may run long before the day's backup follows it.
BACKUP_MARGIN = timedelta(hours=1)
_DETAIL_LIMIT = 300


def backup_max_age(settings: Settings) -> timedelta:
    return timedelta(days=1, minutes=settings.sync_interval_minutes) + BACKUP_MARGIN


# The environment variable that points the doctor at the dashboard's env file.
DASHBOARD_ENV_VARIABLE = "DASHBOARD_ENV_FILE"
DASHBOARD_NOTE = (
    "its settings are not read here; check sign-in (AUTH_MODE=atlassian), APP_URL (https), AUTH_SECRET (at least "
    "32 characters) and ADMIN_EMAILS against docs/DEPLOYMENT.md, \"Production checklist\", or set "
    f"{DASHBOARD_ENV_VARIABLE} to its settings file and run this again"
)

# A word in an address that names a service, not a person.
_SERVICE_WORDS = ("svc", "service", "bot", "readonly", "read-only", "read_only", "reader", "oversight", "jira",
                  "integration", "automation", "noreply", "no-reply", "system", "robot", "sync")
_FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com", "me.com",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.net", "mail.com",
})
_PERSON_NAME = re.compile(r"[a-z]{2,}[._-][a-z]{2,}")
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def run_production_checks(
    settings: Settings, *, now: Callable[[], datetime] = utcnow, environ: Mapping[str, str] | None = None
) -> DoctorReport:
    """Every production check, in order."""
    report = DoctorReport()
    add = report.checks.append
    dashboard = _read_dashboard(os.environ if environ is None else environ)
    values = dashboard.values
    add(_service_key(settings))
    add(_jira(settings))
    account = _jira_account(settings, values)
    if account is not None:
        add(account)
    add(_config(settings))
    add(_model(settings))
    add(_snapshot(settings))
    add(_backups(settings, now()))
    add(_folder("data folder", settings.db_file.parent, "the first sync makes it"))
    add(_folder("logs folder", PROJECT_ROOT / "logs", "create it: the scheduled jobs and IIS write their logs there"))
    add(_warn_if(settings.api_docs, "api docs", "API_DOCS=true serves /docs and /openapi.json without the key: "
                 "set it to false", "off"))
    add(_warn_if(not settings.report_timezone, "time zone", "REPORT_TIMEZONE is blank, so days are cut in UTC: "
                 "set the organisation's zone, such as Europe/London or America/New_York",
                 settings.report_timezone))
    add(_warn_if(settings.log_format != "json", "log format", f"LOG_FORMAT={settings.log_format}: set json, which "
                 "log collectors read", "json"))
    add(_web_address(settings, values))
    if values is not None:
        add(_sign_in(values))
        add(_auth_secret(settings, values))
        add(_shared_key(settings, values))
        add(_atlassian_app(settings, values))
        add(_allowed_domains(values))
        add(_admins(values))
    for name, detail in _settings_as_set(settings):
        add(Check(name, True, detail, info=True))
    add(dashboard.note)
    get_logger("doctor").info(
        "doctor_production_finished",
        ok=report.ok,
        failed=[check.name for check in report.checks if not check.ok and not check.warning],
        warnings=[check.name for check in report.checks if not check.ok and check.warning],
    )
    return report


# --- the checks ---------------------------------------------------------------------------------------------------


def _service_key(settings: Settings) -> Check:
    name = "service key"
    key = settings.api_service_key.get_secret_value()
    if not key:
        if settings.api_allow_insecure:
            return _fail(name, "API_SERVICE_KEY is blank and API_ALLOW_INSECURE=true, so the API answers anyone: "
                               "set a key and remove API_ALLOW_INSECURE")
        return _fail(name, "API_SERVICE_KEY is blank, so the API will not start: set one")
    if len(key) < MIN_KEY_LENGTH:
        return _fail(name, f"API_SERVICE_KEY is shorter than {MIN_KEY_LENGTH} characters: set a longer one, for "
                           "example python -c \"import secrets; print(secrets.token_urlsafe(48))\"")
    if _repeated(key):
        return _fail(name, "API_SERVICE_KEY is made of a few repeated characters: set a random one, for example "
                           "python -c \"import secrets; print(secrets.token_urlsafe(48))\"")
    if settings.api_allow_insecure:
        return _warn(name, "set; API_ALLOW_INSECURE=true has no effect while a key is set: remove it")
    return _ok(name, f"set, at least {MIN_KEY_LENGTH} characters; the dashboard must present the same key")


def _jira(settings: Settings) -> Check:
    name = "jira"
    try:
        settings.require_jira()
    except ConfigError as exc:
        return _fail(name, str(exc))
    return _ok(name, f"JIRA_BASE_URL is https, JIRA_EMAIL and JIRA_API_TOKEN are set; boards "
                     f"{', '.join(settings.jira_projects)}")


def _config(settings: Settings) -> Check:
    name = "config"
    try:
        process = ProcessConfig.load(settings.process_file)
        RulesConfig.load(settings.rules_file)
        if not settings.fields_file.exists():
            return _fail(name, f"{settings.fields_file.name} is missing: run `python -m oversight doctor` once, "
                               "which writes it")
        FieldsConfig.load(settings.fields_file)
    except ConfigError as exc:
        return _fail(name, str(exc))
    loaded = f"process.yaml, rules.yaml and fields.yaml load from {settings.resolve(settings.config_dir)}"
    unmapped = [key for key in settings.jira_projects if key not in process.projects]
    if unmapped:
        return _warn(name, f"{loaded}; not in process.yaml, so mapped from Jira's status categories: "
                           f"{', '.join(unmapped)} (add an entry to measure code review, testing or sign-off)")
    return _ok(name, f"{loaded}; every board in JIRA_PROJECTS has its own process.yaml entry")


def _model(settings: Settings) -> Check:
    name = "model"
    if settings.llm_mode != "live":
        return _info(name, "LLM_MODE=stub, so Ask and the weekly insights are built in code and no model is called")
    try:
        settings.require_llm()
    except ConfigError as exc:
        return _fail(name, str(exc))
    where = f" in {settings.aws_region}" if settings.llm_provider == "bedrock" else ""
    detail = (f"LLM_MODE=live through {settings.llm_provider}{where}: Ask uses {settings.llm_model_qa}, the insights "
              f"{settings.llm_model_insights}")
    if settings.llm_provider == "bedrock":
        keys = bool(settings.aws_access_key_id.get_secret_value().strip())
        if not keys and not settings.aws_profile.strip():
            return _warn(name, f"{detail}; no AWS keys or profile are set, so the host's role must supply them: on "
                               "EC2, set the instance-metadata hop limit to 2 or more, or a container cannot reach "
                               "the role and every model call fails")
    return _ok(name, detail)


def _snapshot(settings: Settings) -> Check:
    name = "snapshot"
    path = settings.db_file
    if not path.exists():
        return _warn(name, f"none yet at {path}: the first sync makes it")
    try:
        conn = open_readonly(path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _fail(name, f"{path} cannot be read: {exc}")
    if version > SCHEMA_VERSION:
        return _fail(name, f"schema version {version} is from a newer release than this one ({SCHEMA_VERSION}): "
                           "run the release that wrote it, or restore an older backup")
    if version < SCHEMA_VERSION:
        return _info(name, f"schema version {version}: the API brings it to {SCHEMA_VERSION} when it starts "
                           "(take a backup first)")
    return _ok(name, f"schema version {version} at {path}")


def _backups(settings: Settings, now: datetime) -> Check:
    name = "backups"
    folder = settings.backups_path
    backups = list_backups(folder)
    if not backups:
        return _warn(name, f"none yet in {folder}: run `python -m oversight backup` (the sync service makes one a "
                           "day)")
    newest = backups[-1]
    age = now - newest.taken_at
    if age > backup_max_age(settings):
        return _warn(name, f"the newest, {newest.path.name}, is {_age(age)} old, though the sync service makes one "
                           "each UTC day: check it, or run `python -m oversight backup`")
    return _ok(name, f"the newest, {newest.path.name}, is {_age(age)} old; {len(backups)} kept "
                     f"(BACKUP_KEEP={settings.backup_keep}) in {folder}")


def _settings_as_set(settings: Settings) -> list[tuple[str, str]]:
    rule = settings.ask_rate_limit_rule
    if rule is None:
        rate = "off, so no one is limited (ASK_RATE_LIMIT)"
    else:
        count, seconds = rule
        rate = (f"{count} questions per viewer in any {seconds // 60} minutes (ASK_RATE_LIMIT), counted in each API "
                "process's memory")
    budget = settings.llm_daily_token_budget
    return [
        ("people comparison", "on, so admins can compare people side by side (PEOPLE_COMPARISON)"
         if settings.people_comparison == "on" else "off, so people are never compared side by side (PEOPLE_COMPARISON)"),
        ("ask rate limit", rate),
        ("model budget", "no daily limit (LLM_DAILY_TOKEN_BUDGET=0)" if budget == 0 else
         f"{budget:,} tokens per UTC day across Ask and the insights (LLM_DAILY_TOKEN_BUDGET)"),
        ("access log", f"entries kept {settings.access_log_days} days (ACCESS_LOG_DAYS)"),
        ("sync interval", f"a cycle every {settings.sync_interval_minutes} minutes (SYNC_INTERVAL_MINUTES, the sync "
                          "service)"),
    ]


def _jira_account(settings: Settings, dashboard: dict[str, str] | None) -> Check | None:
    """Whether JIRA_EMAIL is a dedicated account, as far as its address can tell."""
    name = "jira account"
    email = settings.jira_email.strip().lower()
    if not email:
        return None
    local, _, domain = email.rpartition("@")
    admins = {item.strip().lower() for item in (dashboard or {}).get("ADMIN_EMAILS", "").split(",") if item.strip()}
    if email in admins:
        return _warn(name, "JIRA_EMAIL is also one of the dashboard's admins, so it is a person's account: use a "
                           "dedicated read-only account, so the sync never depends on one person")
    if domain in _FREE_MAIL:
        return _warn(name, "JIRA_EMAIL is on a free mail provider, so it is a personal account: use a dedicated "
                           "read-only account on the client's own domain")
    if any(word in local for word in _SERVICE_WORDS):
        return _ok(name, "JIRA_EMAIL looks like a service account; check it has Browse Projects and nothing more")
    if _PERSON_NAME.fullmatch(local):
        return _warn(name, "JIRA_EMAIL looks like a person's address (first and last name): use a dedicated "
                           "read-only account, so the sync never depends on one person")
    return _info(name, "the address does not say whether JIRA_EMAIL is a dedicated read-only account: check that "
                       "it is, and that it has Browse Projects and nothing more")


def _folder(name: str, folder: Path, missing: str) -> Check:
    if not folder.is_dir():
        return _warn(name, f"{folder} does not exist: {missing}")
    try:
        handle, temporary = tempfile.mkstemp(prefix=".doctor-", dir=folder)
    except OSError as exc:
        return _fail(name, f"{folder} cannot be written to by this account ({type(exc).__name__}): give it the "
                           "permissions docs/DEPLOYMENT_IIS.md lists")
    os.close(handle)
    try:
        os.unlink(temporary)
    except OSError:
        pass  # an empty file named .doctor-*: harmless, and the next run's check proves the folder is writable
    return _ok(name, f"{folder} is writable by the account running this check")


def _web_address(settings: Settings, dashboard: dict[str, str] | None) -> Check:
    name = "web address"
    origins = settings.cors_origin_list
    if dashboard is None:
        if not origins:
            return _warn(name, "CORS_ALLOW_ORIGINS is blank: set it to the https web address people open")
        for origin in origins:
            problem = _origin_problem(origin)
            if problem:
                return _warn(name, f"CORS_ALLOW_ORIGINS has {problem}: set it to the https web address people open")
        return _ok(name, f"CORS_ALLOW_ORIGINS is {', '.join(origins)}")
    app_url = dashboard.get("APP_URL", "").strip()
    if not app_url:
        return _fail(name, "APP_URL is blank in the dashboard's settings, so nobody can sign in: set the https "
                           "address people open")
    problem = _origin_problem(app_url)
    if problem:
        return _fail(name, f"APP_URL is {problem}: set the https address people open, origin only, such as "
                           "https://oversight.example.com")
    callback = f"{app_url}/api/auth/callback"
    if app_url not in origins:
        return _warn(name, f"APP_URL is {app_url} but CORS_ALLOW_ORIGINS does not list it: set it to the same "
                           f"address. The Atlassian app's callback URL must be exactly {callback}")
    return _ok(name, f"APP_URL is {app_url}; the Atlassian app's callback URL must be exactly {callback} "
                     "(only the Atlassian console can confirm it)")


def _origin_problem(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        _ = parts.port  # raises for a port that is not a number
    except ValueError:
        return "not a web address"
    if parts.scheme != "https":
        return "not https"
    local = host in ("localhost", "0.0.0.0") or host.startswith("127.") or host.endswith((".local", ".localhost"))
    if not host or local:
        return "a local address, not the one people open"
    if parts.path or parts.query or parts.fragment or parts.username is not None or value.endswith("/"):
        return "more than the origin (a path, a query or a trailing slash)"
    return ""


def _sign_in(dashboard: dict[str, str]) -> Check:
    name = "sign-in"
    mode = dashboard.get("AUTH_MODE", "").strip().lower()
    if mode not in ("", "atlassian"):
        return _fail(name, f"AUTH_MODE={mode} turns sign-in off: leave it empty or atlassian")
    left_in = [key for key in ("ATLASSIAN_AUTH_URL", "ATLASSIAN_API_URL", "SAMPLE_NOW", "SAMPLE_LATENCY",
                               "SAMPLE_PEOPLE_COMPARISON") if dashboard.get(key, "").strip()]
    if left_in:
        return _fail(name, f"{', '.join(left_in)} is for tests and the sample only: leave it empty")
    data = dashboard.get("OVERSIGHT_DATA", "").strip().lower()
    if data not in ("", "live"):
        return _fail(name, f"OVERSIGHT_DATA={data} serves the bundled sample, not the client's figures: leave it "
                           "empty or live")
    return _ok(name, "Atlassian sign-in is on and no test setting is left in")


def _auth_secret(settings: Settings, dashboard: dict[str, str]) -> Check:
    name = "auth secret"
    secret = dashboard.get("AUTH_SECRET", "")
    if not secret:
        return _fail(name, "AUTH_SECRET is blank in the dashboard's settings, so nobody can sign in: set 64 random "
                           "characters")
    if len(secret) < MIN_KEY_LENGTH:
        return _fail(name, f"AUTH_SECRET is shorter than {MIN_KEY_LENGTH} characters: set a longer one, for example "
                           "python -c \"import secrets; print(secrets.token_hex(32))\"")
    if _repeated(secret):
        return _fail(name, "AUTH_SECRET is made of a few repeated characters: set a random one, for example "
                           "python -c \"import secrets; print(secrets.token_hex(32))\"")
    if hmac.compare_digest(secret.encode(), settings.api_service_key.get_secret_value().encode()):
        return _fail(name, "AUTH_SECRET is the same as API_SERVICE_KEY: each secret is used nowhere else")
    return _ok(name, f"set, at least {MIN_KEY_LENGTH} characters, and not the service key")


def _shared_key(settings: Settings, dashboard: dict[str, str]) -> Check:
    name = "shared key"
    theirs = dashboard.get("OVERSIGHT_SERVICE_KEY", "")
    ours = settings.api_service_key.get_secret_value()
    if not theirs:
        return _fail(name, "OVERSIGHT_SERVICE_KEY is blank in the dashboard's settings, so every page fails: set it "
                           "to API_SERVICE_KEY")
    if not hmac.compare_digest(theirs.encode(), ours.encode()):
        return _fail(name, "OVERSIGHT_SERVICE_KEY is not the same as API_SERVICE_KEY, so the API refuses the "
                           "dashboard and every page fails: set them equal")
    return _ok(name, "the dashboard presents the API's service key")


def _atlassian_app(settings: Settings, dashboard: dict[str, str]) -> Check:
    name = "atlassian app"
    missing = [key for key in ("ATLASSIAN_CLIENT_ID", "ATLASSIAN_CLIENT_SECRET", "OVERSIGHT_JIRA_BASE_URL")
               if not dashboard.get(key, "").strip()]
    if missing:
        return _fail(name, f"{', '.join(missing)} is blank in the dashboard's settings, so nobody can sign in")
    site = dashboard["OVERSIGHT_JIRA_BASE_URL"].strip().rstrip("/")
    if not site.lower().startswith("https://"):
        return _fail(name, "OVERSIGHT_JIRA_BASE_URL must start with https://")
    if settings.jira_base_url and site.lower() != settings.jira_base_url.lower():
        return _warn(name, "OVERSIGHT_JIRA_BASE_URL is not the same site as JIRA_BASE_URL: the dashboard and the "
                           "sync should read one Jira site")
    return _ok(name, "client id, client secret and the Jira site are set; the app must carry only read:me and "
                     "read:jira-work, with sharing on")


def _allowed_domains(dashboard: dict[str, str]) -> Check:
    name = "allowed domains"
    domains = [item.strip().lower() for item in dashboard.get("ALLOWED_EMAIL_DOMAINS", "").split(",") if item.strip()]
    if not domains:
        return _fail(name, "ALLOWED_EMAIL_DOMAINS is blank, so nobody can sign in: set the client's email domains")
    bad = [item for item in domains if not _DOMAIN.fullmatch(item)]
    if bad:
        return _fail(name, f"{len(bad)} of {len(domains)} entries in ALLOWED_EMAIL_DOMAINS are not a domain such as "
                           "example.com (no @, no wildcard, no spaces)")
    return _ok(name, f"{len(domains)} domain{'s' if len(domains) != 1 else ''} allowed to sign in")


def _admins(dashboard: dict[str, str]) -> Check:
    name = "admins"
    mode = dashboard.get("ACCESS_MODE", "").strip().lower() or "everyone-admin"
    if mode == "everyone-admin":
        return _info(name, "ACCESS_MODE=everyone-admin (the default): everyone who signs in is an admin and sees "
                           "every board and every person's figures")
    if mode != "admin-list":
        return _warn(name, f"ACCESS_MODE={mode} is neither everyone-admin nor admin-list")
    admins = [item.strip().lower() for item in dashboard.get("ADMIN_EMAILS", "").split(",") if item.strip()]
    if not admins:
        return _warn(name, "ACCESS_MODE=admin-list but ADMIN_EMAILS is blank, so nobody is an admin and nobody sees "
                           "people's figures")
    domains = {item.strip().lower() for item in dashboard.get("ALLOWED_EMAIL_DOMAINS", "").split(",") if item.strip()}
    outside = [item for item in admins if item.rpartition("@")[2] not in domains]
    if outside:
        return _warn(name, f"{len(outside)} of {len(admins)} entries in ADMIN_EMAILS are not on an allowed domain, "
                           "so they can never sign in")
    return _ok(name, f"ACCESS_MODE=admin-list with {len(admins)} admin{'s' if len(admins) != 1 else ''}")


# --- helpers ------------------------------------------------------------------------------------------------------


class _Dashboard:

    def __init__(self, values: dict[str, str] | None, note: Check) -> None:
        self.values = values
        self.note = note


def _read_dashboard(environ: Mapping[str, str]) -> _Dashboard:
    named = environ.get(DASHBOARD_ENV_VARIABLE, "").strip()
    if named:
        candidates = [Path(named)]
    else:
        candidates = [PROJECT_ROOT / "deploy" / "dashboard.env", PROJECT_ROOT.parent / "dashboard.env"]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        if named:
            return _Dashboard(None, _warn("dashboard", f"{DASHBOARD_ENV_VARIABLE} names {named}, which is not a "
                                                       "file: its settings were not checked"))
        return _Dashboard(None, _info("dashboard", DASHBOARD_NOTE))
    try:
        values = read_env_file(path)
    except (OSError, UnicodeDecodeError):
        return _Dashboard(None, _warn("dashboard", f"{path} cannot be read by this account: its settings were not "
                                                   "checked"))
    return _Dashboard(values, _info("dashboard", f"its settings were read from {path} and checked above (nothing "
                                                 "from it is printed); the Atlassian app itself is checked in the "
                                                 "Atlassian console"))


def read_env_file(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        name, equals, value = line.partition("=")
        name = name.strip()
        if not equals or not name:
            continue
        value = value.strip()
        if value[:1] in ("'", '"', "`") and value.find(value[0], 1) > 0:
            value = value[1 : value.find(value[0], 1)]
        else:
            value = value.partition("#")[0].strip()
        found[name] = value
    return found


def _repeated(secret: str) -> bool:
    return len(set(secret)) < MIN_DISTINCT_CHARACTERS


def _age(age: timedelta) -> str:
    hours = int(age.total_seconds() // 3600)
    if hours < 1:
        return "under an hour"
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours // 24} days"


def _warn_if(condition: bool, name: str, problem: str, fine: str) -> Check:
    return _warn(name, problem) if condition else _ok(name, fine)


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= _DETAIL_LIMIT else line[: _DETAIL_LIMIT - 3] + "..."


def _ok(name: str, detail: str) -> Check:
    return Check(name, True, _one_line(detail))


def _info(name: str, detail: str) -> Check:
    return Check(name, True, _one_line(detail), info=True)


def _warn(name: str, detail: str) -> Check:
    return Check(name, False, _one_line(detail), warning=True)


def _fail(name: str, detail: str) -> Check:
    return Check(name, False, _one_line(detail))
