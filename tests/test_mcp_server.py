"""The read-only MCP server (Phase 6, ticket 14), driven in-process through
mcp.client.session.ClientSession over an in-memory transport, against a seeded
demo project: every catalogue tool is listed with read-only annotations and
returns the same rows as catalog.run_query; bad input, a missing database and
an unconfigured project come back as JSON error objects; oversight_catalog
lists the vocabulary; nothing writes.

The demo is seeded and derived once per module (the rules engine is not
needed: a few hand-written findings give list_findings and hot_spots rows).
The server's clock is pinned to the seeding `now`, so period windows match the
reference queries exactly.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent, Tool
from structlog.testing import capture_logs

from oversight.ask.catalog import CATALOG, CatalogContext, catalog_schema, run_query
from oversight.cli import EXIT_CONFIG, EXIT_OK, main as cli_main
from oversight.config import ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.errors import ConfigError
from oversight.mcp_server import server as mcp_server
from oversight.mcp_server.server import CATALOG_TOOL, SERVER_NAME, build_server, main
from oversight.rules.catalog import RulesConfig
from oversight.rules.settings import Change, save_board_settings
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC, to_iso

from tests.conftest import CONFIG_FIXTURES

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
PROJECT = "DEMO"
ISSUES = 240
RULES_VERSION = "0.1.0"
# One valid call per spec, plus the parameters that take other code paths (periods, groupings, filters).
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("oversight_count_issues", {}),
    ("oversight_count_issues", {"status": "in progress", "issue_type": "bug", "period": "30d"}),
    ("oversight_count_issues", {"has_estimate": False, "period": "sprint"}),
    ("oversight_top_issues", {"metric": "cycle_time"}),
    ("oversight_top_issues", {"metric": "effort_ratio", "n": 3, "status_category": "done"}),
    ("oversight_top_issues", {"metric": "status_age", "issue_type": "Task"}),
    ("oversight_top_issues", {"metric": "reopens"}),
    ("oversight_time_in_status", {}),
    ("oversight_time_in_status", {"group_by": "issue_type", "period": "all"}),
    ("oversight_time_in_status", {"group_by": "epic", "period": "sprint"}),
    ("oversight_effort_vs_estimate", {}),
    ("oversight_effort_vs_estimate", {"group_by": "sprint", "period": "all"}),
    ("oversight_list_findings", {}),
    ("oversight_list_findings", {"rule_id": "R-FLOW-02", "state": "all", "n": 2}),
    ("oversight_list_findings", {"severity": "warning", "state": "resolved"}),
    ("oversight_trend", {"metric": "median_cycle_time_h"}),
    ("oversight_trend", {"metric": "open_breaches", "last_n": 3}),
    ("oversight_coverage", {"field": "estimate"}),
    ("oversight_coverage", {"field": "time_logged", "period": "all"}),
    ("oversight_coverage", {"field": "code_review"}),
    ("oversight_hot_spots", {}),
    ("oversight_rule_status", {}),
]
BAD_PARAMS: list[tuple[str, Any, str]] = [
    ("oversight_top_issues", {"metric": "velocity"}, "metric"),
    ("oversight_top_issues", {"metric": "cycle_time", "n": 99}, "n"),
    ("oversight_count_issues", {"period": "1y"}, "period"),
    ("oversight_count_issues", {"unknown": 1}, "unknown parameter unknown"),
    ("oversight_issue_timeline", {}, "issue_key is required"),
    ("oversight_issue_timeline", {"issue_key": "demo"}, "issue_key"),
    ("oversight_list_findings", {"rule_id": "R-EST-01; DROP TABLE findings"}, "rule_id"),
    ("oversight_hot_spots", "garbage", "parameters"),
    ("oversight_rule_status", 7, "parameters"),
]


# --- the seeded demo and the server -------------------------------------------------------------


def _finding(finding_id: str, rule_id: str, issue_key: str, severity: str, *, resolved: bool = False) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "rule_id": rule_id,
        "rule_version": RULES_VERSION,
        "issue_key": issue_key,
        "project_key": PROJECT,
        "kind": "warning" if rule_id.startswith("W-") else "rule",
        "severity": severity,
        "evidence_key": f"{rule_id}-{issue_key}",
        "message": "hand-written for the MCP tests",
        "evidence_json": {},
        "first_seen_at": to_iso(NOW),
        "last_seen_at": to_iso(NOW),
        "resolved_at": to_iso(NOW) if resolved else None,
        "resolved_why": "cleared" if resolved else None,
        "run_id": "rules-test",
    }


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Settings for the demo project, every path in a module-scoped temp folder, configs from the fixtures."""
    root = tmp_path_factory.mktemp("mcp")
    return Settings(
        jira_base_url="https://fake.atlassian.net",
        jira_email="dev@example.com",
        jira_api_token="test-token",
        jira_projects=PROJECT,
        db_path=root / "data" / "oversight.db",
        config_dir=CONFIG_FIXTURES,
        reports_dir=root / "reports",
        log_format="console",
    )


