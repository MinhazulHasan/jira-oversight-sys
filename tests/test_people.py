from __future__ import annotations

import hashlib
import re
import runpy
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oversight.api.people import UNNAMED, avatar, person, person_id, person_name
from oversight.ask.analyst import QueryError, SnapshotQuery
from oversight.cli import EXIT_OK, main
from oversight.config import FieldsConfig
from oversight.demo import ASSIGNEE_NAMES, seed_demo
from oversight.ingest.sync import SyncResult, run_sync
from oversight.jira.mapping import map_assignee
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store import db
from oversight.store.db import SCHEMA_VERSION, Store
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES
from tests.fakes import fake_jira
from tests.fakes.builders import START, ail_and_ops
from tests.fakes.fake_jira import FakeJira

ZONE = "Pacific/Chatham"
MAPPED_COLUMNS = ["account_id", "display_name", "active", "updated_at", "avatar_url"]
PICTURE_COLUMNS = ["avatar", "avatar_type", "avatar_hash", "avatar_source", "avatar_fetched_at"]
PEOPLE_COLUMNS = MAPPED_COLUMNS + PICTURE_COLUMNS


def picture_url(account: str, size: str = "48x48") -> str:
    return f"https://avatars.example.invalid/{account}/{size}.png"


def user(account: str, name: str | None = None, **fields: Any) -> dict[str, Any]:
    found: dict[str, Any] = {
        "self": f"https://example.invalid/rest/api/3/user?accountId={account}",
        "accountId": account,
        "accountType": "atlassian",
        "emailAddress": f"{account}@example.invalid",
        "avatarUrls": {size: picture_url(account, size) for size in ("16x16", "24x24", "32x32", "48x48")},
        "active": True,
        "timeZone": ZONE,
    }
    if name is not None:
        found["displayName"] = name
    return found | fields


def issue_row(key: str, assignee: dict[str, Any] | None, updated: str, raw: Any = None) -> dict[str, Any]:
    payload = {
        "key": key,
        "fields": {"assignee": assignee, "reporter": user("acc-reporter", "Report Er"), "updated": updated},
    }
    return {
        "issue_key": key,
        "issue_id": str(int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)),
        "project_key": key.rsplit("-", 1)[0],
        "status": "To Do",
        "assignee_id": assignee.get("accountId") if assignee else None,
        "updated_at": updated,
        "raw_json": payload if raw is None else raw,
    }


def version_3(path: Path, rows: list[dict[str, Any]], synced: dict[str, str]) -> None:
    with Store.open(path) as store:
        with store.transaction():
            for row in rows:
                store.upsert("issues", row)
            for scope, at in synced.items():
                store.set_cursor(scope, last_synced_at=at, run_id="r1", account_timezone="Europe/London")
        store.conn.executescript("DROP TABLE people; PRAGMA user_version = 3;")


VERSION_4_PEOPLE = """
    DROP TABLE people;
    CREATE TABLE people (account_id TEXT PRIMARY KEY, display_name TEXT, active INTEGER, updated_at TEXT NOT NULL);
"""


def version_4(path: Path, rows: list[dict[str, Any]], people: dict[str, str | None]) -> None:
    with Store.open(path) as store:
        with store.transaction():
            for row in rows:
                store.upsert("issues", row)
        store.conn.executescript(VERSION_4_PEOPLE)
        store.conn.executemany(
            "INSERT INTO people (account_id, display_name, active, updated_at) VALUES (?, ?, 1, ?)",
            [(account, name, AIL_SYNCED) for account, name in people.items()],
        )
        store.conn.execute("PRAGMA user_version = 4")


