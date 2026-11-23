"""`python -m oversight demo`: a synthetic project in the snapshot, so the whole
pipeline (derive -> rules -> digest -> ask -> insights -> api) runs with no
Jira access and no API key. Also the fallback for a client demo.

CONTRACT (stub; the demo build implements it).

seed_demo(store, *, now, seed=20261104, project="DEMO", issues=1150, reset=False) -> DemoStats
- Deterministic: random.Random(seed); the same inputs give byte-identical rows.
  Every timestamp is an offset from `now`, so the data is always "recent".
- Writes through Store.upsert / replace_rows inside one transaction: statuses
  (To Do / In Progress / Done with categories), issues (with raw_json holding a
  realistic sprint object list under customfield_10020 and a story-point value
  under customfield_10016, matching config/fields.yaml), status_changes,
  field_changes (story_points, assignee), worklogs (all by one app account id
  "app-tempo"), links (a few issue links of types blocks / relates to), people
  (each assignee's display name, as a sync stores them; two of them, PICTURED,
  with a picture as a sync stores a fetched one), board_members (the
  project's members, as a sync's member fetch stores them: every assignee and
  three people who hold no ticket, MEMBERS), and sync_state for the project
  (last_synced_at = now, account_timezone "Europe/London").
  reset=True deletes the project's rows first.
- The story (mirrors the UI's sample data so the two agree in spirit):
  about 14 epics with domain-neutral names ("Onboarding flow", "Reporting
  exports", ...); types Task 60%, Bug 20%, Subtask 12%, Test 5%, Epic 3%;
  three-status workflow; story points on about 65% (fibonacci-ish values),
  original estimates on none; logged time on about 85% of done work; about
  16% of done issues went straight To Do -> Done; effort ratios with a
  realistic spread (median about 1.0, long tail to 3×), two epics drifting
  upwards in the last 5 weeks; a process change about 5 weeks ago after which
  the skipped-stage share falls; a few reopens; some unassigned in-progress
  issues; an active sprint ending in 2 days with ~35% not started; about 10%
  of open issues older than the project's p90 in their status.
- The people have habits (JO-11), so the person scorecards show every state
  over their default 90 days: Ada Brooks estimates before she starts and
  Elif Aydin keeps within the estimate (closer to the process than the
  board), Finn Gallagher logs time less, Dev Malhotra's work is reopened
  more and Carmen Ruiz's open work sits longer (further from it), Ben Okafor
  and Grace Lindqvist take the other side of each, and Hana Mori, part time
  of late, holds too few recent tickets to compare. Work only changes hands
  (in pairs, after every draw), so no figure of the board's changes. DEMO's
  workflow has no stage between In Progress and Done, and Jira records no
  code review there, so two measures are not measurable; the three members
  below hold no tickets.
- Assignee ids are opaque ("acc-01".."acc-08") and carry fictional display
  names (Ada Brooks to Hana Mori, ASSIGNEE_NAMES), so the dashboard can show
  people by name; no email address, picture URL or real name anywhere. Two of
  them (acc-01 and acc-05) have a small abstract picture drawn in code (picture:
  a pattern, not a photo of anyone), stored directly with no fetch, so the
  dashboard shows both a picture and the initials the other six get.
- Three more fictional accounts are the board's members with no tickets
  (acc-09 Noor Haddad, acc-10 Tomás Ferreira, acc-11 Yuki Tanabe: MEMBER_NAMES,
  no pictures), so Ask's @ list shows its second group and Ask's answer for
  someone with no tickets can be tried. They come from user objects shaped as
  Jira's assignable-user search returns them, through the sync's own mapping
  (mapping.map_member), with no Jira call; the list also holds the worklogs'
  app account, which that mapping keeps out, as it keeps out every app,
  customer and inactive account.
DemoStats: counts per table and the sprint ids; render() prints them.
After seeding, the CLI prints the next commands (derive, rules, digest, ask).

Implementation notes
- The story is planned first (_Story: pure draws from the seed, no I/O), then
  rendered as Jira-shaped payloads. The `issues` rows come from
  oversight.jira.mapping.map_issue over the payload kept in raw_json, and the
  `links` rows from map_issue_links over the same payload, so a row and its
  audit payload can never disagree and the demo takes the same path as a sync.
- 14 epics is the story's number; at 1,150 issues that is 1.2% of issues, not
  the 3% the type mix names, so the other four types share the remainder.
  Smaller demos get one epic per 20 issues.
- Issue ids are numeric and unique per project key, so two demo projects can
  share one database.
- reset=True removes everything the project left behind: its issues and their
  child rows (status changes, field changes, worklogs, links, status intervals,
  metrics), statuses, sync_state, findings, rule_status, warning_links,
  digests, insights and its board_members rows. Runs, qa_log and llm_calls are
  audit trails and stay; people are accounts, not the project's, and stay too.
- Each seeding is recorded as a run of kind "demo", like every other command.
"""
from __future__ import annotations

import hashlib
import math
import random
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from oversight.config import FieldRef, FieldsConfig
from oversight.errors import ConfigError
from oversight.ingest.avatars import version_of
from oversight.jira.mapping import map_assignee, map_issue, map_issue_links, map_member
from oversight.logs import get_logger
from oversight.store.db import ISSUE_CHILD_TABLES, Store
from oversight.timeutil import to_iso, zone

log = get_logger("demo")

# --- the site the demo pretends to be --------------------------------------------------------

ACCOUNT_TIMEZONE = "Europe/London"
WORKLOG_AUTHOR = "app-tempo"
# The field ids in config/fields.yaml (and its test copy); the ids a sync of AIL stores under.
POINTS_FIELD = "customfield_10016"
SPRINT_FIELD = "customfield_10020"
DEMO_FIELDS = FieldsConfig(
    fields={
        "story_points": FieldRef(
            id=POINTS_FIELD, name="Story point estimate", custom_type="com.pyxis.greenhopper.jira:jsw-story-points"
        ),
        "sprint": FieldRef(id=SPRINT_FIELD, name="Sprint", custom_type="com.pyxis.greenhopper.jira:gh-sprint"),
    }
)
# Opaque account ids, each with a fictional display name: the dashboard names assignees
# by display name, with their picture beside it, never by account id or email address.
ASSIGNEES: tuple[str, ...] = tuple(f"acc-{n:02d}" for n in range(1, 9))
ASSIGNEE_NAMES: dict[str, str] = dict(
    zip(
        ASSIGNEES,
        ("Ada Brooks", "Ben Okafor", "Carmen Ruiz", "Dev Malhotra", "Elif Aydin", "Finn Gallagher",
         "Grace Lindqvist", "Hana Mori"),
        strict=True,
    )
)
# The board's members who hold none of its tickets (JO-10): fictional too, and with no picture. With the
# assignees, they are everyone Jira would let DEMO's tickets be assigned to.
MEMBERS: tuple[str, ...] = ("acc-09", "acc-10", "acc-11")
MEMBER_NAMES: dict[str, str] = dict(zip(MEMBERS, ("Noor Haddad", "Tomás Ferreira", "Yuki Tanabe"), strict=True))
# Busier accounts first: a team never shares its work evenly.
_ASSIGNEE_WEIGHTS = (5, 5, 4, 4, 3, 3, 2, 2)
# The accounts with a picture: a pattern drawn in code (picture()), so the dashboard shows a picture
# for these two and initials for the other six. Nothing is fetched.
PICTURED: tuple[str, ...] = ("acc-01", "acc-05")
_PICTURE_SIZE = 48  # pixels a side, as Jira's avatarUrls["48x48"]
_PICTURE_GROUND = (0xEE, 0xF0, 0xF3)
_PICTURE_INKS = ((0x2F, 0x6E, 0xA8), (0x7A, 0x4E, 0xA0), (0x2E, 0x7D, 0x5F), (0xA8, 0x62, 0x2A), (0x9C, 0x45, 0x5A))
_SITE = "https://demo.invalid"  # .invalid is reserved for exactly this (RFC 2606)

