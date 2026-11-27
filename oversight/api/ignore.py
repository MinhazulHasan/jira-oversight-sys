"""Ignoring findings without changing rules: the bodies, what an ignore would cover, the writes, the list."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from oversight.api.access_log import Viewer, names_someone, plain_text
from oversight.api.deps import ApiContext, ApiError
from oversight.api.rule_settings import _writer
from oversight.api.timelines import (
    CATEGORIES,
    FINDING_ID,
    ISSUE_KEY,
    Categories,
    FindingRecord,
    issue_category,
    key_number,
    load_records,
)
from oversight.ask.catalog import _rows
from oversight.logs import get_logger
from oversight.rules.ignores import (
    IGNORED,
    REASON_MAX,
    SCOPES,
    IgnoreInfo,
    Scope,
    active_ignores,
    add_ignores,
    resolve_ignored,
    restore_ignores,
)
from oversight.timeutil import to_iso

log = get_logger("api")

INVALID = "ignore_invalid"
BUSY = "ignore_busy"
BODY_LIMIT = 256 * 1024  # bytes
RESTORE_BODY_LIMIT = 1024 * 1024  # bytes: an ignore returns up to MAX_IGNORES ids and an undo sends them all back
MAX_TARGETS = 10_000  # finding ids or issue keys in one request
MAX_IGNORES = 25_000  # ignores one request may make, and so the ids one restore may name
BUSY_DETAIL = "The rules are running on this board right now, so nothing was changed. Try again in a moment."
_TOO_LARGE = "The request body is larger than {} KB."
_TOO_MANY = "That would make more than {:,} ignores in one request. Narrow the target."
_NOT_AN_OBJECT = "The request body must be a JSON object."
_TARGETS = ("finding_ids", "issue_keys", "query")
_PREVIEW_KEYS = frozenset({"target", "scope"})
_IGNORE_KEYS = frozenset({"target", "scope", "reason"})
_RESTORE_KEYS = frozenset({"ignore_ids"})
_IGNORE_ID = re.compile(r"[0-9a-f]{32}")
_KEY_QUERY = re.compile(r"[A-Z][A-Z0-9_]+-[0-9]+")  # as the findings list reads q
_NUMBER_QUERY = re.compile(r"#?([0-9]{1,9})")
_CHUNK = 500  # keys per IN (...) list, well under SQLite's variable limit


@dataclass(frozen=True)
class Problem:
    field: str | None
    message: str

    def as_dict(self) -> dict[str, str | None]:
        return {"field": self.field, "message": self.message}


def invalid(problems: Sequence[Problem]) -> ApiError:
    """The 422 for a refused body, each problem at its field"""
    detail = " ".join(dict.fromkeys(problem.message for problem in problems))
    return ApiError(422, INVALID, detail, extra={"errors": [problem.as_dict() for problem in problems]})


def too_large(limit: int = BODY_LIMIT) -> ApiError:
    return invalid([Problem(None, _TOO_LARGE.format(limit // 1024))])


# --- reading a body ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """Exactly one of: findings by id, tickets by key, or the findings filters (checked by the route)."""

    finding_ids: tuple[str, ...] = ()
    issue_keys: tuple[str, ...] = ()
    query: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class IgnoreRequest:
    target: Target
    scope: Scope
    reason: str | None = None


def parse_preview(raw: bytes) -> IgnoreRequest:
    """A preview's body; 422 for anything else."""
    body = _object(raw)
    problems = _unknown(body, _PREVIEW_KEYS)
    request = _request(body, problems)
    if problems:
        raise invalid(problems)
    return request


def parse_ignore(raw: bytes) -> IgnoreRequest:
    """An ignore's body; 422 for anything else."""
    body = _object(raw)
    problems = _unknown(body, _IGNORE_KEYS)
    request = _request(body, problems)
    reason = _reason(body.get("reason"), problems)
    if problems:
        raise invalid(problems)
    return IgnoreRequest(request.target, request.scope, reason)


def parse_restore(raw: bytes) -> list[str]:
    """A restore's ignore ids, each once; 422 for anything else."""
    body = _object(raw, RESTORE_BODY_LIMIT)
    problems = _unknown(body, _RESTORE_KEYS)
    ids = _ids(body.get("ignore_ids"), "ignore_ids", _IGNORE_ID, "an ignore id", problems, MAX_IGNORES)
    if problems:
        raise invalid(problems)
    return ids


