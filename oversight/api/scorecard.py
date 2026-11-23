"""Person scorecards and the people comparison (JO-11; contract 4.15 in the jira-oversight-ui repository).

The product owner's decision of 11 Nov 2026: an admin (people level) can ask how anyone's tickets follow the process,
and see everyone side by side. A scorecard is one person's process figures from Jira, each next to the whole board's,
with the tickets behind them; the comparison orders everyone by how closely their tickets follow the process. The
figures are facts: whatever is written about them never labels anyone, and the decision about a person is the admin's.

One place computes every measure, for the board and for people: GET /people/scorecard (routes/people.py), Ask's two
person tools and the analyst's scorecard view (oversight/ask/people.py, oversight/ask/scorecards.py) and the tests all
read it here. It is worked out on read from what the sync, derive and rules steps already store (issues,
issue_metrics, status_intervals, findings, worklogs, board_members, people): no schema change, no sync step, nothing
written, so a sync running from disk is never touched. load_board() reads a board in eight queries, whatever its
size: people and figures in bulk, never one query per person or ticket.

Whose tickets. A person's tickets are the board's work (every issue type but the rules' defaults.exclude_types: an
Epic is a container, not work, as every rule counts it) whose current assignee they are: basis "current_assignee", as
findings follow the ticket's current assignee. The board's are all of its work, assigned or not. `tickets` (a
person's, the board's) counts the same work, Epics left out: one meaning of a person's tickets everywhere (JO-12,
G5), GET /people's and the directory's count too. Someone who holds only Epics holds none of the board's work: they
are on it (the directory lists them) with no tickets, as a member who holds none is.

The period. `to` is the snapshot's as-of (the board's last successful sync; now, before the first one) and `from` is
`days` (30, 90 or 180) before it; "in the period" is from <= t <= to, on the stored UTC timestamps.

The eleven measures (MEASURES, in the contract's order):
- open_tickets, in_progress (counts): their work whose status category is not done, and is in progress, now (the
  category the Issue page shows, as GET /people counts it);
- done_in_period (count): their work with issue_metrics.done_at in the period;
- estimated_before_start (rate): of their work started in the period (started_at), the share whose estimate was first
  set no later than work started, allowing R-EST-02's grace (grace_hours from the board's rules: rules.yaml's 1 hour
  unless the board's rule settings change it, JO-15), so the figure and R-EST-02's findings agree;
- time_logged (rate): of their work done in the period, the share with time logged on it or its subtasks
  (time_spent_s or aggregate_time_spent_s above 0);
- within_estimate (rate): of their work done in the period with an effort ratio and time logged (time_spent_s above
  0: an estimate with nothing logged against it is missing data, not effort, as the effort medians count it; contract
  section 8), the share under R-EFF-01's threshold (the board's: rules.yaml's unless its rule settings change it,
  never written here): R-EFF-01 breaches at it or over it, so the figure and R-EFF-01's findings agree;
- stuck_now (rate): of their open work, the share with an open W-AGE-01 finding;
- reopened (rate): of their work done in the period, the share reopened after Done (reopen_count above 0; R-FLOW-03);
- skipped_stage (rate): of their work done in the period, the share that skipped a stage (skipped_stages; R-FLOW-02);
- code_review (rate): of their work done in the period that reached R-FLOW-01's stage (testing) and that the rules
  have checked, the share with no R-FLOW-01 breach (one that is open, or resolved for any reason but "cleared": an
  aged-out breach still happened). Checked: the rules' look-back (defaults.lookback_days) never checks work finished
  before it, so done work counts only when it was finished within the look-back of the first rules run that could
  measure R-FLOW-01 on the board, or when R-FLOW-01 has a finding on it; work the rules never saw is not counted as
  reviewed (on a young rules history the 180-day figure would otherwise count it so). With no look-back, all of it;
- open_findings_per_10 (ratio): the open breaches and warnings on their open work per 10 of their open tickets, to
  one decimal (numerator: those findings; denominator: the open tickets). Both sides count the same tickets: an open
  finding on finished work (an effort breach, say, until the look-back ages it out) would otherwise be set against
  open tickets it is not on, and someone who has just finished their work would look worst.
A rate is a whole percentage and the ratio has one decimal, both rounded half up as a reader rounds: 5 of 8 is 63%.

Enough: a figure rests on at least MIN_TICKETS (5) tickets, its denominator; a count always does. A figure that rests
on fewer is still given ("1 of 2"), but never compared. vs_board, for rates and the ratio when both sides have enough:
"level" within 5 percentage points of the board (rates) or 0.5 (the ratio), on the figures as shown; otherwise
"closer" or "further" by the measure's direction; null for counts, and whenever either side has too few.

Measurable. A measure the board cannot show is measurable false, with the reason (the contract's words), and every
value null. One truth per board (JO-12, G11): skipped_stage and code_review are measurable exactly when the rules
engine measures their rule on the board, so a scorecard never calls a measure unmeasurable beside an Overview that
lists the rule's open findings. "Measures" is what the engine recorded in rule_status in its latest run on the board
(measurable 1), for a rule the board's rules still have on (rules.yaml with the board's rule settings, JO-15: a rule
an admin turned off there is measured no more); before any rules run has reached the board, the engine's own test
(catalog.rule_missing: no evidence or stage the rule needs is missing) stands for it.
- skipped_stage: R-FLOW-02 is not measured on the board ("This board has no stages to skip.");
- code_review: R-FLOW-01 is not measured on the board (no code-review evidence, or no testing stage, say), or the
  board's rules have no R-FLOW-01 that is on ("This board doesn't record code review in Jira.");
- time_logged: no worklog on the board started in the period;
- estimated_before_start, within_estimate: the board's estimate field (process.yaml) is not configured on the site
  (fields.yaml has no story points field; original estimates are a Jira system field, and a synthetic board's demo
  data carries its own); within_estimate also when the catalogue gives R-EFF-01 no threshold;
- a measure whose rules the board has turned off (JO-15: an admin's rule settings, or rules.yaml itself), so its rules
  hold no one to it there: "Its rule is off on this board." (estimated_before_start: both R-EST-01 and R-EST-02 off,
  "Its rules are off on this board."; code_review: R-FLOW-01 off). A board that could not show the measure with them on
  says why instead (code review not recorded, say), and stuck_now never reads 0% for want of a W-AGE-01 that is off.

The comparison (order(), Ask's oversight_people_compare) puts everyone who holds a ticket in the order a question
asks for (the product owner's decision of 13 Nov 2026, JO-14: answer exactly what was asked). "closer" (no direction
word in the question): closer to the process first, by one measure highest or lowest first by its direction; overall,
by the number of measures on which they are closer to the process than the board, most first, ties by
open_findings_per_10 (lower first; a figure on too few tickets is never compared, so it counts as the most). "most"
(most, highest, largest, top N): the figure highest first, whichever way the measure points; overall, the same order
as "closer". "fewest" (fewest, least, lowest, smallest, bottom N): the figure lowest first; overall, the exact
reverse of "most" (fewest measures closer first, ties by open_findings_per_10 higher first). A count (RANKED_COUNTS)
has no closer side, so "closer" puts it most first. Whatever the order, people with too few tickets for a rate or the
ratio come last, and overall the people compared on no measure; ties then keep the directory's order.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, TypedDict

from oversight.api.deps import ApiContext
from oversight.api.meta import last_synced_at
from oversight.api.people import Person, directory_key, person
from oversight.api.timelines import Categories, issue_category, ts
from oversight.ask.catalog import _rows, rule_missing
from oversight.errors import ConfigError
from oversight.rules.catalog import Rule, RulesConfig
from oversight.rules.checks import DEFAULT_GRACE_HOURS
from oversight.timeutil import hours_between, parse_ts, to_iso

MIN_TICKETS = 5  # the tickets a figure must rest on before it is compared with anyone's
PERIODS: tuple[int, ...] = (30, 90, 180)
DEFAULT_DAYS = 90
BASIS = "current_assignee"
LEVEL_POINTS = 5  # a rate within this many percentage points of the board's is level with it
LEVEL_TENTHS = 5  # the ratio within 0.5 of the board's (counted in tenths) is level with it

STUCK_RULE = "W-AGE-01"
REVIEW_RULE = "R-FLOW-01"
SKIP_RULE = "R-FLOW-02"
EFFORT_RULE = "R-EFF-01"
LATE_ESTIMATE_RULE = "R-EST-02"
OVERALL = "overall"  # the comparison over every measure, rather than one

Kind = Literal["count", "rate", "ratio"]
Direction = Literal["neutral", "higher_is_closer", "lower_is_closer"]
VsBoard = Literal["closer", "further", "level"]
# The comparison's order (JO-14): closer to the process first (the default), or the figure highest or lowest first.
Order = Literal["closer", "most", "fewest"]
ORDERS: tuple[str, ...] = ("closer", "most", "fewest")


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    kind: Kind
    direction: Direction
    rule_ids: tuple[str, ...] = ()
    every_rule: bool = False  # its rules are every issue rule in the catalogue (it counts every open finding)


MEASURES: tuple[Measure, ...] = (
    Measure("open_tickets", "Open tickets", "count", "neutral"),
    Measure("in_progress", "In progress now", "count", "neutral"),
    Measure("done_in_period", "Done in the period", "count", "neutral"),
    Measure("estimated_before_start", "Estimated before work started", "rate", "higher_is_closer",
            ("R-EST-01", "R-EST-02")),
    Measure("time_logged", "Time logged on done work", "rate", "higher_is_closer"),
    Measure("within_estimate", "Done within the effort limit", "rate", "higher_is_closer", ("R-EFF-01",)),
    Measure("stuck_now", "Open tickets stuck in a status", "rate", "lower_is_closer", ("W-AGE-01",)),
    Measure("reopened", "Reopened after Done", "rate", "lower_is_closer", ("R-FLOW-03",)),
    Measure("skipped_stage", "Skipped a stage", "rate", "lower_is_closer", ("R-FLOW-02",)),
    Measure("code_review", "Code review recorded", "rate", "higher_is_closer", ("R-FLOW-01", "W-REV-01")),
    Measure("open_findings_per_10", "Open findings per 10 open tickets", "ratio", "lower_is_closer", every_rule=True),
)
BY_KEY: dict[str, Measure] = {measure.key: measure for measure in MEASURES}
# The measures a person can be closer to the process on, or further from it: the rates and the ratio.
COMPARABLE: tuple[str, ...] = tuple(measure.key for measure in MEASURES if measure.direction != "neutral")
# The counts everyone can be put in order by (most first: who finished the most, who has the most in progress, whose
# backlog is biggest; or fewest first). A count is never set beside the board as closer or further. The open tickets
# here are the scorecard's (Epics left out), so the biggest backlog is counted as one person's backlog is; JO-09's
# ranking by GET /people's open tickets (oversight_people) stays as it was.
RANKED_COUNTS: tuple[str, ...] = ("open_tickets", "in_progress", "done_in_period")
# The counts of now, which no period bounds: their comparison takes no days.
NOW_COUNTS: tuple[str, ...] = ("open_tickets", "in_progress")

# Why a board cannot show a measure: the contract's words, exactly.
NO_STAGES_TO_SKIP = "This board has no stages to skip."
NO_CODE_REVIEW = "This board doesn't record code review in Jira."
NO_TIME_LOGGED = "No one logged time on this board in the period."
NO_ESTIMATES = "This board has no estimates."
NO_EFFORT_LIMIT = "The rules set no effort limit."
RULE_OFF = "Its rule is off on this board."
RULES_OFF = "Its rules are off on this board."


class Value(TypedDict):
    value: int | float | None
    numerator: int | None
    denominator: int | None
    enough: bool
    vs_board: VsBoard | None


def unmeasured() -> Value:
    """A value there is none of: not measurable, or someone who holds no work."""
    return {"value": None, "numerator": None, "denominator": None, "enough": False, "vs_board": None}


def percent(numerator: int, denominator: int) -> int:
    """numerator / denominator as a whole percentage, rounded half up as a reader rounds: 5 of 8 is 63. The same
    integer is the ratio per 10 in tenths: 7 per 3 is 233, so 23.3 per 10."""
    return (200 * numerator + denominator) // (2 * denominator)


@dataclass(frozen=True)
class Period:
    days: int
    since: str  # storage format, inclusive
    until: str  # the snapshot's as-of, inclusive

    def holds(self, at: str | None) -> bool:
        return at is not None and self.since <= at <= self.until


@dataclass(frozen=True)
class Ticket:
    """One piece of the board's work, as the measures read it."""

    issue_key: str
    holder: str | None  # the current assignee's person id; None: nobody
    category: str  # new | indeterminate | done, now
    started_at: str | None
    done_at: str | None
    estimated_in_time: bool  # its estimate was first set no later than R-EST-02's grace after work started
    time_logged: bool  # time logged on it or on its subtasks
    effort_ratio: float | None  # logged over estimated, when time was logged against an estimate
    reopened: bool
    skipped: bool
    reached_review: bool  # entered R-FLOW-01's stage (testing)
    review_checked: bool  # the rules could have checked it for R-FLOW-01 (within the look-back of their first run)
    review_breach: bool  # an R-FLOW-01 finding, open or resolved for any reason but "cleared"
    stuck: bool  # an open W-AGE-01 finding
    open_findings: int  # its open breaches and warnings

    @property
    def open(self) -> bool:
        return self.category != "done"


