from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from typing import TypedDict

from oversight.ask.catalog import _rows

UNASSIGNED = "unassigned"
UNASSIGNED_LABEL = "Unassigned"
UNNAMED = "Unnamed account"
NAME_MAX = 128  # characters: code points, not UTF-16 units
PERSON_ID = re.compile(r"p[0-9a-f]{12}")
AVATAR_VERSION = re.compile(r"[0-9a-f]{12}")
# An email address anywhere in a display name: "someone@example.com", "Jo Bloggs (jo@corp.example)".
_EMAIL_IN_NAME = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
# Letters that keep a shape of their own when the accents come off, folded as the dashboard folds them.
_UNFOLDED = str.maketrans({"ı": "i", "ł": "l", "ø": "o", "đ": "d", "ð": "d", "þ": "th", "ß": "ss", "æ": "ae", "œ": "oe"})


class Person(TypedDict):
    id: str
    name: str
    avatar: str | None


# Every account assigned at least one of the board's tickets now, with its stored display name and picture version.
_BOARD_PEOPLE_SQL = """
    SELECT DISTINCT i.assignee_id AS account_id, a.display_name, a.avatar_hash
    FROM issues AS i LEFT JOIN people AS a ON a.account_id = i.assignee_id
    WHERE i.project_key = ? AND i.assignee_id IS NOT NULL AND i.assignee_id != ''
"""
# Every account on the board: assigned one of its tickets now, or a member from its last member fetch.
_BOARD_ACCOUNTS_SQL = """
    SELECT assignee_id AS account_id FROM issues WHERE project_key = ? AND assignee_id IS NOT NULL AND assignee_id != ''
    UNION
    SELECT account_id FROM board_members WHERE project_key = ?
"""
# The same accounts, with their stored display names and picture versions.
_BOARD_EVERYONE_SQL = f"""
    SELECT b.account_id, a.display_name, a.avatar_hash
    FROM ({_BOARD_ACCOUNTS_SQL}) AS b LEFT JOIN people AS a ON a.account_id = b.account_id
"""


def person_id(account_id: str) -> str:
    """The person's id: "p" and the first 12 hex characters of sha256(account id)."""
    return "p" + hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12]


def person_name(display_name: object) -> str:
    """The display name trimmed and cut to 128 characters (trimmed again after the cut)"""
    text = display_name.strip() if isinstance(display_name, str) else ""
    if _EMAIL_IN_NAME.search(text):
        return UNNAMED
    return text[:NAME_MAX].rstrip() or UNNAMED


def avatar(avatar_hash: object) -> str | None:
    """The stored picture's version as the contract sends it (12 lower-case hex characters); None without one."""
    return avatar_hash if isinstance(avatar_hash, str) and AVATAR_VERSION.fullmatch(avatar_hash) else None


def person(account_id: object, display_name: object, avatar_hash: object) -> Person | None:
    """{'id', 'name', 'avatar'} for the account, from its people row's display_name and avatar_hash"""
    if not isinstance(account_id, str) or not account_id:
        return None
    return {"id": person_id(account_id), "name": person_name(display_name), "avatar": avatar(avatar_hash)}


def fold(text: str) -> str:
    """Lower case with the accents taken off, as a typed name is compared and the directory is ordered"""
    bare = "".join(char for char in unicodedata.normalize("NFD", text) if not unicodedata.category(char).startswith("M"))
    return bare.lower().translate(_UNFOLDED)


def directory_key(who: Person) -> tuple[str, str]:
    """The directory's order (GET /people/directory, the scorecard's people)"""
    return fold(who["name"]), who["id"]


def sort_key(who: Person | None) -> tuple[str, str]:
    """By name lower-cased, then id; nobody sorts as "Unassigned", with its filter value as the id."""
    if who is None:
        return UNASSIGNED_LABEL.lower(), UNASSIGNED
    return who["name"].lower(), who["id"]


def board_people(conn: sqlite3.Connection, project: str) -> list[Person]:
    """Every person currently assigned at least one ticket on the board, by name lower-cased, then id."""
    found = (
        person(row["account_id"], row["display_name"], row["avatar_hash"])
        for row in _rows(conn, _BOARD_PEOPLE_SQL, (project,))
    )
    return sorted((who for who in found if who is not None), key=sort_key)


def board_everyone(conn: sqlite3.Connection, project: str) -> list[Person]:
    found = (
        person(row["account_id"], row["display_name"], row["avatar_hash"])
        for row in _rows(conn, _BOARD_EVERYONE_SQL, (project, project))
    )
    return sorted((who for who in found if who is not None), key=sort_key)


def board_account(conn: sqlite3.Connection, project: str, wanted: str) -> str | None:
    if not PERSON_ID.fullmatch(wanted):
        return None
    for row in _rows(conn, _BOARD_ACCOUNTS_SQL, (project, project)):
        if person_id(row["account_id"]) == wanted:
            account: str = row["account_id"]
            return account
    return None