TO_DO, IN_PROGRESS, DONE = "To Do", "In Progress", "Done"
# status name -> (status id, category key, category id)
_STATUSES: dict[str, tuple[str, str, int]] = {
    TO_DO: ("10001", "new", 2),
    IN_PROGRESS: ("10002", "indeterminate", 4),
    DONE: ("10003", "done", 3),
}
# issue type name -> (issue type id, hierarchy level); an epic is level 1, a subtask -1
_ISSUE_TYPES: dict[str, tuple[str, int]] = {
    "Epic": ("10000", 1),
    "Task": ("10001", 0),
    "Subtask": ("10003", -1),
    "Bug": ("10004", 0),
    "Test": ("10005", 0),
}
# link type name -> (link type id, inward text, outward text)
_LINK_TYPES: dict[str, tuple[str, str, str]] = {
    "Blocks": ("10000", "is blocked by", "blocks"),
    "Relates": ("10003", "relates to", "relates to"),
}

EPIC_NAMES: tuple[str, ...] = (
    "Onboarding flow",
    "Reporting exports",
    "Search improvements",
    "Notification settings",
    "Access control",
    "Audit trail",
    "Data import",
    "Performance tuning",
    "Mobile layout",
    "Billing summary",
    "API versioning",
    "Error handling",
    "Localisation",
    "Dashboard widgets",
)
# Days before `now` each epic was created: most at the start, a few added as the work grew.
_EPIC_AGES_DAYS = (181, 180, 179, 178, 177, 176, 175, 174, 150, 125, 100, 75, 50, 25)
# Effort in these epics (indices into EPIC_NAMES) drifts upwards in the last five
# weeks, and recent work concentrates there, as it does on the epics a team is busy with.
_DRIFTING_EPICS = (2, 5)
_DRIFT_FACTOR = 1.7
_DRIFT_WEIGHT = 3

# Summary vocabulary: domain-neutral, lower-case phrases, so nothing reads as a
# person's name and nothing imitates a real ticket.
_ACTIONS = ("Add", "Update", "Refactor", "Document", "Migrate", "Simplify", "Implement", "Extend", "Tidy", "Automate")
_OBJECTS = (
    "export filters",
    "the login form",
    "the audit log view",
    "retry handling",
    "the search index",
    "cache warm-up",
    "permission checks",
    "date parsing",
    "pagination",
    "the settings page",
    "the import wizard",
    "the weekly summary",
    "column sorting",
    "the empty state",
    "keyboard shortcuts",
    "the progress indicator",
    "error messages",
    "the CSV layout",
    "the mobile menu",
    "session handling",
    "the archive job",
    "the print layout",
    "currency rounding",
    "the health check",
)
_SYMPTOMS = (
    "wrong totals",
    "empty result",
    "slow response",
    "stale cache",
    "broken link",
    "missing validation",
    "timeout on save",
    "duplicate rows",
    "off-by-one in paging",
    "overlapping layout",
)
_SUBTASK_ACTIONS = ("Write unit tests for", "Wire up", "Update the docs for", "Review", "Clean up", "Add logging to")
_TEST_FORMS = ("Regression test for", "Smoke test for", "Edge cases for", "Test plan for")

# --- the story, as shares and typical durations -----------------------------------------------

HOUR = timedelta(hours=1)
_HISTORY = timedelta(days=182)  # the project's age: enough closed work for 180-day thresholds
_PROCESS_CHANGE = timedelta(days=35)  # "about 5 weeks ago": skips fall, two epics start to drift
_SPRINT_LENGTH = timedelta(days=14)
_ACTIVE_SPRINT_ENDS_IN = timedelta(hours=44)  # "in 2 days", and inside W-SPR-01's 2-day limit
_SPRINT_ID_BASE = 4400
_CHANGELOG_ID_BASE = 100_001
_WORKLOG_ID_BASE = 70_001

_TYPE_WEIGHTS = {"Task": 60, "Bug": 20, "Subtask": 12, "Test": 5}
_STATUS_WEIGHTS = {DONE: 64, IN_PROGRESS: 11, TO_DO: 25}
_POINT_VALUES = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0)
_POINT_WEIGHTS = (6, 14, 22, 22, 18, 12, 6)
_HOURS_PER_POINT = 8  # process.yaml: DEMO.estimate.hours_per_point
_UNPOINTED_EFFORT_H = 8.0  # typical effort on work nobody estimated

_POINTS_SHARE = 0.65
_RATIO_SIGMA = 0.4  # effort / estimate is log-normal around 1.0: p90 about 1.7×, a few over 3×
_LOGGED_DONE_SHARE = 0.85
_LOGGED_IN_PROGRESS_SHARE = 0.70
_SKIP_SHARE_BEFORE = 0.175  # To Do -> Done before the process change ...
_SKIP_SHARE_AFTER = 0.07  # ... and after it: about 16% of done work over the whole history
_REOPEN_SHARE = 0.03
_REOPEN_IN_PROGRESS_SHARE = 0.015
_STALE_SHARE = 0.10  # open issues sitting far longer than the project's own history
_ASSIGNED_SHARE = {TO_DO: 0.55, IN_PROGRESS: 0.94, DONE: 0.97}  # by status; the rest is unassigned
_EPIC_ASSIGNED_SHARE = 0.5
_REASSIGNED_SHARE = 0.08
_POINTS_AT_CREATION_SHARE = 0.08
_POINTS_AFTER_START_SHARE = 0.12  # the estimate describes the work instead of predicting it
_RE_ESTIMATED_SHARE = 0.06
_UNDER_EPIC_SHARE = 0.85
_NOT_STARTED_SHARE = 0.38  # of the active sprint's issues
_FUTURE_SPRINT_SHARE = 0.05  # of the remaining backlog, planned into the next sprint
_LINK_PAIRS_PER_ISSUES = 40
# The people's habits (_Story._habits), over the person scorecards' default period (oversight/api/scorecard.py).
_HABITS_WINDOW = timedelta(days=90)
_PART_TIMER = "acc-08"  # Hana Mori, part time of late
_PART_TIMER_KEEPS = (3, 2)  # the recent tickets she keeps: done, and still open
_PARTNERS = ("acc-02", "acc-07")  # Ben Okafor and Grace Lindqvist: no habit of their own; the other side of each