@pytest.fixture(scope="module")
def process() -> ProcessConfig:
    return ProcessConfig.load(CONFIG_FIXTURES / "process.yaml")


@pytest.fixture(scope="module")
def rules() -> RulesConfig:
    return RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")


@pytest.fixture(scope="module")
def demo_db(settings: Settings, process: ProcessConfig) -> Path:
    """The demo seeded and derived at NOW, with findings on the children of two epics."""
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=NOW, issues=ISSUES)
        run_derive(store, process, projects=[PROJECT], now=lambda: NOW)
        epics = [row["issue_key"] for row in store.query("SELECT issue_key FROM issues WHERE issue_type = 'Epic' ORDER BY issue_key")]
        assert len(epics) >= 2
        children = [
            row["issue_key"]
            for row in store.query("SELECT issue_key FROM issues WHERE epic_key = ? ORDER BY issue_key LIMIT 3", (epics[0],))
        ]
        other = store.query_one("SELECT issue_key FROM issues WHERE epic_key = ? ORDER BY issue_key", (epics[1],))
        assert len(children) == 3 and other is not None
        with store.transaction():
            store.upsert_many(
                "findings",
                [
                    _finding("f-1", "R-FLOW-02", children[0], "breach"),
                    _finding("f-2", "R-FLOW-02", children[1], "breach"),
                    _finding("f-3", "W-AGE-01", children[2], "warning"),
                    _finding("f-4", "R-EST-01", other["issue_key"], "breach"),
                    _finding("f-5", "W-EFF-01", children[0], "warning", resolved=True),
                ],
            )
    return settings.db_file


