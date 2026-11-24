from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from oversight.api.app import create_app
from oversight.api.refresh import Refresher
from oversight.api.routes import portfolio as portfolio_module
from oversight.api.rule_draft import ERROR_NOTE, REFUSED_NOTE, RuleDraftReply
from oversight.api.rule_settings import BUDGET_REASON, LIMIT_REASON
from oversight.ask.analyst import SnapshotQuery
from oversight.config import ProcessConfig
from oversight.llm.audit import record_llm_call
from oversight.llm.provider import LLMError, LLMRefusal, Usage
from oversight.notify.digest import build_digest
from oversight.rules.registry import CHECK_SPECS
from oversight.rules.settings import board_rules
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import to_iso
from tests.conftest import CONFIG_FIXTURES
from tests.fakes.fake_llm import FakeProvider
from tests.test_api_overview import is_text
from tests.test_api_rule_settings import (  # noqa: F401  (board and seeded are fixtures)
    ADMIN,
    ADMIN_NAME,
    CATALOGUE,
    DEFINITION_KEYS,
    INVALID,
    PREVIEW,
    SAVED,
    SETTINGS,
    Clock,
    Steps,
    assert_meta,
    assert_preview_contract,
    assert_refusal_contract,
    assert_settings_contract,
    board,
    everything,
    headers,
    query,
    rules_of,
    save,
    seeded,
    serve,
    settle,
)

DRAFT = "/api/v1/rules/settings/draft?level=people"
DRAFT_KEYS = {"meta", "project", "outcome", "draft", "explanation", "note", "problems"}
BUGS = {
    "title": "Bugs in progress over 3 days",
    "check": "time_in_status_over",
    "params": {"statuses": ["In Progress"], "hours": 72},
    "applies_to": {"status_category": ["indeterminate"], "issue_type": ["Bug"]},
    "severity": "breach",
}
STORED = {**BUGS, "description": None, "params": {"statuses": ["In Progress"], "hours": 72.0}}
WORDS = "Bugs in progress for more than 3 days"
SCRIPTED_DRAFT = {
    "title": "Bugs In Progress over 3 days", "description": "Flags bugs In Progress for more than 3 days.",
    "check": "time_in_status_over", "params": {"statuses": ["In Progress"], "hours": 72.0},
    "applies_to": {"status_category": ["indeterminate"], "issue_type": ["Bug"]}, "severity": "breach",
}


def stuck(settings: Settings, hours: float = 72.0) -> list[str]:
    """DEMO's bugs in In Progress for more than `hours`, by number: what BUGS (at that limit) finds."""
    rows = query(settings, "SELECT i.issue_key FROM issues AS i JOIN issue_metrics AS m ON m.issue_key = i.issue_key"
                           " WHERE i.project_key = 'DEMO' AND i.status = 'In Progress' AND i.issue_type = 'Bug'"
                           " AND m.status_age_h > ?", (hours,))
    return sorted((key for (key,) in rows), key=lambda key: int(key.rsplit("-", 1)[1]))


def findings_of(client: TestClient, state: str = "open") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = client.get(
        f"/api/v1/findings?rule=R-CUS-01&state={state}&page_size=100").json()["items"]
    return items


def serve_with(settings: Settings, provider: Any, clock: Clock | None = None) -> tuple[TestClient, Refresher]:
    """As serve, with this provider."""
    clock = clock or Clock(SAVED)
    refresher = Refresher(runner=Steps(settings, clock), now=clock)
    app = create_app(settings, provider=provider, now=clock, refresher=refresher)
    return TestClient(app, headers=headers()), refresher


def draft_calls(settings: Settings) -> list[tuple[Any, ...]]:
    return query(settings, "SELECT purpose, provider, outcome FROM llm_calls WHERE purpose = 'rule_draft'"
                           " ORDER BY called_at, call_id")


def assert_draft_contract(body: dict[str, Any], project: str = "DEMO") -> None:
    assert set(body) == DRAFT_KEYS and body["project"] == project
    assert_meta(body["meta"])
    assert body["outcome"] in ("drafted", "not_supported", "not_understood", "unavailable")
    if body["outcome"] == "drafted":
        assert set(body["draft"]) == DEFINITION_KEYS and body["note"] is None
        assert body["explanation"] is None or is_text(body["explanation"])
    else:
        assert (body["draft"], body["explanation"], body["problems"]) == (None, None, []) and is_text(body["note"])
    for problem in body["problems"]:
        assert set(problem) == {"field", "message"} and is_text(problem["field"]) and is_text(problem["message"])