@dataclass(frozen=True)
class Board:
    """What every scorecard of one board is worked out from, read once (load_board)."""

    project: str
    as_of: str  # the snapshot's as-of: the end of every period
    total: int  # the board's tickets: its work, assigned or not (Epics left out)
    work: tuple[Ticket, ...]
    holdings: Mapping[str, int]  # person id -> tickets held now (their work, Epics left out)
    holders: tuple[Person, ...]  # everyone holding a ticket now (work), in the directory's order
    # Everyone else on the board, holding none of its work (its members who hold no ticket, and anyone holding only
    # Epics), in the directory's order
    members: tuple[Person, ...]
    reasons: Mapping[str, str]  # measure key -> why this board can never show it
    last_worklog: str | None  # the latest worklog started on the board up to the as-of
    effort_limit: float | None  # R-EFF-01's threshold
    every_rule: tuple[str, ...]  # the catalogue's issue rules, in its order

    def period(self, days: int) -> Period:
        if days not in PERIODS:
            raise ValueError(f"a period is {', '.join(map(str, PERIODS))} days, not {days}")
        until = parse_ts(self.as_of)
        assert until is not None
        since = to_iso(until - timedelta(days=days))
        assert since is not None
        return Period(days=days, since=since, until=self.as_of)

    def reason(self, key: str, period: Period) -> str | None:
        """Why the board cannot show this measure over the period; None when it can."""
        if key in self.reasons:
            return self.reasons[key]
        if key == "time_logged" and (self.last_worklog is None or self.last_worklog < period.since):
            return NO_TIME_LOGGED
        return None

    @property
    def records_code_review(self) -> bool:
        """Whether code review shows on this board (the caveat's last sentence is said when it does not): a board that
        records it, with R-FLOW-01 turned off, does."""
        return self.reasons.get("code_review") != NO_CODE_REVIEW

    def rule_ids(self, measure: Measure) -> list[str]:
        return list(self.every_rule if measure.every_rule else measure.rule_ids)

    def measures(self, period: Period) -> list[dict[str, Any]]:
        """The eleven measures as the contract sends them, in its order, with what this board can show."""
        return [
            {
                "key": measure.key,
                "label": measure.label,
                "kind": measure.kind,
                "direction": measure.direction,
                "rule_ids": self.rule_ids(measure),
                "measurable": self.reason(measure.key, period) is None,
                "reason": self.reason(measure.key, period),
            }
            for measure in MEASURES
        ]

    @property
    def everyone(self) -> list[Person]:
        """Everyone on the board, as the directory lists them: holders and members, in its order."""
        return sorted([*self.holders, *self.members], key=directory_key)

    def who(self, person_id: str) -> Person | None:
        """The person with this id when they are on the board (holding a ticket now, or a member); None otherwise."""
        return next((who for who in (*self.holders, *self.members) if who["id"] == person_id), None)

    def tickets_of(self, person_id: str) -> list[Ticket]:
        return [ticket for ticket in self.work if ticket.holder == person_id]