@pytest.fixture(scope="module")
def server(settings: Settings, process: ProcessConfig, rules: RulesConfig, demo_db: Path) -> FastMCP:
    return build_server(settings, process, rules, db_path=demo_db)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server reads the clock per call; pin it to the seeding `now` so windows match the reference queries."""
    monkeypatch.setattr(mcp_server, "utcnow", lambda: NOW)


@pytest.fixture
def reference(process: ProcessConfig, rules: RulesConfig, demo_db: Path) -> Iterator[Callable[[str, dict[str, Any]], dict[str, Any]]]:
    """run_query on a read-only connection with the same context the server builds, as a plain dict."""
    conn = open_readonly(demo_db)
    ctx = CatalogContext(process=process, rules=rules, project=PROJECT, now=NOW, row_limit=200)

    def run(name: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = run_query(conn, name, params, ctx, ref="F1").to_dict()
        payload.pop("ref")
        return payload

    yield run
    conn.close()


# --- driving the server --------------------------------------------------------------------------


def list_tools(server: FastMCP) -> list[Tool]:
    async def go() -> list[Tool]:
        async with create_connected_server_and_client_session(server) as session:
            return (await session.list_tools()).tools

    return anyio.run(go)


def call(server: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
    """One tools/call round trip through a client session over the in-memory transport."""

    async def go() -> CallToolResult:
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool(name, arguments)

    return anyio.run(go)


def call_many(server: FastMCP, calls: list[tuple[str, dict[str, Any] | None]]) -> list[CallToolResult]:
    """Several calls in one session, in order."""

    async def go() -> list[CallToolResult]:
        async with create_connected_server_and_client_session(server) as session:
            return [await session.call_tool(name, arguments) for name, arguments in calls]

    return anyio.run(go)


def payload(result: CallToolResult) -> dict[str, Any]:
    """The JSON object a tool returned; the result is a normal one (never isError) with one text block."""
    assert result.isError is False
    assert len(result.content) == 1 and isinstance(result.content[0], TextContent)
    assert result.structuredContent is None  # structured_output=False: the JSON text is the whole result
    return json.loads(result.content[0].text)


def _counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    finally:
        conn.close()


# --- listing ---------------------------------------------------------------------------------------


def test_every_catalogue_tool_is_listed_read_only(server: FastMCP) -> None:
    tools = {tool.name: tool for tool in list_tools(server)}
    assert list(tools) == [*CATALOG, CATALOG_TOOL]
    assert server.name == SERVER_NAME
    for name, spec in CATALOG.items():
        tool = tools[name]
        assert tool.description == spec.description
        assert tool.title == tool.annotations.title and tool.title
        assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == (True, False)
        assert (tool.annotations.idempotentHint, tool.annotations.openWorldHint) == (True, False)
        # The schema a client sees for `params` is the spec's own pydantic model.
        schema = tool.inputSchema["properties"]["params"]
        expected = spec.params_model.model_json_schema()
        assert {key: value for key, value in schema.items() if key != "default"} == expected
        assert schema.get("default") is None and "params" not in tool.inputSchema.get("required", [])
        assert tool.outputSchema is None
    catalog = tools[CATALOG_TOOL]
    assert catalog.annotations.readOnlyHint is True and catalog.annotations.destructiveHint is False
    assert catalog.inputSchema.get("properties", {}) == {}
    assert tools["oversight_time_in_status"].title == "Time in status"
    assert "team and process level" in (server.instructions or "")


# --- the same rows as the catalogue ----------------------------------------------------------------


@pytest.mark.parametrize(("name", "params"), CALLS, ids=[f"{n}:{json.dumps(p)}" for n, p in CALLS])
def test_each_tool_returns_the_same_rows_as_run_query(
    server: FastMCP, reference: Callable[[str, dict[str, Any]], dict[str, Any]], name: str, params: dict[str, Any]
) -> None:
    expected = reference(name, params)
    got = payload(call(server, name, {"params": params}))
    assert got == expected
    assert set(got) == {"tool", "params", "columns", "rows", "truncated", "as_of", "note"}
    assert got["tool"] == name and got["as_of"] == to_iso(NOW)
    if name in ("oversight_count_issues", "oversight_coverage", "oversight_rule_status", "oversight_hot_spots"):
        assert got["rows"], (name, params)  # the demo has data for these whatever the filter


def test_the_demo_findings_and_timeline_come_through(server: FastMCP, demo_db: Path) -> None:
    findings = payload(call(server, "oversight_list_findings", {"params": {"state": "all"}}))
    assert len(findings["rows"]) == 5
    assert sorted(row["rule_id"] for row in findings["rows"]) == ["R-EST-01", "R-FLOW-02", "R-FLOW-02", "W-AGE-01", "W-EFF-01"]
    assert all(row["rule_title"] for row in findings["rows"])
    hot = payload(call(server, "oversight_hot_spots", {"params": {"n": 1}}))
    assert hot["rows"][0]["open_breaches"] == 2 and hot["rows"][0]["open_warnings"] == 1 and hot["rows"][0]["issues"] == 3
    key = findings["rows"][0]["issue_key"]
    timeline = payload(call(server, "oversight_issue_timeline", {"params": {"issue_key": key.lower()}}))
    assert timeline["params"] == {"issue_key": key} and timeline["rows"] and timeline["rows"][0]["seq"] == 0
    assert f"Findings for {key}" in timeline["note"]
    status = payload(call(server, "oversight_rule_status", {}))
    by_rule = {row["rule_id"]: row for row in status["rows"]}
    assert by_rule["R-FLOW-02"]["open_findings"] == 2 and by_rule["W-EFF-01"]["open_findings"] == 0
    assert by_rule["R-FLOW-01"]["measurable"] is False and "code_review" in by_rule["R-FLOW-01"]["missing"]


def test_params_may_be_omitted_or_sent_as_a_json_string(server: FastMCP) -> None:
    plain, as_string, omitted, empty = call_many(
        server,
        [
            ("oversight_count_issues", {"params": {"status": "Done"}}),
            ("oversight_count_issues", {"params": json.dumps({"status": "Done"})}),  # Claude Desktop's habit
            ("oversight_count_issues", None),
            ("oversight_count_issues", {"params": {}}),
        ],
    )
    assert payload(plain) == payload(as_string)
    assert payload(plain)["params"] == {"status": "Done", "period": "all"}
    assert payload(omitted) == payload(empty) and payload(omitted)["rows"][0]["count"] == ISSUES


def test_the_project_defaults_to_settings_and_a_parameter_overrides_it(server: FastMCP) -> None:
    default, ail = call_many(
        server,
        [("oversight_count_issues", {"params": {}}), ("oversight_count_issues", {"params": {"project": "ail"}})],
    )
    assert "project" not in payload(default)["params"] and payload(default)["rows"][0]["count"] == ISSUES
    assert payload(ail)["params"]["project"] == "AIL" and payload(ail)["rows"][0]["count"] == 0  # configured, never synced


def test_fastmcp_call_tool_gives_the_same_text_as_the_session(server: FastMCP) -> None:
    direct = anyio.run(server.call_tool, "oversight_hot_spots", {"params": {"n": 2}})
    over_session = call(server, "oversight_hot_spots", {"params": {"n": 2}})
    assert [block.text for block in direct] == [over_session.content[0].text]


# --- errors are data, never exceptions ------------------------------------------------------------


@pytest.mark.parametrize(("name", "params", "mentions"), BAD_PARAMS, ids=[f"{n}:{p!r}" for n, p, _ in BAD_PARAMS])
def test_invalid_params_give_a_json_error(server: FastMCP, name: str, params: Any, mentions: str) -> None:
    got = payload(call(server, name, {"params": params}))
    assert set(got) == {"tool", "error"} and got["tool"] == name
    assert got["error"].startswith("invalid parameters: ") and mentions in got["error"]


def test_an_unconfigured_project_is_refused_before_any_query(server: FastMCP) -> None:
    got = payload(call(server, "oversight_coverage", {"params": {"project": "XYZ", "field": "estimate"}}))
    assert "XYZ" in got["error"] and "AIL, DEMO" in got["error"] and "process.yaml" in got["error"]


def test_a_missing_database_gives_a_json_error(settings: Settings, process: ProcessConfig, rules: RulesConfig, tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "oversight.db"
    server = build_server(settings, process, rules, db_path=missing)
    results = call_many(server, [(name, {"params": params}) for name, params in CALLS[:3]] + [("oversight_issue_timeline", {"params": {"issue_key": "DEMO-1"}})])
    for result in results:
        got = payload(result)
        assert "error" in got and str(missing) in got["error"] and "python -m oversight demo" in got["error"]
    assert not missing.exists()  # nothing created the file
    # The vocabulary needs no database.
    assert payload(call(server, CATALOG_TOOL))["default_project"] == PROJECT


def test_a_file_that_is_not_a_database_gives_a_json_error(settings: Settings, process: ProcessConfig, rules: RulesConfig, tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite file, just bytes " * 40)
    server = build_server(settings, process, rules, db_path=garbage)
    got = payload(call(server, "oversight_count_issues", {"params": {}}))
    assert "error" in got and "could not be" in got["error"]


def test_an_unexpected_failure_is_reported_not_raised(server: FastMCP, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path: Path) -> Any:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(mcp_server, "open_readonly", boom)
    with capture_logs() as captured:
        got = payload(call(server, "oversight_rule_status", {}))
    assert got["tool"] == "oversight_rule_status" and "RuntimeError: disk on fire" in got["error"]
    assert any(entry["event"] == "mcp_tool_failed" and entry["tool"] == "oversight_rule_status" for entry in captured)


# --- the vocabulary, read-only connections, nothing written --------------------------------------


def test_oversight_catalog_lists_the_vocabulary(server: FastMCP, rules: RulesConfig) -> None:
    got = payload(call(server, CATALOG_TOOL))
    assert got["server"] == SERVER_NAME and got["default_project"] == PROJECT and got["projects"] == ["AIL", "DEMO"]
    assert got["tools"] == catalog_schema() and [tool["name"] for tool in got["tools"]] == list(CATALOG)
    assert [rule["id"] for rule in got["rules"]] == [rule.id for rule in rules.enabled_rules]
    assert got["rules"][0] == {"id": "R-EST-01", "title": "Work started without an estimate", "kind": "rule", "severity": "breach"}
    assert got["row_limit"] == 200


def test_each_query_runs_with_the_rules_of_its_board_as_its_admins_set_them(
    settings: Settings, process: ProcessConfig, rules: RulesConfig, demo_db: Path, tmp_path: Path
) -> None:
    """JO-15: a rule turned off on the board is not listed, and a rule's severity and the effort threshold are the
    board's (a copy of the demo, so the other tests' board keeps rules.yaml's settings)."""
    board = tmp_path / "oversight.db"
    for suffix in ("", "-wal"):
        if Path(f"{demo_db}{suffix}").exists():
            shutil.copyfile(f"{demo_db}{suffix}", f"{board}{suffix}")
    with Store.open(board) as store:
        changes = [Change("R-EFF-01", params={"threshold": 2.5}), Change("R-HYG-01", severity="warning"),
                   Change("W-AGE-01", enabled=False)]
        save_board_settings(store, rules, PROJECT, revision=0, changes=changes, now=NOW, viewer_hash="local",
                            viewer_name="Local (sign-in off)")
    served = build_server(settings, process, rules, db_path=board)

    status, effort = call_many(served, [
        ("oversight_rule_status", None),
        ("oversight_effort_vs_estimate", {"params": {"group_by": "issue_type", "period": "all"}}),
    ])

    listed = {row["rule_id"]: row for row in payload(status)["rows"]}
    assert "W-AGE-01" not in listed and listed["R-HYG-01"]["severity"] == "warning"
    assert listed["R-EFF-01"]["version"] == RULES_VERSION  # the catalogue's version, whatever the board changed
    thresholds = {row["threshold"] for row in payload(effort)["rows"]}
    assert thresholds == {2.5}
    # The catalogue tool lists rules.yaml's rules, needing no database, and each board's as its admins set them.
    catalogue = payload(call(served, CATALOG_TOOL))
    assert "W-AGE-01" in {rule["id"] for rule in catalogue["rules"]}
    demo = {rule["id"]: rule for rule in catalogue["board_rules"][PROJECT]}
    assert (demo["W-AGE-01"]["enabled"], demo["R-HYG-01"]["severity"]) == (False, "warning")


