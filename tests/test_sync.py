"""Full and incremental sync, end to end against an in-memory Jira.

A real JiraClient talks through httpx.MockTransport to FakeJira
(tests/fakes/fake_jira.py), which answers the allowlisted endpoints in the
shapes of tests/fixtures/jira/SHAPES.md (observed and documented forms: ISO and
epoch-ms changelog times, -0500 and -0600 offsets, token and startAt paging).
Every change in the fake is dated by its own clock, never the wall clock, and
retries never really sleep.

Expected rows come from the mapping functions applied to the fake's current
state: what a perfect sync would store. The counts are also spelled out by
hand from the plans in tests/fakes/builders.py, so an empty fake can't pass by
accident.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from oversight.config import FieldsConfig
from oversight.errors import JiraAuthError, JiraBudgetSpent, JiraError, JiraUnavailable
from oversight.ingest.sync import (
    MEMBERS_BOARD_BUDGET_S,
    MEMBERS_BUDGET_CAP_S,
    SITE_PROBE_BUDGET_S,
    SyncResult,
    members_budget_s,
    run_sync,
)
from oversight.jira.client import ALLOWED, JiraClient, route_of
from oversight.jira.mapping import (
    map_field_changes,
    map_issue,
    map_issue_links,
    map_remote_links,
    map_status_changes,
    map_statuses,
    map_worklog,
)
from oversight.store.db import SYNC_TABLES, TABLE_PKS, Store, WriteStats, encode
from oversight.timeutil import epoch_ms, to_iso

from tests.fakes.builders import (
    AIL_COUNTS,
    AIL_LINKS,
    AIL_PLAN,
    AIL_WITH_TIME,
    START,
    ail_and_ops,
    build,
)
from tests.fakes.fake_jira import (
    ASSIGNABLE,
    BULK,
    CHANGELOG,
    CHICAGO,
    DELETED_FEED,
    MYSELF,
    REMOTE_LINKS,
    SEARCH,
    SITE_USERS,
    TOKEN,
    UPDATED_FEED,
    WORKLOG_LIST,
    WORKLOGS,
    FakeJira,
)


@pytest.fixture
def fake() -> FakeJira:
    """AIL (34 issues, every type) and OPS (3 issues) on one site; the clock at START."""
    return ail_and_ops()


@pytest.fixture(autouse=True)
def logs() -> Iterator[list[dict[str, Any]]]:
    """Every structlog event of the test, as dicts; the configuration is restored after."""
    saved = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        with capture_logs() as captured:
            yield captured
    finally:
        structlog.configure(**saved)


# --- helpers ------------------------------------------------------------------------------


def sync(
    fake: FakeJira,
    store: Store,
    fields: FieldsConfig,
    mode: str = "full",
    projects: tuple[str, ...] = ("AIL",),
    **options: Any,
) -> SyncResult:
    """One run through a real client, with the fake's clock as `now`."""
    return run_sync(
        fake.client(), store, fields, projects=list(projects), mode=mode, now=lambda: fake.clock, **options
    )


def stored(store: Store) -> dict[str, list[dict[str, Any]]]:
    """Every synced table's rows, in primary-key order."""
    return {
        table: [dict(row) for row in store.query(f"SELECT * FROM {table} ORDER BY {', '.join(pk)}")]
        for table, pk in TABLE_PKS.items()
        if table in SYNC_TABLES
    }


def expected(
    fake: FakeJira, fields: FieldsConfig, projects: tuple[str, ...] = ("AIL",)
) -> dict[str, list[dict[str, Any]]]:
    """What a perfect sync stores for the fake's current state, via the mapping functions."""
    rows: dict[str, list[dict[str, Any]]] = {table: [] for table in SYNC_TABLES}
    for project in projects:
        rows["statuses"] += map_statuses(project, fake.project_statuses())
        for issue in fake.project_issues(project):
            payload = fake.payload(issue, fields.search_fields())
            histories = [fake.history(h, epoch=False) for h in issue.histories]
            rows["issues"].append(map_issue(payload, fields))
            rows["status_changes"] += map_status_changes(issue.key, histories)
            rows["field_changes"] += map_field_changes(issue.key, histories, fields)
            rows["worklogs"] += [map_worklog(issue.key, fake.worklog(w)) for w in fake.worklogs_of(issue.key)]
            rows["links"] += map_issue_links(payload) + map_remote_links(issue.key, issue.remote_links)
    return {table: _as_stored(table, table_rows) for table, table_rows in rows.items()}


