"""Rule settings for admins through the API (JO-15; contract 4.20 to 4.22, and 4.9's GET /rules): a board's settings
as GET /rules/settings reads them, the preview of a change, which writes nothing, and the save, all three at people
level only and with the viewer named; the re-check a save starts and the apply state it leaves, read from the runs
table too, so an API restart keeps it; the board's settings wherever a board-scoped route reads its rules; and every
body, in every state, against the dashboard's schemas (assert_settings_contract and the others mirror them).

The seeded DEMO board (tests/test_api_ask_insights._seed), a fresh copy for each test, served beside AIL (which has
synced and holds no data), and a fictional admin as the viewer. The re-check's step runs the rules in this process,
on the test's copy, as `rules --project KEY` would: no process is started and nothing talks to Jira.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from oversight.api import rule_settings as rule_settings_module
from oversight.api.app import create_app
from oversight.api.refresh import Refresher
from oversight.api.rule_settings import (
    CONFLICT_DETAIL,
    NOT_MEASURABLE_NOTE,
    PENDING_MESSAGE,
    PENDING_NOTE,
    apply_state,
    snapshot,
)
from oversight.config import FieldsConfig, ProcessConfig
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.rules.settings import RECENT_RUNS, Change, load_board_settings, read_history, save_board_settings
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES
from tests.test_api_ask_insights import _seed
from tests.test_api_overview import KEY, is_count, is_number, is_text, is_timestamp, make_settings

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)  # the seed's clock: its rules run
SAVED = NOW + timedelta(minutes=5)  # when the tests change the settings, preview them and re-check the board
ADMIN = "0123456789abcdef0123456789abcdef"  # a viewer hash, as the dashboard's server makes one
ADMIN_NAME = "Morgan Reyes"  # a fictional admin
SETTINGS = "/api/v1/rules/settings?level=people"
PREVIEW = "/api/v1/rules/settings/preview?level=people"
CATALOGUE = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")
BODY_KEYS = {
    "meta", "project", "revision", "catalogue_version", "updated_at", "updated_by", "apply", "rules", "history",
    # JO-20: the check registry for the board, custom rules' limit and count, the board's issue types and the AI draft.
    "checks", "custom_limit", "custom_count", "issue_types", "ai_draft",
}
INVALID = "rule_settings_invalid"
# How long a test waits for this process's jobs: they take a second or two, so this only bounds a very busy machine.
WAIT_S = 120


class Clock:
    """A clock a test can move."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def run_the_rules(settings: Settings, projects: Sequence[str], at: datetime) -> None:
    """The rules step on the settings' snapshot, as the CLI's `rules` runs it (the catalogue, each board's settings
    read by the engine)."""
    process, fields = ProcessConfig.load(settings.process_file), FieldsConfig.load(settings.fields_file)
    catalogue = RulesConfig.load(settings.rules_file)
    with Store.open(settings.db_file) as store:
        run_rules(store, process, catalogue, fields, projects=list(projects), now=lambda: at)


class Steps:
    """The steps "Sync now" and the re-check run, here in this process: a rules step runs the rules (on the board
    `--project` names, else every board) at the clock's time; any other step does nothing. Every call is recorded.
    hold: each step waits until released; fail: every step fails."""

    def __init__(self, settings: Settings, clock: Clock, *, hold: bool = False, fail: bool = False) -> None:
        self.settings = settings
        self.clock = clock
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []
        self.release = threading.Event()
        if not hold:
            self.release.set()

    def __call__(self, argv: Sequence[str]) -> int:
        self.release.wait(WAIT_S)
        self.calls.append(tuple(argv))
        if self.fail:
            return 1
        if argv[0] == "rules":
            projects = [argv[2]] if len(argv) > 2 else list(self.settings.jira_projects)
            run_the_rules(self.settings, projects, self.clock())
        return 0


def settle(refresher: Refresher) -> None:
    """Wait for the jobs this process's refresher runs (a sync, the re-checks) to end, and make sure they did."""
    refresher.wait(WAIT_S)
    for project in ("DEMO", "AIL"):
        assert refresher.status(project)["state"] not in ("queued", "running"), f"{project}'s sync did not end in time"
        job = refresher.recheck_of(project)
        assert job is None or job.state in ("succeeded", "failed"), f"{project}'s re-check did not end in time"


def headers(viewer: str | None = ADMIN, name: str | None = ADMIN_NAME) -> dict[str, str]:
    found = {"X-Service-Key": KEY}
    if viewer is not None:
        found["X-Oversight-Viewer"] = viewer
    if name is not None:
        found["X-Oversight-Viewer-Name"] = quote(name)
    return found


def serve(
    settings: Settings, *, clock: Clock | None = None, steps: Steps | None = None
) -> tuple[TestClient, Refresher]:
    """The API over the settings' snapshot, as the admin, with its refresher (a fresh one: as after a restart)."""
    clock = clock or Clock(SAVED)
    refresher = Refresher(runner=steps or Steps(settings, clock), now=clock)
    app = create_app(settings, provider=ScriptedProvider(), now=clock, refresher=refresher)
    return TestClient(app, headers=headers()), refresher


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    settings = make_settings(tmp_path_factory.mktemp("rule_settings_seed"))
    _seed(settings, briefing=False)
    with Store.open(settings.db_file) as store:
        # AIL has synced and holds nothing: a board-scoped route on a board that never synced is a 409 (JO-17).
        store.set_cursor("AIL", last_synced_at=to_iso(SAVED - timedelta(days=1)), run_id="r-ail",
                         account_timezone=None)
    return settings.db_file


@pytest.fixture
def board(seeded: Path, tmp_path: Path) -> Settings:
    """A copy of the seeded snapshot for this test alone (it may write to it), served as DEMO, then AIL."""
    settings = Settings(
        _env_file=None, jira_projects="DEMO,AIL", api_service_key=KEY, db_path=tmp_path / "oversight.db",
        config_dir=CONFIG_FIXTURES, reports_dir=tmp_path / "reports",
    )
    for suffix in ("", "-wal"):
        source = Path(f"{seeded}{suffix}")
        if source.exists():
            shutil.copyfile(source, Path(f"{settings.db_file}{suffix}"))
    return settings


def query(settings: Settings, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    conn = open_readonly(settings.db_file)
    try:
        return [tuple(row) for row in conn.execute(sql, tuple(params))]
    finally:
        conn.close()


def open_by_rule(settings: Settings) -> dict[str, int]:
    rows = query(settings, "SELECT rule_id, COUNT(*) FROM findings WHERE project_key = 'DEMO' AND resolved_at IS NULL"
                           " GROUP BY rule_id")
    return {rule_id: count for rule_id, count in rows}


def everything(settings: Settings) -> dict[str, list[tuple[Any, ...]]]:
    """Every row a change or a re-check could touch, to show that nothing was written."""
    return {table: query(settings, f"SELECT * FROM {table} ORDER BY 1, 2")
            for table in ("findings", "rule_status", "runs", "rule_settings", "rule_settings_changes", "warning_links",
                          "custom_rules")}


def under_effort(settings: Settings, threshold: float) -> list[str]:
    """The issues of R-EFF-01's open findings whose effort ratio is under `threshold`, by number."""
    rows = query(settings, "SELECT f.issue_key FROM findings AS f JOIN issue_metrics AS m ON m.issue_key = f.issue_key"
                           " WHERE f.rule_id = 'R-EFF-01' AND f.resolved_at IS NULL AND m.effort_ratio < ?",
                 (threshold,))
    return sorted((key for (key,) in rows), key=lambda key: int(key.rsplit("-", 1)[1]))


def save(client: TestClient, *changes: dict[str, Any], revision: int = 0, **extra: Any) -> dict[str, Any]:
    response = client.post(SETTINGS, json={"revision": revision, "changes": list(changes), **extra})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def rules_of(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["rule_id"]: rule for rule in body["rules"]}


# --- the dashboard's schemas (src/lib/api/schemas.ts), as assertions -------------------------------------------------
# ruleSettingsSchema and ruleSettingsPreviewSchema, their refinements included: the dashboard parses every body the
# service sends against them, so one that fails here would be its 502 upstream_invalid.

PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,19}$")
RULE_ID = re.compile(r"^[RW]-[A-Z]+-\d{2,3}$")  # JO-20: a custom rule's id too (R-CUS-01, W-CUS-104)
CHANGE_ID = re.compile(r"^[0-9a-f]{32}$")
VIEWER_HASH = re.compile(r"^(?:[0-9a-f]{32}|local)$")
PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SETTING_FIELD = re.compile(
    r"^(?:enabled|severity|params\.[a-z][a-z0-9_]{0,39}|rule|title|description|applies_to)$")
EXAMPLE_KEY = re.compile(r"^(?:[A-Z][A-Z0-9_]{1,19}-[1-9]\d{0,8}|sprint:\d{1,12})$")
META_KEYS = {"data_mode", "last_synced_at", "projects", "rules_version", "process_version", "generated_at",
             "report_timezone"}