def test_the_catalogue_lists_each_boards_own_rules_its_custom_rules_among_them(
    settings: Settings, process: ProcessConfig, rules: RulesConfig, demo_db: Path, tmp_path: Path
) -> None:
    """JO-20: a rule an admin added to DEMO is in DEMO's rules, never AIL's; without a snapshot every board's rules
    are rules.yaml's."""
    board = tmp_path / "oversight.db"
    for suffix in ("", "-wal"):
        if Path(f"{demo_db}{suffix}").exists():
            shutil.copyfile(f"{demo_db}{suffix}", f"{board}{suffix}")
    with Store.open(board) as store:
        save_board_settings(store, rules, PROJECT, revision=0, now=NOW, viewer_hash="local",
                            viewer_name="Local (sign-in off)", process=process, changes=[Change.adding({
                                "title": "Open over 60 days", "check": "open_longer_than", "params": {"days": 60},
                                "applies_to": {"status_category": ["new", "indeterminate"], "issue_type": None},
                                "severity": "warning"})])
    got = payload(call(build_server(settings, process, rules, db_path=board), CATALOG_TOOL))
    assert set(got["board_rules"]) == {"AIL", "DEMO"}
    assert got["board_rules"][PROJECT][-1] == {"id": "W-CUS-01", "title": "Open over 60 days", "kind": "warning",
                                               "severity": "warning", "enabled": True, "custom": True}
    assert "W-CUS-01" not in {rule["id"] for rule in got["board_rules"]["AIL"]}
    assert [rule["id"] for rule in got["board_rules"]["AIL"]] == [rule.id for rule in rules.rules]
    missing = payload(call(build_server(settings, process, rules, db_path=tmp_path / "none" / "oversight.db"),
                           CATALOG_TOOL))
    assert {project: [rule["id"] for rule in listed] for project, listed in missing["board_rules"].items()} == {
        project: [rule.id for rule in rules.rules] for project in ("AIL", "DEMO")}
    assert not any(rule["custom"] for listed in missing["board_rules"].values() for rule in listed)


