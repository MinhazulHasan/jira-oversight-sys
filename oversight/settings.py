"""Typed settings, read from the environment and `.env`."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from oversight.errors import ConfigError

# Relative paths in settings resolve against the project folder, so the CLI
# behaves the same from Task Scheduler as from a terminal.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Project keys end up inside JQL, so anything else is rejected up front.
_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")

# The access log's retention (ACCESS_LOG_DAYS): a year unless an organisation sets otherwise, never under a month.
ACCESS_LOG_DAYS_DEFAULT = 365
ACCESS_LOG_DAYS_MIN = 30
# ASK_RATE_LIMIT: "<count>/<n>m", questions per viewer per n minutes.
_RATE_LIMIT = re.compile(r"^([1-9][0-9]{0,5})/([1-9][0-9]{0,3})m$")
RATE_LIMIT_MAX_MINUTES = 1440  # a day
# BACKUP_KEEP: how many of its own backups `python -m oversight backup` keeps (oversight/backup.py).
BACKUP_KEEP_DEFAULT = 14
# SYNC_INTERVAL_MINUTES: the scheduled cycle's interval (`python -m oversight cycle --loop`, oversight/cycle.py).
# Never more often than every 5 minutes, which spares the Jira site; at most once a day.
SYNC_INTERVAL_DEFAULT = 60
SYNC_INTERVAL_MIN = 5
SYNC_INTERVAL_MAX = 1440


def validate_project_keys(keys: list[str]) -> list[str]:
    """Upper-case and check project keys; raise ValueError naming the bad ones."""
    keys = [key.strip().upper() for key in keys if key.strip()]
    bad = [key for key in keys if not _PROJECT_KEY.match(key)]
    if bad:
        raise ValueError(f"invalid Jira project keys: {', '.join(bad)}")
    return keys


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- jira (read-only) ---------------------------------------------------
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: SecretStr = SecretStr("")
    # The boards the scheduled cycle always keeps in sync; the first is the dashboard's default board.
    jira_projects: Annotated[list[str], NoDecode] = ["AIL"]
    # JO-17. on: the service serves every Jira board (project) its Jira account can browse, found by a read-only
    # project search (oversight/boards.py), besides JIRA_PROJECTS. off: JIRA_PROJECTS only.
    board_discovery: Literal["on", "off"] = "on"
    jira_timeout_s: float = 30.0
    jira_connect_timeout_s: float = 5.0
    # Attempts per request, counting the first; only 429, 5xx and network errors are retried.
    jira_max_attempts: int = 4
    jira_page_size: int = 100

    # --- storage and files --------------------------------------------------
    db_path: Path = Path("data/oversight.db")
    config_dir: Path = Path("config")
    reports_dir: Path = Path("reports")

    # --- reporting ----------------------------------------------------------
    # The organisation's reporting time zone, an IANA name such as
    # America/Chicago: report days, digest dates and the insights week are cut
    # in it, and every API response names it. Blank: UTC. Never taken from a
    # Jira profile: the sync account's zone only writes JQL dates.
    report_timezone: str = ""

    # --- llm (Phase 6 onwards) ----------------------------------------------
    # stub: the scripted provider answers from the data with no API key (demos,
    # CI). live: the configured provider.
    llm_mode: Literal["stub", "live"] = "stub"
    # anthropic: the Claude API with ANTHROPIC_API_KEY. bedrock: Claude on Amazon
    # Bedrock, signed with the AWS settings below.
    llm_provider: Literal["anthropic", "bedrock"] = "anthropic"
    anthropic_api_key: SecretStr = SecretStr("")
    # Amazon Bedrock. Leave the keys and profile blank on an AWS host to use its
    # role through the standard AWS credential chain.
    aws_region: str = ""
    aws_access_key_id: SecretStr = SecretStr("")
    aws_secret_access_key: SecretStr = SecretStr("")
    aws_session_token: SecretStr = SecretStr("")
    aws_profile: str = ""
    llm_model_qa: str = "claude-sonnet-5-5"
    llm_model_insights: str = "claude-opus-5-5"
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    # Server-side refusal fallbacks ("default" routing) on text calls.
    llm_fallbacks: bool = True
    # The plan may make at most this many catalogue calls; each returns at most this many rows.
    ask_max_calls: int = 3
    ask_row_limit: int = 200
    # analyst: with a live model, Claude answers any question by querying the snapshot
    # (read-only views, several steps). catalog: only the fixed lookups. LLM_MODE=stub
    # always uses the catalogue: the scripted provider cannot hold a conversation.
    ask_mode: Literal["analyst", "catalog"] = "analyst"
    # The analyst's tool turns per question (each may run several queries).
    ask_max_steps: int = 6

    # --- notifications (Phase 5) --------------------------------------------
    # file: writes the digest under reports/digests (works out of the box).
    digest_channel: Literal["file", "email", "teams"] = "file"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    # Comma-separated recipients.
    digest_to: Annotated[list[str], NoDecode] = []
    teams_webhook_url: SecretStr = SecretStr("")

    # --- dashboard api (Phase 8) --------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 3300
    # run.py only: restart the API when a source file changes (development, never production).
    api_reload: bool = False
    # Serve interactive docs at /docs (Authorize with the service key). Local use only.
    api_docs: bool = False
    # The UI's server route presents this key; blank is refused unless
    # API_ALLOW_INSECURE=true, so an open API is always a choice somebody made.
    api_service_key: SecretStr = SecretStr("")
    api_allow_insecure: bool = False
    cors_allow_origins: str = "http://localhost:3400"
    # Person scorecards (JO-11): comparing people (the everyone table, several people side by side) is on unless
    # an organisation turns it off; off, the API and Ask refuse any comparison, whatever the browser sends.
    # Anything but "on" or "off" is a config error at start.
    people_comparison: Literal["on", "off"] = "on"

    # --- governance (JO-12) -------------------------------------------------
    # The access log (who viewed whose person-level data, oversight/api/access_log.py) keeps an entry this many days,
    # then deletes it: at the API's start and once a day. At least 30.
    access_log_days: int = ACCESS_LOG_DAYS_DEFAULT
    # Questions each viewer may ask (POST /ask) in a window: "off" (the default: an admin is never limited unless the
    # organisation chooses to), or "<count>/<n>m", such as 30/10m. Counted in memory, per process.
    ask_rate_limit: str = "off"
    # Model tokens (input + output) per UTC day, across Ask, the weekly insights and the rule drafts (JO-20), counted
    # from llm_calls. 0 (the default) is no limit. Once reached, Ask answers without the model, insights skip and a
    # rule draft is not offered, until 00:00 UTC.
    llm_daily_token_budget: int = 0

    # --- deployment (JO-12) -------------------------------------------------
    # `python -m oversight backup` keeps this many of the newest backups it made (in the backups folder beside the
    # snapshot) and deletes its older ones; any other file there is left alone. At least 1.
    backup_keep: int = BACKUP_KEEP_DEFAULT
    # `python -m oversight cycle --loop` (the deployment's sync service) starts a cycle this many minutes after the
    # last one started: 5 to 1440.
    sync_interval_minutes: int = SYNC_INTERVAL_DEFAULT

    # --- demo data ----------------------------------------------------------
    demo_seed: int = 20261104

    # --- logging ------------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    @field_validator("jira_projects", mode="before")
    @classmethod
    def _split_projects(cls, value: object) -> object:
        if isinstance(value, str):
            return [p.strip().upper() for p in value.split(",") if p.strip()]
        return value

    @field_validator("jira_projects")
    @classmethod
    def _check_project_keys(cls, value: list[str]) -> list[str]:
        return validate_project_keys(value)

    @field_validator("digest_to", mode="before")
    @classmethod
    def _split_recipients(cls, value: object) -> object:
        if isinstance(value, str):
            return [r.strip() for r in value.split(",") if r.strip()]
        return value

    @field_validator("jira_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("report_timezone")
    @classmethod
    def _check_report_timezone(cls, value: str) -> str:
        value = value.strip()
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                raise ValueError(
                    f"REPORT_TIMEZONE must be an IANA time zone name such as America/Chicago, not {value!r}"
                ) from None
        return value

    @field_validator("access_log_days")
    @classmethod
    def _check_access_log_days(cls, value: int) -> int:
        if value < ACCESS_LOG_DAYS_MIN:
            raise ValueError(
                f"ACCESS_LOG_DAYS must be at least {ACCESS_LOG_DAYS_MIN} (the days the access log is kept), not {value}"
            )
        return value

    @field_validator("ask_rate_limit")
    @classmethod
    def _check_ask_rate_limit(cls, value: str) -> str:
        text = value.strip().lower()
        if text == "off":
            return text
        found = _RATE_LIMIT.fullmatch(text)
        if found is None or int(found.group(2)) > RATE_LIMIT_MAX_MINUTES:
            raise ValueError(
                f"ASK_RATE_LIMIT must be off or <count>/<n>m with n at most {RATE_LIMIT_MAX_MINUTES}, such as 30/10m "
                f"(30 questions per viewer in any 10 minutes), not {value!r}"
            )
        return text

    @field_validator("llm_daily_token_budget")
    @classmethod
    def _check_token_budget(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"LLM_DAILY_TOKEN_BUDGET must be 0 (no limit) or a number of tokens, not {value}")
        return value

    @field_validator("backup_keep")
    @classmethod
    def _check_backup_keep(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"BACKUP_KEEP must be at least 1 (the newest backups kept), not {value}")
        return value

    @field_validator("sync_interval_minutes")
    @classmethod
    def _check_sync_interval(cls, value: int) -> int:
        if not SYNC_INTERVAL_MIN <= value <= SYNC_INTERVAL_MAX:
            raise ValueError(
                f"SYNC_INTERVAL_MINUTES must be {SYNC_INTERVAL_MIN} to {SYNC_INTERVAL_MAX} (minutes between two "
                f"cycles), not {value}"
            )
        return value

    @property
    def ask_rate_limit_rule(self) -> tuple[int, int] | None:
        """ASK_RATE_LIMIT as (questions, window in seconds); None when off."""
        found = _RATE_LIMIT.fullmatch(self.ask_rate_limit)
        return None if found is None else (int(found.group(1)), int(found.group(2)) * 60)

    @property
    def discovers_boards(self) -> bool:
        """BOARD_DISCOVERY is on: every board the Jira account can browse is served, not only JIRA_PROJECTS."""
        return self.board_discovery == "on"

    @property
    def jira_configured(self) -> bool:
        """Whether require_jira() would pass: a Jira call can be made at all."""
        try:
            self.require_jira()
        except ConfigError:
            return False
        return True

    @property
    def report_zone(self) -> str:
        """The zone reports are cut in: REPORT_TIMEZONE, else UTC."""
        return self.report_timezone or "UTC"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def rules_file(self) -> Path:
        return self.resolve(self.config_dir) / "rules.yaml"

    @property
    def digests_path(self) -> Path:
        return self.reports_path / "digests"

    def require_llm(self) -> None:
        """Fail early before any LLM call in live mode."""
        if self.llm_mode != "live":
            return
        if self.llm_provider == "anthropic":
            if not self.anthropic_api_key.get_secret_value():
                raise ConfigError("LLM_MODE=live needs ANTHROPIC_API_KEY (or set LLM_MODE=stub)")
            return
        if not self.aws_region.strip():
            raise ConfigError("LLM_PROVIDER=bedrock needs AWS_REGION, for example us-east-1")
        has_key = bool(self.aws_access_key_id.get_secret_value().strip())
        has_secret = bool(self.aws_secret_access_key.get_secret_value().strip())
        if has_key != has_secret:
            raise ConfigError(
                "LLM_PROVIDER=bedrock needs both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, "
                "or neither to use the AWS credential chain (AWS_PROFILE or the host's role)"
            )

    def require_api_auth(self) -> None:
        """An unauthenticated API must be an explicit, visible choice."""
        if not self.api_service_key.get_secret_value() and not self.api_allow_insecure:
            raise ConfigError(
                "API_SERVICE_KEY is blank: set one (python -c \"import secrets; print(secrets.token_urlsafe(32))\") "
                "or set API_ALLOW_INSECURE=true for a local demo"
            )

    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def db_file(self) -> Path:
        return self.resolve(self.db_path)

    @property
    def backups_path(self) -> Path:
        """Where `python -m oversight backup` writes: a backups folder beside the snapshot (data/backups)."""
        return self.db_file.parent / "backups"

    @property
    def process_file(self) -> Path:
        return self.resolve(self.config_dir) / "process.yaml"

    @property
    def fields_file(self) -> Path:
        return self.resolve(self.config_dir) / "fields.yaml"

    @property
    def baseline_expected_file(self) -> Path:
        return self.resolve(self.config_dir) / "baseline_expected.yaml"

    @property
    def reports_path(self) -> Path:
        return self.resolve(self.reports_dir)

    def require_jira(self) -> None:
        """Fail early, naming every missing variable, before any Jira call."""
        missing = [
            name
            for name, value in (
                ("JIRA_BASE_URL", self.jira_base_url),
                ("JIRA_EMAIL", self.jira_email),
                ("JIRA_API_TOKEN", self.jira_api_token.get_secret_value()),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"missing Jira settings: {', '.join(missing)} "
                "(copy .env.example to .env and fill them in)"
            )
        # Every request carries the API token; never send it unencrypted. (The
        # value isn't echoed: a mistyped URL may hold credentials.)
        if not self.jira_base_url.lower().startswith("https://"):
            raise ConfigError(
                "JIRA_BASE_URL must start with https:// (e.g. https://<site>.atlassian.net): "
                "every request carries the API token"
            )
        if not self.jira_projects:
            raise ConfigError("JIRA_PROJECTS is empty")


@lru_cache
def get_settings() -> Settings:
    return Settings()