def read(path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def people_of(path: Path) -> dict[str, tuple[Any, ...]]:
    return {row[0]: row[1:] for row in read(path, "SELECT account_id, display_name, active, updated_at FROM people")}


def urls_of(path: Path) -> dict[str, str | None]:
    return dict(read(path, "SELECT account_id, avatar_url FROM people"))  # type: ignore[arg-type]


def columns_of(path: Path) -> list[str]:
    return [row[1] for row in read(path, "PRAGMA table_info(people)")]


def version_of(path: Path) -> int:
    return read(path, "PRAGMA user_version")[0][0]


def test_a_persons_id_is_p_and_twelve_hex_characters_of_the_account_ids_sha256() -> None:
    assert (person_id("acc-01"), person_id("acc-08")) == ("pbc7fe62842ca", "pe0f1fe0c54ec")
    account = "712020:5f0c0001-a1b2-4c3d-8e9f-0123456789ab"
    assert person_id(account) == "p" + hashlib.sha256(account.encode("utf-8")).hexdigest()[:12]
    assert re.fullmatch(r"p[0-9a-f]{12}", person_id(account)) and person_id(account) == person_id(account)


@pytest.mark.parametrize(
    ("given", "shown"),
    [
        ("Ada Brooks", "Ada Brooks"),
        ("  Ada Brooks \n", "Ada Brooks"),
        ("", UNNAMED),
        ("   ", UNNAMED),
        (None, UNNAMED),
        (42, UNNAMED),
        ("x" * 128, "x" * 128),
        ("x" * 129, "x" * 128),
        ("abc " * 40, ("abc " * 32).rstrip()),
        ("\U0001f600" * 130, "\U0001f600" * 128),
        ("a" + "\U0001f600" * 128, "a" + "\U0001f600" * 127),
    ],
)
def test_a_persons_name_is_the_display_name_trimmed_to_128_characters(given: object, shown: str) -> None:
    assert person_name(given) == shown
    assert 1 <= len(shown) <= 128


def test_a_person_is_an_id_a_name_and_a_picture_version_and_nobody_is_none() -> None:
    assert person("acc-01", " Ada Brooks ", "1e76ee511549") == {"id": "pbc7fe62842ca", "name": "Ada Brooks",
                                                                 "avatar": "1e76ee511549"}
    assert person("acc-01", None, None) == {"id": "pbc7fe62842ca", "name": UNNAMED, "avatar": None}
    assert UNNAMED == "Unnamed account"
    assert person(None, "Ada Brooks", "1e76ee511549") is None and person("", "Ada Brooks", None) is None


@pytest.mark.parametrize(
    "stored", [None, "", "1E76EE511549", "1e76ee51154", "1e76ee5115490", "1e76ee51154g", " 1e76ee511549", 42, b"1e76"]
)
def test_a_stored_hash_that_is_not_12_lower_case_hex_characters_sends_no_avatar(stored: object) -> None:
    assert avatar(stored) is None
    assert person("acc-01", "Ada Brooks", stored) == {"id": "pbc7fe62842ca", "name": "Ada Brooks", "avatar": None}


SEEN = "2026-11-04T15:00:30.000Z"


def test_an_assignee_maps_to_its_account_id_display_name_active_flag_and_picture_url_and_nothing_else() -> None:
    row = map_assignee({"key": "AIL-1", "fields": {"assignee": user("acc-a", "Ada Brooks")}}, SEEN)
    assert row == {"account_id": "acc-a", "display_name": "Ada Brooks", "active": 1, "updated_at": SEEN,
                   "avatar_url": picture_url("acc-a")}
    assert list(row) == MAPPED_COLUMNS


@pytest.mark.parametrize(
    "pictures", [None, {}, {"16x16": picture_url("acc-a", "16x16")}, {"48x48": ""}, {"48x48": 48}, ["48x48"], "x"]
)
def test_an_assignee_without_a_48_pixel_picture_url_maps_to_none(pictures: object) -> None:
    row = map_assignee({"fields": {"assignee": user("acc-a", "Ada Brooks", avatarUrls=pictures)}}, SEEN)
    assert row is not None and row["avatar_url"] is None


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"active": False}, ("Ada Brooks", 0)),
        ({"active": None}, ("Ada Brooks", None)),
        ({"active": "true"}, ("Ada Brooks", None)),
        ({"displayName": ""}, (None, 1)),
        ({"displayName": 7}, (None, 1)),
    ],
)
def test_odd_names_and_flags_map_to_none(fields: dict[str, Any], expected: tuple[Any, Any]) -> None:
    row = map_assignee({"fields": {"assignee": user("acc-a", "Ada Brooks", **fields)}}, SEEN)
    assert row is not None and (row["display_name"], row["active"]) == expected


@pytest.mark.parametrize(
    "issue",
    [{}, {"fields": None}, {"fields": {}}, {"fields": {"assignee": None}}, {"fields": {"assignee": {}}},
     {"fields": {"assignee": {"displayName": "No Id"}}}, {"fields": {"assignee": {"accountId": ""}}},
     {"fields": {"assignee": {"accountId": 7}}}],
)
def test_an_issue_with_nobody_assigned_maps_to_no_row(issue: dict[str, Any]) -> None:
    assert map_assignee(issue, SEEN) is None


AIL_SYNCED = "2026-11-04T09:00:00.000Z"
DEMO_SYNCED = "2026-11-05T09:00:00.000Z"


