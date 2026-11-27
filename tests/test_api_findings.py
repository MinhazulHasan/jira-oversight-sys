from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cmp_to_key
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oversight.api.app import create_app
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import to_iso
from tests.conftest import CONFIG_FIXTURES, VIEWER_HEADERS

T = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)  # a Wednesday; the demo is seeded "now"
KEY = "test-key"


def seed_lifecycle(db_path: Path) -> None:
    """DEMO seeded and derived at T; rules at T-35d (R-EFF-01 at 1.2×, W-EFF-01 at 0.5×), T-10d and T."""
    process = ProcessConfig.load(CONFIG_FIXTURES / "process.yaml")
    rules = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")
    fields = FieldsConfig.load(CONFIG_FIXTURES / "fields.yaml")
    early = rules.model_copy(deep=True)
    for rule in early.rules:
        if rule.id in ("R-EFF-01", "W-EFF-01"):
            rule.params["threshold"] = {"R-EFF-01": 1.2, "W-EFF-01": 0.5}[rule.id]
    store = Store.open(db_path)
    try:
        seed_demo(store, now=T, seed=20261104)
        run_derive(store, process, projects=["DEMO"], now=lambda: T)
        for catalogue, when in ((early, T - timedelta(days=35)), (rules, T - timedelta(days=10)), (rules, T)):
            run_rules(store, process, catalogue, fields, projects=["DEMO"], now=lambda when=when: when)
    finally:
        store.close()


def make_client(db_path: Path, root: Path, *, config_dir: Path = CONFIG_FIXTURES, base_url: str = "") -> TestClient:
    settings = Settings(
        _env_file=None,
        jira_projects="DEMO",
        jira_base_url=base_url,
        api_service_key=KEY,
        db_path=db_path,
        config_dir=config_dir,
        reports_dir=root / "reports",
    )
    app = create_app(settings, provider=ScriptedProvider(), now=lambda: T)
    return TestClient(app, headers={"X-Service-Key": KEY, **VIEWER_HEADERS})


@dataclass
class Api:
    client: TestClient
    db: sqlite3.Connection
    db_path: Path
    root: Path

    def get(self, path: str, params: Any = None) -> Any:
        response = self.client.get(f"/api/v1{path}", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def rows(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params if isinstance(params, dict) else tuple(params)).fetchall()


def open_api(root: Path) -> Iterator[Api]:
    db_path = root / "oversight.db"
    seed_lifecycle(db_path)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        with make_client(db_path, root) as client:
            yield Api(client, db, db_path, root)
    finally:
        db.close()


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Api]:
    yield from open_api(tmp_path_factory.mktemp("api_findings"))


ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,19}-[1-9]\d{0,8}$")
RULE_ID = re.compile(r"^[RW]-[A-Z]+-\d{2}$")
FINDING_ID = re.compile(r"^[0-9a-f]{32}$")
STAGES = {"backlog", "estimate", "development", "code_review", "dev_signoff", "testing", "test_review",
          "test_signoff", "done"}
UNITS = {"count", "hours", "ratio", "share", "points", "days"}
CATEGORIES = {"new", "indeterminate", "done"}
FINDING_FIELDS = (
    "finding_id", "rule_id", "rule_title", "rule_version", "kind", "severity", "state", "message", "scope",
    "issue_key", "issue_summary", "issue_type", "epic_key", "epic_title", "assignee", "sprint", "first_seen_at",
    "last_seen_at", "resolved_at", "resolved_why", "threshold", "evidence", "warned_before", "ignore",
)
PERSON_ID = re.compile(r"^p[0-9a-f]{12}$")
AVATAR = re.compile(r"^[0-9a-f]{12}$")


def fields_are(obj: Any, *names: str) -> None:
    assert isinstance(obj, dict), obj
    assert sorted(obj) == sorted(names), sorted(set(obj) ^ set(names))


def is_ts(value: Any) -> bool:
    return isinstance(value, str) and bool(ISO_TS.match(value)) and datetime.fromisoformat(value) is not None


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def text_or_none(value: Any, *, min_length: int = 0) -> bool:
    return value is None or (isinstance(value, str) and len(value) >= min_length)


