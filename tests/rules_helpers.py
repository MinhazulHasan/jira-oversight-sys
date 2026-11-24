from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from oversight.config import FieldsConfig, ProcessConfig
from oversight.derive.run import DeriveResult, run_derive
from oversight.rules.catalog import RulesConfig
from oversight.rules.checks import CHECKS, Hit, RuleContext
from oversight.rules.engine import RulesResult, run_rules
from oversight.store.db import Store
from oversight.timeutil import to_iso
from tests.conftest import CONFIG_FIXTURES

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
RULES_FILE = CONFIG_FIXTURES / "rules.yaml"
SPRINT_FIELD = "customfield_10020"
CODE_REVIEWERS_FIELD = "customfield_10139"
TEAM_ACCOUNT = "acct-team-1"

STATUS_CATEGORIES: dict[str, str] = {
    "To Do": "new",
    "In Progress": "indeterminate",
    "Code Review": "indeterminate",
    "Testing": "indeterminate",
    "Done": "done",
}
FIVE_STAGES: dict[str, str] = {
    "To Do": "backlog",
    "In Progress": "development",
    "Code Review": "code_review",
    "Testing": "testing",
    "Done": "done",
}
HOURS_BASIS: dict[str, str] = {"basis": "hours", "field": "original_estimate"}

Change = tuple[float, str, str]
FieldChange = tuple[float, str, Any, Any]


def at(hours: float, now: datetime = NOW) -> str:
    stamp = to_iso(now + timedelta(hours=hours))
    assert stamp is not None
    return stamp


def sprint(
    sprint_id: int, *, state: str = "active", start: float = -240.0, end: float = 48.0, name: str | None = None,
    now: datetime = NOW,
) -> dict[str, Any]:
    return {
        "id": sprint_id,
        "name": name or f"Sprint {sprint_id}",
        "state": state,
        "startDate": at(start, now),
        "endDate": at(end, now),
    }


def add_issue(
    store: Store,
    key: str,
    *,
    status: str = "To Do",
    created: float = -240.0,
    changes: Sequence[Change] = (),
    field_changes: Sequence[FieldChange] = (),
    issue_type: str = "Task",
    assignee: str | None = TEAM_ACCOUNT,
    points: float | None = None,
    original_h: float | None = None,
    spent_h: float | None = None,
    worklogs: Sequence[tuple[float, float]] = (),
    labels: Sequence[str] = (),
    sprints: Sequence[Mapping[str, Any]] = (),
    remote_links: Sequence[str] = (),
    raw_fields: Mapping[str, Any] | None = None,
    epic: str | None = None,
    parent: str | None = None,
    updated: float | None = None,
    now: datetime = NOW,
) -> None:
    project = key.split("-")[0]
    logged = spent_h if spent_h is not None else (sum(h for _, h in worklogs) if worklogs else None)
    category = STATUS_CATEGORIES.get(status)
    last_change = max([created, *(h for h, _, _ in changes)]) if updated is None else updated
    raw_fields_out: dict[str, Any] = {"labels": list(labels), **(raw_fields or {})}
    if sprints:
        raw_fields_out[SPRINT_FIELD] = [dict(s) for s in sprints]
    row = {
        "issue_key": key,
        "issue_id": f"id-{key}",
        "project_key": project,
        "issue_type": issue_type,
        "summary": f"{issue_type} {key}",
        "status": status,
        "status_category": category,
        "assignee_id": assignee,
        "parent_key": parent,
        "epic_key": epic,
        "created_at": at(created, now),
        "updated_at": at(last_change, now),
        "resolved_at": at(last_change, now) if category == "done" else None,
        "original_estimate_s": None if original_h is None else round(original_h * 3600),
        "time_spent_s": None if logged is None else round(logged * 3600),
        "story_points": points,
        "sprint_ids": [s["id"] for s in sprints],
        "raw_json": {"key": key, "fields": raw_fields_out},
    }
    with store.transaction():
        store.upsert_many("statuses", [
            {"project_key": project, "status_id": str(n), "name": name, "category": cat}
            for n, (name, cat) in enumerate(STATUS_CATEGORIES.items(), start=1)
        ])
        store.upsert("issues", row)
        store.upsert_many("status_changes", [
            {"issue_key": key, "changelog_id": str(n), "changed_at": at(h, now), "author_id": TEAM_ACCOUNT,
             "from_status": a, "to_status": b}
            for n, (h, a, b) in enumerate(changes, start=1)
        ])
        store.upsert_many("field_changes", [
            {"issue_key": key, "changelog_id": str(100 + n), "field": name, "changed_at": at(h, now),
             "from_value": before, "to_value": after}
            for n, (h, name, before, after) in enumerate(field_changes, start=1)
        ])
        store.upsert_many("worklogs", [
            {"worklog_id": f"{key}-w{n}", "issue_key": key, "started_at": at(h, now), "seconds": round(hours * 3600),
             "author_id": "acct-tempo", "updated_at": at(h, now)}
            for n, (h, hours) in enumerate(worklogs, start=1)
        ])
        store.upsert_many("links", [
            {"issue_key": key, "kind": "remote_link", "target": url, "title": "Pull request"} for url in remote_links
        ])


