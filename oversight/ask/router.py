"""Deterministic keyword routing of questions to catalogue calls, for the scripted provider."""
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
# The person tools: in the catalogue only at people level; the comparison only where allowed.
SCORECARD_TOOL = "oversight_person_scorecard"
COMPARE_TOOL = "oversight_people_compare"
ASSIGNEE_TOOL = "oversight_ticket_assignee"
BOARD_PEOPLE_TOOL = "oversight_board_people"
PEOPLE_NAMES: tuple[str, ...] = (
    "oversight_people", BOARD_PEOPLE_TOOL, "oversight_person_tickets", ASSIGNEE_TOOL, "oversight_person_findings",
    SCORECARD_TOOL, COMPARE_TOOL,
)
# The longest ticket list a plan asks for: the whole list, up to the facts' row limit.
WHOLE_LIST = 200

MAX_CALLS = 3
# The window the baseline report uses, for tools that need a period.
_DEFAULT_PERIOD = "90d"
# Weekly trend points to show for each period.
_WEEKS_FOR = {"7d": 2, "30d": 5, "90d": 12, "sprint": 3, "all": 12}
_MAX_WEEKS = 12

# Rule ids are read in any case; issue keys only in upper case, since "sprint-4" is not a key.
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

# "top 5", "the lowest 3", "which 3 people": at most that many, in the question's order.
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

# Questions about individuals, declined at team level before any routing.
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
# Group words for people, read only beside an evaluating word ("rank the team").
_PEOPLE_AS_A_GROUP = re.compile(
    r"\b(?:assignees?|dev|(?:the\s+)?team|squad|the\s+others|the\s+rest|each\s+other)\b"
)
# What one mentioned person has still to do; the dashboard's sample reads these lists alike.
_BACKLOG = r"\bbacklog\b|\bto[\s-]?do\b|\bopen work\b|\boutstanding\b|\bon (?:their|his|her) plate\b"
# A heavy load: the open tickets, most first (one person: their backlog).
_OVERLOAD = (
    r"\boverload\w*|\boverwhelm\w*|\bswamped\b|\bsnowed under\b|\bstretched(?: too)? thin\b|\btoo much (?:work|on)\b"
    r"|\b(?:heaviest|biggest|largest) (?:load|workload)\b|\b(?:over|at|beyond|out of) capacity\b|\bno (?:spare )?capacity\b"
)
# How much spare room people have: open tickets fewest first, plus members holding no ticket.
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
# Heavy load, or room negated ("at full capacity"): busiest first. Read before _SPARE, which shares words.
_NO_ROOM = (
    r"\b(?:no|zero|little|limited|least|full)\s+(?:spare\s+|free\s+)?(?:capacity|bandwidth|room)\b"
    r"|\b(?:at|near|nearing|reached|reaching|hit|hitting|over|beyond|out\s+of)\s+(?:(?:their|his|her|full)\s+)?capacity\b"
    r"|\b(?:not|never)\s+(?:very\s+)?available\b|n['’]t\s+(?:very\s+)?available\b|\bunavailable\b"
    r"|\b(?:can['’]?t|cannot|can\s+not|could\s*n['’]t|could\s+not|unable\s+to)\s+(?:take|handle|pick)\s+(?:on\s+|up\s+)?"
    r"(?:any\s+)?(?:more|extra|another|additional)\b"
    r"|\bmaxed(?:\s+out)?\b|\bflat\s+out\b|\bover-?worked\b|\bover-?stretched\b|\b(?:got|has|have|had)\s+(?:the\s+)?most\s+on\b"
)
# A negation (or "least") before a load word turns it round ("not overloaded" has room).
_NEGATED = r"(?:\bnot|n['’]t|\bnever|\bleast)\s+(?:very\s+|too\s+|that\s+)?"
_NOT_OVERLOADED = _NEGATED + r"(?:overload\w*|overwhelm\w*|swamped|snowed\s+under|stretched|over-?worked|over-?stretched)\b"
_NOT_UNDERWORKED = _NEGATED + r"under-?(?:used|utili[sz]ed|loaded|work\w*|employ\w*|occupied)\b"
_NOT_IDLE = _NEGATED + r"idle\b"
# The busiest: open tickets most first, plus tickets done in the period when the question says "doing".
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
# Direction words for the order a question asks: most first, fewest first, else closer to the process.
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
# The other side of following the process (struggling, the worst): its figure's order runs the other way.
_FURTHER = (
    r"\b(?:worst|worse|weak(?:est|er)?|poor(?:est|er|ly)?|bad(?:ly)?|slow(?:est|er|ly)?|underperform\w*|lagg\w*"
    r"|lags?|struggl\w*|(?:fall(?:s|ing|en)?|fell|running|lagging|is|are|was|were|get(?:s|ting)?|got)\s+behind"
    r"|need(?:s|ed|ing)?\s+(?:\w+\s+){0,2}?(?:help|support|attention|coaching|mentoring|improv\w*)"
    r"|troubl\w*|difficult(?:y|ies)|problems?\s+(?:with|in|at|on|over|\w+ing)|mistakes?"
    r"|(?:further|furthest|farthest)\s+(?:away\s+)?from|deviat\w*|stray\w*|non-?complian\w*|non-?conform\w*"
    r"|(?:ignor|break|bend|skirt|flout|disregard)\w*\s+(?:the\s+|our\s+|their\s+)?(?:agile\s+)?"
    r"(?:process|workflow|rules?)"
    # Character words a question may use and an answer never does: their figures in the same order.
    r"|lazy|laz(?:ier|iest|iness)|slack(?:er|ers|ing|ed)|unproductive|inefficien\w*|careless\w*|unreliab\w*"
    r"|incompeten\w*|useless\w*|hopeless\w*|(?:not|n['’]t|never)\s+(?:\w+\s+)?pull\w*\s+(?:their|his|her)\s+"
    r"(?:own\s+)?weight)\b"
)
# A measure named the other way round ("over the effort limit", "unestimated") turns the order.
_OVER_THE_LIMIT = (
    r"\b(?:over|beyond)\s+(?:the |their |its )?(?:estimates?|effort(?: limit)?|limit)\b|\bover[\s-]?r[au]n\w*"
    r"|\bburn\w*|\bexceed\w*|\beffort[\s-]?ratios?\b|\bover-?spen[dt]\w*"
    r"|\bactuals?\s+(?:vs\.?|versus|against|over|to)\s+(?:the\s+)?estimates?\b"
)
_NEGATION_BEFORE = (
    r"(?:\bnot|\bnever|\bno|\bwithout|n['’]t|\bmissing|\black(?:s|ing)?|\bfail(?:s|ed|ing)?\s+to"
    r"|\bforg[eo]t(?:s|ten)?\s+to)\s+(?:\w+\s+){0,2}$"
)
# The ratio named as one; findings named alone are a count.
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
# The scorecard's periods in words, longest first; 90 days otherwise. Matches the dashboard's sample.
_HALF_YEAR = (
    r"\b(?:180|hundred and eighty)\s+days\b|\b(?:6|six)\s+months\b|\bhalf[\s-](?:a[\s-])?year\b"
    r"|\b(?:26|twenty[\s-]six)\s+weeks\b|\ball[\s-]time\b|\bsince the (?:beginning|start)\b"
)
_SHORT_PERIOD = (
    r"\b(?:last|this|active|current|the)\s+sprint\b|\b(?:7|seven|30|thirty)\s+days\b"
    r"|\b(?:this|last|past|previous)\s+(?:week|month)\b|\b(?:4|four)\s+weeks\b|\ba\s+month\b"
)
# Scorecard measure words, first match wins; same order as the dashboard's sample (MEASURE_WORDS).
_MEASURE_WORDS: tuple[tuple[str, str], ...] = (
    (r"\b(?:within|over|under|beyond) (?:the |their |its )?(?:estimates?|effort(?: limit)?|limit)\b"
     r"|\bover[\s-]?r[au]n\w*|\beffort\b|\bburn|\bexceed\w*\s+(?:the |their |its )?(?:estimates?|effort|limit)\b"
     r"|\bover-?spen[dt]\w*|\bactuals?\s+(?:vs\.?|versus|against|over|to)\s+(?:the\s+)?estimates?\b",
     "within_estimate"),
    (r"\bun-?estimat\w*|\bunsized\b|\bestimat\w*|\bsized?\b|\bsizing\b|\bstory points?\b", "estimated_before_start"),
    (r"\blog(?:s|ged|ging)?\s+(?:\w+\s+){0,2}?time\b|\btime[\s-]?(?:logged|logging|tracking|sheets?)\b"
     r"|\bworklogs?\b|\btimesheets?\b"
     # Hours logged, in either word order; hours are not summed per person, so this is time logged on done work.
     r"|\b(?:log|book|record)(?:s|ed|ged|ing|ging)?\s+"
     r"(?:(?:their|his|her|any|the|most|more|fewest|fewer|least|less|no)\s+){0,2}hours\b"
     # ... and time or hours with what is done with them after ("how many hours did she book?").
     r"|\b(?:time|hours)\s+(?:\w+\s+){0,4}?(?:log|book|record|track)(?:s|ed|ged|ing|ging)?\b"
     # Time tracked, recorded, booked or filled in, and hours worked: time logged, never findings or tickets.
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
# What a people question asks for: tickets with findings, findings by severity, or tickets held.
_PEOPLE_TOPICS: tuple[tuple[str, str], ...] = (
    (r"\b(?:tickets?|issues?)\s+(?:with|that\s+have|having)\s+(?:any\s+|open\s+)?(?:findings?|problems?|breaches|warnings)\b",
     "tickets_with_findings"),
    (r"\bbreach|\bviolation", "breach"),
    (r"\bwarning", "warning"),
    (r"\bfinding|\bproblems?\b|\bbroken rules?\b|\bflagged\b", "finding"),
    (r"\bworking on\b|\bwork(?:load)?\b|\btickets?\b|\bissues?\b|\bassigned\b|\bholds?\b|\bholding\b|\bbusy\b", "tickets"),
)
# The figure each topic ranks by; "findings" are breaches and warnings together.
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
    """"Which tickets ...": a list of issues rather than a grouped view."""
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
    # "Open", "not done" and "in progress" are status categories; "to do" and "done" stay status names.
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


# Each rule returns None when it does not apply, [] when nothing can be planned, else the calls.

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
    """Turn a question into catalogue calls; mentions are the person ids it mentions (people level only)."""
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
    """A question about people: its calls or a not-understood Plan; None when it is about no one."""
    if not mentions and not _about_people(q):
        return None
    if _judging(q, mentions) is not None:
        return _scorecard_calls(q, mentions, available)
    topic = _people_topic(q)
    finished = _finished(q, topic)
    load = _load(q)
    if load == "spare" and _how_little_only(q) and _figure_named(q, finished):
        # A figure named with "least" ("who reopens the least work?") is that figure, not the open tickets.
        load = None
    if len(mentions) > 1:
        return _side_by_side(q, mentions, available, topic, finished, load)
    if mentions:
        (person,) = mentions
        if _scorecard_measure(q) is not None:
            # A scorecard measure named: their scorecard, never their tickets and findings.
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
    # Idle or spare: the count fewest first, plus members holding no ticket; qualifiers are read first.
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
        # A measure named: its comparison, read before work done or in progress, whose words it may contain.
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
    """Which way a question about workload points: "active", "idle", "busiest" or "spare"; None for none."""
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
    """Whether the words of room only say how little ("the least work"), with no word of room itself."""
    return q.has(_SPARE_HOW_MUCH) and not q.has(_SPARE_ROOM) and not q.has(_NOT_OVERLOADED)


def _figure_named(q: _Question, finished: bool) -> bool:
    """Whether a question names a figure of its own: work finished or in progress, a rate, or the ratio."""
    measure = _measure_named(q)
    return (finished or q.has(r"\bin progress\b|\bwip\b")
            or (measure is not None and (measure != "open_findings_per_10" or q.has(_RATIO))))


def _side_by_side(q: _Question, mentions: list[str], available: set[str], topic: str | None, finished: bool,
                  load: str | None) -> Plan | list[PlanCall]:
    """Two to ten people side by side by the figure the question names, in the order it asks."""
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
        # Where the comparison is off, what a few people work on or finished is each one's tickets.
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
    """Whether a question asks about one of the counts people can be ordered by (backlog, load, done, idle)."""
    return (q.has(_BACKLOG) or q.has(_OVERLOAD) or q.has(_SPARE) or q.has(_IDLE) or q.has(_NO_ROOM)
            or q.has(_BUSIEST) or q.has(r"\bin progress\b|\bwip\b") or _finished(q, _people_topic(q)))


def _count_ranking(q: _Question, available: set[str], measure: str, *, order: str, members: bool = False
                   ) -> Plan | list[PlanCall]:
    """Everyone by a count (oversight_people_compare), most or fewest first; optionally members with no ticket."""
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
    """How a people-level question evaluates people: "personnel", "scorecard", or None."""
    return _judging(_parse(question or "", ""), list(dict.fromkeys(mentions)))


def asks_of_someone(question: str) -> bool:
    """Whether a question asks something of one person in its own words, as the router reads it."""
    q = _parse(question or "", "")
    return (evaluates(q.lower) or _counted(q) or q.has(_WORKING_ON) or q.has(_FINISHED)
            or _people_topic(q) is not None)


# A creative piece asked for. "Story" and "rap" are left out: a Jira issue type, and possibly a board key.
_CREATIVE_PIECE = re.compile(
    r"\b(?:poems?|poetry|poetic|haikus?|limericks?|sonnets?|verses?|rhym(?:e|es|ed|ing)|lyrics|songs?|ballads?"
    r"|jokes?|riddles?)\b"
)
_CREATIVE_ASK = re.compile(
    r"\b(?:write|writes|writing|compose|composing|make|create|give|tell|generate|draft|sing|turn|put|craft)\b"
    r"|\bin\s+(?:verse|rhyme)\b"
)


def creative(question: str) -> bool:
    """Whether a question asks for a creative piece (verse, a song, a joke) rather than about the work."""
    text = (question or "").lower()
    return _CREATIVE_PIECE.search(text) is not None and _CREATIVE_ASK.search(text) is not None


def judging_plan(question: str, catalog_names: Sequence[str], *, project: str, mentions: Sequence[str] = ()
                 ) -> Plan | None:
    """The code-made plan for a question that evaluates people; None for any other question."""
    q = _parse(question or "", project or "")
    people = list(dict.fromkeys(mentions))
    if _judging(q, people) is None:
        return None
    planned = _scorecard_calls(q, people, set(catalog_names))
    return planned if isinstance(planned, Plan) else Plan(understood=True, calls=planned)


def last_resort(question: str, catalog_names: Sequence[str], *, project: str, mentions: Sequence[str] = ()
                ) -> Plan | None:
    """The last-resort plan for a people question nothing else places: the overall comparison or scorecards."""
    q = _parse(question or "", project or "")
    people = list(dict.fromkeys(mentions))
    if not people and not _about_people(q):
        return None
    # The overall comparison, closer to the process first, unless the question is about the other side.
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
        # A ranking by a count stays a raw-count ranking; one naming a compared measure is its comparison.
        return None
    return "personnel" if personnel(q.lower) else "scorecard"


def _measure_named(q: _Question) -> str | None:
    """The scorecard measure a question names for the comparison (_MEASURE_WORDS, first match), if any."""
    return next((key for pattern, key in _MEASURE_WORDS if q.has(pattern)), None)


def _order(q: _Question, measure: str | None = None) -> str | None:
    """The order a question asks for: "most", "fewest", "closer", or None when it asks for none."""
    asked = _direction(q, measure)
    if measure is not None and measure not in RANKED_COUNTS and _further(q):
        return "closer" if asked == "fewest" else _further_first(measure)
    if asked is not None and measure is not None and measure != OVERALL and _turned(q, measure):
        return "fewest" if asked == "most" else "most"
    return asked


def _further_first(measure: str) -> str:
    """The order that puts those further from the process first, for the overall count or a measure."""
    if measure == OVERALL:
        return "fewest"
    return "most" if BY_KEY[measure].direction == "lower_is_closer" else "fewest"


def _further(q: _Question) -> bool:
    """Whether a question asks about the other side of following the process, with no negation before it."""
    return any(re.search(_NEGATION_BEFORE, q.lower[:found.start()]) is None
               for found in re.finditer(_FURTHER, q.lower))


def _direction(q: _Question, measure: str | None = None) -> str | None:
    """Which way a question's direction words point: "most", "fewest" or None."""
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
    """Whether a question names a rate or the ratio the other way round; both at once cancel out."""
    words = dict((key, pattern) for pattern, key in _MEASURE_WORDS).get(measure)
    found = re.search(words, q.lower) if words is not None else None
    if found is None:
        return False
    negated = (re.search(_NEGATION_BEFORE, q.lower[:found.start()]) is not None
               or found.group().startswith("un"))
    over = measure == "within_estimate" and q.has(_OVER_THE_LIMIT)
    return negated != over


def _scorecard_measure(q: _Question) -> str | None:
    """The scorecard-only measure a question about mentioned people names; None for none or findings alone."""
    measure = _measure_named(q)
    return None if measure == "open_findings_per_10" and not q.has(_RATIO) else measure


def _scorecard_calls(q: _Question, mentions: list[str], available: set[str], *, measure: str | None = None,
                     order: str | None = None) -> Plan | list[PlanCall]:
    """The mentioned people's scorecards, or everyone's comparison by a measure, in the order asked."""
    days = _scorecard_days(q)
    if len(mentions) == 1:
        return [_call(SCORECARD_TOOL, project=q.project, person_ids=list(mentions), days=days)]
    if COMPARE_TOOL not in available:
        return Plan(understood=False, reason=COMPARISON_OFF_REASON)
    if mentions:
        named = measure if measure is not None and measure != OVERALL else _scorecard_measure(q)
        if named is not None and measure != OVERALL:
            # A measure named for mentioned people: that measure's comparison, those people only, in the order asked.
            return [_call(COMPARE_TOOL, project=q.project, measure=named, days=days,
                          order=effective_order(named, order or _order(q, named) or "closer"),
                          person_ids=list(mentions))]
        return [_call(SCORECARD_TOOL, project=q.project, person_ids=list(mentions), days=days)]
    measure = measure or _measure_named(q) or "overall"
    asked = effective_order(measure, order or _order(q, measure) or "closer")  # as the facts record it
    return [_call(COMPARE_TOOL, project=q.project, measure=measure, days=days, order=asked, n=_cap(q.n, 50))]


def _scorecard_days(q: _Question) -> int:
    """The scorecard's period from the words: 30, 90 (default) or 180 days."""
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