def check_meta(meta: Any) -> None:
    fields_are(meta, "data_mode", "last_synced_at", "projects", "rules_version", "process_version",
               "generated_at", "report_timezone")
    assert meta["data_mode"] == "live"
    assert is_ts(meta["last_synced_at"]) and is_ts(meta["generated_at"])
    assert meta["projects"] == ["DEMO"]
    assert meta["rules_version"] and meta["process_version"] and meta["report_timezone"]


def check_interval(interval: Any) -> None:
    fields_are(interval, "status", "stage", "status_category", "entered_at", "left_at", "hours")
    assert isinstance(interval["status"], str) and interval["status"]
    assert interval["stage"] is None or interval["stage"] in STAGES
    assert interval["status_category"] in CATEGORIES
    assert is_ts(interval["entered_at"])
    assert interval["left_at"] is None or is_ts(interval["left_at"])
    assert interval["hours"] is None or (is_number(interval["hours"]) and interval["hours"] >= 0)


def check_avatar(value: Any) -> None:
    """A picture's version: 12 lower-case hex characters, or null when the service holds no picture."""
    assert value is None or (isinstance(value, str) and AVATAR.match(value)), value


def check_person(value: Any) -> None:
    if value is not None:
        fields_are(value, "id", "name", "avatar")
        assert PERSON_ID.match(value["id"]) and isinstance(value["name"], str) and 1 <= len(value["name"]) <= 128
        check_avatar(value["avatar"])


def check_evidence(item: Any) -> None:
    fields_are(item, "kind", "label", "value", "unit", "from", "to", "at", "changelog_id")
    assert item["kind"] in {"status_change", "field_change", "metric", "sprint"}
    assert isinstance(item["label"], str) and item["label"]
    assert item["value"] is None or isinstance(item["value"], str) or is_number(item["value"])
    assert item["unit"] is None or item["unit"] in UNITS
    assert text_or_none(item["from"]) and text_or_none(item["to"]) and text_or_none(item["changelog_id"])
    assert item["at"] is None or is_ts(item["at"])


def check_finding(finding: Any) -> None:
    fields_are(finding, *FINDING_FIELDS)
    assert FINDING_ID.match(finding["finding_id"]) and RULE_ID.match(finding["rule_id"])
    assert finding["rule_title"] and finding["rule_version"] and finding["message"]
    assert finding["kind"] in {"rule", "warning"} and finding["severity"] in {"breach", "warning"}
    assert finding["state"] in {"open", "resolved", "ignored"} and finding["scope"] in {"issue", "sprint"}
    assert finding["issue_key"] is None or ISSUE_KEY.match(finding["issue_key"])
    assert finding["epic_key"] is None or ISSUE_KEY.match(finding["epic_key"])
    for name in ("issue_summary", "issue_type", "epic_title", "threshold"):
        assert text_or_none(finding[name]), name
    sprint = finding["sprint"]
    if sprint is not None:
        fields_are(sprint, "sprint_id", "name")
        assert isinstance(sprint["sprint_id"], int) and not isinstance(sprint["sprint_id"], bool) and sprint["name"]
    assert is_ts(finding["first_seen_at"]) and is_ts(finding["last_seen_at"])
    assert finding["resolved_at"] is None or is_ts(finding["resolved_at"])
    assert finding["resolved_why"] in {None, "cleared", "superseded", "ignored"}
    assert isinstance(finding["evidence"], list)
    for item in finding["evidence"]:
        check_evidence(item)
    warned = finding["warned_before"]
    if warned is not None:
        fields_are(warned, "warning_rule_id", "warning_finding_id", "warned_at")
        assert RULE_ID.match(warned["warning_rule_id"]) and FINDING_ID.match(warned["warning_finding_id"])
        assert is_ts(warned["warned_at"])
    check_person(finding["assignee"])
    assert (finding["scope"] == "issue") == (finding["issue_key"] is not None)
    assert (finding["scope"] == "sprint") == (finding["sprint"] is not None)
    assert (finding["state"] != "open") == (finding["resolved_at"] is not None)
    assert (finding["state"] == "ignored") == (finding["resolved_why"] == "ignored")
    assert finding["scope"] == "issue" or finding["assignee"] is None  # a sprint finding belongs to no one
    ignore = finding["ignore"]
    if ignore is not None:  # set only beside an ignored finding, with the ignore that holds it
        fields_are(ignore, "ignore_id", "scope", "rule_id", "reason", "by", "at")
        assert finding["state"] == "ignored" and FINDING_ID.match(ignore["ignore_id"]) and is_ts(ignore["at"])
        assert (ignore["scope"], ignore["rule_id"]) in {("ticket", None), ("finding", finding["rule_id"])}
        assert text_or_none(ignore["reason"]) and text_or_none(ignore["by"], min_length=1)