# --- reading a board -------------------------------------------------------------------------

# Every ticket on the board with its assignee's name and picture version and what derive worked out for it.
_TICKETS_SQL = """
    SELECT i.issue_key, i.issue_type, i.status, i.status_category, i.assignee_id, a.display_name, a.avatar_hash,
           i.time_spent_s, i.aggregate_time_spent_s, m.started_at, m.done_at, m.reopen_count, m.skipped_stages,
           m.estimate_set_at, m.effort_ratio
    FROM issues AS i
    LEFT JOIN people AS a ON a.account_id = i.assignee_id
    LEFT JOIN issue_metrics AS m ON m.issue_key = i.issue_key
    WHERE i.project_key = ?
"""
# Per issue: its open findings, whether one of them is W-AGE-01's, and whether R-FLOW-01 ever breached on it (open, or
# resolved for any reason but "cleared"). A sprint finding's key is no ticket's, so it counts for no one.
_FINDINGS_SQL = """
    SELECT issue_key,
           SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS open_findings,
           MAX(CASE WHEN rule_id = ? AND resolved_at IS NULL THEN 1 ELSE 0 END) AS stuck,
           MAX(CASE WHEN rule_id = ? AND COALESCE(resolved_why, '') != 'cleared' THEN 1 ELSE 0 END) AS review_breach,
           MAX(CASE WHEN rule_id = ? THEN 1 ELSE 0 END) AS review_seen
    FROM findings
    WHERE project_key = ?
    GROUP BY issue_key
"""
# The board's issues that ever entered a stage.
_REACHED_SQL = """
    SELECT DISTINCT si.issue_key
    FROM status_intervals AS si JOIN issues AS i ON i.issue_key = si.issue_key
    WHERE i.project_key = ? AND si.stage = ?
"""
# The latest worklog started on the board up to a moment.
_LAST_WORKLOG_SQL = """
    SELECT MAX(w.started_at) AS started_at
    FROM worklogs AS w JOIN issues AS i ON i.issue_key = w.issue_key
    WHERE i.project_key = ? AND w.started_at <= ?
"""
# The first rules run that could measure a rule on the board: the look-back is counted back from it.
_FIRST_RUN_SQL = """
    SELECT MIN(r.started_at) AS started_at
    FROM rule_status AS s JOIN runs AS r ON r.run_id = s.run_id
    WHERE s.project_key = ? AND s.rule_id = ? AND s.measurable = 1
"""
# What the rules engine measured on the board in its latest run there (rule_status): rule id -> measurable 0 or 1.
_LATEST_STATUS_SQL = """
    SELECT rule_id, measurable
    FROM rule_status
    WHERE project_key = ? AND run_id = (
        SELECT s.run_id
        FROM rule_status AS s JOIN runs AS r ON r.run_id = s.run_id
        WHERE s.project_key = ?
        ORDER BY r.started_at DESC, s.run_id DESC
        LIMIT 1
    )
"""
# The board's members from its last member fetch, with their names and picture versions.
_MEMBERS_SQL = """
    SELECT b.account_id, a.display_name, a.avatar_hash
    FROM board_members AS b LEFT JOIN people AS a ON a.account_id = b.account_id
    WHERE b.project_key = ?
"""