def test_the_settings_list_the_checks_with_the_boards_options(board: Settings) -> None:
    client, _ = serve(board)
    body = client.get(SETTINGS).json()
    assert_settings_contract(body)
    assert (body["custom_limit"], body["custom_count"], body["ai_draft"]) == (
        50, 0, {"available": True, "reason": None})
    assert body["issue_types"] == ["Bug", "Subtask", "Task", "Test"]  # from DEMO's issues, Epic left out
    checks = {check["name"]: check for check in body["checks"]}
    assert list(checks) == list(CHECK_SPECS)
    statuses, hours = checks["time_in_status_over"]["params"]
    assert statuses == {"name": "statuses", "label": "Statuses", "kind": "statuses", "unit": None, "min": None,
                        "max": None, "step": None, "default": None, "options": [
                            {"value": "To Do", "label": "To Do", "category": "new"},
                            {"value": "In Progress", "label": "In Progress", "category": "indeterminate"},
                            {"value": "Done", "label": "Done", "category": "done"}]}
    assert hours == {"name": "hours", "label": "Time in the status", "kind": "number", "unit": "hours", "min": 1.0,
                     "max": 8760.0, "step": 1.0, "default": 72.0, "options": None}
    assert (checks["time_in_status_over"]["label"], checks["time_in_status_over"]["categories"],
            checks["time_in_status_over"]["default_categories"]) == (
        "Time in a status", ["new", "indeterminate"], ["indeterminate"])
    assert [option["value"] for option in checks["missing_field"]["params"][0]["options"]] == [
        "assignee", "estimate", "epic", "sprint"]
    review = checks["entered_stage_without_evidence"]
    assert (review["measurable"], review["missing_evidence"]) == (False, [
        "code_review", "dev_signoff", "testing", "test_review", "test_signoff"])
    assert [option["value"] for option in review["params"][0]["options"]] == ["backlog", "development", "done"]
    assert not any(rule["custom"] for rule in body["rules"])


def test_a_preview_of_a_rule_to_add_says_what_it_would_raise_and_writes_nothing(board: Settings) -> None:
    client, _ = serve(board)
    before, expected = everything(board), stuck(board)
    assert len(expected) > 5  # enough for the examples to be a sample

    body = client.post(PREVIEW, json={"changes": [{"op": "create", "rule": BUGS}]}).json()

    assert_preview_contract(body)
    assert body["impact"] == [{
        "rule_id": "R-CUS-01", "measurable": True, "open_now": 0, "would_open": len(expected), "would_close": 0,
        "would_stay": 0, "examples": {"open": expected[:5], "close": []}, "note": None,
    }]
    assert body["totals"] == {"would_open": len(expected), "would_close": 0}
    assert everything(board) == before