def check_page(body: Any) -> None:
    fields_are(body, "meta", "items", "total", "page", "page_size", "facets")
    check_meta(body["meta"])
    for finding in body["items"]:
        check_finding(finding)
    assert isinstance(body["total"], int) and body["total"] >= 0
    assert isinstance(body["page"], int) and body["page"] >= 1
    assert isinstance(body["page_size"], int) and 1 <= body["page_size"] <= 100
    fields_are(body["facets"], "rules", "epics", "severity", "state", "people")
    for name, entries in body["facets"].items():
        for entry in entries:
            if name == "people":
                fields_are(entry, "value", "label", "count", "avatar")
                check_avatar(entry["avatar"])
                assert entry["value"] != "unassigned" or entry["avatar"] is None
            else:
                fields_are(entry, "value", "label", "count")
            assert entry["value"] and entry["label"] and isinstance(entry["count"], int) and entry["count"] >= 0


FROM = """
    FROM findings AS f
    LEFT JOIN issues AS i ON i.issue_key = f.issue_key
    LEFT JOIN issues AS p ON p.issue_key = i.parent_key
    LEFT JOIN issues AS e ON e.issue_key = COALESCE(i.epic_key, p.epic_key)
    WHERE f.project_key = 'DEMO'
"""
EPIC = "COALESCE(i.epic_key, p.epic_key)"
OPEN = " AND f.resolved_at IS NULL"
STATE_SQL = "CASE WHEN f.resolved_at IS NULL THEN 'open' WHEN f.resolved_why = 'ignored' THEN 'ignored' ELSE 'resolved' END"
API_KEY = "CASE WHEN f.issue_key LIKE 'sprint:%' THEN NULL ELSE f.issue_key END"


def sql_ids(api: Api, where: str = "", params: Sequence[Any] | dict[str, Any] = ()) -> set[str]:
    return {row[0] for row in api.rows(f"SELECT f.finding_id {FROM} {where}", params)}


def sql_counts(api: Api, column: str, where: str = "", params: Sequence[Any] = ()) -> dict[str, int]:
    return {row[0]: row[1] for row in api.rows(f"SELECT {column}, count(*) {FROM} {where} GROUP BY 1", params)}


def all_items(api: Api, params: Sequence[tuple[str, Any]] = ()) -> list[dict[str, Any]]:
    """Every finding a query matches, page by page (100 at a time)."""
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        body = api.get("/findings", [*params, ("page_size", 100), ("page", page)])
        items += body["items"]
        if not body["items"] or len(items) >= body["total"]:
            assert len(items) == body["total"]
            return items
        page += 1


def since(days: int) -> str:
    return to_iso(T - timedelta(days=days)) or ""


def facet(body: Any, name: str) -> dict[str, int]:
    return {entry["value"]: entry["count"] for entry in body["facets"][name]}


def matches_sql(counts: dict[str, int], expected: dict[str, int]) -> bool:
    """Facet counts equal SQL's, with the entries SQL never sees at zero."""
    return set(expected) <= set(counts) and counts == {value: expected.get(value, 0) for value in counts}


def account_ids(api: Api) -> set[str]:
    """Every account id in the snapshot: assignees, changelog and worklog authors, assignee changes."""
    rows = api.rows(
        "SELECT assignee_id FROM issues UNION SELECT author_id FROM status_changes UNION SELECT author_id FROM worklogs"
        " UNION SELECT from_value FROM field_changes WHERE field = 'assignee'"
        " UNION SELECT to_value FROM field_changes WHERE field = 'assignee'"
    )
    return {row[0] for row in rows if row[0]}


RULES = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")


def test_needs_the_service_key(api: Api) -> None:
    assert api.client.get("/api/v1/findings", headers={"X-Service-Key": "wrong"}).status_code == 401