def load_board(conn: sqlite3.Connection, ctx: ApiContext) -> Board:
    """The board ctx reads, as every scorecard of it is worked out: eight queries, people and figures in bulk."""
    project, process, rules = ctx.project, ctx.process, ctx.rules
    as_of = last_synced_at(conn, project) or to_iso(ctx.now())
    assert as_of is not None
    review = _rule(rules, REVIEW_RULE, enabled=True)
    review_stage = str(review.params.get("stage") or "") if review is not None else ""
    effort_limit = _effort_limit(rules)
    grace = _grace_hours(rules)
    excluded = {name.casefold() for name in rules.defaults.exclude_types}

    findings = {row["issue_key"]: row
                for row in _rows(conn, _FINDINGS_SQL, (STUCK_RULE, REVIEW_RULE, REVIEW_RULE, project))}
    checked_since = _checked_since(conn, project, rules)
    reached = {row["issue_key"] for row in _rows(conn, _REACHED_SQL, (project, review_stage))} if review_stage else set()
    categories = Categories(conn, ctx)
    holdings: Counter[str] = Counter()
    holders: dict[str, Person] = {}
    on_the_board: dict[str, Person] = {}  # everyone assigned a ticket now, an Epic too
    work: list[Ticket] = []
    for row in _rows(conn, _TICKETS_SQL, (project,)):
        who = person(row["assignee_id"], row["display_name"], row["avatar_hash"])
        if who is not None:
            on_the_board.setdefault(who["id"], who)
        if (row["issue_type"] or "").casefold() in excluded:
            continue  # a container, not work: never in a measure, and no one's ticket (JO-12, G5)
        if who is not None:
            holdings[who["id"]] += 1
            holders.setdefault(who["id"], who)
        category = issue_category(row, ctx, categories)
        work.append(_ticket(row, who, category, findings.get(row["issue_key"]), row["issue_key"] in reached, grace,
                            checked_since))

    # Everyone else on the board holds none of its work: its members who hold no ticket, and anyone holding only Epics.
    members = {who_id: who for who_id, who in on_the_board.items() if who_id not in holders}
    for row in _rows(conn, _MEMBERS_SQL, (project,)):
        who = person(row["account_id"], row["display_name"], row["avatar_hash"])
        if who is not None and who["id"] not in holders:
            members.setdefault(who["id"], who)
    last = _rows(conn, _LAST_WORKLOG_SQL, (project, as_of))
    return Board(
        project=project,
        as_of=as_of,
        total=len(work),
        work=tuple(work),
        holdings=dict(holdings),
        holders=tuple(sorted(holders.values(), key=directory_key)),
        members=tuple(sorted(members.values(), key=directory_key)),
        reasons=_reasons(ctx, effort_limit, _measured_rules(conn, ctx)),
        last_worklog=ts(last[0]["started_at"]) if last else None,
        effort_limit=effort_limit,
        every_rule=tuple(rule.id for rule in rules.rules if rule.scope == "issue"),
    )