def _object(raw: bytes, limit: int = BODY_LIMIT) -> dict[str, Any]:
    if len(raw) > limit:
        raise too_large(limit)
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise invalid([Problem(None, _NOT_AN_OBJECT)]) from None
    if not isinstance(body, dict):
        raise invalid([Problem(None, _NOT_AN_OBJECT)])
    return body


def _no_constant(name: str) -> Any:
    """NaN and Infinity are not JSON, whatever Python's reader allows."""
    raise ValueError(name)


def _cut(name: object) -> str:
    """A name from the body, short enough to say back."""
    return str(name)[:40]


def _unknown(body: Mapping[str, Any], allowed: frozenset[str], at: str = "") -> list[Problem]:
    return [Problem(f"{at}{_cut(name)}", f"{_cut(name)} is not something this request takes.")
            for name in sorted(set(body) - allowed, key=str)]


def _request(body: Mapping[str, Any], problems: list[Problem]) -> IgnoreRequest:
    scope = body.get("scope")
    target = _target(body.get("target"), problems)
    if scope not in SCOPES:
        problems.append(Problem("scope", "The scope must be finding (only this rule) or ticket (every rule on it)."))
        return IgnoreRequest(target, "finding")
    if target.issue_keys and scope != "ticket":
        problems.append(Problem("scope", "Tickets named by key are ignored whole: the scope must be ticket."))
    return IgnoreRequest(target, scope)


def _target(value: object, problems: list[Problem]) -> Target:
    if not isinstance(value, dict):
        problems.append(Problem("target", "The target must be an object naming finding_ids, issue_keys or query."))
        return Target()
    problems.extend(_unknown(value, frozenset(_TARGETS), "target."))
    named = [kind for kind in _TARGETS if kind in value]
    if len(named) != 1:
        problems.append(Problem("target", "The target must name exactly one of finding_ids, issue_keys or query."))
        return Target()
    if named[0] == "query":
        query = value["query"]
        if not isinstance(query, dict):
            problems.append(Problem("target.query", "The query must be an object of the findings filters."))
            return Target(query={})
        return Target(query=query)
    if named[0] == "finding_ids":
        return Target(finding_ids=tuple(_ids(value["finding_ids"], "target.finding_ids", FINDING_ID, "a finding id",
                                             problems)))
    return Target(issue_keys=tuple(_ids(value["issue_keys"], "target.issue_keys", ISSUE_KEY, "an issue key",
                                        problems)))


def _ids(value: object, at: str, pattern: re.Pattern[str], what: str, problems: list[Problem],
         limit: int = MAX_TARGETS) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        problems.append(Problem(at, f"{at.rpartition('.')[2]} must list 1 to {limit:,} of them."))
        return []
    for number, item in enumerate(value, start=1):
        if not isinstance(item, str) or not pattern.fullmatch(item):
            problems.append(Problem(at, f"Item {number} of {at.rpartition('.')[2]} is not {what}."))
            return []
    return list(dict.fromkeys(value))


