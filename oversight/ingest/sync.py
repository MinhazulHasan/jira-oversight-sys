"""Full and incremental sync (Phase 1).

CONTRACT (tickets 3 and 4).

run_sync(client, store, fields, projects=..., mode=...)
1. Validate project keys (oversight.settings.validate_project_keys). Start a
   run (store.start_run("sync", started_at=now())); `cursor_time` is the run's
   start, captured before any fetch, so anything that changes during the run is
   picked up next time.
2. client.myself()["timeZone"] is the account zone. JQL dates are read in it,
   and it is saved with each project's cursor.
3. Per project (each keeps its own cursor in sync_state, scope = project key):
   - effective mode = "full" if mode == "full" or the project has no cursor yet
     (log a warning when an incremental run falls back to full).
   - statuses: store.replace_rows("statuses", {"project_key": p},
     mapping.map_statuses(p, client.project_statuses(p))).
   - JQL: f'project = "{p}"', plus for incremental
     f' AND updated >= "{jql_datetime(cursor - overlap, account_tz)}"',
     then " ORDER BY updated ASC". overlap defaults to 5 minutes (clock skew,
     and JQL has only minute precision).
   - For each page of client.search_jql(jql, fields.search_fields()), inside one
     store.transaction():
       * store.upsert_issue(map_issue(...)) for each issue, and its assignee
         store.upsert("people", map_assignee(issue, run start)): the display name
         and picture URL the latest issue carries win, and updated_at says when a
         sync last saw the account;
       * changelogs: client.bulk_changelog([issue ids], list of
         fields.tracked_changelog_fields() ids); merge entries by issueId (the
         page gives id -> key); upsert map_status_changes and map_field_changes
         rows. If bulk fetch fails with JiraError status 400, 404 or 405, warn
         once and fall back to client.issue_changelog(key) per issue for the
         rest of the run.
       * links: store.replace_rows("links", {"issue_key": key},
         map_issue_links(issue) + map_remote_links(key, client.remote_links(key))).
       * full mode only, worklogs: if the issue's timespent is null or 0,
         replace its worklogs with [] without calling Jira (nothing can be
         logged); otherwise replace with client.issue_worklogs(key) mapped.
   - Full mode only, once every page has been read: issues stored for the project
     but not returned by the search were deleted or moved in Jira, so
     store.delete_issue() each one. Never do this after a failed or partial search.
4. If any project ran incremental: since = (earliest incremental cursor - overlap)
   in epoch ms. client.worklog_updated(since) gives ids; client.worklog_list(ids)
   gives worklogs; keep those whose issueId is a stored issue
   (store.issue_id_map()) and upsert them. client.worklog_deleted(since) gives
   ids to delete from worklogs.
5. Only after every project succeeded: set each project's cursor
   (last_synced_at = to_iso(cursor_time), run id, account zone), then
   finish_run(status="succeeded", stats=...). The stats name the run's boards
   (`projects`), so health reads each board's own syncs (JO-18). On any exception:
   finish_run(status="failed", stats={"error": str(exc), ...}) and re-raise.
   Cursors don't move.
6. Once the cursors are set: each synced board's members, the people Jira
   lets its tickets be assigned to (Jira's assignable users, as its own
   assignee picker lists them): client.assignable_users(board), one paged read
   per board, mapped by mapping.map_member (people accounts only, active ones
   only). A complete fetch replaces the board's board_members rows and upserts
   their people rows (account id, display name, active flag, when seen and the
   picture URL; never the stored picture's columns) in one transaction. A
   board whose fetch fails (403, timeout, a bad payload, the time budget) keeps
   the rows it had, logs board_members_not_refreshed with the board and a short
   reason (never a name, an account id or an email address), and is counted
   as failed; the next board is read all the same, and nothing in this step
   fails the sync. The step's time (members_budget_s, measured on `clock`) is
   MEMBERS_BOARD_BUDGET_S for each board (a board's read is always ten pages)
   and SITE_PROBE_BUDGET_S for the size check below, at most
   MEMBERS_BUDGET_CAP_S in all. A board may use at most an equal share of the
   time left (what is left divided by the boards still to read), so a slow
   board never leaves the boards after it without time, and the time a quick
   one leaves passes on to them. It is checked before each board, each page
   and each attempt, with every attempt's timeouts and retry waits cut to the
   board's share (the client's `deadline`): a board it cuts short or does not
   reach keeps its rows. Its counts are the run's `members` (boards refreshed,
   members those boards have now, boards failed). Full and incremental runs
   alike, so "sync now" (POST /refresh) too. Jira's assignable-user search
   scans only the site's first 1,000 users (client.ASSIGNABLE_SCAN_LIMIT), so
   before the boards, once a run and within at most SITE_PROBE_BUDGET_S of the
   step's time, client.site_has_users_beyond_scan() asks whether Jira lists
   more accounts on the site (people, apps and former users alike): True adds a
   warning (board_members_site_over_scan_limit) that some people may be
   missing from the @ list; None (Jira would not say) is the client's
   site_user_count_unknown log line only; anything it raises (its time spent,
   which is how a slow or timing-out answer ends) is logged as
   site_user_count_unknown with a short reason and read as None. None of it
   fails the step or the sync, and the `members` counts are unchanged by it.
7. Then, before the run finishes: the pictures of the synced boards' people,
   assignees and members (oversight.ingest.avatars.refresh_pictures, through
   `pictures` or a fetcher of the run's own). Their counts (fetched, unchanged,
   failed) join the run's stats, with each failure's person id and reason; a
   picture that fails is counted, kept there and logged, and nothing in this
   step ever fails the sync. A failed sync fetches neither members nor
   pictures.

Idempotency (Phase 1 done-when): a second run with no Jira changes must report
rows_written == 0. Every write goes through upsert/replace_rows, which count
only real changes. The people rows are counted apart (SyncResult.people), not in
rows_written: a run that reads an issue again also notes when it saw its
assignee, which is not a change in Jira.

Details the contract leaves open:
- A page is fetched completely before its transaction opens, so a failure
  never leaves half a page behind and the database is locked only while rows
  are written. Write counts join the run's totals once their transaction commits.
- An issue new to the snapshot under its key (moved in from another project,
  or re-keyed) also gets its worklogs read in an incremental run: the
  updated-worklog feed lists recent changes only, never its older worklogs.
- If /myself names no usable zone, incremental projects run full (with a
  warning), because a JQL date read in the wrong zone could skip updates.
- A moved or re-keyed issue's old key is removed before the new one is
  written, so its removed rows are counted per table. `issues_deleted` counts
  only issues that disappeared from a full search.
- A bulk changelog entry, feed entry or listed worklog without the id it is
  filed under raises JiraError (like mapping does); entries for issues the
  snapshot doesn't hold are skipped.
- Bad arguments (unknown mode, negative overlap, invalid or no project keys)
  raise ValueError before a run is recorded.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from oversight.config import FieldsConfig
from oversight.errors import ConfigError, JiraBudgetSpent, JiraError, JiraUnavailable
from oversight.ingest.avatars import PictureFetcher, PictureStats, refresh_pictures
from oversight.jira.client import ASSIGNABLE_SCAN_LIMIT
from oversight.jira.mapping import (
    map_assignee,
    map_field_changes,
    map_issue,
    map_issue_links,
    map_member,
    map_remote_links,
    map_status_changes,
    map_statuses,
    map_worklog,
)
from oversight.logs import get_logger
from oversight.settings import validate_project_keys
from oversight.store.db import SYNC_TABLES, Store, WriteStats
from oversight.timeutil import epoch_ms, hours_between, jql_datetime, parse_ts, to_iso, utcnow, zone

if TYPE_CHECKING:
    from oversight.jira.client import JiraClient

# What Jira answers when a site doesn't offer the bulk changelog fetch. Any
# other error (auth, outage, a bad payload) stops the run instead.
_NO_BULK_FETCH = frozenset({400, 404, 405})

# The synced tables that hold an issue's rows, counted when an issue is removed.
_ISSUE_TABLES: tuple[str, ...] = ("issues", "status_changes", "field_changes", "worklogs", "links")

# The members step's (6) time: MEMBERS_BOARD_BUDGET_S for each board, whose read is always ten pages (three
# seconds a page, a retry wait or two included), and SITE_PROBE_BUDGET_S for the site-size check
# (JiraClient.site_has_users_beyond_scan), which may never spend more, so a slow answer to it never leaves the boards
# without time. MEMBERS_BUDGET_CAP_S bounds the whole step however many boards are synced, well inside the
# 15 minutes "sync now" gives the sync (oversight.api.refresh.STEP_TIMEOUT_S). A board it cuts short or does not
# reach keeps its rows.
MEMBERS_BOARD_BUDGET_S = 30.0
SITE_PROBE_BUDGET_S = 10.0
MEMBERS_BUDGET_CAP_S = 300.0


def members_budget_s(boards: int) -> float:
    """The members step's time for `boards` boards: each board's share and the size check's, at most the cap."""
    return min(SITE_PROBE_BUDGET_S + MEMBERS_BOARD_BUDGET_S * boards, MEMBERS_BUDGET_CAP_S)


