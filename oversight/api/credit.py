from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

from oversight.api.people import Person, fold, person, person_id
from oversight.ask.catalog import _rows
from oversight.config import FIELD_CANDIDATES, FieldsConfig, ProcessConfig
from oversight.jira.mapping import people_named
from oversight.store.db import Store

Basis = Literal["assignee_now", "field"]
Source = Literal["auto", "assignee", "field"]
SOURCES: tuple[str, ...] = ("auto", "assignee", "field")
AUTO_FIELD = "programmer"  # the people field Automatic reads first, where the site has it
_CHUNK = 500  # accounts per IN (...) list, well under SQLite's variable limit


class Credit(NamedTuple):
    person_id: str
    basis: Basis
    field: str | None  # the Jira field's name when basis is "field"


class Choice(NamedTuple):
    source: Source
    field: str | None = None  # a people field's logical name in fields.yaml, exactly when source is "field"


AUTOMATIC = Choice("auto")


@dataclass(frozen=True)
class Setting:
    choice: Choice
    revision: int  # 0 while config/process.yaml's choice stands
    changed_by: str | None = None
    changed_at: str | None = None


@dataclass(frozen=True)
class PeopleField:
    key: str  # its logical name in fields.yaml
    name: str  # Jira's name for it
    ids: tuple[str, ...]  # Jira's ids for it, one per team-managed board that has its own; none: it names no one


class StaleRevision(Exception):
    def __init__(self, revision: int) -> None:
        super().__init__(f"the board's credit setting is at revision {revision}")
        self.revision = revision


def people_fields(fields: FieldsConfig) -> list[PeopleField]:
    """The site's fields that name people, by name."""
    found = [PeopleField(key, ref.name, ref.ids) for key, ref in fields.people_fields().items()]
    return sorted(found, key=lambda one: (fold(one.name), one.key))


def field_named(fields: FieldsConfig, key: str | None) -> dict[str, str] | None:
    """{key, name} of a people field, named as the site names it; None for no field."""
    if key is None:
        return None
    ref = fields.fields.get(key)
    name = ref.name if ref is not None else (FIELD_CANDIDATES.get(key) or (key,))[0]
    return {"key": key, "name": name}


def step_one(choice: Choice, fields: FieldsConfig) -> PeopleField | None:
    """The people field the choice reads: Automatic's Programmer, the chosen field, or none (Assignee)."""
    if choice.source == "assignee":
        return None
    wanted = AUTO_FIELD if choice.source == "auto" else choice.field
    found = next((one for one in people_fields(fields) if one.key == wanted), None)
    named = field_named(fields, choice.field)
    if found is None and choice.source == "field" and named is not None:
        # A chosen field the site has lost is still the choice: it reads as naming no one, not as Assignee.
        return PeopleField(named["key"], named["name"], ())
    return found


# --- the setting ------------------------------------------------------------------------------------------------

_TABLE_SQL = "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'people_credit'"
_SETTING_SQL = "SELECT source, field_key, revision, changed_by, changed_at FROM people_credit WHERE project_key = ?"
_SAVE_SQL = """
    INSERT INTO people_credit (project_key, source, field_key, revision, changed_by, changed_at)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (project_key) DO UPDATE SET source = excluded.source, field_key = excluded.field_key,
      revision = excluded.revision, changed_by = excluded.changed_by, changed_at = excluded.changed_at
"""


def _stored(conn: sqlite3.Connection) -> bool:
    """Whether the snapshot keeps credit settings (version 12 on)."""
    return int(conn.execute(_TABLE_SQL).fetchone()[0]) == 1


def file_choice(process: ProcessConfig, project: str) -> Choice:
    """The board's choice in config/process.yaml; Automatic for a board it does not name."""
    config = process.projects.get(project)
    return Choice(config.credit.source, config.credit.field) if config is not None else AUTOMATIC


def board_setting(conn: sqlite3.Connection, process: ProcessConfig, project: str) -> Setting:
    """The board's credit setting: the one an admin saved, else config/process.yaml's."""
    if _stored(conn):
        for row in _rows(conn, _SETTING_SQL, (project,)):
            return Setting(_choice(row["source"], row["field_key"]), int(row["revision"] or 0),
                           row["changed_by"] or None, row["changed_at"] or None)
    return Setting(file_choice(process, project), 0)


def _choice(source: object, field: object) -> Choice:
    if source == "field" and isinstance(field, str) and field:
        return Choice("field", field)
    return Choice("assignee") if source == "assignee" else AUTOMATIC


def save_setting(store: Store, process: ProcessConfig, project: str, choice: Choice, *, revision: int, by: str,
                 at: str) -> Setting:
    """Save an admin's choice for the board from the revision they read: StaleRevision when that is not the current."""
    with store.transaction():
        current = board_setting(store.conn, process, project)
        if revision != current.revision:
            raise StaleRevision(current.revision)
        if choice == current.choice:
            return current
        saved = Setting(choice, current.revision + 1, by, at)
        store.conn.execute(_SAVE_SQL, (project, choice.source, choice.field, saved.revision, by, at))
    return saved


# --- the rule ---------------------------------------------------------------------------------------------------

# Every ticket on the board with its current assignee and, for each people field asked about, what it holds now.
_TICKETS_SQL = "SELECT i.issue_key, i.issue_type, i.assignee_id{fields} FROM issues AS i WHERE i.project_key = ?"
_FIELD_SQL = (
    ", CASE WHEN json_valid(i.raw_json) THEN CASE WHEN json_type(i.raw_json, ?) IN ('array', 'object')"
    " THEN json_extract(i.raw_json, ?) END END AS f{n}"
)
_PEOPLE_SQL = "SELECT account_id, display_name, avatar_hash FROM people WHERE account_id IN ({marks})"