def _reason(value: object, problems: list[Problem]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        problems.append(Problem("reason", "The reason must be text."))
        return None
    text = plain_text(value)
    if len(text) > REASON_MAX:
        problems.append(Problem("reason", f"The reason can be at most {REASON_MAX} characters."))
        return None
    if names_someone(text):
        problems.append(Problem("reason", "The reason cannot hold an email address or an account id."))
        return None
    return text or None


# --- what an ignore covers ----------------------------------------------------------------------------------------

Pair = tuple[str, str | None]  # (ticket, rule id); None: every rule on the ticket


@dataclass(frozen=True)
class Plan:
    """What an ignore of a target does now: the ignores it needs, the open findings it resolves."""

    pairs: tuple[Pair, ...]  # each once, in the target's order
    findings: tuple[FindingRecord, ...]  # the open findings the pairs cover
    already: int  # the target's findings ignored already

    @property
    def tickets(self) -> int:
        """The tickets the findings it resolves sit on."""
        return len({_key(record) for record in self.findings})

    def by_rule(self, ctx: ApiContext) -> list[dict[str, Any]]:
        """The findings it resolves, by rule: the board's rules in their order, then any other by id."""
        counts = Counter(record.rule_id for record in self.findings)
        order = [rule.id for rule in ctx.rules.rules if rule.id in counts]
        order += sorted(set(counts) - set(order))
        return [{"rule_id": rule_id, "rule_title": ctx.rules.title_of(rule_id), "findings": counts[rule_id]}
                for rule_id in order]


def plan(
    records: Sequence[FindingRecord],
    target: Target,
    scope: Scope,
    *,
    keep: Callable[[FindingRecord], bool] | None = None,
    tickets: Iterable[str] = (),
) -> Plan:
    """keep: the query's filters, every one but the state; tickets: the board's issue keys."""
    ticketed = [record for record in records if record.sprint is None]  # a sprint finding is no ticket's
    if target.issue_keys:
        on_board = set(tickets)
        keys = [key for key in target.issue_keys if key in on_board]
        named_keys = set(keys)
        named = [record for record in ticketed if _key(record) in named_keys]
        pairs: list[Pair] = [(key, None) for key in keys]
    else:
        if target.finding_ids:
            by_id = {record.finding_id: record for record in ticketed}
            named = [by_id[finding_id] for finding_id in target.finding_ids if finding_id in by_id]
            seeds = named
        else:
            named = [record for record in ticketed if keep is None or keep(record)]
            seeds = [record for record in named if record.state == "open"]
        pairs = [(_key(record), None if scope == "ticket" else record.rule_id) for record in seeds]
    pairs = list(dict.fromkeys(pairs))
    whole = {key for key, rule_id in pairs if rule_id is None}
    single = {pair for pair in pairs if pair[1] is not None}
    covered = tuple(record for record in ticketed if record.state == "open"
                    and (_key(record) in whole or (_key(record), record.rule_id) in single))
    return Plan(tuple(pairs), covered, sum(record.state == IGNORED for record in named))


def _key(record: FindingRecord) -> str:
    """The ticket a finding is stored under."""
    return str(record.row["issue_key"])


def board_tickets(conn: sqlite3.Connection, project: str) -> set[str]:
    return {row[0] for row in _rows(conn, "SELECT issue_key FROM issues WHERE project_key = ?", (project,))}


def preview_body(plan_now: Plan, ctx: ApiContext, meta: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "meta": meta,
        "findings": len(plan_now.findings),
        "tickets": plan_now.tickets,
        "by_rule": plan_now.by_rule(ctx),
        "already_ignored": plan_now.already,
    }


# --- writing -----------------------------------------------------------------------------------------------------


def apply_ignore(
    ctx: ApiContext,
    asked: IgnoreRequest,
    viewer: Viewer,
    keep: Callable[[FindingRecord], bool] | None,
) -> tuple[Plan, list[str]]:
    """Ignore the target: the plan carried out, and the ids of the ignores it made, in one write transaction."""
    store = _writer(ctx.db_path)
    try:
        with store.transaction():  # the plan is read under the write lock: what it says is what is written
            now = plan(load_records(store.conn, ctx), asked.target, asked.scope, keep=keep,
                       tickets=board_tickets(store.conn, ctx.project) if asked.target.issue_keys else ())
            if len(now.pairs) > MAX_IGNORES:
                raise invalid([Problem("target", _TOO_MANY.format(MAX_IGNORES))])
            at = to_iso(ctx.now()) or ""
            made = add_ignores(store.conn, ctx.project, now.pairs, reason=asked.reason, by=viewer.name, at=at)
            resolve_ignored(store.conn, ctx.project, [record.finding_id for record in now.findings], at)
        return now, made
    except sqlite3.OperationalError as exc:
        if _busy(exc):
            log.warning("findings_ignore_busy", project=ctx.project)
            raise ApiError(503, BUSY, BUSY_DETAIL) from None
        raise
    finally:
        store.close()


def apply_restore(ctx: ApiContext, ignore_ids: Sequence[str], viewer: Viewer) -> int:
    """End these of the board's active ignores; how many were active."""
    store = _writer(ctx.db_path)
    try:
        with store.transaction():
            return restore_ignores(store.conn, ctx.project, ignore_ids, by=viewer.name, at=to_iso(ctx.now()) or "")
    except sqlite3.OperationalError as exc:
        if _busy(exc):
            log.warning("findings_restore_busy", project=ctx.project)
            raise ApiError(503, BUSY, BUSY_DETAIL) from None
        raise
    finally:
        store.close()


def _busy(exc: sqlite3.OperationalError) -> bool:
    """A rules run held the write lock past the wait: nothing was written."""
    return str(getattr(exc, "sqlite_errorname", "")).startswith(("SQLITE_BUSY", "SQLITE_LOCKED"))


# --- the ignores on a board ---------------------------------------------------------------------------------------

_ISSUES_SQL = "SELECT issue_key, summary, issue_type, status, status_category FROM issues WHERE issue_key IN ({marks})"


@dataclass(frozen=True)
class IgnoredQuery:
    page: int = 1
    page_size: int = 25
    rules: tuple[str, ...] = ()
    q: str | None = None
    sort: str = "at"
    order: str = "desc"


@dataclass
class _Held:
    """What one ignore holds now."""

    findings: int = 0
    rules: set[str] = field(default_factory=set)


def ignored_page(conn: sqlite3.Connection, ctx: ApiContext, query: IgnoredQuery) -> dict[str, Any]:
    """GET /findings/ignored's items, total, page and page size (the route adds meta)."""
    ignores = active_ignores(conn, ctx.project)
    held: dict[str, _Held] = defaultdict(_Held)
    for record in load_records(conn, ctx, " AND f.resolved_why = 'ignored'"):
        if record.ignore is not None:
            held[record.ignore.ignore_id].findings += 1
            held[record.ignore.ignore_id].rules.add(record.rule_id)
    every = [*ignores.tickets.items(), *((key, info) for (key, _rule), info in ignores.findings.items())]
    issues = _issues(conn, [key for key, _info in every])
    categories = Categories(conn, ctx)
    items = [_item(key, info, issues.get(key), held.get(info.ignore_id) or _Held(), ctx, categories)
             for key, info in every]
    wanted = set(query.rules)
    if wanted:
        items = [item for item in items
                 if item["rule_id"] in wanted or (item["rule_id"] is None and _rules_held(held, item) & wanted)]
    if query.q:
        items = [item for item in items if _found(item, query.q)]
    items = _sorted(items, query.sort, query.order)
    start = (query.page - 1) * query.page_size
    return {"items": items[start : start + query.page_size], "total": len(items), "page": query.page,
            "page_size": query.page_size}


def _rules_held(held: Mapping[str, _Held], item: Mapping[str, Any]) -> set[str]:
    """The rules of the findings a whole ticket's ignore holds now."""
    found = held.get(item["ignore_id"])
    return found.rules if found is not None else set()


def _issues(conn: sqlite3.Connection, keys: Sequence[str]) -> dict[str, sqlite3.Row]:
    wanted = sorted(set(keys))
    found: dict[str, sqlite3.Row] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        found.update((row["issue_key"], row)
                     for row in _rows(conn, _ISSUES_SQL.format(marks=", ".join("?" * len(chunk))), chunk))
    return found


def _item(
    key: str, info: IgnoreInfo, issue: sqlite3.Row | None, held: _Held, ctx: ApiContext, categories: Categories
) -> dict[str, Any]:
    category = issue_category(issue, ctx, categories) if issue is not None else None
    return {
        "ignore_id": info.ignore_id,
        "scope": info.scope,
        "issue_key": key,
        "issue_summary": issue["summary"] if issue is not None else None,
        "issue_type": issue["issue_type"] if issue is not None else None,
        "status": issue["status"] if issue is not None else None,
        "status_category": category if category in CATEGORIES else None,
        "rule_id": info.rule_id,
        "rule_title": ctx.rules.title_of(info.rule_id) if info.rule_id is not None else None,
        "findings": held.findings,
        "reason": info.reason,
        "by": info.by,
        "at": info.at,
    }


def _found(item: Mapping[str, Any], q: str) -> bool:
    """q as the findings list reads it: an exact key, a key's number, or words in the key or summary."""
    text, upper, key = q.strip().lower(), q.strip().upper(), str(item["issue_key"])
    summary = str(item["issue_summary"] or "").lower()
    if _KEY_QUERY.fullmatch(upper):
        return key == upper or text in summary
    number = _NUMBER_QUERY.fullmatch(text)
    if number and key.endswith(f"-{int(number.group(1))}"):
        return True
    return text in key.lower() or text in summary


def _sorted(items: list[dict[str, Any]], sort: str, order: str) -> list[dict[str, Any]]:
    """The column in the chosen order; ties newest first, then by id."""
    ordered = sorted(items, key=lambda item: item["ignore_id"])
    ordered.sort(key=lambda item: item["at"], reverse=True)
    if sort == "issue_key":
        ordered.sort(key=lambda item: (item["issue_key"].rpartition("-")[0], key_number(item["issue_key"]) or 0),
                     reverse=order == "desc")
    elif sort == "rule_id":
        present = [item for item in ordered if item["rule_id"] is not None]
        empty = [item for item in ordered if item["rule_id"] is None]  # a whole ticket's: last in either order
        present.sort(key=lambda item: item["rule_id"], reverse=order == "desc")
        return present + empty
    else:
        ordered.sort(key=lambda item: item["at"], reverse=order == "desc")
    return ordered