def snapshot_rows() -> list[dict[str, Any]]:
    return [
        issue_row("AIL-1", user("acc-a", "Ada Older"), "2026-10-01T10:00:00.000Z"),
        issue_row("AIL-2", user("acc-a", "Ada Brooks"), "2026-11-01T10:00:00.000Z"),
        issue_row("AIL-3", user("acc-a", "Ada Middle"), "2026-10-15T10:00:00.000Z"),
        issue_row("DEMO-1", user("acc-a", "Ada Oldest"), "2026-09-01T10:00:00.000Z"),
        issue_row("AIL-4", user("acc-b", "Ben Okafor", active=False), "2026-10-20T10:00:00.000Z"),
        issue_row("OPS-2", user("acc-b", "Ben Okafor", active=False), "2026-10-02T10:00:00.000Z"),
        issue_row("OPS-1", {"accountId": "acc-c", "avatarUrls": {"16x16": "https://avatars.example.invalid/c.png"}},
                  "2026-10-03T10:00:00.000Z"),
        issue_row("AIL-5", None, "2026-10-04T10:00:00.000Z"),
        issue_row("AIL-6", user("acc-z", "Zed Broken"), "2026-10-05T10:00:00.000Z", raw="{not json"),
        issue_row("AIL-7", user("acc-y", "Yan Null"), "2026-10-06T10:00:00.000Z", raw=[1, 2]),
    ]