def _as_stored(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mapped rows as the store returns them: values encoded, in primary-key order."""
    encoded = [{column: encode(value) for column, value in row.items()} for row in rows]
    return sorted(encoded, key=lambda row: tuple(row[column] for column in TABLE_PKS[table]))


def counts(store: Store) -> dict[str, int]:
    return {table: store.count(table) for table in SYNC_TABLES}


def stats_of(result: SyncResult) -> dict[str, dict[str, int]]:
    return {table: stats.as_dict() for table, stats in result.tables.items()}


def write_stats(**by_table: WriteStats) -> dict[str, dict[str, int]]:
    """Per-table counts, every table not named being all zero."""
    return {table: by_table.get(table, WriteStats()).as_dict() for table in SYNC_TABLES}


def run_row(store: Store, run_id: str) -> dict[str, Any]:
    row = dict(store.query_one("SELECT * FROM runs WHERE run_id = ?", (run_id,)))
    row["stats"] = json.loads(row.pop("stats_json"))
    return row


def last_run(store: Store) -> dict[str, Any]:
    row = store.query_one("SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1")
    return run_row(store, row["run_id"])


def is_later_page(request: httpx.Request) -> bool:
    return "nextPageToken" in json.loads(request.content)


def chicago_minute(at: datetime) -> str:
    return at.astimezone(ZoneInfo(CHICAGO)).strftime("%Y-%m-%d %H:%M")


# --- full sync ----------------------------------------------------------------------------


def test_a_full_sync_stores_exactly_what_jira_holds(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    # Several pages everywhere: 8 issues per search page, 7 histories per bulk page.
    fake.search_page_cap = 8
    fake.bulk_page_size = 7

    result = sync(fake, store, fields)

    assert counts(store) == AIL_COUNTS
    assert stored(store) == expected(fake, fields)
    assert stats_of(result) == write_stats(**{t: WriteStats(inserted=n) for t, n in AIL_COUNTS.items()})
    assert result.rows_written == sum(AIL_COUNTS.values()) == 137
    assert result.project_modes == {"AIL": "full"}
    assert (result.issues_seen, result.issues_deleted, result.worklogs_from_feed) == (34, 0, 0)
    assert result.warnings == []
    assert result.requests == len(fake.requests)

    # 34 issues -> 5 search pages; a later bulk page shows the bulk paging ran.
    assert len(fake.calls(SEARCH)) == 5
    assert any(is_later_page(r) for r in fake.calls(BULK))
    assert sorted(fake.keys_called(REMOTE_LINKS)) == sorted(i.key for i in fake.project_issues("AIL"))
    assert not fake.calls(CHANGELOG)
    assert not fake.calls(UPDATED_FEED) and not fake.calls(DELETED_FEED)
    assert fake.jqls()[0] == 'project = "AIL" ORDER BY updated ASC'
    search = json.loads(fake.calls(SEARCH)[0].content)
    assert search["fields"] == fields.search_fields()
    bulk = json.loads(fake.calls(BULK)[0].content)
    assert bulk["fieldIds"] == list(fields.tracked_changelog_fields())

    cursor = store.get_cursor("AIL")
    assert cursor.last_synced_at == to_iso(START) == result.started_at
    assert cursor.last_run_id == result.run_id
    assert cursor.account_timezone == CHICAGO
    run = run_row(store, result.run_id)
    assert (run["kind"], run["status"], run["started_at"]) == ("sync", "succeeded", to_iso(START))
    assert run["stats"]["tables"] == stats_of(result)
    assert run["stats"]["project_modes"] == {"AIL": "full"}
    assert run["stats"]["requests"] == result.requests


def test_synced_rows_keep_the_shapes_jira_sent(fake: FakeJira, store: Store, fields: FieldsConfig) -> None:
    sync(fake, store, fields)

    task = store.query_one("SELECT * FROM issues WHERE issue_key = 'AIL-4'")
    assert (task["issue_type"], task["parent_key"], task["epic_key"]) == ("Task", "AIL-1", "AIL-1")
    assert (task["status"], task["status_category"], task["story_points"]) == ("Done", "done", 2.0)
    assert task["sprint_ids"] == "[4400]" and task["time_spent_s"] == 7200
    subtask = store.query_one("SELECT * FROM issues WHERE issue_key = 'AIL-29'")
    assert (subtask["parent_key"], subtask["epic_key"]) == ("AIL-4", None)
    # Built on 28 Oct (CDT, -0500) and 1 Nov (CST, -0600): both stored in UTC.
    epic = store.query_one("SELECT created_at FROM issues WHERE issue_key = 'AIL-1'")
    assert epic["created_at"] == "2026-10-28T17:00:00.000Z"
    assert fake.payload(fake.issue("AIL-1"))["fields"]["created"] == "2026-10-28T12:00:00.000-0500"
    links = store.query("SELECT target, title FROM links WHERE issue_key = 'AIL-5' ORDER BY target")
    assert [tuple(r) for r in links] == [("AIL-4", "is blocked by; relates to")]
    assert store.count("links", {"issue_key": "AIL-9", "kind": "remote_link"}) == 2
    points = store.query(
        "SELECT from_value, to_value FROM field_changes"
        " WHERE issue_key = 'AIL-15' AND field = 'story_points' ORDER BY changed_at"
    )
    assert [tuple(r) for r in points] == [(None, "2"), ("2", "0.5")]


def test_a_second_full_sync_and_then_an_incremental_one_write_nothing(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)
    before = stored(store)

    again = sync(fake, store, fields)
    assert again.rows_written == 0
    assert stats_of(again) == write_stats(**{t: WriteStats(unchanged=n) for t, n in AIL_COUNTS.items()})
    assert stored(store) == before

    fake.clock += timedelta(minutes=30)
    incremental = sync(fake, store, fields, "incremental")
    assert incremental.rows_written == 0
    assert incremental.project_modes == {"AIL": "incremental"}
    assert incremental.issues_seen == 0
    assert stored(store) == before
    assert fake.calls(UPDATED_FEED) and fake.calls(DELETED_FEED)
    assert not fake.calls(WORKLOG_LIST)  # the feed listed nothing, so nothing to fetch
    assert store.get_cursor("AIL").last_synced_at == to_iso(START + timedelta(minutes=30))


# --- incremental sync ------------------------------------------------------------------------


def test_an_incremental_sync_writes_exactly_what_changed(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)
    before = stored(store)

    fake.clock = START + timedelta(minutes=30)
    fake.transition("AIL-15", "Done")  # a status change in a new history...
    fake.set_points("AIL-15", 3)  # ...and a points change
    fake.log_work("AIL-10", 3600)  # a new worklog
    gone_worklog = fake.worklogs_of("AIL-5")[0].id
    fake.delete_worklog(gone_worklog)  # a deleted worklog
    new_issue = fake.create("AIL", "Task", parent="AIL-2").key  # a new issue
    fake.clock += timedelta(minutes=2)
    before_requests = len(fake.requests)

    result = sync(fake, store, fields, "incremental")

    assert stats_of(result) == write_stats(
        issues=WriteStats(inserted=1, updated=3),
        status_changes=WriteStats(inserted=1, unchanged=5),
        field_changes=WriteStats(inserted=1, unchanged=10),
        worklogs=WriteStats(inserted=1, deleted=1),
        links=WriteStats(unchanged=2),
        statuses=WriteStats(unchanged=3),
    )
    assert result.rows_written == 8
    assert (result.issues_seen, result.worklogs_from_feed) == (4, 1)
    assert stored(store) == expected(fake, fields)
    assert result.requests == len(fake.requests) - before_requests

    # Exactly those rows changed; every other row is byte-for-byte what it was.
    after = stored(store)
    changed = {"AIL-5", "AIL-10", "AIL-15"}
    assert [r for r in after["issues"] if r["issue_key"] not in changed | {new_issue}] == [
        r for r in before["issues"] if r["issue_key"] not in changed
    ]
    new_status = [r for r in after["status_changes"] if r not in before["status_changes"]]
    assert [(r["issue_key"], r["from_status"], r["to_status"]) for r in new_status] == [
        ("AIL-15", "In Progress", "Done")
    ]
    assert all(r in after["status_changes"] for r in before["status_changes"])
    new_fields = [r for r in after["field_changes"] if r not in before["field_changes"]]
    assert [(r["issue_key"], r["field"], r["from_value"], r["to_value"]) for r in new_fields] == [
        ("AIL-15", "story_points", "0.5", "3")
    ]
    assert all(r in after["field_changes"] for r in before["field_changes"])
    new_worklog = str(max(fake.worklogs))
    assert {r["worklog_id"] for r in before["worklogs"]} ^ {r["worklog_id"] for r in after["worklogs"]} == {
        str(gone_worklog),
        new_worklog,
    }
    assert after["links"] == before["links"] and after["statuses"] == before["statuses"]

    # The JQL reads the cursor (START) less 5 minutes as Chicago wall-clock time.
    cutoff = chicago_minute(START - timedelta(minutes=5))
    assert cutoff == "2026-11-04 08:55"  # 14:55:30 UTC is 08:55:30 CST
    assert fake.jqls()[-1] == f'project = "AIL" AND updated >= "{cutoff}" ORDER BY updated ASC'
    feed_since = {r.url.params["since"] for r in fake.calls(UPDATED_FEED) + fake.calls(DELETED_FEED)}
    assert feed_since == {str(epoch_ms(START - timedelta(minutes=5)))}
    # Incremental runs take worklogs from the feed, not issue by issue: every
    # per-issue worklog call is still the first full sync's.
    assert len(fake.calls(WORKLOGS)) == len(AIL_WITH_TIME)


def test_a_custom_overlap_widens_the_window(fake: FakeJira, store: Store, fields: FieldsConfig) -> None:
    sync(fake, store, fields)
    fake.clock += timedelta(hours=1)
    sync(fake, store, fields, "incremental", overlap=timedelta(minutes=30))
    assert f'updated >= "{chicago_minute(START - timedelta(minutes=30))}"' in fake.jqls()[-1]
    assert fake.calls(UPDATED_FEED)[-1].url.params["since"] == str(epoch_ms(START - timedelta(minutes=30)))


def test_the_cursor_is_the_run_start_so_changes_during_a_run_reach_the_next_one(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    # Each request takes a minute, so the run lasts far longer than the overlap.
    fake.request_tick = timedelta(minutes=1)
    changed: list[str] = []

    def change_ail7(request: httpx.Request) -> None:
        # After the search has been read: this run can't see the change.
        if route_of(request.url.path) == REMOTE_LINKS and not changed:
            fake.transition("AIL-7", "In Progress")
            changed.append(str(fake.log_work("AIL-7")))

    fake.after_request.append(change_ail7)
    first = sync(fake, store, fields)

    assert changed
    assert first.started_at == to_iso(START)
    assert first.finished_at == to_iso(fake.clock)
    assert fake.clock - START >= first.requests * timedelta(minutes=1)  # over an hour
    assert store.get_cursor("AIL").last_synced_at == to_iso(START)
    assert store.query_one("SELECT status FROM issues WHERE issue_key = 'AIL-7'")[0] == "To Do"
    assert store.count("worklogs", {"issue_key": "AIL-7"}) == 0

    fake.after_request.clear()
    second = sync(fake, store, fields, "incremental")
    assert second.project_modes == {"AIL": "incremental"}
    assert store.query_one("SELECT status FROM issues WHERE issue_key = 'AIL-7'")[0] == "In Progress"
    worklogs = store.query("SELECT worklog_id FROM worklogs WHERE issue_key = 'AIL-7'")
    assert [r["worklog_id"] for r in worklogs] == changed
    assert stored(store) == expected(fake, fields)


def test_an_issue_that_changes_mid_search_is_read_again_and_counted_once(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    fake.search_page_cap = 8
    first_on_page_1 = min(fake.project_issues("AIL"), key=lambda i: (i.updated, int(i.id))).key

    def change_after_page_1(request: httpx.Request) -> None:
        if route_of(request.url.path) == SEARCH and not is_later_page(request):
            fake.assign(first_on_page_1, "acc-dev-2")  # now the newest: it comes back on the last page

    fake.after_request.append(change_after_page_1)
    result = sync(fake, store, fields)

    assert result.issues_seen == 34
    assert stats_of(result)["issues"]["inserted"] == 34
    row = store.query_one("SELECT assignee_id FROM issues WHERE issue_key = ?", (first_on_page_1,))
    assert row["assignee_id"] == "acc-dev-2"
    assert stored(store) == expected(fake, fields)


def test_requests_count_only_the_run_when_a_client_is_reused(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    client = fake.client()
    first = run_sync(client, store, fields, projects=["AIL"], mode="full", now=lambda: fake.clock)
    sent_by_first = len(fake.requests)
    second = run_sync(client, store, fields, projects=["AIL"], mode="incremental", now=lambda: fake.clock)
    assert first.requests == sent_by_first
    # myself, statuses, search, two feeds, the site-size probe, and the board's members (ten ranges: none yet)
    assert second.requests == len(fake.requests) - sent_by_first == 16


def test_a_failure_on_a_later_page_fails_the_run_keeps_the_cursor_and_the_next_run_recovers(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)
    fake.clock = START + timedelta(minutes=30)
    for key in ("AIL-16", "AIL-23", "AIL-26"):
        fake.transition(key, "In Progress")
    fake.log_work("AIL-16")
    fake.search_page_cap = 2  # the three changed issues take two pages
    undo = fake.fail(SEARCH, status=503, when=is_later_page)
    fake.sleeps.clear()

    with pytest.raises(JiraUnavailable, match="after 4 attempts"):
        sync(fake, store, fields, "incremental")

    run = last_run(store)
    assert run["status"] == "failed"
    assert run["stats"]["error_type"] == "JiraUnavailable"
    assert "HTTP 503" in run["stats"]["error"]
    assert run["finished_at"] is not None
    assert store.get_cursor("AIL").last_synced_at == to_iso(START)  # the cursor didn't move
    assert fake.sleeps == [1, 2, 4]  # retried with backoff, never really sleeping
    assert not fake.calls(UPDATED_FEED)  # the run stopped before the worklog feed
    # Oldest change first: AIL-23 and AIL-26 made page 1 (committed); AIL-16,
    # changed twice, was on the failed page and wrote nothing.
    rows = store.query(
        "SELECT issue_key, status FROM issues WHERE issue_key IN ('AIL-16', 'AIL-23', 'AIL-26')"
    )
    assert {r["issue_key"]: r["status"] for r in rows} == {
        "AIL-16": "To Do",
        "AIL-23": "In Progress",
        "AIL-26": "In Progress",
    }
    assert store.count("worklogs", {"issue_key": "AIL-16"}) == 0

    undo()
    fake.clock += timedelta(minutes=30)
    recovered = sync(fake, store, fields, "incremental")
    # Still reading from the old cursor, so nothing from the failed window is lost.
    assert f'updated >= "{chicago_minute(START - timedelta(minutes=5))}"' in fake.jqls()[-1]
    assert stored(store) == expected(fake, fields)
    assert recovered.worklogs_from_feed == 1
    assert store.get_cursor("AIL").last_synced_at == recovered.started_at == to_iso(fake.clock)
    assert last_run(store)["status"] == "succeeded"


def test_a_page_is_written_all_or_nothing(fake: FakeJira, store: Store, fields: FieldsConfig) -> None:
    fake.search_page_cap = 8
    order = sorted(fake.project_issues("AIL"), key=lambda i: (i.updated, int(i.id)))
    first_page = {i.key for i in order[:8]}
    broken = order[9].key  # second page
    fake.fail(REMOTE_LINKS, status=500, when=lambda r: r.url.path.split("/")[5] == broken)

    with pytest.raises(JiraUnavailable):
        sync(fake, store, fields)

    assert store.issue_keys("AIL") == first_page
    child_keys = {
        row[0]
        for table in ("status_changes", "field_changes", "worklogs", "links")
        for row in store.query(f"SELECT DISTINCT issue_key FROM {table}")
    }
    assert child_keys and child_keys <= first_page
    assert store.get_cursor("AIL") is None
    assert last_run(store)["stats"]["tables"]["issues"]["inserted"] == 8  # committed writes only


def test_a_database_error_rolls_back_the_page_and_its_counts(
    fake: FakeJira, store: Store, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.search_page_cap = 8
    order = sorted(fake.project_issues("AIL"), key=lambda i: (i.updated, int(i.id)))
    first_page = {i.key for i in order[:8]}
    broken = order[10].key  # its links are written after two second-page issues
    replace_rows = store.replace_rows

    def failing_replace_rows(table: str, where: dict[str, Any], rows: Any) -> WriteStats:
        if table == "links" and where == {"issue_key": broken}:
            raise sqlite3.OperationalError("database is locked")
        return replace_rows(table, where, rows)

    monkeypatch.setattr(store, "replace_rows", failing_replace_rows)
    with pytest.raises(sqlite3.OperationalError):
        sync(fake, store, fields)

    assert store.issue_keys("AIL") == first_page  # the second page's issues were rolled back
    run = last_run(store)
    assert (run["status"], run["stats"]["error_type"]) == ("failed", "OperationalError")
    assert run["stats"]["tables"]["issues"]["inserted"] == 8  # rolled-back writes aren't counted
    assert run["stats"]["rows_written"] == sum(counts(store).values())


def test_an_incremental_run_without_a_cursor_runs_full_with_a_warning(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]]
) -> None:
    result = sync(fake, store, fields, "incremental")

    assert result.requested_mode == "incremental"
    assert result.project_modes == {"AIL": "full"}
    assert result.warnings == ["AIL: no earlier successful sync, so it was read in full"]
    fallbacks = [e for e in logs if e["event"] == "sync_full_fallback"]
    assert [(e["log_level"], e["project"]) for e in fallbacks] == [("warning", "AIL")]
    assert fake.jqls() == ['project = "AIL" ORDER BY updated ASC']
    assert not fake.calls(UPDATED_FEED)  # no project ran incremental
    assert stored(store) == expected(fake, fields)
    assert store.get_cursor("AIL").last_synced_at == to_iso(START)


def test_an_unreadable_cursor_is_treated_as_missing_and_rewritten(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]]
) -> None:
    sync(fake, store, fields)
    store.set_cursor("AIL", last_synced_at="last tuesday", run_id="r0", account_timezone=CHICAGO)
    fake.clock += timedelta(hours=1)

    result = sync(fake, store, fields, "incremental")

    assert result.project_modes == {"AIL": "full"}
    assert [e["project"] for e in logs if e["event"] == "sync_cursor_unreadable"] == ["AIL"]
    assert store.get_cursor("AIL").last_synced_at == to_iso(fake.clock)
    assert result.rows_written == 0


def test_each_project_keeps_its_own_cursor_and_mode(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields, projects=("AIL",))  # AIL's cursor: START
    fake.clock = START + timedelta(hours=2)
    fake.transition("OPS-3", "In Progress")
    fake.log_work("OPS-1")
    run_start = to_iso(fake.clock)

    result = sync(fake, store, fields, "incremental", projects=("AIL", "OPS"))

    assert result.project_modes == {"AIL": "incremental", "OPS": "full"}
    assert result.warnings == ["OPS: no earlier successful sync, so it was read in full"]
    cutoff = chicago_minute(START - timedelta(minutes=5))
    assert fake.jqls()[-2:] == [
        f'project = "AIL" AND updated >= "{cutoff}" ORDER BY updated ASC',
        'project = "OPS" ORDER BY updated ASC',
    ]
    assert fake.calls(UPDATED_FEED)[-1].url.params["since"] == str(epoch_ms(START - timedelta(minutes=5)))
    assert stored(store) == expected(fake, fields, ("AIL", "OPS"))
    cursors = {p: store.get_cursor(p).last_synced_at for p in ("AIL", "OPS")}
    assert cursors == {"AIL": run_start, "OPS": run_start}

    # From now on both have a cursor, so both run incremental.
    fake.clock += timedelta(hours=1)
    again = sync(fake, store, fields, "incremental", projects=("AIL", "OPS"))
    assert again.project_modes == {"AIL": "incremental", "OPS": "incremental"}
    assert again.warnings == []
    assert again.rows_written == 0


def test_the_worklog_feed_starts_at_the_earliest_incremental_cursor(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields, projects=("AIL",))  # AIL's cursor: START
    fake.clock = START + timedelta(minutes=30)
    worklog_id = fake.log_work("AIL-10", 3600)  # after AIL's cursor, before OPS's
    fake.clock = START + timedelta(hours=1)
    sync(fake, store, fields, projects=("OPS",))  # OPS's cursor: START + 1 h
    fake.clock = START + timedelta(hours=2)

    result = sync(fake, store, fields, "incremental", projects=("AIL", "OPS"))

    assert result.project_modes == {"AIL": "incremental", "OPS": "incremental"}
    assert fake.calls(UPDATED_FEED)[-1].url.params["since"] == str(epoch_ms(START - timedelta(minutes=5)))
    assert store.count("worklogs", {"worklog_id": str(worklog_id)}) == 1
    assert stored(store) == expected(fake, fields, ("AIL", "OPS"))


# --- deletions and moves -----------------------------------------------------------------------


def test_a_full_sync_deletes_issues_gone_from_jira_but_never_after_a_failed_search(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)
    fake.clock += timedelta(hours=1)
    fake.delete_issue("AIL-18")
    rows_of_ail18 = {t: store.count(t, {"issue_key": "AIL-18"}) for t in SYNC_TABLES if t != "statuses"}
    assert rows_of_ail18 == {"issues": 1, "status_changes": 1, "field_changes": 1, "worklogs": 1, "links": 1}

    fake.search_page_cap = 8
    undo = fake.fail(SEARCH, status=503, when=is_later_page)
    with pytest.raises(JiraUnavailable):
        sync(fake, store, fields)
    assert store.count("issues", {"issue_key": "AIL-18"}) == 1  # a partial search deletes nothing
    assert store.count("issues") == 34

    undo()
    result = sync(fake, store, fields)
    assert result.issues_deleted == 1
    assert stats_of(result) == write_stats(
        issues=WriteStats(deleted=1, unchanged=33),
        status_changes=WriteStats(deleted=1, unchanged=35),
        field_changes=WriteStats(deleted=1, unchanged=37),
        worklogs=WriteStats(deleted=1, unchanged=15),
        links=WriteStats(deleted=1, unchanged=9),
        statuses=WriteStats(unchanged=3),
    )
    assert all(store.count(t, {"issue_key": "AIL-18"}) == 0 for t in rows_of_ail18)
    assert stored(store) == expected(fake, fields)


def test_an_incremental_sync_does_not_delete_issues(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)
    fake.clock += timedelta(hours=1)
    fake.delete_issue("AIL-16")  # no worklogs, nothing else changes
    result = sync(fake, store, fields, "incremental")
    assert result.issues_deleted == 0 and result.rows_written == 0
    assert store.count("issues", {"issue_key": "AIL-16"}) == 1  # only a full sync can notice


@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_a_moved_issue_is_stored_under_its_new_key_only(
    fake: FakeJira, store: Store, fields: FieldsConfig, mode: str
) -> None:
    both = ("AIL", "OPS")
    sync(fake, store, fields, projects=both)
    issue_id = fake.issue("AIL-11").id
    fake.clock += timedelta(hours=1)
    new_key = fake.move("AIL-11", "OPS")
    assert new_key == "OPS-4"

    result = sync(fake, store, fields, mode, projects=both)

    assert all(store.count(t, {"issue_key": "AIL-11"}) == 0 for t in SYNC_TABLES if t != "statuses")
    assert store.issue_id_map()[issue_id] == "OPS-4"
    child_tables = ("status_changes", "field_changes", "worklogs")
    moved = {table: store.count(table, {"issue_key": "OPS-4"}) for table in child_tables}
    assert moved == {"status_changes": 3, "field_changes": 3, "worklogs": 2}
    assert stored(store) == expected(fake, fields, both)
    # The old key's rows go and the new key's arrive, whichever way it is noticed.
    tables = stats_of(result)
    issues = tables["issues"]
    assert (issues["inserted"], issues["updated"], issues["deleted"]) == (1, 0, 1)
    for table, moved in (("status_changes", 3), ("field_changes", 3), ("worklogs", 2)):
        assert (tables[table]["inserted"], tables[table]["deleted"]) == (moved, moved)
    # A full sync sees AIL-11 vanish from AIL; an incremental one sees OPS-4 re-keyed.
    assert result.issues_deleted == (1 if mode == "full" else 0)
    # Its older worklogs aren't in the feed, so they are read for the new key.
    assert "OPS-4" in fake.keys_called(WORKLOGS)


def test_an_issue_moved_in_from_outside_the_snapshot_brings_its_worklogs(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)  # AIL only: OPS is not in the snapshot
    fake.clock += timedelta(hours=1)
    new_key = fake.move("OPS-1", "AIL")
    result = sync(fake, store, fields, "incremental")
    assert result.worklogs_from_feed == 0  # its worklog was logged long ago
    assert store.count("worklogs", {"issue_key": new_key}) == 1
    assert stored(store) == expected(fake, fields)


# --- worklogs ------------------------------------------------------------------------------------


def test_a_full_sync_asks_for_worklogs_only_where_time_was_logged(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)
    assert set(fake.keys_called(WORKLOGS)) == AIL_WITH_TIME
    assert len(fake.calls(WORKLOGS)) == len(AIL_WITH_TIME)
    assert fake.issue("AIL-14").time_spent == 0  # logged and removed: 0, not null, and no call either
    assert "AIL-14" not in fake.keys_called(WORKLOGS)


def test_a_full_sync_removes_worklogs_deleted_in_jira(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)
    fake.clock += timedelta(hours=1)
    fake.delete_worklog(fake.worklogs_of("AIL-5")[0].id)  # AIL-5 keeps one worklog
    fake.delete_worklog(fake.worklogs_of("AIL-6")[0].id)  # AIL-6's only one: timespent 0
    calls_before = len(fake.calls(WORKLOGS))

    result = sync(fake, store, fields)

    assert stats_of(result)["worklogs"] == {"inserted": 0, "updated": 0, "deleted": 2, "unchanged": 14}
    assert store.count("worklogs", {"issue_key": "AIL-6"}) == 0
    assert "AIL-6" not in fake.keys_called(WORKLOGS)[calls_before:]
    assert stored(store) == expected(fake, fields)


def test_the_worklog_feed_ignores_issues_outside_the_snapshot(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)  # OPS exists in Jira but is not synced
    fake.clock += timedelta(hours=1)
    ours = fake.log_work("AIL-10", 3600)
    theirs = fake.log_work("OPS-1", 3600)
    fake.delete_worklog(fake.worklogs_of("OPS-2")[0].id)

    result = sync(fake, store, fields, "incremental")

    listed = json.loads(fake.calls(WORKLOG_LIST)[-1].content)["ids"]
    assert sorted(listed) == sorted([ours, theirs])  # the feed covers every project...
    assert result.worklogs_from_feed == 1  # ...but only stored issues keep theirs
    assert stats_of(result)["worklogs"] == {"inserted": 1, "updated": 0, "deleted": 0, "unchanged": 0}
    assert store.query_one("SELECT issue_key FROM worklogs WHERE worklog_id = ?", (str(ours),))[0] == "AIL-10"
    assert store.count("worklogs", {"worklog_id": str(theirs)}) == 0
    assert not store.issue_keys("OPS")
    assert stored(store) == expected(fake, fields)


def test_a_worklog_deleted_while_the_feed_is_read_ends_up_deleted(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    sync(fake, store, fields)
    fake.clock += timedelta(hours=1)
    worklog_id = fake.log_work("AIL-10", 3600)

    def delete_after_listing(request: httpx.Request) -> None:
        # Listed as updated, then deleted before the deleted feed is read.
        if route_of(request.url.path) == WORKLOG_LIST and worklog_id in fake.worklogs:
            fake.delete_worklog(worklog_id)

    fake.after_request.append(delete_after_listing)
    result = sync(fake, store, fields, "incremental")

    assert result.worklogs_from_feed == 1
    assert stats_of(result)["worklogs"] == {"inserted": 1, "updated": 0, "deleted": 1, "unchanged": 0}
    assert store.count("worklogs", {"worklog_id": str(worklog_id)}) == 0
    # The issue changed again after it was read; the next run catches up.
    fake.after_request.clear()
    sync(fake, store, fields, "incremental")
    assert stored(store) == expected(fake, fields)


# --- changelogs ----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 405])
def test_without_bulk_fetch_changelogs_are_read_per_issue_with_one_warning(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]], status: int
) -> None:
    fake.search_page_cap = 8
    fake.changelog_page_cap = 2  # several changelog pages per issue
    fake.bulkfetch_unavailable(status)

    result = sync(fake, store, fields)

    assert len(fake.calls(BULK)) == 1  # tried once, then never again this run
    assert set(fake.keys_called(CHANGELOG)) == {i.key for i in fake.project_issues("AIL")}
    assert any(r.url.params.get("startAt") not in (None, "0") for r in fake.calls(CHANGELOG))
    assert result.warnings == [
        f"bulk changelog fetch answered HTTP {status}: changelogs were read one issue at a time"
    ]
    assert [(e["log_level"], e["status"]) for e in logs if e["event"] == "bulk_changelog_unavailable"] == [
        ("warning", status)
    ]
    assert stored(store) == expected(fake, fields)

    # The same rows the bulk fetch gives, on an identical site.
    other = FakeJira()
    build(other, "AIL", AIL_PLAN, AIL_LINKS)
    other.clock = START
    with Store.open(":memory:") as bulk_store:
        sync(other, bulk_store, fields)
        assert stored(bulk_store) == stored(store)


@pytest.mark.parametrize(("status", "error"), [(500, JiraUnavailable), (403, JiraAuthError)])
def test_other_bulk_fetch_errors_fail_the_run(
    fake: FakeJira, store: Store, fields: FieldsConfig, status: int, error: type[Exception]
) -> None:
    fake.bulkfetch_unavailable(status)
    with pytest.raises(error):
        sync(fake, store, fields)
    assert not fake.calls(CHANGELOG)
    assert last_run(store)["status"] == "failed"
    assert store.count("issues") == 0


@pytest.mark.parametrize(
    ("route", "body", "message"),
    [
        pytest.param(
            BULK,
            {"issueChangeLogs": [{"changeHistories": []}]},
            "bulk changelog entry without issueId",
            id="bulk entry",
        ),
        pytest.param(
            UPDATED_FEED,
            {"values": [{"updatedTime": 1}], "since": 0, "until": 1, "lastPage": True},
            "updated worklog feed without worklogId",
            id="feed entry",
        ),
        pytest.param(
            WORKLOG_LIST,
            [{"id": "1", "timeSpentSeconds": 60}],
            "listed worklog without issueId",
            id="listed worklog",
        ),
    ],
)
def test_an_entry_without_its_id_fails_the_run_instead_of_dropping_data(
    fake: FakeJira, store: Store, fields: FieldsConfig, route: str, body: Any, message: str
) -> None:
    sync(fake, store, fields)
    fake.clock += timedelta(hours=1)
    fake.log_work("AIL-10")
    fake.faults.append(lambda r: httpx.Response(200, json=body) if route_of(r.url.path) == route else None)

    with pytest.raises(JiraError, match=f"unexpected Jira payload: .*{message}") as err:
        sync(fake, store, fields, "incremental")

    assert err.value.status_code is None
    assert last_run(store)["status"] == "failed"
    assert store.get_cursor("AIL").last_synced_at == to_iso(START)


# --- read-only end to end -------------------------------------------------------------------------


def test_every_request_is_an_allowlisted_read_and_feeds_never_follow_next_page(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    fake.search_page_cap = 8
    fake.bulk_page_size = 7
    fake.feed_page_size = 1  # one worklog per feed page
    sync(fake, store, fields)
    fake.clock += timedelta(hours=1)
    first = fake.log_work("AIL-10", 3600)
    fake.log_work("AIL-22", 3600)
    fake.delete_worklog(fake.worklogs_of("AIL-5")[0].id)
    fake.delete_worklog(fake.worklogs_of("AIL-9")[0].id)
    fake.bulkfetch_unavailable(404)
    sync(fake, store, fields, "incremental")

    assert len(fake.requests) > 60
    for request in fake.requests:
        assert (request.method, route_of(request.url.path)) in ALLOWED
        assert (request.url.scheme, request.url.host) == ("https", "fake.atlassian.net")
        assert "nextPage" not in str(request.url)
    # Feed pages follow `until` as `since`: never the absolute nextPage URL.
    since = epoch_ms(START - timedelta(minutes=5))
    assert [r.url.params["since"] for r in fake.calls(UPDATED_FEED)] == [
        str(since),
        str(epoch_ms(fake.worklogs[first].updated)),
    ]
    assert len(fake.calls(DELETED_FEED)) == 2
    assert stored(store) == expected(fake, fields)


def test_render_names_modes_issues_tables_and_requests(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    fake.request_tick = timedelta(seconds=2)
    result = sync(fake, store, fields)
    text = result.render()
    assert f"sync run {result.run_id} (full requested): AIL full" in text
    assert "issues: 34 seen, 0 deleted; worklogs from the feed: 0" in text
    assert "rows written: 137" in text
    for table, count in AIL_COUNTS.items():
        assert f"    {table:<14}  {count} inserted, 0 updated, 0 deleted, 0 unchanged" in text
    assert "  members: 1 board refreshed, 0 failed; 0 members\n  pictures:" in text
    assert f"requests: {result.requests} in {result.requests * 2:.1f} s" in text
    assert text.endswith("warnings: none")

    fake.bulkfetch_unavailable(404)
    fake.clock += timedelta(hours=1)
    fallback = sync(fake, store, fields, "incremental", projects=("AIL", "OPS")).render()
    assert "(incremental requested): AIL incremental, OPS full" in fallback
    assert fallback.endswith(
        "  warnings:\n"
        "    - OPS: no earlier successful sync, so it was read in full\n"
        "    - bulk changelog fetch answered HTTP 404: changelogs were read one issue at a time"
    )


# --- account zone and arguments ------------------------------------------------------------------


@pytest.mark.parametrize("reported", [None, "", "Mars/Olympus_Mons"])
def test_without_a_known_account_zone_incremental_projects_run_full(
    fake: FakeJira, store: Store, fields: FieldsConfig, reported: str | None
) -> None:
    fake.reported_zone = reported
    sync(fake, store, fields)
    assert store.get_cursor("AIL").account_timezone is None
    fake.clock += timedelta(hours=1)

    result = sync(fake, store, fields, "incremental")

    assert result.project_modes == {"AIL": "full"}
    assert result.warnings == ["AIL: Jira did not report the account's time zone, so it was read in full"]
    assert "updated >=" not in fake.jqls()[-1]
    assert result.rows_written == 0


def test_project_keys_are_checked_normalised_and_deduplicated(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    result = sync(fake, store, fields, projects=("ail", " AIL ", "AIL"))
    assert result.project_modes == {"AIL": "full"}
    assert fake.jqls() == ['project = "AIL" ORDER BY updated ASC']


@pytest.mark.parametrize(
    ("options", "message"),
    [
        pytest.param({"projects": ["AIL-1"]}, "invalid Jira project keys", id="bad key"),
        pytest.param({"projects": ['AIL" OR project = "X']}, "invalid Jira project keys", id="jql injection"),
        pytest.param({"projects": []}, "no Jira projects", id="no projects"),
        pytest.param({"mode": "partial"}, "mode must be", id="unknown mode"),
        pytest.param({"overlap": timedelta(minutes=-1)}, "overlap", id="negative overlap"),
    ],
)
def test_bad_arguments_fail_before_any_request_or_run(
    fake: FakeJira, store: Store, fields: FieldsConfig, options: dict[str, Any], message: str
) -> None:
    arguments: dict[str, Any] = {"projects": ["AIL"], "mode": "full"} | options
    with pytest.raises(ValueError, match=message):
        run_sync(fake.client(), store, fields, now=lambda: fake.clock, **arguments)
    assert fake.requests == []
    assert store.count("runs") == 0


def test_an_auth_failure_records_a_failed_run_and_sets_no_cursor(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]]
) -> None:
    fake.fail(MYSELF, status=401)
    with pytest.raises(JiraAuthError):
        sync(fake, store, fields)
    assert len(fake.requests) == 1
    run = last_run(store)
    assert (run["status"], run["stats"]["error_type"]) == ("failed", "JiraAuthError")
    assert store.get_cursor("AIL") is None
    failed = [e for e in logs if e["event"] == "sync_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "error"
    assert all(TOKEN not in json.dumps(e, default=str) for e in logs)


# --- each board's members (step 6) -------------------------------------------------------------------

# Every range of the site's users Jira's assignable-user search serves: the members read asks for each of them.
RANGES = ["0", "100", "200", "300", "400", "500", "600", "700", "800", "900"]
# users/search lists accounts of every kind, so the warning says what was counted: never "users", which an admin
# looking at the site's people could rightly dispute.
OVER_THE_SCAN = ("Jira lists more than 1,000 accounts on this site (people, apps and former users), and the board "
                 "members read can scan only the first 1,000 of them, so some people may be missing from the @ list")


def members_of(store: Store, project: str) -> dict[str, str]:
    """account -> seen_at, the board's board_members rows."""
    return {row[0]: row[1] for row in store.query(
        "SELECT account_id, seen_at FROM board_members WHERE project_key = ?", (project,))}


def team(fake: FakeJira) -> None:
    """AIL's assignable users as Jira lists them: its two assignees, two people with no tickets, and the
    accounts that are never members (an app, a customer, a deactivated person)."""
    fake.add_member("AIL", "acc-dev-1")
    fake.add_member("AIL", "acc-dev-2")
    fake.add_member("AIL", "acc-m1", "Noor Haddad", avatarUrls={"48x48": "https://example.invalid/m1/48"})
    fake.add_member("AIL", "acc-m2", "Yuki Tanabe")
    fake.add_member("AIL", "app-tempo", "Timesheets app", accountType="app")
    fake.add_member("AIL", "acc-c1", "Customer One", accountType="customer")
    fake.add_member("AIL", "acc-gone", "Former Member", active=False)
    fake.add_member("OPS", "acc-dev-1")
    fake.add_member("OPS", "acc-o1", "Tomás Ferreira")


def test_a_sync_stores_each_boards_members_and_their_names(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]]
) -> None:
    team(fake)
    result = sync(fake, store, fields, projects=("AIL", "OPS"))

    seen = result.started_at
    assert members_of(store, "AIL") == {account: seen for account in ("acc-dev-1", "acc-dev-2", "acc-m1", "acc-m2")}
    assert members_of(store, "OPS") == {"acc-dev-1": seen, "acc-o1": seen}
    people = {row[0]: tuple(row)[1:] for row in store.query(
        "SELECT account_id, display_name, active, updated_at, avatar_url, avatar FROM people")}
    assert people["acc-m1"] == ("Noor Haddad", 1, seen, "https://example.invalid/m1/48", None)
    assert people["acc-o1"] == ("Tomás Ferreira", 1, seen, None, None)
    assert not {"app-tempo", "acc-c1", "acc-gone"} & set(people)  # never members, so never people either
    # One paged read a board, of every range Jira serves, the empty ones too; before them, one site-size probe a run.
    assert [(r.url.params["project"], r.url.params["startAt"]) for r in fake.calls(ASSIGNABLE)] == [
        (board, start) for board in ("AIL", "OPS") for start in RANGES]
    [probe] = fake.calls(SITE_USERS)
    assert dict(probe.url.params) == {"startAt": "1000", "maxResults": "1"}
    assert fake.requests.index(probe) < fake.requests.index(fake.calls(ASSIGNABLE)[0])
    assert stored(store) == expected(fake, fields, ("AIL", "OPS"))  # the snapshot as before
    assert result.members.as_dict() == {"boards": 2, "members": 6, "failed_boards": 0}
    assert last_run(store)["stats"]["members"] == {"boards": 2, "members": 6, "failed_boards": 0}
    assert "  members: 2 boards refreshed, 0 failed; 6 members\n" in result.render()
    assert result.warnings == []
    # Nothing of the user objects but the people row's columns is kept, and the log names no one.
    everything = json.dumps([[*row] for table in ("people", "board_members") for row in store.query(f"SELECT * FROM {table}")])
    assert "example.invalid/rest" not in everything and "@example.invalid" not in everything
    assert "Europe/Berlin" not in everything and "de_DE" not in everything
    assert [(e["board"], e["members"]) for e in logs if e["event"] == "board_members_refreshed"] == [("AIL", 4), ("OPS", 2)]
    assert "Noor" not in json.dumps(logs, default=str) and "acc-m1" not in json.dumps(logs, default=str)