def test_a_custom_rule_is_added_edited_deleted_and_restored_with_its_history(board: Settings) -> None:
    clock = Clock(SAVED)
    client, refresher = serve(board, clock=clock)
    expected = stuck(board)

    made = save(client, {"op": "create", "rule": BUGS}, reason="Bugs sit too long")
    assert_settings_contract(made)
    assert made["custom_count"] == 1 and [rule["rule_id"] for rule in made["rules"]][-1] == "R-CUS-01"
    assert rules_of(made)["R-CUS-01"] == {
        "rule_id": "R-CUS-01", "title": BUGS["title"], "description": "", "kind": "rule",
        "check": "time_in_status_over", "scope": "issue", "escalates_to": None, "enabled": True,
        "default_enabled": True, "severity": "breach", "default_severity": "breach",
        "params": [
            {"name": "statuses", "label": "Statuses", "kind": "statuses", "unit": None, "value": ["In Progress"],
             "default": ["In Progress"], "min": None, "max": None, "step": None, "options": [
                 {"value": "To Do", "label": "To Do", "category": "new"},
                 {"value": "In Progress", "label": "In Progress", "category": "indeterminate"},
                 {"value": "Done", "label": "Done", "category": "done"}]},
            {"name": "hours", "label": "Time in the status", "kind": "number", "unit": "hours", "value": 72.0,
             "default": 72.0, "min": 1.0, "max": 8760.0, "step": 1.0, "options": None},
        ],
        "customised": False, "measurable": True, "missing_evidence": [], "open": 0, "custom": True,
        "check_label": "Time in a status", "applies_to": BUGS["applies_to"], "created_at": to_iso(SAVED),
        "created_by": ADMIN_NAME,
    }
    assert made["history"][0]["changes"] == [{"rule_id": "R-CUS-01", "field": "rule", "from": None, "to": STORED}]
    assert made["history"][0]["reason"] == "Bugs sit too long"
    settle(refresher)

    found = findings_of(client)
    assert sorted((item["issue_key"] for item in found), key=lambda key: int(key.rsplit("-", 1)[1])) == expected
    assert {(item["rule_title"], item["severity"], item["threshold"]) for item in found} == {
        (BUGS["title"], "breach", "3 days in 'In Progress'")}
    first = {item["issue_key"]: (item["finding_id"], item["first_seen_at"]) for item in found}
    listed = client.get("/api/v1/rules").json()
    assert {(rule["rule_id"], rule["custom"]) for rule in listed["rules"]} >= {("R-CUS-01", True), ("R-EFF-01", False)}
    mine = rules_of(listed)["R-CUS-01"]
    assert (mine["check_label"], mine["threshold"], mine["needs"], mine["open"], mine["version"]) == (
        "Time in a status", "3 days in 'In Progress'", ["Status history"], len(expected), "0.1.0")

    clock.at += timedelta(minutes=1)
    edited = save(client, {"op": "edit", "rule_id": "R-CUS-01", "title": "Bugs stuck in progress",
                           "params": {"hours": 96}}, revision=1)
    assert edited["history"][0]["changes"] == [
        {"rule_id": "R-CUS-01", "field": "title", "from": BUGS["title"], "to": "Bugs stuck in progress"},
        {"rule_id": "R-CUS-01", "field": "params.hours", "from": 72.0, "to": 96.0},
    ]
    settle(refresher)
    kept = stuck(board, 96.0)
    assert {item["issue_key"] for item in findings_of(client)} == set(kept)
    narrowed = {item["issue_key"]: item["resolved_why"] for item in findings_of(client, "resolved")}
    assert narrowed == {key: "settings_changed" for key in expected if key not in kept}

    clock.at += timedelta(minutes=1)
    [deleting] = client.post(PREVIEW, json={"changes": [{"op": "delete", "rule_id": "R-CUS-01"}]}).json()["impact"]
    assert (deleting["rule_id"], deleting["open_now"], deleting["would_close"], deleting["would_open"]) == (
        "R-CUS-01", len(kept), len(kept), 0)
    deleted = save(client, {"op": "delete", "rule_id": "R-CUS-01"}, revision=2)
    assert deleted["custom_count"] == 0 and "R-CUS-01" not in rules_of(deleted)
    [change] = deleted["history"][0]["changes"]
    assert (change["field"], change["from"]["title"], change["to"]) == ("rule", "Bugs stuck in progress", None)
    settle(refresher)
    assert findings_of(client) == []
    closed = findings_of(client, "resolved")
    assert {item["resolved_why"] for item in closed} == {"settings_changed"}
    assert {item["rule_title"] for item in closed} == {"Bugs stuck in progress"}  # a deleted rule keeps its title
    facets = client.get("/api/v1/findings?state=all&page_size=1").json()["facets"]["rules"]
    assert {"value": "R-CUS-01", "label": "Bugs stuck in progress", "count": len(expected)} in facets
    assert "R-CUS-01" not in rules_of(client.get("/api/v1/rules").json())

    clock.at += timedelta(minutes=1)
    restored = save(client, {"op": "restore", "rule_id": "R-CUS-01"}, revision=3, reason="Undo of revision 3",
                    undo_of=deleted["history"][0]["change_id"])
    assert restored["history"][0]["undo_of"] == deleted["history"][0]["change_id"]
    settle(refresher)
    back = findings_of(client)
    assert {item["issue_key"]: (item["finding_id"], item["first_seen_at"]) for item in back} == {
        key: first[key] for key in kept}
    assert_settings_contract(client.get(SETTINGS).json())