def _checked_since(conn: sqlite3.Connection, project: str, rules: RulesConfig) -> str | None:
    """Since when the rules have checked done work for R-FLOW-01 on the board: the look-back counted back from their
    first run that could measure it; "" when there is no look-back (all of it), None when no run has measured it."""
    lookback = rules.defaults.lookback_days
    if lookback is None:
        return ""
    first = _rows(conn, _FIRST_RUN_SQL, (project, REVIEW_RULE))
    started = parse_ts(first[0]["started_at"]) if first and first[0]["started_at"] else None
    return to_iso(started - timedelta(days=lookback)) if started is not None else None


def _ticket(row: sqlite3.Row, who: Person | None, category: str, finding: sqlite3.Row | None, reached: bool,
            grace: float, checked_since: str | None = "") -> Ticket:
    started, done, set_at = ts(row["started_at"]), ts(row["done_at"]), ts(row["estimate_set_at"])
    spent, below = _seconds(row["time_spent_s"]), _seconds(row["aggregate_time_spent_s"])
    ratio = _finite(row["effort_ratio"])
    return Ticket(
        issue_key=row["issue_key"],
        holder=who["id"] if who is not None else None,
        category=category,
        started_at=started,
        done_at=done,
        estimated_in_time=started is not None and set_at is not None and hours_between(started, set_at) <= grace,
        time_logged=spent > 0 or below > 0,
        effort_ratio=ratio if spent > 0 else None,
        reopened=(_finite(row["reopen_count"]) or 0) > 0,
        skipped=_skipped(row["skipped_stages"]),
        reached_review=reached,
        review_checked=bool(finding is not None and finding["review_seen"]) or checked_since == "" or (
            checked_since is not None and done is not None and done >= checked_since),
        review_breach=bool(finding is not None and finding["review_breach"]),
        stuck=bool(finding is not None and finding["stuck"]),
        open_findings=int(finding["open_findings"] or 0) if finding is not None else 0,
    )


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _seconds(value: object) -> float:
    return _finite(value) or 0.0


