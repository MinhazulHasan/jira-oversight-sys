"""People as the dashboard contract shows them: the one place a person object is made.

docs/API_CONTRACT.md (sections 2, 4.12, 4.13 and 4.14) in the jira-oversight-ui repository;
its zod schemas in src/lib/api/schemas.ts are the source of truth. Wherever a
route names a person it sends {"id", "name", "avatar"}, made by person():

- id: "p" and the first 12 hex characters of the SHA-256 of the Jira account id
  (UTF-8), for example "p3f9a2c41b0de". Every route makes it here, so they all
  name a person the same way, and no response holds the account id itself.
- name: the account's Jira display name as of the last sync (the people table),
  trimmed and at most 128 characters (code points, as Python's len and the
  interface's [...name].length count them); "Unnamed account" when Jira gave none,
  or gave a name with an email address in it (some invited or partly set-up
  accounts have their address as their display name), so an address is never
  sent as a name.
- avatar: the version of the person's Jira picture the service holds
  (people.avatar_hash: the first 12 hex characters of the SHA-256 of the stored
  bytes), or None when it holds none. A version, not a URL: the picture is served
  by GET /people/{id}/avatar, and Jira's picture URL (people.avatar_url) never
  leaves the service.

A person is on a board when they are assigned one of its tickets now, or are a
member from its last successful member fetch (board_members: the people Jira
lets its tickets be assigned to). GET /people and everything that counts work
reads the first; the directory (GET /people/directory), the picture route and
Ask's mention check read both (board_account here, oversight/api/routes/people.py).

A query that names people selects the people row's display_name and avatar_hash
beside the account id, and hands all three to person(). Unassigned work has no
person: null where a person goes, and "unassigned" (label "Unassigned", avatar
null) as the filter and facet value. Lists of people go by name, compared
lower-cased character by character, then by id. People are named by display
name, with their picture beside it, and nothing else: never by an email address,
Jira's picture URL or an account id. The people table holds no email address,
and the account id goes no further than person_id.
"""
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
    """The display name trimmed and cut to 128 characters (trimmed again after the cut); "Unnamed account"
    without one, and for a name holding an email address (looked for in the whole name, before the cut)."""
    text = display_name.strip() if isinstance(display_name, str) else ""
    if _EMAIL_IN_NAME.search(text):
        return UNNAMED
    return text[:NAME_MAX].rstrip() or UNNAMED


def avatar(avatar_hash: object) -> str | None:
    """The stored picture's version as the contract sends it (12 lower-case hex characters); None without one."""
    return avatar_hash if isinstance(avatar_hash, str) and AVATAR_VERSION.fullmatch(avatar_hash) else None


def person(account_id: object, display_name: object, avatar_hash: object) -> Person | None:
    """{"id", "name", "avatar"} for the account, from its people row's display_name and avatar_hash;
    None for nobody (unassigned)."""
    if not isinstance(account_id, str) or not account_id:
        return None
    return {"id": person_id(account_id), "name": person_name(display_name), "avatar": avatar(avatar_hash)}


def fold(text: str) -> str:
    """Lower case with the accents taken off, as a typed name is compared and the directory is ordered: "Élif Aydın"
    reads "elif aydin" (foldText in the dashboard's src/lib/names.ts)."""
    bare = "".join(char for char in unicodedata.normalize("NFD", text) if not unicodedata.category(char).startswith("M"))
    return bare.lower().translate(_UNFOLDED)


def directory_key(who: Person) -> tuple[str, str]:
    """The directory's order (GET /people/directory, the scorecard's people): name folded for case and accents, then
    id."""
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
    """Every person on the board (assigned one of its tickets now, or a member from its last member fetch), each
    once, by name lower-cased, then id: one query."""
    found = (
        person(row["account_id"], row["display_name"], row["avatar_hash"])
        for row in _rows(conn, _BOARD_EVERYONE_SQL, (project, project))
    )
    return sorted((who for who in found if who is not None), key=sort_key)


def board_account(conn: sqlite3.Connection, project: str, wanted: str) -> str | None:
    """The account behind a person id, when it is on the board (assigned one of its tickets now, or a member); None
    otherwise (a malformed id included). The id is one way, so the board's own accounts are hashed until one
    matches."""
    if not PERSON_ID.fullmatch(wanted):
        return None
    for row in _rows(conn, _BOARD_ACCOUNTS_SQL, (project, project)):
        if person_id(row["account_id"]) == wanted:
            account: str = row["account_id"]
            return account
    return None