RULE_KEYS = {"rule_id", "title", "description", "kind", "check", "scope", "escalates_to", "enabled", "default_enabled",
             "severity", "default_severity", "params", "customised", "measurable", "missing_evidence", "open",
             "custom", "check_label", "applies_to", "created_at", "created_by"}
PARAM_KEYS = {"name", "label", "kind", "unit", "value", "default", "min", "max", "step", "options"}
PARAM_KINDS = ("number", "statuses", "stage", "evidence", "field")
CATEGORIES = ("new", "indeterminate", "done")
DEFINITION_KEYS = {"title", "description", "check", "params", "applies_to", "severity"}
SEVERITIES = ("breach", "warning")
NAME_MAX = 128  # PERSON_NAME_MAX
SHOWN_MAX = 300  # the longest message of the service's the dashboard passes on
# A new rule's problem's field (JO-20): its create's place in the body, then the rule's field (live.ts CHANGE_PLACE).
NEW_RULE_PLACE = re.compile(r"^changes\.\d{1,3}\.rule(?:\.[a-z_]{1,40}(?:\.[a-z][a-z0-9_]{0,39})?)?$")


def assert_meta(meta: dict[str, Any]) -> None:
    """serviceMetaSchema: the meta of any board, one that has never synced too (last_synced_at null)."""
    assert set(meta) == META_KEYS and meta["data_mode"] in ("sample", "live")
    assert meta["last_synced_at"] is None or is_timestamp(meta["last_synced_at"])
    assert meta["projects"] and all(PROJECT_KEY.fullmatch(key) for key in meta["projects"])
    assert is_text(meta["rules_version"]) and is_text(meta["process_version"]) and is_text(meta["report_timezone"])
    assert is_timestamp(meta["generated_at"])


def assert_settings_contract(body: dict[str, Any], project: str = "DEMO") -> None:
    """ruleSettingsSchema: GET /rules/settings, and a save's answer."""
    assert set(body) == BODY_KEYS and body["project"] == project and PROJECT_KEY.fullmatch(project)
    assert_meta(body["meta"])
    assert is_count(body["revision"]) and is_text(body["catalogue_version"])
    assert body["updated_at"] is None or is_timestamp(body["updated_at"])
    assert body["updated_by"] is None or (is_text(body["updated_by"]) and len(body["updated_by"]) <= NAME_MAX)
    # When and by whom exactly when the settings were changed.
    assert (body["revision"] == 0) == (body["updated_at"] is None) == (body["updated_by"] is None)
    apply = body["apply"]
    assert set(apply) == {"state", "applied_revision", "started_at", "finished_at", "message", "result"}
    assert apply["state"] in ("applied", "pending", "running", "failed") and is_count(apply["applied_revision"])
    assert all(apply[key] is None or is_timestamp(apply[key]) for key in ("started_at", "finished_at"))
    assert apply["message"] is None or is_text(apply["message"])
    result = apply["result"]
    assert result is None or (set(result) == {"opened", "closed"} and all(is_count(n) for n in result.values()))
    # Applied only once the revision has been reached, pending only before.
    assert not (apply["state"] == "applied" and apply["applied_revision"] < body["revision"])
    assert not (apply["state"] == "pending" and apply["applied_revision"] >= body["revision"])
    ids = [rule["rule_id"] for rule in body["rules"]]
    assert len(ids) == len(set(ids))
    for rule in body["rules"]:
        assert set(rule) == RULE_KEYS and RULE_ID.fullmatch(rule["rule_id"])
        assert is_text(rule["title"]) and isinstance(rule["description"], str) and is_text(rule["check"])
        assert rule["kind"] in ("rule", "warning") and rule["scope"] in ("issue", "sprint")
        assert rule["escalates_to"] is None or RULE_ID.fullmatch(rule["escalates_to"])
        assert all(isinstance(rule[key], bool)
                   for key in ("enabled", "default_enabled", "customised", "measurable", "custom"))
        assert rule["severity"] in SEVERITIES and rule["default_severity"] in SEVERITIES
        assert all(isinstance(name, str) for name in rule["missing_evidence"]) and is_count(rule["open"])
        assert is_text(rule["check_label"]) and set(rule["applies_to"]) == {"status_category", "issue_type"}
        categories, types = rule["applies_to"]["status_category"], rule["applies_to"]["issue_type"]
        assert all(category in CATEGORIES for category in categories) and (rule["scope"] == "sprint") == (not categories)
        assert types is None or (types and all(is_text(name) for name in types))
        # When and by whom exactly for a custom rule.
        assert (rule["created_at"] is not None) == (rule["created_by"] is not None) == rule["custom"]
        assert rule["created_at"] is None or is_timestamp(rule["created_at"])
        assert rule["created_by"] is None or (is_text(rule["created_by"]) and len(rule["created_by"]) <= NAME_MAX)
        names = [param["name"] for param in rule["params"]]
        assert len(names) == len(set(names))
        for param in rule["params"]:
            assert set(param) == PARAM_KEYS and param["kind"] in PARAM_KINDS
            assert PARAM_NAME.fullmatch(param["name"]) and is_text(param["label"])
            if param["kind"] == "number":
                assert param["unit"] in ("ratio", "hours", "percentile", "days", "share") and param["options"] is None
                assert all(is_number(param[key]) for key in ("value", "default", "min", "max", "step"))
                assert param["step"] > 0 and param["min"] <= param["max"]
            else:  # a custom rule's statuses, stage, evidence or field: one of the board's options
                assert rule["custom"] and all(param[key] is None for key in ("unit", "min", "max", "step"))
                values = [option["value"] for option in param["options"]]
                chosen = param["value"] if param["kind"] == "statuses" else [param["value"]]
                assert chosen and set(chosen) <= set(values) and param["default"] == param["value"]
        # Customised says exactly whether anything of it differs from the catalogue's (a custom rule's: as created).
        differs = (rule["enabled"] != rule["default_enabled"] or rule["severity"] != rule["default_severity"]
                   or any(param["value"] != param["default"] for param in rule["params"]))
        assert rule["customised"] == differs, rule["rule_id"]
    assert_checks(body["checks"])
    assert body["custom_limit"] == 50 and is_count(body["custom_count"])
    assert body["custom_count"] == sum(rule["custom"] for rule in body["rules"]) <= body["custom_limit"]
    assert all(is_text(name) and name.casefold() != "epic" for name in body["issue_types"])
    assert set(body["ai_draft"]) == {"available", "reason"} and isinstance(body["ai_draft"]["available"], bool)
    assert (body["ai_draft"]["reason"] is None) == body["ai_draft"]["available"]
    history = body["history"]
    assert len(history) <= 50 and (body["revision"] > 0 or history == [])
    assert not history or history[0]["revision"] == body["revision"]  # headed by the board's revision
    assert all(later["revision"] < earlier["revision"] for earlier, later in zip(history, history[1:]))
    for entry in history:
        assert set(entry) == {"change_id", "at", "revision", "by", "reason", "undo_of", "changes"}
        assert CHANGE_ID.fullmatch(entry["change_id"]) and is_timestamp(entry["at"]) and entry["revision"] >= 1
        assert set(entry["by"]) == {"hash", "name"} and VIEWER_HASH.fullmatch(entry["by"]["hash"])
        assert is_text(entry["by"]["name"]) and len(entry["by"]["name"]) <= NAME_MAX
        assert entry["reason"] is None or (is_text(entry["reason"]) and len(entry["reason"]) <= 500)
        assert entry["undo_of"] is None or CHANGE_ID.fullmatch(entry["undo_of"])
        assert entry["changes"]
        for change in entry["changes"]:
            assert set(change) == {"rule_id", "field", "from", "to"} and RULE_ID.fullmatch(change["rule_id"])
            assert SETTING_FIELD.fullmatch(change["field"]) and change["from"] != change["to"]
            if change["field"] == "rule":  # a custom rule added or restored (from null), or deleted (to null)
                assert (change["from"] is None) != (change["to"] is None)
                definition = change["from"] or change["to"]
                assert set(definition) == DEFINITION_KEYS and is_text(definition["title"])
                continue
            for value in (change["from"], change["to"]):  # each of its field's kind
                if change["field"] == "enabled":
                    assert isinstance(value, bool)
                elif change["field"] == "severity":
                    assert value in SEVERITIES
                elif change["field"] in ("title", "description"):
                    assert value is None or is_text(value)
                elif change["field"] == "applies_to":
                    assert set(value) == {"status_category", "issue_type"}
                else:  # a param: a number, a custom rule's statuses (names), stage, evidence or field
                    assert is_number(value) or is_text(value) or (
                        isinstance(value, list) and value and all(is_text(name) for name in value))


