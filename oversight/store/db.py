"""SQLite snapshot store: connection, schema, change-aware upserts, run bookkeeping."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from oversight.errors import ConfigError
from oversight.logs import get_logger
from oversight.timeutil import to_iso, utcnow

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 12

_NEW_COLUMNS: dict[int, tuple[tuple[str, str, str], ...]] = {
    4: (
        ("people", "avatar_url", "TEXT"),
        ("people", "avatar", "BLOB"),
        ("people", "avatar_type", "TEXT"),
        ("people", "avatar_hash", "TEXT"),
        ("people", "avatar_source", "TEXT"),
        ("people", "avatar_fetched_at", "TEXT"),
    ),
    # Every question asked before version 6 was answered at team level: the default records that.
    5: (
        ("qa_log", "level", "TEXT NOT NULL DEFAULT 'team'"),
        ("qa_log", "mentions_json", "TEXT"),
    ),
    # A finding and a rule's status written before version 9 ran with rules.yaml's params: NULL records that.
    8: (
        ("findings", "settings_hash", "TEXT"),
        ("rule_status", "settings_hash", "TEXT"),
    ),
}

_REBUILT: dict[int, tuple[str, ...]] = {2: ("findings", "qa_log", "rule_status")}

_MIGRATIONS: dict[int, str] = {
    # findings and qa_log gained columns; nothing had written to them before Phase 3.
    2: "DROP TABLE IF EXISTS findings; DROP TABLE IF EXISTS qa_log;",
    3: """
        CREATE TABLE IF NOT EXISTS people (
          account_id   TEXT PRIMARY KEY,
          display_name TEXT,
          active       INTEGER,
          updated_at   TEXT NOT NULL
        );
        WITH assigned AS MATERIALIZED (
          SELECT issue_key, project_key, updated_at, json_extract(raw_json, '$.fields.assignee') AS assignee
          FROM issues
          WHERE CASE WHEN json_valid(raw_json)
                     THEN json_type(raw_json, '$.fields.assignee.accountId') = 'text'
                          AND json_extract(raw_json, '$.fields.assignee.accountId') != '' END
        ),
        ranked AS (
          SELECT json_extract(a.assignee, '$.accountId') AS account_id,
                 CASE json_type(a.assignee, '$.displayName')
                   WHEN 'text' THEN NULLIF(json_extract(a.assignee, '$.displayName'), '') END AS display_name,
                 CASE json_type(a.assignee, '$.active') WHEN 'true' THEN 1 WHEN 'false' THEN 0 END AS active,
                 max(s.last_synced_at) OVER accounts AS seen_at,
                 row_number() OVER (accounts ORDER BY a.updated_at DESC, a.issue_key DESC) AS latest
          FROM assigned AS a LEFT JOIN sync_state AS s ON s.scope = a.project_key
          WINDOW accounts AS (PARTITION BY json_extract(a.assignee, '$.accountId'))
        )
        INSERT INTO people (account_id, display_name, active, updated_at)
        SELECT account_id, display_name, active, COALESCE(seen_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        FROM ranked
        WHERE latest = 1
        ON CONFLICT (account_id) DO NOTHING;
        UPDATE people
        SET display_name = CASE account_id
          WHEN 'acc-01' THEN 'Ada Brooks' WHEN 'acc-02' THEN 'Ben Okafor' WHEN 'acc-03' THEN 'Carmen Ruiz'
          WHEN 'acc-04' THEN 'Dev Malhotra' WHEN 'acc-05' THEN 'Elif Aydin' WHEN 'acc-06' THEN 'Finn Gallagher'
          WHEN 'acc-07' THEN 'Grace Lindqvist' WHEN 'acc-08' THEN 'Hana Mori' END
        WHERE display_name IS NULL
          AND account_id IN ('acc-01', 'acc-02', 'acc-03', 'acc-04', 'acc-05', 'acc-06', 'acc-07', 'acc-08')
          AND NOT EXISTS (
            SELECT 1 FROM issues AS i
            WHERE i.assignee_id = people.account_id
              AND COALESCE(CASE WHEN json_valid(i.raw_json)
                                THEN json_extract(i.raw_json, '$.fields.assignee.self') END, '')
                  NOT LIKE 'https://demo.invalid/%'
          );
    """,
    4: """
        WITH assigned AS MATERIALIZED (
          SELECT issue_key, updated_at, json_extract(raw_json, '$.fields.assignee') AS assignee
          FROM issues
          WHERE CASE WHEN json_valid(raw_json)
                     THEN json_type(raw_json, '$.fields.assignee.accountId') = 'text'
                          AND json_extract(raw_json, '$.fields.assignee.accountId') != '' END
        ),
        ranked AS MATERIALIZED (
          SELECT json_extract(assignee, '$.accountId') AS account_id,
                 CASE json_type(assignee, '$.avatarUrls."48x48"')
                   WHEN 'text' THEN NULLIF(json_extract(assignee, '$.avatarUrls."48x48"'), '') END AS avatar_url,
                 row_number() OVER (PARTITION BY json_extract(assignee, '$.accountId')
                                    ORDER BY updated_at DESC, issue_key DESC) AS latest
          FROM assigned
        )
        UPDATE people
        SET avatar_url = ranked.avatar_url
        FROM ranked
        WHERE ranked.account_id = people.account_id AND ranked.latest = 1
          AND ranked.avatar_url IS NOT NULL AND people.avatar_url IS NULL;
    """,
    5: "-- qa_log: level and mentions_json (_NEW_COLUMNS[5])",
    6: "-- board_members: created by the schema script",
    7: "-- access_log and the time indexes: created by the schema script",
    8: "-- settings_hash (_NEW_COLUMNS[8]); rule_settings and rule_settings_changes: created by the schema script",
    9: "-- boards: created by the schema script",
    10: "-- custom_rules: created by the schema script",
    11: "-- finding_ignores and people_credit: created by the schema script",
}

# Primary keys of the tables written through upsert/replace_rows.
TABLE_PKS: dict[str, tuple[str, ...]] = {
    "issues": ("issue_key",),
    "status_changes": ("issue_key", "changelog_id"),
    "field_changes": ("issue_key", "changelog_id", "field"),
    "worklogs": ("worklog_id",),
    "links": ("issue_key", "kind", "target"),
    "statuses": ("project_key", "status_id"),
    "people": ("account_id",),
    "board_members": ("project_key", "account_id"),
    "issue_metrics": ("issue_key",),
    "findings": ("finding_id",),
    "rule_status": ("run_id", "project_key", "rule_id"),
    "warning_links": ("warning_finding_id", "breach_finding_id"),
    "digests": ("digest_id",),
    "qa_log": ("qa_id",),
    "llm_calls": ("call_id",),
    "insights": ("insight_id",),
    "access_log": ("id",),
    "rule_settings": ("project_key", "rule_id"),
    "rule_settings_changes": ("change_id",),
    "boards": ("project_key",),
    "custom_rules": ("rule_id",),
    "finding_ignores": ("ignore_id",),
}

# Tables holding rows that belong to one issue (keyed by issue_key).
ISSUE_CHILD_TABLES: tuple[str, ...] = (
    "status_changes",
    "field_changes",
    "worklogs",
    "links",
    "status_intervals",
    "issue_metrics",
)

# Tables a sync writes; "rows written" in sync results counts these.
SYNC_TABLES: tuple[str, ...] = ("issues", "status_changes", "field_changes", "worklogs", "links", "statuses")

Outcome = Literal["inserted", "updated", "unchanged"]

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
log = get_logger("store")


def _ident(name: str) -> str:
    """Table and column names come from code, never from data; check anyway."""
    if not _IDENT.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def encode(value: Any) -> Any:
    """Lists and dicts become stable JSON, so an unchanged payload compares equal."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


@dataclass
class WriteStats:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0

    @property
    def written(self) -> int:
        return self.inserted + self.updated + self.deleted

    def add(self, outcome: Outcome) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)

    def __iadd__(self, other: WriteStats) -> WriteStats:
        self.inserted += other.inserted
        self.updated += other.updated
        self.deleted += other.deleted
        self.unchanged += other.unchanged
        return self

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
        }