@dataclass(frozen=True)
class _Facts:
    issue_type: str | None
    assignee: str | None
    users: Mapping[str, tuple[str, ...]]  # people field key -> the accounts it names now


@dataclass(frozen=True)
class BoardCredit:
    """Whom each of a board's tickets counts for (its builders), under the board's credit setting"""

    setting: Setting
    field: PeopleField | None  # the people field it reads; None: none (Assignee, or Automatic without a Programmer)
    builders: Mapping[str, tuple[Credit, ...]]  # issue key -> its builders; none: it counts for nobody
    accounts: Mapping[str, str]  # person id -> account id, every builder's
    people: Mapping[str, Person]  # person id -> the person, every builder's

    def of(self, issue_key: str) -> tuple[Credit, ...]:
        return self.builders.get(issue_key, ())


@dataclass(frozen=True)
class Coverage:
    """How one choice credits the board's work finished in a period"""

    choice: Choice
    source_filled: int  # finished tickets its field fills (Automatic: Programmer; Assignee: assigned now)
    credited: int  # finished tickets that count for someone under it


def board_credit(conn: sqlite3.Connection, process: ProcessConfig, fields: FieldsConfig, project: str,
                 setting: Setting | None = None) -> BoardCredit:
    """The builders of every ticket on the board, under its credit setting (see _builders)."""
    setting = setting if setting is not None else board_setting(conn, process, project)
    field = step_one(setting.choice, fields)
    assignee = setting.choice.source != "field"
    builders: dict[str, tuple[Credit, ...]] = {}
    accounts: dict[str, str] = {}
    for key, facts in _read(conn, project, [field] if field is not None else []).items():
        found = _builders(facts, field, assignee=assignee)
        if found:
            builders[key] = tuple(credit for credit, _account in found)
            accounts.update((credit.person_id, account) for credit, account in found)
    return BoardCredit(setting, field, builders, accounts, _people(conn, accounts.values()))


def coverage(conn: sqlite3.Connection, fields: FieldsConfig, project: str, *, excluded: Collection[str],
             finished: Collection[str]) -> tuple[list[PeopleField], list[Coverage]]:
    """The people fields filled on the board's work, and each choice it offers counted over the finished tickets."""
    every = people_fields(fields)
    facts = _read(conn, project, every)
    work = [found for found in facts.values() if (found.issue_type or "").casefold() not in excluded]
    eligible = [one for one in every if any(found.users.get(one.key) for found in work)]
    done = [facts[key] for key in finished if key in facts]
    automatic = step_one(AUTOMATIC, fields)

    def counted(field: PeopleField | None, *, assignee: bool) -> int:
        return sum(bool(_builders(found, field, assignee=assignee)) for found in done)

    assigned = counted(None, assignee=True)
    options = [Coverage(AUTOMATIC, counted(automatic, assignee=False), counted(automatic, assignee=True)),
               Coverage(Choice("assignee"), assigned, assigned)]
    for one in eligible:
        filled = counted(one, assignee=False)
        options.append(Coverage(Choice("field", one.key), filled, filled))
    return eligible, options


def _builders(facts: _Facts, field: PeopleField | None, *, assignee: bool) -> tuple[tuple[Credit, str], ...]:
    """Everyone the field names now; where it names no one and the choice reads the assignee, the assignee now"""
    users = facts.users.get(field.key, ()) if field is not None else ()
    if field is not None and users:
        return tuple((Credit(person_id(account), "field", field.name), account) for account in users)
    if assignee and facts.assignee is not None:
        return ((Credit(person_id(facts.assignee), "assignee_now", None), facts.assignee),)
    return ()


def _read(conn: sqlite3.Connection, project: str, wanted: Sequence[PeopleField]) -> dict[str, _Facts]:
    readable = [(one.key, field_id) for one in wanted for field_id in one.ids]
    sql = _TICKETS_SQL.format(fields="".join(_FIELD_SQL.format(n=n) for n in range(len(readable))))
    params = [_path(field_id) for _key, field_id in readable for _use in ("json_type", "json_extract")]
    found: dict[str, _Facts] = {}
    for row in _rows(conn, sql, (*params, project)):
        users: dict[str, tuple[str, ...]] = {}
        for n, (field_key, _field_id) in enumerate(readable):
            # A ticket holds only its own board's copy of the field: the first that names anyone is it.
            if not users.get(field_key):
                users[field_key] = _users(row[f"f{n}"])
        found[row["issue_key"]] = _Facts(row["issue_type"], _account(row["assignee_id"]), users)
    return found


def _path(field_id: str) -> str:
    return f'$.fields."{field_id}"'


def _account(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _users(value: object) -> tuple[str, ...]:
    """The accounts a people field holds now, from its JSON as the snapshot keeps it"""
    if not isinstance(value, str):
        return ()
    try:
        return people_named(json.loads(value))
    except (ValueError, RecursionError):
        return ()


def _people(conn: sqlite3.Connection, accounts: Iterable[str]) -> dict[str, Person]:
    """Each account as a person, by its people row's name and picture (an unknown one: "Unnamed account")"""
    wanted = sorted(set(accounts))
    found: dict[str, Person] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        rows = {row["account_id"]: row
                for row in _rows(conn, _PEOPLE_SQL.format(marks=", ".join("?" * len(chunk))), chunk)}
        for account in chunk:
            row = rows.get(account)
            who = person(account, row["display_name"] if row else None, row["avatar_hash"] if row else None)
            if who is not None:
                found[who["id"]] = who
    return found