def assert_checks(checks: list[dict[str, Any]]) -> None:
    """The check registry as the dashboard parses it: each check once, its params with their kinds and the board's
    options, its categories within the three and its default ones among them."""
    names = [check["name"] for check in checks]
    assert names and len(names) == len(set(names))
    for check in checks:
        assert set(check) == {"name", "label", "summary", "scope", "params", "categories", "default_categories",
                              "measurable", "missing_evidence"}
        assert is_text(check["label"]) and is_text(check["summary"]) and len(check["summary"]) <= 90
        assert check["scope"] in ("issue", "sprint") and isinstance(check["measurable"], bool)
        assert check["measurable"] == (not check["missing_evidence"])
        assert set(check["default_categories"]) <= set(check["categories"]) <= set(CATEGORIES)
        assert (check["scope"] == "sprint") == (not check["categories"])
        for param in check["params"]:
            assert set(param) == {"name", "label", "kind", "unit", "min", "max", "step", "default", "options"}
            assert PARAM_NAME.fullmatch(param["name"]) and is_text(param["label"]) and param["kind"] in PARAM_KINDS
            if param["kind"] == "number":
                assert param["options"] is None and param["min"] <= param["default"] <= param["max"]
            else:
                assert all(set(option) >= {"value", "label"} and is_text(option["value"]) for option in param["options"])


def assert_preview_contract(body: dict[str, Any], project: str = "DEMO") -> None:
    """ruleSettingsPreviewSchema: POST /rules/settings/preview."""
    assert set(body) == {"meta", "project", "revision", "impact", "totals"} and body["project"] == project
    assert_meta(body["meta"])
    assert is_count(body["revision"])
    ids = [impact["rule_id"] for impact in body["impact"]]
    assert len(ids) == len(set(ids))
    for impact in body["impact"]:
        assert set(impact) == {"rule_id", "measurable", "open_now", "would_open", "would_close", "would_stay",
                               "examples", "note"}
        assert RULE_ID.fullmatch(impact["rule_id"]) and isinstance(impact["measurable"], bool)
        assert all(is_count(impact[key]) for key in ("open_now", "would_open", "would_close", "would_stay"))
        assert impact["would_close"] + impact["would_stay"] == impact["open_now"]
        assert set(impact["examples"]) == {"open", "close"}
        for keys in impact["examples"].values():
            assert len(keys) <= 5 and all(EXAMPLE_KEY.fullmatch(key) for key in keys)
        assert impact["note"] is None or is_text(impact["note"])
    assert body["totals"] == {"would_open": sum(impact["would_open"] for impact in body["impact"]),
                              "would_close": sum(impact["would_close"] for impact in body["impact"])}


def assert_refusal_contract(response: Any, status: int, code: str) -> None:
    """A refusal: {detail, code}, and for a 422 each problem {rule_id, field, message}, its message plain enough for the
    dashboard to pass on (one line, at most 300 characters). A field is at most 40 characters, or (a new rule's, JO-20:
    it has no id yet) its place in the body, "changes.<index>.rule" and the rule's field (live.ts maps it)."""
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["code"] == code and is_text(body["detail"])
    if code != INVALID:
        assert set(body) == {"detail", "code"}
        return
    assert set(body) == {"detail", "code", "errors"} and body["errors"]
    for error in body["errors"]:
        assert set(error) == {"rule_id", "field", "message"}
        assert error["rule_id"] is None or RULE_ID.fullmatch(error["rule_id"])
        field = error["field"]
        assert field is None or (isinstance(field, str) and (1 <= len(field) <= 40 or (
            error["rule_id"] is None and NEW_RULE_PLACE.fullmatch(field) is not None)))
        assert is_text(error["message"]) and "\n" not in error["message"] and len(error["message"]) <= SHOWN_MAX


# --- reading a board's settings ---------------------------------------------------------------------------------


