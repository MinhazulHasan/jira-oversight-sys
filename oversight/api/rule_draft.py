"""POST /rules/settings/draft (JO-20; contract 4.23 in the jira-oversight-ui repository): a custom rule drafted from an
admin's words, for the admin to check, adjust and add. It never writes settings: the draft fills the dashboard's form,
and only a save (POST /rules/settings) adds the rule. It writes one llm_calls row per model call, and nothing else.

The body is {"text": "..."}: 3 to 500 characters once cleaned as a reason is (control characters as spaces, spaces
collapsed), with no email address or account id in it (it goes to the model); anything else is a 422
rule_settings_invalid on the field `text`. The answer, 200 in every case below:
    {meta, project, outcome, draft, explanation, note, problems}
- outcome "drafted": draft {title, description, check, params, applies_to: {status_category, issue_type}, severity}
  and explanation (what it will flag, one or two plain sentences); problems [] once it validates exactly as a save of
  it would (settings.plan_changes with the board's settings and options as they are now: the registry, the board's
  names, the bounds, the title free on the board, the limit), names written as the board writes them, and its title
  says no number the rule does not hold. Otherwise one repair, when a problem is with something the model fills in
  (its title, description, check, severity, params or applies_to; never the board's limit, which no draft can put
  right) and the day's budget is not used up: the same prompt with the draft and its problems, and the repaired draft
  validated again; if that still fails (or the repair call does not answer, or is not made), outcome "drafted" with its
  problems, each {field, message} on the form field it is about (title, description, check, severity, params.<name>,
  applies_to.status_category, applies_to.issue_type; rule for the rule as a whole), so the admin fixes the form. A
  title or description longer than the form takes is cut to it, at a word.
- outcome "not_supported" (no check can do it: note says why and names the closest check) or "not_understood" (note
  says what to add): draft null.
- outcome "unavailable", draft null, note in plain words, never a 500: with no call, the board already holds as many
  custom rules as it can (LIMIT_REASON: no draft could be added) or the day's model budget is used up (BUDGET_REASON;
  never for the scripted drafter, which calls no model); the model declined (REFUSED_NOTE) or failed (ERROR_NOTE).
The model's words (title, description, explanation, note) are kept as plain text, cut to their lengths, and one that
holds an email address or an account id is left out (a default note said instead). The explanation and the
description must say what the rule does: each number in them one the rule holds, in its unit or one a reader turns it
into (72 hours as 3 days), and, for a check that flags only work past its limit, no "or more" or "at least"; words that
say anything else are replaced by registry.explain's sentence, written from the rule itself ("Flags bugs in 'Code
Review' for more than 3 days."), so the form never shows the AI describing another rule than the one it drafted.

The call (oversight/ask/prompts.py: RULE_DRAFT_SYSTEM, rule_draft_user): provider.structured with RuleDraftReply, a
closed schema (structured outputs close every object, so params is a JSON object in a string, as Ask's plan writes a
call's params: llm.provider.CallParams), on settings.llm_model_qa at effort low, max_tokens DRAFT_MAX_TOKENS: Claude
Opus 5.5 thinks before every answer (adaptive thinking cannot be turned off), so the cap leaves room for it beside a
reply of a few hundred tokens. The prompt holds the check registry for the board, the board's names (statuses with
their categories, issue types, mapped stages, evidence names, estimate basis) and the request quoted as data: no
ticket text, no people, no figure from the board. Each call, the repair too, is recorded with llm.audit
(purpose "rule_draft"), so Governance's usage counts it: a call that failed with the tokens its response reported (a
refusal, a reply cut off), so the day's budget counts what was billed. The app log has one line for each draft
(rule_draft: the board, the viewer's hash, the outcome, how many problems and the llm_calls ids of its model calls;
never the words), so who asked the AI to draft on which board, and when, is always known. In LLM_MODE=stub the
ScriptedProvider drafts from keywords (oversight/llm/scripted.py).
"""
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