def test_a_complete_fetch_replaces_the_boards_members(fake: FakeJira, store: Store, fields: FieldsConfig) -> None:
    team(fake)
    sync(fake, store, fields)
    fake.members["AIL"] = [user for user in fake.members["AIL"] if user["accountId"] != "acc-m2"]  # left the team
    fake.add_member("AIL", "acc-m3", "Ines Duarte")  # joined it
    fake.clock += timedelta(hours=1)

    again = sync(fake, store, fields, "incremental")  # incremental too: every sync reads the members

    assert set(members_of(store, "AIL")) == {"acc-dev-1", "acc-dev-2", "acc-m1", "acc-m3"}
    assert set(members_of(store, "AIL").values()) == {again.started_at}  # seen by this run
    assert store.count("people", {"account_id": "acc-m2"}) == 1  # an account stays; only its membership goes
    assert again.members.as_dict() == {"boards": 1, "members": 4, "failed_boards": 0}
    assert len(fake.calls(ASSIGNABLE)) == 20 and len(fake.calls(SITE_USERS)) == 2  # ten ranges and a probe a sync


def test_a_members_page_never_touches_a_stored_picture(fake: FakeJira, store: Store, fields: FieldsConfig) -> None:
    team(fake)
    sync(fake, store, fields)
    with store.transaction():
        store.conn.execute("UPDATE people SET avatar = ?, avatar_type = 'image/png', avatar_hash = 'a1b2c3d4e5f6',"
                           " avatar_source = 'https://example.invalid/m1/48', avatar_fetched_at = ? WHERE account_id = 'acc-m1'",
                           (b"\x89PNG\r\n\x1a\n", to_iso(fake.clock)))
    before = dict(store.query_one("SELECT * FROM people WHERE account_id = 'acc-m1'"))
    fake.members["AIL"][2]["displayName"] = "Noor Haddad-Lee"  # renamed in Jira
    fake.clock += timedelta(hours=1)
    sync(fake, store, fields, "incremental")
    after = dict(store.query_one("SELECT * FROM people WHERE account_id = 'acc-m1'"))
    assert after["display_name"] == "Noor Haddad-Lee"
    picture = ("avatar", "avatar_type", "avatar_hash", "avatar_source", "avatar_fetched_at")
    assert {column: after[column] for column in picture} == {column: before[column] for column in picture}