def _skipped(text: object) -> bool:
    if not isinstance(text, str) or not text:
        return False
    try:
        skips = json.loads(text)
    except ValueError:
        return False
    return isinstance(skips, list) and len(skips) > 0


def _rule(rules: RulesConfig, rule_id: str, *, enabled: bool = False) -> Rule | None:
    try:
        rule = rules.by_id(rule_id)
    except ConfigError:
        return None  # not in the catalogue
    return rule if rule.enabled or not enabled else None


def _effort_limit(rules: RulesConfig) -> float | None:
    """R-EFF-01's threshold on the board (rules.yaml's unless the board's rule settings change it); None when the
    rules give it none."""
    rule = _rule(rules, EFFORT_RULE)
    threshold = rule.params.get("threshold") if rule is not None else None
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not threshold > 0:
        return None
    return float(threshold)


def _grace_hours(rules: RulesConfig) -> float:
    """R-EST-02's grace: an estimate set within it of the start counts as set before it, as the rule counts it."""
    rule = _rule(rules, LATE_ESTIMATE_RULE)
    if rule is None:
        return 0.0
    grace = rule.params.get("grace_hours", DEFAULT_GRACE_HOURS)
    return float(grace) if isinstance(grace, (int, float)) and not isinstance(grace, bool) else DEFAULT_GRACE_HOURS


def _measured_rules(conn: sqlite3.Connection, ctx: ApiContext) -> frozenset[str]:
    """The rules the engine measures on the board (JO-12, G11: one truth per board): each enabled rule its latest run
    there recorded as measurable in rule_status; before any rules run has reached the board, each enabled rule the
    engine's own test finds nothing missing for (catalog.rule_missing), as the Overview's rule list judges them."""
    enabled = {rule.id: rule for rule in ctx.rules.enabled_rules}
    latest = {row["rule_id"]: row["measurable"] for row in _rows(conn, _LATEST_STATUS_SQL, (ctx.project, ctx.project))}
    if latest:
        return frozenset(rule_id for rule_id in enabled if latest.get(rule_id) == 1)
    return frozenset(rule_id for rule_id, rule in enabled.items() if not rule_missing(rule, ctx.process, ctx.project))


def _reasons(ctx: ApiContext, effort_limit: float | None, measured: frozenset[str]) -> dict[str, str]:
    """The measures this board can never show, whatever the period, and why. measured: the rules the engine measures
    on the board (_measured_rules). A measure whose rules are all off on the board (JO-15) says so, unless the board
    could not show it with them on either."""
    process, project = ctx.process, ctx.project
    off = frozenset(rule.id for rule in ctx.rules.rules if not rule.enabled)
    reasons: dict[str, str] = {}
    if SKIP_RULE not in measured:
        reasons["skipped_stage"] = _turned_off(ctx, SKIP_RULE, off) or NO_STAGES_TO_SKIP
    if REVIEW_RULE not in measured:
        reasons["code_review"] = _turned_off(ctx, REVIEW_RULE, off) or NO_CODE_REVIEW
    estimate = process.estimate_for(project)
    configured = (estimate.field == "original_estimate" or process.project(project).synthetic
                  or ctx.fields.id_of("story_points") is not None)
    if not configured:
        reasons["estimated_before_start"] = reasons["within_estimate"] = NO_ESTIMATES
    elif effort_limit is None:
        reasons["within_estimate"] = NO_EFFORT_LIMIT
    for measure in MEASURES:
        drawn_from = set(measure.rule_ids)
        if measure.key not in reasons and drawn_from and not measure.every_rule and drawn_from <= off:
            reasons[measure.key] = RULE_OFF if len(drawn_from) == 1 else RULES_OFF
    return reasons


