from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from oversight.errors import ConfigError

if TYPE_CHECKING:
    from oversight.rules.checks import RuleContext

SOURCE_KINDS: tuple[str, ...] = ("field", "status", "remote_link_contains", "label")


def evidence_source(ctx: RuleContext, name: str) -> tuple[str, str] | None:
    """The configured (kind, value) for the evidence name, None when process.yaml says null."""
    source = ctx.process.project(ctx.project).evidence.get(name)
    if source is None:
        return None
    if not isinstance(source, Mapping) or len(source) != 1:
        raise ConfigError(
            f"process.yaml {ctx.project} evidence {name!r}: expected one source such as "
            "{field: code_reviewers} or {status: \"In Review\"}"
        )
    kind, value = next(iter(source.items()))
    if kind not in SOURCE_KINDS or not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"process.yaml {ctx.project} evidence {name!r}: unknown source {kind!r} "
            f"(expected one of {', '.join(SOURCE_KINDS)} with a text value)"
        )
    return kind, value.strip()


def evidence_time(ctx: RuleContext, issue_key: str, name: str) -> str | None:
    source = evidence_source(ctx, name)
    if source is None:
        return None
    issue = ctx.issue(issue_key)
    if issue is None:
        return None
    kind, value = source
    created = issue["created_at"]
    if kind == "field":
        return _field_time(ctx, issue_key, value, created)
    if kind == "status":
        return _status_time(ctx, issue_key, value, created, issue["status"])
    if kind == "remote_link_contains":
        return created if ctx.has_remote_link(issue_key, value) else None
    return created if value in _labels(ctx.raw(issue_key)) else None


def has_evidence_before(ctx: RuleContext, issue_key: str, name: str, before: str) -> bool:
    """True when evidence_time(...) is set and <= `before` (storage-format strings compare as time)."""
    found = evidence_time(ctx, issue_key, name)
    return found is not None and found <= before


def _field_time(ctx: RuleContext, issue_key: str, logical: str, created: str | None) -> str | None:
    for change in ctx.field_changes(issue_key, logical):
        if _present(change["to_value"]):
            return change["changed_at"]
    field_id = ctx.fields.id_of(logical)
    if field_id and _present((ctx.raw(issue_key).get("fields") or {}).get(field_id)):
        return created
    return None


def _status_time(
    ctx: RuleContext, issue_key: str, status: str, created: str | None, current: str | None
) -> str | None:
    wanted = status.casefold()
    changes = ctx.status_changes(issue_key)
    initial = changes[0]["from_status"] if changes else current
    if (initial or "").casefold() == wanted:
        return created
    for change in changes:
        if (change["to_status"] or "").casefold() == wanted:
            return change["changed_at"]
    return None


def _labels(raw: Mapping[str, Any]) -> list[str]:
    labels = (raw.get("fields") or {}).get("labels")
    return [label for label in labels if isinstance(label, str)] if isinstance(labels, list) else []


def _present(value: object) -> bool:
    """A field value that means something was recorded: not None, blank, "[]" or empty."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in ("", "[]", "{}", "null")
    if isinstance(value, (list, dict, tuple)):
        return bool(value)
    return True
