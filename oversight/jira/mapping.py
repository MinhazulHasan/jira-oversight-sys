"""Jira JSON to our rows. Pure functions: no I/O, no database."""
from __future__ import annotations

import math
import re
from typing import Any

from oversight.config import SYSTEM_TRACKED, FieldsConfig
from oversight.errors import JiraError
from oversight.timeutil import iso

_LEGACY_SPRINT_ID = re.compile(r"[\[,]id=([0-9]+)")

# What an Epic Link field holds: an issue key such as "AIL-5".
_ISSUE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[0-9]+")


def map_issue(issue: dict[str, Any], fields: FieldsConfig) -> dict[str, Any]:
    """One search result -> an `issues` row."""
    issue_key = _required(_dig(issue, "key"), "an issue without a key")
    values = _dig(issue, "fields")
    parent_key = _text(_dig(values, "parent", "key"))
    points_id = fields.id_of("story_points")
    sprint_id = fields.id_of("sprint")
    return {
        "issue_key": issue_key,
        "issue_id": _required(_dig(issue, "id"), f"issue {issue_key} without an id"),
        "project_key": _text(_dig(values, "project", "key")) or issue_key.rsplit("-", 1)[0],
        "issue_type": _text(_dig(values, "issuetype", "name")),
        "summary": _text(_dig(values, "summary")),
        "status": _text(_dig(values, "status", "name")),
        "status_category": _text(_dig(values, "status", "statusCategory", "key")),
        "assignee_id": _text(_dig(values, "assignee", "accountId")),
        "parent_key": parent_key,
        "epic_key": _epic_key(values, parent_key, fields),
        "created_at": _timestamp(_dig(values, "created"), f"'created' of {issue_key}"),
        "updated_at": _timestamp(_dig(values, "updated"), f"'updated' of {issue_key}"),
        "resolved_at": _timestamp(_dig(values, "resolutiondate"), f"'resolutiondate' of {issue_key}"),
        "original_estimate_s": _seconds(_dig(values, "timeoriginalestimate")),
        "remaining_estimate_s": _seconds(_dig(values, "timeestimate")),
        "time_spent_s": _seconds(_dig(values, "timespent")),
        "aggregate_time_spent_s": _seconds(_dig(values, "aggregatetimespent")),
        "story_points": _number(_dig(values, points_id)) if points_id else None,
        "sprint_ids": _sprint_ids(_dig(values, sprint_id)) if sprint_id else None,
        "raw_json": issue,
    }


def map_assignee(issue: dict[str, Any], seen_at: str) -> dict[str, Any] | None:
    """A search result's assignee -> a `people` row, or None when nobody is assigned."""
    assignee = _dig(issue, "fields", "assignee")
    account_id = _text(_dig(assignee, "accountId"))
    if account_id is None:
        return None
    active = _dig(assignee, "active")
    return {
        "account_id": account_id,
        "display_name": _text(_dig(assignee, "displayName")),
        "active": int(active) if isinstance(active, bool) else None,
        "updated_at": seen_at,
        "avatar_url": _text(_dig(assignee, "avatarUrls", "48x48")),
    }


def map_member(user: dict[str, Any], seen_at: str) -> dict[str, Any] | None:
    """One assignable user (GET user/assignable/search) -> a `people` row, or None."""
    account_id = _text(_dig(user, "accountId"))
    if account_id is None or _dig(user, "accountType") != "atlassian" or _dig(user, "active") is not True:
        return None
    return {
        "account_id": account_id,
        "display_name": _text(_dig(user, "displayName")),
        "active": 1,
        "updated_at": seen_at,
        "avatar_url": _text(_dig(user, "avatarUrls", "48x48")),
    }