@dataclass
class MemberStats:
    """The members step's counts (step 6): numbers only."""

    boards: int = 0  # boards whose members were refreshed: a complete fetch, stored
    members: int = 0  # the members those boards have now
    failed_boards: int = 0  # boards whose fetch failed or was not reached: their stored members stay

    def as_dict(self) -> dict[str, int]:
        return {"boards": self.boards, "members": self.members, "failed_boards": self.failed_boards}


@dataclass
class SyncResult:
    run_id: str
    requested_mode: str
    # project -> the mode that actually ran ("full" or "incremental")
    project_modes: dict[str, str]
    # table name -> write counts, for every table in oversight.store.db.SYNC_TABLES
    tables: dict[str, WriteStats]
    issues_seen: int
    issues_deleted: int
    worklogs_from_feed: int
    requests: int
    started_at: str
    finished_at: str
    warnings: list[str] = field(default_factory=list)
    # The assignees' people rows; not in rows_written (see the module docstring).
    people: WriteStats = field(default_factory=WriteStats)
    # Each synced board's members, fetched once the cursors are set (step 6).
    members: MemberStats = field(default_factory=MemberStats)
    # The synced boards' people's pictures, fetched after the sync (step 7).
    pictures: PictureStats = field(default_factory=PictureStats)

    @property
    def rows_written(self) -> int:
        return sum(stats.written for stats in self.tables.values())

    def render(self) -> str:
        """A short human summary: modes, issues seen, per-table counts, requests, duration."""
        modes = ", ".join(f"{project} {mode}" for project, mode in self.project_modes.items())
        duration_s = hours_between(self.started_at, self.finished_at) * 3600
        lines = [
            f"sync run {self.run_id} ({self.requested_mode} requested): {modes}",
            f"  issues: {self.issues_seen:,} seen, {self.issues_deleted:,} deleted; "
            f"worklogs from the feed: {self.worklogs_from_feed:,}",
            f"  rows written: {self.rows_written:,}",
        ]
        width = max((len(table) for table in self.tables), default=0)
        for table, stats in self.tables.items():
            lines.append(
                f"    {table:<{width}}  {stats.inserted:,} inserted, {stats.updated:,} updated, "
                f"{stats.deleted:,} deleted, {stats.unchanged:,} unchanged"
            )
        lines.append(
            f"  people (assignees): {self.people.inserted:,} inserted, {self.people.updated:,} updated, "
            f"{self.people.unchanged:,} unchanged"
        )
        lines.append(
            f"  members: {_count(self.members.boards, 'board')} refreshed, {self.members.failed_boards:,} failed; "
            f"{_count(self.members.members, 'member')}"
        )
        lines.append(
            f"  pictures: {self.pictures.fetched:,} fetched, {self.pictures.unchanged:,} unchanged, "
            f"{self.pictures.failed:,} failed"
        )
        lines.append(f"  requests: {self.requests:,} in {duration_s:.1f} s")
        if self.warnings:
            lines.append("  warnings:")
            lines.extend(f"    - {warning}" for warning in self.warnings)
        else:
            lines.append("  warnings: none")
        return "\n".join(lines)


