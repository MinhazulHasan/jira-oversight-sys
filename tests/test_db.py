from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from structlog.testing import capture_logs

import oversight.api.app as app_module
from oversight.errors import ConfigError
from oversight.store import db
from oversight.store.db import SCHEMA_VERSION, Store, WriteStats, open_readonly
from oversight.settings import Settings
from oversight.timeutil import UTC


def _issue(key: str = "AIL-1", issue_id: str = "1", **extra: object) -> dict:
    row = {
        "issue_key": key,
        "issue_id": issue_id,
        "project_key": "AIL",
        "status": "To Do",
        "sprint_ids": [4427],
        "raw_json": {"key": key, "fields": {"b": 1, "a": 2}},
    }
    row.update(extra)
    return row


def test_schema_is_created_and_versioned(store: Store) -> None:
    tables = {r["name"] for r in store.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "issues", "status_changes", "field_changes", "worklogs", "links", "statuses",
        "sync_state", "status_intervals", "issue_metrics", "runs", "findings", "qa_log",
    } <= tables
    assert store.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION


def test_reopening_an_existing_database_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    with Store.open(path) as s:
        s.upsert("issues", _issue())
    with Store.open(path) as s:
        assert s.count("issues") == 1


def test_a_version_2_database_migrates_to_the_current_schema_and_keeps_its_data(tmp_path: Path) -> None:
    path = tmp_path / "v2.sqlite"
    with Store.open(path) as s:
        s.upsert("issues", _issue())
        s.conn.executescript(
            """
            DROP TABLE rule_status; DROP TABLE warning_links; DROP TABLE digests;
            DROP TABLE llm_calls; DROP TABLE insights; DROP TABLE findings; DROP TABLE qa_log;
            CREATE TABLE findings (finding_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, rule_version TEXT NOT NULL,
              issue_key TEXT NOT NULL, severity TEXT NOT NULL, message TEXT, evidence_json TEXT, first_seen_at TEXT,
              last_seen_at TEXT, resolved_at TEXT, run_id TEXT);
            CREATE TABLE qa_log (qa_id TEXT PRIMARY KEY, asked_at TEXT, asked_by TEXT, question TEXT, plan_json TEXT,
              facts_json TEXT, answer TEXT, outcome TEXT, model TEXT, prompt_hash TEXT);
            PRAGMA user_version = 2;
            """
        )

    with Store.open(path) as s:  # any command's first open migrates (2 to 3, and on to 12)
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 12
        assert s.count("issues") == 1
        tables = {r["name"] for r in s.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"rule_status", "warning_links", "digests", "llm_calls", "insights", "people", "board_members",
                "access_log", "rule_settings", "rule_settings_changes"} <= tables
        assert "settings_hash" in {r["name"] for r in s.query("PRAGMA table_info(rule_status)")}
        assert "ix_qa_log_time" in {r["name"] for r in s.query("PRAGMA index_list(qa_log)")}
        assert {"avatar_url", "avatar", "avatar_type", "avatar_hash", "avatar_source", "avatar_fetched_at"} <= {
            r["name"] for r in s.query("PRAGMA table_info(people)")}
        findings = {r["name"] for r in s.query("PRAGMA table_info(findings)")}
        assert {"project_key", "kind", "evidence_key", "resolved_why", "settings_hash"} <= findings
        assert {"citations_json", "level", "mentions_json"} <= {r["name"] for r in s.query("PRAGMA table_info(qa_log)")}


VERSION_5_QA_LOG = """
    DROP TABLE qa_log;
    CREATE TABLE qa_log (qa_id TEXT PRIMARY KEY, asked_at TEXT, asked_by TEXT, project_key TEXT, question TEXT,
      plan_json TEXT, facts_json TEXT, answer TEXT, citations_json TEXT, outcome TEXT, missing_json TEXT, model TEXT,
      prompt_hash TEXT, latency_ms INTEGER);
    INSERT INTO qa_log (qa_id, asked_by, question, outcome) VALUES ('q1', 'dashboard', 'Where do tickets get stuck most?', 'answered');
    PRAGMA user_version = 5;
"""


