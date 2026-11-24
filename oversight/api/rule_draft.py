"""POST /rules/settings/draft: a custom rule drafted by the model from an admin's words; it never saves."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, WithJsonSchema

from oversight.api.access_log import Viewer, names_someone, plain_text
from oversight.api.deps import ApiContext
from oversight.api.meta import build_meta
from oversight.api.rule_settings import _object, _unknown, invalid, snapshot, unavailable
from oversight.ask.prompts import RULE_DRAFT_SYSTEM, rule_draft_repair, rule_draft_user
from oversight.ask.validate import NUM, SPELLED, spelled_value
from oversight.llm import audit as llm_audit
from oversight.llm.budget import SCRIPTED, budget_reached
from oversight.llm.provider import LLMError, LLMRefusal, _params_from_json
from oversight.logs import get_logger
from oversight.rules.catalog import RulesConfig
from oversight.rules.registry import (
    CHECK_SPECS,
    DESCRIPTION_MAX,
    TITLE_MAX,
    BoardOptions,
    checks_payload,
    explain,
    number,
)
from oversight.rules.settings import (
    BoardSettings,
    Change,
    CustomContext,
    Problem,
    SettingsInvalid,
    custom_context,
    load_board_settings,
    plan_changes,
)
from oversight.settings import Settings
from oversight.store.db import Store

log = get_logger("api")

PURPOSE = "rule_draft"
DRAFT_MAX_TOKENS = 4096
TEXT_MIN, TEXT_MAX = 3, 500  # characters, once cleaned
NOTE_MAX = 300  # characters: a note or an explanation, as the dashboard shows one
REFUSED_NOTE = "The AI didn't draft this rule. You can still add it by hand."
ERROR_NOTE = "The AI couldn't draft a rule just now. Try again in a moment, or add it by hand."
NOT_UNDERSTOOD_NOTE = 'Describe the rule in a few words, such as "Bugs in Code Review for more than 3 days".'
NOT_SUPPORTED_NOTE = "None of the checks can do that."
_TEXT_WORDS = f"Describe the rule in {TEXT_MIN} to {TEXT_MAX} characters."
# JSON inside words: a quote closing an object or a list, one opening a key, or a key's colon ("}, "params": ...).
_JSON_IN_WORDS = re.compile(r'"\s*[}\]]|[{\[]\s*"|"\s*:\s*["{\[]')
Outcome = Literal["drafted", "not_supported", "not_understood", "unavailable"]
# The fields of a draft the model fills in: a problem on one of them (or a part of one) is the model's to put right.
_MODEL_FIELDS = frozenset({"title", "description", "check", "severity", "params", "applies_to"})
# Checks that flag only work past their limit ("more than 3 days"): words for "at or past it" say another rule.
_PAST_ONLY = frozenset({"time_in_status_over", "open_longer_than", "no_update_for", "estimate_after_start",
                        "stuck_in_status", "no_evidence_after_percentile"})
_AT_OR_PAST = re.compile(r"\b(?:or more|or longer|or over|or above|at least|no less than)\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d+(?:\.\d+)?")
_NEAR = 0.05  # how near a number in words must be to the rule's ("3.3 days" for 80 hours)

# Structured outputs close every object, so params travels as a JSON string, as in Ask's plan.
DraftParams = Annotated[
    dict[str, Any],
    BeforeValidator(_params_from_json),
    WithJsonSchema({
        "type": "string",
        "description": (
            "The rule's params as a JSON object, for example "
            '"{\\"statuses\\": [\\"Code Review\\"], \\"hours\\": 72}". '
            "Every param the check lists, and nothing else; \"{}\" for a check with none."
        ),
    }),
]


class RuleDraftReply(BaseModel):
    """What the draft call returns: one rule built from the listed checks, or why none can be.

    Every field is required (null, or empty, where it does not apply), so the model writes each one in its place. With
    the fields optional, a live draft once skipped params and then wrote them into the end of its note, as text
    (23 Nov 2026)."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["drafted", "not_supported", "not_understood"]
    title: str | None
    description: str | None
    check: str | None
    params: DraftParams
    status_categories: list[Literal["new", "indeterminate", "done"]]
    issue_types: list[str] | None
    severity: Literal["breach", "warning"] | None
    explanation: str | None
    note: str | None