def test_a_custom_rules_switch_and_severity_work_as_any_rules(board: Settings) -> None:
    client, refresher = serve(board)
    save(client, {"op": "create", "rule": BUGS})
    settle(refresher)
    before = {item["finding_id"] for item in findings_of(client)}
    warned = save(client, {"rule_id": "R-CUS-01", "severity": "warning"}, revision=1)
    mine = rules_of(warned)["R-CUS-01"]
    assert (mine["severity"], mine["default_severity"], mine["kind"], mine["customised"]) == (
        "warning", "breach", "rule", True)
    settle(refresher)
    after = findings_of(client)
    assert {item["severity"] for item in after} == {"warning"} and not before & {item["finding_id"] for item in after}
    assert {item["resolved_why"] for item in findings_of(client, "resolved")} == {"superseded"}
    off = save(client, {"rule_id": "R-CUS-01", "enabled": False}, revision=2)
    assert_settings_contract(off)
    settle(refresher)
    assert findings_of(client) == []


def test_a_custom_rule_change_made_from_another_revision_is_a_conflict(board: Settings) -> None:
    client, refresher = serve(board)
    save(client, {"op": "create", "rule": BUGS})
    settle(refresher)
    before = everything(board)
    response = client.post(SETTINGS, json={"revision": 0, "changes": [
        {"op": "create", "rule": {**BUGS, "title": "Another one"}}]})
    assert_refusal_contract(response, 409, "rule_settings_conflict")
    assert everything(board) == before


@pytest.mark.parametrize(("changes", "errors"), [
    ([{"op": "create", "rule": {**BUGS, "params": {"statuses": ["Code Review"], "hours": 72}}}],
     [(None, "changes.0.rule.params.statuses", "Not a status on this board: Code Review.")]),
    ([{"op": "create", "rule": {**BUGS, "params": {"statuses": ["In Progress"], "hours": 9000}}}],
     [(None, "changes.0.rule.params.hours", "Time in the status must be from 1 to 8760 hours.")]),
    ([{"op": "create", "rule": {**BUGS, "params": {"statuses": ["Done"], "hours": 24}}}],
     [(None, "changes.0.rule.params.statuses",
       "'Done' is a Done status: Time in a status checks To do and In progress work only.")]),
    ([{"op": "create", "rule": {**BUGS, "applies_to": {"status_category": ["done"], "issue_type": ["Story"]}}}],
     [(None, "changes.0.rule.applies_to.status_category", "Time in a status applies to To do and In progress only."),
      (None, "changes.0.rule.applies_to.issue_type", "Not an issue type on this board: Story.")]),
    ([{"op": "create", "rule": {**BUGS, "check": "time_in_review"}}],
     [(None, "changes.0.rule.check", "Pick one of the checks.")]),
    ([{"op": "create", "rule": {**BUGS, "title": "Reopened after done"}}],
     [(None, "changes.0.rule.title", "Another rule on this board already has this name.")]),
    ([{"op": "create", "rule": {**BUGS, "title": "Ask ada@example.com"}}],
     [(None, "changes.0.rule.title", "The name cannot hold an email address or an account id.")]),
    ([{"op": "create", "rule": {**BUGS, "title": "​​​​"}}],
     [(None, "changes.0.rule.title", "The name must be 3 to 80 characters.")]),
    ([{"op": "create"}], [(None, "rule", "Change 1 must hold the new rule as an object.")]),
    ([{"op": "create", "rule": BUGS, "rule_id": "R-CUS-05"}],
     [(None, "rule_id", "A new rule's change cannot set rule_id.")]),
    ([{"op": "frobnicate", "rule_id": "R-HYG-01"}],
     [(None, "op", "Change 1's op must be create, edit, delete or restore.")]),
    ([{"op": "edit", "rule_id": "R-HYG-01", "title": "Mine now"}],
     [("R-HYG-01", None, "R-HYG-01 is a catalogue rule: only a custom rule can be edited.")]),
    ([{"op": "edit", "rule_id": "R-CUS-01", "check": "reopened"}],
     [("R-CUS-01", "check", "The check can't change: delete the rule and add it again.")]),
    ([{"op": "delete", "rule_id": "R-CUS-07"}], [("R-CUS-07", None, "R-CUS-07 is not a rule on this board.")]),
    ([{"op": "restore", "rule_id": "R-CUS-07", "title": "x"}], [("R-CUS-07", "title", "A change cannot set title.")]),
    ([{"op": "delete", "rule_id": "R-CUS-1"}], [(None, "rule_id", "Change 1 must name its rule by id, such as R-EFF-01.")]),
])
def test_a_bad_custom_rule_change_is_refused_whole_on_its_fields(
    board: Settings, changes: list[dict[str, Any]], errors: list[tuple[str | None, str | None, str]]
) -> None:
    client, refresher = serve(board)
    before = everything(board)
    for path in (PREVIEW, SETTINGS):
        response = client.post(path, json={"revision": 0, "changes": changes})
        assert_refusal_contract(response, 422, INVALID)
        assert [(error["rule_id"], error["field"], error["message"]) for error in response.json()["errors"]] == errors
    settle(refresher)
    assert everything(board) == before


