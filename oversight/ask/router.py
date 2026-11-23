"""Deterministic question routing for the scripted provider (no API key).

route(question, catalog_names, *, project, mentions=()) -> Plan. Keyword rules
over a lower-cased question; parameters parsed from the text (a project key, a
rule id, an issue key, "top 5", "last sprint" / "this month" / "last 30 days" /
"last 90 days"). Anything unmatched returns understood=false with reason None.

People. The level is the catalogue's: the person tools (PEOPLE_NAMES) are in
it only at people level (oversight/ask/people.py), and the comparison
(oversight_people_compare) only where people may be compared.
- Team level: per-person questions ("who", "developer", "slowest", "best", a
  person-like phrase) return understood=false with reason PERSON_REASON, the
  contract's team-level decline.
- People level: a question is about people when it mentions someone (the
  person ids in `mentions`) or uses a person word ("who", "developer",
  "person", "everyone", "anyone", "users", ...; and, in a question that
  evaluates people, a group word: "the team", "assignees", "the rest"). Nothing
  about people is declined there (the product owner's direction of 11 Nov
  2026). One that evaluates people (people.SCORECARD_WORDS: how someone is
  doing, follows the process, best, worst, rank, compare, versus, ...;
  people.PERSONNEL_WORDS: fire, let go, demote, promote, bonus, blame, trust,
  lazy, attitude, ...) is answered with figures (JO-11; judging_plan): one
  mention -> oversight_person_scorecard(person_ids, days); two to ten -> the
  same, side by side; none -> oversight_people_compare over the measure the
  question names (_MEASURE_WORDS: estimates, effort, time logged, stuck,
  reopened, skipped, code review, findings), else overall. Without the
  comparison in the catalogue (PEOPLE_COMPARISON=off), two mentions or none
  return understood=false with reason COMPARISON_OFF_REASON. The period is 30,
  90 (the default) or 180 days, from the words (_SHORT_PERIOD, _HALF_YEAR). A
  ranking by one of GET /people's counts ("top 5 people by breaches", "rank
  people by open tickets", "bottom 3 people by open tickets", "compare @A and
  @B by open breaches") is JO-09's, as it was: ranking and comparing words
  alone do not evaluate.
  Every comparison is in the order the question asks for (JO-14, the product
  owner's decision of 13 Nov 2026: answer exactly what was asked; _order):
  "most" for most, highest, largest, biggest, greatest, maximum, top N, more,
  higher, descending (the figure highest first), "fewest" for fewest, least,
  lowest, smallest, minimum, bottom N, fewer, less, lower, ascending (lowest
  first); with none, "closer" (closer to the process first; a count, most
  first). A range's first word decides ("from most to least"), top N and
  bottom N only where no other direction word stands, and a question that
  asks both ways takes the word nearest its measure (_direction). It holds
  for rates, the ratio and counts alike, and the plan always carries the
  order. A rate named the other way round turns it (_turned): "who overruns
  the most?" and "who has the highest effort ratio?" are the lowest share
  done within the effort limit first, "who has the most unestimated work?"
  the lowest share estimated before it started. A question about the other
  side of the process (_FURTHER: worst, struggling, needs help, falling
  behind, slowest, furthest from or deviating from the process, trouble
  with) is further from it first, and "the least" of that closer first:
  "who struggles the most?" is overall fewest, "who is worst for reopened
  work?" reopened most, "who needs the least help?" overall closer. "At
  most", "at least", "most closely" and "more closely" are no direction,
  and the last resort's overall comparison is closer first unless it asks
  about the other side of the process. The ratio is the comparison only
  when the question names it as one (_RATIO: "per 10", "findings per ticket",
  "findings rate"): findings named alone are JO-09's count.
  Otherwise one mention routes to the person tools: a scorecard measure named
  (time logged, stuck, reopened, skipped, estimates, effort, code review) ->
  oversight_person_scorecard(person_ids, days); "backlog", "to do",
  "open work", overloaded -> oversight_person_tickets(open, the whole list);
  finished, done, closed, contributed -> oversight_person_tickets(done, days,
  the whole list); "working on", "doing", "in progress", idle ->
  oversight_person_tickets(in_progress); breaches, warnings or findings ->
  oversight_person_findings (severity from the word, both or neither named:
  every finding; open unless "resolved" or "all"); tickets ->
  oversight_person_tickets (open unless "all" or "ever"); no such word ->
  both. Two to ten mentions, with a scorecard measure named (whether or not
  the question ranks, compares or evaluates them) ->
  oversight_people_compare(measure, days, order, person_ids), those people only (with
  comparing people off: not understood, reason COMPARISON_OFF_REASON); with
  another figure named, that figure (_side_by_side); with none, one
  oversight_people call with their person_ids: each one's open breaches and
  warnings. Without a mention: an
  issue key -> oversight_ticket_assignee (who holds it now); who is idle (has
  nothing in progress) or has nothing to do (spare capacity) ->
  oversight_people_compare(in_progress or open_tickets, fewest first) and
  oversight_board_people(none), the members who hold no ticket at all;
  a measure named -> that measure's comparison (stuck, reopened, estimates,
  time logged, ...), read before work done or in progress, whose words name
  the tickets a rate is of ("reopened after Done", "time logged on done
  work", "stuck tickets in progress"); finished work (or who contributes) ->
  oversight_people_compare(done_in_period); work in progress ->
  oversight_people_compare(in_progress); who has no tickets -> oversight_board_people(none); a
  backlog or a heavy load (overloaded, swamped) ->
  oversight_people_compare(open_tickets), the scorecard's open tickets, as
  one person's backlog is counted; breaches, warnings, findings or tickets ->
  oversight_people ranked by open breaches, open warnings or open tickets
  ("who has the most open breaches?"); who is on the board, or its members
  -> oversight_board_people(any), everyone on it. A count goes most first,
  or fewest first when the question asks for the fewest (_FEWEST: least,
  fewest, lowest, smallest, bottom N, ...). The synonym table for how much
  work people have (JO-12, G3/G4): the least, doing or working the least, underworked,
  capacity, bandwidth, could take (on) more, the lightest load, not busy,
  available (_SPARE) -> oversight_people_compare(open_tickets, fewest first)
  and oversight_board_people(none), but words that only say how little
  (_SPARE_HOW_MUCH: "the least work"), with no word of room, beside a figure
  named are that figure (JO-14: "who reopens the least work?" is reopened,
  "who has the least work in progress?" in_progress, each fewest first);
  the busiest, busy, the most work,
  working the most (_BUSIEST) -> oversight_people_compare(open_tickets), most
  first, and with "doing" (do, does, did: _DOING) the comparison by
  done_in_period too. Hours, in any word order ("who logged the most hours?",
  "who has the most hours logged?"), are the time_logged measure. A question
  about people that no figure here places (cycle time by person, say) is not
  understood, with reason
  PEOPLE_REASON (what is answered, and @): the service then answers it with
  last_resort() (the overall comparison, or the mentioned people's
  scorecards), never turning an admin away, and keeps PEOPLE_REASON for where
  comparing people is off; one about no one goes through the table below.

The routing table, first match wins, after the per-person check:

 1. an issue key                      -> oversight_issue_timeline(issue_key)
 2. a rule id                         -> oversight_list_findings(rule_id)
 3. trend / trending / over time      -> oversight_trend; the metric comes from
    the words (effort or estimate -> median_effort_ratio, cycle ->
    median_cycle_time_h, skipped or straight -> skipped_stage_share, reopen ->
    reopen_rate, breach -> open_breaches, warning -> open_warnings); no
    recognisable metric -> not understood
 4. hot spot, or epic + breach        -> oversight_hot_spots
 5. rules + measured / can't / status / which -> oversight_rule_status
 6. without an estimate / unestimated -> oversight_count_issues(has_estimate
    false) when counting, else oversight_list_findings(R-EST-01, or W-EST-01
    when the question is about the sprint)
 7. coverage phrases ("how much of", "has an estimate", "share of",
    "estimated", ...) -> oversight_coverage(field from the words), plus
    oversight_list_findings(R-EST-02) for "estimated" / "estimate before"
 8. cycle time                        -> oversight_top_issues(cycle_time) for a
    list of tickets, else oversight_trend(median_cycle_time_h)
 9. stuck / longest / time in status / how long / oldest ->
    oversight_list_findings(W-AGE-01) for "which tickets are stuck",
    oversight_top_issues(status_age) for a list of tickets, else
    oversight_time_in_status(group_by from the words, status by default)
10. straight / skipped / To Do to Done -> oversight_list_findings(R-FLOW-02)
11. code review / testing             -> oversight_list_findings(R-FLOW-01)
12. reopen                            -> oversight_top_issues(reopens) for
    "most", else oversight_list_findings(R-FLOW-03)
13. burn                              -> oversight_top_issues(effort_ratio)
14. unassigned / no assignee          -> oversight_list_findings(R-HYG-01)
15. at risk                           -> oversight_list_findings(W-SPR-01)
16. effort / over the estimate        -> oversight_top_issues(effort_ratio) for
    a list of tickets, else oversight_effort_vs_estimate(group_by from the
    words, epic by default)
17. breach / warning / finding / problem -> oversight_list_findings(severity
    from the word)
18. how many / count, or "which tickets" with nothing more specific ->
    oversight_count_issues(status and issue type from the words)
19. nothing matched                   -> understood=false, reason None

A plan names only tools in `catalog_names` (an unavailable tool is dropped)
and holds at most three calls. The parsed period applies wherever the tool
takes one; a tool that needs a period gets the baseline window (90d) when the
question has none. Findings are listed as open by default, as "all" when a
period is given, and as resolved when the question says so. "top N" is capped
at each tool's maximum. A project key in the question overrides `project`;
when neither names one, the catalogue's context project applies.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from oversight.api.scorecard import BY_KEY, NOW_COUNTS, OVERALL, RANKED_COUNTS, effective_order
from oversight.ask.people import evaluates, personnel, ranks_only
from oversight.ask.prompts import COMPARISON_OFF, PEOPLE_HELP, TEAM_DECLINE
from oversight.llm.provider import Plan, PlanCall

PERSON_REASON = TEAM_DECLINE
PEOPLE_REASON = PEOPLE_HELP
COMPARISON_OFF_REASON = COMPARISON_OFF

# The catalogue vocabulary (ask/catalog.py), for callers without a catalogue listing.
CATALOG_NAMES: tuple[str, ...] = (
    "oversight_count_issues",
    "oversight_top_issues",
    "oversight_time_in_status",
    "oversight_effort_vs_estimate",
    "oversight_list_findings",
    "oversight_issue_timeline",
    "oversight_trend",
    "oversight_coverage",
    "oversight_hot_spots",
    "oversight_rule_status",
)
# The person tools (ask/people.py, PEOPLE_CATALOG): in the catalogue only at people level; the comparison, the last,
# only where people may be compared.
SCORECARD_TOOL = "oversight_person_scorecard"
COMPARE_TOOL = "oversight_people_compare"
ASSIGNEE_TOOL = "oversight_ticket_assignee"
BOARD_PEOPLE_TOOL = "oversight_board_people"
PEOPLE_NAMES: tuple[str, ...] = (
    "oversight_people", BOARD_PEOPLE_TOOL, "oversight_person_tickets", ASSIGNEE_TOOL, "oversight_person_findings",
    SCORECARD_TOOL, COMPARE_TOOL,
)
# The longest list of one person's tickets a plan asks for (their backlog, what they finished): the whole of it, up to
# the facts' row limit.
WHOLE_LIST = 200

MAX_CALLS = 3
# The window the baseline report uses, for tools that need a period.
_DEFAULT_PERIOD = "90d"
# Weekly trend points to show for each period.
_WEEKS_FOR = {"7d": 2, "30d": 5, "90d": 12, "sprint": 3, "all": 12}
_MAX_WEEKS = 12

# Identifiers. Rule ids are read in any case ("r-flow-02"); issue keys as Jira
# writes them (upper case), since a lower-case "sprint-4" is not a key.
_RULE_ID = re.compile(r"\b([RW]-[A-Z]+-\d{2,3})\b", re.IGNORECASE)
_ISSUE_KEY = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")
_PROJECT_KEY = re.compile(r"\b([A-Z][A-Z0-9_]{1,19})\b")
# Upper-case words that are not project keys: abbreviations, shouted statuses, percentiles.
_NOT_PROJECTS = frozenset(
    "AI AM API ASAP CD CI DEV ETA EU FYI ID IDS IT KPI LLM MCP MVP MVPS OK P50 P75 P90 P95 P99 VIP "
    "PM PR PRS PROD QA SIT SLA TEST UAT UI UK UX US VS WIP "
    "TO DO TODO IN ON AT FOR NEW OPEN DONE CLOSED PROGRESS REVIEW TESTING BLOCKED RESOLVED "
    "BACKLOG READY SELECTED DEVELOPMENT STAGING LIVE".split()
)

# "top 5", "bottom 3", "the lowest 3", "which 3 people", "the 5 developers": at most that many (the order is the
# question's: _order).
_NUMBER = r"(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|twenty)"
_TOP_N = re.compile(
    rf"\b(?:top|bottom|highest|lowest)\s+{_NUMBER}\b"
    rf"|\b(?:which|the|first)\s+{_NUMBER}\s+(?:people|persons|developers|devs|engineers|programmers|testers|assignees"
    r"|members|team\s*-?\s*members|teammates|colleagues|performers|users|staff|employees|of\s+(?:them|us|the\s+team))\b"
)
_WORD_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "twenty": 20}
_LAST_N_WEEKS = re.compile(r"\b(?:last|past)\s+(\d{1,2})\s+weeks?\b")

# Period phrases, most specific first. "by sprint" is a grouping, not a period.
_PERIOD_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:last|this|active|current|the)\s+sprint\b", "sprint"),
    (r"\b(?:last|past|previous)\s+(?:7|seven)\s+days\b|\b(?:this|last|past)\s+week\b", "7d"),
    (
        r"\b(?:last|past|previous)\s+(?:30|thirty)\s+days\b|\b(?:this|last|past)\s+month\b"
        r"|\b(?:4|four)\s+weeks\b",
        "30d",
    ),
    (
        r"\b(?:last|past|previous)\s+(?:90|ninety)\s+days\b|\b(?:this|last|past)\s+quarter\b"
        r"|\b(?:3|three)\s+months\b|\b(?:12|13|twelve|thirteen)\s+weeks\b",
        "90d",
    ),
    (r"\ball[\s-]time\b|\bever\b|\boverall\b|\bto date\b|\bsince the (?:beginning|start)\b", "all"),
)

# Questions about individuals. Answers stay at team and process level, so these
# are declined before any routing (Planning.md principle 8).
_PERSON = re.compile(
    r"\b(?:who|whom|whose|developers?|engineers?|person|people|individuals?|team\s*-?\s*members?"
    r"|teammates?|colleagues?|staff|slowest|fastest|best|worst\s+performers?|performance\s+of"
    r"|productive|productivity|per\s+person|by\s+name|each\s+person|headcount)\b"
)
# People level: the words that make a question about people (a mention does too) ...
_ABOUT_PEOPLE = re.compile(
    r"\b(?:who|whom|whose|developers?|devs|engineers?|programmers?|testers?|person|people|individuals?"
    r"|team\s*-?\s*members?|members?|teammates?|colleagues?|staff|employees?|users?|everyone|everybody|anyone"
    r"|anybody|someone|somebody|performers?|per\s+person|by\s+person|by\s+name|each\s+person|headcount)\b"
)
# ... and, in a question that evaluates people, the words that name them as a group ("rank the team", "which assignee
# follows the process best?", "rank the rest"). Read only beside an evaluating word: "the team's open tickets" is about
# the work.
_PEOPLE_AS_A_GROUP = re.compile(
    r"\b(?:assignees?|dev|(?:the\s+)?team|squad|the\s+others|the\s+rest|each\s+other)\b"
)
# (Those that evaluate someone are answered with their scorecard or the comparison: judging_plan.)
# The dashboard's sample reads the word lists below alike (src/server/sample/ask/match.ts).
# What one mentioned person has still to do: their open tickets.
_BACKLOG = r"\bbacklog\b|\bto[\s-]?do\b|\bopen work\b|\boutstanding\b|\bon (?:their|his|her) plate\b"
# A heavy load: the open tickets, most first (one person: their backlog).
_OVERLOAD = (
    r"\boverload\w*|\boverwhelm\w*|\bswamped\b|\bsnowed under\b|\bstretched(?: too)? thin\b|\btoo much (?:work|on)\b"
    r"|\b(?:heaviest|biggest|largest) (?:load|workload)\b|\b(?:over|at|beyond|out of) capacity\b|\bno (?:spare )?capacity\b"
)
# The synonym table for how much work people have (JO-12, G3/G4: never turn an admin away; the dashboard's sample reads
# the same words, src/server/sample/ask/match.ts). The least: nothing to do, room for more, could take more work, the
# least busy, doing the least, underworked: the open tickets, fewest first, and the members who hold no ticket at all.
# Two kinds of words (JO-14): words of room (capacity, bandwidth, available, room for more, ...), and words that only
# say how little ("the least work", "does the least", "the fewest tasks"), which a figure named beside them makes that
# figure's (_route_people): "who reopens the least work?" is reopened, "who has the least work in progress?" the work
# in progress, "who finished the least work?" the work done, each fewest first; "who has the least work?" stays here.
_SPARE_ROOM = (
    r"\bnothing to do\b|\b(?:spare|free|most) capacity\b|\bcapacity\b|\bbandwidth\b"
    r"|\bunder-?(?:used|utili[sz]ed|loaded|work\w*|employ\w*|occupied)\b|\b(?:lightest|smallest|least) (?:load|workload)\b"
    r"|\blightest\b|\bon the bench\b|\broom for more\b|\bavailab(?:le|ility)\b|\bquietest\b|\bfree\s+(?:to|for)\b"
    r"|\b(?:take|taking|took|handle|pick)\s+(?:on\s+|up\s+)?(?:more|extra|another|additional)\b"
    r"|\b(?:least|less|not(?:\s+very)?)\s+busy\b|n['’]t(?:\s+very)?\s+busy\b|\bleast\s+loaded\b"
    r"|\b(?:spare|free)\s+(?:time|cycles?)\b|\blight(?:ly)?\s+(?:on\s+(?:work|load|tickets|tasks)|loaded)\b"
    r"|\blight\s+(?:work)?load\b|\b(?:got|has|have|had)\s+(?:the\s+)?least\s+on\b"
)
_SPARE_HOW_MUCH = (
    r"\b(?:do|does|doing|did)\s+(?:the\s+)?least\b|\bwork(?:s|ing|ed)?\s+(?:the\s+)?least\b"
    r"|\bleast\s+(?:work|tasks|to\s+do|going\s+on|on\s+(?:their|his|her)\s+plate)\b|\bfewest\s+tasks\b"
    r"|\b(?:has|have|got|holds?)\s+(?:the\s+)?(?:least|fewest)\s*[?.!]*$"
)
_SPARE = _SPARE_ROOM + "|" + _SPARE_HOW_MUCH
# How much work people have, said the other way round (the JO-12 review, the G4 class: an answer ordered against the
# question): a qualifier before a word of room ("at full capacity", "the least bandwidth", "isn't available", "can't
# take on more", "no room for more") is no room at all, and so is a heavy load in plain words ("flat out", "maxed
# out", "overworked", "who has the most on"): the busiest, most first. Read before _SPARE, whose words they share.
_NO_ROOM = (
    r"\b(?:no|zero|little|limited|least|full)\s+(?:spare\s+|free\s+)?(?:capacity|bandwidth|room)\b"
    r"|\b(?:at|near|nearing|reached|reaching|hit|hitting|over|beyond|out\s+of)\s+(?:(?:their|his|her|full)\s+)?capacity\b"
    r"|\b(?:not|never)\s+(?:very\s+)?available\b|n['’]t\s+(?:very\s+)?available\b|\bunavailable\b"
    r"|\b(?:can['’]?t|cannot|can\s+not|could\s*n['’]t|could\s+not|unable\s+to)\s+(?:take|handle|pick)\s+(?:on\s+|up\s+)?"
    r"(?:any\s+)?(?:more|extra|another|additional)\b"
    r"|\bmaxed(?:\s+out)?\b|\bflat\s+out\b|\bover-?worked\b|\bover-?stretched\b|\b(?:got|has|have|had)\s+(?:the\s+)?most\s+on\b"
)
# A negation (or "least") before a load word turns it round: "not overloaded", "isn't swamped" have room (fewest
# first); "not underworked" is busy (most first); "not idle", "the least idle" have work in progress (most first).
_NEGATED = r"(?:\bnot|n['’]t|\bnever|\bleast)\s+(?:very\s+|too\s+|that\s+)?"
_NOT_OVERLOADED = _NEGATED + r"(?:overload\w*|overwhelm\w*|swamped|snowed\s+under|stretched|over-?worked|over-?stretched)\b"
_NOT_UNDERWORKED = _NEGATED + r"under-?(?:used|utili[sz]ed|loaded|work\w*|employ\w*|occupied)\b"
_NOT_IDLE = _NEGATED + r"idle\b"
# The most: the busiest, doing the most, the most work: the open tickets, most first; and, where the question says
# what someone is doing ("doing", "does", "did"), the tickets done in the period, most first, beside them.
_BUSIEST = (
    r"\bbusiest\b|\bbusy\b|\bmost\s+(?:busy|loaded|work|tasks|going\s+on|on\s+(?:their|his|her)\s+plate)\b"
    r"|\b(?:do|does|doing|did)\s+(?:the\s+)?most\b|\bwork(?:s|ing|ed)?\s+(?:the\s+)?most\b"
    r"|\b(?:has|have|got|holds?)\s+(?:the\s+)?most\s*[?.!]*$"
)
_DOING = r"(?<!\bto )\b(?:do|does|doing|did)\b"
# Idle: nothing in progress now, fewest first, and the members who hold no ticket at all.
_IDLE = (
    r"\bidle\b|\bnot (?:doing|working on) anything\b|\bnothing (?:in progress|on the go)\b|\bnothing to work on\b"
    r"|\bno (?:tickets?|work|issues?) in progress\b"
)
# The order a question asks for, for every measure: rates, the ratio and counts (JO-14, the product owner's decision of
# 13 Nov 2026: answer exactly what was asked). Most first: most, highest, largest, biggest, greatest, maximum, top N,
# more, higher, larger, bigger, greater, descending, high to low. Fewest first: fewest, least, lowest, smallest,
# minimum, bottom N, fewer, less, lower, smaller, ascending, low to high. No such word: closer to the process first
# (a count, most first). "At most" and "at least" are thresholds, not an order, and "most closely" or "more closely"
# is how closely someone follows the process (no direction). Which word decides (_direction): a range's first word
# ("from most to least" is most first); else the words above, "top N", "bottom N", "at the top" and "at the bottom"
# only when the question has none of them ("top 3 people with the fewest reopened tickets" is fewest first); and where
# a question asks both ways ("the most reopened and the fewest stuck"), the word nearest the measure it is answered by.
# The dashboard's sample reads the same words (src/server/sample/ask/match.ts: orderIn, orderNear).
_N_WORDS = r"(?:\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|twenty)"
_MOST = (
    r"(?<!\bat )\bmost\b(?!\s+closely)|\bmore\b(?!\s+closely)|\b(?:highest|largest|biggest|greatest|maximum)\b"
    r"|\b(?:higher|larger|bigger|greater)\b|\bdescending\b|\bhigh\s+to\s+low\b"
)
_FEWEST = (
    r"(?<!\bat )\bleast\b|\b(?:fewest|lowest|smallest|minimum)\b|\b(?:fewer|less|lower|smaller)\b"
    r"|\bascending\b|\blow\s+to\s+high\b"
)
_TOP = rf"\btop\s+{_N_WORDS}\b|\bat\s+the\s+(?:very\s+)?top\b"
_BOTTOM = rf"\bbottom\s+{_N_WORDS}\b|\bat\s+the\s+(?:very\s+)?bottom\b"
# The other side of following the process (the JO-14 review): who is further from it, struggling, falling behind or
# needing help, the worst, the slowest or the weakest, who deviates from or ignores the process, who has trouble with
# something. Their figure's order runs the other way (_order): with no direction word, or "the most", further from the
# process first (overall, the fewest measures closer; a rate or the ratio, the higher one where lower is closer and
# the lower one where higher is closer); with "the least", "less" or "fewest", closer to the process first, as "who
# struggles the least?" asks. A negation before one ("not struggling", "never needs help", "no trouble with") is not
# further. The answer still only reports the order ("highest first", "fewest first"): it never says worst.
_FURTHER = (
    r"\b(?:worst|worse|weak(?:est|er)?|poor(?:est|er|ly)?|bad(?:ly)?|slow(?:est|er|ly)?|underperform\w*|lagg\w*"
    r"|lags?|struggl\w*|(?:fall(?:s|ing|en)?|fell|running|lagging|is|are|was|were|get(?:s|ting)?|got)\s+behind"
    r"|need(?:s|ed|ing)?\s+(?:\w+\s+){0,2}?(?:help|support|attention|coaching|mentoring|improv\w*)"
    r"|troubl\w*|difficult(?:y|ies)|problems?\s+(?:with|in|at|on|over|\w+ing)|mistakes?"
    r"|(?:further|furthest|farthest)\s+(?:away\s+)?from|deviat\w*|stray\w*|non-?complian\w*|non-?conform\w*"
    r"|(?:ignor|break|bend|skirt|flout|disregard)\w*\s+(?:the\s+|our\s+|their\s+)?(?:agile\s+)?"
    r"(?:process|workflow|rules?)"
    # The character words a question may use and an answer never does ("who is the laziest?", "who is slacking?",
    # "who is not pulling their weight?"): their figures in the same order.
    r"|lazy|laz(?:ier|iest|iness)|slack(?:er|ers|ing|ed)|unproductive|inefficien\w*|careless\w*|unreliab\w*"
    r"|incompeten\w*|useless\w*|hopeless\w*|(?:not|n['’]t|never)\s+(?:\w+\s+)?pull\w*\s+(?:their|his|her)\s+"
    r"(?:own\s+)?weight)\b"
)
# A measure named the other way round turns the order: going over the effort limit is the other side of "Done within
# the effort limit" (and so is the effort ratio, actual over estimate, R-EFF-01's term: the higher it is, the further
# over), and a measure's words after a negation ("without an estimate", "not logging time", "never reopened") or with
# "un" before them ("unestimated", "unsized") are its other side too. The most of that is the fewest of the measure:
# "who overruns the most?" and "who has the highest effort ratio?" are the lowest share done within the limit first,
# "who has the most work without an estimate?" the lowest share estimated before it started. A negation stands at most
# two words before the measure's words.
_OVER_THE_LIMIT = (
    r"\b(?:over|beyond)\s+(?:the |their |its )?(?:estimates?|effort(?: limit)?|limit)\b|\bover[\s-]?r[au]n\w*"
    r"|\bburn\w*|\bexceed\w*|\beffort[\s-]?ratios?\b|\bover-?spen[dt]\w*"
    r"|\bactuals?\s+(?:vs\.?|versus|against|over|to)\s+(?:the\s+)?estimates?\b"
)
_NEGATION_BEFORE = (
    r"(?:\bnot|\bnever|\bno|\bwithout|n['’]t|\bmissing|\black(?:s|ing)?|\bfail(?:s|ed|ing)?\s+to"
    r"|\bforg[eo]t(?:s|ten)?\s+to)\s+(?:\w+\s+){0,2}$"
)
# The ratio named as one ("open findings per 10 open tickets", "findings per ticket", "the findings rate"): findings
# named alone are a count, JO-09's ranking by open findings.
_RATIO = (
    r"\bper\s+(?:10|ten)\b|\b(?:findings?|breach(?:es)?|warnings?)\s+(?:per\s+(?:open\s+)?(?:tickets?|issues?)"
    r"|ratio|rate|density)\b"
)
# What one mentioned person is working on: their tickets in progress, as the dashboard's sample reads it.
_WORKING_ON = r"\bwork(?:s|ing)? on\b|\bdoing\b|\bin progress\b|\bbusy with\b|\bpicked up\b"
# Work someone finished (or contributed): their tickets done in the period (one person), or everyone by that count.
_FINISHED = (
    r"\bclos(?:e|es|ed|ing)\b|\bdone\b|\bfinish\w*|\bcomplet(?:e|es|ed|ing)\b|\bresolv(?:e|es|ed|ing)\b"
    r"|\bdeliver(?:s|ed|ing)?\b|\bshipp?(?:ed|ing)?\b|\bcontribut\w*"
)
# ... but not work still to finish ("not done yet").
_NOT_FINISHED = r"\b(?:not|never)\s+(?:yet\s+)?(?:been\s+)?(?:done|finished|closed|completed?|resolved)\b"
# The members of the board who hold none of its tickets.
_NO_TICKETS = r"\bno tickets?\b|\bwithout (?:any |a )?tickets?\b|\bnothing assigned\b|\bno (?:work|issues?) assigned\b"
# Who is on the board: everyone on it, those who hold its tickets and its members who hold none.
_ON_THE_BOARD = (
    r"\b(?:on|in|of) (?:this|the) (?:board|team|project)\b|\bwho works? (?:here|on (?:this|the) board)\b"
    r"|\bmembers?\b|\bmembership\b|\broster\b|\bheadcount\b|\bwho (?:is|are) here\b"
)
# The scorecard's periods in words, for the scorecard and the comparison (the dashboard's sample reads the same words,
# src/server/sample/ask/scorecard.ts: daysIn): the longest first, then the shortest; 90 days otherwise.
_HALF_YEAR = (
    r"\b(?:180|hundred and eighty)\s+days\b|\b(?:6|six)\s+months\b|\bhalf[\s-](?:a[\s-])?year\b"
    r"|\b(?:26|twenty[\s-]six)\s+weeks\b|\ball[\s-]time\b|\bsince the (?:beginning|start)\b"
)
_SHORT_PERIOD = (
    r"\b(?:last|this|active|current|the)\s+sprint\b|\b(?:7|seven|30|thirty)\s+days\b"
    r"|\b(?:this|last|past|previous)\s+(?:week|month)\b|\b(?:4|four)\s+weeks\b|\ba\s+month\b"
)
# The scorecard measure a question names, for the comparison: first match wins (the effort words before "estimate").
# The dashboard's sample reads the same words in the same order (src/server/sample/ask/scorecard.ts: MEASURE_WORDS).
_MEASURE_WORDS: tuple[tuple[str, str], ...] = (
    (r"\b(?:within|over|under|beyond) (?:the |their |its )?(?:estimates?|effort(?: limit)?|limit)\b"
     r"|\bover[\s-]?r[au]n\w*|\beffort\b|\bburn|\bexceed\w*\s+(?:the |their |its )?(?:estimates?|effort|limit)\b"
     r"|\bover-?spen[dt]\w*|\bactuals?\s+(?:vs\.?|versus|against|over|to)\s+(?:the\s+)?estimates?\b",
     "within_estimate"),
    (r"\bun-?estimat\w*|\bunsized\b|\bestimat\w*|\bsized?\b|\bsizing\b|\bstory points?\b", "estimated_before_start"),
    (r"\blog(?:s|ged|ging)?\s+(?:\w+\s+){0,2}?time\b|\btime[\s-]?(?:logged|logging|tracking|sheets?)\b"
     r"|\bworklogs?\b|\btimesheets?\b"
     # Hours, as executives say it, in either word order ("not logging hours", "booking their hours", "logged the
     # most hours", "has the most hours logged"): one question, one figure. Hours are not added up per person, so the
     # time logged on done work is the figure for every one of them (the JO-12 check: never the overall comparison).
     r"|\b(?:log|book|record)(?:s|ed|ged|ing|ging)?\s+"
     r"(?:(?:their|his|her|any|the|most|more|fewest|fewer|least|less|no)\s+){0,2}hours\b"
     # ... and time or hours with what is done with them after ("how much time do they log on their done work?",
     # "how many hours did she book?", "how much time does he track?").
     r"|\b(?:time|hours)\s+(?:\w+\s+){0,4}?(?:log|book|record|track)(?:s|ed|ged|ing|ging)?\b"
     # Time tracked, recorded, booked or filled in ("do they track time?", "who records their time?", "does she
     # book time?", "who keeps track of their time?", "who fills in their timesheet?"), and hours worked or spent
     # ("who worked the most hours?"): time logged too, never their findings or tickets (the JO-12 check).
     r"|\b(?:track|record|book)(?:s|ed|ing)?\s+(?:of\s+)?"
     r"(?:(?:their|his|her|any|the|most|more|fewest|fewer|least|less|no|all)\s+){0,2}time\b"
     r"|\bfill(?:s|ed|ing)?\s+in\s+(?:(?:their|his|her|any|the)\s+){0,2}(?:time|hours|timesheets?)\b"
     r"|\b(?:work(?:s|ed|ing)?|spen(?:d|ds|ding|t))\s+"
     r"(?:(?:their|his|her|any|the|most|more|fewest|fewer|least|less|no)\s+){0,2}hours\b", "time_logged"),
    (r"\bstuck\b|\bstalled\b|\bstale\b|\bsitting\b|\blinger\w*", "stuck_now"),
    (r"\bre-?open\w*|\bbounced back\b|\bcame back\b", "reopened"),
    (r"\bskip\w*|\bstraight to done\b|\bjump\w*|\bbypass\w*", "skipped_stage"),
    (r"\bcode[\s-]?reviews?\b|\breview(?:s|ed|ing)?\s+(?:the\s+|their\s+)?code\b|\breviewed\b", "code_review"),
    (r"\bfindings?\b|\bbreach\w*|\bwarnings?\b|\bviolations?\b|\bbroken rules?\b", "open_findings_per_10"),
)
# What a question about people asks for: tickets that have findings, findings by severity (or all of them), or the
# tickets someone holds.
_PEOPLE_TOPICS: tuple[tuple[str, str], ...] = (
    (r"\b(?:tickets?|issues?)\s+(?:with|that\s+have|having)\s+(?:any\s+|open\s+)?(?:findings?|problems?|breaches|warnings)\b",
     "tickets_with_findings"),
    (r"\bbreach|\bviolation", "breach"),
    (r"\bwarning", "warning"),
    (r"\bfinding|\bproblems?\b|\bbroken rules?\b|\bflagged\b", "finding"),
    (r"\bworking on\b|\bwork(?:load)?\b|\btickets?\b|\bissues?\b|\bassigned\b|\bholds?\b|\bholding\b|\bbusy\b", "tickets"),
)
# The figure the people ranking orders by, for each topic: "findings" are breaches and warnings together, so the
# leader is who holds the most of both, never the most breaches alone.
_RANK_BY = {"breach": "open_breaches", "warning": "open_warnings", "finding": "open_findings", "tickets": "open_tickets",
            "tickets_with_findings": "tickets_with_findings"}

_TREND_WORDS = (
    r"\btrend|\bover time\b|\bweek (?:by|on|over) week\b|\bweekly\b|\bgetting (?:worse|better)\b"
    r"|\bimproving\b|\bworsening\b"
)
_NO_ESTIMATE_WORDS = (
    r"\bwithout (?:an? )?estimates?\b|\bno estimates?\b|\bun-?estimated\b|\bnot estimated\b"
    r"|\bmissing (?:an? )?estimates?\b|\black(?:s|ing)? (?:an? )?estimates?\b"
)
_COVERAGE_WORDS = (
    r"\bcoverage\b|\bhow much of\b|\bhow much work\b|\bwhat (?:share|fraction|percentage|proportion|percent)\b"
    r"|\bshare of\b|\bpercentage of\b|\bproportion of\b|\bha(?:s|ve|d) an estimate\b"
    r"|\bwith an estimate\b|\bestimated\b|\bestimate before\b|\bha(?:s|ve|d) (?:story )?points\b"
    r"|\bha(?:s|ve|d) (?:code[\s-]?review|test) evidence\b|\bha(?:s|ve|d) time logged\b"
)
_STUCK_WORDS = (
    r"\bstuck\b|\blongest\b|\btime in status\b|\bsits?\b|\bsitting\b|\bhow long\b|\boldest\b"
    r"|\bstatus age\b|\bwait(?:s|ing|ed)?\b|\bidle\b|\bstale\b|\bbottlenecks?\b|\blinger"
)
_EFFORT_WORDS = (
    r"\beffort\b|\bover(?:ran|run|runs|running|shot|spent)\b|\bbeyond (?:the |their )?estimates?\b"
    r"|\bover (?:the |their )?estimates?\b|\bover budget\b|\bactual(?:s| hours| time)?\b"
    r"|\bestimates? (?:vs\.?|versus|against) actual"
)


@dataclass(frozen=True)
class _Question:
    lower: str
    project: str | None
    rule_id: str | None
    issue_key: str | None
    n: int | None
    period: str | None
    weeks: int | None

    def has(self, pattern: str) -> bool:
        return re.search(pattern, self.lower) is not None


def _parse(question: str, default_project: str) -> _Question:
    text = " ".join(question.split())
    rule = _RULE_ID.search(text)
    # Rule ids come out before keys: "FLOW-02" in "R-FLOW-02" looks like a key.
    scrubbed = _RULE_ID.sub(" ", text)
    key = _ISSUE_KEY.search(scrubbed)
    issue_key = key.group(1) if key else None
    lower = text.lower()
    top = _TOP_N.search(lower)
    weeks = _LAST_N_WEEKS.search(lower)
    return _Question(
        lower=lower,
        project=_project_in(scrubbed, issue_key) or (default_project.strip().upper() or None),
        rule_id=rule.group(1).upper() if rule else None,
        issue_key=issue_key,
        n=_number(top.group(1) or top.group(2)) if top else None,
        period=next((period for pattern, period in _PERIOD_PATTERNS if re.search(pattern, lower)), None),
        weeks=int(weeks.group(1)) if weeks else None,
    )


def _number(word: str) -> int:
    return _WORD_NUMBERS.get(word) or int(word)


def _project_in(text: str, issue_key: str | None) -> str | None:
    if issue_key is not None:
        return issue_key.rsplit("-", 1)[0]
    for token in _PROJECT_KEY.findall(_ISSUE_KEY.sub(" ", text)):
        if token not in _NOT_PROJECTS:
            return token
    return None


# --- shared parameter helpers ----------------------------------------------------


def _call(name: str, **params: Any) -> PlanCall:
    return PlanCall(name=name, params={key: value for key, value in params.items() if value is not None})


def _cap(n: int | None, maximum: int) -> int | None:
    return None if n is None else max(1, min(n, maximum))


def _counting(q: _Question) -> bool:
    return q.has(r"\bhow many\b|\bcount\b|\bnumber of\b")


def _asks_for_tickets(q: _Question) -> bool:
    """"Which tickets ...": a list of issues rather than a grouped view.

    One adjective may sit between the verb and the noun ("which open tickets",
    "top 5 issues"); "which ticket types" is a grouping and does not match.
    """
    return q.has(
        r"\b(?:which|what|list|show|top)\s+(?:me\s+)?(?:the\s+|all\s+)?"
        r"(?:(?!tickets?\b|issues?\b)\w+\s+)?(?:tickets|issues)\b"
    )


def _findings(q: _Question, *, rule_id: str | None = None, severity: str | None = None) -> PlanCall:
    if q.has(r"\bresolved\b|\bclosed\b|\bfixed\b"):
        state = "resolved"
    elif q.has(r"\bopen\b|\bcurrently\b|\bright now\b|\bstill\b"):
        state = "open"
    else:
        state = "all" if q.period else "open"
    return _call(
        "oversight_list_findings",
        project=q.project,
        rule_id=rule_id,
        severity=severity,
        state=state,
        period=q.period,
        n=_cap(q.n, 50),
    )


def _top(q: _Question, metric: str) -> PlanCall:
    return _call("oversight_top_issues", project=q.project, metric=metric, n=_cap(q.n, 20))


def _count(q: _Question, *, has_estimate: bool | None = None) -> PlanCall:
    status = None
    # "Open", "not done" and "in progress" are status categories, over the board's work, as the Overview and the
    # Portfolio count them (one meaning of open tickets everywhere); "to do" and "done" stay the statuses so named.
    state = ("open" if q.has(r"\bopen\b|\bnot (?:yet )?done\b|\bunfinished\b|\boutstanding\b")
             else "in_progress" if q.has(r"\bin progress\b|\bwip\b") else None)
    for pattern, name in ((r"\bto do\b", "To Do"), (r"\bdone\b", "Done")):
        if state is None and q.has(pattern):
            status = name
            break
    issue_type = None
    for pattern, name in (
        (r"\bbugs?\b", "Bug"),
        (r"\btasks?\b", "Task"),
        (r"\bstor(?:y|ies)\b", "Story"),
        (r"\bepics?\b", "Epic"),
        (r"\bsub-?tasks?\b", "Subtask"),
    ):
        if q.has(pattern):
            issue_type = name
            break
    return _call(
        "oversight_count_issues",
        project=q.project,
        status=status,
        issue_type=issue_type,
        period=q.period,
        has_estimate=has_estimate,
        state=state,
    )


def _group_by(q: _Question, *, default: str, allowed: tuple[str, ...]) -> str:
    if "issue_type" in allowed and q.has(r"\btypes?\b"):
        return "issue_type"
    if "epic" in allowed and q.has(r"\bepics?\b"):
        return "epic"
    if "sprint" in allowed and q.has(r"\b(?:by|per|each)\s+sprint\b|\bsprints\b"):
        return "sprint"
    if "status" in allowed and q.has(r"\bstatus(?:es)?\b"):
        return "status"
    return default


def _last_n(q: _Question) -> int:
    if q.weeks:
        return min(q.weeks, _MAX_WEEKS)
    return _WEEKS_FOR.get(q.period or "", _MAX_WEEKS)


def _trend_metric(q: _Question) -> str | None:
    for pattern, metric in (
        (r"\beffort\b|\bestimates?\b", "median_effort_ratio"),
        (r"\bcycle\b", "median_cycle_time_h"),
        (r"\bskip|\bstraight\b", "skipped_stage_share"),
        (r"\breopen", "reopen_rate"),
        (r"\bbreach", "open_breaches"),
        (r"\bwarning", "open_warnings"),
    ):
        if q.has(pattern):
            return metric
    return None


def _coverage_field(q: _Question) -> str:
    for pattern, field in (
        (r"\bstory points?\b|\bpoints\b", "story_points"),
        (r"\blogged\b|\bworklogs?\b|\btime spent\b|\bhours booked\b", "time_logged"),
        (r"\bcode[\s-]?review", "code_review"),
        (r"\btest", "test_evidence"),
    ):
        if q.has(pattern):
            return field
    return "estimate"


# --- the routing table ------------------------------------------------------------
# Each rule returns None when it does not apply, [] when it applies but nothing
# can be planned (the plan is then "not understood"), else the calls.

Rule = Callable[[_Question], "list[PlanCall] | None"]


def _rule_issue_timeline(q: _Question) -> list[PlanCall] | None:
    if q.issue_key is None:
        return None
    return [_call("oversight_issue_timeline", issue_key=q.issue_key)]


def _rule_by_rule_id(q: _Question) -> list[PlanCall] | None:
    if q.rule_id is None:
        return None
    return [_findings(q, rule_id=q.rule_id)]


def _rule_trend(q: _Question) -> list[PlanCall] | None:
    if not q.has(_TREND_WORDS):
        return None
    metric = _trend_metric(q)
    if metric is None:
        return []
    return [_call("oversight_trend", project=q.project, metric=metric, last_n=_last_n(q))]


def _rule_hot_spots(q: _Question) -> list[PlanCall] | None:
    if not (q.has(r"\bhot[\s-]?spots?\b") or (q.has(r"\bepics?\b") and q.has(r"\bbreach"))):
        return None
    return [_call("oversight_hot_spots", project=q.project, n=_cap(q.n, 10))]


def _rule_rule_status(q: _Question) -> list[PlanCall] | None:
    if not q.has(r"\brules?\b"):
        return None
    if not q.has(r"\bmeasur|\bcan(?:['’]|no)?t\b|\bcannot\b|\bunable\b|\bstatus\b|\bwhich\b|\bwhat\b|\bcatalog"):
        return None
    return [_call("oversight_rule_status", project=q.project)]


def _rule_no_estimate(q: _Question) -> list[PlanCall] | None:
    if not q.has(_NO_ESTIMATE_WORDS):
        return None
    if _counting(q):
        return [_count(q, has_estimate=False)]
    return [_findings(q, rule_id="W-EST-01" if q.has(r"\bsprint\b") else "R-EST-01")]


def _rule_coverage(q: _Question) -> list[PlanCall] | None:
    if not q.has(_COVERAGE_WORDS):
        return None
    field = _coverage_field(q)
    calls = [_call("oversight_coverage", project=q.project, field=field, period=q.period or _DEFAULT_PERIOD)]
    # "Estimated before it started" is also the question R-EST-02 answers.
    if field == "estimate" and q.has(r"\bestimated\b|\bestimate before\b|\bbefore (?:it|work|they|the work) (?:was )?start"):
        calls.append(_findings(q, rule_id="R-EST-02"))
    return calls


def _rule_cycle_time(q: _Question) -> list[PlanCall] | None:
    if not q.has(r"\bcycle[\s-]?times?\b|\blead[\s-]?times?\b"):
        return None
    if _asks_for_tickets(q) or q.has(r"\blongest\b|\btop\b"):
        return [_top(q, "cycle_time")]
    return [_call("oversight_trend", project=q.project, metric="median_cycle_time_h", last_n=_last_n(q))]


def _rule_stuck(q: _Question) -> list[PlanCall] | None:
    if not q.has(_STUCK_WORDS):
        return None
    if q.has(r"\bstuck\b") and _asks_for_tickets(q):
        return [_findings(q, rule_id="W-AGE-01")]
    if _asks_for_tickets(q) or q.has(r"\boldest\b|\bstatus age\b"):
        return [_top(q, "status_age")]
    group_by = _group_by(q, default="status", allowed=("status", "issue_type", "epic"))
    return [
        _call("oversight_time_in_status", project=q.project, group_by=group_by, period=q.period or _DEFAULT_PERIOD)
    ]


def _rule_skipped(q: _Question) -> list[PlanCall] | None:
    if not q.has(r"\bstraight\b|\bskip|\bto do (?:straight )?to done\b|\bjump"):
        return None
    return [_findings(q, rule_id="R-FLOW-02")]


def _rule_review(q: _Question) -> list[PlanCall] | None:
    if not q.has(r"\bcode[\s-]?review|\breviewed\b|\bwithout (?:a )?review\b|\btesting\b"):
        return None
    return [_findings(q, rule_id="R-FLOW-01")]


def _rule_reopened(q: _Question) -> list[PlanCall] | None:
    if not q.has(r"\breopen"):
        return None
    if q.has(r"\bmost\b|\btop\b"):
        return [_top(q, "reopens")]
    return [_findings(q, rule_id="R-FLOW-03")]


def _rule_burn(q: _Question) -> list[PlanCall] | None:
    if not q.has(r"\bburn"):
        return None
    return [_top(q, "effort_ratio")]


def _rule_unassigned(q: _Question) -> list[PlanCall] | None:
    if not q.has(r"\bunassigned\b|\bno assignee\b|\bwithout (?:an )?assignee\b|\bnobody assigned\b|\bnot assigned\b"):
        return None
    return [_findings(q, rule_id="R-HYG-01")]


def _rule_at_risk(q: _Question) -> list[PlanCall] | None:
    if not q.has(r"\bat risk\b"):
        return None
    return [_findings(q, rule_id="W-SPR-01")]


def _rule_effort(q: _Question) -> list[PlanCall] | None:
    if not q.has(_EFFORT_WORDS):
        return None
    if _asks_for_tickets(q) or q.has(r"\btop\b"):
        return [_top(q, "effort_ratio")]
    group_by = _group_by(q, default="epic", allowed=("epic", "issue_type", "sprint"))
    return [
        _call(
            "oversight_effort_vs_estimate", project=q.project, group_by=group_by, period=q.period or _DEFAULT_PERIOD
        )
    ]


def _rule_findings(q: _Question) -> list[PlanCall] | None:
    if not q.has(r"\bbreach|\bwarning|\bfinding|\bviolation|\bbroken rules?\b|\bproblems?\b"):
        return None
    if q.has(r"\bbreach|\bviolation"):
        severity: str | None = "breach"
    elif q.has(r"\bwarning"):
        severity = "warning"
    else:
        severity = None
    return [_findings(q, severity=severity)]


def _rule_count(q: _Question) -> list[PlanCall] | None:
    if not (_counting(q) or _asks_for_tickets(q)):
        return None
    return [_count(q)]


_RULES: tuple[Rule, ...] = (
    _rule_issue_timeline,  # 1
    _rule_by_rule_id,  # 2
    _rule_trend,  # 3
    _rule_hot_spots,  # 4
    _rule_rule_status,  # 5
    _rule_no_estimate,  # 6
    _rule_coverage,  # 7
    _rule_cycle_time,  # 8
    _rule_stuck,  # 9
    _rule_skipped,  # 10
    _rule_review,  # 11
    _rule_reopened,  # 12
    _rule_burn,  # 13
    _rule_unassigned,  # 14
    _rule_at_risk,  # 15
    _rule_effort,  # 16
    _rule_findings,  # 17
    _rule_count,  # 18
)


def route(question: str, catalog_names: list[str], *, project: str, mentions: Sequence[str] = ()) -> Plan:
    """Turn a question into catalogue calls by the table in the module docstring. mentions: the person ids the
    question mentions (people level only)."""
    q = _parse(question or "", project or "")
    if not q.lower and not mentions:
        return Plan(understood=False)
    available = set(catalog_names)
    calls: list[PlanCall] = []
    if PEOPLE_NAMES[0] in available:  # people level: the person tools are on offer
        about_people = _route_people(q, list(dict.fromkeys(mentions)), available)
        if isinstance(about_people, Plan):
            return about_people
        calls = about_people or []
    elif _PERSON.search(q.lower):
        return Plan(understood=False, reason=PERSON_REASON)
    if not calls:
        for rule in _RULES:
            matched = rule(q)
            if matched is not None:
                calls = matched
                break
    calls = [call for call in calls if call.name in available][:MAX_CALLS]
    if not calls:
        return Plan(understood=False)
    return Plan(understood=True, calls=calls)


# --- people level ---------------------------------------------------------------------


def _route_people(q: _Question, mentions: list[str], available: set[str]) -> Plan | list[PlanCall] | None:
    """A question about people: its calls, or the Plan that does not understand it (or refuses a comparison that is
    turned off). None: the question is about no one, so the routing table answers it."""
    if not mentions and not _about_people(q):
        return None
    if _judging(q, mentions) is not None:
        return _scorecard_calls(q, mentions, available)
    topic = _people_topic(q)
    finished = _finished(q, topic)
    load = _load(q)
    if load == "spare" and _how_little_only(q) and _figure_named(q, finished):
        # "Who reopens the least work?", "who has the least work in progress?": the figure named, in the order asked,
        # never the open tickets a word of room would give (JO-14). A word of room itself ("capacity") still is.
        load = None
    if len(mentions) > 1:
        return _side_by_side(q, mentions, available, topic, finished, load)
    if mentions:
        (person,) = mentions
        if _scorecard_measure(q) is not None:
            # A scorecard measure named ("does @A skip stages?", "time logged by @A"): their scorecard, that measure
            # next to the whole board's with the rest, never their tickets and findings (the JO-12 check).
            return [_call(SCORECARD_TOOL, project=q.project, person_ids=[person], days=_scorecard_days(q))]
        if q.has(_BACKLOG) or q.has(_OVERLOAD) or load in ("spare", "busiest"):
            return [_person_tickets(q, person, state="open", n=WHOLE_LIST)]
        if finished:
            return [_person_tickets(q, person, state="done", n=WHOLE_LIST)]
        if q.has(_WORKING_ON) or q.has(_IDLE):
            return [_person_tickets(q, person, state="in_progress")]
        if topic is None:
            return [_person_tickets(q, person), _person_findings(q, person, "finding")]
        return [_person_call(q, person, topic)]
    # Nobody mentioned: an answer from the figures whenever one answers the question per person.
    if q.issue_key is not None:
        return [_call(ASSIGNEE_TOOL, project=q.project, issue_key=q.issue_key)]
    # A count most first, or fewest first when the question asks for the fewest (_FEWEST).
    counted = "fewest" if _order(q) == "fewest" else "most"
    # Who is idle, or has nothing to do, room for more or the least work: the count fewest first, and the members who
    # hold no ticket at all; their qualifiers and negations read first ("not idle", "at full capacity", "not
    # overloaded"), so an answer is never ordered against its question.
    if load == "idle":
        return _count_ranking(q, available, "in_progress", order="fewest", members=True)
    if load == "active":
        return _count_ranking(q, available, "in_progress", order="most")
    if load == "spare":
        return _count_ranking(q, available, "open_tickets", order="fewest", members=True)
    if load == "busiest":
        return _count_ranking(q, available, "open_tickets", order="most")
    measure = _measure_named(q)
    if measure is not None and (measure != "open_findings_per_10" or q.has(_RATIO)):
        # A measure named: its comparison, in the order the question asks (most, fewest, or closer first). Read before
        # work done or in progress, whose words name the tickets a rate is of ("reopened after Done", "time logged on
        # done work", "closed without code review", "stuck tickets in progress"): the rate is the figure asked for.
        return _scorecard_calls(q, [], available, measure=measure)
    if finished:
        return _count_ranking(q, available, "done_in_period", order=counted)
    if q.has(r"\bin progress\b|\bwip\b"):
        return _count_ranking(q, available, "in_progress", order=counted)
    if q.has(_NO_TICKETS):
        # The board's members who hold none of its tickets, as the @ list shows them.
        return [_call(BOARD_PEOPLE_TOOL, project=q.project, holding="none")]
    if q.has(_BUSIEST):
        # The busiest: the open tickets, most first; and for what someone is doing, the work done in the period too.
        busiest = _count_ranking(q, available, "open_tickets", order="most")
        if isinstance(busiest, Plan) or not q.has(_DOING):
            return busiest
        done = _count_ranking(q, available, "done_in_period", order="most")
        return busiest + (done if isinstance(done, list) else [])
    if q.has(_BACKLOG) or q.has(_OVERLOAD):
        # The scorecard's open tickets, as one person's backlog is counted (Epics left out), so the two agree.
        return _count_ranking(q, available, "open_tickets", order=counted)
    if topic is not None:
        return [_call("oversight_people", project=q.project, rank_by=_RANK_BY[topic],
                      order="fewest" if counted == "fewest" else None, n=_cap(q.n, 50))]
    if q.has(_ON_THE_BOARD):
        return [_call(BOARD_PEOPLE_TOOL, project=q.project, holding="any")]
    # Cycle time by person, say: no figure here is worked out per person.
    return Plan(understood=False, reason=PEOPLE_REASON)


def _load(q: _Question) -> str | None:
    """Which way a question about how much work people have points, its qualifiers and negations read before its words
    of room or load: "active" (not idle: in progress, most first), "idle" (in progress, fewest first, and the members
    who hold none), "busiest" (open tickets, most first), "spare" (open tickets, fewest first, and the members who hold
    none); None when it asks none of these."""
    if q.has(_NOT_IDLE):
        return "active"
    if q.has(_IDLE):
        return "idle"
    if q.has(_NOT_UNDERWORKED) or q.has(_NO_ROOM):
        return "busiest"
    if q.has(_NOT_OVERLOADED) or (q.has(_SPARE) and not q.has(_OVERLOAD)):
        return "spare"
    return None


def _how_little_only(q: _Question) -> bool:
    """Whether a question's words of room only say how little ("the least work", "does the least", "the fewest tasks";
    _SPARE_HOW_MUCH), with no word of room itself ("capacity", "available", "not overloaded")."""
    return q.has(_SPARE_HOW_MUCH) and not q.has(_SPARE_ROOM) and not q.has(_NOT_OVERLOADED)


def _figure_named(q: _Question, finished: bool) -> bool:
    """Whether a question names a figure of its own: work finished, work in progress, a rate, or the ratio named as
    one."""
    measure = _measure_named(q)
    return (finished or q.has(r"\bin progress\b|\bwip\b")
            or (measure is not None and (measure != "open_findings_per_10" or q.has(_RATIO))))


def _side_by_side(q: _Question, mentions: list[str], available: set[str], topic: str | None, finished: bool,
                  load: str | None) -> Plan | list[PlanCall]:
    """Two to ten people, side by side, by the figure the question names (the JO-12 review: never their breaches for a
    question about their tickets): a scorecard measure (that measure's comparison, those people only, then the board;
    read first, so "the time they log on their done work" is time logged, not work done), what they finished or have
    in progress (the comparison of that count, those people only), their open tickets, tickets with findings,
    findings, breaches or warnings (GET /people's rows for them, which compare no one: a raw count); their open
    breaches and warnings when it names none. Each in the order the question asks (JO-14): "who has more reopened
    tickets, @A or @B?" is the highest first, "fewer" the lowest first."""
    people = list(mentions)
    asked = _order(q)
    measure = _scorecard_measure(q)
    if measure is not None:
        if COMPARE_TOOL not in available:
            return Plan(understood=False, reason=COMPARISON_OFF_REASON)
        return [_call(COMPARE_TOOL, project=q.project, measure=measure, days=_scorecard_days(q),
                      order=_order(q, measure) or "closer", person_ids=people)]
    count = ("done_in_period" if finished
             else "in_progress" if q.has(r"\bin progress\b|\bwip\b") or q.has(_WORKING_ON) or load in ("idle", "active")
             else None)
    if count is not None:
        # What a few people are working on, or what they finished, where the comparison is off: each one's tickets,
        # which compare no one.
        asks_what = q.has(r"\bwork(?:s|ing)? on\b|\bdoing\b|\bbusy with\b|\bpicked up\b")
        if len(people) <= MAX_CALLS and (asks_what or COMPARE_TOOL not in available):
            state = "done" if count == "done_in_period" else "in_progress"
            return [_person_tickets(q, person, state=state, n=WHOLE_LIST if state == "done" else None)
                    for person in people]
        if COMPARE_TOOL not in available:
            return Plan(understood=False, reason=COMPARISON_OFF_REASON)
        return [_call(COMPARE_TOOL, project=q.project, measure=count,
                      days=None if count in NOW_COUNTS else _scorecard_days(q),
                      order="fewest" if asked == "fewest" or load == "idle" else "most", person_ids=people)]
    if topic is None and (q.has(_BACKLOG) or q.has(_OVERLOAD) or q.has(_BUSIEST) or load in ("spare", "busiest")):
        topic = "tickets"
    fewest = asked == "fewest" or load == "spare"
    if topic == "finding" and q.has(r"\bbreach|\bviolation") and q.has(r"\bwarning"):
        topic = None  # breaches and warnings both named: each one's of both, side by side, as below
    if topic is not None:
        return [_call("oversight_people", project=q.project, rank_by=_RANK_BY[topic],
                      order="fewest" if fewest else None, person_ids=people)]
    # Nothing named: each one's open breaches and warnings, from one read of their rows.
    return [_call("oversight_people", project=q.project, rank_by="open_breaches", person_ids=people)]


def _finished(q: _Question, topic: str | None) -> bool:
    """Whether a question asks about work someone finished (not work still to finish)."""
    return (q.has(_FINISHED) and not q.has(r"\bopen\b") and not q.has(_NOT_FINISHED)
            and topic in (None, "tickets"))


def _counted(q: _Question) -> bool:
    """Whether a question asks about one of the counts people can be put in order by: a backlog, a load (the busiest
    too, so "top 3 busiest people" is the open tickets, most first, never the overall comparison), what is in
    progress, what was finished, who is idle or has nothing to do."""
    return (q.has(_BACKLOG) or q.has(_OVERLOAD) or q.has(_SPARE) or q.has(_IDLE) or q.has(_NO_ROOM)
            or q.has(_BUSIEST) or q.has(r"\bin progress\b|\bwip\b") or _finished(q, _people_topic(q)))


def _count_ranking(q: _Question, available: set[str], measure: str, *, order: str, members: bool = False
                   ) -> Plan | list[PlanCall]:
    """Everyone by a count (oversight_people_compare): order "most" (most first) or "fewest"; a count of now (open
    tickets, in progress) over no period. members: the board's members who hold no ticket at all as well, for a
    question about who has the least (they have less than anyone)."""
    if COMPARE_TOOL not in available:
        return Plan(understood=False, reason=COMPARISON_OFF_REASON)
    calls = [_call(COMPARE_TOOL, project=q.project, measure=measure,
                   days=None if measure in NOW_COUNTS else _scorecard_days(q), order=order, n=_cap(q.n, 50))]
    if members:
        calls.append(_call(BOARD_PEOPLE_TOOL, project=q.project, holding="none"))
    return calls


def _about_people(q: _Question) -> bool:
    """Whether a question at people level is about people: a person word, or a group word beside an evaluating one."""
    return _ABOUT_PEOPLE.search(q.lower) is not None or (
        _PEOPLE_AS_A_GROUP.search(q.lower) is not None and evaluates(q.lower))


def judging(question: str, mentions: Sequence[str] = ()) -> str | None:
    """How a people-level question evaluates people (JO-11): "personnel" (fire, promote, blame, trust, lazy, ...),
    "scorecard" (how someone is doing, best, worst, rank, ...), or None: it evaluates no one, it is about no one, or
    it ranks people by one of GET /people's counts, which stays JO-09's raw-count ranking."""
    return _judging(_parse(question or "", ""), list(dict.fromkeys(mentions)))


def asks_of_someone(question: str) -> bool:
    """Whether a question asks something of one person in its own words, as the router reads it with one mention:
    how they are doing (any word that evaluates people), their backlog or load, what they finished, what they work on
    or whether they are idle, their tickets or their findings. One that asks none of these ("when will the sprint
    end?") asks nothing of anyone, so a word in it that is also someone's first name is no one (the service's reading
    of a name typed in lower case)."""
    q = _parse(question or "", "")
    return (evaluates(q.lower) or _counted(q) or q.has(_WORKING_ON) or q.has(_FINISHED)
            or _people_topic(q) is not None)


# A creative piece asked for (18 Nov 2026: "Write a short poem about our sprint." got a rhyming poem): the piece, and a
# word that asks for one. "Story" and "rap" are left out: a Story is a Jira issue type, and RAP may be a board's key.
_CREATIVE_PIECE = re.compile(
    r"\b(?:poems?|poetry|poetic|haikus?|limericks?|sonnets?|verses?|rhym(?:e|es|ed|ing)|lyrics|songs?|ballads?"
    r"|jokes?|riddles?)\b"
)
_CREATIVE_ASK = re.compile(
    r"\b(?:write|writes|writing|compose|composing|make|create|give|tell|generate|draft|sing|turn|put|craft)\b"
    r"|\bin\s+(?:verse|rhyme)\b"
)


def creative(question: str) -> bool:
    """Whether a question asks for a creative piece (verse, a song, a joke) rather than about the work: a piece and a
    word that asks for one ("write", "tell", "in verse"). Ask writes no such piece, whatever it is about; a question
    that only mentions one ("which tickets mention songs?") asks nothing of the kind."""
    text = (question or "").lower()
    return _CREATIVE_PIECE.search(text) is not None and _CREATIVE_ASK.search(text) is not None


def judging_plan(question: str, catalog_names: Sequence[str], *, project: str, mentions: Sequence[str] = ()
                 ) -> Plan | None:
    """The plan for a question that evaluates people, made in code: the scorecard of the people it mentions, or the
    comparison of everyone, or the refusal when comparing people is turned off (the comparison is not in
    catalog_names). None for any other question. The service plans such a question here in catalogue mode (and as
    the analyst's fallback), so it is never declined and never waits on a model."""
    q = _parse(question or "", project or "")
    people = list(dict.fromkeys(mentions))
    if _judging(q, people) is None:
        return None
    planned = _scorecard_calls(q, people, set(catalog_names))
    return planned if isinstance(planned, Plan) else Plan(understood=True, calls=planned)


def last_resort(question: str, catalog_names: Sequence[str], *, project: str, mentions: Sequence[str] = ()
                ) -> Plan | None:
    """The last resort for a question about people that nothing here places (JO-12, G3/G4: never turn an admin away):
    the overall comparison of everyone (OVERALL, never a measure: one that was named would have been placed), or the
    scorecards of the people it mentions, over the period its words give. None for a question about no one, and where
    the comparison is needed but not in catalog_names (comparing people turned off): the reason it was not placed
    stands. The service says first which figures the answer uses (prompts.LAST_RESORT_OVERALL, LAST_RESORT_SCORECARD)."""
    q = _parse(question or "", project or "")
    people = list(dict.fromkeys(mentions))
    if not people and not _about_people(q):
        return None
    # The overall comparison, closer to the process first: a direction word alone here ("who writes the most
    # comments?") is about something no figure counts, never the order of these figures. A question about the other
    # side of the process ("who makes the most mistakes?", "who has the most trouble?") is further from it first, and
    # the least of that closer first, as everywhere (_order).
    order = _order(q, OVERALL) if _further(q) else "closer"
    planned = _scorecard_calls(q, people, set(catalog_names), measure=OVERALL, order=order)
    return None if isinstance(planned, Plan) else Plan(understood=True, calls=planned)


def _judging(q: _Question, mentions: Sequence[str]) -> str | None:
    if not mentions and not _about_people(q):
        return None
    if not evaluates(q.lower):
        return None
    if (ranks_only(q.lower) and (_people_topic(q) is not None or _counted(q))
            and _measure_named(q) in (None, "open_findings_per_10")):
        # "top 5 people by open breaches": JO-09's ranking by a count, as it was; "rank people by backlog", by the
        # count. A ranking that names a compared measure ("rank people by reopened tickets") is that measure's
        # comparison, whatever ticket words come with it.
        return None
    return "personnel" if personnel(q.lower) else "scorecard"


def _measure_named(q: _Question) -> str | None:
    """The scorecard measure a question names for the comparison (_MEASURE_WORDS, first match), if any."""
    return next((key for pattern, key in _MEASURE_WORDS if q.has(pattern)), None)


def _order(q: _Question, measure: str | None = None) -> str | None:
    """The order a question asks for (JO-14): "most" (the figure highest first), "fewest" (lowest first), "closer"
    (closer to the process first), or None when it asks for none of them (closer to the process first; a count, most
    first). measure: what it is answered by, a rate, the ratio, OVERALL or a count. A rate, the ratio or the overall
    order asked about from the other side of the process (_further: "who struggles the most?", "who is worst for
    reopened work?") is further from the process first, and "the least" of that closer first; a rate or the ratio
    named the other way round turns the order (_turned: "who overruns the most?")."""
    asked = _direction(q, measure)
    if measure is not None and measure not in RANKED_COUNTS and _further(q):
        return "closer" if asked == "fewest" else _further_first(measure)
    if asked is not None and measure is not None and measure != OVERALL and _turned(q, measure):
        return "fewest" if asked == "most" else "most"
    return asked


def _further_first(measure: str) -> str:
    """The order that puts those further from the process first: overall, the fewest measures closer first; a rate or
    the ratio, the highest first where lower is closer and the lowest first where higher is closer."""
    if measure == OVERALL:
        return "fewest"
    return "most" if BY_KEY[measure].direction == "lower_is_closer" else "fewest"


def _further(q: _Question) -> bool:
    """Whether a question asks about the other side of following the process (_FURTHER), with no negation before the
    words that say so."""
    return any(re.search(_NEGATION_BEFORE, q.lower[:found.start()]) is None
               for found in re.finditer(_FURTHER, q.lower))


def _direction(q: _Question, measure: str | None = None) -> str | None:
    """Which way a question's direction words point: "most", "fewest" or None. A range's first word decides ("from
    most to least", "highest to lowest"); "top N", "bottom N", "at the top" and "at the bottom" only where no other
    direction word stands ("top 3 people with the fewest reopened tickets" is fewest); a question that asks both ways
    ("who has the most reopened tickets and the fewest stuck?") takes the word nearest the measure's own words (here
    stuck: fewest); else the first word decides."""
    found = sorted([(hit.start(), hit.end(), "most") for hit in re.finditer(_MOST, q.lower)]
                   + [(hit.start(), hit.end(), "fewest") for hit in re.finditer(_FEWEST, q.lower)])
    if not found:
        top, bottom = re.search(_TOP, q.lower), re.search(_BOTTOM, q.lower)
        if top is not None and (bottom is None or top.start() < bottom.start()):
            return "most"
        return "fewest" if bottom is not None else None
    if len({kind for _start, _end, kind in found}) == 1:
        return found[0][2]
    for (_start, end, kind), (start, _end, other) in zip(found, found[1:]):
        if kind != other and re.fullmatch(r"\s+to\s+(?:the\s+)?", q.lower[end:start]):
            return kind
    words = dict((key, pattern) for pattern, key in _MEASURE_WORDS).get(measure or "")
    named = re.search(words, q.lower) if words is not None else None
    if named is None:
        return found[0][2]

    def gap(hit: tuple[int, int, str]) -> tuple[int, int]:
        start, end, _kind = hit
        return (named.start() - end, 0) if end <= named.start() else (max(0, start - named.end()), 1)

    return min(found, key=gap)[2]


def _turned(q: _Question, measure: str) -> bool:
    """Whether a question names a rate or the ratio the other way round (_OVER_THE_LIMIT, _NEGATION_BEFORE): going
    over the effort limit, the measure's words after a negation or with "un" before them ("unestimated"); both at once
    is the measure itself again."""
    words = dict((key, pattern) for pattern, key in _MEASURE_WORDS).get(measure)
    found = re.search(words, q.lower) if words is not None else None
    if found is None:
        return False
    negated = (re.search(_NEGATION_BEFORE, q.lower[:found.start()]) is not None
               or found.group().startswith("un"))
    over = measure == "within_estimate" and q.has(_OVER_THE_LIMIT)
    return negated != over


def _scorecard_measure(q: _Question) -> str | None:
    """The scorecard measure a question about people it mentions names, one that only the scorecard holds (time
    logged, stuck, reopened, skipped a stage, estimates, effort, code review, and the ratio when it is named as one:
    "findings per 10 tickets"); None for none, and for findings named alone, which GET /people's rows count for them
    (their open breaches and warnings)."""
    measure = _measure_named(q)
    return None if measure == "open_findings_per_10" and not q.has(_RATIO) else measure


def _scorecard_calls(q: _Question, mentions: list[str], available: set[str], *, measure: str | None = None,
                     order: str | None = None) -> Plan | list[PlanCall]:
    """The scorecards of the people mentioned, or with none the comparison of everyone by the measure (else the one
    the question names, else overall), in `order`, else the order the question asks for (_order: most or fewest),
    else closer to the process first."""
    days = _scorecard_days(q)
    if len(mentions) == 1:
        return [_call(SCORECARD_TOOL, project=q.project, person_ids=list(mentions), days=days)]
    if COMPARE_TOOL not in available:
        return Plan(understood=False, reason=COMPARISON_OFF_REASON)
    if mentions:
        named = measure if measure is not None and measure != OVERALL else _scorecard_measure(q)
        if named is not None and measure != OVERALL:
            # "Rank @A and @B by reopened tickets, highest first", "who is worse on reopened tickets, @A or @B?": that
            # measure's comparison, those people only, in the order asked, as _side_by_side plans one.
            return [_call(COMPARE_TOOL, project=q.project, measure=named, days=days,
                          order=effective_order(named, order or _order(q, named) or "closer"),
                          person_ids=list(mentions))]
        return [_call(SCORECARD_TOOL, project=q.project, person_ids=list(mentions), days=days)]
    measure = measure or _measure_named(q) or "overall"
    asked = effective_order(measure, order or _order(q, measure) or "closer")  # as the facts record it
    return [_call(COMPARE_TOOL, project=q.project, measure=measure, days=days, order=asked, n=_cap(q.n, 50))]


def _scorecard_days(q: _Question) -> int:
    """The scorecard's period from the words: 30, 90 (the default) or 180 days; a week, a month or a sprint reads as
    the shortest, half a year or all time as the longest. "Overall" ("is she doing well overall?") means in general,
    not all time."""
    if q.has(_HALF_YEAR):
        return 180
    if q.has(_SHORT_PERIOD):
        return 30
    return 90


def _people_topic(q: _Question) -> str | None:
    """What a question about people asks for; breaches and warnings named together are every finding."""
    if q.has(r"\bbreach|\bviolation") and q.has(r"\bwarning"):
        return "finding"
    return next((name for pattern, name in _PEOPLE_TOPICS if q.has(pattern)), None)


def _person_call(q: _Question, person: str, topic: str) -> PlanCall:
    return _person_tickets(q, person) if topic == "tickets" else _person_findings(q, person, topic)


def _person_tickets(q: _Question, person: str, *, state: str | None = None, n: int | None = None) -> PlanCall:
    if state is None:
        state = "all" if not q.has(r"\bopen\b") and q.has(r"\ball\b|\bever\b") else "open"
    days = _scorecard_days(q) if state == "done" else None
    return _call("oversight_person_tickets", project=q.project, person=person, state=state, days=days,
                 n=_cap(q.n, WHOLE_LIST) or n)


def _person_findings(q: _Question, person: str, topic: str) -> PlanCall:
    if q.has(r"\bresolved\b|\bclosed\b|\bfixed\b"):
        state = "resolved"
    elif not q.has(r"\bopen\b") and q.has(r"\ball\b|\bever\b"):
        state = "all"
    else:
        state = "open"
    severity = topic if topic in ("breach", "warning") else None
    return _call(
        "oversight_person_findings", project=q.project, person=person, severity=severity, state=state, n=_cap(q.n, 50)
    )