@dataclass(frozen=True)
class DraftAnswer:
    """The draft as the route answers it, plus the llm_calls ids of its model calls (for the log only)."""

    outcome: Outcome
    draft: dict[str, Any] | None = None
    explanation: str | None = None
    note: str | None = None
    problems: list[dict[str, str]] = field(default_factory=list)
    calls: tuple[str, ...] = field(default=(), compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "draft": self.draft, "explanation": self.explanation, "note": self.note,
                "problems": list(self.problems)}


def parse_draft(raw: bytes) -> str:
    """The admin's words from the body, cleaned; 422 rule_settings_invalid for anything else."""
    body = _object(raw)
    problems = _unknown(body, frozenset({"text"}))
    value = body.get("text")
    text = plain_text(value) if isinstance(value, str) else ""
    if not TEXT_MIN <= len(text) <= TEXT_MAX:
        problems.append(Problem(None, "text", _TEXT_WORDS))
    elif names_someone(text):
        problems.append(Problem(None, "text", "The description cannot hold an email address or an account id."))
    if problems:
        raise invalid(problems)
    return text


def draft_body(conn: sqlite3.Connection, ctx: ApiContext, text: str, viewer: Viewer) -> dict[str, Any]:
    """POST /rules/settings/draft's body: check availability, draft the rule, record its calls, log one line."""
    with snapshot(conn):
        current = load_board_settings(conn, ctx.project)
        board = custom_context(conn, ctx.process, ctx.catalogue, ctx.project, at="", by=viewer.name)
        reason = unavailable(conn, ctx, len(current.live_custom))
        meta = build_meta(conn, ctx)
    if reason is not None:
        answer = DraftAnswer("unavailable", note=reason)
    else:
        with Store.open(ctx.db_path) as store:
            answer = draft_rule(text, catalogue=ctx.catalogue, current=current, board=board, provider=ctx.provider,
                                settings=ctx.settings, store=store, now=ctx.now)
    log.info("rule_draft", project=ctx.project, viewer=viewer.hash, outcome=answer.outcome,
             problems=len(answer.problems), calls=list(answer.calls))
    return {"meta": meta, "project": ctx.project, **answer.as_dict()}


def draft_rule(
    text: str,
    *,
    catalogue: RulesConfig,
    current: BoardSettings,
    board: CustomContext,
    provider: Any,
    settings: Settings,
    store: Store,
    now: Callable[[], datetime],
) -> DraftAnswer:
    """Draft `text` on a board: the call, validation, one repair and the wording check; calls recorded in `store`."""
    calls: list[str] = []
    user = rule_draft_user(text, checks_payload(board.options), board_payload(board.options))
    try:
        reply = _call(store, provider, settings, user, now, calls)
    except LLMRefusal:
        return DraftAnswer("unavailable", note=REFUSED_NOTE, calls=tuple(calls))
    except LLMError:
        return DraftAnswer("unavailable", note=ERROR_NOTE, calls=tuple(calls))
    if reply.outcome != "drafted":
        default = NOT_SUPPORTED_NOTE if reply.outcome == "not_supported" else NOT_UNDERSTOOD_NOTE
        return DraftAnswer(reply.outcome, note=_words(reply.note, NOTE_MAX) or default, calls=tuple(calls))
    definition = _definition(reply)
    problems, valid = _checked(catalogue, current, definition, board)
    if problems and _repairable(problems) and not _spent(store, provider, settings, now):
        try:
            again = _call(store, provider, settings, user + rule_draft_repair(definition, problems), now, calls)
        except LLMError:
            again = None
        if again is not None and again.outcome == "drafted":
            reply, definition = again, _definition(again)
            problems, valid = _checked(catalogue, current, definition, board)
    rule = valid or definition
    said = explain(str(rule.get("check")), _mapping(rule.get("params")), _mapping(rule.get("applies_to")))
    explanation = _words(reply.explanation, NOTE_MAX)
    if not _says_the_rule(explanation, rule):
        explanation = said
    if not _says_the_rule(rule.get("description"), rule):
        rule = {**rule, "description": said}
    return DraftAnswer("drafted", draft=rule, explanation=explanation, problems=problems, calls=tuple(calls))


