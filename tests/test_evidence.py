"""Where evidence lives (oversight/rules/evidence.py): each source kind, found and not found.

Every test configures code_review evidence on AIL with a five-stage workflow,
builds its issues, and asks evidence_time for the earliest moment the evidence
existed. Sources that carry no timestamp of their own answer created_at.
"""
from __future__ import annotations

from typing import Any

import pytest

from oversight.config import FieldsConfig, ProcessConfig
from oversight.errors import ConfigError
from oversight.rules.checks import RuleContext
from oversight.rules.evidence import evidence_source, evidence_time, has_evidence_before
from oversight.store.db import Store
from tests.rules_helpers import CODE_REVIEWERS_FIELD, FIVE_STAGES, add_issue, at, context, with_project

CREATED = -100.0
STARTED = [(-90.0, "To Do", "In Progress")]


def _ctx(store: Store, process: ProcessConfig, fields: FieldsConfig, source: Any) -> RuleContext:
    configured = with_project(process, status_to_stage=FIVE_STAGES, evidence={"code_review": source})
    return context(store, configured, fields)


def _time(ctx: RuleContext, key: str) -> str | None:
    return evidence_time(ctx, key, "code_review")


def test_field_source_takes_the_first_change_that_records_a_value(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", created=CREATED, changes=STARTED, field_changes=[
        (-80, "code_reviewers", None, "[]"),  # an empty value records nothing
        (-50, "code_reviewers", "[]", "acct-team-2"),
        (-40, "code_reviewers", "acct-team-2", "acct-team-3"),
        (-60, "programmer", None, "acct-team-4"),  # another field
    ])
    ctx = _ctx(store, process, fields, {"field": "code_reviewers"})

    assert _time(ctx, "AIL-1") == at(-50)


def test_field_source_set_at_creation_answers_created_at(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", created=CREATED, changes=STARTED,
              raw_fields={CODE_REVIEWERS_FIELD: [{"accountId": "acct-team-2"}]})
    add_issue(store, "AIL-2", status="In Progress", created=CREATED, changes=STARTED,
              raw_fields={CODE_REVIEWERS_FIELD: []})
    add_issue(store, "AIL-3", status="In Progress", created=CREATED, changes=STARTED)
    ctx = _ctx(store, process, fields, {"field": "code_reviewers"})

    assert _time(ctx, "AIL-1") == at(CREATED)
    assert _time(ctx, "AIL-2") is None
    assert _time(ctx, "AIL-3") is None


def test_status_source_is_the_first_entry_into_that_status(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="Done", created=CREATED, changes=[
        *STARTED, (-60, "In Progress", "Code Review"), (-50, "Code Review", "In Progress"),
        (-40, "In Progress", "Code Review"), (-30, "Code Review", "Done"),
    ])
    add_issue(store, "AIL-2", status="Code Review", created=CREATED)  # created in it, no history
    add_issue(store, "AIL-3", status="In Progress", created=CREATED,
              changes=[(-90, "Code Review", "In Progress")])  # created in it, then left
    add_issue(store, "AIL-4", status="Done", created=CREATED, changes=[*STARTED, (-30, "In Progress", "Done")])
    ctx = _ctx(store, process, fields, {"status": "code review"})  # matched ignoring case

    assert _time(ctx, "AIL-1") == at(-60)
    assert _time(ctx, "AIL-2") == at(CREATED)
    assert _time(ctx, "AIL-3") == at(CREATED)
    assert _time(ctx, "AIL-4") is None


def test_remote_link_source_needs_a_remote_link_holding_the_text(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", created=CREATED,
              remote_links=["https://git.example.invalid/team/repo/pull-requests/7"])
    add_issue(store, "AIL-2", created=CREATED, remote_links=["https://wiki.example.invalid/design"])
    add_issue(store, "AIL-3", created=CREATED)
    # An issue link is not a remote link, whatever its target says.
    store.upsert("links", {"issue_key": "AIL-3", "kind": "issue_link", "target": "pull-requests", "title": "blocks"})
    ctx = _ctx(store, process, fields, {"remote_link_contains": "pull-requests"})

    assert _time(ctx, "AIL-1") == at(CREATED)
    assert _time(ctx, "AIL-2") is None
    assert _time(ctx, "AIL-3") is None


def test_label_source_needs_that_exact_label(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    add_issue(store, "AIL-1", created=CREATED, labels=["backend", "code-reviewed"])
    add_issue(store, "AIL-2", created=CREATED, labels=["code-review"])
    add_issue(store, "AIL-3", created=CREATED)
    ctx = _ctx(store, process, fields, {"label": "code-reviewed"})

    assert _time(ctx, "AIL-1") == at(CREATED)
    assert _time(ctx, "AIL-2") is None
    assert _time(ctx, "AIL-3") is None


def test_null_evidence_is_not_recorded_anywhere(store: Store, process: ProcessConfig, fields: FieldsConfig) -> None:
    add_issue(store, "AIL-1", created=CREATED, labels=["code-reviewed"])
    ctx = context(store, process, fields)  # AIL as configured: every evidence null

    assert evidence_source(ctx, "code_review") is None
    assert _time(ctx, "AIL-1") is None
    assert _time(ctx, "AIL-404") is None  # and an unknown issue has none either


@pytest.mark.parametrize(
    "source",
    [
        {"commit_message": "reviewed"},  # an unknown kind
        {"field": "code_reviewers", "label": "code-reviewed"},  # two sources at once
        {"label": "   "},  # no text
        {"label": 7},  # not text
    ],
)
def test_an_unknown_source_raises_config_error(
    store: Store, process: ProcessConfig, fields: FieldsConfig, source: dict[str, Any]
) -> None:
    add_issue(store, "AIL-1", created=CREATED)
    ctx = _ctx(store, process, fields, source)

    with pytest.raises(ConfigError, match="code_review"):
        _time(ctx, "AIL-1")


def test_has_evidence_before_compares_with_the_evidence_time(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="Code Review", created=CREATED,
              changes=[*STARTED, (-50, "In Progress", "Code Review")])
    add_issue(store, "AIL-2", status="In Progress", created=CREATED, changes=STARTED)
    ctx = _ctx(store, process, fields, {"status": "Code Review"})

    assert has_evidence_before(ctx, "AIL-1", "code_review", at(-40))
    assert has_evidence_before(ctx, "AIL-1", "code_review", at(-50))  # at the moment counts
    assert not has_evidence_before(ctx, "AIL-1", "code_review", at(-60))
    assert not has_evidence_before(ctx, "AIL-2", "code_review", at(0))