def add_history(
    store: Store, status: str, hours: Iterable[float], *, project: str = "AIL", left: float = -24.0,
    now: datetime = NOW,
) -> list[str]:
    taken = {r["issue_key"] for r in store.query("SELECT issue_key FROM issues WHERE project_key = ?", (project,))}
    free = (f"{project}-{n}" for n in itertools.count(900) if f"{project}-{n}" not in taken)
    keys = []
    for value, key in zip(hours, free):
        add_issue(store, key, status="Done", created=left - value, changes=[(left, status, "Done")], points=1, now=now)
        keys.append(key)
    return keys


def derive(
    store: Store, process: ProcessConfig, projects: Sequence[str] = ("AIL",), now: datetime = NOW
) -> DeriveResult:
    return run_derive(store, process, projects=list(projects), now=lambda: now)


def with_project(
    process: ProcessConfig, key: str = "AIL", *, status_to_stage: Mapping[str, str] | None = None,
    estimate: Mapping[str, Any] | None = None, evidence: Mapping[str, Any] | None = None,
) -> ProcessConfig:
    data = process.model_dump()
    base = data["projects"]["AIL"]
    data["projects"][key] = {
        "status_to_stage": dict(status_to_stage or base["status_to_stage"]),
        "estimate": dict(estimate or base["estimate"]),
        "evidence": dict(evidence or {}),
    }
    return ProcessConfig.model_validate(data)


def catalogue(*, version: str | None = None, enabled: Iterable[str] | None = None, **defaults: Any) -> RulesConfig:
    data = RulesConfig.load(RULES_FILE).model_dump()
    if version is not None:
        data["version"] = version
    if enabled is not None:
        wanted = set(enabled)
        for rule in data["rules"]:
            rule["enabled"] = rule["id"] in wanted
    data["defaults"].update(defaults)
    return RulesConfig.model_validate(data)


def context(
    store: Store, process: ProcessConfig, fields: FieldsConfig, rules: RulesConfig | None = None, *,
    project: str = "AIL", now: datetime = NOW, rule: str | None = None,
) -> RuleContext:
    ctx = RuleContext(
        store=store, process=process, fields=fields, rules=rules or catalogue(), project=project, now=now,
        now_iso=at(0, now),
    )
    ctx.current_rule = rule
    return ctx


def hits(ctx: RuleContext, check: str, key: str, **params: Any) -> list[Hit]:
    issue = ctx.issue(key)
    assert issue is not None, f"{key} is not in project {ctx.project}"
    result = CHECKS[check](issue, ctx, params)
    return [] if result is None else result if isinstance(result, list) else [result]


def run_at(
    store: Store, process: ProcessConfig, fields: FieldsConfig, rules: RulesConfig | None = None, *,
    projects: Sequence[str] = ("AIL",), now: datetime = NOW,
) -> list[RulesResult]:
    return run_rules(store, process, rules or catalogue(), fields, projects=list(projects), now=lambda: now)