def _checked(
    catalogue: RulesConfig, current: BoardSettings, definition: Mapping[str, Any], board: CustomContext
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """validate_draft, plus a title problem when the title says a number the rule does not hold."""
    problems, valid = validate_draft(catalogue, current, definition, board)
    title = definition.get("title")
    if isinstance(title, str):
        wrong = _unheld(title, valid or definition)
        if wrong is not None:
            message = f"The name says {wrong}, but the rule's settings don't."
            problems = [*problems, {"field": "title", "message": message}]
    return problems, valid


def _repairable(problems: list[dict[str, str]]) -> bool:
    """Whether a problem is with something the model fills in, so a repair could fix it."""
    return any(str(problem["field"]).split(".")[0] in _MODEL_FIELDS for problem in problems)


def _spent(store: Store, provider: Any, settings: Settings, now: Callable[[], datetime]) -> bool:
    """Whether the day's model budget is used up now; never for the scripted drafter."""
    return getattr(provider, "name", None) != SCRIPTED and budget_reached(store.conn, settings, now())


def _says_the_rule(text: object, rule: Mapping[str, Any]) -> bool:
    """Whether the model's words say only what the rule does; empty words say nothing wrong."""
    if not isinstance(text, str) or not text:
        return True
    if _unheld(text, rule) is not None:
        return False
    return not (rule.get("check") in _PAST_ONLY and _AT_OR_PAST.search(text))


def _unheld(text: str, rule: Mapping[str, Any]) -> str | None:
    """The first number the words say that the rule does not hold, as written; None when there is none."""
    figures = _figures(rule)
    said = [(match.group(), match.group().rstrip("%").replace(",", "")) for match in NUM.finditer(text)]
    said += [(match.group(), str(spelled_value(match.group()))) for match in SPELLED.finditer(text)
             if match.group().lower() != "one"]  # "one" alone is no count ("in one status")
    for written, value in said:
        if not any(abs(float(value) - figure) < _NEAR for figure in figures):
            return written
    return None


def _figures(rule: Mapping[str, Any]) -> list[float]:
    """Every number words about the rule may say, in its unit and the units a reader converts it to."""
    check = rule.get("check")
    spec = CHECK_SPECS.get(check) if isinstance(check, str) else None
    params, applies = _mapping(rule.get("params")), _mapping(rule.get("applies_to"))
    figures: list[float] = []
    for param in spec.params if spec is not None else ():
        value = number(params.get(param.name))
        if param.spec is None or value is None:
            continue
        unit = param.spec.unit
        if unit == "hours":
            figures += [value, value / 24, value / 168]
        elif unit == "days":
            figures += [value, value * 24, value / 7]
        elif unit == "share":
            figures += [value, value * 100]
        else:
            figures.append(value)
    for names in (params.get("statuses"), applies.get("issue_type")):
        for name in names if isinstance(names, list) else []:
            if isinstance(name, str):
                figures += [float(found) for found in _DIGITS.findall(name)]
    return figures


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def validate_draft(
    catalogue: RulesConfig, current: BoardSettings, definition: Mapping[str, Any], board: CustomContext
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """A drafted rule validated exactly as a save would be: its problems, or none and the rule as kept."""
    try:
        plan = plan_changes(catalogue, current, [Change.adding(definition)], board)
    except SettingsInvalid as exc:
        return [{"field": problem.field or "rule", "message": problem.message} for problem in exc.problems], None
    known = {rule.rule_id for rule in current.custom}
    made = next(rule for rule in plan.settings.custom if rule.rule_id not in known)
    return [], made.definition()


def board_payload(options: BoardOptions) -> dict[str, Any]:
    """The board's names as the prompt holds them (<board>): nothing from its tickets, no people, no figures."""
    return {
        "statuses": [{"name": name, "category": category} for name, category in options.statuses],
        "issue_types": list(options.issue_types),
        "stages": list(options.stages),
        "evidence": list(options.evidence),
        "estimate_basis": options.estimate_basis,
    }


def _call(
    store: Store, provider: Any, settings: Settings, user: str, now: Callable[[], datetime], calls: list[str]
) -> RuleDraftReply:
    """One structured call, always recorded with any billed tokens; raises LLMError (or LLMRefusal) on failure."""
    name = str(getattr(provider, "name", "unknown"))
    prompt_hash = llm_audit.prompt_hash(RULE_DRAFT_SYSTEM, user)
    model = settings.llm_model_qa
    try:
        result = provider.structured(RULE_DRAFT_SYSTEM, user, RuleDraftReply, model=model, effort="low",
                                     max_tokens=DRAFT_MAX_TOKENS)
    except LLMError as exc:
        calls.append(_failed(store, name, model, prompt_hash, exc, now))
        raise
    try:
        parsed = result.parsed
        reply = parsed if isinstance(parsed, RuleDraftReply) else RuleDraftReply.model_validate(parsed.model_dump())
    except ValidationError:
        error = LLMError("the draft did not match RuleDraftReply", usage=result.usage, stop_reason=result.stop_reason)
        calls.append(_failed(store, name, result.model, prompt_hash, error, now, latency_ms=result.latency_ms))
        raise error from None
    calls.append(llm_audit.record_llm_call(
        store, purpose=PURPOSE, provider=name, model=result.model, prompt_hash=prompt_hash, usage=result.usage,
        latency_ms=result.latency_ms, stop_reason=result.stop_reason, outcome="ok", now=now))
    return reply


def _failed(
    store: Store, provider: str, model: str, prompt_hash: str, error: LLMError, now: Callable[[], datetime], *,
    latency_ms: int | None = None,
) -> str:
    """A failed call's llm_calls row (refused or error, with what its response reported) and its log line; its id."""
    message = f"{type(error).__name__}: {error}"[:200]  # names the failure, never the prompt
    log.warning("rule_draft_call_failed", error=message, retryable=error.retryable)
    return llm_audit.record_llm_call(
        store, purpose=PURPOSE, provider=provider, model=model, prompt_hash=prompt_hash, usage=error.usage,
        latency_ms=latency_ms, stop_reason=error.stop_reason,
        outcome="refused" if isinstance(error, LLMRefusal) else "error", error=message, now=now)


def _definition(reply: RuleDraftReply) -> dict[str, Any]:
    """The reply as a new rule's definition, its words kept as plain text and cut to the form's lengths."""
    return {
        "title": _words(reply.title, TITLE_MAX),
        "description": _words(reply.description, DESCRIPTION_MAX),
        "check": reply.check,
        "params": json.loads(json.dumps(reply.params)),  # a copy, as JSON holds it
        "applies_to": {"status_category": list(reply.status_categories), "issue_type": reply.issue_types or None},
        "severity": reply.severity,
    }


def _words(text: str | None, limit: int) -> str | None:
    """The model's words as plain text, cut at a word to `limit`; None if empty or holding personal identifiers."""
    if not isinstance(text, str):
        return None
    words = plain_text(text)
    leaked = _JSON_IN_WORDS.search(words)
    if leaked is not None:
        words = words[:leaked.start()].rstrip(" ,;:")
    if not words or names_someone(words):
        return None
    if len(words) > limit:
        cut = words[:limit + 1].rsplit(" ", 1)[0] if " " in words[:limit + 1] else words[:limit]
        words = cut[:limit].rstrip(" ,;:")
    return words
