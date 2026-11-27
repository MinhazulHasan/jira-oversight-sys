"""Findings an admin chose to ignore on a board: one rule on a ticket, or every rule on it."""
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

Scope = Literal["finding", "ticket"]
SCOPES: tuple[str, ...] = ("finding", "ticket")
IGNORED = "ignored"  # findings.resolved_why of a finding an ignore holds
REASON_MAX = 500  # characters
_CHUNK = 500  # ids per IN (...) list, well under SQLite's variable limit


class IgnoreInfo(NamedTuple):
    ignore_id: str
    scope: Scope
    rule_id: str | None  # None: every rule on the ticket
    reason: str | None
    by: str | None  # the admin's display name
    at: str

    def body(self, *, people: bool = True) -> dict[str, Any]:
        """The contract's ignore object; who made it only at people level."""
        return {"scope": self.scope, "rule_id": self.rule_id, "reason": self.reason,
                "by": self.by if people else None, "at": self.at}


@dataclass(frozen=True)
class Ignores:
    """A board's active ignores: whole tickets, and single rules on a ticket."""

    tickets: Mapping[str, IgnoreInfo] = field(default_factory=dict)
    findings: Mapping[tuple[str, str], IgnoreInfo] = field(default_factory=dict)

    def ticket(self, issue_key: str) -> IgnoreInfo | None:
        return self.tickets.get(issue_key)

    def covers(self, issue_key: str, rule_ids: Iterable[str]) -> IgnoreInfo | None:
        """The ignore that covers any of these rules on the ticket: the ticket's own first."""
        found = self.tickets.get(issue_key)
        if found is not None:
            return found
        for rule_id in rule_ids:
            found = self.findings.get((issue_key, rule_id))
            if found is not None:
                return found
        return None

    def holds(self, issue_key: str, rule_id: str | None) -> IgnoreInfo | None:
        """The active ignore of exactly this pair (rule_id None: the ticket's own)."""
        return self.tickets.get(issue_key) if rule_id is None else self.findings.get((issue_key, rule_id))

    def __len__(self) -> int:
        return len(self.tickets) + len(self.findings)


_TABLE_SQL = "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'finding_ignores'"
_ACTIVE_SQL = (
    "SELECT ignore_id, issue_key, rule_id, reason, created_by, created_at FROM finding_ignores"
    " WHERE project_key = ? AND restored_at IS NULL ORDER BY created_at, ignore_id"
)
_INSERT_SQL = (
    "INSERT INTO finding_ignores (ignore_id, project_key, issue_key, rule_id, scope, reason, created_by, created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_RESOLVE_SQL = (
    "UPDATE findings SET resolved_at = ?, resolved_why = 'ignored'"
    " WHERE resolved_at IS NULL AND project_key = ? AND finding_id IN ({marks})"
)
_RESTORE_SQL = (
    "UPDATE finding_ignores SET restored_by = ?, restored_at = ?"
    " WHERE project_key = ? AND restored_at IS NULL AND ignore_id IN ({marks})"
)


def _has_table(conn: sqlite3.Connection) -> bool:
    """Whether the snapshot has finding_ignores (version 12 on)."""
    return int(conn.execute(_TABLE_SQL).fetchone()[0]) == 1


def active_ignores(conn: sqlite3.Connection, project: str) -> Ignores:
    if not _has_table(conn):
        return Ignores()
    tickets: dict[str, IgnoreInfo] = {}
    findings: dict[tuple[str, str], IgnoreInfo] = {}
    for ignore_id, issue_key, rule_id, reason, by, at in conn.execute(_ACTIVE_SQL, (project,)).fetchall():
        if rule_id is None:
            tickets[issue_key] = IgnoreInfo(ignore_id, "ticket", None, reason or None, by or None, at)
        else:
            findings[(issue_key, rule_id)] = IgnoreInfo(ignore_id, "finding", rule_id, reason or None, by or None, at)
    return Ignores(tickets, findings)


def add_ignores(
    conn: sqlite3.Connection,
    project: str,
    pairs: Iterable[tuple[str, str | None]],
    *,
    reason: str | None,
    by: str,
    at: str,
) -> list[str]:
    """An ignore for each (ticket, rule or None) pair that has no active one; the ids made, in the pairs' order."""
    active = active_ignores(conn, project)
    rows: list[tuple[Any, ...]] = []
    for issue_key, rule_id in dict.fromkeys(pairs):
        if active.holds(issue_key, rule_id) is not None:
            continue  # an ignore of the same pair holds already: reused, never doubled
        scope = "ticket" if rule_id is None else "finding"
        rows.append((uuid.uuid4().hex, project, issue_key, rule_id, scope, reason, by, at))
    conn.executemany(_INSERT_SQL, rows)
    return [row[0] for row in rows]


def resolve_ignored(conn: sqlite3.Connection, project: str, finding_ids: Sequence[str], at: str) -> int:
    """Resolve these of the board's open findings as ignored; how many were."""
    return _in_chunks(conn, _RESOLVE_SQL, (at, project), finding_ids)


def restore_ignores(conn: sqlite3.Connection, project: str, ignore_ids: Sequence[str], *, by: str, at: str) -> int:
    """End these of the board's active ignores (the rows stay, as restored); how many there were."""
    return _in_chunks(conn, _RESTORE_SQL, (by, at, project), ignore_ids)


def _in_chunks(conn: sqlite3.Connection, sql: str, leading: tuple[Any, ...], ids: Sequence[str]) -> int:
    changed = 0
    unique = list(dict.fromkeys(ids))
    for start in range(0, len(unique), _CHUNK):
        chunk = unique[start : start + _CHUNK]
        changed += conn.execute(sql.format(marks=", ".join("?" * len(chunk))), (*leading, *chunk)).rowcount
    return changed
