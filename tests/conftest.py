from __future__ import annotations

import os
import sqlite3
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog

from oversight.ask import analyst
from oversight.config import FieldsConfig, ProcessConfig
from oversight.ingest import avatars
from oversight.jira.client import JiraClient
from oversight.settings import PROJECT_ROOT, Settings, get_settings
from oversight.store.db import Store

Settings.model_config["env_file"] = None
get_settings.cache_clear()

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_FIXTURES = FIXTURES / "config"
JIRA_FIXTURES = FIXTURES / "jira"
REAL_DATA = (PROJECT_ROOT / "data").resolve()
VIEWER_HEADERS = {"X-Oversight-Viewer": "local", "X-Oversight-Viewer-Name": "Local%20%28sign-in%20off%29"}


class RealSnapshotOpened(AssertionError):
    """A test tried to open a file under data/."""


def into_real_data(database: object) -> str | None:
    if isinstance(database, (bytes, os.PathLike)):
        database = os.fsdecode(database)
    if not isinstance(database, str) or database in ("", ":memory:"):
        return None
    text = database
    if text.startswith("file:"):
        parsed = urllib.parse.urlsplit(text)
        if parsed.path in ("", ":memory:") or "mode=memory" in parsed.query:
            return None
        try:
            text = urllib.request.url2pathname(parsed.path)
        except OSError:
            return None
    try:
        resolved = Path(text).resolve()
    except (OSError, ValueError):
        return None
    return str(resolved) if resolved == REAL_DATA or REAL_DATA in resolved.parents else None


@pytest.fixture(autouse=True)
def _no_real_snapshot(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    refused: list[str] = []

    def check(database: object) -> None:
        found = into_real_data(database)
        if found is not None:
            refused.append(found)
            raise RealSnapshotOpened(f"tests never open the real snapshot or anything else under {REAL_DATA}")

    real_connect = sqlite3.connect

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        check(database)
        return real_connect(database, *args, **kwargs)

    real_open = Store.open.__func__  # type: ignore[attr-defined]

    def open_store(cls: type[Store], path: Path | str) -> Store:
        check(path)
        store: Store = real_open(cls, path)
        return store

    real_query = analyst.SnapshotQuery.__init__

    def snapshot_query(self: analyst.SnapshotQuery, db_path: Path | str, *args: Any, **kwargs: Any) -> None:
        check(db_path)
        real_query(self, db_path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(Store, "open", classmethod(open_store))
    monkeypatch.setattr(analyst.SnapshotQuery, "__init__", snapshot_query)
    yield refused
    assert not refused, f"a test tried to open the real snapshot {len(refused)} time(s)"


@pytest.fixture(autouse=True)
def _no_picture_from_the_internet(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    asked: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        raise httpx.ConnectError("tests never use the network", request=request)

    real = avatars.PictureFetcher.__init__

    def offline(self: avatars.PictureFetcher, *, transport: httpx.BaseTransport | None = None, **options: Any) -> None:
        real(self, transport=transport if transport is not None else httpx.MockTransport(refuse), **options)

    monkeypatch.setattr(avatars.PictureFetcher, "__init__", offline)
    yield asked
    assert not asked, f"a test asked the internet for {len(asked)} picture(s)"


@pytest.fixture(autouse=True)
def _no_jira_from_the_internet(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    asked: list[str] = []
    if request.node.get_closest_marker("live") is not None:
        yield asked
        return

    def refuse(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        raise httpx.ConnectError("tests never use the network", request=request)

    real = JiraClient.__init__

    def offline(self: JiraClient, *args: Any, transport: httpx.BaseTransport | None = None, **options: Any) -> None:
        real(self, *args, transport=transport if transport is not None else httpx.MockTransport(refuse), **options)

    monkeypatch.setattr(JiraClient, "__init__", offline)
    yield asked
    assert not asked, f"a test asked a Jira site on the internet {len(asked)} time(s)"


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


@pytest.fixture
def process() -> ProcessConfig:
    return ProcessConfig.load(CONFIG_FIXTURES / "process.yaml")


@pytest.fixture
def fields() -> FieldsConfig:
    return FieldsConfig.load(CONFIG_FIXTURES / "fields.yaml")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "oversight.db")
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("process.yaml", "fields.yaml", "baseline_expected.yaml", "rules.yaml"):
        source = CONFIG_FIXTURES / name
        if source.exists():
            (config_dir / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return Settings(
        jira_base_url="https://fake.atlassian.net",
        jira_email="dev@example.com",
        jira_api_token="test-token",
        jira_projects="AIL",
        db_path=tmp_path / "data" / "oversight.db",
        config_dir=config_dir,
        reports_dir=tmp_path / "reports",
        log_format="console",
    )