def map_status_changes(issue_key: str, histories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Changelog histories -> `status_changes` rows, sorted by (changed_at, numeric id)."""
    rows: dict[str, dict[str, Any]] = {}
    for history in histories or ():
        item = next((item for item in _items(history) if _is_status_item(item)), None)
        if item is None:
            continue
        changelog_id = _history_id(issue_key, history)
        if changelog_id in rows:
            # The same history on two bulk-fetch pages: identical, keep one.
            continue
        rows[changelog_id] = {
            "issue_key": issue_key,
            "changelog_id": changelog_id,
            "changed_at": _history_time(issue_key, history, changelog_id),
            "author_id": _text(_dig(history, "author", "accountId")),
            "from_status": _text(item.get("fromString")),
            "to_status": _text(item.get("toString")),
        }
    return sorted(rows.values(), key=lambda row: (row["changed_at"], _id_order(row["changelog_id"])))


def map_field_changes(
    issue_key: str, histories: list[dict[str, Any]], fields: FieldsConfig
) -> list[dict[str, Any]]:
    """Changelog histories -> `field_changes` rows for tracked fields other than status."""
    tracked = {
        field_id: logical
        for field_id, logical in fields.tracked_changelog_fields().items()
        if logical != "status"
    }
    by_name = _tracked_names(tracked, fields)
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for history in histories or ():
        matched = [
            (logical, item)
            for item in _items(history)
            if (logical := _tracked_field(item, tracked, by_name)) is not None
        ]
        if not matched:
            continue
        changelog_id = _history_id(issue_key, history)
        changed_at = _history_time(issue_key, history, changelog_id)
        for logical, item in matched:
            # A later item for the same field overwrites the earlier one: the last change wins.
            rows[(changelog_id, logical)] = {
                "issue_key": issue_key,
                "changelog_id": changelog_id,
                "field": logical,
                "changed_at": changed_at,
                "from_value": _change_value(item, "from", "fromString"),
                "to_value": _change_value(item, "to", "toString"),
            }
    return sorted(
        rows.values(),
        key=lambda row: (row["changed_at"], _id_order(row["changelog_id"]), row["field"]),
    )


def map_worklog(issue_key: str, worklog: dict[str, Any]) -> dict[str, Any]:
    """A worklog -> a `worklogs` row."""
    worklog_id = _required(_dig(worklog, "id"), f"a worklog of {issue_key} without an id")
    where = f"worklog {worklog_id} of {issue_key}"
    return {
        "worklog_id": worklog_id,
        "issue_key": issue_key,
        "started_at": _timestamp(_dig(worklog, "started"), f"'started' of {where}"),
        "seconds": _seconds(_dig(worklog, "timeSpentSeconds")),
        "author_id": _text(_dig(worklog, "author", "accountId")),
        "updated_at": _timestamp(_dig(worklog, "updated"), f"'updated' of {where}"),
    }


def map_remote_links(issue_key: str, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remote links -> `links` rows: kind "remote_link", target = object.url, title = object.title."""
    rows: dict[str, dict[str, Any]] = {}
    for link in links or ():
        url = _dig(link, "object", "url")
        target = url.strip() if isinstance(url, str) else ""
        if not target or target in rows:
            continue
        rows[target] = {
            "issue_key": issue_key,
            "kind": "remote_link",
            "target": target,
            "title": _text(_dig(link, "object", "title")),
        }
    return list(rows.values())


def map_issue_links(issue: dict[str, Any]) -> list[dict[str, Any]]:
    """fields.issuelinks -> `links` rows: kind "issue_link"."""
    issue_key = _required(_dig(issue, "key"), "an issue without a key")
    links = _dig(issue, "fields", "issuelinks")
    titles: dict[str, list[str]] = {}
    for link in links if isinstance(links, list) else ():
        for side, direction in (("outwardIssue", "outward"), ("inwardIssue", "inward")):
            target = _text(_dig(link, side, "key"))
            if target is None:
                continue
            names = titles.setdefault(target, [])
            title = _text(_dig(link, "type", direction))
            if title is not None and title not in names:
                names.append(title)
    return [
        {
            "issue_key": issue_key,
            "kind": "issue_link",
            "target": target,
            "title": "; ".join(sorted(names)) or None,
        }
        for target, names in titles.items()
    ]


def map_statuses(project_key: str, project_statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """GET project/{key}/statuses -> `statuses` rows."""
    rows: dict[str, dict[str, Any]] = {}
    for issue_type in project_statuses or ():
        statuses = _dig(issue_type, "statuses")
        for status in statuses if isinstance(statuses, list) else ():
            status_id = _required(_dig(status, "id"), f"a status of project {project_key} without an id")
            if status_id in rows:
                # Every issue type lists the statuses it uses, so they repeat.
                continue
            rows[status_id] = {
                "project_key": project_key,
                "status_id": status_id,
                "name": _required(
                    _dig(status, "name"), f"status {status_id} of project {project_key} without a name"
                ),
                "category": _text(_dig(status, "statusCategory", "key")),
            }
    return list(rows.values())


# --- helpers --------------------------------------------------------------------


def _dig(value: Any, *path: str) -> Any:
    """value[path[0]][path[1]]..., or None as soon as a step is missing, null or not an object."""
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _text(value: Any) -> str | None:
    """A non-empty string, or None."""
    return value if isinstance(value, str) and value else None


def _bad_payload(what: str) -> JiraError:
    return JiraError(f"unexpected Jira payload: {what}")


def _required(value: Any, what: str) -> str:
    """A key, id or name that a row cannot exist without, as text."""
    if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "":
        raise _bad_payload(what)
    return str(value)


def _timestamp(value: Any, what: str) -> str | None:
    """A Jira timestamp in the storage format; None when missing."""
    try:
        return iso(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _bad_payload(f"{what} is not a timestamp") from exc


def _number(value: Any) -> float | None:
    """A finite float from a number or a numeric string; None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _seconds(value: Any) -> int | None:
    """Jira durations: integer seconds or null (numeric strings tolerated)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    number = _number(value)
    return None if number is None else round(number)


def _sprint_ids(value: Any) -> list[int]:
    entries = value if isinstance(value, list) else [value]
    return sorted({sprint for entry in entries if (sprint := _sprint_id(entry)) is not None})


def _sprint_id(entry: Any) -> int | None:
    """One sprint field entry ({"id": 4427, ...} or a legacy string) -> its id."""
    if isinstance(entry, dict):
        entry = entry.get("id")
    if isinstance(entry, bool):
        return None
    if isinstance(entry, int):
        return entry
    if isinstance(entry, str):
        text = entry.strip()
        if text.isascii() and text.isdigit():
            return int(text)
        match = _LEGACY_SPRINT_ID.search(text)
        if match:
            return int(match.group(1))
    return None


def _is_epic(issue_type: Any) -> bool:
    """An epic by hierarchy level 1; by the name "Epic" only when the level is absent."""
    level = _number(_dig(issue_type, "hierarchyLevel"))
    if level is not None:
        return level == 1
    name = _dig(issue_type, "name")
    return isinstance(name, str) and name.strip().casefold() == "epic"


def _epic_key(values: Any, parent_key: str | None, fields: FieldsConfig) -> str | None:
    # Team-managed projects (AIL) link an issue to its epic through `parent`.
    if parent_key and _is_epic(_dig(values, "parent", "fields", "issuetype")):
        return parent_key
    # Company-managed projects may hold the epic's key in an Epic Link field instead.
    link_id = fields.id_of("epic_link")
    link = _dig(values, link_id) if link_id else None
    if isinstance(link, str) and _ISSUE_KEY.fullmatch(link.strip()):
        return link.strip()
    return None


def _items(history: Any) -> list[dict[str, Any]]:
    items = _dig(history, "items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _is_status_item(item: dict[str, Any]) -> bool:
    field_id = item.get("fieldId")
    if field_id:
        return field_id == "status"
    return item.get("field") == "status" and item.get("fieldtype") == "jira"


def _history_id(issue_key: str, history: Any) -> str:
    return _required(_dig(history, "id"), f"a changelog history of {issue_key} without an id")


def _history_time(issue_key: str, history: Any, changelog_id: str) -> str:
    where = f"changelog {changelog_id} of {issue_key}"
    changed_at = _timestamp(_dig(history, "created"), f"'created' of {where}")
    if changed_at is None:
        raise _bad_payload(f"{where} without a created time")
    return changed_at


def _id_order(changelog_id: str) -> tuple[int, int, str]:
    """Numeric ids compare as numbers ("9" before "10"); any other id sorts after them."""
    if changelog_id.isascii() and changelog_id.isdigit():
        return (0, int(changelog_id), "")
    return (1, 0, changelog_id)


def _tracked_names(tracked: dict[str, str], fields: FieldsConfig) -> dict[str, str]:
    """Casefolded display name -> logical name, for items that carry no fieldId."""
    candidates: dict[str, set[str]] = {}
    for field_id, logical in tracked.items():
        ref = None if field_id in SYSTEM_TRACKED else fields.fields.get(logical)
        name = ref.name if ref is not None else field_id
        candidates.setdefault(name.casefold(), set()).add(logical)
    return {name: logicals.pop() for name, logicals in candidates.items() if len(logicals) == 1}


def _tracked_field(item: dict[str, Any], tracked: dict[str, str], by_name: dict[str, str]) -> str | None:
    """The logical name of a tracked item, or None for anything we don't keep."""
    field_id = item.get("fieldId")
    if field_id:
        return tracked.get(str(field_id))
    name = _text(item.get("field"))
    return by_name.get(name.casefold()) if name else None


def _change_value(item: dict[str, Any], raw_key: str, text_key: str) -> str | None:
    """`from`/`to` when set, else `fromString`/`toString`; "" is None; values stay text."""
    value = item.get(raw_key)
    if value is None or value == "":
        value = item.get(text_key)
    if value is None or value == "":
        return None
    return value if isinstance(value, str) else str(value)