def test_a_custom_rule_has_nothing_to_reset_to(board: Settings) -> None:
    client, refresher = serve(board)
    save(client, {"op": "create", "rule": BUGS})
    response = client.post(SETTINGS, json={"revision": 1, "changes": [{"rule_id": "R-CUS-01", "reset": True}]})
    assert_refusal_contract(response, 422, INVALID)
    assert response.json()["errors"] == [
        {"rule_id": "R-CUS-01", "field": "reset", "message": "A custom rule has no catalogue settings to reset to."}]
    settle(refresher)


@pytest.mark.parametrize("named", [
    {"rule_id": "R-CUS-01", "enabled": False},
    {"op": "edit", "rule_id": "R-CUS-01", "title": "Renamed in the same body"},
    {"op": "delete", "rule_id": "R-CUS-01"},
    {"op": "restore", "rule_id": "R-CUS-01"},
], ids=["off", "edit", "delete", "restore"])
def test_a_body_never_names_the_id_its_own_new_rule_will_be_given(board: Settings, named: dict[str, Any]) -> None:
    client, refresher = serve(board)
    before = everything(board)
    for path in (PREVIEW, SETTINGS):
        response = client.post(path, json={"revision": 0, "changes": [{"op": "create", "rule": BUGS}, named]})
        assert_refusal_contract(response, 422, INVALID)
        assert response.json()["errors"] == [
            {"rule_id": "R-CUS-01", "field": None, "message": "R-CUS-01 is not a rule on this board."}]
    settle(refresher)
    assert everything(board) == before


def test_each_new_rules_problem_names_its_place_in_the_body(board: Settings) -> None:
    client, refresher = serve(board)
    bad = {**BUGS, "check": "open_longer_than", "params": {"days": 0}, "applies_to": {
        "status_category": ["new", "indeterminate"], "issue_type": None}}
    body = {"revision": 0, "changes": [
        {"rule_id": "R-EFF-01", "params": {"threshold": 1.6}},
        {"op": "create", "rule": {**bad, "title": "Open too long, first"}},
        {"op": "create", "rule": BUGS},
        {"op": "create", "rule": {**bad, "title": "Open too long, second"}},
    ]}
    for path in (PREVIEW, SETTINGS):
        response = client.post(path, json=body)
        assert_refusal_contract(response, 422, INVALID)
        message = "Days open must be from 1 to 3650 days."
        assert response.json()["errors"] == [
            {"rule_id": None, "field": "changes.1.rule.params.days", "message": message},
            {"rule_id": None, "field": "changes.3.rule.params.days", "message": message},
        ]
        assert response.json()["detail"] == message
    settle(refresher)


def test_one_boards_custom_rule_is_no_rule_of_another(board: Settings) -> None:
    client, refresher = serve(board)
    save(client, {"op": "create", "rule": BUGS})
    save(client, {"op": "delete", "rule_id": "R-CUS-01"}, revision=1)
    settle(refresher)
    for named in ({"op": "restore", "rule_id": "R-CUS-01"}, {"op": "edit", "rule_id": "R-CUS-01", "title": "Mine"},
                  {"op": "delete", "rule_id": "R-CUS-01"}, {"rule_id": "R-CUS-01", "enabled": False}):
        for path in ("/api/v1/rules/settings/preview?level=people&project=AIL",
                     "/api/v1/rules/settings?level=people&project=AIL"):
            response = client.post(path, json={"revision": 0, "changes": [named]})
            assert_refusal_contract(response, 422, INVALID)
            assert response.json()["errors"] == [
                {"rule_id": "R-CUS-01", "field": None, "message": "R-CUS-01 is not a rule on this board."}]
    other = client.get("/api/v1/rules/settings?level=people&project=AIL").json()
    assert (other["custom_count"], other["revision"]) == (0, 0) and "R-CUS-01" not in rules_of(other)
    assert "R-CUS-01" not in rules_of(client.get("/api/v1/rules?project=AIL").json())
    restored = save(client, {"op": "restore", "rule_id": "R-CUS-01"}, revision=2)  # still DEMO's own to bring back
    assert "R-CUS-01" in rules_of(restored)
    settle(refresher)