def test_default_page_is_open_findings_newest_first(api: Api) -> None:
    body = api.get("/findings")
    check_page(body)
    assert (body["page"], body["page_size"], len(body["items"])) == (1, 25, 25)
    assert body["total"] == len(sql_ids(api, OPEN)) > 100
    rows = api.rows(f"SELECT f.finding_id {FROM} {OPEN} ORDER BY f.first_seen_at DESC, f.finding_id LIMIT 25")
    assert [item["finding_id"] for item in body["items"]] == [row[0] for row in rows]


def test_findings_carry_the_stored_values(api: Api) -> None:
    items = api.get("/findings", {"state": "all", "sort": "issue_key", "page_size": 100})["items"]
    assert {item["state"] for item in items} == {"open", "resolved"}
    for item in items:
        [row] = api.rows(
            f"SELECT f.*, i.summary, i.issue_type, {EPIC} AS epic, e.summary AS epic_summary {FROM} AND f.finding_id = ?",
            (item["finding_id"],),
        )
        assert (item["rule_id"], item["rule_version"], item["kind"], item["severity"], item["message"]) == (
            row["rule_id"], row["rule_version"], row["kind"], row["severity"], row["message"])
        assert item["rule_title"] == RULES.by_id(row["rule_id"]).title
        assert (item["first_seen_at"], item["last_seen_at"], item["resolved_at"], item["resolved_why"]) == (
            row["first_seen_at"], row["last_seen_at"], row["resolved_at"], row["resolved_why"])
        assert item["state"] == ("resolved" if row["resolved_at"] else "open")
        assert (item["scope"], item["issue_key"], item["sprint"]) == ("issue", row["issue_key"], None)
        assert (item["issue_summary"], item["issue_type"]) == (row["summary"], row["issue_type"])
        assert (item["epic_key"], item["epic_title"]) == (row["epic"], row["epic_summary"])


FILTERS: list[tuple[str, list[tuple[str, str]], str, tuple[Any, ...]]] = [
    ("one rule", [("rule", "R-EST-01")], OPEN + " AND f.rule_id = 'R-EST-01'", ()),
    ("two rules", [("rule", "W-AGE-01"), ("rule", "R-EST-01")], OPEN + " AND f.rule_id IN ('R-EST-01', 'W-AGE-01')", ()),
    ("severity", [("severity", "warning")], OPEN + " AND f.severity = 'warning'", ()),
    ("resolved", [("state", "resolved")], " AND f.resolved_at IS NOT NULL AND COALESCE(f.resolved_why, '') != 'ignored'",
     ()),
    ("all states", [("state", "all")], "", ()),
    ("one epic", [("epic", "DEMO-36")], OPEN + f" AND {EPIC} = 'DEMO-36'", ()),
    ("no epic", [("epic", "none")], OPEN + f" AND {EPIC} IS NULL", ()),
    ("an epic or none", [("epic", "none"), ("epic", "DEMO-36")], OPEN + f" AND ({EPIC} = 'DEMO-36' OR {EPIC} IS NULL)", ()),
    ("first seen 7d", [("first_seen", "7d")], OPEN + " AND f.first_seen_at >= ?", (since(7),)),
    ("first seen 30d", [("first_seen", "30d")], OPEN + " AND f.first_seen_at >= ?", (since(30),)),
    ("first seen 90d", [("first_seen", "90d")], OPEN + " AND f.first_seen_at >= ?", (since(90),)),
    ("summary text", [("q", "Pagination")], OPEN + " AND lower(i.summary) LIKE '%pagination%'", ()),
    ("sprint name", [("q", "sprint 14")],
     OPEN + " AND f.issue_key LIKE 'sprint:%' AND lower(json_extract(f.evidence_json, '$.name')) LIKE '%sprint 14%'", ()),
    ("combined", [("rule", "R-EFF-01"), ("rule", "W-EFF-01"), ("state", "all"), ("first_seen", "30d"), ("epic", "DEMO-36")],
     f" AND f.rule_id IN ('R-EFF-01', 'W-EFF-01') AND f.first_seen_at >= ? AND {EPIC} = 'DEMO-36'", (since(30),)),
]


