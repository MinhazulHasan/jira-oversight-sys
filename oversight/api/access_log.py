from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import HTTPException, Request

from oversight.api.deps import ApiError
from oversight.api.people import PERSON_ID, Person, board_everyone, fold
from oversight.logs import get_logger
from oversight.timeutil import to_iso

VIEWER_HEADER = "X-Oversight-Viewer"
VIEWER_NAME_HEADER = "X-Oversight-Viewer-Name"
LOCAL_VIEWER = "local"
LOCAL_VIEWER_NAME = "Local (sign-in off)"
SIGNED_IN = "Signed-in user"
FORMER_MEMBER = "Former member"
NAME_MAX = 128  # characters, as a person's name
_RAW_NAME_MAX = NAME_MAX * 12  # percent-encoded UTF-8: at most 4 bytes a character, each written %XX
VIEWER_HASH = re.compile(r"[0-9a-f]{32}")
_BIDI_CONTROLS = frozenset("‎‏‪‫‬‭‮⁦⁧⁨⁩")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
# Atlassian account ids: 24 hex characters, or a numeric prefix and a UUID ("557058:f58131cb-...").
_ACCOUNT_ID = re.compile(
    r"(?<![0-9A-Za-z])(?:[0-9a-f]{24}|[0-9A-Za-z]+:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?![0-9A-Za-z])"
)