def test_a_board_at_its_limit_offers_no_draft_and_asks_no_model(board: Settings) -> None:
    client, refresher = serve(board)
    fifty = [{"op": "create", "rule": {**BUGS, "title": f"Bugs in progress, rule {n:02d}"}} for n in range(50)]
    full = save(client, *fifty)
    assert (full["custom_count"], full["ai_draft"]) == (50, {"available": False, "reason": LIMIT_REASON})
    fake = FakeProvider()
    limited, _ = serve_with(board, fake)
    body = limited.post(DRAFT, json={"text": WORDS}).json()
    assert_draft_contract(body)
    assert (body["outcome"], body["note"]) == ("unavailable", LIMIT_REASON)
    assert fake.calls == [] and draft_calls(board) == []
    settle(refresher)


def test_each_draft_is_logged_with_its_board_viewer_and_model_calls(board: Settings) -> None:
    client, _ = serve(board)
    with capture_logs() as events:
        client.post(DRAFT, json={"text": WORDS})
        client.post(DRAFT, json={"text": "Flag tickets whose summary is too short"})
    lines = [event for event in events if event["event"] == "rule_draft"]
    calls = [call_id for (call_id,) in query(board, "SELECT call_id FROM llm_calls WHERE purpose = 'rule_draft'"
                                                    " ORDER BY called_at, call_id")]
    assert [(line["project"], line["viewer"], line["outcome"], line["problems"]) for line in lines] == [
        ("DEMO", ADMIN, "drafted", 0), ("DEMO", ADMIN, "not_supported", 0)]
    assert sorted(call for line in lines for call in line["calls"]) == sorted(calls) and len(calls) == 2
    assert not any(WORDS in str(line) for line in lines)  # never the words