def _turned_off(ctx: ApiContext, rule_id: str, off: frozenset[str]) -> str | None:
    """RULE_OFF when the board has turned the rule off though it could measure it (nothing the rule needs is missing
    there); None otherwise."""
    rule = _rule(ctx.rules, rule_id) if rule_id in off else None
    return RULE_OFF if rule is not None and not rule_missing(rule, ctx.process, ctx.project) else None


# --- the figures -----------------------------------------------------------------------------


def _figures(tickets: Sequence[Ticket], period: Period, limit: float | None) -> dict[str, tuple[int, int | None]]:
    """Each measure's numerator and denominator over these tickets (a count's denominator is None)."""
    open_now = [ticket for ticket in tickets if ticket.open]
    done = [ticket for ticket in tickets if period.holds(ticket.done_at)]
    started = [ticket for ticket in tickets if period.holds(ticket.started_at)]
    ratios = [ticket.effort_ratio for ticket in done if ticket.effort_ratio is not None]
    reviewed = [ticket for ticket in done if ticket.reached_review and ticket.review_checked]
    return {
        "open_tickets": (len(open_now), None),
        "in_progress": (sum(ticket.category == "indeterminate" for ticket in tickets), None),
        "done_in_period": (len(done), None),
        "estimated_before_start": (sum(ticket.estimated_in_time for ticket in started), len(started)),
        "time_logged": (sum(ticket.time_logged for ticket in done), len(done)),
        "within_estimate": (sum(limit is not None and ratio < limit for ratio in ratios), len(ratios)),
        "stuck_now": (sum(ticket.stuck for ticket in open_now), len(open_now)),
        "reopened": (sum(ticket.reopened for ticket in done), len(done)),
        "skipped_stage": (sum(ticket.skipped for ticket in done), len(done)),
        "code_review": (sum(not ticket.review_breach for ticket in reviewed), len(reviewed)),
        "open_findings_per_10": (sum(ticket.open_findings for ticket in open_now), len(open_now)),
    }


def _value(measure: Measure, numerator: int, denominator: int | None) -> Value:
    if measure.kind == "count" or denominator is None:
        return {"value": numerator, "numerator": None, "denominator": None, "enough": True, "vs_board": None}
    figure: int | float | None = None
    if denominator:
        figure = percent(numerator, denominator) if measure.kind == "rate" else percent(numerator, denominator) / 10
    return {"value": figure, "numerator": numerator, "denominator": denominator,
            "enough": denominator >= MIN_TICKETS, "vs_board": None}


def vs_board(measure: Measure, mine: Value, board: Value) -> VsBoard | None:
    """Where a figure stands next to the board's: never for a count, nor when either side rests on too few tickets."""
    if measure.kind == "count" or not (mine["enough"] and board["enough"]):
        return None
    if mine["value"] is None or board["value"] is None:
        return None
    if measure.kind == "rate":
        gap, band = int(mine["value"]) - int(board["value"]), LEVEL_POINTS
    else:
        gap, band = round(mine["value"] * 10) - round(board["value"] * 10), LEVEL_TENTHS
    if abs(gap) <= band:
        return "level"
    closer = gap > 0 if measure.direction == "higher_is_closer" else gap < 0
    return "closer" if closer else "further"


def values(board: Board, tickets: Sequence[Ticket], period: Period, against: Mapping[str, Value] | None = None,
           ) -> dict[str, Value]:
    """Every measure's value over these tickets; against: the board's values, to say where each stands next to it."""
    figures = _figures(tickets, period, board.effort_limit)
    found: dict[str, Value] = {}
    for measure in MEASURES:
        if board.reason(measure.key, period) is not None:
            found[measure.key] = unmeasured()
            continue
        value = _value(measure, *figures[measure.key])
        if against is not None:
            value["vs_board"] = vs_board(measure, value, against[measure.key])
        found[measure.key] = value
    return found


def board_values(board: Board, period: Period) -> dict[str, Value]:
    """The whole board's values: all of its work, assigned or not."""
    return values(board, board.work, period)


def person_values(board: Board, person_id: str, period: Period, against: Mapping[str, Value]) -> dict[str, Value]:
    """A person's values next to the board's; every one null for someone who holds no ticket on the board."""
    if not board.holdings.get(person_id):
        return {measure.key: unmeasured() for measure in MEASURES}
    return values(board, board.tickets_of(person_id), period, against)


