"""Every Jira board (JO-17), oversight/boards.py: discovery (a read-only project search into the boards table), the
boards the service serves and keeps in sync, and the process mapping of a board process.yaml leaves out; and the
`discover` command, and sync, derive and rules on a board first synced from the dashboard.

FakeJira answers the project search (tests/fakes/fake_jira.py): nothing here talks to a real Jira site.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from oversight.boards import (
    AUTOMATIC_HOURS_PER_POINT,
    Board,
    automatic_project,
    board_process,
    category_stages,
    discover_boards,
    kept_boards,
    last_discovery,
    mapped_process,
    served_boards,
)
from oversight.cli import EXIT_CONFIG, EXIT_OK, main
from oversight.config import ProcessConfig
from oversight.errors import JiraUnavailable, ReadOnlyViolation
from oversight.jira.client import ALLOWED, JiraClient, check_allowed, route_of
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import UTC, to_iso, utcnow
from tests.conftest import CONFIG_FIXTURES
from tests.fakes.builders import ail_and_ops
from tests.fakes.fake_jira import PROJECT_SEARCH, FakeJira

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def site() -> FakeJira:
    """Five projects: three live (AIL, OPS, SD), one archived (OLD), one the account cannot see (SECRET)."""
    fake = FakeJira()
    fake.add_project("AIL", "AILED")
    fake.add_project("OPS", "Operations", project_type="business")
    fake.add_project("SD", "Service desk", project_type="service_desk")
    fake.add_project("OLD", "Old work")
    fake.add_project("SECRET", "Board room")
    fake.archived_projects.add("OLD")
    fake.hidden_projects.add("SECRET")
    return fake


def rows(store: Store) -> dict[str, tuple[object, ...]]:
    return {row["project_key"]: (row["name"], row["project_type"], row["discovered_at"], row["last_seen_at"],
                                 row["disappeared_at"])
            for row in store.query("SELECT * FROM boards")}


def at(minutes: int) -> Callable[[], datetime]:
    return lambda: NOW + timedelta(minutes=minutes)


def settings_of(tmp_path: Path, projects: str = "AIL", discovery: str = "on") -> Settings:
    return Settings(_env_file=None, jira_projects=projects, board_discovery=discovery, db_path=tmp_path / "o.db",
                    config_dir=CONFIG_FIXTURES, reports_dir=tmp_path / "reports")


# --- discovery -------------------------------------------------------------------------------------------------------


def test_discovery_lists_every_live_board_the_account_can_browse_page_by_page_and_only_reads(store: Store) -> None:
    fake = site()
    fake.project_search_cap = 2  # two pages for the three live projects
    with fake.client() as client:
        result = discover_boards(client, store, now=at(0))

    assert (result.listed, result.added, result.back, result.disappeared, result.skipped) == (3, 3, 0, 0, 0)
    seen = to_iso(NOW)
    assert rows(store) == {
        "AIL": ("AILED", "software", seen, seen, None),
        "OPS": ("Operations", "business", seen, seen, None),
        "SD": ("Service desk", "service_desk", seen, seen, None),
    }
    calls = fake.calls(PROJECT_SEARCH)
    assert [request.url.params["startAt"] for request in calls] == ["0", "2"]
    assert all((request.url.params["action"], request.url.params["status"]) == ("browse", "live") for request in calls)
    assert all(request.method == "GET" and (request.method, route_of(request.url.path)) in ALLOWED
               for request in fake.requests)
    run = store.query_one("SELECT kind, status, stats_json FROM runs WHERE run_id = ?", (result.run_id,))
    assert (run["kind"], run["status"]) == ("discover", "succeeded") and '"listed": 3' in run["stats_json"]
    assert last_discovery(store.conn) == seen
    assert result.render().splitlines()[0] == f"discover run {result.run_id}: 3 boards this account can browse"


def test_the_project_search_is_an_allowed_read_and_never_mistaken_for_a_project_key() -> None:
    assert check_allowed("GET", "/rest/api/3/project/search") == "/rest/api/3/project/search"
    assert check_allowed("GET", "/rest/api/3/project/SEARCH") == "/rest/api/3/project/{key}"
    with pytest.raises(ReadOnlyViolation):
        check_allowed("POST", "/rest/api/3/project/search")


def test_a_board_gone_from_jira_is_marked_once_and_one_that_comes_back_is_served_again(store: Store) -> None:
    fake = site()
    with fake.client() as client:
        discover_boards(client, store, now=at(0))
        fake.archived_projects.add("OPS")
        fake.add_project("NEW", "New board")
        second = discover_boards(client, store, now=at(30))
        third = discover_boards(client, store, now=at(60))
        fake.archived_projects.discard("OPS")
        fourth = discover_boards(client, store, now=at(90))

    assert (second.added, second.disappeared) == (1, 1)
    assert (third.added, third.disappeared) == (0, 0)  # marked once: its disappearance keeps its first moment
    assert (fourth.back, fourth.disappeared) == (1, 0)
    found = rows(store)
    assert found["OPS"] == ("Operations", "business", to_iso(NOW), to_iso(NOW + timedelta(minutes=90)), None)
    assert found["AIL"][2:4] == (to_iso(NOW), to_iso(NOW + timedelta(minutes=90)))  # first discovery kept
    assert found["NEW"][2] == to_iso(NOW + timedelta(minutes=30))


def test_a_search_that_lists_nothing_leaves_the_stored_list_as_it_was(store: Store) -> None:
    fake = site()
    with fake.client() as client:
        discover_boards(client, store, now=at(0))
        before = rows(store)
        fake.hidden_projects.update(fake.project_ids)
        with capture_logs() as events:
            result = discover_boards(client, store, now=at(30))
    assert result.empty and rows(store) == before
    assert "board_discovery_empty" in [event["event"] for event in events]
    assert "the stored list stays" in result.render()


def test_a_failed_discovery_is_recorded_and_changes_nothing(store: Store) -> None:
    fake = site()
    with fake.client() as client:
        discover_boards(client, store, now=at(0))
        before = rows(store)
        fake.fail(PROJECT_SEARCH, status=503)
        with pytest.raises(JiraUnavailable):
            discover_boards(client, store, now=at(30))
    assert rows(store) == before
    failed = store.query_one("SELECT status, stats_json FROM runs WHERE kind = 'discover' ORDER BY started_at DESC")
    assert failed["status"] == "failed" and '"error_type": "JiraUnavailable"' in failed["stats_json"]
    assert last_discovery(store.conn) == to_iso(NOW)  # the list's age is the last discovery that worked


def test_an_entry_without_a_usable_key_is_skipped_and_counted(store: Store) -> None:
    fake = site()
    fake.add_project("lower-case", "Not a key")
    with fake.client() as client:
        result = discover_boards(client, store, now=at(0))
    assert result.skipped == 1 and "lower-case" not in rows(store)
    assert "skipped: 1 without a usable key" in result.render()


# --- served and kept boards ------------------------------------------------------------------------------------------


def discovered(store: Store, *boards: tuple[str, str]) -> None:
    for key, name in boards:
        store.upsert("boards", {"project_key": key, "name": name, "project_type": "software",
                                "discovered_at": to_iso(NOW), "last_seen_at": to_iso(NOW)})


def test_the_served_boards_are_jira_projects_first_then_every_discovered_board_by_name(
    store: Store, tmp_path: Path
) -> None:
    discovered(store, ("ZED", "alpha board"), ("AIL", "AILED"), ("OPS", "Operations"), ("GONE", "Gone"))
    store.conn.execute("UPDATE boards SET disappeared_at = ? WHERE project_key = 'GONE'", (to_iso(NOW),))
    store.set_cursor("OPS", last_synced_at=to_iso(NOW), run_id="r1", account_timezone=None)

    served = served_boards(store.conn, settings_of(tmp_path, "DEMO,AIL"))
    assert [board.key for board in served] == ["DEMO", "AIL", "ZED", "OPS"]  # "alpha board" before "Operations"
    assert served[1] == Board("AIL", "AILED", "software", None)
    assert served[0] == Board("DEMO", None, None, None)  # not listed by Jira: no name, no type
    assert served[3].synced and not served[2].synced
    # BOARD_DISCOVERY=off: JIRA_PROJECTS alone, as before JO-17. No snapshot: the same, none synced.
    assert [board.key for board in served_boards(store.conn, settings_of(tmp_path, "DEMO,AIL", "off"))] == ["DEMO",
                                                                                                           "AIL"]
    assert served_boards(None, settings_of(tmp_path, "DEMO,AIL")) == [Board("DEMO", None, None, None),
                                                                      Board("AIL", None, None, None)]


def test_the_cycle_keeps_jira_projects_and_every_board_synced_before(store: Store, tmp_path: Path) -> None:
    discovered(store, ("OPS", "Operations"), ("SD", "Service desk"), ("GONE", "Gone"))
    for key in ("OPS", "GONE"):
        store.set_cursor(key, last_synced_at=to_iso(NOW), run_id="r1", account_timezone=None)
    store.conn.execute("UPDATE boards SET disappeared_at = ? WHERE project_key = 'GONE'", (to_iso(NOW),))
    # AIL never synced: still kept, it is in JIRA_PROJECTS. SD never synced: not kept. GONE left Jira: not kept.
    assert kept_boards(store.conn, settings_of(tmp_path, "AIL")) == ["AIL", "OPS"]
    assert kept_boards(store.conn, settings_of(tmp_path, "AIL", "off")) == ["AIL"]


def test_an_older_snapshot_without_the_boards_table_serves_jira_projects(tmp_path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sync_state (scope TEXT, last_synced_at TEXT)")
    assert [board.key for board in served_boards(conn, settings_of(tmp_path, "AIL"))] == ["AIL"]


# --- the automatic process mapping -----------------------------------------------------------------------------------


def statuses(store: Store, project: str, *found: tuple[str, str, str | None]) -> None:
    store.replace_rows("statuses", {"project_key": project}, [
        {"project_key": project, "status_id": status_id, "name": name, "category": category}
        for status_id, name, category in found
    ])


def issue(store: Store, key: str, *, status: str = "To Do", category: str = "new", points: float | None = None,
          estimate_s: int | None = None, issue_type: str = "Task") -> None:
    store.upsert("issues", {"issue_key": key, "issue_id": key.rsplit("-", 1)[1] + key[0], "project_key":
                            key.rsplit("-", 1)[0], "issue_type": issue_type, "status": status,
                            "status_category": category, "story_points": points, "original_estimate_s": estimate_s})


def test_a_board_process_yaml_leaves_out_is_mapped_from_jiras_status_categories(
    store: Store, process: ProcessConfig
) -> None:
    statuses(store, "OPS", ("10", "Open", "new"), ("11", "Doing", "indeterminate"), ("12", "Shipped", "done"),
             ("13", "Parked", "undefined"), ("14", "open", "done"))  # "open" is "Open" ignoring case: the first wins
    issue(store, "OPS-1", status="Waiting for customer", category="indeterminate")  # not in the workflow's list
    issue(store, "OPS-2", status="Open", points=3)

    mapping = automatic_project(store.conn, process, "OPS", name="Operations")

    assert mapping.automatic and mapping.name == "Operations"
    assert mapping.status_to_stage == {"Open": "backlog", "Doing": "development", "Shipped": "done",
                                       "Waiting for customer": "development"}
    assert mapping.stage_of("Parked") is None  # no category: unmapped
    assert mapping.evidence == dict.fromkeys(["code_review", "dev_signoff", "testing", "test_review", "test_signoff"])
    assert (mapping.estimate.basis, mapping.estimate.hours_per_point) == ("points", AUTOMATIC_HOURS_PER_POINT)
    assert category_stages(process) == {"new": "backlog", "indeterminate": "development", "done": "done"}


def test_the_estimate_basis_is_story_points_when_the_boards_issues_carry_them_else_hours(
    store: Store, process: ProcessConfig
) -> None:
    issue(store, "HRS-1", estimate_s=3600)
    issue(store, "HRS-2", estimate_s=7200)
    issue(store, "HRS-3", points=2)
    assert automatic_project(store.conn, process, "HRS").estimate.basis == "hours"  # more carry an estimate
    issue(store, "PTS-1", points=1)
    issue(store, "PTS-2", estimate_s=3600)
    assert automatic_project(store.conn, process, "PTS").estimate.basis == "points"  # as many: points
    issue(store, "NIL-1")
    assert automatic_project(store.conn, process, "NIL").estimate.field == "original_estimate"  # none: hours
    bare = automatic_project(None, process, "NEW")  # before its first sync: nothing to map
    assert (bare.status_to_stage, bare.estimate.basis, bare.name) == ({}, "hours", None)


def test_an_explicit_entry_always_wins_and_one_without_a_name_takes_jiras(store: Store, process: ProcessConfig) -> None:
    statuses(store, "AIL", ("1", "Anything", "done"))
    synced = to_iso(NOW)
    mapped = mapped_process(process, store.conn, [Board("AIL", "AILED", "software", synced),
                                                  Board("DEMO", "Jira's demo", "software", synced),
                                                  Board("OPS", "Operations", "business", None)])
    assert mapped.project("AIL").status_to_stage == process.project("AIL").status_to_stage  # process.yaml's
    assert mapped.project("AIL").name == "AILED" and not mapped.project("AIL").automatic
    assert mapped.project("DEMO").name == "Demo project"  # process.yaml names it
    assert mapped.project("OPS").automatic and mapped.project("OPS").name == "Operations"
    assert process.projects.keys() == {"AIL", "DEMO"}  # process.yaml as loaded is untouched
    with pytest.raises(Exception, match="not configured"):
        mapped.project("NOPE")  # a board the service does not serve is still no board


def test_every_served_board_is_mapped_for_every_command(store: Store, process: ProcessConfig, tmp_path: Path) -> None:
    discovered(store, ("OPS", "Operations"))
    mapped = board_process(process, store.conn, settings_of(tmp_path, "AIL,XYZ"))
    assert {"AIL", "XYZ", "OPS"} <= set(mapped.projects)
    assert mapped.project("XYZ").automatic and mapped.ordered_stages("XYZ") == []  # never synced: nothing mapped yet


# --- the commands ----------------------------------------------------------------------------------------------------


@pytest.fixture
def cli(settings: Settings, capsys: pytest.CaptureFixture[str]) -> Callable[..., tuple[int, str, str]]:
    fake = FakeJira()
    fake.clock = utcnow() - timedelta(days=6)
    ail_and_ops(fake, now=utcnow())

    def run(*argv: str, given: Settings = settings) -> tuple[int, str, str]:
        def factory(_settings: Settings) -> JiraClient:
            return JiraClient(**fake.client_kwargs(), sleep=lambda _: None)

        code = main(list(argv), settings=given, client_factory=factory)
        out, err = capsys.readouterr()
        return code, out, err

    run.fake = fake  # type: ignore[attr-defined]
    return run


def test_the_discover_command_stores_every_board_and_says_how_many(cli: Callable[..., tuple[int, str, str]],
                                                                   settings: Settings) -> None:
    code, out, _ = cli("discover")
    assert code == EXIT_OK and ": 2 boards this account can browse" in out.splitlines()[0]
    with Store.open(settings.db_file) as store:
        assert sorted(rows(store)) == ["AIL", "OPS"]


def test_the_discover_command_does_nothing_with_discovery_off_and_needs_the_jira_settings(
    cli: Callable[..., tuple[int, str, str]], settings: Settings
) -> None:
    code, out, _ = cli("discover", given=settings.model_copy(update={"board_discovery": "off"}))
    assert code == EXIT_OK and "BOARD_DISCOVERY=off" in out
    assert cli.fake.requests == []  # type: ignore[attr-defined]
    code, _, err = cli("discover", given=settings.model_copy(update={"jira_api_token": SecretStr("")}))
    assert code == EXIT_CONFIG and "JIRA_API_TOKEN" in err


def test_a_board_synced_from_the_dashboard_is_derived_and_checked_then_kept_by_the_cycle(
    cli: Callable[..., tuple[int, str, str]], settings: Settings
) -> None:
    """OPS is not in process.yaml nor JIRA_PROJECTS: discovered, synced once by itself (as "Sync now" runs it), derived
    and checked with a mapping worked out from its status categories, then synced again by every plain sync."""
    assert cli("discover")[0] == EXIT_OK
    code, out, _ = cli("sync", "--full", "--project", "OPS")
    assert code == EXIT_OK and "OPS full" in out
    assert cli("derive", "--project", "OPS")[0] == EXIT_OK
    code, out, _ = cli("rules", "--project", "OPS")
    assert code == EXIT_OK
    with Store.open(settings.db_file) as store:
        stages = {row[0] for row in store.query(
            "SELECT DISTINCT stage FROM status_intervals WHERE issue_key LIKE 'OPS-%'")}
        assert stages <= {"backlog", "development", "done"} and "development" in stages
        assert store.count("rule_status", {"project_key": "OPS"}) > 0
    fake: FakeJira = cli.fake  # type: ignore[attr-defined]
    fake.requests.clear()
    code, out, _ = cli("sync", "--incremental")  # no --project: the kept boards
    assert code == EXIT_OK and "AIL full, OPS incremental" in out
    assert {jql.split('"')[1] for jql in fake.jqls()} == {"AIL", "OPS"}


def test_a_sync_runs_stats_name_the_boards_it_synced(
    cli: Callable[..., tuple[int, str, str]], settings: Settings
) -> None:
    """So health reads each board's own syncs (JO-18): a failed sync of one board says nothing about another."""
    assert cli("sync", "--full", "--project", "OPS")[0] == EXIT_OK
    with Store.open(settings.db_file) as store:
        run = store.query_one("SELECT stats_json FROM runs WHERE kind = 'sync' ORDER BY started_at DESC")
    assert '"projects": ["OPS"]' in run["stats_json"]


def test_the_mcp_server_maps_every_served_board_when_it_starts(store: Store, settings: Settings) -> None:
    from oversight.mcp_server.server import served_process

    discovered(store, ("OPS", "Operations"))
    process = ProcessConfig.load(settings.process_file)
    unmapped = settings.model_copy(update={"jira_projects": ["AIL", "XYZ"], "db_path": store.path})
    mapped = served_process(process, unmapped)
    assert {"AIL", "XYZ", "OPS"} <= set(mapped.projects) and mapped.project("OPS").name == "Operations"
    no_snapshot = unmapped.model_copy(update={"db_path": settings.db_file.parent / "none.db"})
    assert "XYZ" in served_process(process, no_snapshot).projects


def test_derive_and_rules_still_refuse_a_board_the_service_does_not_serve(
    cli: Callable[..., tuple[int, str, str]]
) -> None:
    for command in ("derive", "rules"):
        code, _, err = cli(command, "--project", "XYZ")
        assert code == EXIT_CONFIG and "project 'XYZ' is not configured" in err