def test_a_custom_rules_title_reaches_every_place_that_names_a_rule(
    board: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, refresher = serve(board)
    save(client, {"op": "create", "rule": BUGS})
    settle(refresher)
    keys = stuck(board)

    [finding] = [item for item in findings_of(client) if item["issue_key"] == keys[0]]
    drawer = client.get(f"/api/v1/findings/{finding['finding_id']}").json()["finding"]
    assert (drawer["rule_title"], drawer["threshold"]) == (BUGS["title"], "3 days in 'In Progress'")
    assert [item["label"] for item in drawer["evidence"]][-2:] == ["Time in status", "Limit"]
    timeline = client.get(f"/api/v1/issues/{keys[0]}/timeline").json()["findings"]
    assert {item["rule_title"] for item in timeline if item["rule_id"] == "R-CUS-01"} == {BUGS["title"]}

    monkeypatch.setattr(portfolio_module, "ATTENTION", 5000)
    attention = client.get("/api/v1/portfolio?board=DEMO").json()["attention"]
    assert {row["title"] for row in attention if row["rule_id"] == "R-CUS-01"} == {BUGS["title"]}

    asked = client.post("/api/v1/ask", json={"question": "Which findings does R-CUS-01 have open?"}).json()
    [facts] = [fact for fact in asked["facts"] if fact["tool"] == "oversight_list_findings"]
    assert facts["params"]["rule_id"] == "R-CUS-01" and asked["outcome"] == "answered"
    assert {row["rule_title"] for row in facts["rows"]} == {BUGS["title"]}

    analyst = SnapshotQuery(board.db_file, project="DEMO", sprint_field=None, rules=board_rules(
        board.db_file, CATALOGUE, "DEMO"))
    try:
        assert analyst.run("SELECT title, kind, severity FROM rules WHERE rule_id = 'R-CUS-01'")[1] == [
            (BUGS["title"], "rule", "breach")]
    finally:
        analyst.close()

    process = ProcessConfig.load(CONFIG_FIXTURES / "process.yaml")
    with Store.open(board.db_file) as store:
        content = build_digest(store, process, CATALOGUE, project="DEMO", now=SAVED + timedelta(minutes=1))
    assert {item.rule_title for item in content.new_breaches if item.rule_id == "R-CUS-01"} == {BUGS["title"]}


def test_a_draft_fills_the_form_and_writes_nothing_but_its_audit_row(board: Settings) -> None:
    client, refresher = serve(board)
    before = everything(board)

    response = client.post(DRAFT, json={"text": f"  {WORDS}\n"})

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert_draft_contract(body)
    assert {key: body[key] for key in ("outcome", "draft", "explanation", "note", "problems")} == {
        "outcome": "drafted", "draft": SCRIPTED_DRAFT, "explanation": SCRIPTED_DRAFT["description"], "note": None,
        "problems": []}
    assert everything(board) == before  # no setting, no rule: the admin adds it
    assert draft_calls(board) == [("rule_draft", "scripted", "ok")]
    added = save(client, {"op": "create", "rule": body["draft"]})
    assert rules_of(added)["R-CUS-01"]["title"] == SCRIPTED_DRAFT["title"]
    settle(refresher)


def test_what_no_check_can_do_or_what_is_no_rule_is_said_with_a_note(board: Settings) -> None:
    client, _ = serve(board)
    for text, outcome in (("Flag tickets whose summary is too short", "not_supported"), ("?!?", "not_understood")):
        body = client.post(DRAFT, json={"text": text}).json()
        assert_draft_contract(body)
        assert body["outcome"] == outcome
    assert [outcome for _purpose, _provider, outcome in draft_calls(board)] == ["ok", "ok"]


def test_a_draft_waits_once_the_days_model_budget_is_used_up(board: Settings) -> None:
    budgeted = board.model_copy(update={"llm_daily_token_budget": 1000})
    with Store.open(board.db_file) as store:  # today's spend, past the budget
        record_llm_call(store, purpose="plan", provider="fake", model="fake-model", prompt_hash="h",
                        usage=Usage(900, 200), latency_ms=1, stop_reason="end_turn", outcome="ok",
                        now=lambda: SAVED - timedelta(hours=1))
    fake = FakeProvider()
    client, _ = serve_with(budgeted, fake)
    assert client.get(SETTINGS).json()["ai_draft"] == {"available": False, "reason": BUDGET_REASON}

    body = client.post(DRAFT, json={"text": WORDS}).json()

    assert_draft_contract(body)
    assert (body["outcome"], body["note"]) == ("unavailable", BUDGET_REASON)
    assert fake.calls == [] and draft_calls(board) == []  # no model asked
    scripted, _ = serve(budgeted)
    assert scripted.get(SETTINGS).json()["ai_draft"] == {"available": True, "reason": None}
    assert scripted.post(DRAFT, json={"text": WORDS}).json()["outcome"] == "drafted"


@pytest.mark.parametrize(("failure", "note", "outcome"), [
    (LLMRefusal("the model declined the request (cyber)"), REFUSED_NOTE, "refused"),
    (LLMError("Amazon Bedrock is unavailable (HTTP 503)", retryable=True), ERROR_NOTE, "error"),
])
def test_a_refusal_or_a_failed_call_leaves_the_form_to_fill_by_hand_never_a_500(
    board: Settings, failure: LLMError, note: str, outcome: str
) -> None:
    client, _ = serve_with(board, FakeProvider(plans=[failure]))
    response = client.post(DRAFT, json={"text": WORDS})
    assert response.status_code == 200
    body = response.json()
    assert_draft_contract(body)
    assert (body["outcome"], body["note"], body["draft"]) == ("unavailable", note, None)
    assert draft_calls(board) == [("rule_draft", "fake", outcome)]


def test_a_draft_that_fails_validation_is_repaired_once_then_shows_its_problems(board: Settings) -> None:
    review = RuleDraftReply(outcome="drafted", title="Bugs in review over 3 days", description=None,
                            check="time_in_status_over", params={"statuses": ["Code Review"], "hours": 72},
                            status_categories=["indeterminate"], issue_types=["Bug"], severity="breach",
                            explanation="Flags bugs in review for 3 days.", note=None)
    fixed = review.model_copy(update={"params": {"statuses": ["In Progress"], "hours": 72}})

    stubborn = FakeProvider(plans=[review, review])
    client, _ = serve_with(board, stubborn)
    body = client.post(DRAFT, json={"text": "Bugs in review for more than 3 days"}).json()
    assert_draft_contract(body)
    assert (body["outcome"], body["problems"]) == (
        "drafted", [{"field": "params.statuses", "message": "Not a status on this board: Code Review."}])
    assert body["draft"]["params"] == {"statuses": ["Code Review"], "hours": 72}  # as drafted, for the admin to fix
    assert len(stubborn.structured_calls) == 2 and "<problems>" in stubborn.structured_calls[1].user

    client, _ = serve_with(board, FakeProvider(plans=[review, fixed]))
    repaired = client.post(DRAFT, json={"text": "Bugs in review for more than 3 days"}).json()
    assert (repaired["outcome"], repaired["problems"]) == ("drafted", [])
    assert repaired["draft"]["params"] == {"statuses": ["In Progress"], "hours": 72.0}
    assert draft_calls(board) == [("rule_draft", "fake", "ok")] * 4


def test_the_model_sees_the_boards_names_and_the_words_never_its_tickets_or_people(board: Settings) -> None:
    reply = RuleDraftReply(outcome="drafted", title="Bugs over 3 days", description=None, check="time_in_status_over",
                           params={"statuses": ["In Progress"], "hours": 72}, status_categories=["indeterminate"],
                           issue_types=["Bug"], severity="breach", explanation="Flags bugs.", note=None)
    fake = FakeProvider(plans=[reply])
    client, _ = serve_with(board, fake)
    client.post(DRAFT, json={"text": WORDS})
    [call] = fake.structured_calls
    found = re.search(r"<board>(.*?)</board>", call.user)
    assert found is not None and json.loads(found.group(1)) == {
        "statuses": [{"name": "To Do", "category": "new"}, {"name": "In Progress", "category": "indeterminate"},
                     {"name": "Done", "category": "done"}],
        "issue_types": ["Bug", "Subtask", "Task", "Test"], "stages": ["backlog", "development", "done"],
        "evidence": ["code_review", "dev_signoff", "testing", "test_review", "test_signoff"],
        "estimate_basis": "points"}
    assert call.user.endswith(f"<request>{WORDS}</request>")
    summaries = [text for (text,) in query(board, "SELECT summary FROM issues WHERE length(summary) > 20")]
    names = [name for (name,) in query(board, "SELECT display_name FROM people WHERE display_name IS NOT NULL")]
    assert summaries and names and not any(text in call.user for text in summaries + names)
    assert client.get("/api/v1/usage?level=people").json()["today"]["model_calls"] == 1


def test_a_draft_is_for_admins_only_names_its_viewer_and_takes_a_few_plain_words(board: Settings) -> None:
    client, _ = serve(board)
    for level in ("", "?level=team"):
        response = client.post(f"/api/v1/rules/settings/draft{level}", json={"text": WORDS})
        assert (response.status_code, response.json()["code"]) == (403, "people_level_only")
    nameless = TestClient(client.app, headers=headers(viewer=None, name=None))
    response = nameless.post(DRAFT, json={"text": WORDS})
    assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["header", "X-Oversight-Viewer"]
    short = "Describe the rule in 3 to 500 characters."
    for body, errors in (
        ({"text": "ab"}, [(None, "text", short)]),
        ({"text": "x" * 501}, [(None, "text", short)]),
        ({"text": 7}, [(None, "text", short)]),
        ({}, [(None, "text", short)]),
        ({"text": WORDS, "level": "people"}, [(None, "level", "level is not something this request takes.")]),
        ({"text": "As ada@example.com asked: bugs in progress"},
         [(None, "text", "The description cannot hold an email address or an account id.")]),
    ):
        response = client.post(DRAFT, json=body)
        assert_refusal_contract(response, 422, INVALID)
        assert [(error["rule_id"], error["field"], error["message"]) for error in response.json()["errors"]] == errors
    padded = client.post(DRAFT, content=json.dumps({"text": "x" * 17_000}), headers={"Content-Type": "application/json"})
    assert (padded.status_code, padded.json()["detail"]) == (422, "The request body is larger than 16 KB.")
    assert draft_calls(board) == []


def test_drafts_count_against_the_viewers_question_limit(board: Settings) -> None:
    limited = board.model_copy(update={"ask_rate_limit": "1/10m"})
    client, _ = serve(limited)
    assert client.post(DRAFT, json={"text": "ab"}).status_code == 422  # a body refused is never counted
    assert client.post(DRAFT, json={"text": WORDS}).status_code == 200
    refused = client.post(DRAFT, json={"text": WORDS})
    assert_refusal_contract(refused, 429, "ask_rate_limited")
    assert refused.headers["retry-after"] == "600"
    assert len(draft_calls(board)) == 1
    other = TestClient(client.app, headers=headers(viewer="f" * 32, name=ADMIN_NAME))  # another viewer: their own limit
    assert other.post(DRAFT, json={"text": WORDS}).status_code == 200