def no_answer(_request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("no answer")


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        pytest.param({"status": 403}, "HTTP 403", id="403"),
        pytest.param({"status": 503}, "HTTP 503 after retries", id="503"),
        pytest.param({"answer": no_answer}, "no answer from Jira", id="timeouts"),
        pytest.param({"answer": lambda _r: httpx.Response(200, json={"values": []})}, "unexpected payload", id="bad payload"),
        pytest.param({"answer": lambda _r: httpx.Response(200, json=[{"displayName": "No Id"}])}, "unexpected payload",
                     id="a user without an account id"),
    ],
)
def test_a_board_whose_fetch_fails_keeps_its_members_and_never_fails_the_sync(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]], fault: dict[str, Any], reason: str
) -> None:
    team(fake)
    sync(fake, store, fields, projects=("AIL", "OPS"))
    kept = members_of(store, "AIL")
    fake.add_member("AIL", "acc-m9", "Not Yet Seen")
    fake.add_member("OPS", "acc-o2", "Ines Duarte")
    if "status" in fault:
        fake.fail(ASSIGNABLE, status=fault["status"], when=lambda r: r.url.params["project"] == "AIL")
    else:
        answer = fault["answer"]
        fake.faults.append(lambda r: answer(r) if route_of(r.url.path) == ASSIGNABLE and r.url.params["project"] == "AIL" else None)
    fake.clock += timedelta(hours=1)

    result = sync(fake, store, fields, "incremental", projects=("AIL", "OPS"))

    assert members_of(store, "AIL") == kept  # the rows it had, as they were
    assert "acc-o2" in members_of(store, "OPS")  # the next board was read all the same
    assert last_run(store)["status"] == "succeeded" and store.get_cursor("AIL").last_run_id == result.run_id
    assert result.members.as_dict() == {"boards": 1, "members": 3, "failed_boards": 1}
    assert last_run(store)["stats"]["members"] == {"boards": 1, "members": 3, "failed_boards": 1}
    assert result.warnings == [f"AIL: the board's members were not refreshed ({reason}); the stored ones stay"]
    failed = [e for e in logs if e["event"] == "board_members_not_refreshed"]
    assert [(e["board"], e["reason"], e["log_level"]) for e in failed] == [("AIL", reason, "warning")]
    assert "  members: 1 board refreshed, 1 failed; 3 members\n" in result.render()
    text = json.dumps(logs, default=str) + json.dumps(last_run(store)["stats"])
    assert "No Id" not in text and "acc-m9" not in text and "@example.invalid" not in text


