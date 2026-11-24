from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from oversight.api.deps import ApiContext, get_ctx, read_conn
from oversight.api.meta import build_meta
from oversight.ask.catalog import open_findings_by_rule, rule_missing
from oversight.config import EstimateConfig, ProjectProcess
from oversight.rules.catalog import Rule, RuleDefaults
from oversight.rules.checks import join_words, label
from oversight.rules.registry import check_label, reads, threshold_words

router = APIRouter()

# How the work in a stage is described when statuses hold it ("... sits in To Do.").
_WHO_SITS = {
    "backlog": "Work waiting to start",
    "estimate": "Work being estimated",
    "development": "Work in development",
    "dev_signoff": "Work waiting for dev sign-off",
    "test_signoff": "Work waiting for test sign-off",
    "done": "Finished work",
}


@router.get("/rules")
def rules(ctx: ApiContext = Depends(get_ctx), conn: sqlite3.Connection = Depends(read_conn)) -> dict[str, Any]:
    project = ctx.project
    config = ctx.process.project(project)
    open_counts = open_findings_by_rule(conn, project)
    return {
        "meta": build_meta(conn, ctx),
        "rules": [_rule(rule, ctx, config.estimate, open_counts) for rule in ctx.rules.rules],
        "process": _process(ctx, config),
    }


def _rule(rule: Rule, ctx: ApiContext, estimate: EstimateConfig, open_counts: dict[str, int]) -> dict[str, Any]:
    missing = rule_missing(rule, ctx.process, ctx.project)
    return {
        "rule_id": rule.id,
        "title": rule.title,
        "description": rule.description,
        "kind": rule.kind,
        "severity": rule.severity,
        "version": ctx.rules.version,
        "threshold": _threshold(rule),
        "needs": _needs(rule, ctx.rules.defaults, estimate),
        "measurable": not missing,
        "missing_evidence": missing,
        "open": open_counts.get(rule.id, 0),
        "escalates_to": rule.escalates_to,
        "enabled": rule.enabled,
        "customised": rule.customised,
        "custom": rule.custom,
        "check_label": check_label(rule.check),
    }


def _threshold(rule: Rule) -> str | None:
    """The rule's threshold in words, with the check's own defaults"""
    return threshold_words(rule.check, rule.params)


def _needs(rule: Rule, defaults: RuleDefaults, estimate: EstimateConfig) -> list[str]:
    """What the rule reads from Jira, in plain words (registry.reads)"""
    return reads(rule.check, rule.params, history_days=defaults.history_days, estimate=estimate,
                 requires_evidence=rule.requires_evidence, requires_stages=rule.requires_stages)


def _process(ctx: ApiContext, config: ProjectProcess) -> dict[str, Any]:
    process = ctx.process
    estimate = config.estimate
    unrecorded = set(process.missing_evidence(ctx.project))
    stages = []
    for stage in process.stages:
        statuses = [status for status, mapped in config.status_to_stage.items() if mapped == stage]
        evidence, note = _stage_evidence(stage, statuses, config, estimate, unrecorded)
        stages.append(
            {
                "stage": stage,
                "label": _capital(label(stage)),
                "mapped_statuses": statuses,
                "evidence": evidence,
                "measurable": evidence != "none",
                "note": note,
            }
        )
    hours_per_point = estimate.hours_per_point
    return {
        "version": process.process_version,
        "project": ctx.project,
        "stages": stages,
        "estimate": {
            "basis": estimate.basis,
            "field": estimate.field,
            "hours_per_point": hours_per_point if hours_per_point and hours_per_point > 0 else None,
        },
    }


def _stage_evidence(
    stage: str, statuses: list[str], config: ProjectProcess, estimate: EstimateConfig, unrecorded: set[str]
) -> tuple[str, str]:
    """(status | field | none, one sentence for the reader). `unrecorded`: process.missing_evidence."""
    name = label(stage)
    if statuses:
        return "status", f"{_WHO_SITS.get(stage, f'Work in {name}')} sits in {join_words(statuses)}."
    source = config.evidence.get(stage)
    if source:
        # evidence.py's shapes: one key, the kind of source.
        kind, value = next(iter(source.items()))
        if kind == "status":
            return "status", f"Recorded when an issue passes through {value}."
        if kind == "field":
            return "field", f"Recorded in the {label(str(value))} field."
        if kind == "label":
            return "field", f"Recorded by the {value} label on the issue."
        if kind == "remote_link_contains":
            return "field", f"Recorded by a linked pull request (a remote link containing {value!s})."
        return "field", f"Recorded on the issue ({kind})."
    if stage == "estimate":
        if estimate.basis == "points":
            return "field", (
                f"Recorded as story points, at {_num(estimate.hours_per_point or 0)} hours per point. "
                "No original estimates (hours) are used."
            )
        return "field", "Recorded as original estimates, in hours. Story points are not used."
    if stage == "code_review":
        return "none", "No status, field or pull-request link records a review, so rules that need one cannot run."
    if stage in unrecorded:
        return "none", f"Nothing in Jira records {name}, so rules that need it cannot run."
    return "none", f"No status in the workflow maps to {name}."


def _num(value: object) -> str:
    """1 -> "1", 1.5 -> "1.5", 30.000000000000004 -> "30"."""
    return f"{float(value):g}"  # type: ignore[arg-type]


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]