def test_every_call_opens_one_read_only_connection_and_writes_nothing(
    server: FastMCP, demo_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[sqlite3.Connection] = []
    real = mcp_server.open_readonly

    def spy(path: Path) -> sqlite3.Connection:
        assert Path(path) == demo_db
        conn = real(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(mcp_server, "open_readonly", spy)
    before = _counts(demo_db)
    results = call_many(server, [(name, {"params": params}) for name, params in CALLS])
    assert all(result.isError is False for result in results)
    assert len(opened) == len(CALLS)
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):  # closed after the call
            conn.execute("SELECT 1")
    assert _counts(demo_db) == before
    # A fresh read-only connection is really read-only (what every call used).
    conn = real(demo_db)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO runs (run_id, kind) VALUES ('x', 'x')")
    finally:
        conn.close()


# --- main() and the CLI -----------------------------------------------------------------------------


def test_main_builds_the_server_from_settings_and_serves_stdio(settings: Settings, demo_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[FastMCP, str]] = []

    def fake_run(self: FastMCP, transport: str = "stdio", mount_path: str | None = None) -> None:
        served.append((self, transport))

    monkeypatch.setattr(FastMCP, "run", fake_run)
    assert main(settings) == EXIT_OK
    (built, transport) = served[0]
    assert transport == "stdio" and built.name == SERVER_NAME
    assert [tool.name for tool in list_tools(built)] == [*CATALOG, CATALOG_TOOL]
    assert payload(call(built, "oversight_count_issues", {"params": {}}))["rows"][0]["count"] == ISSUES  # settings.db_file

    # The CLI command is wired to it.
    served.clear()
    assert cli_main(["mcp"], settings=settings) == EXIT_OK
    assert served and served[0][1] == "stdio"


def test_main_fails_early_on_bad_config(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FastMCP, "run", lambda self, *a, **k: pytest.fail("the server must not start without its config"))
    broken = settings.model_copy(update={"config_dir": tmp_path / "no-config"})
    with pytest.raises(ConfigError):
        main(broken)
    assert cli_main(["mcp"], settings=broken) == EXIT_CONFIG
    empty = settings.model_copy(update={"jira_projects": []})
    with pytest.raises(ConfigError, match="JIRA_PROJECTS"):
        build_server(empty, ProcessConfig.load(CONFIG_FIXTURES / "process.yaml"), RulesConfig.load(CONFIG_FIXTURES / "rules.yaml"))


def test_main_warns_when_the_database_is_missing(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FastMCP, "run", lambda self, *a, **k: None)
    absent = settings.model_copy(update={"db_path": tmp_path / "absent.db"})
    with capture_logs() as captured:
        assert main(absent) == EXIT_OK
    events = {entry["event"]: entry for entry in captured}
    assert events["mcp_database_missing"]["path"] == str(absent.db_file)
    assert events["mcp_server_starting"]["transport"] == "stdio" and events["mcp_server_starting"]["tools"] == len(CATALOG) + 1
    assert not absent.db_file.exists()  # the server never creates a database