def test_a_database_error_storing_the_members_keeps_the_ones_stored(
    fake: FakeJira, store: Store, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    team(fake)
    sync(fake, store, fields)
    kept = members_of(store, "AIL")
    fake.add_member("AIL", "acc-m9", "Not Yet Seen")
    real = store.upsert

    def failing(table: str, row: Any) -> Any:
        if table == "people" and row["account_id"] == "acc-m9":
            raise sqlite3.OperationalError("disk I/O error")
        return real(table, row)

    monkeypatch.setattr(store, "upsert", failing)
    fake.clock += timedelta(hours=1)
    result = sync(fake, store, fields, "incremental")
    assert members_of(store, "AIL") == kept  # the board's rows and the people rows are one transaction
    assert store.count("people", {"account_id": "acc-m9"}) == 0
    assert result.warnings == ["AIL: the board's members were not refreshed (not stored (OperationalError)); the stored ones stay"]


def answers_take(
    fake: FakeJira, seconds: float, when: Callable[[httpx.Request], bool], *, waiting_for: str = "read"
) -> None:
    """The requests `when` accepts take `seconds` each on the fake clock, as a real transport takes them: an attempt
    whose timeout comes first ends there instead, with that timeout. `waiting_for`: "read" (connected, the answer
    slow) or "connect" (no connection made)."""
    timeout_error = httpx.ReadTimeout if waiting_for == "read" else httpx.ConnectTimeout

    def slow(request: httpx.Request) -> httpx.Response | None:
        if not when(request):
            return None
        limit = request.extensions["timeout"][waiting_for]
        if limit is not None and limit < seconds:
            fake.clock += timedelta(seconds=limit)
            raise timeout_error("no answer in time", request=request)
        fake.clock += timedelta(seconds=seconds)
        return None  # then answered as ever

    fake.faults.append(slow)


def members_step(request: httpx.Request) -> bool:
    return route_of(request.url.path) in (ASSIGNABLE, SITE_USERS)


def size_check(request: httpx.Request) -> bool:
    return route_of(request.url.path) == SITE_USERS


def pages_of(board: str) -> Callable[[httpx.Request], bool]:
    return lambda request: route_of(request.url.path) == ASSIGNABLE and request.url.params["project"] == board


def test_the_members_step_has_thirty_seconds_a_board_and_ten_for_the_size_check_at_most_five_minutes() -> None:
    assert (MEMBERS_BOARD_BUDGET_S, SITE_PROBE_BUDGET_S, MEMBERS_BUDGET_CAP_S) == (30.0, 10.0, 300.0)
    assert [members_budget_s(boards) for boards in (1, 2, 3, 9, 10, 40)] == [40.0, 70.0, 100.0, 280.0, 300.0, 300.0]


def test_three_boards_whose_pages_take_two_seconds_are_all_refreshed(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    """Three boards, as the example .env syncs, each read in its ten pages, at two seconds a page: 62 seconds in all,
    which one minute for every board together could never hold (the third board's last page met it)."""
    team(fake)
    fake.add_project("CORE")
    fake.add_member("CORE", "acc-k1", "Core Member")
    answers_take(fake, 2, members_step)
    start = fake.clock

    result = sync(fake, store, fields, projects=("AIL", "OPS", "CORE"), clock=lambda: fake.clock.timestamp())

    assert fake.clock - start == timedelta(seconds=62)  # the size check and thirty pages, two seconds each
    assert result.members.as_dict() == {"boards": 3, "members": 7, "failed_boards": 0}
    assert set(members_of(store, "CORE")) == {"acc-k1"}
    assert result.warnings == []


def test_a_board_that_hangs_never_takes_the_time_of_the_boards_after_it(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    team(fake)
    sync(fake, store, fields, projects=("AIL", "OPS"))
    kept = members_of(store, "AIL")
    fake.add_member("AIL", "acc-m9", "Not Yet Seen")
    fake.add_member("OPS", "acc-o2", "Ines Duarte")
    answers_take(fake, 600, pages_of("AIL"))  # AIL's answers never come: each attempt waits out its timeout
    fake.clock += timedelta(hours=1)
    start = fake.clock

    result = sync(fake, store, fields, "incremental", projects=("AIL", "OPS"), clock=lambda: fake.clock.timestamp())

    assert fake.clock - start == timedelta(seconds=35)  # AIL's share: half the step's 70 seconds, and no more
    assert members_of(store, "AIL") == kept
    assert "acc-o2" in members_of(store, "OPS")  # OPS had the time AIL could not take
    assert result.members.as_dict() == {"boards": 1, "members": 3, "failed_boards": 1}
    assert result.warnings == ["AIL: the board's members were not refreshed (time budget spent); the stored ones stay"]
    assert last_run(store)["status"] == "succeeded"


def test_the_members_step_never_runs_past_its_time(fake: FakeJira, store: Store, fields: FieldsConfig) -> None:
    """When no answer ever comes, the size check stops at its 10 seconds and each board at its equal share of what
    is left: the step ends at its 70 seconds for two boards, and every board keeps its rows."""
    team(fake)
    sync(fake, store, fields, projects=("AIL", "OPS"))
    kept = {"AIL": members_of(store, "AIL"), "OPS": members_of(store, "OPS")}
    fake.add_member("AIL", "acc-m9", "Not Yet Seen")
    answers_take(fake, 600, members_step)
    fake.clock += timedelta(hours=1)
    start = fake.clock
    asked, probed = len(fake.calls(ASSIGNABLE)), len(fake.calls(SITE_USERS))

    result = sync(fake, store, fields, "incremental", projects=("AIL", "OPS"), clock=lambda: fake.clock.timestamp())

    assert fake.clock - start == timedelta(seconds=70)  # 10 for the size check, then 30 for each board
    assert len(fake.calls(SITE_USERS)) - probed == 1
    # One attempt a board, its share ending inside it: never AIL's retries eating the time OPS was owed.
    assert [r.url.params["project"] for r in fake.calls(ASSIGNABLE)[asked:]] == ["AIL", "OPS"]
    assert {"AIL": members_of(store, "AIL"), "OPS": members_of(store, "OPS")} == kept
    assert result.members.as_dict() == {"boards": 0, "members": 0, "failed_boards": 2}
    assert result.warnings == [
        "AIL: the board's members were not refreshed (time budget spent); the stored ones stay",
        "OPS: the board's members were not refreshed (time budget spent); the stored ones stay",
    ]
    assert last_run(store)["status"] == "succeeded"


def test_jira_asking_for_a_wait_past_the_budget_ends_the_board_at_once(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    team(fake)
    sync(fake, store, fields, projects=("AIL", "OPS"))
    kept = {"AIL": members_of(store, "AIL"), "OPS": members_of(store, "OPS")}

    def throttled(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(429, headers={"Retry-After": "120"}) if request.url.path == ASSIGNABLE else None

    fake.faults.append(throttled)
    fake.clock += timedelta(hours=1)
    slept = len(fake.sleeps)

    result = sync(fake, store, fields, "incremental", projects=("AIL", "OPS"), clock=lambda: fake.clock.timestamp())

    assert fake.sleeps[slept:] == []  # neither board waited two minutes of the step's one
    assert {"AIL": members_of(store, "AIL"), "OPS": members_of(store, "OPS")} == kept
    assert result.members.as_dict() == {"boards": 0, "members": 0, "failed_boards": 2}
    assert result.warnings == [
        "AIL: the board's members were not refreshed (time budget spent); the stored ones stay",
        "OPS: the board's members were not refreshed (time budget spent); the stored ones stay",
    ]
    assert last_run(store)["status"] == "succeeded"


def test_an_interrupted_members_step_still_finishes_the_run_it_follows(
    fake: FakeJira, store: Store, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        raise KeyboardInterrupt

    monkeypatch.setattr(JiraClient, "assignable_users", interrupted)
    with pytest.raises(KeyboardInterrupt):
        sync(fake, store, fields)
    run = last_run(store)
    assert run["status"] == "succeeded" and run["finished_at"] is not None
    assert store.get_cursor("AIL") is not None


def test_a_failed_sync_reads_no_members(fake: FakeJira, store: Store, fields: FieldsConfig) -> None:
    team(fake)
    fake.fail(SEARCH, status=400)
    with pytest.raises(JiraError):
        sync(fake, store, fields)
    assert fake.calls(ASSIGNABLE) == [] and fake.calls(SITE_USERS) == [] and store.count("board_members") == 0


# --- members far apart on the site, and a site past the 1,000 users the read can scan ---------------------------


def test_members_far_apart_among_the_sites_users_are_all_found(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]]
) -> None:
    """The product owner's "I cannot see all the people on the board", end to end: on a real site app accounts,
    customers and other teams sit between a board's people, so whole ranges of the site's users come back empty,
    and the people after them are read all the same."""
    team(fake)
    fake.members_spread = 150  # AIL's seven at places 0, 150, ..., 900: the ranges at 200, 500 and 800 are empty

    result = sync(fake, store, fields, projects=("AIL", "OPS"))

    assert set(members_of(store, "AIL")) == {"acc-dev-1", "acc-dev-2", "acc-m1", "acc-m2"}  # m1 and m2 after a gap
    assert set(members_of(store, "OPS")) == {"acc-dev-1", "acc-o1"}
    assert [r.url.params["startAt"] for r in fake.calls(ASSIGNABLE)] == RANGES * 2
    assert result.members.as_dict() == {"boards": 2, "members": 6, "failed_boards": 0}
    assert result.warnings == []
    assert not [e for e in logs if e["event"] in ("board_members_page_repeated", "board_members_capped")]


def test_a_member_sent_again_as_the_sites_users_move_never_cuts_the_boards_members_short(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]]
) -> None:
    """The site's users move while a board is read: an account joins ahead of everyone once the first page is sent,
    so AIL's first member comes again, alone, at the top of the next range. The read goes on, and every member is
    stored again: never a list cut short at the repeat standing in for the board's members, with no warning."""
    team(fake)
    fake.members_first_place, fake.members_spread = 99, 150  # AIL's seven at places 99, 249, ..., 999
    sync(fake, store, fields)
    everyone = {"acc-dev-1", "acc-dev-2", "acc-m1", "acc-m2"}
    assert set(members_of(store, "AIL")) == everyone

    def someone_joins(request: httpx.Request) -> None:
        if route_of(request.url.path) == ASSIGNABLE and request.url.params["startAt"] == "0":
            fake.members_first_place += 1  # once the first page is sent: AIL's seven at 100, 250, ..., 1000

    fake.after_request.append(someone_joins)
    fake.clock += timedelta(hours=1)
    asked = len(fake.calls(ASSIGNABLE))

    result = sync(fake, store, fields, "incremental")

    assert set(members_of(store, "AIL")) == everyone  # acc-dev-2, acc-m1 and acc-m2 come after the repeat
    assert [r.url.params["startAt"] for r in fake.calls(ASSIGNABLE)[asked:]] == RANGES
    assert result.members.as_dict() == {"boards": 1, "members": 4, "failed_boards": 0}
    assert result.warnings == []
    repeated = [(e["board"], e["pages"], e["kept"]) for e in logs if e["event"] == "board_members_page_repeated"]
    assert repeated == [("AIL", 1, 6)]  # logged once; the read's six include the app and the customer, never members


def test_a_site_of_more_than_1000_users_warns_that_people_may_be_missing(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]]
) -> None:
    """Jira's assignable-user search never reads past the site's 1,000th user, so a member there cannot be found:
    the sync says the @ list may be incomplete, once a run, and stores what it found all the same. The size check
    counts accounts, people, apps and former users alike (users/search lists them all), and the warning says so."""
    team(fake)
    fake.add_member("AIL", "acc-m8", "Past The Scan")  # AIL's eighth: at place 1,050 once spread
    fake.members_spread = 150
    fake.site_user_count = 1200

    result = sync(fake, store, fields, projects=("AIL", "OPS"))

    assert set(members_of(store, "AIL")) == {"acc-dev-1", "acc-dev-2", "acc-m1", "acc-m2"}
    assert store.count("people", {"account_id": "acc-m8"}) == 0  # never sent by Jira
    assert max(int(r.url.params["startAt"]) for r in fake.calls(ASSIGNABLE)) == 900
    assert result.warnings == [OVER_THE_SCAN]
    run = last_run(store)
    assert run["status"] == "succeeded" and run["stats"]["warnings"] == [OVER_THE_SCAN]
    assert run["stats"]["members"] == {"boards": 2, "members": 6, "failed_boards": 0}  # the same counts, no new key
    assert result.render().endswith(f"  warnings:\n    - {OVER_THE_SCAN}")
    over = [e for e in logs if e["event"] == "board_members_site_over_scan_limit"]
    assert over == [{"event": "board_members_site_over_scan_limit", "logger": "sync", "scan_limit": 1000,
                     "log_level": "warning"}]
    assert len(fake.calls(SITE_USERS)) == 1  # once a run, whatever the number of boards
    text = json.dumps(logs, default=str) + json.dumps(run["stats"])
    assert "acc-site-" not in text and "Site User" not in text  # nothing of the probe's user is logged or kept