def qa_log_columns(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(qa_log)")]
    finally:
        conn.close()


def test_a_version_5_database_records_every_earlier_question_at_team_level(tmp_path: Path) -> None:
    path = tmp_path / "v5.sqlite"
    with Store.open(path) as s:
        s.conn.executescript(VERSION_5_QA_LOG)
    assert qa_log_columns(path)[-1] == "latency_ms"

    with Store.open(path) as s:  # any command's first open migrates (on to 12)
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 12
        assert tuple(s.query_one("SELECT level, mentions_json, question FROM qa_log WHERE qa_id = 'q1'")) == (
            "team", None, "Where do tickets get stuck most?")
        s.upsert("qa_log", {"qa_id": "q2", "level": "people", "mentions_json": ["pbc7fe62842ca", "pe0f1fe0c54ec"]})
        assert tuple(s.query_one("SELECT level, mentions_json FROM qa_log WHERE qa_id = 'q2'")) == (
            "people", '["pbc7fe62842ca","pe0f1fe0c54ec"]')
        s.upsert("qa_log", {"qa_id": "q3", "question": "x"})  # a row that names no level is a team-level one
        assert s.query_one("SELECT level FROM qa_log WHERE qa_id = 'q3'")[0] == "team"

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 5")  # as if it had to run again
    conn.close()
    with Store.open(path) as s:
        assert s.count("qa_log") == 3
    columns = qa_log_columns(path)
    assert columns.count("level") == columns.count("mentions_json") == 1  # no column added twice


def test_a_failed_version_6_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v5.sqlite"
    with Store.open(path) as s:
        s.conn.executescript(VERSION_5_QA_LOG)
    monkeypatch.setitem(db._MIGRATIONS, 5, db._MIGRATIONS[5] + "\nSELECT no_such_function();")
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        Store.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    finally:
        conn.close()
    assert "level" not in qa_log_columns(path) and "mentions_json" not in qa_log_columns(path)


VERSION_11 = """
    DROP TABLE finding_ignores;
    DROP TABLE IF EXISTS people_credit_changes;
    DROP TABLE IF EXISTS people_credit;
    PRAGMA user_version = 11;
"""
VERSION_10 = VERSION_11 + """
    DROP INDEX ix_custom_rules_project;
    DROP TABLE custom_rules;
    PRAGMA user_version = 10;
"""
VERSION_9 = VERSION_10 + """
    DROP TABLE boards;
    PRAGMA user_version = 9;
"""
VERSION_8 = VERSION_9 + """
    DROP TABLE rule_settings_changes;
    DROP TABLE rule_settings;
    ALTER TABLE findings DROP COLUMN settings_hash;
    ALTER TABLE rule_status DROP COLUMN settings_hash;
    PRAGMA user_version = 8;
"""
VERSION_7 = VERSION_8 + """
    DROP INDEX ix_access_log_at;
    DROP TABLE access_log;
    DROP INDEX ix_llm_calls_time;
    DROP INDEX ix_qa_log_time;
    PRAGMA user_version = 7;
"""
VERSION_6 = VERSION_7 + """
    DROP INDEX ix_board_members_account;
    DROP TABLE board_members;
    PRAGMA user_version = 6;
"""


def test_every_served_worker_brings_an_older_snapshot_to_this_version(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store.open(settings.db_file) as s:
        s.conn.executescript(VERSION_6)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    assert app_module.serve_app() is not None
    assert board_members_shape(settings.db_file)[1] == ["ix_board_members_account", "sqlite_autoindex_board_members_1"]
    with Store.open(settings.db_file) as s:
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION


def board_members_shape(path: Path) -> tuple[list[tuple[str, str, int, int]], list[str]]:
    conn = sqlite3.connect(path)
    try:
        columns = [(row[1], row[2], row[3], row[5]) for row in conn.execute("PRAGMA table_info(board_members)")]
        indexes = sorted(row[1] for row in conn.execute("PRAGMA index_list(board_members)"))
        return columns, indexes
    finally:
        conn.close()


def test_a_version_6_database_gains_board_members_and_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "v6.sqlite"
    with Store.open(path) as s:
        s.upsert("issues", _issue(assignee_id="acc-1"))
        s.upsert("people", {"account_id": "acc-1", "display_name": "Ada Brooks", "updated_at": "2026-11-04T09:00:00.000Z"})
        s.upsert("qa_log", {"qa_id": "q1", "level": "people", "mentions_json": ["pbc7fe62842ca"]})
        s.conn.executescript(VERSION_6)
    assert board_members_shape(path) == ([], [])

    with Store.open(path) as s:  # any command's first open migrates
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 12
        assert s.count("board_members") == 0
        assert (s.count("issues"), s.count("people"), s.count("qa_log")) == (1, 1, 1)
        s.upsert("board_members", {"project_key": "AIL", "account_id": "acc-1", "seen_at": "2026-11-04T09:00:00.000Z"})
    shape = board_members_shape(path)
    assert shape[0] == [("project_key", "TEXT", 1, 1), ("account_id", "TEXT", 1, 2), ("seen_at", "TEXT", 1, 0)]
    assert "ix_board_members_account" in shape[1]

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 6")  # as if it had to run again: nothing changes, nothing is lost
    conn.close()
    with Store.open(path) as s:
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert s.count("board_members") == 1
    assert board_members_shape(path) == shape


def test_a_failed_version_7_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v6.sqlite"
    with Store.open(path) as s:
        s.conn.executescript(VERSION_6)
    failing = tmp_path / "schema.sql"
    failing.write_text(db.SCHEMA_PATH.read_text(encoding="utf-8") + "\nSELECT no_such_function();\n", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA_PATH", failing)
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        Store.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        conn.close()
    assert board_members_shape(path) == ([], [])


def access_log_shape(path: Path) -> tuple[list[tuple[str, str, int, int]], list[str], list[str]]:
    conn = sqlite3.connect(path)
    try:
        columns = [(row[1], row[2], row[3], row[5]) for row in conn.execute("PRAGMA table_info(access_log)")]
        indexes = sorted(row[1] for row in conn.execute("PRAGMA index_list(access_log)"))
        times = sorted(row[1] for table in ("llm_calls", "qa_log") for row in conn.execute(f"PRAGMA index_list({table})")
                       if row[1].startswith("ix_"))
        return columns, indexes, times
    finally:
        conn.close()


def test_a_version_7_database_gains_the_access_log_and_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "v7.sqlite"
    with Store.open(path) as s:
        s.upsert("issues", _issue(assignee_id="acc-1"))
        s.upsert("people", {"account_id": "acc-1", "display_name": "Ada Brooks", "updated_at": "2026-11-04T09:00:00.000Z"})
        s.upsert("board_members", {"project_key": "AIL", "account_id": "acc-1", "seen_at": "2026-11-04T09:00:00.000Z"})
        s.upsert("qa_log", {"qa_id": "q1", "asked_at": "2026-11-04T09:00:00.000Z", "level": "people"})
        s.upsert("llm_calls", {"call_id": "c1", "called_at": "2026-11-04T09:00:00.000Z", "purpose": "plan",
                               "provider": "anthropic", "input_tokens": 10, "output_tokens": 5})
        s.conn.executescript(VERSION_7)
    assert access_log_shape(path) == ([], [], [])

    with Store.open(path) as s:  # any command's first open migrates
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 12
        assert s.count("access_log") == 0
        counts = tuple(s.count(table) for table in ("issues", "people", "board_members", "qa_log", "llm_calls"))
        assert counts == (1, 1, 1, 1, 1)
        s.upsert("access_log", {"id": "e1", "at": "2026-11-04T09:00:00.000Z", "viewer_hash": "local",
                                "viewer_name": "Local (sign-in off)", "level": "people", "project_key": "AIL",
                                "action": "scorecard_view", "subject_ids_json": ["pbc7fe62842ca"],
                                "detail_json": {"days": 90}})
    shape = access_log_shape(path)
    assert shape[0] == [
        ("id", "TEXT", 0, 1), ("at", "TEXT", 1, 0), ("viewer_hash", "TEXT", 1, 0), ("viewer_name", "TEXT", 1, 0),
        ("level", "TEXT", 1, 0), ("project_key", "TEXT", 0, 0), ("action", "TEXT", 1, 0),
        ("subject_ids_json", "TEXT", 1, 0), ("detail_json", "TEXT", 0, 0),
    ]
    assert "ix_access_log_at" in shape[1]
    assert shape[2] == ["ix_llm_calls_time", "ix_qa_log_time"]

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 7")  # as if it had to run again: nothing changes, nothing is lost
    conn.close()
    with Store.open(path) as s:
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert s.count("access_log") == 1
    assert access_log_shape(path) == shape


def test_a_failed_version_8_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v7.sqlite"
    with Store.open(path) as s:
        s.conn.executescript(VERSION_7)
    failing = tmp_path / "schema.sql"
    failing.write_text(db.SCHEMA_PATH.read_text(encoding="utf-8") + "\nSELECT no_such_function();\n", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA_PATH", failing)
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        Store.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        conn.close()
    assert access_log_shape(path) == ([], [], [])


def rule_settings_shape(path: Path) -> tuple[list[str], list[str], list[str]]:
    conn = sqlite3.connect(path)
    try:
        tables = sorted(row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'rule\\_settings%' ESCAPE '\\'"))
        findings = [row[1] for row in conn.execute("PRAGMA table_info(findings)") if row[1] == "settings_hash"]
        status = [row[1] for row in conn.execute("PRAGMA table_info(rule_status)") if row[1] == "settings_hash"]
        return tables, findings, status
    finally:
        conn.close()


VERSION_9_SHAPE = (["rule_settings", "rule_settings_changes"], ["settings_hash"], ["settings_hash"])


def finding_row(n: int, **extra: object) -> dict[str, object]:
    """A finding as version 8 wrote it"""
    row: dict[str, object] = {
        "finding_id": f"{n:032x}", "rule_id": "R-EFF-01", "rule_version": "0.1.0", "issue_key": f"AIL-{n}",
        "project_key": "AIL", "kind": "rule", "severity": "breach", "evidence_key": "ratio-ge-1.5",
        "message": "Logged 16.0 h against an estimate of 8.0 h (2.00×; threshold 1.5×)",
        "evidence_json": {"effort_ratio": 2.0, "threshold": 1.5}, "first_seen_at": "2026-11-04T09:00:00.000Z",
        "last_seen_at": "2026-11-05T09:00:00.000Z", "resolved_at": None, "resolved_why": None, "run_id": "r1",
    }
    row.update(extra)
    return row


def findings_of(path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT finding_id, rule_id, rule_version, issue_key, evidence_key, message, evidence_json, first_seen_at,"
            " last_seen_at, resolved_at, resolved_why, run_id FROM findings ORDER BY finding_id")]
    finally:
        conn.close()


def test_a_version_8_database_gains_rule_settings_and_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "v8.sqlite"
    with Store.open(path) as s:
        s.upsert("issues", _issue(assignee_id="acc-1"))
        s.upsert("people", {"account_id": "acc-1", "display_name": "Ada Brooks", "updated_at": "2026-11-04T09:00:00.000Z"})
        s.conn.executescript(VERSION_8)
        s.upsert("findings", finding_row(1))
        s.upsert("findings", finding_row(2, resolved_at="2026-11-05T10:00:00.000Z", resolved_why="cleared"))
        s.upsert("rule_status", {"run_id": "r1", "project_key": "AIL", "rule_id": "R-EFF-01", "rule_version": "0.1.0",
                                 "measurable": 1, "issues_checked": 12, "hits": 2})
        s.upsert("access_log", {"id": "e1", "at": "2026-11-04T09:00:00.000Z", "viewer_hash": "local",
                                "viewer_name": "Local (sign-in off)", "level": "people", "project_key": "AIL",
                                "action": "scorecard_view", "subject_ids_json": ["pbc7fe62842ca"],
                                "detail_json": {"days": 90}})
        run = s.start_run("rules", rules_version="0.1.0", started_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC))
        s.finish_run(run, status="succeeded", stats={"AIL": {"opened": 1}})
    assert rule_settings_shape(path) == ([], [], [])
    before = findings_of(path)

    with Store.open(path) as s:  # any command's first open migrates
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 12
        counts = tuple(s.count(table) for table in ("issues", "people", "findings", "rule_status", "access_log", "runs"))
        assert counts == (1, 1, 2, 1, 1, 1)
        assert [row[0] for row in s.query("SELECT settings_hash FROM findings")] == [None, None]
        assert s.query_one("SELECT settings_hash FROM rule_status")[0] is None
        assert s.count("rule_settings") == s.count("rule_settings_changes") == 0
        s.upsert("rule_settings", {"project_key": "AIL", "rule_id": "R-EFF-01", "params_json": {"threshold": 2.0},
                                   "updated_at": "2026-11-17T12:10:00.000Z"})
    assert findings_of(path) == before  # every finding as it was: the same ids, the same history
    assert rule_settings_shape(path) == VERSION_9_SHAPE

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 8")  # as if it had to run again: nothing changes, nothing is lost
    conn.close()
    with Store.open(path) as s:
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert (s.count("findings"), s.count("rule_settings")) == (2, 1)
    assert rule_settings_shape(path) == VERSION_9_SHAPE  # no column added twice
    assert findings_of(path) == before


def test_a_failed_version_9_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v8.sqlite"
    with Store.open(path) as s:
        s.conn.executescript(VERSION_8)
        s.upsert("findings", finding_row(1))
    failing = tmp_path / "schema.sql"
    failing.write_text(db.SCHEMA_PATH.read_text(encoding="utf-8") + "\nSELECT no_such_function();\n", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA_PATH", failing)
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        Store.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        conn.close()
    assert rule_settings_shape(path) == ([], [], [])
    assert len(findings_of(path)) == 1


def test_two_processes_opening_a_version_8_database_at_once_both_end_at_version_9(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v8.sqlite"
    with Store.open(path) as s:
        s.upsert("findings", finding_row(1))
        s.conn.executescript(VERSION_8)
    gather = Store._new_columns
    raced: list[str] = []

    def racing(self: Store, version: int) -> str:
        statements = gather(self, version)
        if version == 8 and not raced:
            raced.append(statements)
            Store.open(path).close()  # the other process migrates the whole file meanwhile
        return statements

    monkeypatch.setattr(Store, "_new_columns", racing)
    with capture_logs() as events, Store.open(path) as late:
        assert late.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert late.count("findings") == 1
        late.upsert("rule_settings_changes", {
            "change_id": "c" * 32, "at": "2026-11-17T12:10:00.000Z", "project_key": "AIL", "revision": 1,
            "viewer_hash": "local", "viewer_name": "Local (sign-in off)", "changes_json": [],
        })
    assert len(raced) == 1 and "ADD COLUMN settings_hash" in raced[0]  # it had gathered the columns as missing
    assert "schema_migrated_elsewhere" in [event["event"] for event in events]
    assert rule_settings_shape(path) == VERSION_9_SHAPE


def boards_shape(path: Path) -> list[tuple[str, str, int, int]]:
    conn = sqlite3.connect(path)
    try:
        return [(row[1], row[2], row[3], row[5]) for row in conn.execute("PRAGMA table_info(boards)")]
    finally:
        conn.close()


BOARDS_SHAPE = [
    ("project_key", "TEXT", 0, 1), ("project_id", "TEXT", 0, 0), ("name", "TEXT", 1, 0),
    ("project_type", "TEXT", 0, 0), ("discovered_at", "TEXT", 1, 0), ("last_seen_at", "TEXT", 1, 0),
    ("disappeared_at", "TEXT", 0, 0),
]


def test_a_version_9_database_gains_the_boards_table_and_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "v9.sqlite"
    with Store.open(path) as s:
        s.upsert("issues", _issue(assignee_id="acc-1"))
        s.upsert("findings", finding_row(1))
        s.upsert("rule_settings", {"project_key": "AIL", "rule_id": "R-EFF-01", "params_json": {"threshold": 2.0},
                                   "updated_at": "2026-11-17T12:10:00.000Z"})
        s.set_cursor("AIL", last_synced_at="2026-11-17T12:00:00.000Z", run_id="r1", account_timezone=None)
        s.conn.executescript(VERSION_9)
    assert boards_shape(path) == []
    before = findings_of(path)

    with Store.open(path) as s:  # any command's first open migrates
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 12
        assert s.count("boards") == 0
        counts = tuple(s.count(table) for table in ("issues", "findings", "rule_settings", "sync_state"))
        assert counts == (1, 1, 1, 1)
        s.upsert("boards", {"project_key": "OPS", "name": "Operations", "project_type": "business",
                            "discovered_at": "2026-11-18T09:00:00.000Z", "last_seen_at": "2026-11-18T09:00:00.000Z"})
    assert boards_shape(path) == BOARDS_SHAPE
    assert findings_of(path) == before

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 9")  # as if it had to run again: nothing changes, nothing is lost
    conn.close()
    with Store.open(path) as s:
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert s.count("boards") == 1
    assert boards_shape(path) == BOARDS_SHAPE


def test_a_failed_version_10_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v9.sqlite"
    with Store.open(path) as s:
        s.conn.executescript(VERSION_9)
    failing = tmp_path / "schema.sql"
    failing.write_text(db.SCHEMA_PATH.read_text(encoding="utf-8") + "\nSELECT no_such_function();\n", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA_PATH", failing)
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        Store.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    finally:
        conn.close()
    assert boards_shape(path) == []


def custom_rules_shape(path: Path) -> tuple[list[tuple[str, str, int, int]], list[str]]:
    conn = sqlite3.connect(path)
    try:
        columns = [(row[1], row[2], row[3], row[5]) for row in conn.execute("PRAGMA table_info(custom_rules)")]
        indexes = sorted(row[1] for row in conn.execute("PRAGMA index_list(custom_rules)") if not row[1].startswith("sqlite_"))
        return columns, indexes
    finally:
        conn.close()


CUSTOM_RULES_SHAPE = ([
    ("rule_id", "TEXT", 0, 1), ("project_key", "TEXT", 1, 0), ("title", "TEXT", 1, 0), ("description", "TEXT", 0, 0),
    ("kind", "TEXT", 1, 0), ("severity", "TEXT", 1, 0), ("check_name", "TEXT", 1, 0), ("params_json", "TEXT", 1, 0),
    ("applies_json", "TEXT", 1, 0), ("created_at", "TEXT", 1, 0), ("created_revision", "INTEGER", 1, 0),
    ("created_by", "TEXT", 1, 0), ("updated_at", "TEXT", 1, 0), ("deleted_at", "TEXT", 0, 0),
], ["ix_custom_rules_project"])


def custom_rule_row(rule_id: str = "R-CUS-01", **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "rule_id": rule_id, "project_key": "AIL", "title": "Bugs in review over 3 days", "description": None,
        "kind": "rule", "severity": "breach", "check_name": "time_in_status_over",
        "params_json": {"statuses": ["In Progress"], "hours": 72.0},
        "applies_json": {"status_category": ["indeterminate"], "issue_type": ["Bug"]},
        "created_at": "2026-11-23T09:00:00.000Z", "created_revision": 2, "created_by": "Local (sign-in off)",
        "updated_at": "2026-11-23T09:00:00.000Z", "deleted_at": None,
    }
    row.update(extra)
    return row


def test_a_version_10_database_gains_custom_rules_and_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "v10.sqlite"
    with Store.open(path) as s:
        s.upsert("issues", _issue(assignee_id="acc-1"))
        s.upsert("findings", finding_row(1, settings_hash="0123456789ab"))
        s.upsert("rule_settings", {"project_key": "AIL", "rule_id": "R-EFF-01", "params_json": {"threshold": 2.0},
                                   "updated_at": "2026-11-17T12:10:00.000Z"})
        s.upsert("rule_settings_changes", {
            "change_id": "c" * 32, "at": "2026-11-17T12:10:00.000Z", "project_key": "AIL", "revision": 1,
            "viewer_hash": "local", "viewer_name": "Local (sign-in off)",
            "changes_json": [{"rule_id": "R-EFF-01", "field": "params.threshold", "from": 1.5, "to": 2.0}],
        })
        s.upsert("boards", {"project_key": "OPS", "name": "Operations", "discovered_at": "2026-11-18T09:00:00.000Z",
                            "last_seen_at": "2026-11-18T09:00:00.000Z"})
        s.set_cursor("AIL", last_synced_at="2026-11-17T12:00:00.000Z", run_id="r1", account_timezone=None)
        s.conn.executescript(VERSION_10)
    assert custom_rules_shape(path) == ([], [])
    before = findings_of(path)

    with Store.open(path) as s:  # any command's first open migrates
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 12
        assert s.count("custom_rules") == 0
        counts = tuple(s.count(table) for table in ("issues", "findings", "rule_settings", "rule_settings_changes",
                                                    "boards", "sync_state"))
        assert counts == (1, 1, 1, 1, 1, 1)
        s.upsert("custom_rules", custom_rule_row())
    assert custom_rules_shape(path) == CUSTOM_RULES_SHAPE
    assert findings_of(path) == before

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 10")  # as if it had to run again: nothing changes, nothing is lost
    conn.close()
    with Store.open(path) as s:
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert s.count("custom_rules") == 1
    assert custom_rules_shape(path) == CUSTOM_RULES_SHAPE


def test_a_failed_version_11_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v10.sqlite"
    with Store.open(path) as s:
        s.upsert("findings", finding_row(1))
        s.conn.executescript(VERSION_10)
    failing = tmp_path / "schema.sql"
    failing.write_text(db.SCHEMA_PATH.read_text(encoding="utf-8") + "\nSELECT no_such_function();\n", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA_PATH", failing)
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        Store.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
    finally:
        conn.close()
    assert custom_rules_shape(path) == ([], [])
    assert len(findings_of(path)) == 1


def test_two_processes_opening_a_version_10_database_at_once_both_end_at_version_11(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v10.sqlite"
    with Store.open(path) as s:
        s.upsert("findings", finding_row(1))
        s.conn.executescript(VERSION_10)
    gather = Store._new_columns
    raced: list[int] = []

    def racing(self: Store, version: int) -> str:
        statements = gather(self, version)
        if version == 10 and not raced:
            raced.append(version)
            with Store.open(path) as other:  # the other process migrates the whole file meanwhile, and adds a rule
                other.upsert("custom_rules", custom_rule_row())
        return statements

    monkeypatch.setattr(Store, "_new_columns", racing)
    with Store.open(path) as late:
        assert late.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert (late.count("findings"), late.count("custom_rules")) == (1, 1)
    assert raced == [10]
    assert custom_rules_shape(path) == CUSTOM_RULES_SHAPE


def finding_ignores_shape(path: Path) -> tuple[list[tuple[str, str, int, int]], list[tuple[str, int, int]]]:
    conn = sqlite3.connect(path)
    try:
        columns = [(row[1], row[2], row[3], row[5]) for row in conn.execute("PRAGMA table_info(finding_ignores)")]
        indexes = sorted((row[1], row[2], row[4]) for row in conn.execute("PRAGMA index_list(finding_ignores)")
                         if not row[1].startswith("sqlite_"))
        return columns, indexes
    finally:
        conn.close()


FINDING_IGNORES_SHAPE = ([
    ("ignore_id", "TEXT", 0, 1), ("project_key", "TEXT", 1, 0), ("issue_key", "TEXT", 1, 0), ("rule_id", "TEXT", 0, 0),
    ("scope", "TEXT", 1, 0), ("reason", "TEXT", 0, 0), ("created_by", "TEXT", 1, 0), ("created_at", "TEXT", 1, 0),
    ("restored_by", "TEXT", 0, 0), ("restored_at", "TEXT", 0, 0),
], [("ux_finding_ignores_active", 1, 1)])  # unique, and partial: active ignores only


def ignore_row(ignore_id: str, rule_id: str | None = "R-EFF-01", **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ignore_id": ignore_id, "project_key": "AIL", "issue_key": "AIL-1", "rule_id": rule_id,
        "scope": "finding" if rule_id else "ticket", "reason": None, "created_by": "Local (sign-in off)",
        "created_at": "2026-11-26T09:00:00.000Z", "restored_by": None, "restored_at": None,
    }
    row.update(extra)
    return row


def test_a_version_11_database_gains_finding_ignores_and_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "v11.sqlite"
    with Store.open(path) as s:
        s.upsert("issues", _issue(assignee_id="acc-1"))
        s.upsert("findings", finding_row(1))
        s.upsert("findings", finding_row(2, resolved_at="2026-11-05T10:00:00.000Z", resolved_why="cleared"))
        s.upsert("rule_settings", {"project_key": "AIL", "rule_id": "R-EFF-01", "params_json": {"threshold": 2.0},
                                   "updated_at": "2026-11-17T12:10:00.000Z"})
        s.upsert("custom_rules", custom_rule_row())
        s.set_cursor("AIL", last_synced_at="2026-11-17T12:00:00.000Z", run_id="r1", account_timezone=None)
        s.conn.executescript(VERSION_11)
    assert finding_ignores_shape(path) == ([], [])
    before = findings_of(path)

    with Store.open(path) as s:  # any command's first open migrates
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 12
        assert s.count("finding_ignores") == 0
        counts = tuple(s.count(table) for table in ("issues", "findings", "rule_settings", "custom_rules", "sync_state"))
        assert counts == (1, 2, 1, 1, 1)
        s.upsert("finding_ignores", ignore_row("a" * 32))
    assert finding_ignores_shape(path) == FINDING_IGNORES_SHAPE
    assert findings_of(path) == before  # every finding as it was: the same ids, the same history

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 11")  # as if it had to run again: nothing changes, nothing is lost
    conn.close()
    with Store.open(path) as s:
        assert s.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert s.count("finding_ignores") == 1
    assert finding_ignores_shape(path) == FINDING_IGNORES_SHAPE
    assert findings_of(path) == before


def test_one_active_ignore_per_ticket_and_rule_and_any_number_restored(store: Store) -> None:
    store.upsert("finding_ignores", ignore_row("a" * 32))
    store.upsert("finding_ignores", ignore_row("b" * 32, rule_id=None))  # the whole ticket: a pair of its own
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert("finding_ignores", ignore_row("c" * 32))
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert("finding_ignores", ignore_row("d" * 32, rule_id=None))
    store.conn.execute("UPDATE finding_ignores SET restored_by = 'Ada', restored_at = '2026-11-26T10:00:00.000Z'")
    store.upsert("finding_ignores", ignore_row("c" * 32))
    store.upsert("finding_ignores", ignore_row("d" * 32, rule_id=None))
    store.conn.execute("UPDATE finding_ignores SET restored_by = 'Ada', restored_at = '2026-11-26T11:00:00.000Z'"
                       " WHERE restored_at IS NULL")
    store.upsert("finding_ignores", ignore_row("e" * 32))
    assert store.count("finding_ignores") == 5
    assert store.query_one("SELECT COUNT(*) FROM finding_ignores WHERE restored_at IS NULL")[0] == 1


def test_a_failed_version_12_migration_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v11.sqlite"
    with Store.open(path) as s:
        s.upsert("findings", finding_row(1))
        s.conn.executescript(VERSION_11)
    failing = tmp_path / "schema.sql"
    failing.write_text(db.SCHEMA_PATH.read_text(encoding="utf-8") + "\nSELECT no_such_function();\n", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA_PATH", failing)
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        Store.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    finally:
        conn.close()
    assert finding_ignores_shape(path) == ([], [])
    assert len(findings_of(path)) == 1


def test_two_processes_opening_a_version_11_database_at_once_both_end_at_version_12(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v11.sqlite"
    with Store.open(path) as s:
        s.upsert("findings", finding_row(1))
        s.conn.executescript(VERSION_11)
    gather = Store._new_columns
    raced: list[int] = []

    def racing(self: Store, version: int) -> str:
        statements = gather(self, version)
        if version == 11 and not raced:
            raced.append(version)
            with Store.open(path) as other:  # the other process migrates the whole file meanwhile, and ignores one
                other.upsert("finding_ignores", ignore_row("a" * 32))
        return statements

    monkeypatch.setattr(Store, "_new_columns", racing)
    with Store.open(path) as late:
        assert late.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
        assert (late.count("findings"), late.count("finding_ignores")) == (1, 1)
    assert raced == [11]
    assert finding_ignores_shape(path) == FINDING_IGNORES_SHAPE


def test_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(ConfigError, match="schema version 99"):
        Store.open(path)


def test_upsert_reports_insert_update_and_unchanged(store: Store) -> None:
    assert store.upsert("issues", _issue()) == "inserted"
    assert store.upsert("issues", _issue()) == "unchanged"
    assert store.upsert("issues", _issue(status="Done")) == "updated"
    assert store.upsert("issues", _issue(status="Done")) == "unchanged"


def test_json_values_are_stable_regardless_of_key_order(store: Store) -> None:
    store.upsert("issues", _issue(raw_json={"a": 2, "b": 1}))
    assert store.upsert("issues", _issue(raw_json={"b": 1, "a": 2})) == "unchanged"
    row = store.query_one("SELECT sprint_ids, raw_json FROM issues")
    assert row["sprint_ids"] == "[4427]"
    assert row["raw_json"] == '{"a":2,"b":1}'


def test_null_to_value_and_back_counts_as_a_change(store: Store) -> None:
    store.upsert("issues", _issue(story_points=None))
    assert store.upsert("issues", _issue(story_points=3.0)) == "updated"
    assert store.upsert("issues", _issue(story_points=None)) == "updated"


def test_upsert_needs_the_key_columns(store: Store) -> None:
    with pytest.raises(ValueError, match="key columns"):
        store.upsert("status_changes", {"issue_key": "AIL-1"})


def test_replace_rows_inserts_updates_and_deletes_only_what_changed(store: Store) -> None:
    rows = [
        {"issue_key": "AIL-1", "kind": "remote_link", "target": "https://a", "title": "A"},
        {"issue_key": "AIL-1", "kind": "remote_link", "target": "https://b", "title": "B"},
    ]
    first = store.replace_rows("links", {"issue_key": "AIL-1"}, rows)
    assert (first.inserted, first.deleted) == (2, 0)
    again = store.replace_rows("links", {"issue_key": "AIL-1"}, rows)
    assert again.written == 0 and again.unchanged == 2
    changed = store.replace_rows(
        "links",
        {"issue_key": "AIL-1"},
        [{"issue_key": "AIL-1", "kind": "remote_link", "target": "https://a", "title": "A2"}],
    )
    assert (changed.updated, changed.deleted) == (1, 1)
    assert store.count("links") == 1


def test_replace_rows_never_touches_other_issues(store: Store) -> None:
    store.upsert("links", {"issue_key": "AIL-2", "kind": "issue_link", "target": "AIL-9", "title": "blocks"})
    store.replace_rows("links", {"issue_key": "AIL-1"}, [])
    assert store.count("links", {"issue_key": "AIL-2"}) == 1


def test_replace_rows_refuses_rows_outside_the_filter(store: Store) -> None:
    with pytest.raises(ValueError, match="does not match"):
        store.replace_rows(
            "links", {"issue_key": "AIL-1"}, [{"issue_key": "AIL-2", "kind": "x", "target": "y"}]
        )
    with pytest.raises(ValueError, match="non-empty"):
        store.replace_rows("links", {}, [])


def test_delete_issue_removes_children(store: Store) -> None:
    store.upsert("issues", _issue())
    store.upsert("worklogs", {"worklog_id": "w1", "issue_key": "AIL-1", "seconds": 60})
    store.upsert(
        "status_changes",
        {"issue_key": "AIL-1", "changelog_id": "1", "changed_at": "2026-01-01T00:00:00.000Z"},
    )
    stats = store.delete_issue("AIL-1")
    assert stats.deleted == 3
    assert store.count("issues") == store.count("worklogs") == store.count("status_changes") == 0


def test_upsert_issue_handles_a_moved_issue(store: Store) -> None:
    store.upsert_issue(_issue("AIL-1", "100"))
    store.upsert("worklogs", {"worklog_id": "w1", "issue_key": "AIL-1", "seconds": 60})
    stats = store.upsert_issue(_issue("NEW-7", "100", project_key="NEW"))
    assert stats.inserted == 1 and stats.deleted == 2
    assert store.issue_id_map() == {"100": "NEW-7"}


def test_transaction_rolls_back_on_error(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.upsert("issues", _issue())
            raise RuntimeError("boom")
    assert store.count("issues") == 0
    with store.transaction():
        with store.transaction():  # nested blocks join the outer one
            store.upsert("issues", _issue())
    assert store.count("issues") == 1


def test_runs_record_status_and_stats(store: Store) -> None:
    started = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    run = store.start_run("sync", started_at=started)
    assert run.started_at == "2026-11-04T09:00:00.000Z"
    assert store.last_run("sync") is None  # still running
    store.finish_run(run, status="succeeded", stats={"rows": 3})
    last = store.last_run("sync")
    assert last["run_id"] == run.run_id and last["stats_json"] == '{"rows": 3}'


def test_cursors_and_account_timezone(store: Store) -> None:
    assert store.get_cursor("AIL") is None
    store.set_cursor("AIL", last_synced_at="2026-11-04T09:00:00.000Z", run_id="r1", account_timezone="America/Chicago")
    store.set_cursor("AIL", last_synced_at="2026-11-04T10:00:00.000Z", run_id="r2", account_timezone="America/Chicago")
    cursor = store.get_cursor("AIL")
    assert cursor.last_synced_at == "2026-11-04T10:00:00.000Z" and cursor.last_run_id == "r2"
    assert cursor.account_timezone == "America/Chicago"


def test_readonly_connection_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    with Store.open(path) as s:
        s.upsert("issues", _issue())
    conn = open_readonly(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM issues")
    finally:
        conn.close()


def test_write_stats_arithmetic() -> None:
    a = WriteStats(inserted=1, unchanged=2)
    a += WriteStats(updated=3, deleted=4)
    assert a.written == 8 and a.as_dict() == {"inserted": 1, "updated": 3, "deleted": 4, "unchanged": 2}