def test_a_board_nobody_changed_lists_every_rule_as_rules_yaml_has_it(board: Settings) -> None:
    client, _ = serve(board)
    response = client.get(SETTINGS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == BODY_KEYS
    assert (body["project"], body["revision"], body["catalogue_version"], body["updated_at"], body["updated_by"]) == (
        "DEMO", 0, "0.1.0", None, None)
    assert body["history"] == [] and body["meta"]["rules_version"] == "0.1.0" and body["meta"]["projects"] == ["DEMO"]
    # The seed's rules run applied revision 0: the board had no settings, and still has none.
    assert body["apply"] == {"state": "applied", "applied_revision": 0, "started_at": None, "finished_at": None,
                             "message": None, "result": None}
    assert [rule["rule_id"] for rule in body["rules"]] == [rule.id for rule in CATALOGUE.rules]  # off or on, in order
    found, open_now = rules_of(body), open_by_rule(board)
    effort = CATALOGUE.by_id("R-EFF-01")
    assert found["R-EFF-01"] == {
        "rule_id": "R-EFF-01", "title": effort.title, "description": effort.description, "kind": "rule",
        "check": "effort_ratio_at_least", "scope": "issue", "escalates_to": None,
        "enabled": True, "default_enabled": True, "severity": "breach", "default_severity": "breach",
        "params": [{"name": "threshold", "label": "Effort ratio", "kind": "number", "unit": "ratio", "value": 1.5,
                    "default": 1.5, "min": 0.1, "max": 10.0, "step": 0.05, "options": None}],
        "customised": False, "measurable": True, "missing_evidence": [], "open": open_now["R-EFF-01"],
        # JO-20: every rule says whether an admin added it, its check's label and what it applies to.
        "custom": False, "check_label": "Effort over estimate",
        "applies_to": {"status_category": ["done"], "issue_type": None}, "created_at": None, "created_by": None,
    }
    assert found["R-EST-02"]["params"] == [{"name": "grace_hours", "label": "Grace after work started",
                                            "kind": "number", "unit": "hours", "value": 1.0, "default": 1.0,
                                            "min": 0.0, "max": 168.0, "step": 0.5, "options": None}]
    assert found["R-FLOW-02"]["applies_to"] == {"status_category": ["new", "indeterminate", "done"], "issue_type": None}
    assert found["W-SPR-01"]["applies_to"] == {"status_category": [], "issue_type": None}
    assert (found["R-EST-02"]["kind"], found["R-EST-02"]["severity"]) == ("rule", "warning")
    assert [(p["name"], p["unit"], p["value"], p["min"], p["max"], p["step"]) for p in found["W-SPR-01"]["params"]] == [
        ("days_left", "days", 2.0, 0.0, 14.0, 0.5), ("not_started_share", "share", 0.3, 0.05, 0.95, 0.05)]
    assert (found["W-SPR-01"]["scope"], found["W-EFF-01"]["escalates_to"]) == ("sprint", "R-EFF-01")
    assert [p["unit"] for p in found["W-AGE-01"]["params"]] == ["percentile"]
    assert found["R-FLOW-02"]["params"] == []  # nothing numeric to set
    flow = found["R-FLOW-01"]  # not measurable on DEMO, and still listed with what it misses
    assert (flow["measurable"], flow["missing_evidence"], flow["open"], flow["params"]) == (
        False, ["code_review", "testing"], 0, [])
    assert not any(rule["customised"] for rule in body["rules"])


def test_rule_settings_are_for_admins_only_and_name_their_viewer(board: Settings) -> None:
    client, _ = serve(board)
    change = {"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False}]}
    refused = {"detail": "rule settings are for people level only", "code": "people_level_only"}
    for level in ("", "?level=team"):
        assert client.get(f"/api/v1/rules/settings{level}").json() == refused
        assert client.post(f"/api/v1/rules/settings/preview{level}", json=change).status_code == 403
        response = client.post(f"/api/v1/rules/settings{level}", json=change)
        assert (response.status_code, response.json()) == (403, refused)

    nameless = TestClient(client.app, headers=headers(viewer=None, name=None))
    for method, path in (("get", SETTINGS), ("post", PREVIEW), ("post", SETTINGS)):
        response = nameless.request(method, path, json=change)
        assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["header", "X-Oversight-Viewer"]
    forged = TestClient(client.app, headers=headers(viewer="not-a-hash"))
    assert forged.post(SETTINGS, json=change).status_code == 422

    unknown = client.get(f"{SETTINGS}&project=NOPE")
    assert (unknown.status_code, unknown.json()["code"]) == (404, "unknown_board")
    assert client.get(f"{SETTINGS}&days=7").status_code == 422  # a parameter it does not take
    assert client.get(f"{SETTINGS}&level=people").status_code == 422  # one given twice
    assert client.post(f"{SETTINGS}&reason=x", json=change).status_code == 422
    assert query(board, "SELECT COUNT(*) FROM rule_settings_changes") == [(0,)]  # nothing was written


def test_a_body_is_read_in_one_snapshot_so_a_save_landing_part_way_never_shows_half_of_it(board: Settings) -> None:
    conn = open_readonly(board.db_file)
    try:
        with snapshot(conn):
            assert load_board_settings(conn, "DEMO").revision == 0  # the snapshot is taken here
            with Store.open(board.db_file) as store:  # another request saves meanwhile
                save_board_settings(store, CATALOGUE, "DEMO", revision=0, changes=[Change("R-HYG-01", enabled=False)],
                                    now=SAVED, viewer_hash=ADMIN, viewer_name=ADMIN_NAME)
            assert (load_board_settings(conn, "DEMO").revision, read_history(conn, "DEMO")) == (0, [])
        assert not conn.in_transaction
        assert [entry["revision"] for entry in read_history(conn, "DEMO")] == [1]  # the next read sees it whole
    finally:
        conn.close()


# --- the preview --------------------------------------------------------------------------------------------------


def test_a_preview_says_what_a_change_would_do_and_writes_nothing(board: Settings) -> None:
    client, _ = serve(board)
    before, open_now = everything(board), open_by_rule(board)
    closing = under_effort(board, 2.0)
    assert 5 < len(closing) < open_now["R-EFF-01"]  # some of R-EFF-01's findings are under 2, some are not

    response = client.post(PREVIEW, json={"changes": [
        {"rule_id": "R-HYG-01", "enabled": False},
        {"rule_id": "R-EFF-01", "params": {"threshold": 2.0}},
        {"rule_id": "R-FLOW-03", "severity": "breach"},
        {"rule_id": "R-FLOW-01", "severity": "warning"},
    ]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"meta", "project", "revision", "impact", "totals"}
    assert (body["project"], body["revision"], body["meta"]["rules_version"]) == ("DEMO", 0, "0.1.0")
    assert [item["rule_id"] for item in body["impact"]] == ["R-EFF-01", "R-FLOW-01", "R-FLOW-03", "R-HYG-01"]
    effort, flow, reopened, hygiene = body["impact"]
    # A threshold raised: the findings under it close (settings_changed), the rest stay, the same ids.
    assert effort == {
        "rule_id": "R-EFF-01", "measurable": True, "open_now": open_now["R-EFF-01"], "would_open": 0,
        "would_close": len(closing), "would_stay": open_now["R-EFF-01"] - len(closing),
        "examples": {"open": [], "close": closing[:5]}, "note": None,
    }
    # Not measurable on DEMO: nothing to change until it can run.
    assert flow == {"rule_id": "R-FLOW-01", "measurable": False, "open_now": 0, "would_open": 0, "would_close": 0,
                    "would_stay": 0, "examples": {"open": [], "close": []}, "note": NOT_MEASURABLE_NOTE}
    # A severity change: every open finding closes (superseded) and each current hit opens under a new id.
    assert (reopened["open_now"], reopened["would_open"], reopened["would_close"], reopened["would_stay"]) == (
        open_now["R-FLOW-03"], open_now["R-FLOW-03"], open_now["R-FLOW-03"], 0)
    assert reopened["examples"]["open"] == reopened["examples"]["close"] and len(reopened["examples"]["open"]) == 5
    # Turned off: every open finding closes.
    assert (hygiene["open_now"], hygiene["would_open"], hygiene["would_close"], hygiene["would_stay"]) == (
        open_now["R-HYG-01"], 0, open_now["R-HYG-01"], 0)
    assert body["totals"] == {
        "would_open": open_now["R-FLOW-03"],
        "would_close": len(closing) + open_now["R-FLOW-03"] + open_now["R-HYG-01"],
    }
    assert everything(board) == before  # nothing written, not even a run


def test_a_preview_reads_the_change_as_a_save_would_and_refuses_it_the_same_way(board: Settings) -> None:
    clock = Clock(SAVED)
    steps = Steps(board, clock, hold=True)
    client, refresher = serve(board, clock=clock, steps=steps)
    save(client, {"rule_id": "R-EFF-01", "params": {"threshold": 2.0}})
    # Against the board's settings now: the threshold is 2 already, so this changes nothing.
    response = client.post(PREVIEW, json={"changes": [{"rule_id": "R-EFF-01", "params": {"threshold": 2}}]})
    assert (response.status_code, response.json()["code"]) == (422, INVALID)
    assert response.json()["errors"] == [
        {"rule_id": "R-EFF-01", "field": None, "message": "R-EFF-01 already has these settings on this board."}]
    # Back to rules.yaml's, before the re-check has run: every open finding is still a hit, under its own id.
    reset = client.post(PREVIEW, json={"changes": [{"rule_id": "R-EFF-01", "reset": True}], "revision": 1})
    assert reset.status_code == 200 and reset.json()["revision"] == 1
    [effort] = reset.json()["impact"]
    assert (effort["rule_id"], effort["would_open"], effort["would_close"], effort["would_stay"]) == (
        "R-EFF-01", 0, 0, open_by_rule(board)["R-EFF-01"])
    # Once the re-check has closed those under 2, going back would reopen exactly them, under their own ids.
    steps.release.set()
    settle(refresher)
    [back] = client.post(PREVIEW, json={"changes": [{"rule_id": "R-EFF-01", "reset": True}]}).json()["impact"]
    assert (back["would_open"], back["would_close"]) == (len(query(
        board, "SELECT 1 FROM findings WHERE rule_id = 'R-EFF-01' AND resolved_why = 'settings_changed'")), 0)
    assert back["examples"]["open"] and back["examples"]["close"] == []
    # A body's shape is checked first; its changes once it is one.
    assert client.post(PREVIEW, json={"changes": [], "reason": "x"}).json()["errors"] == [
        {"rule_id": None, "field": "reason", "message": "reason is not something this request takes."}]
    assert client.post(PREVIEW, json={"changes": []}).json()["errors"] == [
        {"rule_id": None, "field": "changes", "message": "Name at least one change."}]


def test_a_preview_after_a_failed_recheck_says_what_the_next_recheck_does(board: Settings) -> None:
    """A save whose re-check failed (or never ran: the API restarted) leaves the findings on the earlier settings, and
    the re-check after the next save applies every saved setting: the preview of an unrelated change says so, the
    earlier change's rule with its note, and the chip's result then matches it."""
    clock = Clock(SAVED)
    client, refresher = serve(board, clock=clock, steps=Steps(board, clock, fail=True))
    save(client, {"rule_id": "R-EFF-01", "params": {"threshold": 2}})
    settle(refresher)
    assert client.get(SETTINGS).json()["apply"]["state"] == "failed"
    closing, open_now = under_effort(board, 2.0), open_by_rule(board)

    previewed = client.post(PREVIEW, json={"changes": [{"rule_id": "R-HYG-01", "enabled": False}]}).json()

    assert_preview_contract(previewed)
    effort, hygiene = previewed["impact"]
    assert (effort["rule_id"], effort["would_close"], effort["would_open"], effort["note"]) == (
        "R-EFF-01", len(closing), 0, PENDING_NOTE)
    assert (hygiene["rule_id"], hygiene["would_close"], hygiene["note"]) == ("R-HYG-01", open_now["R-HYG-01"], None)
    working, refresher = serve(board, clock=clock)  # a re-check that works, after a restart
    save(working, {"rule_id": "R-HYG-01", "enabled": False}, revision=1)
    settle(refresher)
    result = working.get(SETTINGS).json()["apply"]["result"]
    assert result == {"opened": previewed["totals"]["would_open"], "closed": previewed["totals"]["would_close"]}
    assert result["closed"] == len(closing) + open_now["R-HYG-01"]


# --- saving -----------------------------------------------------------------------------------------------------


def test_a_save_records_the_change_rechecks_the_board_as_previewed_and_answers_with_its_settings(
    board: Settings,
) -> None:
    clock = Clock(SAVED)
    steps = Steps(board, clock, hold=True)
    client, refresher = serve(board, clock=clock, steps=steps)
    changes = [{"rule_id": "R-EFF-01", "params": {"threshold": 2}}, {"rule_id": "W-AGE-01", "enabled": False}]
    previewed = client.post(PREVIEW, json={"changes": changes}).json()["totals"]
    closing, open_before = under_effort(board, 2.0), open_by_rule(board)
    assert previewed == {"would_open": 0, "would_close": len(closing) + open_before["W-AGE-01"]}

    with capture_logs() as events:
        response = client.post(SETTINGS, json={"revision": 0, "changes": changes, "reason": " Agreed  with\nthe team "})

    assert response.status_code == 200, response.text
    body, at = response.json(), to_iso(SAVED)
    assert set(body) == BODY_KEYS
    assert (body["revision"], body["updated_at"], body["updated_by"]) == (1, at, ADMIN_NAME)
    # The re-check is queued or running in this process, so the board's findings are not yet the new settings'.
    assert body["apply"] == {"state": "running", "applied_revision": 0, "started_at": at, "finished_at": None,
                             "message": None, "result": None}
    [change] = body["history"]
    assert change == {
        "change_id": change["change_id"], "at": at, "revision": 1, "by": {"hash": ADMIN, "name": ADMIN_NAME},
        "reason": "Agreed with the team", "undo_of": None,
        "changes": [{"rule_id": "R-EFF-01", "field": "params.threshold", "from": 1.5, "to": 2.0},
                    {"rule_id": "W-AGE-01", "field": "enabled", "from": True, "to": False}],
    }
    found = rules_of(body)
    assert (found["R-EFF-01"]["params"][0]["value"], found["R-EFF-01"]["params"][0]["default"]) == (2.0, 1.5)
    assert (found["W-AGE-01"]["enabled"], found["W-AGE-01"]["default_enabled"]) == (False, True)
    assert [rule_id for rule_id, rule in found.items() if rule["customised"]] == ["R-EFF-01", "W-AGE-01"]
    assert query(board, "SELECT rule_id, enabled, severity, params_json FROM rule_settings ORDER BY rule_id") == [
        ("R-EFF-01", None, None, '{"threshold":2.0}'), ("W-AGE-01", 0, None, None)]
    # One line in the app log: the board, the revision, the rules and the fields; no name, no reason, no value.
    assert [event for event in events if event["event"] == "rule_settings_saved"] == [{
        "event": "rule_settings_saved", "project": "DEMO", "revision": 1, "rules": ["R-EFF-01", "W-AGE-01"],
        "fields": ["enabled", "params.threshold"], "logger": "api", "log_level": "info",
    }]
    logged = json.dumps(events)
    assert ADMIN_NAME not in logged and "Agreed" not in logged and ADMIN not in logged

    steps.release.set()
    settle(refresher)
    assert steps.calls == [("rules", "--project", "DEMO")]  # the one board's rules, and no sync
    after = client.get(SETTINGS).json()
    # The re-check did what the preview said.
    assert after["apply"] == {"state": "applied", "applied_revision": 1, "started_at": at, "finished_at": at,
                              "message": None, "result": {"opened": previewed["would_open"],
                                                          "closed": previewed["would_close"]}}
    now_open = open_by_rule(board)
    assert "W-AGE-01" not in now_open and now_open["R-EFF-01"] == open_before["R-EFF-01"] - len(closing)
    assert rules_of(after)["R-EFF-01"]["open"] == now_open["R-EFF-01"]
    whys = query(board, "SELECT DISTINCT resolved_why FROM findings WHERE rule_id IN ('R-EFF-01', 'W-AGE-01')"
                        " AND resolved_at IS NOT NULL")
    assert whys == [("settings_changed",)]  # closed because the settings changed, never "cleared"


def test_a_save_made_from_another_revision_is_a_conflict_and_changes_nothing(board: Settings) -> None:
    client, refresher = serve(board)
    save(client, {"rule_id": "R-HYG-01", "enabled": False})
    settle(refresher)
    before = everything(board)

    stale = {"revision": 0, "changes": [{"rule_id": "R-EFF-01", "params": {"threshold": 3}}]}
    response = client.post(SETTINGS, json=stale)

    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_DETAIL, "code": "rule_settings_conflict"}
    assert CONFLICT_DETAIL.startswith("Someone else changed this board's rule settings")
    assert everything(board) == before
    assert client.get(SETTINGS).json()["revision"] == 1


@pytest.mark.parametrize(("body", "errors"), [
    ("not json", [(None, None, "The request body must be a JSON object.")]),
    ([], [(None, None, "The request body must be a JSON object.")]),
    ({"changes": [{"rule_id": "R-HYG-01", "enabled": False}]},
     [(None, "revision", "The revision the changes were made from must be a whole number.")]),
    ({"revision": True, "changes": [{"rule_id": "R-HYG-01", "enabled": False}]},
     [(None, "revision", "The revision the changes were made from must be a whole number.")]),
    ({"revision": 0, "changes": []}, [(None, "changes", "Name at least one change.")]),
    ({"revision": 0, "changes": {"rule_id": "R-HYG-01"}},
     [(None, "changes", "changes must be a list of changes, one for each rule.")]),
    ({"revision": 0, "changes": [7]}, [(None, "changes", "Change 1 must be an object.")]),
    ({"revision": 0, "changes": [{"rule_id": "hygiene"}]},
     [(None, "rule_id", "Change 1 must name its rule by id, such as R-EFF-01.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-NOPE-01", "enabled": False}]},
     [("R-NOPE-01", None, "R-NOPE-01 is not a rule in the catalogue.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False, "title": "x"}]},
     [("R-HYG-01", "title", "A change cannot set title.")]),
    ({"revision": 0, "changes": [{"rule_id": "W-REV-01", "params": {"stage": 1}}]},
     [("W-REV-01", "params.stage", "W-REV-01 has no setting called stage that can be changed.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-EFF-01", "params": {"threshold": 11}}]},
     [("R-EFF-01", "params.threshold", "Effort ratio must be from 0.1 to 10.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-EFF-01", "params": {"threshold": "2"}}]},
     [("R-EFF-01", "params.threshold", "Effort ratio must be a number.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-EFF-01", "params": {"threshold": True}}]},
     [("R-EFF-01", "params.threshold", "Effort ratio must be a number.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-EFF-01", "params": {"threshold": 2.00001}}]},
     [("R-EFF-01", "params.threshold", "Effort ratio can have at most 4 decimal places.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-EFF-01", "params": [2]}]},
     [("R-EFF-01", "params", "Its thresholds must be an object of numbers, by name.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": "no"}]},
     [("R-HYG-01", "enabled", "On or off must be true or false.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "severity": "critical"}]},
     [("R-HYG-01", "severity", "Severity must be breach or warning.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-EFF-01", "reset": True, "enabled": False}]},
     [("R-EFF-01", "reset", "A reset cannot be combined with other changes to the same rule.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False},
                                 {"rule_id": "R-HYG-01", "severity": "warning"}]},
     [("R-HYG-01", None, "R-HYG-01 is named more than once: make one change per rule.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": True}]},
     [("R-HYG-01", None, "R-HYG-01 already has these settings on this board.")]),
    ({"revision": 0, "changes": [{"rule_id": "W-EFF-01", "params": {"threshold": 1.5}}]},
     [("W-EFF-01", "params.threshold", "Must stay below R-EFF-01's 1.5× while both are on: the warning comes first.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-EFF-01", "params": {"threshold": 0.75}}]},
     [("R-EFF-01", "params.threshold", "Must stay above W-EFF-01's 0.8× while both are on.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False}] * 101},
     [(None, "changes", "A request can change at most 100 rules.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False}], "reason": "x" * 501},
     [(None, "reason", "The reason can be at most 500 characters.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False}], "reason": "As jane@example.com asked"},
     [(None, "reason", "The reason cannot hold an email address or an account id.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False}], "undo_of": "the last one"},
     [(None, "undo_of", "The change to undo must be named by its change_id.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False}], "undo_of": "f" * 32},
     [(None, "undo_of", "The change to undo is not one of this board's.")]),
    ({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False}], "level": "people"},
     [(None, "level", "level is not something this request takes.")]),
])
def test_a_bad_body_or_change_set_is_refused_whole_naming_each_problem(
    board: Settings, body: Any, errors: list[tuple[str | None, str | None, str]]
) -> None:
    client, refresher = serve(board)
    before = everything(board)
    content = body if isinstance(body, str) else json.dumps(body)

    response = client.post(SETTINGS, content=content, headers={"Content-Type": "application/json"})

    assert response.status_code == 422, response.text
    refused = response.json()
    assert refused["code"] == INVALID
    assert [(error["rule_id"], error["field"], error["message"]) for error in refused["errors"]] == errors
    # The detail says each problem in plain words, naming the rule where the message does not.
    for rule_id, _field, message in errors:
        assert message in refused["detail"]
        if rule_id is not None:
            assert rule_id in refused["detail"] and refused["detail"].count(f"{rule_id}: {rule_id}") == 0
    settle(refresher)
    assert everything(board) == before  # nothing written, and no re-check started


def test_a_body_larger_than_16_kb_is_refused_before_it_is_read(board: Settings) -> None:
    client, _ = serve(board)
    padded = json.dumps({"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False}], "reason": " " * 17_000})
    for path in (SETTINGS, PREVIEW):
        response = client.post(path, content=padded, headers={"Content-Type": "application/json"})
        assert (response.status_code, response.json()["code"]) == (422, INVALID)
        assert response.json()["detail"] == "The request body is larger than 16 KB."
    assert query(board, "SELECT COUNT(*) FROM rule_settings_changes") == [(0,)]


def test_a_save_while_a_rules_run_holds_the_write_lock_is_busy_and_writes_nothing(
    board: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rule_settings_module, "WRITE_WAIT_MS", 50)
    client, _ = serve(board)
    # The access log's daily purge rides on the first request (and would wait for the lock too): it is done now.
    assert client.get(SETTINGS).status_code == 200
    holder = sqlite3.connect(board.db_file, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")  # as a rules run holds it for a board
        response = client.post(SETTINGS, json={"revision": 0, "changes": [{"rule_id": "R-HYG-01", "enabled": False}]})
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert response.status_code == 503 and response.json()["code"] == "rule_settings_busy"
    assert "Nothing was changed" in response.json()["detail"]
    assert query(board, "SELECT COUNT(*) FROM rule_settings_changes") == [(0,)]


def test_an_undo_puts_the_settings_back_and_names_the_change_it_undid(board: Settings) -> None:
    client, refresher = serve(board)
    first = save(client, {"rule_id": "R-EFF-01", "params": {"threshold": 2.5}},
                 {"rule_id": "R-HYG-01", "severity": "warning"})
    settle(refresher)
    [done] = first["history"]

    undone = save(client, {"rule_id": "R-EFF-01", "params": {"threshold": 1.5}},
                  {"rule_id": "R-HYG-01", "severity": "breach"},
                  revision=1, reason="Undo of revision 1", undo_of=done["change_id"])
    settle(refresher)

    assert undone["revision"] == 2 and not any(rule["customised"] for rule in undone["rules"])
    assert [(entry["revision"], entry["undo_of"], entry["reason"]) for entry in undone["history"]] == [
        (2, done["change_id"], "Undo of revision 1"), (1, None, None)]
    assert undone["history"][0]["changes"] == [
        {"rule_id": "R-EFF-01", "field": "params.threshold", "from": 2.5, "to": 1.5},
        {"rule_id": "R-HYG-01", "field": "severity", "from": "warning", "to": "breach"}]
    assert query(board, "SELECT COUNT(*) FROM rule_settings") == [(0,)]  # nothing differs from rules.yaml
    assert client.get(SETTINGS).json()["apply"]["state"] == "applied"


def test_an_undo_must_undo_the_boards_latest_change_exactly(board: Settings) -> None:
    """The history says a change was undone only when it no longer applies: an undo naming another board's change, or
    putting back something else than the change it names, is refused and nothing is written."""
    client, refresher = serve(board)
    elsewhere = client.post(f"{SETTINGS}&project=AIL", json={"revision": 0, "changes": [
        {"rule_id": "R-HYG-01", "enabled": False}]})
    assert elsewhere.status_code == 200, elsewhere.text
    [theirs] = elsewhere.json()["history"]  # a real change, of another board
    [done] = save(client, {"rule_id": "R-HYG-01", "enabled": False})["history"]
    settle(refresher)
    before = everything(board)
    for undo_of, change, message in (
        (theirs["change_id"], {"rule_id": "R-HYG-01", "enabled": True}, "The change to undo is not one of this board's."),
        (done["change_id"], {"rule_id": "W-AGE-01", "params": {"pct": 80}},
         "This change does not undo that one: an undo sets each field it changed back to what it was, and nothing else."),
    ):
        response = client.post(SETTINGS, json={"revision": 1, "changes": [change], "reason": "Undo of revision 1",
                                               "undo_of": undo_of})
        assert_refusal_contract(response, 422, INVALID)
        assert response.json()["errors"] == [{"rule_id": None, "field": "undo_of", "message": message}]
    assert everything(board) == before  # revision 1 still applies, and nothing says it was undone

    undone = save(client, {"rule_id": "R-HYG-01", "enabled": True}, revision=1, reason="Undo of revision 1",
                  undo_of=done["change_id"])
    assert [(entry["revision"], entry["undo_of"]) for entry in undone["history"]] == [(2, done["change_id"]), (1, None)]
    settle(refresher)


# --- the apply state ------------------------------------------------------------------------------------------


def test_the_apply_state_follows_the_recheck_and_survives_a_restart(board: Settings) -> None:
    clock = Clock(SAVED)
    client, refresher = serve(board, clock=clock)
    unassigned = open_by_rule(board)["R-HYG-01"]
    save(client, {"rule_id": "R-HYG-01", "enabled": False})
    settle(refresher)
    applied = client.get(SETTINGS).json()["apply"]
    assert (applied["state"], applied["applied_revision"], applied["result"]) == (
        "applied", 1, {"opened": 0, "closed": unassigned})

    # After a restart the state comes from the runs table: the same answer.
    restarted, _ = serve(board, clock=clock)
    assert restarted.get(SETTINGS).json()["apply"] == applied

    # A re-check that fails says so, and that the next sync applies the settings.
    clock.at = SAVED + timedelta(minutes=10)
    failing = Steps(board, clock, fail=True)
    client, refresher = serve(board, clock=clock, steps=failing)
    save(client, {"rule_id": "R-EFF-01", "params": {"threshold": 2}}, revision=1)
    settle(refresher)
    failed = client.get(SETTINGS).json()["apply"]
    at = to_iso(clock())
    assert failed == {
        "state": "failed", "applied_revision": 1, "started_at": at, "finished_at": at,
        # The board by its name, as the page names it, never its key (18 Nov 2026).
        "message": "Couldn't re-check Demo project's findings with the new settings: the rules run failed. "
                   "They will apply at the next sync.",
        "result": None,
    }
    # Another API process (or this one, restarted) knows nothing of that re-check: the settings wait for a run.
    restarted, _ = serve(board, clock=clock)
    assert restarted.get(SETTINGS).json()["apply"] == {
        "state": "pending", "applied_revision": 1, "started_at": None, "finished_at": None,
        "message": PENDING_MESSAGE, "result": None}

    # The next sync's rules step (the scheduled cycle, another process) applies them.
    clock.at = SAVED + timedelta(minutes=20)
    run_the_rules(board, ["DEMO", "AIL"], clock())
    for reader in (client, restarted):
        state = reader.get(SETTINGS).json()["apply"]
        assert (state["state"], state["applied_revision"], state["started_at"]) == ("applied", 2, to_iso(clock()))


def record_run(
    settings: Settings, started: datetime, revision: int, *, minutes: int = 1, project: str = "DEMO",
    status: Literal["succeeded", "failed"] = "succeeded",
) -> None:
    """A rules run as the engine records it: the revision it applied to the board, and what it did there."""
    with Store.open(settings.db_file) as store:
        run = store.start_run("rules", rules_version="0.1.0", started_at=started)
        store.finish_run(run, status=status,
                         stats={project: {"settings_revision": revision, "opened": revision, "resolved": 1}},
                         finished_at=started + timedelta(minutes=minutes))


def applied_state(settings: Settings, revision: int, saved_at: datetime = SAVED) -> dict[str, Any]:
    """The board's apply state, read as GET /rules/settings reads it, for its settings at `revision` saved at
    `saved_at`, with no re-check in this process."""
    conn = open_readonly(settings.db_file)
    try:
        return apply_state(conn, "DEMO", revision, None, saved_at=to_iso(saved_at))
    finally:
        conn.close()


def test_the_applied_revision_is_the_highest_the_boards_latest_runs_applied(board: Settings) -> None:
    """A run reads the board's settings inside its write transaction for the board, and revisions only grow, so the
    findings show the highest revision a run applied, whichever run started last: a cycle that started first but
    reached the board after a re-check did, or two runs started at the same moment."""
    record_run(board, SAVED + timedelta(minutes=1), 3, minutes=30)  # the cycle: it reached DEMO after the save of 3
    record_run(board, SAVED + timedelta(minutes=2), 2)  # a re-check of revision 2, started later, done sooner
    found = applied_state(board, 3)
    assert (found["state"], found["applied_revision"]) == ("applied", 3)
    # The first run that applied revision 3 says when, and what it did.
    assert (found["started_at"], found["result"]) == (to_iso(SAVED + timedelta(minutes=1)), {"opened": 3, "closed": 1})

    for revision in (5, 4):  # two runs started at the same moment
        record_run(board, SAVED + timedelta(minutes=40), revision)
    record_run(board, SAVED + timedelta(minutes=50), 9, status="failed")  # a failed run applied nothing
    record_run(board, SAVED + timedelta(minutes=50), 8, project="AIL")  # another board's
    assert (applied_state(board, 5)["state"], applied_state(board, 5)["applied_revision"]) == ("applied", 5)
    waiting = applied_state(board, 6)
    assert (waiting["state"], waiting["applied_revision"], waiting["message"]) == ("pending", 5, PENDING_MESSAGE)


def vm_steps(settings: Settings, read: Callable[[sqlite3.Connection], object]) -> int:
    """How much work SQLite does for `read` on a read-only connection, in hundreds of its virtual machine's steps."""
    conn = open_readonly(settings.db_file)
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0  # carry on

    try:
        conn.set_progress_handler(tick, 100)
        read(conn)
    finally:
        conn.close()
    return steps


def test_the_apply_state_reads_the_boards_latest_runs_alone_however_long_its_history(board: Settings) -> None:
    """GET /rules/settings, and every save's answer, read the board's latest RECENT_RUNS succeeded rules runs and no
    more, so a year of a 15-minute cycle costs what a day does. The run that first applied the revision is said while
    it is among them; once every run read applied it, what the first one did is no longer known (null), never another
    run's figures in its place."""
    def read(conn: sqlite3.Connection) -> None:
        apply_state(conn, "DEMO", 1, None, saved_at=to_iso(SAVED))

    record_run(board, SAVED + timedelta(minutes=1), 1)  # the re-check after the save of revision 1
    for minutes in range(2, RECENT_RUNS):  # the next cycles: the seed's run is the oldest read, the re-check the next
        record_run(board, SAVED + timedelta(minutes=minutes), 1)
    found = applied_state(board, 1)
    assert (found["state"], found["started_at"], found["result"]) == (
        "applied", to_iso(SAVED + timedelta(minutes=1)), {"opened": 1, "closed": 1})
    record_run(board, SAVED + timedelta(minutes=RECENT_RUNS), 1)  # one more: every run read applied revision 1
    unknown = applied_state(board, 1)
    assert (unknown["state"], unknown["applied_revision"], unknown["started_at"], unknown["finished_at"],
            unknown["result"]) == ("applied", 1, None, None, None)
    a_day = vm_steps(board, read)

    # A year of runs more before them: the answer, and the work done for it, stay the same.
    first = NOW - timedelta(minutes=15)
    with Store.open(board.db_file) as store, store.transaction():
        store.conn.executemany(
            "INSERT INTO runs (run_id, kind, status, started_at, finished_at, rules_version, stats_json)"
            " VALUES (?, 'rules', 'succeeded', ?, ?, '0.1.0', ?)",
            [(uuid.uuid4().hex, to_iso(first - timedelta(minutes=15 * n)), to_iso(first - timedelta(minutes=15 * n - 1)),
              json.dumps({"DEMO": {"settings_revision": 0, "opened": 0}})) for n in range(35_000)],
        )
    assert applied_state(board, 1) == unknown
    assert vm_steps(board, read) <= a_day + 1  # bounded by RECENT_RUNS, never by the history


def test_a_recheck_asked_for_during_a_sync_runs_right_after_it(board: Settings) -> None:
    clock = Clock(SAVED)
    steps = Steps(board, clock, hold=True)
    client, refresher = serve(board, clock=clock, steps=steps)
    assert client.post("/api/v1/refresh").json()["state"] == "running"  # "Sync now", held at its first step

    body = save(client, {"rule_id": "R-HYG-01", "enabled": False})

    assert body["apply"]["state"] == "running"
    sync = client.get("/api/v1/refresh").json()
    assert set(sync) == {"project", "state", "started_at", "finished_at", "message"} and sync["state"] == "running"
    steps.release.set()
    settle(refresher)
    # Never two jobs at once: the board's sync (its steps, JO-18, and its baseline when it has none), then the board's
    # re-check.
    assert steps.calls == [("sync", "--incremental", "--project", "DEMO"), ("derive", "--project", "DEMO"),
                           ("rules", "--project", "DEMO"), ("baseline", "--missing", "--project", "DEMO"),
                           ("rules", "--project", "DEMO")]
    assert client.get("/api/v1/refresh").json()["state"] == "succeeded"
    assert client.get(SETTINGS).json()["apply"]["state"] == "applied"


# --- the board's settings everywhere --------------------------------------------------------------------------


def test_get_rules_lists_every_rule_on_or_off_as_the_board_has_it(board: Settings) -> None:
    client, refresher = serve(board)
    save(client, {"rule_id": "R-HYG-01", "enabled": False}, {"rule_id": "R-EFF-01", "params": {"threshold": 2}},
         {"rule_id": "R-FLOW-03", "severity": "breach"}, {"rule_id": "W-SPR-01", "params": {"days_left": 1}})
    settle(refresher)

    body = client.get("/api/v1/rules").json()  # team level too: it holds no person

    assert [rule["rule_id"] for rule in body["rules"]] == [rule.id for rule in CATALOGUE.rules]
    found = rules_of(body)
    hygiene = found["R-HYG-01"]
    assert (hygiene["enabled"], hygiene["customised"], hygiene["open"]) == (False, True, 0)
    assert (found["R-EFF-01"]["threshold"], found["R-EFF-01"]["customised"]) == ("2×", True)
    assert (found["R-FLOW-03"]["severity"], found["R-FLOW-03"]["customised"]) == ("breach", True)
    assert found["W-SPR-01"]["threshold"] == "≤ 1 day left, > 30% not started"
    changed = ("R-HYG-01", "R-EFF-01", "R-FLOW-03", "W-SPR-01")
    unchanged = [rule for rule_id, rule in found.items() if rule_id not in changed]
    assert all(rule["enabled"] and not rule["customised"] for rule in unchanged)
    assert {rule["version"] for rule in body["rules"]} == {"0.1.0"} and body["meta"]["rules_version"] == "0.1.0"
    # Another board keeps rules.yaml's settings.
    other = rules_of(client.get("/api/v1/rules?project=AIL").json())
    assert all(rule["enabled"] and not rule["customised"] for rule in other.values())
    assert other["R-EFF-01"]["threshold"] == "1.5×"
    assert client.get(f"{SETTINGS}&project=AIL").json()["revision"] == 0


def test_the_boards_settings_reach_every_route_that_reads_its_rules(board: Settings) -> None:
    client, refresher = serve(board)
    closing = under_effort(board, 2.0)
    open_now = query(board, "SELECT issue_key FROM findings WHERE rule_id = 'R-EFF-01' AND resolved_at IS NULL")
    kept = [key for (key,) in open_now if key not in closing]
    save(client, {"rule_id": "R-EFF-01", "params": {"threshold": 2}}, {"rule_id": "W-AGE-01", "enabled": False})
    settle(refresher)

    # Findings: a finding the new threshold closed says why; one still over it carries the board's threshold.
    resolved = client.get("/api/v1/findings?state=resolved&rule=R-EFF-01&page_size=100").json()["items"]
    assert {finding["resolved_why"] for finding in resolved} == {"settings_changed"}
    assert {finding["issue_key"] for finding in resolved} == set(closing)
    still = client.get("/api/v1/findings?state=open&rule=R-EFF-01&page_size=100").json()["items"]
    assert {finding["issue_key"] for finding in still} == set(kept)
    assert {finding["threshold"] for finding in still} == {"2×"}
    drawer = client.get(f"/api/v1/findings/{resolved[0]['finding_id']}").json()["finding"]
    assert (drawer["state"], drawer["resolved_why"]) == ("resolved", "settings_changed")
    # The Issue page's effort threshold, and the Overview's rules: W-AGE-01 is off on the board.
    assert client.get(f"/api/v1/issues/{kept[0]}/timeline").json()["issue"]["effort_threshold"] == 2.0
    overview = client.get("/api/v1/overview").json()
    assert "W-AGE-01" not in {rule["rule_id"] for rule in overview["by_rule"]}
    # Ask's rule status, at the board's settings too.
    asked = client.post("/api/v1/ask", json={"question": "Which rules can't be measured in Jira today?"}).json()
    status = [fact for fact in asked["facts"] if fact["tool"] == "oversight_rule_status"]
    listed = {row["rule_id"] for fact in status for row in fact["rows"]}
    assert listed and "W-AGE-01" not in listed


def test_a_change_of_rule_settings_is_neither_new_nor_resolved_work_on_the_overview_or_the_portfolio(
    board: Settings,
) -> None:
    """The Overview's "New this week" and "Resolved this week", and the Portfolio's figures (the same totals), count
    what happened to the work. A severity changed on the board raises the rule's findings again under new ids, a rule
    turned off and a threshold raised close findings as settings_changed: none of it is new work, and none of it a
    fix."""
    clock = Clock(NOW + timedelta(days=8))  # the seed's findings were first seen more than a week ago
    run_the_rules(board, ["DEMO"], clock())  # the board as of then, its settings untouched
    client, refresher = serve(board, clock=clock)

    def tiles() -> dict[str, int]:
        totals = client.get("/api/v1/overview").json()["totals"]
        [row] = client.get("/api/v1/portfolio?board=DEMO").json()["boards"]
        assert (row["new_findings_7d"], row["resolved_findings_7d"]) == (
            totals["new_7d"]["current"], totals["resolved_7d"]["current"])
        return {name: totals[name]["current"] for name in ("new_7d", "resolved_7d", "open_breaches", "open_warnings")}

    before, open_now = tiles(), open_by_rule(board)
    assert all(open_now[rule_id] > 0 for rule_id in ("R-FLOW-02", "R-FLOW-03", "R-EFF-01"))
    raised = open_now["R-EFF-01"] - len(under_effort(board, 2.0))
    clock.at += timedelta(minutes=1)  # the change and its re-check, a minute on
    save(client, {"rule_id": "R-FLOW-03", "severity": "breach"}, {"rule_id": "R-FLOW-02", "enabled": False},
         {"rule_id": "R-EFF-01", "params": {"threshold": 2}})
    settle(refresher)

    after = tiles()
    assert (after["new_7d"], after["resolved_7d"]) == (before["new_7d"], before["resolved_7d"])
    # The findings did change: R-FLOW-03's are breaches now, and R-FLOW-02's and some of R-EFF-01's are closed.
    assert after["open_warnings"] == before["open_warnings"] - open_now["R-FLOW-03"]
    assert after["open_breaches"] == (before["open_breaches"] + open_now["R-FLOW-03"] - open_now["R-FLOW-02"]
                                      - open_now["R-EFF-01"] + raised)
    whys = query(board, "SELECT resolved_why, COUNT(*) FROM findings WHERE resolved_at = ? GROUP BY resolved_why",
                 (to_iso(clock()),))
    assert dict(whys).keys() == {"settings_changed", "superseded"}  # what the change closed: never "cleared"


def test_a_closed_finding_says_the_threshold_it_was_found_under(board: Settings) -> None:
    """A finding the board's settings closed still says the threshold it crossed, whatever the board's is now: the
    evidence records it. One still open says the board's new threshold."""
    client, refresher = serve(board)
    save(client, {"rule_id": "R-EST-02", "params": {"grace_hours": 24}},
         {"rule_id": "W-SPR-01", "params": {"days_left": 0, "not_started_share": 0.95}})
    settle(refresher)
    for rule_id, then in (("R-EST-02", "1 h grace"), ("W-SPR-01", "≤ 2 days left, > 30% not started")):
        closed = client.get(f"/api/v1/findings?state=resolved&rule={rule_id}&page_size=100").json()["items"]
        assert closed and {finding["resolved_why"] for finding in closed} == {"settings_changed"}
        assert {finding["threshold"] for finding in closed} == {then}
        drawer = client.get(f"/api/v1/findings/{closed[0]['finding_id']}").json()["finding"]
        assert drawer["threshold"] == then
    still = client.get("/api/v1/findings?state=open&rule=R-EST-02&page_size=100").json()["items"]
    assert still and {finding["threshold"] for finding in still} == {"24 h grace"}
    rules = rules_of(client.get("/api/v1/rules").json())
    assert (rules["R-EST-02"]["threshold"], rules["W-SPR-01"]["threshold"]) == (
        "24 h grace", "≤ 0 days left, > 95% not started")


def test_a_rule_rules_yaml_has_off_holds_no_findings_once_the_board_turns_it_on_and_off_again(
    board: Settings, tmp_path: Path
) -> None:
    """rules.yaml with R-HYG-01 off: its findings from before are left as they were while the board never touched it.
    Turned on by the board, it is customised; turned off again it is rules.yaml's once more (no row is kept), and its
    findings, the board's doing, close as settings_changed: a rule that is off holds no open findings (contract 4.9)."""
    config = tmp_path / "config"
    shutil.copytree(CONFIG_FIXTURES, config)
    rules_yaml = config / "rules.yaml"
    marker = "    check: in_progress_unassigned\n"
    rules_yaml.write_text(rules_yaml.read_text(encoding="utf-8").replace(marker, f"{marker}    enabled: false\n"),
                          encoding="utf-8")
    stingy = board.model_copy(update={"config_dir": config})
    client, refresher = serve(stingy)
    held = open_by_rule(board)["R-HYG-01"]
    hygiene = rules_of(client.get(SETTINGS).json())["R-HYG-01"]
    assert (hygiene["enabled"], hygiene["default_enabled"], hygiene["customised"], hygiene["open"]) == (
        False, False, False, held)

    turned_on = save(client, {"rule_id": "R-HYG-01", "enabled": True})
    assert_settings_contract(turned_on)
    assert (rules_of(turned_on)["R-HYG-01"]["enabled"], rules_of(turned_on)["R-HYG-01"]["customised"]) == (True, True)
    settle(refresher)
    turned_off = save(client, {"rule_id": "R-HYG-01", "enabled": False}, revision=1)
    assert_settings_contract(turned_off)
    assert (rules_of(turned_off)["R-HYG-01"]["enabled"], rules_of(turned_off)["R-HYG-01"]["customised"]) == (
        False, False)
    assert query(board, "SELECT COUNT(*) FROM rule_settings") == [(0,)]
    settle(refresher)

    assert "R-HYG-01" not in open_by_rule(board)
    assert query(board, "SELECT DISTINCT resolved_why FROM findings WHERE rule_id = 'R-HYG-01'") == [
        ("settings_changed",)]
    after = client.get(SETTINGS).json()
    assert (rules_of(after)["R-HYG-01"]["open"], after["apply"]["result"]) == (0, {"opened": 0, "closed": held})
    listed = rules_of(client.get("/api/v1/rules").json())["R-HYG-01"]
    assert (listed["enabled"], listed["customised"], listed["open"]) == (False, False, 0)


# --- the dashboard's contract, in every state -------------------------------------------------------------------


def test_every_body_keeps_the_dashboards_contract_in_every_state(
    board: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every body the service sends for contract 4.20 to 4.22, as the dashboard parses it (assert_settings_contract and
    assert_preview_contract mirror its schemas, refinements included): a board never changed and one with no data;
    previews; a save while its re-check runs, applied by it, failed, pending after a restart and applied by a cycle
    run; an undo; and each refusal (422, 409, 503), a board that never synced (409 board_not_synced, JO-17) too."""
    clock = Clock(SAVED)
    steps = Steps(board, clock, hold=True)
    client, refresher = serve(board, clock=clock, steps=steps)
    assert_settings_contract(client.get(SETTINGS).json())
    empty = client.get(f"{SETTINGS}&project=AIL").json()
    assert_settings_contract(empty, project="AIL")
    assert empty["rules"] and empty["revision"] == 0

    every_kind = [{"rule_id": "R-HYG-01", "enabled": False}, {"rule_id": "R-EFF-01", "params": {"threshold": 2}},
                  {"rule_id": "R-FLOW-03", "severity": "breach"}, {"rule_id": "R-FLOW-01", "severity": "warning"},
                  {"rule_id": "W-SPR-01", "params": {"days_left": 0.5, "not_started_share": 0.5}}]
    for changes in (every_kind, [{"rule_id": "R-FLOW-01", "severity": "warning"}]):  # the second: not measurable
        assert_preview_contract(client.post(PREVIEW, json={"changes": changes}).json())
    assert_preview_contract(client.post(f"{PREVIEW}&project=AIL", json={"changes": every_kind}).json(), project="AIL")

    running = save(client, *every_kind, reason="Agreed with the team")
    assert_settings_contract(running)
    assert running["apply"]["state"] == "running"
    steps.release.set()
    settle(refresher)
    applied = client.get(SETTINGS).json()
    assert_settings_contract(applied)
    assert (applied["apply"]["state"], applied["apply"]["result"] is not None) == ("applied", True)

    clock.at = SAVED + timedelta(minutes=10)
    failing, refresher = serve(board, clock=clock, steps=Steps(board, clock, fail=True))
    assert_settings_contract(save(failing, {"rule_id": "W-AGE-01", "params": {"pct": 80}}, revision=1))
    settle(refresher)
    failed = failing.get(SETTINGS).json()
    assert_settings_contract(failed)
    assert failed["apply"]["state"] == "failed"
    restarted, _ = serve(board, clock=clock)
    pending = restarted.get(SETTINGS).json()
    assert_settings_contract(pending)
    assert pending["apply"]["state"] == "pending"
    assert_preview_contract(restarted.post(PREVIEW, json={"changes": [{"rule_id": "W-EFF-01", "enabled": False}]}).json())
    clock.at = SAVED + timedelta(minutes=20)
    run_the_rules(board, ["DEMO", "AIL"], clock())  # the next sync's rules step, in another process
    by_cycle = restarted.get(SETTINGS).json()
    assert_settings_contract(by_cycle)
    assert by_cycle["apply"]["state"] == "applied"

    latest = by_cycle["history"][0]
    undo, refresher = serve(board, clock=clock)
    undone = save(undo, {"rule_id": "W-AGE-01", "params": {"pct": 90}}, revision=2, reason="Undo of revision 2",
                  undo_of=latest["change_id"])
    assert_settings_contract(undone)
    settle(refresher)
    assert_settings_contract(undo.get(SETTINGS).json())

    for body in ({"revision": 3, "changes": [{"rule_id": "W-EFF-01", "params": {"threshold": 3}}]},
                 {"revision": 3, "changes": [{"rule_id": "R-NOPE-01", "enabled": False}], "reason": "x" * 501},
                 {"revision": 3, "changes": [{"rule_id": "hygiene"}], "title": "x"}):
        assert_refusal_contract(undo.post(SETTINGS, json=body), 422, INVALID)
    assert_refusal_contract(undo.post(PREVIEW, json={"changes": []}), 422, INVALID)
    assert_refusal_contract(undo.post(SETTINGS, json={"revision": 0, "changes": [
        {"rule_id": "R-HYG-01", "enabled": True}]}), 409, "rule_settings_conflict")
    monkeypatch.setattr(rule_settings_module, "WRITE_WAIT_MS", 50)
    holder = sqlite3.connect(board.db_file, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")  # as a rules run holds it for a board
        busy = undo.post(SETTINGS, json={"revision": 3, "changes": [{"rule_id": "R-HYG-01", "enabled": True}]})
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert_refusal_contract(busy, 503, "rule_settings_busy")

    with Store.open(board.db_file) as store:  # AIL as it was before its first sync
        store.delete_rows("sync_state", {"scope": "AIL"})
    for response in (undo.get(f"{SETTINGS}&project=AIL"), undo.post(f"{PREVIEW}&project=AIL", json={"changes": []}),
                     undo.post(f"{SETTINGS}&project=AIL", json={"revision": 0, "changes": []})):
        assert_refusal_contract(response, 409, "board_not_synced")
        assert response.json()["detail"] == "AIL hasn't been synced yet."