SCORECARD_VIEW = "scorecard_view"
PEOPLE_COMPARE_VIEW = "people_compare_view"
PERSON_FINDINGS_VIEW = "person_findings_view"
PERSON_ASK = "person_ask"
ACCESS_LOG_VIEW = "access_log_view"
ACCESS_LOG_EXPORT = "access_log_export"
PERSON_CARD_VIEW = "person_card_view"
SCORECARD_TICKETS_VIEW = "scorecard_tickets_view"
ACTIONS: tuple[str, ...] = (
    SCORECARD_VIEW, PEOPLE_COMPARE_VIEW, PERSON_FINDINGS_VIEW, PERSON_ASK, ACCESS_LOG_VIEW, ACCESS_LOG_EXPORT,
    PERSON_CARD_VIEW, SCORECARD_TICKETS_VIEW,
)
PERSON_CARD_ONCE_IN = timedelta(minutes=10)
# A page of the log itself, at most once per viewer and person filtered by in this long: see the docstring.
ACCESS_LOG_VIEW_ONCE_IN = timedelta(minutes=10)
UNAVAILABLE = "access_log_unavailable"
_UNAVAILABLE_DETAIL = "the access log could not be written, so nothing was served"
_INSERT_SQL = (
    "INSERT INTO access_log (id, at, viewer_hash, viewer_name, level, project_key, action, subject_ids_json,"
    " detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
# An entry of the same action, viewer, board and subjects at or after a moment (the `at` index finds the moment).
_HELD_SQL = (
    "SELECT 1 FROM access_log WHERE at >= ? AND action = ? AND viewer_hash = ? AND project_key IS ?"
    " AND subject_ids_json = ? LIMIT 1"
)

log = get_logger("access_log")


# --- the viewer ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Viewer:
    hash: str
    name: str


def _problem(header: str, message: str, kind: str) -> HTTPException:
    return HTTPException(422, [{"loc": ["header", header], "msg": message, "type": kind}])


def _clean(char: str) -> str:
    """A control character (a line break, a tab) reads as a space"""
    category = unicodedata.category(char)
    if category == "Cc":
        return " "
    if char in _BIDI_CONTROLS or category in ("Cs", "Co", "Cn"):
        return ""
    return char


def plain_text(text: str) -> str:
    """Text as the log keeps a name"""
    return " ".join("".join(_clean(char) for char in text).split())


def names_someone(text: str) -> bool:
    """Whether the text holds an email address or an Atlassian account id: never kept, never shown."""
    return bool(_EMAIL.search(text) or _ACCOUNT_ID.search(text))


def viewer_name(raw: str | None, viewer_hash: str) -> str:
    """The name to keep for the viewer: the decoded display name, made safe, or the fallback (see the docstring)."""
    if viewer_hash == LOCAL_VIEWER:
        return LOCAL_VIEWER_NAME
    if not raw or len(raw) > _RAW_NAME_MAX:
        return SIGNED_IN
    try:
        text = unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return SIGNED_IN
    text = plain_text(text)
    if not text or len(text) > NAME_MAX or names_someone(text):
        return SIGNED_IN
    return text


def read_viewer(request: Request) -> Viewer | None:
    """The viewer the dashboard's server named; None when it named none; 422 for a hash that is not one."""
    found = request.headers.get(VIEWER_HEADER)
    if found is None:
        return None
    value = found.strip()
    if value != LOCAL_VIEWER and not VIEWER_HASH.fullmatch(value):
        raise _problem(VIEWER_HEADER, "must be 32 lower-case hex characters, or local", "value_error")
    return Viewer(hash=value, name=viewer_name(request.headers.get(VIEWER_NAME_HEADER), value))


def need_viewer(request: Request) -> Viewer:
    """The viewer, for a request that is recorded or counted: 422 when none is named."""
    viewer = read_viewer(request)
    if viewer is None:
        raise _problem(VIEWER_HEADER, "the viewer must be named for this request", "missing")
    return viewer


# --- writing ------------------------------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    """A connection that writes to the snapshot that exists, and never makes a new file."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def subject_ids(ids: Iterable[str | None]) -> list[str]:
    """Person ids, each once, in the order first given; anything that is not a person id is left out."""
    return list(dict.fromkeys(value for value in ids if isinstance(value, str) and PERSON_ID.fullmatch(value)))


def purge_access_log(db_path: Path, days: int, now: datetime) -> int:
    """Delete the entries older than `days`; returns how many went. Raises sqlite3.Error."""
    cutoff = to_iso(now - timedelta(days=days))
    conn = _connect(db_path)
    try:
        deleted = conn.execute("DELETE FROM access_log WHERE at < ?", (cutoff,)).rowcount
    finally:
        conn.close()
    log.info("access_log_purged", deleted=deleted, days=days)
    return int(deleted)


class AccessLog:
    """One app's access log: the entries it writes, and its once-a-day retention purge."""

    def __init__(self, db_path: Path, *, days: int, now: Callable[[], datetime]) -> None:
        self.db_path = Path(db_path)
        self.days = days
        self.now = now
        self._lock = threading.Lock()
        self._purged_at: datetime | None = None

    def record(
        self,
        *,
        viewer: Viewer,
        level: str,
        project: str | None,
        action: str,
        subjects: Sequence[str],
        detail: Mapping[str, Any],
        once_within: timedelta | None = None,
    ) -> str | None:
        """Write one entry and return its id; ApiError 500 (access_log_unavailable) when it cannot be written."""
        if action not in ACTIONS:
            raise ValueError(f"not an access log action: {action!r}")
        entry_id = uuid.uuid4().hex
        people = subject_ids(subjects)
        now = self.now()
        row = (
            entry_id, to_iso(now), viewer.hash, viewer.name, level, project, action,
            json.dumps(people), json.dumps(dict(detail), sort_keys=True, ensure_ascii=False),
        )
        try:
            conn = _connect(self.db_path)
            try:
                if once_within is None:
                    conn.execute(_INSERT_SQL, row)
                    held = False
                else:
                    held = self._write_unless_held(conn, row, to_iso(now - once_within) or "")
            finally:
                conn.close()  # an open transaction (a failure part way) is rolled back
        except sqlite3.Error as exc:
            log.error("access_log_write_failed", action=action, at_level=level, project=project,
                      subjects=len(people), error_type=type(exc).__name__)
            raise ApiError(500, UNAVAILABLE, _UNAVAILABLE_DETAIL) from exc
        if held:
            log.debug("access_log_already_held", action=action, at_level=level, project=project, subjects=len(people))
            return None
        log.info("access_logged", entry=entry_id, action=action, at_level=level, project=project,
                 subjects=len(people))
        return entry_id

    @staticmethod
    def _write_unless_held(conn: sqlite3.Connection, row: tuple[Any, ...], since: str) -> bool:
        """Write the entry unless one of its action, viewer, board and subjects is held from `since` on"""
        _id, _at, viewer_hash, _name, _level, project, action, subjects_json, _detail = row
        key = (since, action, viewer_hash, project, subjects_json)
        if conn.execute(_HELD_SQL, key).fetchone() is not None:
            return True
        conn.execute("BEGIN IMMEDIATE")
        held = conn.execute(_HELD_SQL, key).fetchone() is not None
        if not held:
            conn.execute(_INSERT_SQL, row)
        conn.execute("COMMIT")
        return held

    def due(self) -> bool:
        """Whether a day has passed since the last purge (or none has run): a cheap look, no database."""
        purged = self._purged_at
        return purged is None or self.now() - purged >= timedelta(days=1)

    def purge_if_due(self) -> None:
        """The retention purge, at most once a day; a failure is logged and never raised."""
        now = self.now()
        with self._lock:
            if self._purged_at is not None and now - self._purged_at < timedelta(days=1):
                return
            self._purged_at = now
        if not self.db_path.exists():
            return
        try:
            purge_access_log(self.db_path, self.days, now)
        except sqlite3.Error as exc:
            log.warning("access_log_purge_failed", error_type=type(exc).__name__)


# --- reading ------------------------------------------------------------------------------------------------

_STORED_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_ENTRY_ID = re.compile(r"[0-9a-f]{32}")


def encode_cursor(at: str, entry_id: str) -> str:
    """An opaque cursor naming the last entry of a page."""
    return base64.urlsafe_b64encode(f"{at}|{entry_id}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str] | None:
    """The entry a cursor names, (at, id); None when it is not a cursor this service made."""
    if not cursor or len(cursor) > 128:
        return None
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    at, _, entry_id = text.partition("|")
    if not _STORED_TIME.fullmatch(at) or not _ENTRY_ID.fullmatch(entry_id):
        return None
    return at, entry_id


@dataclass(frozen=True)
class LogQuery:
    """GET /access-log's filters, checked: times in the storage format, both optional ([start, end))."""

    start: str | None = None
    end: str | None = None
    actions: tuple[str, ...] = ()
    person: str | None = None
    viewer: str | None = None
    after: tuple[str, str] | None = None  # the cursor's entry: the page starts just after it
    limit: int = 50


def read_entries(conn: sqlite3.Connection, query: LogQuery) -> tuple[list[sqlite3.Row], bool]:
    """The page's rows, newest first, and whether more follow: one query."""
    where: list[str] = []
    params: list[Any] = []
    if query.start is not None:
        where.append("at >= ?")
        params.append(query.start)
    if query.end is not None:
        where.append("at < ?")
        params.append(query.end)
    if query.actions:
        where.append(f"action IN ({', '.join('?' * len(query.actions))})")
        params.extend(query.actions)
    if query.viewer is not None:
        where.append("viewer_hash = ?")
        params.append(query.viewer)
    if query.person is not None:
        where.append("EXISTS (SELECT 1 FROM json_each(access_log.subject_ids_json) AS s WHERE s.value = ?)")
        params.append(query.person)
    if query.after is not None:
        where.append("(at < ? OR (at = ? AND id < ?))")
        params.extend([query.after[0], query.after[0], query.after[1]])
    sql = (
        "SELECT id, at, viewer_hash, viewer_name, level, project_key, action, subject_ids_json, detail_json"
        " FROM access_log"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + " ORDER BY at DESC, id DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*params, query.limit + 1)).fetchall()
    return rows[: query.limit], len(rows) > query.limit


def everyone_served(conn: sqlite3.Connection, projects: Sequence[str]) -> dict[str, Person]:
    found: dict[str, Person] = {}
    for project in projects:
        for who in board_everyone(conn, project):
            found.setdefault(who["id"], who)
    return found


def former_member(person_id: str) -> Person:
    return {"id": person_id, "name": FORMER_MEMBER, "avatar": None}


# Each viewer once, by the name of their latest entry (SQLite takes a bare column from the row MAX() picks).
_FACET_VIEWERS_SQL = "SELECT viewer_hash, viewer_name, MAX(at) AS latest FROM access_log GROUP BY viewer_hash"
_FACET_SUBJECTS_SQL = "SELECT DISTINCT s.value AS person_id FROM access_log, json_each(access_log.subject_ids_json) AS s"


def read_facets(conn: sqlite3.Connection, people: Mapping[str, Person]) -> dict[str, Any]:
    viewers = sorted(
        ({"hash": row["viewer_hash"], "name": row["viewer_name"]} for row in conn.execute(_FACET_VIEWERS_SQL)),
        key=lambda viewer: (fold(viewer["name"]), viewer["hash"]),
    )
    ids = subject_ids(row["person_id"] for row in conn.execute(_FACET_SUBJECTS_SQL))
    subjects = sorted(
        (people.get(person_id) or former_member(person_id) for person_id in ids),
        key=lambda who: (who["id"] not in people, fold(who["name"]), who["id"]),
    )
    return {"viewers": viewers, "subjects": subjects}


def _json(text: str | None, fallback: Any) -> Any:
    try:
        value = json.loads(text) if text else fallback
    except ValueError:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def entry_body(row: sqlite3.Row, people: Mapping[str, Person]) -> dict[str, Any]:
    """One entry as the contract sends it (4.16)."""
    return {
        "id": row["id"],
        "at": row["at"],
        "viewer": {"hash": row["viewer_hash"], "name": row["viewer_name"]},
        "level": row["level"],
        "project": row["project_key"],
        "action": row["action"],
        "subjects": [people.get(person_id) or former_member(person_id)
                     for person_id in subject_ids(_json(row["subject_ids_json"], []))],
        "detail": _json(row["detail_json"], {}),
    }