def answer_on(route: str, answer: Any) -> Callable[[FakeJira], object]:
    """A setup that makes the fake answer `answer(request)` on `route`."""
    return lambda fake: fake.faults.append(lambda r: answer(r) if route_of(r.url.path) == route else None)


def refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    ("setup", "unknown"),
    [
        pytest.param(lambda _fake: None, [], id="a small site"),
        pytest.param(lambda fake: setattr(fake, "site_user_count", 1000), [], id="exactly 1,000 users"),
        pytest.param(lambda fake: setattr(fake, "browse_users", False), [403], id="403: no Browse users and groups"),
        pytest.param(lambda fake: fake.fail(SITE_USERS, status=404), [404], id="404"),
        pytest.param(lambda fake: fake.fail(SITE_USERS, status=503), [503], id="503 after the retries"),
        pytest.param(answer_on(SITE_USERS, refused), [None], id="no answer after the retries: refused at once"),
        pytest.param(answer_on(SITE_USERS, lambda _r: httpx.Response(200, json={"values": []})), [None],
                     id="not an array"),
    ],
)
def test_a_site_within_the_scan_or_one_jira_will_not_size_gets_no_warning(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]],
    setup: Callable[[FakeJira], object], unknown: list[int | None],
) -> None:
    team(fake)
    setup(fake)

    result = sync(fake, store, fields, projects=("AIL", "OPS"))

    assert result.warnings == []
    assert [e["status"] for e in logs if e["event"] == "site_user_count_unknown"] == unknown  # a log line only
    assert not [e for e in logs if e["event"] == "board_members_site_over_scan_limit"]
    assert result.members.as_dict() == {"boards": 2, "members": 6, "failed_boards": 0}
    assert last_run(store)["status"] == "succeeded"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        pytest.param(RuntimeError("the probe itself broke"), "unexpected RuntimeError", id="a fault of its own"),
        pytest.param(JiraBudgetSpent("GET /rest/api/3/users/search stopped: its time ran out"), "time budget spent",
                     id="its time spent"),
    ],
)
def test_a_failing_site_probe_never_fails_the_sync_or_the_members_step(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
    failure: Exception, reason: str,
) -> None:
    team(fake)

    def failing(*_args: object, **_kwargs: object) -> bool | None:
        raise failure

    monkeypatch.setattr(JiraClient, "site_has_users_beyond_scan", failing)

    result = sync(fake, store, fields, projects=("AIL", "OPS"))

    assert last_run(store)["status"] == "succeeded" and store.get_cursor("AIL").last_run_id == result.run_id
    assert result.members.as_dict() == {"boards": 2, "members": 6, "failed_boards": 0}
    assert set(members_of(store, "AIL")) == {"acc-dev-1", "acc-dev-2", "acc-m1", "acc-m2"}
    assert result.warnings == []  # Jira has not said: a log line, never a warning
    unknown = [e for e in logs if e["event"] == "site_user_count_unknown"]
    assert unknown == [{"event": "site_user_count_unknown", "logger": "sync", "reason": reason, "log_level": "warning"}]
    assert str(failure) not in json.dumps(logs, default=str)  # a short reason, never the error's text