def test_a_version_3_snapshot_gains_its_people_from_raw_json(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite"
    version_3(path, snapshot_rows(), {"AIL": AIL_SYNCED, "DEMO": DEMO_SYNCED})
    assert version_of(path) == 3 and not read(path, "SELECT name FROM sqlite_master WHERE name = 'people'")
    before = to_iso(datetime.now(UTC) - timedelta(seconds=2)) or ""

    with Store.open(path) as store:
        assert store.count("issues") == 10

    after = to_iso(datetime.now(UTC) + timedelta(seconds=2)) or ""
    assert version_of(path) == SCHEMA_VERSION == 11
    found = people_of(path)
    assert sorted(found) == ["acc-a", "acc-b", "acc-c"]
    assert found["acc-a"] == ("Ada Brooks", 1, DEMO_SYNCED)
    assert found["acc-b"] == ("Ben Okafor", 0, AIL_SYNCED)
    name, active, seen = found["acc-c"]
    assert (name, active) == (None, None) and before <= seen <= after
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", seen)
    assert "acc-reporter" not in found
    assert urls_of(path) == {"acc-a": picture_url("acc-a"), "acc-b": picture_url("acc-b"), "acc-c": None}
    assert read(path, "SELECT COUNT(*) FROM people WHERE COALESCE(avatar, avatar_type, avatar_hash, avatar_source,"
                      " avatar_fetched_at) IS NOT NULL") == [(0,)]


def test_the_migrations_keep_no_email_address_or_time_zone_and_one_picture_url(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite"
    version_3(path, snapshot_rows(), {"AIL": AIL_SYNCED})
    Store.open(path).close()
    assert columns_of(path) == PEOPLE_COLUMNS
    stored = " ".join(str(value) for row in read(path, "SELECT * FROM people") for value in row)
    for kept_out in ("@", ZONE, "Europe/London", "16x16", "24x24", "32x32", "example.invalid/rest"):
        assert kept_out not in stored
    assert stored.count("https://") == 2


def test_the_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite"
    version_3(path, snapshot_rows(), {"AIL": AIL_SYNCED, "DEMO": DEMO_SYNCED})
    Store.open(path).close()
    first, urls = people_of(path), urls_of(path)

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 3")
    conn.close()
    Store.open(path).close()
    assert people_of(path) == first and urls_of(path) == urls and version_of(path) == SCHEMA_VERSION
    assert columns_of(path) == PEOPLE_COLUMNS

    conn = sqlite3.connect(path)
    conn.executescript(db._MIGRATIONS[3])
    conn.close()
    assert people_of(path) == first


def test_a_failed_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v3.sqlite"
    version_3(path, snapshot_rows(), {"AIL": AIL_SYNCED})
    monkeypatch.setitem(db._MIGRATIONS, 3, db._MIGRATIONS[3] + "\nSELECT no_such_function();")
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        Store.open(path)
    assert version_of(path) == 3
    assert not read(path, "SELECT name FROM sqlite_master WHERE name = 'people'")
    assert read(path, "SELECT COUNT(*) FROM issues") == [(10,)]


def test_the_migration_is_fast(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite"
    rows = [
        issue_row(f"AIL-{n}", None if n % 7 == 0 else user(f"acc-{n % 40:02d}", f"Name {n % 40}"),
                  f"2026-10-{1 + n % 28:02d}T10:00:00.000Z")
        for n in range(1, 4001)
    ]
    version_3(path, rows, {"AIL": AIL_SYNCED})
    started = time.perf_counter()
    Store.open(path).close()
    assert time.perf_counter() - started < 5.0
    assert len(people_of(path)) == 40
    assert urls_of(path) == {f"acc-{n:02d}": picture_url(f"acc-{n:02d}") for n in range(40)}


def test_a_demo_seeded_before_it_named_anyone_gets_its_fictional_names(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite"
    with Store.open(path) as store:
        seed_demo(store, now=datetime(2026, 11, 4, 12, 0, tzinfo=UTC), issues=200)
        with store.transaction():
            store.conn.execute("UPDATE issues SET raw_json = json_remove(raw_json, '$.fields.assignee.displayName')")
            store.upsert("issues", issue_row("AIL-1", {"accountId": "acc-08"}, "2026-10-01T10:00:00.000Z"))
        assert not store.query("SELECT 1 FROM issues WHERE json_extract(raw_json, '$.fields.assignee.displayName') NOTNULL")
        store.conn.executescript("DROP TABLE people; PRAGMA user_version = 3;")

    Store.open(path).close()
    names = {account: name for account, (name, _active, _seen) in people_of(path).items()}
    assert names == {**ASSIGNEE_NAMES, "acc-08": None}

    conn = sqlite3.connect(path)
    conn.executescript(db._MIGRATIONS[3])
    conn.close()
    assert {account: name for account, (name, _active, _seen) in people_of(path).items()} == names


def pictures(**urls: str) -> dict[str, str]:
    return {size.lstrip("_"): url for size, url in urls.items()}


def test_a_version_4_snapshot_gains_its_picture_urls_from_raw_json(tmp_path: Path) -> None:
    path = tmp_path / "v4.sqlite"
    rows = [
        issue_row("AIL-1", user("acc-a", "Ada Brooks", avatarUrls=pictures(_48x48="https://a.example.invalid/old")),
                  "2026-10-01T10:00:00.000Z"),
        issue_row("AIL-2", user("acc-a", "Ada Brooks", avatarUrls=pictures(_48x48="https://a.example.invalid/new")),
                  "2026-11-01T10:00:00.000Z"),
        issue_row("OPS-1", user("acc-a", "Ada Brooks", avatarUrls=pictures(_48x48="https://a.example.invalid/mid")),
                  "2026-10-15T10:00:00.000Z"),
        issue_row("AIL-3", user("acc-b", "Ben Okafor", avatarUrls=pictures(_16x16="https://b.example.invalid/16")),
                  "2026-10-02T10:00:00.000Z"),
        issue_row("AIL-4", user("acc-c", "Carmen Ruiz"), "2026-10-03T10:00:00.000Z"),
        issue_row("AIL-5", user("acc-d", "Dev Malhotra"), "2026-10-04T10:00:00.000Z"),
        issue_row("AIL-6", user("acc-e", "Elif Aydin"), "2026-10-05T10:00:00.000Z", raw="{not json"),
        issue_row("AIL-7", None, "2026-10-06T10:00:00.000Z"),
    ]
    version_4(path, rows, {"acc-a": "Ada Brooks", "acc-b": "Ben Okafor", "acc-c": "Carmen Ruiz", "acc-e": "Elif Aydin"})
    before = people_of(path)
    assert version_of(path) == 4 and columns_of(path) == PEOPLE_COLUMNS[:4]

    with Store.open(path) as store:
        assert store.count("issues") == 8

    assert version_of(path) == SCHEMA_VERSION and columns_of(path) == PEOPLE_COLUMNS
    assert people_of(path) == before
    assert urls_of(path) == {
        "acc-a": "https://a.example.invalid/new",
        "acc-b": None,
        "acc-c": picture_url("acc-c"),
        "acc-e": None,
    }
    assert read(path, "SELECT COUNT(*) FROM people WHERE COALESCE(avatar, avatar_type, avatar_hash, avatar_source,"
                      " avatar_fetched_at) IS NOT NULL") == [(0,)]


def test_the_version_5_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "v4.sqlite"
    version_4(path, snapshot_rows(), {"acc-a": "Ada Brooks", "acc-b": "Ben Okafor"})
    Store.open(path).close()
    with Store.open(path) as store, store.transaction():
        store.conn.execute(
            "UPDATE people SET avatar = x'89504e470d0a1a0a', avatar_type = 'image/png', avatar_hash = '0123456789ab',"
            " avatar_source = avatar_url, avatar_fetched_at = ? WHERE account_id = 'acc-a'", (AIL_SYNCED,))
    everything = read(path, "SELECT * FROM people ORDER BY account_id")
    assert everything[0][4] == picture_url("acc-a") and everything[0][7] == "0123456789ab"

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 4")
    conn.close()
    Store.open(path).close()
    assert version_of(path) == SCHEMA_VERSION and columns_of(path) == PEOPLE_COLUMNS
    assert read(path, "SELECT * FROM people ORDER BY account_id") == everything

    conn = sqlite3.connect(path)
    conn.executescript(db._MIGRATIONS[4])
    conn.close()
    assert read(path, "SELECT * FROM people ORDER BY account_id") == everything


def test_a_failed_version_5_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v4.sqlite"
    version_4(path, snapshot_rows(), {"acc-a": "Ada Brooks"})
    monkeypatch.setitem(db._MIGRATIONS, 4, db._MIGRATIONS[4] + "\nSELECT no_such_function();")
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        Store.open(path)
    assert version_of(path) == 4 and columns_of(path) == PEOPLE_COLUMNS[:4]
    assert people_of(path) == {"acc-a": ("Ada Brooks", 1, AIL_SYNCED)}


def test_a_process_that_finds_the_migration_done_by_another_carries_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v4.sqlite"
    version_4(path, snapshot_rows(), {"acc-a": "Ada Brooks"})
    real = Store._new_columns
    raced: list[str] = []

    def other_process_first(self: Store, version: int) -> str:
        statements = real(self, version)
        if not raced:
            raced.append(statements)
            Store.open(path).close()
        return statements

    monkeypatch.setattr(Store, "_new_columns", other_process_first)
    with Store.open(path) as store:
        assert store.count("people") == 1
    assert "ADD COLUMN avatar_url" in raced[0]
    assert version_of(path) == SCHEMA_VERSION and columns_of(path) == PEOPLE_COLUMNS
    assert urls_of(path) == {"acc-a": picture_url("acc-a")}


@pytest.fixture
def fake() -> FakeJira:
    return ail_and_ops()


def sync(fake: FakeJira, store: Store, fields: FieldsConfig, mode: str = "full") -> SyncResult:
    return run_sync(fake.client(), store, fields, projects=["AIL"], mode=mode, now=lambda: fake.clock)


def people_in(store: Store) -> dict[str, tuple[Any, ...]]:
    sql = "SELECT account_id, display_name, active, updated_at FROM people ORDER BY account_id"
    return {row[0]: tuple(row)[1:] for row in store.query(sql)}


def test_a_sync_stores_each_assignee_by_display_name_and_counts_them_apart(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    result = sync(fake, store, fields)

    assert people_in(store) == {"acc-dev-1": ("Developer One", 1, to_iso(START))}
    assigned = store.count("issues") - len(store.query("SELECT 1 FROM issues WHERE assignee_id IS NULL"))
    assert result.people.as_dict() == {"inserted": 1, "updated": 0, "deleted": 0, "unchanged": assigned - 1}
    assert result.rows_written == 137
    run = store.query_one("SELECT stats_json FROM runs WHERE run_id = ?", (result.run_id,))
    assert '"people": {"deleted": 0, "inserted": 1' in run[0]
    assert f"people (assignees): 1 inserted, 0 updated, {assigned - 1} unchanged" in result.render()
    assert [row[1] for row in store.query("PRAGMA table_info(people)")] == PEOPLE_COLUMNS


def test_every_sync_keeps_people_current_and_the_latest_display_name_wins(
    fake: FakeJira, store: Store, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync(fake, store, fields)
    fake.clock = START + timedelta(minutes=30)
    monkeypatch.setitem(fake_jira.PEOPLE, "acc-dev-1", "Developer One Renamed")
    fake.set_points("AIL-7", 5)
    fake.assign("AIL-16", "acc-dev-2")
    fake.clock += timedelta(minutes=2)

    incremental = sync(fake, store, fields, "incremental")
    seen = incremental.started_at
    assert seen > to_iso(START + timedelta(minutes=32))
    assert people_in(store) == {
        "acc-dev-1": ("Developer One Renamed", 1, seen),
        "acc-dev-2": ("Developer Two", 1, seen),
    }
    assert (incremental.people.inserted, incremental.people.updated) == (1, 1)

    fake.clock += timedelta(hours=1)
    assert sync(fake, store, fields).rows_written > 0
    fake.clock += timedelta(hours=1)
    again = sync(fake, store, fields)
    assert again.rows_written == 0
    assert (again.people.inserted, again.people.updated) == (0, 2)
    assert {row[2] for row in people_in(store).values()} == {again.started_at}
    assert people_in(store)["acc-dev-1"][0] == "Developer One Renamed"


def test_a_sync_with_nothing_new_to_read_writes_no_people(fake: FakeJira, store: Store, fields: FieldsConfig) -> None:
    sync(fake, store, fields)
    before = people_in(store)
    fake.clock += timedelta(minutes=30)
    incremental = sync(fake, store, fields, "incremental")
    assert incremental.issues_seen == 0 and incremental.people.as_dict() == {
        "inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    assert people_in(store) == before


def api_settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        jira_projects="AIL",
        api_service_key="test-key",
        db_path=root / "data" / "oversight.db",
        config_dir=CONFIG_FIXTURES,
        reports_dir=root / "reports",
    )


def test_the_api_command_migrates_the_snapshot_before_serving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = api_settings(tmp_path)
    version_3(settings.db_file, snapshot_rows(), {"AIL": AIL_SYNCED})
    served: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda _app, **options: served.append(options))

    assert main(["api", "--port", "3301"], settings=settings) == EXIT_OK

    assert served and served[0]["port"] == 3301
    assert version_of(settings.db_file) == SCHEMA_VERSION and people_of(settings.db_file)["acc-a"][0] == "Ada Brooks"


def test_the_api_command_leaves_a_missing_snapshot_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = api_settings(tmp_path)
    monkeypatch.setattr("uvicorn.run", lambda _app, **_options: None)
    assert main(["api", "--port", "3301"], settings=settings) == EXIT_OK
    assert not settings.db_file.exists()


def test_run_py_migrates_the_snapshot_before_serving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = api_settings(tmp_path)
    version_3(settings.db_file, snapshot_rows(), {"AIL": AIL_SYNCED})
    served: list[tuple[str, int]] = []
    monkeypatch.setattr("oversight.settings.get_settings", lambda: settings)
    monkeypatch.setattr("uvicorn.run", lambda target, **_options: served.append((target, version_of(settings.db_file))))

    runpy.run_path(str(Path(__file__).parent.parent / "run.py"), run_name="__main__")

    assert served == [("oversight.api.app:serve_app", SCHEMA_VERSION)]
    assert people_of(settings.db_file)["acc-a"][0] == "Ada Brooks"


def test_seeding_the_demo_twice_writes_its_people_once(store: Store) -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    seed_demo(store, now=now, issues=60)
    first = people_in(store)
    pictures = store.query("SELECT * FROM people WHERE avatar IS NOT NULL ORDER BY account_id")
    assert first and all(seen == to_iso(now) for _name, _active, seen in first.values())
    assert [row["account_id"] for row in pictures] == ["acc-01", "acc-05"]
    seed_demo(store, now=now + timedelta(days=1), issues=60)
    assert people_in(store) == {account: (name, active, to_iso(now + timedelta(days=1)))
                                for account, (name, active, _seen) in first.items()}
    again = store.query("SELECT * FROM people WHERE avatar IS NOT NULL ORDER BY account_id")
    assert [tuple(row) for row in again] == [tuple(row)[:3] + (to_iso(now + timedelta(days=1)),) + tuple(row)[4:]
                                             for row in pictures]


def test_ask_cannot_read_people(tmp_path: Path) -> None:
    path = tmp_path / "oversight.db"
    with Store.open(path) as store:
        seed_demo(store, now=datetime(2026, 11, 4, 12, 0, tzinfo=UTC), issues=30)
        assert store.count("people") > 0
    query = SnapshotQuery(path, project="DEMO", sprint_field=None, rules=RulesConfig.load(CONFIG_FIXTURES / "rules.yaml"))
    try:
        for sql in ("SELECT * FROM people", "SELECT display_name FROM people", f"SELECT * FROM {query.schema}.people"):
            with pytest.raises(QueryError, match="not allowed"):
                query.run(sql)
        with pytest.raises(QueryError, match="no such column"):
            query.run("SELECT display_name FROM issues")
    finally:
        query.close()