def run_sync(
    client: JiraClient,
    store: Store,
    fields: FieldsConfig,
    *,
    projects: list[str],
    mode: Literal["full", "incremental"],
    now: Callable[[], datetime] = utcnow,
    overlap: timedelta = timedelta(minutes=5),
    pictures: PictureFetcher | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SyncResult:
    """Sync the projects into the store. See the module docstring. `pictures` fetches the people's
    pictures afterwards (tests give it a stub transport); without it the run makes its own. `clock`
    measures the members step's budget."""
    if mode not in ("full", "incremental"):
        raise ValueError(f"mode must be 'full' or 'incremental', not {mode!r}")
    if overlap < timedelta(0):
        raise ValueError("overlap must not be negative")
    keys = list(dict.fromkeys(validate_project_keys(projects)))
    if not keys:
        raise ValueError("no Jira projects to sync")

    cursor_time = now()
    run = store.start_run("sync", started_at=cursor_time)
    requests_before = client.request_count
    sync = _Sync(client, store, fields, seen_at=run.started_at, projects=keys)
    modes: dict[str, str] = {}
    account_tz: str | None = None
    _log().info("sync_started", run_id=run.run_id, mode=mode, projects=keys)
    try:
        account_tz = _account_zone(client.myself())
        cursors = {key: _cursor_of(store, key) for key in keys}
        for key in keys:
            modes[key] = sync.effective_mode(key, mode, cursors[key], account_tz)
        incremental = {
            key: cursor
            for key, cursor in cursors.items()
            if modes[key] == "incremental" and cursor is not None
        }
        for key in keys:
            cursor = incremental.get(key)
            sync.sync_project(key, None if cursor is None else cursor - overlap, account_tz)
        if incremental:
            sync.sync_worklog_feed(min(incremental.values()) - overlap)
        with store.transaction():
            for key in keys:
                store.set_cursor(
                    key, last_synced_at=to_iso(cursor_time), run_id=run.run_id, account_timezone=account_tz
                )
    except BaseException as exc:
        # BaseException: even Ctrl-C leaves a failed run, not one stuck at "running".
        stats = sync.stats(mode, modes, account_tz, client.request_count - requests_before)
        stats.update(error=str(exc), error_type=type(exc).__name__)
        store.finish_run(run, status="failed", stats=stats, finished_at=now())
        _log().error("sync_failed", run_id=run.run_id, error_type=type(exc).__name__, error=str(exc))
        raise

    try:
        sync.refresh_members(keys, clock)
        sync.refresh_pictures(keys, pictures, now)
    except BaseException:
        # Ctrl-C while the members or pictures were fetched: the sync itself is done and its cursors are set.
        requests = client.request_count - requests_before
        store.finish_run(run, status="succeeded", stats=sync.stats(mode, modes, account_tz, requests), finished_at=now())
        raise
    finished = now()
    requests = client.request_count - requests_before
    result = SyncResult(
        run_id=run.run_id,
        requested_mode=mode,
        project_modes=modes,
        tables=sync.tables,
        issues_seen=sync.issues_seen,
        issues_deleted=sync.issues_deleted,
        worklogs_from_feed=sync.worklogs_from_feed,
        requests=requests,
        started_at=run.started_at,
        finished_at=to_iso(finished),
        warnings=sync.warnings,
        people=sync.people,
        members=sync.members,
        pictures=sync.pictures,
    )
    stats = sync.stats(mode, modes, account_tz, requests)
    store.finish_run(run, status="succeeded", stats=stats, finished_at=finished)
    _log().info(
        "sync_finished",
        run_id=run.run_id,
        rows_written=result.rows_written,
        issues_seen=result.issues_seen,
        requests=requests,
    )
    return result


@dataclass
class _IssueWrite:
    """Everything one issue of a search page writes, fetched and mapped up front."""

    row: dict[str, Any]
    person: dict[str, Any] | None  # the assignee's people row; None when unassigned
    stored_key: str | None  # the key this issue id is stored under now, if any
    status_changes: list[dict[str, Any]]
    field_changes: list[dict[str, Any]]
    links: list[dict[str, Any]]
    worklogs: list[dict[str, Any]] | None  # None: keep the stored worklogs


class _Sync:
    """One run's work and tallies: write counts, warnings, issues seen."""

    def __init__(
        self, client: JiraClient, store: Store, fields: FieldsConfig, *, seen_at: str, projects: list[str]
    ) -> None:
        self.client = client
        self.store = store
        self.fields = fields
        self.seen_at = seen_at  # the run's start: when this run saw each assignee
        self.projects = list(projects)  # the boards this run syncs: its stats name them, so health reads each board's
        self.tables = {table: WriteStats() for table in SYNC_TABLES}
        self.people = WriteStats()
        self.members = MemberStats()
        self.pictures = PictureStats()
        self.warnings: list[str] = []
        self.issues_seen = 0
        self.issues_deleted = 0
        self.worklogs_from_feed = 0
        self.bulk_changelog = True
        self.search_fields = fields.search_fields()
        self.changelog_fields = list(fields.tracked_changelog_fields())

    # --- bookkeeping ---------------------------------------------------------------

    def warn(self, event: str, message: str, **details: Any) -> None:
        self.warnings.append(message)
        _log().warning(event, **details)

    @contextmanager
    def writing(self) -> Iterator[dict[str, WriteStats]]:
        """One transaction; its write counts join the run's once it commits."""
        counts = {table: WriteStats() for table in (*SYNC_TABLES, "people")}
        with self.store.transaction():
            yield counts
        self.people += counts.pop("people")
        for table, stats in counts.items():
            self.tables[table] += stats

    def stats(
        self, requested_mode: str, modes: dict[str, str], account_tz: str | None, requests: int
    ) -> dict[str, Any]:
        return {
            "requested_mode": requested_mode,
            "projects": list(self.projects),
            "project_modes": dict(modes),
            "account_timezone": account_tz,
            "issues_seen": self.issues_seen,
            "issues_deleted": self.issues_deleted,
            "worklogs_from_feed": self.worklogs_from_feed,
            "requests": requests,
            "rows_written": sum(stats.written for stats in self.tables.values()),
            "tables": {table: stats.as_dict() for table, stats in self.tables.items()},
            "people": self.people.as_dict(),
            "members": self.members.as_dict(),
            "pictures": self.pictures.record(),
            "warnings": list(self.warnings),
        }

    def effective_mode(
        self, project: str, requested: str, cursor: datetime | None, account_tz: str | None
    ) -> str:
        if requested == "full":
            return "full"
        if cursor is None:
            self.warn(
                "sync_full_fallback",
                f"{project}: no earlier successful sync, so it was read in full",
                project=project,
                reason="no cursor",
            )
            return "full"
        if account_tz is None:
            self.warn(
                "sync_full_fallback",
                f"{project}: Jira did not report the account's time zone, so it was read in full",
                project=project,
                reason="unknown account time zone",
            )
            return "full"
        return "incremental"

    # --- one project -------------------------------------------------------------------

    def sync_project(self, project: str, since: datetime | None, account_tz: str | None) -> None:
        """Statuses, then every page of the search; `since` None means a full read."""
        statuses = map_statuses(project, self.client.project_statuses(project))
        with self.writing() as counts:
            counts["statuses"] += self.store.replace_rows("statuses", {"project_key": project}, statuses)

        jql = f'project = "{project}"'
        if since is not None:
            jql += f' AND updated >= "{jql_datetime(since, account_tz)}"'
        jql += " ORDER BY updated ASC"
        full = since is None
        seen: set[str] = set()
        for issues in self.client.search_jql(jql, self.search_fields):
            seen.update(self._sync_page(issues, full=full))
        self.issues_seen += len(seen)

        gone: list[str] = []
        if full:
            # Every page arrived, so a stored issue the search didn't return was
            # deleted in Jira or moved to another project.
            gone = sorted(self.store.issue_keys(project) - seen)
            if gone:
                with self.writing() as counts:
                    for issue_key in gone:
                        self._delete_issue(issue_key, counts)
                self.issues_deleted += len(gone)
        _log().info(
            "sync_project",
            project=project,
            mode="full" if full else "incremental",
            issues=len(seen),
            deleted=len(gone),
        )

    def _sync_page(self, issues: list[dict[str, Any]], *, full: bool) -> list[str]:
        """Fetch and map what one page of issues needs, then write it in one transaction."""
        pairs = [(issue, map_issue(issue, self.fields)) for issue in issues]
        histories = self._changelogs({row["issue_id"]: row["issue_key"] for _, row in pairs})
        planned: list[_IssueWrite] = []
        for issue, row in pairs:
            key = row["issue_key"]
            stored_key = self._stored_key(row["issue_id"])
            changes = histories.get(key, [])
            worklogs = None
            if full or stored_key != key:
                worklogs = self._issue_worklogs(key, row["time_spent_s"])
            planned.append(
                _IssueWrite(
                    row=row,
                    person=map_assignee(issue, self.seen_at),
                    stored_key=stored_key,
                    status_changes=map_status_changes(key, changes),
                    field_changes=map_field_changes(key, changes, self.fields),
                    links=map_issue_links(issue) + map_remote_links(key, self.client.remote_links(key)),
                    worklogs=worklogs,
                )
            )

        with self.writing() as counts:
            for write in planned:
                key = write.row["issue_key"]
                if write.stored_key is not None and write.stored_key != key:
                    _log().info("issue_rekeyed", old_key=write.stored_key, new_key=key)
                    self._delete_issue(write.stored_key, counts)
                counts["issues"] += self.store.upsert_issue(write.row)
                if write.person is not None:
                    # Pages come oldest update first, so the latest issue's display name wins.
                    counts["people"].add(self.store.upsert("people", write.person))
                counts["status_changes"] += self.store.upsert_many("status_changes", write.status_changes)
                counts["field_changes"] += self.store.upsert_many("field_changes", write.field_changes)
                counts["links"] += self.store.replace_rows("links", {"issue_key": key}, write.links)
                if write.worklogs is not None:
                    worklogs = self.store.replace_rows("worklogs", {"issue_key": key}, write.worklogs)
                    counts["worklogs"] += worklogs
        return [write.row["issue_key"] for write in planned]

    def _changelogs(self, keys_by_id: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        """Each issue's changelog histories, by issue key."""
        if self.bulk_changelog:
            try:
                return self._bulk_changelogs(keys_by_id)
            except JiraError as exc:
                if exc.status_code not in _NO_BULK_FETCH:
                    raise
                self.bulk_changelog = False
                self.warn(
                    "bulk_changelog_unavailable",
                    f"bulk changelog fetch answered HTTP {exc.status_code}: "
                    "changelogs were read one issue at a time",
                    status=exc.status_code,
                )
        return {key: self.client.issue_changelog(key) for key in keys_by_id.values()}

    def _bulk_changelogs(self, keys_by_id: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        histories: dict[str, list[dict[str, Any]]] = {key: [] for key in keys_by_id.values()}
        # One issue's histories can come in several entries (pages, field chunks).
        for entry in self.client.bulk_changelog(list(keys_by_id), self.changelog_fields):
            key = keys_by_id.get(str(_id_of(entry, "issueId", "a bulk changelog entry")))
            changes = entry.get("changeHistories")
            if key is not None and isinstance(changes, list):
                histories[key].extend(changes)
        return histories

    def _issue_worklogs(self, issue_key: str, time_spent_s: int | None) -> list[dict[str, Any]]:
        if not time_spent_s:
            # No time logged means no worklogs, so there is nothing to ask Jira for.
            return []
        return [map_worklog(issue_key, worklog) for worklog in self.client.issue_worklogs(issue_key)]

    def _stored_key(self, issue_id: str) -> str | None:
        row = self.store.query_one("SELECT issue_key FROM issues WHERE issue_id = ?", (issue_id,))
        return None if row is None else row["issue_key"]

    def _delete_issue(self, issue_key: str, counts: dict[str, WriteStats]) -> None:
        """store.delete_issue, with the rows it removes counted per synced table."""
        for table in _ISSUE_TABLES:
            counts[table].deleted += self.store.count(table, {"issue_key": issue_key})
        self.store.delete_issue(issue_key)

    # --- members (step 6) ----------------------------------------------------------------

    def refresh_members(self, projects: list[str], clock: Callable[[], float]) -> None:
        """Each board's members, a board at a time, once Jira has been asked whether it lists more accounts on the
        site than the members read can scan (a warning when it does). Whatever goes wrong with that question is a
        log line, whatever goes wrong with one board is a warning, its stored members stay, and the next board is
        read all the same: never the sync's failure. Each board has at most an equal share of the time left."""
        deadline = clock() + members_budget_s(len(projects))
        if self._site_over_scan_limit(deadline, clock):
            self.warn(
                "board_members_site_over_scan_limit",
                f"Jira lists more than {ASSIGNABLE_SCAN_LIMIT:,} accounts on this site (people, apps and former "
                f"users), and the board members read can scan only the first {ASSIGNABLE_SCAN_LIMIT:,} of them, so "
                "some people may be missing from the @ list",
                scan_limit=ASSIGNABLE_SCAN_LIMIT,
            )
        for index, project in enumerate(projects):
            now = clock()
            if now >= deadline:
                self._members_not_refreshed(project, "time budget spent")
                continue
            # An equal share of the time left: a slow board never takes the time of the boards after it, and the
            # time a quick one leaves passes on to them.
            share = (deadline - now) / (len(projects) - index)
            try:
                users = self.client.assignable_users(project, deadline=now + share, clock=clock)
                found = [row for user in users if (row := map_member(user, self.seen_at)) is not None]
                members = [{"project_key": project, "account_id": row["account_id"], "seen_at": self.seen_at}
                           for row in found]
                with self.store.transaction():
                    self.store.replace_rows("board_members", {"project_key": project}, members)
                    for row in found:
                        # The person as Jira names them; the stored picture's columns are the pictures step's.
                        self.store.upsert("people", row)
            except Exception as exc:
                self._members_not_refreshed(project, _members_reason(exc))
                continue
            self.members.boards += 1
            self.members.members += len(members)
            _log().info("board_members_refreshed", board=project, members=len(members))

    def _site_over_scan_limit(self, deadline: float, clock: Callable[[], float]) -> bool | None:
        """The client's site_has_users_beyond_scan, given at most SITE_PROBE_BUDGET_S of the step's time. Anything
        it raises (its time spent, which is how a slow or timing-out answer ends, or a fault of its own) is logged
        and read as None, Jira not having said: never the step's failure, nor the sync's."""
        try:
            return self.client.site_has_users_beyond_scan(
                deadline=min(deadline, clock() + SITE_PROBE_BUDGET_S), clock=clock
            )
        except Exception as exc:
            _log().warning("site_user_count_unknown", reason=_members_reason(exc))
            return None

    def _members_not_refreshed(self, project: str, reason: str) -> None:
        self.members.failed_boards += 1
        self.warn(
            "board_members_not_refreshed",
            f"{project}: the board's members were not refreshed ({reason}); the stored ones stay",
            board=project,
            reason=reason,
        )

    # --- pictures (step 7) ---------------------------------------------------------------

    def refresh_pictures(
        self, projects: list[str], fetcher: PictureFetcher | None, now: Callable[[], datetime]
    ) -> None:
        """The synced boards' people's pictures. Whatever goes wrong here is a warning, never the sync's failure."""
        try:
            if fetcher is not None:
                self.pictures = refresh_pictures(self.store, fetcher, projects=projects, now=now)
            else:
                with PictureFetcher() as own:
                    self.pictures = refresh_pictures(self.store, own, projects=projects, now=now)
        except Exception as exc:
            self.warn(
                "pictures_not_refreshed",
                f"the people's pictures were not refreshed ({type(exc).__name__}); the stored ones stay",
                error_type=type(exc).__name__,
            )

    # --- worklog feed --------------------------------------------------------------------

    def sync_worklog_feed(self, since: datetime) -> None:
        """Worklogs updated or deleted since `since`, for issues in the snapshot."""
        since_ms = epoch_ms(since)
        worklogs = self.client.worklog_list(_worklog_ids(self.client.worklog_updated(since_ms), "updated"))
        deleted = _worklog_ids(self.client.worklog_deleted(since_ms), "deleted")
        # The feeds cover every project the account can see: keep stored issues only.
        keys_by_id = self.store.issue_id_map()
        rows: list[dict[str, Any]] = []
        for worklog in worklogs:
            issue_key = keys_by_id.get(str(_id_of(worklog, "issueId", "a listed worklog")))
            if issue_key is not None:
                rows.append(map_worklog(issue_key, worklog))
        with self.writing() as counts:
            counts["worklogs"] += self.store.upsert_many("worklogs", rows)
            # After the upserts, so a worklog updated and then deleted ends up deleted.
            for worklog_id in deleted:
                gone = self.store.delete_rows("worklogs", {"worklog_id": str(worklog_id)})
                counts["worklogs"].deleted += gone
        self.worklogs_from_feed += len(rows)
        _log().info(
            "sync_worklog_feed",
            since=to_iso(since),
            updated=len(worklogs),
            kept=len(rows),
            deleted=len(deleted),
        )


# --- helpers ----------------------------------------------------------------------------


def _log() -> Any:
    # Looked up per call: a logger bound at import would miss configure_logging().
    return get_logger("sync")


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}" if number == 1 else f"{number:,} {noun}s"


def _members_reason(exc: Exception) -> str:
    """Why a board's members were not refreshed, or the site's size not learnt, in a few words: never the error's
    text, which may quote Jira's answer, and so a name or an address."""
    if isinstance(exc, JiraBudgetSpent):
        return "time budget spent"
    if isinstance(exc, JiraUnavailable):
        return "no answer from Jira" if exc.status_code is None else f"HTTP {exc.status_code} after retries"
    if isinstance(exc, JiraError):
        status = exc.status_code
        return f"HTTP {status}" if status is not None and not 200 <= status < 300 else "unexpected payload"
    if isinstance(exc, sqlite3.Error):
        return f"not stored ({type(exc).__name__})"
    return f"unexpected {type(exc).__name__}"


def _account_zone(me: dict[str, Any]) -> str | None:
    """The account's IANA zone from /myself, or None when it is missing or unknown."""
    name = me.get("timeZone")
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        zone(name.strip())
    except ConfigError:
        return None
    return name.strip()


def _cursor_of(store: Store, project: str) -> datetime | None:
    """When the project's last successful sync started; None if it has none."""
    cursor = store.get_cursor(project)
    if cursor is None or not cursor.last_synced_at:
        return None
    try:
        return parse_ts(cursor.last_synced_at)
    except (TypeError, ValueError):
        # Treated as missing, so the project is read in full and the cursor rewritten.
        _log().warning("sync_cursor_unreadable", project=project)
        return None


def _worklog_ids(entries: list[Any], feed: str) -> list[int | str]:
    """The worklogId of every feed entry, as Jira sent it (worklog/list documents ints)."""
    return [_id_of(entry, "worklogId", f"an entry of the {feed} worklog feed") for entry in entries]


def _id_of(entry: Any, name: str, what: str) -> int | str:
    """entry[name], the id an entry is filed under. Without it the payload is not
    the documented shape, so JiraError stops the run rather than drop data."""
    value = entry.get(name) if isinstance(entry, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or value == "":
        raise JiraError(f"unexpected Jira payload: {what} without {name}")
    return value