@pytest.mark.parametrize(
    ("waiting_for", "attempts"),
    [
        pytest.param("connect", 2, id="no connection: two attempts"),
        pytest.param("read", 1, id="no answer: one attempt"),
    ],
)
def test_a_size_check_that_times_out_spends_its_own_ten_seconds_and_is_logged_so(
    fake: FakeJira, store: Store, fields: FieldsConfig, logs: list[dict[str, Any]], waiting_for: str, attempts: int
) -> None:
    """A size check whose answer never comes waits out each attempt's timeout, as a real one does, each cut to what
    is left of its SITE_PROBE_BUDGET_S, so its time runs out before Jira's retries do. The sync logs that reason,
    never a status, and the boards have the rest of the step's time: a hung question costs a log line, never a
    board's members."""
    team(fake)
    answers_take(fake, 600, size_check, waiting_for=waiting_for)
    start = fake.clock

    result = sync(fake, store, fields, projects=("AIL", "OPS"), clock=lambda: fake.clock.timestamp())

    assert len(fake.calls(SITE_USERS)) == attempts
    assert fake.clock - start == timedelta(seconds=SITE_PROBE_BUDGET_S)  # the boards answered at once
    unknown = [e for e in logs if e["event"] == "site_user_count_unknown"]
    assert unknown == [{"event": "site_user_count_unknown", "logger": "sync", "reason": "time budget spent",
                        "log_level": "warning"}]
    assert result.members.as_dict() == {"boards": 2, "members": 6, "failed_boards": 0}
    assert result.warnings == []
    assert last_run(store)["status"] == "succeeded"


def test_the_docs_quote_the_over_the_scan_warning_as_the_sync_gives_it() -> None:
    """The reference (docs/REFERENCE.md) and, where this machine has them, the demo's presenter notes quote the
    warning word for word, so what an operator or the client reads there is what the sync says. The presenter notes
    are client-specific and kept out of git (.gitignore), so a fresh clone checks the reference alone."""
    root = Path(__file__).resolve().parent.parent
    assert (root / "docs" / "REFERENCE.md").exists()
    for doc in ("docs/REFERENCE.md", "docs/DEMO_SECURITY_AND_PEOPLE.md"):
        if not (root / doc).exists():
            continue
        text = " ".join((root / doc).read_text(encoding="utf-8").split())
        assert OVER_THE_SCAN in text, doc