@pytest.mark.parametrize(("params", "where", "values"), [case[1:] for case in FILTERS], ids=[case[0] for case in FILTERS])
def test_each_filter_narrows_to_what_sql_finds(
    api: Api, params: list[tuple[str, str]], where: str, values: tuple[Any, ...]
) -> None:
    expected = sql_ids(api, where, values)
    assert expected, "a case that matches nothing proves nothing"
    found = [item["finding_id"] for item in all_items(api, params)]
    assert len(found) == len(set(found)) == len(expected)
    assert set(found) == expected


def test_q_finds_an_exact_key_in_any_case_and_a_key_number(api: Api) -> None:
    key = api.rows(f"SELECT f.issue_key {FROM} {OPEN} AND f.issue_key NOT LIKE 'sprint:%' ORDER BY f.issue_key LIMIT 1")[0][0]
    number = key.rsplit("-", 1)[1]
    exact = sql_ids(api, OPEN + " AND (f.issue_key = ? OR lower(i.summary) LIKE ?)", (key, f"%{key.lower()}%"))
    assert exact and {item["finding_id"] for item in all_items(api, [("q", f"  {key.lower()} ")])} == exact
    by_number = sql_ids(api, OPEN + " AND f.issue_key LIKE ?", (f"%-{number}",))
    assert by_number and {item["finding_id"] for item in all_items(api, [("q", f"#{number}")])} == by_number
    sprint_name = "f.issue_key LIKE 'sprint:%' AND json_extract(f.evidence_json, '$.name') LIKE :text"
    bare = sql_ids(api, OPEN + f" AND ({API_KEY} LIKE :text OR i.summary LIKE :text OR ({sprint_name}))",
                   {"text": f"%{number}%"})
    assert by_number < bare and {item["finding_id"] for item in all_items(api, [("q", number)])} == bare


def test_epic_none_includes_sprint_findings(api: Api) -> None:
    items = all_items(api, [("epic", "none"), ("state", "all")])
    assert all(item["epic_key"] is None for item in items)
    sprint = {item["finding_id"] for item in items if item["scope"] == "sprint"}
    assert sprint and sprint == sql_ids(api, " AND f.issue_key LIKE 'sprint:%'")


def test_a_subtasks_epic_is_its_parents(api: Api) -> None:
    [row] = api.rows(
        f"SELECT f.finding_id, p.epic_key AS epic, e.summary AS title {FROM} AND i.issue_type = 'Subtask'"
        " AND i.epic_key IS NULL AND p.epic_key IS NOT NULL ORDER BY f.finding_id LIMIT 1"
    )
    finding = api.get(f"/findings/{row['finding_id']}")["finding"]
    assert (finding["epic_key"], finding["epic_title"]) == (row["epic"], row["title"])
    in_epic = {item["finding_id"] for item in all_items(api, [("epic", row["epic"]), ("state", "all")])}
    assert row["finding_id"] in in_epic and in_epic == sql_ids(api, f" AND {EPIC} = ?", (row["epic"],))
    assert row["finding_id"] not in {item["finding_id"] for item in all_items(api, [("epic", "none"), ("state", "all")])}


def test_facets_list_every_entry_in_a_fixed_order(api: Api) -> None:
    body = api.get("/findings")
    assert [(e["value"], e["label"]) for e in body["facets"]["rules"]] == [(r.id, r.title) for r in RULES.rules]
    epics = api.rows("SELECT issue_key, summary FROM issues WHERE project_key = 'DEMO' AND issue_type = 'Epic'")
    by_number = sorted(((row[0], row[1]) for row in epics), key=lambda pair: int(pair[0].rsplit("-", 1)[1]))
    assert len(by_number) == 14
    assert [(e["value"], e["label"]) for e in body["facets"]["epics"]] == [*by_number, ("none", "No epic")]
    assert [(e["value"], e["label"]) for e in body["facets"]["severity"]] == [("breach", "Breach"), ("warning", "Warning")]
    assert [(e["value"], e["label"]) for e in body["facets"]["state"]] == [
        ("open", "Open"), ("resolved", "Resolved"), ("ignored", "Ignored")]
    assert facet(body, "rules")["R-FLOW-01"] == 0  # not measurable in DEMO: listed at zero
    assert matches_sql(facet(body, "rules"), sql_counts(api, "f.rule_id", OPEN))
    assert matches_sql(facet(body, "epics"), sql_counts(api, f"COALESCE({EPIC}, 'none')", OPEN))
    assert matches_sql(facet(body, "severity"), sql_counts(api, "f.severity", OPEN))
    state = STATE_SQL
    assert matches_sql(facet(body, "state"), sql_counts(api, state))