@dataclass(frozen=True)
class Run:
    run_id: str
    kind: str
    started_at: str


@dataclass(frozen=True)
class SyncCursor:
    scope: str
    last_synced_at: str | None
    last_run_id: str | None
    account_timezone: str | None


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path | None = None) -> None:
        self.conn = conn
        self.path = path

    # --- lifecycle --------------------------------------------------------------

    @classmethod
    def open(cls, path: Path | str) -> Store:
        """Open (creating if needed) the database at `path`; ":memory:" works for tests."""
        if str(path) == ":memory:":
            conn = sqlite3.connect(":memory:", isolation_level=None)
            file_path = None
        else:
            file_path = Path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(file_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        if file_path is not None:
            conn.execute("PRAGMA journal_mode = WAL")
        store = cls(conn, file_path)
        try:
            store._init_schema()
        except BaseException:
            conn.close()  # a refused or failed migration leaves no open handle on the file
            raise
        return store

    def _init_schema(self) -> None:
        version = self._version()
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION or (version != 0 and version not in _MIGRATIONS):
            raise ConfigError(
                f"database schema version {version}, expected {SCHEMA_VERSION}: "
                f"delete {self.path} and run `python -m oversight sync --full`"
            )
        steps: list[str] = []
        while version != 0 and version < SCHEMA_VERSION:
            log.info("schema_migrate", from_version=version, to_version=version + 1)
            steps.append(self._new_columns(version))
            steps.append(_MIGRATIONS[version])
            version += 1
        steps.append(SCHEMA_PATH.read_text(encoding="utf-8"))
        steps.append(f"PRAGMA user_version = {SCHEMA_VERSION};")
        try:
            self.conn.executescript("BEGIN IMMEDIATE;\n" + "\n".join(steps) + "\nCOMMIT;")
        except BaseException as exc:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error) and self._version() == SCHEMA_VERSION:
                log.info("schema_migrated_elsewhere", version=SCHEMA_VERSION)
                return
            raise

    def _version(self) -> int:
        version: int = self.conn.execute("PRAGMA user_version").fetchone()[0]
        return version

    def _new_columns(self, version: int) -> str:
        """ALTER TABLE statements for the columns version + 1 adds (_NEW_COLUMNS) that are not there yet."""
        # Called while the steps are gathered, before any runs: the file is still at the version it started at.
        rebuilt = {table for step in range(self._version(), version) for table in _REBUILT.get(step, ())}
        statements = []
        for table, column, kind in _NEW_COLUMNS.get(version, ()):
            if table in rebuilt:
                continue
            present = {row[1] for row in self.conn.execute(f"PRAGMA table_info({_ident(table)})")}
            if column not in present:
                statements.append(f"ALTER TABLE {_ident(table)} ADD COLUMN {_ident(column)} {kind};")
        return "\n".join(statements)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One atomic unit of writes. Inside an open transaction it just joins it."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # --- generic reads ----------------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def count(self, table: str, where: Mapping[str, Any] | None = None) -> int:
        sql = f"SELECT COUNT(*) FROM {_ident(table)}"
        params: list[Any] = []
        if where:
            sql += " WHERE " + " AND ".join(f"{_ident(c)} = ?" for c in where)
            params = [encode(v) for v in where.values()]
        return self.conn.execute(sql, params).fetchone()[0]

    # --- change-aware writes ------------------------------------------------------

    def upsert(self, table: str, row: Mapping[str, Any]) -> Outcome:
        """Insert the row, or update it only if a value differs. Says which happened."""
        pk = TABLE_PKS[table]
        missing = [c for c in pk if c not in row]
        if missing:
            raise ValueError(f"{table} row is missing key columns {missing}")
        cols = [_ident(c) for c in row]
        values = [encode(row[c]) for c in cols]
        pk_sql = " AND ".join(f"{c} = ?" for c in pk)
        existed = (
            self.conn.execute(
                f"SELECT 1 FROM {table} WHERE {pk_sql}", [encode(row[c]) for c in pk]
            ).fetchone()
            is not None
        )
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        non_pk = [c for c in cols if c not in pk]
        if non_pk:
            sets = ", ".join(f"{c} = excluded.{c}" for c in non_pk)
            changed = " OR ".join(f"{table}.{c} IS NOT excluded.{c}" for c in non_pk)
            sql += f" ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {sets} WHERE {changed}"
        else:
            sql += f" ON CONFLICT ({', '.join(pk)}) DO NOTHING"
        cursor = self.conn.execute(sql, values)
        if cursor.rowcount == 0:
            return "unchanged"
        return "updated" if existed else "inserted"

    def upsert_many(self, table: str, rows: Iterable[Mapping[str, Any]]) -> WriteStats:
        stats = WriteStats()
        for row in rows:
            stats.add(self.upsert(table, row))
        return stats

    def upsert_issue(self, row: Mapping[str, Any]) -> WriteStats:
        """Upsert an issue row."""
        stats = WriteStats()
        other = self.conn.execute(
            "SELECT issue_key FROM issues WHERE issue_id = ? AND issue_key != ?",
            (row["issue_id"], row["issue_key"]),
        ).fetchone()
        if other is not None:
            log.warning("issue_rekeyed", old_key=other["issue_key"], new_key=row["issue_key"])
            stats += self.delete_issue(other["issue_key"])
        stats.add(self.upsert("issues", row))
        return stats

    def replace_rows(
        self, table: str, where: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]
    ) -> WriteStats:
        """Make the rows matching `where` exactly `rows`: upsert each, delete the rest."""
        if not where:
            raise ValueError("replace_rows needs a non-empty filter")
        pk = TABLE_PKS[table]
        rows = list(rows)
        for row in rows:
            for col, value in where.items():
                if encode(row.get(col)) != encode(value):
                    raise ValueError(f"{table} row does not match the filter on {col}")
        stats = WriteStats()
        keep: set[tuple[Any, ...]] = set()
        for row in rows:
            stats.add(self.upsert(table, row))
            keep.add(tuple(encode(row[c]) for c in pk))
        cond = " AND ".join(f"{_ident(c)} = ?" for c in where)
        existing = self.conn.execute(
            f"SELECT {', '.join(pk)} FROM {table} WHERE {cond}", [encode(v) for v in where.values()]
        ).fetchall()
        pk_sql = " AND ".join(f"{c} = ?" for c in pk)
        for record in existing:
            key = tuple(record)
            if key not in keep:
                self.conn.execute(f"DELETE FROM {table} WHERE {pk_sql}", key)
                stats.deleted += 1
        return stats

    def delete_rows(self, table: str, where: Mapping[str, Any]) -> int:
        if not where:
            raise ValueError("delete_rows needs a non-empty filter")
        cond = " AND ".join(f"{_ident(c)} = ?" for c in where)
        cursor = self.conn.execute(
            f"DELETE FROM {_ident(table)} WHERE {cond}", [encode(v) for v in where.values()]
        )
        return cursor.rowcount

    def delete_issue(self, issue_key: str) -> WriteStats:
        """Remove an issue and every row that belongs to it."""
        stats = WriteStats()
        for table in ISSUE_CHILD_TABLES:
            stats.deleted += self.delete_rows(table, {"issue_key": issue_key})
        stats.deleted += self.delete_rows("issues", {"issue_key": issue_key})
        return stats

    # --- lookups -------------------------------------------------------------------

    def issue_id_map(self) -> dict[str, str]:
        """issue_id -> issue_key for every stored issue."""
        return {r["issue_id"]: r["issue_key"] for r in self.query("SELECT issue_id, issue_key FROM issues")}

    def issue_keys(self, project_key: str) -> set[str]:
        rows = self.query("SELECT issue_key FROM issues WHERE project_key = ?", (project_key,))
        return {r["issue_key"] for r in rows}

    # --- runs ------------------------------------------------------------------------

    def start_run(
        self, kind: str, *, rules_version: str | None = None, started_at: datetime | None = None
    ) -> Run:
        run = Run(run_id=uuid.uuid4().hex, kind=kind, started_at=to_iso(started_at or utcnow()))
        self.conn.execute(
            "INSERT INTO runs (run_id, kind, status, started_at, rules_version) VALUES (?, ?, 'running', ?, ?)",
            (run.run_id, run.kind, run.started_at, rules_version),
        )
        return run

    def finish_run(
        self,
        run: Run,
        *,
        status: Literal["succeeded", "failed"],
        stats: Mapping[str, Any] | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, stats_json = ? WHERE run_id = ?",
            (
                status,
                to_iso(finished_at or utcnow()),
                json.dumps(stats or {}, sort_keys=True, default=str),
                run.run_id,
            ),
        )

    def last_run(self, kind: str, *, status: str = "succeeded") -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM runs WHERE kind = ? AND status = ? ORDER BY started_at DESC LIMIT 1",
            (kind, status),
        )

    # --- sync cursors ------------------------------------------------------------------

    def get_cursor(self, scope: str) -> SyncCursor | None:
        row = self.query_one("SELECT * FROM sync_state WHERE scope = ?", (scope,))
        if row is None:
            return None
        return SyncCursor(
            scope=row["scope"],
            last_synced_at=row["last_synced_at"],
            last_run_id=row["last_run_id"],
            account_timezone=row["account_timezone"],
        )

    def set_cursor(
        self, scope: str, *, last_synced_at: str, run_id: str, account_timezone: str | None
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO sync_state (scope, last_synced_at, last_run_id, account_timezone)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (scope) DO UPDATE SET
              last_synced_at = excluded.last_synced_at,
              last_run_id = excluded.last_run_id,
              account_timezone = excluded.account_timezone
            """,
            (scope, last_synced_at, run_id, account_timezone),
        )


def open_readonly(path: Path | str, *, any_thread: bool = False) -> sqlite3.Connection:
    """A connection that cannot write: for every AI and reporting path."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=not any_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn
