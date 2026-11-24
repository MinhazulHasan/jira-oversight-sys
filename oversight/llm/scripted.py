"""ScriptedProvider: an offline, deterministic stand-in for the model, so the product runs without an API key."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from oversight.ask.people import no_tickets_sentence, strip_names
from oversight.ask.router import CATALOG_NAMES, route
from oversight.ask.scorecards import code_built
from oversight.llm.provider import LLMError, Plan, StructuredResult, TextResult, Usage

T = TypeVar("T", bound=BaseModel)

MODEL_NAME = "scripted"
WORD_LIMIT = 120
NARRATIVE_TOP = 5

_KEY = re.compile(r"[A-Z][A-Z0-9_]+-\d+")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DIGIT = re.compile(r"\d")

# Period parameters in words: digits that are not fact values would be flagged.
_PERIOD_WORDS = {
    "7d": " in the last seven days",
    "30d": " in the last thirty days",
    "90d": " in the last ninety days",
    "sprint": " in the active sprint",
    "all": " over all time",
}
_METRIC_LABELS = {
    "cycle_time": "cycle time",
    "effort_ratio": "effort-to-estimate ratio",
    "status_age": "time in the current status",
    "reopens": "reopen count",
}
_TREND_LABELS = {
    "median_cycle_time_h": "median cycle time in hours",
    "median_effort_ratio": "median effort-to-estimate ratio",
    "skipped_stage_share": "share of work that skipped a stage",
    "reopen_rate": "reopen rate",
    "open_breaches": "number of open breaches",
    "open_warnings": "number of open warnings",
}
_FIELD_WORDS = {
    "estimate": "an estimate",
    "story_points": "story points",
    "time_logged": "time logged",
    "code_review": "code-review evidence",
    "test_evidence": "test evidence",
}
_EVIDENCE_WORDS = {
    "estimate": "estimate",
    "story_points": "story-point",
    "time_logged": "time-logging",
    "code_review": "code-review",
    "test_evidence": "test",
}
_UNIT_WORDS = {"h": "hours", "d": "days", "x": "times the estimate"}
_SINGULAR = {"hours": "hour", "days": "day", "reopens": "reopen"}
_TONE_FOR = {"breach": "blatantly_bad", "warning": "trending_bad"}
_FRAME = {"blatantly_bad": "Blatantly bad: ", "trending_bad": "On its way to being bad: ", "neutral": ""}


class ScriptedProvider:
    name = "scripted"

    def structured(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        model: str,
        effort: str = "low",
        max_tokens: int = 1024,
    ) -> StructuredResult:
        if isinstance(schema, type) and issubclass(schema, Plan):
            parsed: BaseModel = _plan(user, schema)
        elif "sentences" in getattr(schema, "model_fields", {}):
            parsed = _narrative(user, schema)
        elif _DRAFT_FIELDS <= set(getattr(schema, "model_fields", {})):
            parsed = _rule_draft(user, schema)
        else:
            raise LLMError(f"the scripted provider has no script for {schema.__name__}", retryable=False)
        return StructuredResult(
            parsed=parsed,
            model=MODEL_NAME,
            stop_reason="end_turn",
            usage=_usage(system, user, parsed.model_dump_json()),
            latency_ms=1,
        )

    def compose(
        self,
        system: str,
        user: str,
        *,
        model: str,
        effort: str = "medium",
        max_tokens: int = 1024,
    ) -> TextResult:
        facts = _json_in(user, "facts")
        fact_sets = [fact for fact in facts if isinstance(fact, dict)] if isinstance(facts, list) else []
        if not fact_sets:
            text = "No facts were provided, so there is nothing to answer from."
        else:
            sentences: list[str] = []
            for fact in fact_sets:
                sentences.extend(_render(fact))
            text = _within_budget(sentences)
        return TextResult(
            text=text,
            model=MODEL_NAME,
            stop_reason="end_turn",
            usage=_usage(system, user, text),
            latency_ms=1,
        )


# --- prompt parsing ----------------------------------------------------------------


def _json_in(user: str, tag: str) -> Any:
    """The JSON value after <tag>; raw_decode stops at its end, so a tag inside a summary cannot cut it short."""
    opening = f"<{tag}>"
    start = user.find(opening)
    if start < 0:
        return None
    index = start + len(opening)
    while index < len(user) and user[index].isspace():
        index += 1
    try:
        value, _end = json.JSONDecoder().raw_decode(user, index)
    except ValueError:
        return None
    return value


def _text_in(user: str, tag: str) -> str:
    opening, closing = f"<{tag}>", f"</{tag}>"
    start = user.find(opening)
    if start < 0:
        return ""
    begin = start + len(opening)
    end = user.find(closing, begin)
    body = user[begin:] if end < 0 else user[begin:end]
    # ask/prompts._escape turns "</" inside the question into "<\/".
    return body.replace("<\\/", "</").strip()


def _usage(system: str, user: str, output: str) -> Usage:
    return Usage(input_tokens=(len(system) + len(user)) // 4, output_tokens=len(output) // 4)


# --- structured: the plan and the narrative ------------------------------------------


def _plan(user: str, schema: type[T]) -> T:
    question = _text_in(user, "question")
    catalog = _json_in(user, "catalog")
    names: list[str] | None = None
    if isinstance(catalog, list):
        names = [entry["name"] for entry in catalog if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
    mentions = _mentions_in(user)
    # Route without the mentioned names: "@Bug Hunter" is neither a bug nor a project key.
    question = strip_names(question, [name for _person, name in mentions] + _no_tickets_in(user))
    plan = route(question, names if names is not None else list(CATALOG_NAMES), project="",
                 mentions=[person for person, _name in mentions])
    if isinstance(plan, schema):
        return plan
    return schema.model_validate(plan.model_dump())


def _mentions_in(user: str) -> list[tuple[str, str]]:
    """(person id, name) per <mentions> entry; only a tag before the reader's question counts."""
    question = user.find("<question>")
    head = user if question < 0 else user[:question]
    start = head.rfind("<mentions>")
    mentions = _json_in(head[start:], "mentions") if start >= 0 else None
    if not isinstance(mentions, list):
        return []
    return [
        (entry["id"], entry["name"])
        for entry in mentions
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("name"), str)
        and entry["name"].strip()
    ]


def _no_tickets_in(user: str) -> list[str]:
    """Names in the <no_tickets> tag, read from a tag before the question only."""
    question = user.find("<question>")
    head = user if question < 0 else user[:question]
    start = head.rfind("<no_tickets>")
    names = _json_in(head[start:], "no_tickets") if start >= 0 else None
    if not isinstance(names, list):
        return []
    return [name for name in names if isinstance(name, str) and name.strip()]


def _narrative(user: str, schema: type[T]) -> T:
    candidates = _json_in(user, "candidates")
    ranked = candidates[:NARRATIVE_TOP] if isinstance(candidates, list) else []
    sentences = [_sentence(candidate) for candidate in ranked if isinstance(candidate, dict)]
    try:
        return schema.model_validate({"sentences": sentences})
    except ValidationError as exc:
        raise LLMError(
            f"the scripted narrative did not fit {schema.__name__} ({exc.error_count()} validation errors)",
            retryable=False,
        ) from exc


def _sentence(candidate: dict[str, Any]) -> dict[str, Any]:
    tone = _TONE_FOR.get(str(candidate.get("severity") or ""), "neutral")
    title = str(candidate.get("title") or candidate.get("kind") or "A hot spot").strip().rstrip(".")
    text = _FRAME[tone] + title
    phrases = _detail_phrases(candidate.get("detail"))
    if phrases:
        text += ": " + ", ".join(phrases)
    keys = [f"[{key}]" for key in _issue_keys(candidate.get("issue_keys"))[:3]]
    if keys:
        text += " (see " + _join(keys) + ")"
    candidate_id = candidate.get("id")
    return {
        "text": text + ".",
        "tone": tone,
        "candidate_ids": [] if candidate_id in (None, "") else [str(candidate_id)],
    }


def _detail_phrases(detail: Any) -> list[str]:
    """Up to four "label value" phrases from a candidate's detail entries, numbers verbatim for the validator."""
    if isinstance(detail, dict):
        detail = [{"key": str(key), "label": str(key).replace("_", " "), "value": value} for key, value in detail.items()]
    if not isinstance(detail, list):
        return []
    phrases: list[str] = []
    for entry in detail:
        if not isinstance(entry, dict) or ":" in str(entry.get("key") or ""):
            continue
        phrase = _detail_phrase(_label_words(entry.get("label") or entry.get("key")), entry.get("value"), entry.get("unit"))
        if phrase:
            phrases.append(phrase)
        if len(phrases) == 4:
            break
    return phrases


def _detail_phrase(label: str, value: Any, unit: Any) -> str | None:
    if not label:
        return None
    if isinstance(value, bool):
        return f"{label} {'yes' if value else 'no'}"
    if isinstance(value, (int, float)):
        number = _fmt(value, "share" if unit == "share" else "number")
        if number is None:
            return None
        if unit == "hours" or (unit is None and label.endswith(" h")):
            return f"{label.removesuffix(' h')} {number} hours"
        return f"{label} {number}"
    if isinstance(value, str) and value and len(value) <= 40 and not _DIGIT.search(value):
        return f"{label} {value}"
    return None


def _label_words(label: Any) -> str:
    """A label inside a sentence: "Open breaches" -> "open breaches"; an acronym or a key keeps its case."""
    text = str(label or "").strip()
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


# --- structured: a rule drafted from an admin's words ---------------------------------------------------------

_DRAFT_FIELDS = frozenset({"outcome", "check", "params", "status_categories"})
_NUMBER = r"(\d+(?:\.\d+)?)"
_DURATION = re.compile(_NUMBER + r"\s*(hours?|hrs?|h|days?|d|weeks?|wks?|w)\b", re.IGNORECASE)
_TIMES = re.compile(_NUMBER + r"\s*(?:×|x\b|times\b)", re.IGNORECASE)
_NO = r"(?:no|without(?: an?)?|missing(?: an?)?)"
_MISSING_FIELDS = (
    ("assignee", re.compile(rf"\b{_NO}\s+assignees?\b|\bunassigned\b", re.IGNORECASE)),
    ("epic", re.compile(rf"\b{_NO}\s+epics?\b", re.IGNORECASE)),
    ("estimate", re.compile(rf"\b{_NO}\s+estimates?\b|\bunestimated\b", re.IGNORECASE)),
    ("sprint", re.compile(rf"\bnot in (?:a|any) sprint\b|\b{_NO}\s+sprints?\b", re.IGNORECASE)),
)
_FIELD_PHRASES = {"assignee": "with no assignee", "epic": "with no epic", "estimate": "with no estimate",
                  "sprint": "not in a sprint"}
_NO_UPDATE = re.compile(r"\bno updates?\b|\bnot (?:been )?updated\b|\buntouched\b|\bno changes?\b"
                        r"|\b(?:nobody|no one|not) (?:(?:has|have|been) )*touched\b", re.IGNORECASE)
_OPEN_FOR = re.compile(r"\bopen (?:for )?(?:more than |longer than |over )?\d|\bolder than\b", re.IGNORECASE)
_EFFORT = re.compile(r"\beffort\b|\bover (?:the |its )?estimates?\b", re.IGNORECASE)
_REOPEN = re.compile(r"\breopen", re.IGNORECASE)
_SKIP = re.compile(r"\bskip", re.IGNORECASE)
_WARNING = re.compile(r"\bwarn(?:ing)?s?\b|\bearly\b", re.IGNORECASE)
_LONG_WORD = re.compile(r"[A-Za-z]{3,}")
_IN_A_PLACE = re.compile(r"\bin\s+[A-Za-z]", re.IGNORECASE)  # "in Blocked", "in review": where work sits
_TITLE_MAX = 80
_CATEGORY_WORDS = {"new": "To do", "indeterminate": "In progress", "done": "Done"}
_TIME_IN_STATUS = "time_in_status_over"


def _rule_draft(user: str, schema: type[T]) -> T:
    request = _text_in(user, "request")
    board = _json_in(user, "board")
    checks = _json_in(user, "checks")
    draft = _drafted(request, board if isinstance(board, dict) else {}, checks if isinstance(checks, list) else [])
    # The one repair's <problems> come after the request: the request's own text never reads as them.
    closing = user.find("</request>")
    problems = _json_in(user[closing:], "problems") if closing >= 0 else None
    if isinstance(problems, list) and draft["outcome"] == "drafted":
        refused = {str(problem.get("field")) for problem in problems if isinstance(problem, dict)}
        if "title" in refused:
            draft["title"] = f"{draft['title']} (custom)"[:_TITLE_MAX]
        if any(name.startswith("applies_to.issue_type") for name in refused):
            draft["issue_types"] = None
    return schema.model_validate(draft)


def _drafted(request: str, board: dict[str, Any], checks: list[Any]) -> dict[str, Any]:
    """The script (see the module docstring): the first keyword that matches decides the check."""
    specs = {spec["name"]: spec for spec in checks if isinstance(spec, dict) and isinstance(spec.get("name"), str)}
    statuses = [(entry["name"], entry.get("category")) for entry in board.get("statuses") or []
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
    types = _types_named(request, [name for name in board.get("issue_types") or [] if isinstance(name, str)])
    subject = " and ".join(_plural(name) for name in types) or "Work"
    who = subject if types else "work"
    duration = _DURATION.search(request)
    hours = _hours(duration) if duration is not None else None

    def rule(check: str, params: dict[str, Any], title: str, explanation: str,
             categories: list[str] | None = None) -> dict[str, Any]:
        spec = specs.get(check) or {}
        return {
            "outcome": "drafted", "title": title[:_TITLE_MAX], "description": explanation, "check": check,
            "params": params,
            "status_categories": categories if categories is not None else list(spec.get("default_categories") or []),
            "issue_types": types or None, "severity": "warning" if _WARNING.search(request) else "breach",
            "explanation": explanation, "note": None,
        }

    if _REOPEN.search(request):
        return rule("reopened", {}, f"{subject} reopened after Done",
                    f"Flags {who.lower()} moved out of Done to a status that is not Done.")
    if _SKIP.search(request):
        return rule("skipped_stage", {}, f"{subject} that skipped a stage",
                    f"Flags {who.lower()} whose status jumped over a stage of the workflow.")
    times = _TIMES.search(request)
    if _EFFORT.search(request) and times is not None:
        ratio = float(times.group(1))
        return rule("effort_ratio_at_least", {"threshold": ratio}, f"{subject} at {ratio:g}× the estimate",
                    f"Flags {who.lower()} whose logged time is at least {ratio:g} times its estimate.")
    for name, pattern in _MISSING_FIELDS:
        if pattern.search(request):
            phrase = _FIELD_PHRASES[name]
            return rule("missing_field", {"field": name}, f"{subject} {phrase}", f"Flags {who.lower()} {phrase}.")
    if _NO_UPDATE.search(request):
        days = _days_of(hours, 7.0)
        return rule("no_update_for", {"days": days}, f"{subject} not updated for {days:g} days",
                    f"Flags open {who.lower()} with no change in Jira for more than {days:g} days.")
    if _OPEN_FOR.search(request):
        days = _days_of(hours, 30.0)
        return rule("open_longer_than", {"days": days}, f"{subject} open over {days:g} days",
                    f"Flags {who.lower()} still open more than {days:g} days after it was created.")
    named = _statuses_named(request, statuses)
    if named:
        limit = hours if hours is not None else 72.0
        spec = specs.get(_TIME_IN_STATUS, {})
        allowed = spec.get("categories") or ["new", "indeterminate"]
        held = [category for name, category in statuses if name in named]
        if held and all(category and category not in allowed for category in held):
            # Every status named is one the check never looks at (Done): no rule of it could flag anything there.
            label = spec.get("label") or "Time in a status"
            words = " and ".join(_CATEGORY_WORDS.get(category, category) for category in allowed)
            return _not_drafted("not_supported", f"None of the checks can do that: '{label}' checks {words} work only.")
        picked = set(held)
        categories = [category for category in allowed if category in picked] or None
        names = " or ".join(named)
        where = names if names.casefold().startswith("in ") else f"in {names}"  # "Bugs In Progress", not "in In"
        return rule(_TIME_IN_STATUS, {"statuses": named, "hours": limit},
                    f"{subject} {where} over {_limit_words(limit)}",
                    f"Flags {who.lower()} {where} for more than {_limit_words(limit)}.", categories)
    if not _LONG_WORD.search(request):
        return _not_drafted("not_understood", None)
    closest = _closest(request, list(specs.values()))
    note = "None of the checks can do that."
    if closest:
        note += f" The closest is '{closest}'."
    return _not_drafted("not_supported", note)


def _not_drafted(outcome: str, note: str | None) -> dict[str, Any]:
    return {"outcome": outcome, "title": None, "description": None, "check": None, "params": {},
            "status_categories": [], "issue_types": None, "severity": None, "explanation": None, "note": note}


def _hours(found: re.Match[str]) -> float:
    number, unit = float(found.group(1)), found.group(2).lower()
    return number * (168 if unit.startswith("w") else 24 if unit.startswith("d") else 1)


def _days_of(hours: float | None, default: float) -> float:
    return round(hours / 24, 4) if hours is not None else default


def _limit_words(hours: float) -> str:
    return f"{round(hours / 24, 1):g} days" if hours >= 48 else f"{hours:g} hours"


def _whole(text: str, phrase: str) -> re.Match[str] | None:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE)


def _types_named(request: str, types: list[str]) -> list[str]:
    """The board's issue types the request names, by name or plural ("bugs", "stories"), in the board's order."""
    return [name for name in types if _whole(request, name) or _whole(request, _plural(name))]


def _plural(name: str) -> str:
    if name.lower().endswith("s"):
        return name
    if len(name) > 1 and name.lower().endswith("y") and name[-2].lower() not in "aeiou":
        return name[:-1] + "ies"
    return name + "s"


def _statuses_named(request: str, statuses: list[tuple[str, Any]]) -> list[str]:
    """Statuses the request names as whole words, longest first; else "review" picks the first status holding it."""
    text = request
    found: set[str] = set()
    for name, _category in sorted(statuses, key=lambda item: -len(item[0])):
        match = _whole(text, name)
        if match is not None:
            found.add(name)
            text = text[:match.start()] + " " * (match.end() - match.start()) + text[match.end():]
    if not found and re.search(r"\breview", request, re.IGNORECASE):
        reviewing = next((name for name, _category in statuses if "review" in name.casefold()), None)
        if reviewing is not None:
            found.add(reviewing)
    return [name for name, _category in statuses if name in found]


def _closest(request: str, specs: list[dict[str, Any]]) -> str | None:
    """Label of the closest check for a request no check matched, for the not_supported note."""
    if _DURATION.search(request) and _IN_A_PLACE.search(request):
        label = next((spec.get("label") for spec in specs if spec.get("name") == _TIME_IN_STATUS), None)
        if isinstance(label, str):
            return label
    words = {word.casefold() for word in re.findall(r"[A-Za-z]{4,}", request)}
    best, score = None, -1
    for spec in specs:
        own = {word.casefold() for word in re.findall(r"[A-Za-z]{4,}", f"{spec.get('label')} {spec.get('summary')}")}
        overlap = len(words & own)
        if overlap > score and isinstance(spec.get("label"), str):
            best, score = spec["label"], overlap
    return best


# --- compose: one or two sentences per fact set ---------------------------------------


@dataclass(frozen=True)
class _Fact:
    tool: str
    params: dict[str, Any]
    rows: list[dict[str, Any]]
    kinds: dict[str, str]
    truncated: bool = False
    note: str = ""

    def num(self, row: dict[str, Any], column: str) -> str | None:
        return _fmt(row.get(column), self.kinds.get(column, "number"))

    def ranked(self, column: str) -> list[dict[str, Any]]:
        """Rows with a number in `column`, highest first."""
        numeric = [row for row in self.rows if _is_number(row.get(column))]
        return sorted(numeric, key=lambda row: row[column], reverse=True)

    def keys(self, column: str, limit: int = 3) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            value = row.get(column)
            if isinstance(value, str) and _KEY.fullmatch(value) and value not in seen:
                seen.append(value)
        return [f"[{key}]" for key in seen[:limit]]

    def period(self) -> str:
        return _PERIOD_WORDS.get(str(self.params.get("period") or ""), "")

    def subject(self, row: dict[str, Any], group_by: str, *, capital: bool = False) -> str:
        """How a group row is named ("Bug tickets"); names with digits are described, not quoted, for the validator."""
        tickets = "Tickets" if capital else "tickets"
        group = row.get("group")
        if not isinstance(group, str) or not group:
            return f"{tickets} with no {group_by.replace('_', ' ')}"
        if group_by == "epic" and self.kinds.get("group") == "issue_key" and _KEY.fullmatch(group):
            return f"{tickets} under epic [{group}]"
        if _DIGIT.search(group):
            return {"epic": f"{tickets} under one epic", "sprint": f"{tickets} in one sprint"}.get(
                group_by, f"{tickets} in one {group_by.replace('_', ' ')}"
            )
        if group_by == "issue_type":
            return f"{group} tickets"
        if group_by == "epic":
            return f"{tickets} under epic {group}"
        if group_by == "sprint":
            return f"{tickets} in sprint {group}"
        return f"{tickets} in {group}"


Renderer = Callable[[_Fact], list[str]]


def _render(raw: dict[str, Any]) -> list[str]:
    tool = str(raw.get("tool") or "")
    rows = [row for row in (raw.get("rows") or []) if isinstance(row, dict)]
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    note = raw.get("note") if isinstance(raw.get("note"), str) else ""
    fact = _Fact(tool=tool, params=params, rows=rows, kinds=_kinds(raw, rows), truncated=bool(raw.get("truncated")),
                 note=note)
    if not rows and tool == "oversight_board_people":
        return [_EVERYONE_HOLDS]  # no member without a ticket: said, beside the count it goes with
    if not rows:
        return [f"Nothing came back for {_describe(tool)}{fact.period()}."]
    sentences = _RENDERERS.get(tool, _render_generic)(fact)
    if raw.get("truncated"):
        shown = _shown(tool, rows)
        if shown is not None:
            sentences.append(f"The table shows the first {shown}.")
    return sentences


def _shown(tool: str, rows: list[dict[str, Any]]) -> int | None:
    """How many rows a cut table shows, as its sentence counts them; None for no sentence."""
    if tool == "oversight_people_compare":
        return None
    if tool == "oversight_people":
        return sum(1 for row in rows if row.get("person_id"))
    return len(rows)


def _kinds(raw: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, str]:
    kinds: dict[str, str] = {}
    for column in raw.get("columns") or []:
        if isinstance(column, dict) and isinstance(column.get("key"), str):
            kinds[column["key"]] = str(column.get("kind") or "number")
    for row in rows[:1]:
        for key in row:
            kinds.setdefault(str(key), _guess_kind(str(key)))
    return kinds


def _guess_kind(key: str) -> str:
    if key.endswith("_h") or key == "hours":
        return "hours"
    if "share" in key:
        return "share"
    if "ratio" in key:
        return "ratio"
    if key.endswith("_key") or key == "sample_keys":
        return "issue_key"
    return "number"


def _render_count(fact: _Fact) -> list[str]:
    row = fact.rows[0]
    count = fact.num(row, "count")
    scope = ""
    if fact.params.get("issue_type"):
        scope += f" of type {fact.params['issue_type']}"
    if fact.params.get("status"):
        scope += f" in status {fact.params['status']}"
    if fact.params.get("has_estimate") is True:
        scope += " with an estimate"
    elif fact.params.get("has_estimate") is False:
        scope += " without an estimate"
    # By status category, over the board's work: open tickets, as the Overview and the Portfolio count them.
    state = fact.params.get("state")
    one, many = {"open": ("open ticket", "open tickets"), "in_progress": ("ticket in progress", "tickets in progress"),
                 "done": ("done ticket", "done tickets")}.get(str(state), ("issue", "issues"))
    # A count of now (by status category) over every ticket has no period to say.
    when = "" if state is not None and fact.params.get("period") in (None, "all") else fact.period()
    if count is None:
        sentence = f"The count of {many}{scope}{when} is not available."
    elif count == "1":
        sentence = f"There is 1 {one}{scope}{when}."
    else:
        sentence = f"There are {count} {many}{scope}{when}."
    examples = [f"[{key}]" for key in _issue_keys(row.get("sample_keys"))[:3]]
    if examples:
        sentence += " Examples: " + _join(examples) + "."
    return [sentence]


def _render_top(fact: _Fact) -> list[str]:
    metric = str(fact.params.get("metric") or "value")
    label = _METRIC_LABELS.get(metric, metric.replace("_", " "))
    leaders: list[str] = []
    for row in fact.rows[:3]:
        key, value = row.get("issue_key"), fact.num(row, "value")
        if not (isinstance(key, str) and _KEY.fullmatch(key)) or value is None:
            continue
        unit = row.get("unit")
        unit_text = ""
        if isinstance(unit, str) and unit and not _DIGIT.search(unit):
            words = _UNIT_WORDS.get(unit, unit)
            unit_text = " " + (_SINGULAR.get(words, words) if value == "1" else words)
        leaders.append(f"[{key}] at {value}{unit_text}")
    if not leaders:
        return [f"The {label} ranking returned {len(fact.rows)} rows without readable values."]
    return [f"The highest {label} is {_join(leaders)}."]


def _render_time_in_status(fact: _Fact) -> list[str]:
    ranked = fact.ranked("median_h")
    if not ranked:
        return [f"Time in status returned {len(fact.rows)} rows without medians."]
    group_by = str(fact.params.get("group_by") or "status")
    top = ranked[0]
    sentence = f"{fact.subject(top, group_by, capital=True)} wait longest: a median of {fact.num(top, 'median_h')} hours"
    p90 = fact.num(top, "p90_h")
    if p90 is not None:
        sentence += f", p90 {p90} hours"
    n = fact.num(top, "n")
    if n is not None:
        sentence += f", over {n} closed intervals"
    sentences = [sentence + f"{fact.period()}."]
    if len(ranked) > 1:
        second = ranked[1]
        sentences.append(f"Next come {fact.subject(second, group_by)} with a median of {fact.num(second, 'median_h')} hours.")
    return sentences


def _render_effort(fact: _Fact) -> list[str]:
    ranked = fact.ranked("median_ratio")
    if not ranked:
        return [f"Effort against estimate returned {len(fact.rows)} rows without ratios."]
    group_by = str(fact.params.get("group_by") or "epic")
    top = ranked[0]
    sentence = (
        f"{fact.subject(top, group_by, capital=True)} run furthest over their estimates"
        f"{fact.period()}, with a median effort-to-estimate ratio of {fact.num(top, 'median_ratio')}"
    )
    share, threshold, n = fact.num(top, "share_over_threshold"), fact.num(top, "threshold"), fact.num(top, "n")
    if share is not None and threshold is not None:
        finished = f"its {n} finished issues" if n is not None else "its finished issues"
        sentence += f" and {share} of {finished} above the {threshold}x threshold"
    return [sentence + "."]


def _render_findings(fact: _Fact) -> list[str]:
    state = str(fact.params.get("state") or "open")
    count = len(fact.rows)
    one = count == 1 and not fact.truncated
    state_words = {
        "open": "is open" if one else "are open",
        "resolved": "was resolved" if one else "were resolved",
    }.get(state, "was recorded" if one else "were recorded")
    noun = "finding" if one else "findings"
    # A capped list is a sample: its length is a lower bound, never the total.
    amount = f"At least {count}" if fact.truncated else str(count)
    rule_ids = [row.get("rule_id") for row in fact.rows if isinstance(row.get("rule_id"), str)]
    sentence = f"{amount} {noun} {state_words}{fact.period()}"
    if rule_ids:
        # The most frequent rule, decided in code.
        top_rule = max(dict.fromkeys(rule_ids), key=rule_ids.count)
        title = next((row.get("rule_title") for row in fact.rows if row.get("rule_id") == top_rule), None)
        name = _rule_name(title, top_rule)
        if all(rule_id == top_rule for rule_id in rule_ids):
            sentence = f"{amount} {noun} for {name} {state_words}{fact.period()}"
        elif not fact.truncated:
            # Only a whole list says which rule is most frequent; a sample's ordering would decide it.
            sentence += f", most often for {name}"
    keys = fact.keys("issue_key")
    if keys:
        sentence += ", including " + _join(keys)
    return [sentence + "."]


def _render_timeline(fact: _Fact) -> list[str]:
    ordered = sorted(fact.rows, key=lambda row: row.get("seq") if _is_number(row.get("seq")) else 0)
    count = len(ordered)
    sentence = f"The issue has passed through {count} status {'interval' if count == 1 else 'intervals'}"
    current = ordered[-1].get("status")
    if isinstance(current, str) and current:
        sentence += f" and is now in {current}"
    longest = fact.ranked("hours")
    if longest:
        hours, status = fact.num(longest[0], "hours"), longest[0].get("status")
        if hours is not None and isinstance(status, str) and status:
            sentence += f"; the longest completed stay was {hours} hours in {status}"
    return [sentence + "."]


def _render_trend(fact: _Fact) -> list[str]:
    metric = str(fact.params.get("metric") or "value")
    label = _TREND_LABELS.get(metric, metric.replace("_", " "))
    ordered = sorted(fact.rows, key=lambda row: str(row.get("week_of") or ""))
    valued = [row for row in ordered if _is_number(row.get("value"))]
    if not valued:
        return [f"There is no weekly data for the {label} yet."]
    last, first = valued[-1], valued[0]
    sentence = f"The {label} was {fact.num(last, 'value')} in {_week(last.get('week_of'), 'the latest week')}"
    if len(valued) > 1:
        sentence += f", against {fact.num(first, 'value')} in {_week(first.get('week_of'), 'the earliest week')}"
    return [sentence + "."]


def _render_coverage(fact: _Fact) -> list[str]:
    row = fact.rows[0]
    field = str(fact.params.get("field") or "estimate")
    what = _FIELD_WORDS.get(field, field.replace("_", " "))
    measurable = row.get("measurable")
    if measurable is not None and not measurable:
        evidence = _EVIDENCE_WORDS.get(field, field.replace("_", " "))
        return [f"Jira holds no {evidence} evidence for this project, so coverage cannot be measured."]
    covered, total, share = fact.num(row, "covered"), fact.num(row, "total"), fact.num(row, "share")
    if covered is None or total is None:
        return [f"Coverage of {what} is not available{fact.period()}."]
    sentence = f"{covered} of {total} issues have {what}{fact.period()}"
    if share is not None:
        sentence += f", a share of {share}"
    return [sentence + "."]


def _render_hot_spots(fact: _Fact) -> list[str]:
    ranked = fact.ranked("open_breaches") or fact.rows
    top = ranked[0]
    subject = _epic(top.get("epic_key"), capital=True)
    parts: list[str] = []
    breaches = fact.num(top, "open_breaches")
    if breaches is not None:
        parts.append(f"{breaches} open {'breach' if breaches == '1' else 'breaches'}")
    warnings = fact.num(top, "open_warnings")
    if warnings is not None:
        parts.append(f"{warnings} open {'warning' if warnings == '1' else 'warnings'}")
    sentence = f"{subject} is the hottest spot" + (f" with {_join(parts)}" if parts else "")
    issues = fact.num(top, "issues")
    if issues is not None:
        sentence += f" across {issues} issues"
    sentences = [sentence + "."]
    if len(ranked) > 1:
        second = ranked[1]
        more = fact.num(second, "open_breaches")
        if more is not None:
            sentences.append(
                f"Next is {_epic(second.get('epic_key'))} with {more} open {'breach' if more == '1' else 'breaches'}."
            )
    return sentences


def _render_rule_status(fact: _Fact) -> list[str]:
    blocked = [row for row in fact.rows if row.get("measurable") is not None and not row.get("measurable")]
    if blocked:
        names = [_rule_name(row.get("title"), row["rule_id"]) for row in blocked[:3] if isinstance(row.get("rule_id"), str)]
        more = " and others" if len(blocked) > 3 else ""
        sentences = [f"These rules cannot be measured in Jira today: {_join(names)}{more}."]
    else:
        sentences = [f"All {len(fact.rows)} rules can be measured in Jira today."]
    measurable = _Fact(fact.tool, fact.params, [row for row in fact.rows if row not in blocked], fact.kinds)
    busiest = measurable.ranked("open_findings")
    if busiest and busiest[0]["open_findings"] and isinstance(busiest[0].get("rule_id"), str):
        row = busiest[0]
        sentences.append(
            f"The rule with the most open findings is {_rule_name(row.get('title'), row['rule_id'])}"
            f" at {fact.num(row, 'open_findings')}."
        )
    return sentences


# The people ranking's figure -> (one, several), as a sentence names it.
_FIGURE_WORDS = {
    "open_breaches": ("open breach", "open breaches"),
    "open_warnings": ("open warning", "open warnings"),
    "open_findings": ("open finding", "open findings"),
    "open_tickets": ("open ticket", "open tickets"),
    "tickets": ("ticket", "tickets"),
    "tickets_with_findings": ("ticket with findings", "tickets with findings"),
}
_FOLLOWS = "Findings follow the ticket's current assignee."


def _figure(column: str, value: str) -> str:
    """A people figure's words for one value: "1 open breach", "3 open breaches"."""
    one, several = _FIGURE_WORDS.get(column, (column.replace("_", " "),) * 2)
    return one if value == "1" else several


def _render_people(fact: _Fact) -> list[str]:
    """The people ranking by one figure; unassigned tickets are said separately, never ranked."""
    rank_by = str(fact.params.get("rank_by") or "open_breaches")
    if fact.params.get("person_ids") and rank_by == "open_breaches":
        return _render_side_by_side(fact)
    one, several = _FIGURE_WORDS.get(rank_by, (rank_by.replace("_", " "),) * 2)
    ranked = [row for row in fact.rows if row.get("person_id") and _is_number(row.get(rank_by))]
    nobody = next((row for row in fact.rows if not row.get("person_id") and _is_number(row.get(rank_by))), None)
    unassigned = []
    if nobody is not None and nobody[rank_by]:
        amount = fact.num(nobody, rank_by) or ""
        unassigned = [f"Unassigned: {amount} {_figure(rank_by, amount)}."]
    follows = [_FOLLOWS] if rank_by in ("open_breaches", "open_warnings", "open_findings", "tickets_with_findings") else []
    if not ranked:
        if unassigned:
            return ["No one on this board holds a ticket now.", *unassigned]
        return [f"The people ranking returned {len(fact.rows)} rows without readable figures."]
    if fact.params.get("order") == "fewest":
        return [_render_fewest(fact, ranked, rank_by, several), *unassigned, *follows]
    top = ranked[0][rank_by]
    if not top:
        return [f"No one on this board has {'an' if one[0] in 'aeiou' else 'a'} {one}.", *unassigned, *follows]
    leaders = [row for row in ranked if row[rank_by] == top][:3]
    value = fact.num(leaders[0], rank_by)
    if len(leaders) == 1:
        sentence = f"{_holder(leaders[0])} has the most {several}: {value}"
    else:
        sentence = f"{_join([_holder(row) for row in leaders])} have the most {several}, {value} each"
    after = next((row for row in ranked if row[rank_by] != top and row[rank_by]), None)
    if after is not None:
        sentence += f", then {_holder(after, capital=False)} with {fact.num(after, rank_by)}"
    return [sentence + ".", *unassigned, *follows]


def _render_fewest(fact: _Fact, ranked: list[dict[str, Any]], rank_by: str, several: str) -> str:
    """The people ranking, fewest first; everyone tied at the fewest is mentioned."""
    low = ranked[0][rank_by]
    tied = [row for row in ranked if row[rank_by] == low]
    value = fact.num(tied[0], rank_by)
    if len(tied) == 1:
        sentence = f"{_holder(tied[0])} has the fewest {several}: {value}"
    else:
        names = [_holder(row) for row in tied[:3]] + (["others"] if len(tied) > 3 else [])
        sentence = f"{_join(names)} have the fewest {several}, {value} each"
    after = next((row for row in ranked if row[rank_by] != low), None)
    if after is not None:
        sentence += f", then {_holder(after, capital=False)} with {fact.num(after, rank_by)}"
    return sentence + "."


# More names than this and the answer says how many, and the facts list them.
_NAMES_IN_A_SENTENCE = 20
# The board's members who hold no ticket, when there are none (the dashboard's sample says the same).
_EVERYONE_HOLDS = "Everyone on this board holds at least one of its tickets."


def _render_board_people(fact: _Fact) -> list[str]:
    """Everyone on the board by name and who holds no ticket, or only those holding none."""
    people = [row for row in fact.rows if row.get("person_id") and isinstance(row.get("name"), str)
              and row["name"].strip()]
    names = [row["name"].strip() for row in people]
    none = [row["name"].strip() for row in people if row.get("tickets") == 0]
    if str(fact.params.get("holding") or "any") == "none":
        if not names:
            return [_EVERYONE_HOLDS]
        if len(names) > _NAMES_IN_A_SENTENCE:
            amount, _count = _amount(fact)
            return [f"{amount} members of this board have no tickets on it; the facts list them by name."]
        return [no_tickets_sentence(names)]
    amount, count = _amount(fact)
    if count > _NAMES_IN_A_SENTENCE or len(names) > _NAMES_IN_A_SENTENCE:
        return [f"{amount} people are on this board: those assigned its tickets first, then its members who hold "
                f"none; the facts list them by name."]
    who = f"{amount} {'person is' if count == 1 else 'people are'} on this board"
    if not none:
        return [f"{who}: {_join(names)}."]
    # Each name once: the people who hold its tickets, then how many members hold none, and who.
    holders = [row["name"].strip() for row in people if row.get("tickets") != 0]
    members = (f"{len(none)} {'member who holds' if len(none) == 1 else 'members who hold'} none of its tickets: "
               f"{_join(none)}.")
    if not holders:
        return [f"{who}, {'a member who holds' if count == 1 else 'all members who hold'} none of its tickets: "
                f"{_join(none)}."]
    return [f"{who}: {_join(holders)}, and {members}"]


def _render_side_by_side(fact: _Fact) -> list[str]:
    """Each person asked about, in the facts' order, with their open breaches and warnings."""
    parts: list[str] = []
    for row in fact.rows:
        breaches, warnings = fact.num(row, "open_breaches"), fact.num(row, "open_warnings")
        if not row.get("person_id") or breaches is None or warnings is None:
            continue
        if parts:
            parts.append(f"{_holder(row)} has {breaches} and {warnings}")
        else:
            parts.append(f"{_holder(row)} has {breaches} {_figure('open_breaches', breaches)} and {warnings} "
                         f"{_figure('open_warnings', warnings)}")
    if not parts:
        return [f"The people asked about returned {len(fact.rows)} rows without readable figures."]
    return ["; ".join(parts) + ".", _FOLLOWS]


def _holder(row: dict[str, Any], *, capital: bool = True) -> str:
    """A ranking row's person by name, or the unassigned tickets (said in words: "Unassigned" is no one's name)."""
    name = row.get("name")
    if row.get("person_id") and isinstance(name, str) and name.strip():
        return name.strip()
    return "Unassigned tickets" if capital else "unassigned tickets"


def _person_name(fact: _Fact) -> str:
    """The person a person tool's facts are about, by the name its rows hold."""
    name = fact.rows[0].get("assignee") if fact.rows else None
    return name.strip() if isinstance(name, str) and name.strip() else "This person"


_IN_ALL = re.compile(r"\b(\d+) in all\b")


def _amount(fact: _Fact) -> tuple[str, int]:
    """The count as the answer writes it: the note's "N in all" total, else the rows (a lower bound if capped)."""
    found = _IN_ALL.search(fact.note)
    if found:
        return found.group(1), int(found.group(1))
    count = len(fact.rows)
    return (f"at least {count}" if fact.truncated else str(count)), count


def _render_person_tickets(fact: _Fact) -> list[str]:
    amount, count = _amount(fact)
    state = str(fact.params.get("state") or "open")
    noun = {"open": "open ticket", "in_progress": "ticket"}.get(state, "ticket") + ("" if count == 1 else "s")
    if state == "in_progress":
        noun += " in progress"
    elif state == "done":
        noun += f" done in the last {fact.params.get('days') or 90} days"
    sentence = f"{_person_name(fact)} has {amount} {noun}"
    keys = fact.keys("issue_key")
    if keys:
        sentence += ", including " + _join(keys)
    return [sentence + "."]


def _render_ticket_assignee(fact: _Fact) -> list[str]:
    """Who holds one ticket now, by name, or that nobody does."""
    row = fact.rows[0]
    key = str(row.get("issue_key") or "The ticket")
    status = f" ({row.get('status')})" if isinstance(row.get("status"), str) and row.get("status") else ""
    if row.get("assignee_id") and isinstance(row.get("assignee"), str):
        return [f"{key}{status} is assigned to {row['assignee'].strip()} now."]
    return [f"Nobody is assigned {key}{status} now."]


def _render_person_findings(fact: _Fact) -> list[str]:
    amount, count = _amount(fact)
    one = count == 1
    severity = str(fact.params.get("severity") or "")
    singular, plural = {"breach": ("breach", "breaches"), "warning": ("warning", "warnings")}.get(
        severity, ("finding", "findings"))
    state = {"open": "open ", "resolved": "resolved "}.get(str(fact.params.get("state") or "open"), "")
    sentence = f"{_person_name(fact)} has {amount} {state}{singular if one else plural}"
    rule_ids = [row.get("rule_id") for row in fact.rows if isinstance(row.get("rule_id"), str)]
    if rule_ids and not fact.truncated:
        # The most frequent rule, decided in code; only a whole list can say it.
        top_rule = max(dict.fromkeys(rule_ids), key=rule_ids.count)
        rule = _rule_name(next((row.get("rule_title") for row in fact.rows if row.get("rule_id") == top_rule), None), top_rule)
        if len(set(rule_ids)) > 1:
            sentence += f", most often for {rule}"
        else:
            sentence += f", for {rule}" if one else f", all for {rule}"
    keys = fact.keys("issue_key")
    if keys:
        sentence += ", including " + _join(keys)
    return [sentence + "."]


def _render_scorecard(fact: _Fact) -> list[str]:
    """A scorecard or comparison, built by ask/scorecards.py so the scripted model and the code agree."""
    text = code_built([{"tool": fact.tool, "params": fact.params, "rows": fact.rows}]) or ""
    return [paragraph for paragraph in text.split("\n\n") if paragraph]


def _render_generic(fact: _Fact) -> list[str]:
    count = len(fact.rows)
    sentence = f"{_describe(fact.tool).capitalize()} returned {count} {'row' if count == 1 else 'rows'}"
    values: list[str] = []
    for key, value in fact.rows[0].items():
        formatted = _fmt(value, fact.kinds.get(str(key), "number"))
        if formatted is not None:
            values.append(f"{str(key).replace('_', ' ')} {formatted}")
        if len(values) == 3:
            break
    if values:
        sentence += f"; the first row has {_join(values)}"
    return [sentence + "."]


_RENDERERS: dict[str, Renderer] = {
    "oversight_count_issues": _render_count,
    "oversight_top_issues": _render_top,
    "oversight_time_in_status": _render_time_in_status,
    "oversight_effort_vs_estimate": _render_effort,
    "oversight_list_findings": _render_findings,
    "oversight_issue_timeline": _render_timeline,
    "oversight_trend": _render_trend,
    "oversight_coverage": _render_coverage,
    "oversight_hot_spots": _render_hot_spots,
    "oversight_rule_status": _render_rule_status,
    "oversight_people": _render_people,
    "oversight_board_people": _render_board_people,
    "oversight_person_tickets": _render_person_tickets,
    "oversight_person_findings": _render_person_findings,
    "oversight_ticket_assignee": _render_ticket_assignee,
    "oversight_person_scorecard": _render_scorecard,
    "oversight_people_compare": _render_scorecard,
}


# --- formatting helpers --------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _plain_number(value: int | float) -> str | None:
    """An integer verbatim; a float verbatim up to two decimals, else one decimal (both pass the validator)."""
    if isinstance(value, bool) or not _is_number(value):
        return None
    if isinstance(value, int):
        return str(value)
    text = repr(value)
    if "e" in text or "E" in text:
        return f"{value:.1f}"
    _whole, _dot, decimals = text.partition(".")
    return text if len(decimals) <= 2 else f"{value:.1f}"


def _fmt(value: Any, kind: str = "number") -> str | None:
    """A number the way the validator will accept it; None for anything that is not a number."""
    if not _is_number(value):
        return None
    if kind == "share" and isinstance(value, float) and 0 <= value <= 1:
        pct = value * 100
        # An exact half ("12.5%") stays at one decimal, where no rounding rule can disagree.
        return f"{pct:.1f}%" if pct % 1 == 0.5 else f"{pct:.0f}%"
    return _plain_number(value)


def _issue_keys(value: Any) -> list[str]:
    """Keys from a list or a "AIL-1, AIL-2 ..." string, in order, without repeats."""
    if isinstance(value, str):
        candidates = re.split(r"[,\s]+", value)
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        return []
    keys: list[str] = []
    for candidate in candidates:
        if _KEY.fullmatch(candidate) and candidate not in keys:
            keys.append(candidate)
    return keys


def _epic(key: Any, *, capital: bool = False) -> str:
    word = "Epic" if capital else "epic"
    if isinstance(key, str) and _KEY.fullmatch(key):
        return f"{word} [{key}]"
    # The catalogue groups issues with no epic under "(no epic)": that is not an epic.
    return "Work outside any epic" if capital else "work outside any epic"


def _rule_name(title: Any, rule_id: str) -> str:
    """The rule's title, quoted; never its id, which the validator would read as an issue key."""
    if isinstance(title, str) and title.strip() and not _DIGIT.search(title):
        return f"'{title.strip()}'"
    return "one rule"


def _week(value: Any, fallback: str) -> str:
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        return f"the week of {value}"
    return fallback


def _describe(tool: str) -> str:
    name = tool.removeprefix("oversight_").replace("_", " ").strip()
    return name or "this query"


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _within_budget(sentences: list[str]) -> str:
    """Drop trailing sentences until the answer is under the word limit."""
    kept = [sentence for sentence in sentences if sentence]
    while len(kept) > 1 and len(" ".join(kept).split()) >= WORD_LIMIT:
        kept.pop()
    text = " ".join(kept)
    if len(text.split()) >= WORD_LIMIT:
        # Last resort: a whole-word cut keeps every number and key intact.
        text = " ".join(text.split()[: WORD_LIMIT - 1])
    return text