def test_facet_counts_apply_every_filter_but_their_own(api: Api) -> None:
    body = api.get("/findings", [("severity", "breach"), ("epic", "DEMO-36"), ("first_seen", "90d"), ("q", "Fix")])
    window = " AND f.first_seen_at >= ? AND lower(i.summary) LIKE '%fix%'"
    breach, epic, state = " AND f.severity = 'breach'", f" AND {EPIC} = 'DEMO-36'", OPEN
    params = (since(90),)
    assert body["total"] == len(sql_ids(api, state + breach + epic + window, params)) > 0
    assert matches_sql(facet(body, "rules"), sql_counts(api, "f.rule_id", state + breach + epic + window, params))
    assert matches_sql(facet(body, "severity"), sql_counts(api, "f.severity", state + epic + window, params))
    assert matches_sql(facet(body, "epics"), sql_counts(api, f"COALESCE({EPIC}, 'none')", state + breach + window, params))
    by_state = STATE_SQL
    assert matches_sql(facet(body, "state"), sql_counts(api, by_state, breach + epic + window, params))
    unfiltered = api.get("/findings")
    assert sum(facet(body, "epics").values()) < sum(facet(unfiltered, "epics").values())


SORT_RULES = ("W-SPR-01", "R-FLOW-03", "R-HYG-01", "W-EFF-01")  # 94 findings: nulls, both states, three days


def expected_order(rows: Sequence[sqlite3.Row], sort: str, order: str) -> list[str]:

    def value(row: sqlite3.Row) -> Any:
        if sort == "severity":
            return {"breach": 2, "warning": 1}[row["severity"]]
        if sort in ("issue_key", "epic_key"):
            return None if row[sort] is None else int(row[sort].rsplit("-", 1)[1])
        return row[sort]

    def compare(a: sqlite3.Row, b: sqlite3.Row) -> int:
        left, right = value(a), value(b)
        if left is None or right is None:
            if (left is None) != (right is None):
                return 1 if left is None else -1
        elif left != right:
            return (-1 if left < right else 1) * (1 if order == "asc" else -1)
        if a["first_seen_at"] != b["first_seen_at"]:
            return -1 if a["first_seen_at"] > b["first_seen_at"] else 1
        return -1 if a["finding_id"] < b["finding_id"] else 1

    return [row["finding_id"] for row in sorted(rows, key=cmp_to_key(compare))]


@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize(
    "sort", ["first_seen_at", "last_seen_at", "resolved_at", "severity", "rule_id", "issue_key", "epic_key"]
)
def test_sorting(api: Api, sort: str, order: str) -> None:
    marks = ", ".join("?" * len(SORT_RULES))
    rows = api.rows(
        f"SELECT f.finding_id, f.rule_id, f.severity, f.first_seen_at, f.last_seen_at, f.resolved_at,"
        f" {API_KEY} AS issue_key, {EPIC} AS epic_key {FROM} AND f.rule_id IN ({marks})",
        SORT_RULES,
    )
    assert any(row["epic_key"] is None for row in rows) and any(row["resolved_at"] for row in rows)
    params = [*(("rule", rule) for rule in SORT_RULES), ("state", "all"), ("sort", sort), ("order", order), ("page_size", 100)]
    body = api.get("/findings", params)
    assert body["total"] == len(rows) <= 100
    assert [item["finding_id"] for item in body["items"]] == expected_order(rows, sort, order)


def test_paging_is_stable_and_a_page_past_the_end_is_empty(api: Api) -> None:
    whole = [item["finding_id"] for item in all_items(api)]
    pages, page = [], 1
    while page == 1 or pages[-1]:
        pages.append([item["finding_id"] for item in api.get("/findings", {"page": page, "page_size": 25})["items"]])
        page += 1
    assert [finding_id for chunk in pages for finding_id in chunk] == whole
    assert len(whole) == len(set(whole)) and all(len(chunk) == 25 for chunk in pages[:-2])
    assert api.get("/findings", {"page": 3}) == api.get("/findings", {"page": 3})
    beyond = api.get("/findings", {"page": 10_000})
    assert (beyond["items"], beyond["total"], beyond["page"]) == ([], len(whole), 10_000)