def scorecard(board: Board, days: int, wanted: Sequence[str] | None, *, comparison: bool) -> dict[str, Any]:
    """GET /people/scorecard's body but meta (contract 4.15). wanted: the person ids asked for (each on the board: the
    route checks), or None for everyone who holds a ticket, who are listed only when comparison is on."""
    period = board.period(days)
    against = board_values(board, period)
    if wanted is None:
        chosen = list(board.holders) if comparison else []
    else:
        found = (board.who(person_id) for person_id in dict.fromkeys(wanted))
        chosen = sorted((who for who in found if who is not None), key=directory_key)
    return {
        "period": {"days": period.days, "from": period.since, "to": period.until},
        "basis": BASIS,
        "comparison": "on" if comparison else "off",
        "measures": board.measures(period),
        "board": {"tickets": board.total, "values": against},
        "people": [
            {"person": who, "tickets": board.holdings.get(who["id"], 0),
             "values": person_values(board, who["id"], period, against)}
            for who in chosen
        ],
    }


# --- the comparison --------------------------------------------------------------------------


@dataclass(frozen=True)
class Standing:
    """One person in the comparison: their values next to the board's, and how many measures each way."""

    person: Person
    tickets: int
    values: Mapping[str, Value]

    def _count(self, where: VsBoard) -> int:
        return sum(self.values[key]["vs_board"] == where for key in COMPARABLE)

    @property
    def closer(self) -> int:
        return self._count("closer")

    @property
    def further(self) -> int:
        return self._count("further")

    @property
    def level(self) -> int:
        return self._count("level")

    @property
    def compared(self) -> int:
        """The measures they were compared with the board on."""
        return self.closer + self.further + self.level


def standings(board: Board, period: Period, against: Mapping[str, Value]) -> list[Standing]:
    """Everyone who holds a ticket on the board, in the directory's order."""
    return [
        Standing(person=who, tickets=board.holdings.get(who["id"], 0),
                 values=person_values(board, who["id"], period, against))
        for who in board.holders
    ]


def too_few(board: Board, standing: Standing, period: Period) -> int:
    """The measures the board can show on which this person was not compared: too few tickets, theirs or the board's."""
    return sum(board.reason(key, period) is None and standing.values[key]["vs_board"] is None for key in COMPARABLE)


def effective_order(measure: str, first: str) -> Order:
    """The order a comparison by this measure is put in, as the facts record it: as asked, but a count has no closer
    side, so "closer" puts it most first; and overall, the most measures closer to the process first is the closer
    order itself, so "most" is "closer" there. ValueError for an order that is none of ORDERS."""
    if first not in ORDERS:
        raise ValueError(f"not an order: {first!r} (closer, most or fewest)")
    if first == "fewest":
        return "fewest"
    if measure in RANKED_COUNTS:
        return "most"
    if measure == OVERALL or first == "closer":
        return "closer"
    return "most"


def order(everyone: Sequence[Standing], measure: str, *, first: str = "closer") -> list[Standing]:
    """The comparison's order (see the module docstring). measure: a key of COMPARABLE, or OVERALL, or a count of
    RANKED_COUNTS. first: "closer" (closer to the process first; a count most first), "most" (the figure highest
    first) or "fewest" (lowest first). Too few tickets for a rate or the ratio, or no measure compared overall, last
    whatever the order; ties keep the directory's order (the sorts are stable)."""
    first = effective_order(measure, first)
    if measure == OVERALL:
        compared = [one for one in everyone if one.compared]

        def ratio(one: Standing) -> float:
            return _lower_first(_compared_value(one.values["open_findings_per_10"]))

        if first == "fewest":
            # The exact reverse of the closer order: the fewest measures closer first, ties by the ratio highest first
            # (a figure on too few tickets counting as the most, so it leads its tie).
            compared.sort(key=lambda one: (one.closer, -ratio(one)))
        else:
            compared.sort(key=lambda one: (-one.closer, ratio(one)))
        return compared + [one for one in everyone if not one.compared]
    if measure in RANKED_COUNTS:
        sign = 1 if first == "fewest" else -1
        return sorted(everyone, key=lambda one: sign * float(one.values[measure]["value"] or 0))
    if measure not in COMPARABLE:
        raise ValueError(f"not a measure people are compared on: {measure!r}")
    if first == "closer":
        highest = BY_KEY[measure].direction == "higher_is_closer"
    else:
        highest = first == "most"
    enough = [one for one in everyone if one.values[measure]["enough"] and one.values[measure]["value"] is not None]
    enough.sort(key=lambda one: -float(one.values[measure]["value"] or 0) if highest
                else float(one.values[measure]["value"] or 0))
    return enough + [one for one in everyone if one not in enough]


def _compared_value(value: Value) -> int | float | None:
    """A figure as the comparison may use it: never one that rests on too few tickets."""
    return value["value"] if value["enough"] else None


def _lower_first(value: int | float | None) -> float:
    return math.inf if value is None else float(value)