@dataclass
class DemoStats:
    project: str
    issues: int
    status_changes: int
    field_changes: int
    worklogs: int
    links: int
    sprints: list[int] = field(default_factory=list)

    def render(self) -> str:
        """A short human summary: rows per table and the sprint ids."""
        lines = [
            f"demo project {self.project}: {self.issues:,} issues, {self.status_changes:,} status changes, "
            f"{self.field_changes:,} field changes, {self.worklogs:,} worklogs, {self.links:,} links"
        ]
        if self.sprints:
            lines.append(f"  sprints ({len(self.sprints)}): {', '.join(str(s) for s in self.sprints)}")
        return "\n".join(lines)


def seed_demo(
    store: Store,
    *,
    now: datetime,
    seed: int = 20261104,
    project: str = "DEMO",
    issues: int = 1150,
    reset: bool = False,
) -> DemoStats:
    """Seed the synthetic project (see the module docstring) and say what was written."""
    if now.tzinfo is None:
        raise ValueError("now must be an aware datetime")
    if issues < 1:
        raise ConfigError("--issues must be at least 1")
    clock = time.perf_counter()
    planned, sprints, links = _Story(random.Random(seed), now, project, issues).plan()
    tables = _render(project, now, planned, sprints, links)
    stats = DemoStats(
        project=project,
        issues=len(tables.issues),
        status_changes=sum(len(issue.status_changes) for issue in tables.issues),
        field_changes=sum(len(issue.field_changes) for issue in tables.issues),
        worklogs=sum(len(issue.worklogs) for issue in tables.issues),
        links=sum(len(issue.links) for issue in tables.issues),
        sprints=[sprint.sprint_id for sprint in sprints],
    )
    run_stats: dict[str, Any] = {"project": project, "seed": seed, "reset": reset}

    # The run record lives outside the data transaction, so a failed seeding leaves its failed run behind.
    run = store.start_run("demo", started_at=now)
    try:
        with store.transaction():
            deleted = _delete_project(store, project) if reset else 0
            _write(store, project, tables)
            _store_members(store, project, to_iso(now))
            _store_pictures(store, to_iso(now))
            store.set_cursor(
                project, last_synced_at=to_iso(now), run_id=run.run_id, account_timezone=ACCOUNT_TIMEZONE
            )
    except BaseException as exc:
        run_stats["error"] = f"{type(exc).__name__}: {exc}"
        store.finish_run(run, status="failed", stats=run_stats)
        log.error("demo_failed", run_id=run.run_id, project=project, error=run_stats["error"])
        raise

    elapsed = round(time.perf_counter() - clock, 3)
    run_stats.update(
        issues=stats.issues,
        status_changes=stats.status_changes,
        field_changes=stats.field_changes,
        worklogs=stats.worklogs,
        links=stats.links,
        sprints=stats.sprints,
        rows_deleted=deleted,
        elapsed_s=elapsed,
    )
    store.finish_run(run, status="succeeded", stats=run_stats)
    log.info("demo_seeded", run_id=run.run_id, project=project, issues=stats.issues, elapsed_s=elapsed)
    return stats


# --- planning: the story, drawn from the seed --------------------------------------------------


@dataclass
class _Issue:
    """One planned issue. Times are aware UTC datetimes until the rows are rendered."""

    issue_type: str
    created: datetime
    status: str
    transitions: list[tuple[datetime, str, str]]  # (at, from, to), in time order; the chain is unbroken
    stale: bool = False
    ratio: float = 1.0  # effort / estimate drawn for this issue
    points: float | None = None
    number: int = 0  # key number, assigned in creation order
    parent: int | None = None  # key number of the parent: an epic, or a task for a subtask
    epic: int | None = None  # key number of the epic (tasks, bugs and tests only)
    assignee: str | None = None
    assignee_changes: list[tuple[datetime, str | None, str]] = field(default_factory=list)
    points_changes: list[tuple[datetime, str | None, str]] = field(default_factory=list)
    worklogs: list[tuple[datetime, int]] = field(default_factory=list)  # (started, seconds)
    sprints: list[int] = field(default_factory=list)
    summary: str = ""

    @property
    def started(self) -> datetime | None:
        """First entry into In Progress."""
        return next((at for at, _from, to in self.transitions if to == IN_PROGRESS), None)

    @property
    def resolved(self) -> datetime | None:
        """When the issue reached its current Done status; None while it is open."""
        if self.status != DONE or not self.transitions:
            return None
        return self.transitions[-1][0]

    def work_windows(self, until: datetime) -> list[tuple[datetime, datetime]]:
        """When work could have been logged: the In Progress intervals, or the
        whole life of an issue that never went through In Progress."""
        windows: list[tuple[datetime, datetime]] = []
        start: datetime | None = None
        for at, _from, to in self.transitions:
            if to == IN_PROGRESS:
                start = at
            elif start is not None:
                windows.append((start, at))
                start = None
        if start is not None:
            windows.append((start, until))
        return windows or [(self.created, self.resolved or until)]


@dataclass(frozen=True)
class _Sprint:
    sprint_id: int
    name: str
    state: str  # closed | active | future
    start: datetime
    end: datetime


# (link type name, source key number, target key number): source <outward> target
_Link = tuple[str, int, int]