# The model writes params as a JSON object inside a string, as Ask's plan does (llm.provider.CallParams): structured
# outputs close every object, so a free-form object could only ever be {}.
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
    """The draft as the route answers it (see the module docstring), and the llm_calls ids of the model calls it made
    (for the log line; never answered)."""

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
    """The admin's words from the body, cleaned; 422 rule_settings_invalid for anything else (the body read as every
    rule settings body is: a JSON object, no key it does not take)."""
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
    """POST /rules/settings/draft's body: the board's settings and options read in one snapshot, whether a draft can be
    made at all (the board's limit, the day's budget: rule_settings.unavailable), then the draft (draft_rule), its model
    calls recorded through a Store of its own; and the draft's log line (see the module docstring)."""
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
    """The draft of `text` on a board (its settings `current` and options `board`): the call, the validation, the one
    repair, the words checked against the rule (see the module docstring). Each model call is recorded in `store`, and
    its llm_calls id kept on the answer."""
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
    """validate_draft, and the drafted title's numbers: a name that says a number the rule does not hold ("over 4
    days" for a limit of 3) is a problem on the title, for the repair or the admin to put right."""
    problems, valid = validate_draft(catalogue, current, definition, board)
    title = definition.get("title")
    if isinstance(title, str):
        wrong = _unheld(title, valid or definition)
        if wrong is not None:
            message = f"The name says {wrong}, but the rule's settings don't."
            problems = [*problems, {"field": "title", "message": message}]
    return problems, valid


def _repairable(problems: list[dict[str, str]]) -> bool:
    """Whether a problem is with something the model fills in (_MODEL_FIELDS, or a part of one): the board's limit, or
    its custom rule ids all given, are no draft's to put right."""
    return any(str(problem["field"]).split(".")[0] in _MODEL_FIELDS for problem in problems)


def _spent(store: Store, provider: Any, settings: Settings, now: Callable[[], datetime]) -> bool:
    """Whether the day's model budget is used up now (never for the scripted drafter, which calls no model): a draft
    that reaches it makes no repair call."""
    return getattr(provider, "name", None) != SCRIPTED and budget_reached(store.conn, settings, now())


def _says_the_rule(text: object, rule: Mapping[str, Any]) -> bool:
    """Whether the model's words say only what the rule does (see the module docstring): each number in them one the
    rule holds (_unheld), and no "or more" for a check that flags only work past its limit. No words say nothing
    wrong."""
    if not isinstance(text, str) or not text:
        return True
    if _unheld(text, rule) is not None:
        return False
    return not (rule.get("check") in _PAST_ONLY and _AT_OR_PAST.search(text))


def _unheld(text: str, rule: Mapping[str, Any]) -> str | None:
    """The first number the words say (in digits, or in words: "two weeks") that the rule does not hold (_figures), as
    written; None when there is none."""
    figures = _figures(rule)
    said = [(match.group(), match.group().rstrip("%").replace(",", "")) for match in NUM.finditer(text)]
    said += [(match.group(), str(spelled_value(match.group()))) for match in SPELLED.finditer(text)
             if match.group().lower() != "one"]  # "one" alone is no count ("in one status")
    for written, value in said:
        if not any(abs(float(value) - figure) < _NEAR for figure in figures):
            return written
    return None


def _figures(rule: Mapping[str, Any]) -> list[float]:
    """Every number words about the rule may say: each number param in its unit and in the units a reader turns it
    into (hours as days and weeks, days as hours and weeks, a share as a percentage), and the numbers in the status
    and issue type names it picks."""
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
    """A drafted rule validated exactly as a save of it would be: its problems, {field, message} each, or none and the
    rule as the board would keep it (names in the board's spelling, numbers as floats)."""
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
    """One structured call, recorded (ok, refused or error) whatever happens, its llm_calls id added to `calls`;
    LLMError (LLMRefusal) when it fails. A failure that came with a response is recorded with the tokens it reported
    (LLMError.usage), as is a reply that does not fit RuleDraftReply: those tokens were billed."""
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
    """The model's words as plain text, cut at a word to `limit` characters; None when there are none, or when they
    hold an email address or an account id. Anything after a piece of JSON in them (a quote that closes an object or
    opens a key, which no sentence holds) is left out: the reply's structure written into its words."""
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