class _Story:
    """Plans one project's issues from the seed and `now`. Pure draws: no I/O."""

    def __init__(self, rng: random.Random, now: datetime, project: str, count: int) -> None:
        self.rng = rng
        self.now = now
        self.project = project
        self.count = count
        self.floor = now - _HISTORY  # nothing happened before the project began
        self.epic_names: dict[int, str] = {}  # epic key number -> name
        self.drifting: set[int] = set()  # epic key numbers whose effort drifts

    def plan(self) -> tuple[list[_Issue], list[_Sprint], list[_Link]]:
        epics = self._epics(min(len(EPIC_NAMES), self.count // 20))
        issues = epics + [self._work_item() for _ in range(self.count - len(epics))]
        issues.sort(key=lambda issue: issue.created)  # keys follow creation order, as in Jira
        for number, issue in enumerate(issues, start=1):
            issue.number = number
        self.epic_names = {epic.number: epic.summary for epic in epics}
        drifting_names = {EPIC_NAMES[i] for i in _DRIFTING_EPICS}
        self.drifting = {number for number, name in self.epic_names.items() if name in drifting_names}
        self._place_in_hierarchy(issues)
        for issue in issues:
            self._estimate(issue)
            self._assign(issue)
            self._log_time(issue)
            if issue.issue_type != "Epic":
                issue.summary = self._summary(issue)
        # After every draw: who holds what changes, and nothing else (the seed's stream is untouched).
        self._habits(issues)
        sprints = self._sprints(issues)
        return issues, sprints, self._links(issues)

    # --- the people's habits (JO-11) -------------------------------------------------------------------

    def _habits(self, issues: list[_Issue]) -> None:
        """Each person's way of working, so the person scorecards show every state over their default period: Ada
        Brooks estimates before she starts and Elif Aydin keeps within the estimate more than the rest (closer to the
        process), Finn Gallagher logs time less, Dev Malhotra's work is reopened more and Carmen Ruiz's open work sits
        longer (further from it), Ben Okafor and Grace Lindqvist take the other side of each, and Hana Mori, part time
        of late, holds too few recent tickets to compare. Work only changes hands, in pairs, so no figure of the
        board's changes and no draw is made: only who holds what. No real person's habits: the names are fictional."""
        since = self.now - _HABITS_WINDOW
        work = [issue for issue in issues if issue.issue_type != "Epic" and issue.assignee is not None]
        self._part_time(work, since)
        done = [issue for issue in work if issue.resolved is not None and issue.resolved >= since]
        started = [issue for issue in work if issue.started is not None and issue.started >= since]
        rated = [issue for issue in done if issue.points is not None and issue.worklogs]
        open_now = [issue for issue in work if issue.status != DONE]
        _swap(started, "acc-01", _estimated_in_time, closer=True)  # Ada Brooks
        _swap(rated, "acc-05", lambda issue: not _over_effort(issue), closer=True)  # Elif Aydin
        _swap(done, "acc-06", lambda issue: bool(issue.worklogs), closer=False)  # Finn Gallagher
        _swap(done, "acc-04", lambda issue: not _reopened(issue), closer=False)  # Dev Malhotra
        _swap(open_now, "acc-03", lambda issue: not issue.stale, closer=False)  # Carmen Ruiz

    def _part_time(self, work: list[_Issue], since: datetime) -> None:
        """Hana Mori keeps her older work and a few recent tickets; the rest of her recent work goes to the two
        people with no habit of their own, in turn."""
        recent = [issue for issue in work if issue.assignee == _PART_TIMER
                  and (issue.status != DONE or (issue.resolved is not None and issue.resolved >= since)
                       or (issue.started is not None and issue.started >= since))]
        kept_done = [issue for issue in recent if issue.status == DONE][-_PART_TIMER_KEEPS[0]:]
        kept_open = [issue for issue in recent if issue.status != DONE][-_PART_TIMER_KEEPS[1]:]
        handed = [issue for issue in recent if issue not in kept_done and issue not in kept_open]
        for index, issue in enumerate(handed):
            _hand_to(issue, _PARTNERS[index % len(_PARTNERS)])

    # --- draws -----------------------------------------------------------------------------------

    def _weighted(self, weights: dict[str, int]) -> str:
        return self.rng.choices(list(weights), weights=list(weights.values()))[0]

    def _hours(self, median: float, sigma: float) -> timedelta:
        """A right-skewed duration: most near the median, a few much longer."""
        return timedelta(hours=self.rng.lognormvariate(math.log(median), sigma))

    def _span(self, low: timedelta, high: timedelta) -> timedelta:
        return low + (high - low) * self.rng.random()

    def _moment(self, start: datetime, end: datetime) -> datetime:
        """A uniform instant in [start, end]; start itself when the window is empty."""
        if end <= start:
            return start
        return start + (end - start) * self.rng.random()

    def _fit(self, end: datetime, durations: list[timedelta]) -> list[timedelta]:
        """Shrink durations proportionally so their chain, ending at `end`, starts after the project began."""
        total = sum(durations, timedelta())
        room = end - self.floor
        if total <= room:
            return durations
        factor = room / total * 0.9
        return [duration * factor for duration in durations]

    # --- issues and their timelines ----------------------------------------------------------------

    def _epics(self, count: int) -> list[_Issue]:
        epics = []
        for name, age in zip(EPIC_NAMES[:count], _EPIC_AGES_DAYS):
            created = self.now - timedelta(days=age) + self._span(timedelta(0), timedelta(hours=20))
            started = created + self._span(timedelta(days=1), timedelta(days=3))
            epics.append(_Issue("Epic", created, IN_PROGRESS, [(started, TO_DO, IN_PROGRESS)], summary=name))
        return epics

    def _work_item(self) -> _Issue:
        issue_type = self._weighted(_TYPE_WEIGHTS)
        status = self._weighted(_STATUS_WEIGHTS)
        points = None
        if self.rng.random() < _POINTS_SHARE:
            points = self.rng.choices(_POINT_VALUES, weights=_POINT_WEIGHTS)[0]
        ratio = self.rng.lognormvariate(0.0, _RATIO_SIGMA)
        stale = status != DONE and self.rng.random() < _STALE_SHARE
        if status == DONE:
            created, transitions = self._done_timeline(points, ratio)
        elif status == IN_PROGRESS:
            created, transitions = self._in_progress_timeline(points, ratio, stale)
        else:
            created, transitions = self._to_do_timeline(stale)
        return _Issue(issue_type, created, status, transitions, stale=stale, ratio=ratio, points=points)

    def _calendar_work(self, points: float | None, ratio: float) -> timedelta:
        """Calendar time in progress: the effort, stretched by other work, meetings and nights."""
        effort_h = (points * _HOURS_PER_POINT if points else _UNPOINTED_EFFORT_H) * ratio
        return timedelta(hours=max(1.0, effort_h * self.rng.uniform(2.0, 6.0)))

    def _done_timeline(self, points: float | None, ratio: float) -> tuple[datetime, list[tuple[datetime, str, str]]]:
        resolved = self.now - self._span(HOUR, _HISTORY - timedelta(days=4))
        skip_share = _SKIP_SHARE_AFTER if resolved > self.now - _PROCESS_CHANGE else _SKIP_SHARE_BEFORE
        if self.rng.random() < skip_share:
            # Straight from To Do to Done: the work was never tracked as in progress.
            (wait,) = self._fit(resolved, [self._hours(48, 1.0)])
            created = resolved - wait
            transitions = [(resolved, TO_DO, DONE)]
        else:
            wait, work = self._fit(resolved, [self._hours(36, 1.0), self._calendar_work(points, ratio)])
            created = resolved - work - wait
            transitions = [(created + wait, TO_DO, IN_PROGRESS), (resolved, IN_PROGRESS, DONE)]
        if self.rng.random() < _REOPEN_SHARE:
            reopened = resolved + self._span(timedelta(hours=4), timedelta(hours=120))
            closed = reopened + self._span(timedelta(hours=2), timedelta(hours=30))
            if closed < self.now - HOUR:
                transitions += [(reopened, DONE, IN_PROGRESS), (closed, IN_PROGRESS, DONE)]
        return created, transitions

    def _in_progress_timeline(
        self, points: float | None, ratio: float, stale: bool
    ) -> tuple[datetime, list[tuple[datetime, str, str]]]:
        age = self._span(timedelta(hours=600), timedelta(hours=1600)) if stale else self._hours(48, 0.8)
        started = self.now - age
        wait = self._hours(36, 1.0)
        if self.rng.random() < _REOPEN_IN_PROGRESS_SHARE:
            # Finished once, then reopened: in progress again now.
            gap = self._span(timedelta(hours=4), timedelta(hours=120))
            wait, work, gap = self._fit(started, [wait, self._calendar_work(points, ratio), gap])
            first_done = started - gap
            created = first_done - work - wait
            transitions = [
                (created + wait, TO_DO, IN_PROGRESS),
                (first_done, IN_PROGRESS, DONE),
                (started, DONE, IN_PROGRESS),
            ]
        else:
            (wait,) = self._fit(started, [wait])
            created = started - wait
            transitions = [(started, TO_DO, IN_PROGRESS)]
        return created, transitions

    def _to_do_timeline(self, stale: bool) -> tuple[datetime, list[tuple[datetime, str, str]]]:
        age = self._span(timedelta(days=20), timedelta(days=70)) if stale else self._hours(40, 0.7)
        (age,) = self._fit(self.now, [age])
        return self.now - age, []

    # --- hierarchy, estimates, people, time --------------------------------------------------------

    def _place_in_hierarchy(self, issues: list[_Issue]) -> None:
        """Epics get children; subtasks hang under earlier tasks. Issues come in creation order."""
        epics: list[int] = []
        tasks: list[int] = []
        for issue in issues:
            if issue.issue_type == "Epic":
                epics.append(issue.number)
                continue
            if issue.issue_type == "Subtask":
                if tasks:
                    issue.parent = self.rng.choice(tasks[-30:])  # recent work, as subtasks are
                    continue
                issue.issue_type = "Task"  # the first work items have nothing to hang under
            if epics and self.rng.random() < _UNDER_EPIC_SHARE:
                # Work created in the last seven weeks mostly finishes in the last five: that is
                # where the drifting epics must have the team's attention.
                recent = issue.created > self.now - _PROCESS_CHANGE - _SPRINT_LENGTH
                weights = [_DRIFT_WEIGHT if recent and epic in self.drifting else 1 for epic in epics]
                issue.parent = issue.epic = self.rng.choices(epics, weights=weights)[0]
            if issue.issue_type == "Task":
                tasks.append(issue.number)

    def _estimate(self, issue: _Issue) -> None:
        """The story-point history: usually set before the start, sometimes after, rarely twice."""
        if issue.points is None:
            return
        started = issue.started
        end = (issue.resolved or self.now) - HOUR
        # The window for an estimate made after the start: the first two days of work,
        # when the issue stayed open that long.
        after_start: tuple[datetime, datetime] | None = None
        if started is not None and started + 2 * HOUR < min(end, started + 48 * HOUR):
            after_start = (started + 2 * HOUR, min(end, started + 48 * HOUR))
        first_set: datetime | None = None
        draw = self.rng.random()
        if draw < _POINTS_AT_CREATION_SHARE:
            pass  # set on creation: no history row
        elif after_start is not None and draw < _POINTS_AT_CREATION_SHARE + _POINTS_AFTER_START_SHARE:
            first_set = self._moment(*after_start)
        else:
            first_set = self._moment(issue.created, started or end)
        if first_set is not None:
            issue.points_changes.append((first_set, None, _points_text(issue.points)))
        if self.rng.random() < _RE_ESTIMATED_SHARE:
            revised = _neighbour_points(issue.points, self.rng)
            at = self._moment(first_set or issue.created, end)
            issue.points_changes.append((at, _points_text(issue.points), _points_text(revised)))
            issue.points = revised

    def _assign(self, issue: _Issue) -> None:
        share = _EPIC_ASSIGNED_SHARE if issue.issue_type == "Epic" else _ASSIGNED_SHARE[issue.status]
        if self.rng.random() >= share:
            return
        account = self.rng.choices(ASSIGNEES, weights=_ASSIGNEE_WEIGHTS)[0]
        end = issue.started or (self.now - HOUR)
        at = self._moment(issue.created, end)
        if issue.started is not None and self.rng.random() < _REASSIGNED_SHARE:
            first = self.rng.choice([other for other in ASSIGNEES if other != account])
            issue.assignee_changes.append((self._moment(issue.created, at), None, first))
            issue.assignee_changes.append((at, first, account))
        else:
            issue.assignee_changes.append((at, None, account))
        issue.assignee = account

    def _log_time(self, issue: _Issue) -> None:
        """Tempo worklogs: the effort, in 15-minute units, spread over the time in progress."""
        if issue.status == TO_DO or issue.issue_type == "Epic":
            return
        share = _LOGGED_DONE_SHARE if issue.status == DONE else _LOGGED_IN_PROGRESS_SHARE
        if self.rng.random() >= share:
            return
        if issue.points is not None:
            hours = issue.points * _HOURS_PER_POINT * issue.ratio
        else:
            hours = self.rng.lognormvariate(math.log(_UNPOINTED_EFFORT_H), 0.8)
        recent = (issue.resolved or self.now) > self.now - _PROCESS_CHANGE
        if issue.epic in self.drifting and recent:
            hours *= _DRIFT_FACTOR
        if issue.status == IN_PROGRESS:
            hours *= self.rng.uniform(0.2, 1.1)  # part-way through, sometimes already over
        windows = [(start, end) for start, end in issue.work_windows(self.now - HOUR) if end > start]
        if not windows:
            return  # started within the hour: nothing to log yet
        units = max(1, round(hours * 4))
        entries = min(units, 1 + min(7, int(hours // 4)))
        cuts = sorted(self.rng.sample(range(1, units), entries - 1)) if entries > 1 else []
        pieces = [b - a for a, b in zip([0, *cuts], [*cuts, units])]
        total = sum((end - start for start, end in windows), timedelta())
        for piece in pieces:
            offset = total * self.rng.random()
            for start, end in windows:
                if offset <= end - start:
                    issue.worklogs.append((start + offset, piece * 900))
                    break
                offset -= end - start
        issue.worklogs.sort()

    def _summary(self, issue: _Issue) -> str:
        thing = self.rng.choice(_OBJECTS)
        if issue.issue_type == "Bug":
            text = f"Fix {thing}: {self.rng.choice(_SYMPTOMS)}"
        elif issue.issue_type == "Test":
            text = f"{self.rng.choice(_TEST_FORMS)} {thing}"
        elif issue.issue_type == "Subtask":
            text = f"{self.rng.choice(_SUBTASK_ACTIONS)} {thing}"
        else:
            text = f"{self.rng.choice(_ACTIONS)} {thing}"
        if issue.epic is not None and self.rng.random() < 0.5:
            text += f" ({self.epic_names[issue.epic]})"
        return text

    # --- sprints and links -----------------------------------------------------------------------------

    def _sprints(self, issues: list[_Issue]) -> list[_Sprint]:
        """Two-week sprints back to the project's start, the active one ending in two days, one planned."""
        active_end = self.now + _ACTIVE_SPRINT_ENDS_IN
        starts: list[datetime] = []
        start = active_end - _SPRINT_LENGTH
        while True:
            starts.append(start)
            if start <= self.floor:
                break
            start -= _SPRINT_LENGTH
        starts.reverse()
        sprints = [
            _Sprint(
                _SPRINT_ID_BASE + index,
                f"{self.project} Sprint {index}",
                "active" if index == len(starts) else "closed",
                start,
                start + _SPRINT_LENGTH,
            )
            for index, start in enumerate(starts, start=1)
        ]
        future_index = len(starts) + 1
        sprints.append(
            _Sprint(
                _SPRINT_ID_BASE + future_index,
                f"{self.project} Sprint {future_index}",
                "future",
                active_end,
                active_end + _SPRINT_LENGTH,
            )
        )
        active, future = sprints[-2], sprints[-1]

        # Started work belongs to the sprints it was worked in, carried over at most twice.
        for issue in issues:
            started = issue.started
            if issue.issue_type == "Epic" or started is None:
                continue
            until = issue.resolved or self.now
            overlapping = [s for s in sprints if s.state != "future" and s.start < until and s.end > started]
            issue.sprints = [s.sprint_id for s in overlapping[-3:]]

        # Commit enough backlog to the active sprint that ~38% of it is not started; stale items last.
        members = sum(1 for issue in issues if active.sprint_id in issue.sprints)
        backlog = [issue for issue in issues if issue.status == TO_DO and issue.issue_type != "Epic"]
        self.rng.shuffle(backlog)
        backlog.sort(key=lambda issue: issue.stale)
        committed = round(members * _NOT_STARTED_SHARE / (1 - _NOT_STARTED_SHARE)) or min(3, len(backlog))
        for issue in backlog[:committed]:
            issue.sprints = [active.sprint_id]
        remaining = backlog[committed:]
        for issue in remaining[: min(20, round(len(remaining) * _FUTURE_SPRINT_SHARE))]:
            issue.sprints = [future.sprint_id]
        return sprints

    def _links(self, issues: list[_Issue]) -> list[_Link]:
        work = [issue for issue in issues if issue.issue_type != "Epic"]
        wanted = self.count // _LINK_PAIRS_PER_ISSUES if len(work) >= 2 else 0
        links: list[_Link] = []
        seen: set[tuple[int, int]] = set()
        attempts = 0
        while len(links) < wanted and attempts < wanted * 10:
            attempts += 1
            source, target = self.rng.sample(work, 2)
            pair = (min(source.number, target.number), max(source.number, target.number))
            if pair in seen:
                continue
            seen.add(pair)
            kind = "Blocks" if self.rng.random() < 0.6 else "Relates"
            links.append((kind, source.number, target.number))
        return links


def _hand_to(issue: _Issue, account: str) -> None:
    """The issue changes hands: its last assignment names the new holder. When the holder before that was the new one,
    the old holder takes that earlier place, so the history still runs from one person to another."""
    if issue.assignee is None or issue.assignee == account or not issue.assignee_changes:
        return
    at, before, _to = issue.assignee_changes[-1]
    if before == account and len(issue.assignee_changes) > 1:
        first_at, first_before, _first_to = issue.assignee_changes[-2]
        issue.assignee_changes[-2] = (first_at, first_before, issue.assignee)
        before = issue.assignee
    issue.assignee_changes[-1] = (at, before, account)
    issue.assignee = account


def _swap(pool: list[_Issue], account: str, keeps_to_process: Callable[[_Issue], bool], *, closer: bool) -> None:
    """Pairs of issues change hands between `account` and the two people with no habit of their own: closer, its
    issues that miss the process for theirs that keep to it; otherwise the other way round. Counts stay as they were."""
    mine = [issue for issue in pool if issue.assignee == account and keeps_to_process(issue) != closer]
    theirs = [issue for issue in pool if issue.assignee in _PARTNERS and keeps_to_process(issue) == closer]
    for given, taken in zip(mine, theirs):
        partner = taken.assignee
        assert partner is not None
        _hand_to(given, partner)
        _hand_to(taken, account)


def _estimated_in_time(issue: _Issue) -> bool:
    """Its estimate was first set no later than an hour after work started (R-EST-02's grace), as derive works it out:
    a first change from no estimate is when it was set; a first change from one, or no change at all, means it was
    set when the issue was created."""
    if issue.points is None or issue.started is None:
        return False
    for at, before, _after in issue.points_changes:
        return before is not None or at <= issue.started + HOUR
    return True


def _over_effort(issue: _Issue) -> bool:
    """Logged time at R-EFF-01's threshold (1.5 times the estimate) or over it."""
    if issue.points is None or not issue.worklogs:
        return False
    logged_h = sum(seconds for _at, seconds in issue.worklogs) / 3600
    return logged_h >= 1.5 * issue.points * _HOURS_PER_POINT


def _reopened(issue: _Issue) -> bool:
    return any(before == DONE and after != DONE for _at, before, after in issue.transitions)


def _points_text(points: float) -> str:
    """Story points as the changelog's toString shows them: "3", or "0.5"."""
    return str(int(points)) if points.is_integer() else str(points)


def _neighbour_points(points: float, rng: random.Random) -> float:
    """A re-estimate: one step up or down the point scale."""
    index = _POINT_VALUES.index(points)
    candidates = [i for i in (index - 1, index + 1) if 0 <= i < len(_POINT_VALUES)]
    return _POINT_VALUES[rng.choice(candidates)]


# --- rendering: Jira-shaped payloads, then rows through the real mapping ----------------------------


@dataclass
class _IssueRows:
    row: dict[str, Any]
    person: dict[str, Any] | None  # the assignee's people row, from the same payload
    status_changes: list[dict[str, Any]]
    field_changes: list[dict[str, Any]]
    worklogs: list[dict[str, Any]]
    links: list[dict[str, Any]]


@dataclass
class _Tables:
    statuses: list[dict[str, Any]]
    issues: list[_IssueRows]


def _id_base(project: str) -> int:
    """A numeric issue-id range of its own per project key, so two demo projects can share a database."""
    return 100_000 * (1 + zlib.crc32(project.encode("utf-8")) % 9_000)


def _render(project: str, now: datetime, issues: list[_Issue], sprints: list[_Sprint], links: list[_Link]) -> _Tables:
    by_number = {issue.number: issue for issue in issues}
    sprint_objects = {sprint.sprint_id: _sprint_object(sprint) for sprint in sprints}
    issuelinks = _issuelinks(project, by_number, links)
    histories = _histories(project, issues)
    worklogs = _worklog_rows(project, now, issues)
    own_seconds = {issue.number: sum(seconds for _at, seconds in issue.worklogs) for issue in issues}
    aggregate_seconds = dict(own_seconds)
    for issue in issues:
        if issue.issue_type == "Subtask" and issue.parent is not None:
            aggregate_seconds[issue.parent] += own_seconds[issue.number]

    rendered = []
    for issue in issues:
        status_changes, field_changes = histories[issue.number]
        issue_worklogs = worklogs.get(issue.number, [])
        last_touched = max(
            [issue.created]
            + [at for at, _from, _to in issue.transitions]
            + [at for at, _from, _to in issue.assignee_changes]
            + [at for at, _from, _to in issue.points_changes]
            + [row["_updated"] for row in issue_worklogs]
        )
        payload = _payload(
            project,
            issue,
            by_number,
            updated=last_touched,
            sprints=[sprint_objects[sprint_id] for sprint_id in issue.sprints],
            issuelinks=issuelinks.get(issue.number, []),
            time_spent=own_seconds[issue.number] or None,
            aggregate=aggregate_seconds[issue.number] or None,
        )
        rendered.append(
            _IssueRows(
                row=map_issue(payload, DEMO_FIELDS),
                person=map_assignee(payload, to_iso(now)),
                status_changes=status_changes,
                field_changes=field_changes,
                worklogs=[{k: v for k, v in row.items() if k != "_updated"} for row in issue_worklogs],
                links=map_issue_links(payload),
            )
        )
    statuses = [
        {"project_key": project, "status_id": status_id, "name": name, "category": category}
        for name, (status_id, category, _category_id) in _STATUSES.items()
    ]
    return _Tables(statuses=statuses, issues=rendered)


def _histories(project: str, issues: list[_Issue]) -> dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """status_changes and field_changes rows per issue, with changelog ids that grow with time
    across the project, as Jira's do."""
    events: list[tuple[datetime, int, int, str, str | None, str | None]] = []
    for issue in issues:
        events += [(at, issue.number, 0, "status", f, t) for at, f, t in issue.transitions]
        events += [(at, issue.number, 1, "assignee", f, t) for at, f, t in issue.assignee_changes]
        events += [(at, issue.number, 2, "story_points", f, t) for at, f, t in issue.points_changes]
    events.sort(key=lambda event: event[:3])
    out: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {issue.number: ([], []) for issue in issues}
    for changelog_id, (at, number, _order, field_name, from_value, to_value) in enumerate(
        events, start=_CHANGELOG_ID_BASE
    ):
        key = f"{project}-{number}"
        if field_name == "status":
            out[number][0].append(
                {
                    "issue_key": key,
                    "changelog_id": str(changelog_id),
                    "changed_at": to_iso(at),
                    "author_id": _author(number, from_value),
                    "from_status": from_value,
                    "to_status": to_value,
                }
            )
        else:
            out[number][1].append(
                {
                    "issue_key": key,
                    "changelog_id": str(changelog_id),
                    "field": field_name,
                    "changed_at": to_iso(at),
                    "from_value": from_value,
                    "to_value": to_value,
                }
            )
    return out


def _author(number: int, from_status: str | None) -> str:
    """Who moved the issue: an opaque account; a reopen comes from someone else."""
    if from_status == DONE:
        return ASSIGNEES[(number + 3) % len(ASSIGNEES)]
    return ASSIGNEES[number % len(ASSIGNEES)]


def _worklog_rows(project: str, now: datetime, issues: list[_Issue]) -> dict[int, list[dict[str, Any]]]:
    """worklogs rows per issue, ids growing with time; "_updated" carries the datetime for updated_at."""
    entries = sorted((started, issue.number, seconds) for issue in issues for started, seconds in issue.worklogs)
    out: dict[int, list[dict[str, Any]]] = {}
    for worklog_id, (started, number, seconds) in enumerate(entries, start=_WORKLOG_ID_BASE):
        updated = min(started + timedelta(seconds=seconds) + HOUR, now - timedelta(minutes=1))
        out.setdefault(number, []).append(
            {
                "worklog_id": str(worklog_id),
                "issue_key": f"{project}-{number}",
                "started_at": to_iso(started),
                "seconds": seconds,
                "author_id": WORKLOG_AUTHOR,
                "updated_at": to_iso(updated),
                "_updated": updated,
            }
        )
    return out


def _issuelinks(project: str, by_number: dict[int, _Issue], links: list[_Link]) -> dict[int, list[dict[str, Any]]]:
    """fields.issuelinks per issue: each link appears on both issues, as Jira sends it."""
    out: dict[int, list[dict[str, Any]]] = {}
    for link_id, (kind, source, target) in enumerate(links, start=20_001):
        type_id, inward, outward = _LINK_TYPES[kind]
        link = {
            "id": str(link_id),
            "self": f"{_SITE}/rest/api/3/issueLink/{link_id}",
            "type": {"id": type_id, "name": kind, "inward": inward, "outward": outward},
        }
        out.setdefault(source, []).append(link | {"outwardIssue": _brief(project, by_number[target])})
        out.setdefault(target, []).append(link | {"inwardIssue": _brief(project, by_number[source])})
    return out


def _payload(
    project: str,
    issue: _Issue,
    by_number: dict[int, _Issue],
    *,
    updated: datetime,
    sprints: list[dict[str, Any]],
    issuelinks: list[dict[str, Any]],
    time_spent: int | None,
    aggregate: int | None,
) -> dict[str, Any]:
    """One search result, in the shape tests/fixtures/jira/SHAPES.md documents."""
    issue_id = _issue_id(project, issue.number)
    resolved = issue.resolved
    fields: dict[str, Any] = {
        "project": {
            "self": f"{_SITE}/rest/api/3/project/10200",
            "id": "10200",
            "key": project,
            "name": f"{project} (sample)",
            "projectTypeKey": "software",
            "simplified": True,
        },
        "issuetype": _type_object(issue.issue_type),
        "summary": issue.summary,
        "status": _status_object(issue.status),
        "assignee": None if issue.assignee is None else _account(issue.assignee),
        "created": _jira_ts(issue.created),
        "updated": _jira_ts(updated),
        "resolutiondate": None if resolved is None else _jira_ts(resolved),
        "timeoriginalestimate": None,
        "timeestimate": None,
        "timespent": time_spent,
        "aggregatetimespent": aggregate,
        POINTS_FIELD: issue.points,
        SPRINT_FIELD: sprints or None,
        "labels": [],
        "issuelinks": issuelinks,
    }
    if issue.parent is not None:
        fields["parent"] = _brief(project, by_number[issue.parent])
    return {
        "id": issue_id,
        "self": f"{_SITE}/rest/api/3/issue/{issue_id}",
        "key": f"{project}-{issue.number}",
        "fields": fields,
    }


def _issue_id(project: str, number: int) -> str:
    return str(_id_base(project) + number)


def _brief(project: str, issue: _Issue) -> dict[str, Any]:
    """How another issue (a parent, a linked issue) appears inside an issue."""
    issue_id = _issue_id(project, issue.number)
    return {
        "id": issue_id,
        "key": f"{project}-{issue.number}",
        "self": f"{_SITE}/rest/api/3/issue/{issue_id}",
        "fields": {
            "summary": issue.summary,
            "status": _status_object(issue.status),
            "issuetype": _type_object(issue.issue_type),
        },
    }


def _status_object(name: str) -> dict[str, Any]:
    status_id, category, category_id = _STATUSES[name]
    return {
        "self": f"{_SITE}/rest/api/3/status/{status_id}",
        "name": name,
        "id": status_id,
        "statusCategory": {
            "self": f"{_SITE}/rest/api/3/statuscategory/{category_id}",
            "id": category_id,
            "key": category,
            "name": name,
        },
    }


def _type_object(name: str) -> dict[str, Any]:
    type_id, level = _ISSUE_TYPES[name]
    return {
        "self": f"{_SITE}/rest/api/3/issuetype/{type_id}",
        "id": type_id,
        "name": name,
        "subtask": level == -1,
        "hierarchyLevel": level,
    }


def _account(account_id: str) -> dict[str, Any]:
    # A fictional display name, and no email address or picture URL: nothing that belongs to anyone real.
    return {
        "self": f"{_SITE}/rest/api/3/user?accountId={account_id}",
        "accountId": account_id,
        "accountType": "atlassian",
        "displayName": ASSIGNEE_NAMES.get(account_id) or MEMBER_NAMES[account_id],
        "active": True,
        "timeZone": ACCOUNT_TIMEZONE,
    }


def _assignable_users() -> list[dict[str, Any]]:
    """The board's assignable users as Jira's user/assignable/search would list them: every assignee, the three
    members with no tickets, and the worklogs' app account (which is no member: map_member keeps it out)."""
    app = {"self": f"{_SITE}/rest/api/3/user?accountId={WORKLOG_AUTHOR}", "accountId": WORKLOG_AUTHOR,
           "accountType": "app", "displayName": "Timesheets app", "active": True}
    return [*(_account(account) for account in (*ASSIGNEES, *MEMBERS)), app]


def _sprint_object(sprint: _Sprint) -> dict[str, Any]:
    """A sprint as the sprint field lists it; a closed sprint also carries its completeDate."""
    value: dict[str, Any] = {
        "id": sprint.sprint_id,
        "name": sprint.name,
        "state": sprint.state,
        "boardId": 7,
        "goal": "",
        "startDate": to_iso(sprint.start),
        "endDate": to_iso(sprint.end),
    }
    if sprint.state == "closed":
        value["completeDate"] = to_iso(sprint.end)
    return value


def _jira_ts(at: datetime) -> str:
    """Jira's timestamp form: the account's zone, milliseconds, offset without a colon."""
    local = at.astimezone(zone(ACCOUNT_TIMEZONE))
    return local.strftime("%Y-%m-%dT%H:%M:%S.") + f"{local.microsecond // 1000:03d}" + local.strftime("%z")


# --- writing -------------------------------------------------------------------------------------------

_PROJECT_ISSUE_KEYS = "SELECT issue_key FROM issues WHERE project_key = ?"
_PROJECT_FINDING_IDS = "SELECT finding_id FROM findings WHERE project_key = ?"


def _write(store: Store, project: str, tables: _Tables) -> None:
    """Make the project's rows exactly the rendered ones; unchanged rows are left alone."""
    store.replace_rows("statuses", {"project_key": project}, tables.statuses)
    for issue in tables.issues:
        where = {"issue_key": issue.row["issue_key"]}
        store.upsert("issues", issue.row)
        if issue.person is not None:
            store.upsert("people", issue.person)
        store.replace_rows("status_changes", where, issue.status_changes)
        store.replace_rows("field_changes", where, issue.field_changes)
        store.replace_rows("worklogs", where, issue.worklogs)
        store.replace_rows("links", where, issue.links)


def _store_members(store: Store, project: str, seen_at: str) -> None:
    """The board's members, stored as a sync's member fetch stores them (oversight/ingest/sync.py, step 6): the
    board's board_members rows replaced, and each member's people row upserted, through the same mapping."""
    found = [row for user in _assignable_users() if (row := map_member(user, seen_at)) is not None]
    members = [{"project_key": project, "account_id": row["account_id"], "seen_at": seen_at} for row in found]
    store.replace_rows("board_members", {"project_key": project}, members)
    for row in found:
        store.upsert("people", row)


def _store_pictures(store: Store, stored_at: str) -> None:
    """The PICTURED accounts' pictures, kept as a sync keeps a fetched one but with no source URL. Only for an
    account the demo has written (one with a ticket), and only when its picture is not already this one."""
    for account in PICTURED:
        data = picture(account)
        store.conn.execute(
            "UPDATE people SET avatar = ?, avatar_type = 'image/png', avatar_hash = ?, avatar_source = NULL,"
            " avatar_fetched_at = ? WHERE account_id = ? AND avatar_hash IS NOT ?",
            (data, version_of(data), stored_at, account, version_of(data)),
        )


def picture(account: str) -> bytes:
    """A 48x48 PNG of an abstract pattern drawn from the account id: a 5x5 grid of squares, mirrored left to
    right, in one colour on a pale ground, as code hosts draw for accounts without a photo. Nobody's likeness."""
    bits = hashlib.sha256(f"demo picture {account}".encode()).digest()
    ink = _PICTURE_INKS[bits[0] % len(_PICTURE_INKS)]
    # Columns 0..4 read bits for 0, 1, 2, 1, 0: the right half mirrors the left.
    cells = [[bool(bits[1 + row * 3 + min(col, 4 - col)] & 1) for col in range(5)] for row in range(5)]
    cell, margin = 8, 4
    lines = bytearray()
    for y in range(_PICTURE_SIZE):
        lines.append(0)  # filter type 0: the scanline as it is
        for x in range(_PICTURE_SIZE):
            row, col = (y - margin) // cell, (x - margin) // cell
            filled = margin <= y < _PICTURE_SIZE - margin and margin <= x < _PICTURE_SIZE - margin and cells[row][col]
            lines.extend(ink if filled else _PICTURE_GROUND)
    return _png(_PICTURE_SIZE, _PICTURE_SIZE, bytes(lines))


def _png(width: int, height: int, scanlines: bytes) -> bytes:
    """PNG bytes for 8-bit RGB scanlines, each already led by its filter byte."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8 bits a channel, RGB, no interlace
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 9))
        + chunk(b"IEND", b"")
    )


def _delete_project(store: Store, project: str) -> int:
    """Remove everything the project left behind (see the module notes); returns rows deleted."""
    deleted = 0
    for table in ISSUE_CHILD_TABLES:
        cursor = store.conn.execute(f"DELETE FROM {table} WHERE issue_key IN ({_PROJECT_ISSUE_KEYS})", (project,))
        deleted += cursor.rowcount
    cursor = store.conn.execute(
        "DELETE FROM warning_links "
        f"WHERE warning_finding_id IN ({_PROJECT_FINDING_IDS}) OR breach_finding_id IN ({_PROJECT_FINDING_IDS})",
        (project, project),
    )
    deleted += cursor.rowcount
    for table in ("findings", "rule_status", "digests", "insights", "statuses", "board_members", "issues"):
        deleted += store.delete_rows(table, {"project_key": project})
    deleted += store.delete_rows("sync_state", {"scope": project})
    return deleted
