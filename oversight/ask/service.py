"""Ask: plan, fetch, compose and validate an answer from catalogue facts, with citations."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from oversight.api.people import Person, board_everyone
from oversight.ask.analyst import (
    PEOPLE_VERIFY_RETRY,
    VERIFY_RETRY,
    QueryError,
    analyst_system,
    analyst_tools,
    snapshot_tools,
    tidy_markdown,
    tool_result_text,
)
from oversight.ask.catalog import CatalogContext, Facts, QuerySpec, catalog_schema, validate_params, board_of
from oversight.ask.people import (
    LEVELS,
    MAX_MENTIONS,
    Level,
    NameCheck,
    PeopleScope,
    check_names,
    first_named_in,
    loosely_named_in,
    mention_problem,
    mentions_payload,
    named_in,
    names_someone,
    no_tickets_sentence,
    people_catalog_schema,
    specs_for,
    strip_names,
    unknown_name,
)
from oversight.ask.prompts import (
    COMPARISON_OFF,
    COMPOSE_SYSTEM,
    CREATIVE_DECLINE,
    DECISION_YOURS,
    JUDGEMENT_DECLINE,
    LAST_RESORT_OVERALL,
    LAST_RESORT_SCORECARD,
    PEOPLE_COMPOSE_SYSTEM,
    PEOPLE_PLAN_SYSTEM,
    PICK_WITH_AT,
    PLAN_RETRY_SUFFIX,
    PLAN_SYSTEM,
    TEAM_DECLINE,
    TOO_MANY_PEOPLE,
    ambiguous_first_name,
    ambiguous_name,
    caveat,
    compose_user,
    mentions_tag,
    no_one_called,
    no_tickets_tag,
    plan_user,
)
from oversight.ask.prompts import _escape as escape
from oversight.ask.router import asks_of_someone, creative, judging, judging_plan, last_resort
from oversight.ask.scorecards import (
    SCORECARD_TOOL,
    ComparisonOff,
    built_answer,
    character_labels,
    labels_problem,
    scorecard_days,
    without_labels,
)
from oversight.ask.validate import Binding, Check, validate
from oversight.config import FieldsConfig, ProcessConfig
from oversight.llm import audit as llm_audit
from oversight.llm.budget import BUDGET_NOTE, SCRIPTED, budget_reached
from oversight.llm.provider import (
    ConverseResult,
    LLMError,
    LLMProvider,
    LLMRefusal,
    Plan,
    PlanCall,
    StructuredResult,
    TextResult,
)
from oversight.llm.scripted import ScriptedProvider
from oversight.logs import get_logger
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import to_iso, utcnow

Outcome = Literal["answered", "not_understood", "no_data", "not_measurable", "not_verified"]

SUGGESTED_QUESTIONS: list[str] = [
    "Where do tickets get stuck most?",
    "Which epics have the most open breaches?",
    "How much work was estimated before it started?",
    "Which tickets went straight from To Do to Done this month?",
    "How is effort against estimate trending?",
    "Which rules can't be measured in Jira today?",
    "What is the median cycle time this quarter?",
    "Which tickets have been reopened after Done?",
    "How much of the active sprint has an estimate?",
    "Which open tickets have burned the most of their estimate?",
]
# At people level, the questions about people come first (the dashboard's sample suggests the same).
PEOPLE_SUGGESTED_QUESTIONS: list[str] = [
    "Who follows the process most closely?",
    "Who has the most open breaches?",
    "Who finished the most work this month?",
    *SUGGESTED_QUESTIONS[:7],
]

# Longer questions are almost never questions; the limit also bounds the plan prompt.
MAX_QUESTION_CHARS = 500
NOT_VERIFIED_MESSAGE = "The written answer did not match the facts, so only the facts are shown."
UNKNOWN_MENTION_MESSAGE = "Someone you mentioned has no tickets on this board."
_NOT_UNDERSTOOD_DEFAULT = "Jira data cannot answer that. Try a suggestion."
# What the facts table shows for an empty cell.
_EMPTY_CELL = "-"

log = get_logger("ask")


@dataclass
class AskResponse:
    qa_id: str
    question: str
    outcome: Outcome
    answer: str | None
    citations: list[Binding]
    facts: list[Facts]
    plan: list[PlanCall]
    missing_evidence: list[str]
    message: str | None
    suggestions: list[str]
    latency_ms: int
    model: str | None
    asked_at: str
    project: str
    extra: dict[str, Any] = field(default_factory=dict)
    level: str = "team"
    mentions: list[str] = field(default_factory=list)  # the person ids the question mentioned
    # The people the verified answer names ({id, name, avatar}), in order of first appearance; [] at team level.
    people: list[Person] = field(default_factory=list)
    # How the answer was made, shown under it (budget.BUDGET_NOTE when built without the model).
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The shape the API returns (docs/API_CONTRACT.md in jira-oversight-ui: POST ask)."""
        return {
            "qa_id": self.qa_id,
            "question": self.question,
            "outcome": self.outcome,
            "answer": self.answer,
            "citations": [asdict(binding) for binding in self.citations],
            "facts": [facts.to_dict() for facts in self.facts],
            "plan": [call.model_dump(mode="json") for call in self.plan],
            "missing_evidence": list(self.missing_evidence),
            "message": self.message,
            "suggestions": list(self.suggestions),
            "latency_ms": self.latency_ms,
            "model": self.model,
            "asked_at": self.asked_at,
            "project": self.project,
            "extra": dict(self.extra),
            "level": self.level,
            "mentions": list(self.mentions),
            "people": [dict(who) for who in self.people],
            "note": self.note,
        }

    def render(self) -> str:
        """Console rendering: the answer (or the message), then each fact set as a plain table."""
        lines = [self.answer or self.message or self.outcome]
        if self.note:
            lines.append(self.note)
        if self.missing_evidence:
            lines.append(f"Missing evidence: {', '.join(self.missing_evidence)}")
        if self.outcome == "not_understood" and self.suggestions:
            lines.append("Try one of these:")
            lines.extend(f"  - {question}" for question in self.suggestions[:5])
        for facts in self.facts:
            lines.append("")
            lines.extend(_render_facts(facts))
        return "\n".join(lines)


def ask(
    question: str,
    *,
    store: Store,
    process: ProcessConfig,
    rules: RulesConfig,
    settings: Settings,
    provider: LLMProvider,
    user: str = "cli",
    project: str | None = None,
    now: Callable[[], datetime] = utcnow,
    level: Level = "team",
    mentions: Sequence[str] = (),
    people: PeopleScope | None = None,
) -> AskResponse:
    """people: the board's people (the API's routes/people.py: people_scope), needed at people level."""
    if level not in LEVELS:
        raise ValueError(f"not a level: {level!r}")
    if mentions and level != "people":
        raise ValueError("only a people-level question can mention people")
    problem = mention_problem(list(mentions))
    if problem is not None:
        raise ValueError(problem)
    if level == "people" and people is None:
        raise ValueError("a people-level question needs the board's people")
    session = _Session(
        qa_id=uuid.uuid4().hex,
        question=question.strip(),
        project=project or settings.jira_projects[0],
        user=user,
        asked_at=to_iso(now()) or "",
        started=time.perf_counter(),
        store=store,
        settings=settings,
        provider=provider,
        now=now,
        level=level,
        scope=people if level == "people" else None,
        mention_ids=list(mentions),
        rules=rules,
    )
    suggestions = session.suggestions
    if not session.question:
        return session.finish("not_understood", message="Ask a question about the Jira data.", suggestions=suggestions)
    if len(session.question) > MAX_QUESTION_CHARS:
        return session.finish(
            "not_understood",
            message=f"The question is too long (over {MAX_QUESTION_CHARS} characters); ask one thing at a time.",
            suggestions=suggestions,
        )
    # A creative piece (verse, a song, a joke) is no question about the work: declined in code, before any model.
    if creative(session.question):
        return session.finish("not_understood", message=CREATIVE_DECLINE, suggestions=suggestions)
    # Day's model budget used up: the answer is built in code from the same facts, and says so.
    if session.provider_name != SCRIPTED and budget_reached(store.conn, settings, now()):
        session.provider = ScriptedProvider()
        session.note = BUDGET_NOTE
        provider = session.provider
        log.info("ask_budget_reached", qa_id=session.qa_id, budget=settings.llm_daily_token_budget)
    # Everyone mentioned must be on this board; members holding no ticket are answered in code.
    if session.scope is not None:
        scope = session.scope
        for mention in session.mention_ids:
            holder, member = scope.person(mention), scope.member(mention)
            if holder is not None:
                session.mentioned.append(holder)
            elif member is not None:
                session.without_tickets.append(member)
            else:
                return session.finish("not_understood", message=UNKNOWN_MENTION_MESSAGE, suggestions=suggestions)
        # A full name typed in the question is a mention too, as if picked with @.
        problem = session.resolve_typed_names()
        if problem is not None:
            return session.finish("not_understood", message=problem, suggestions=suggestions)
        if session.without_tickets and not session.mentioned:
            sentence = no_tickets_sentence([who["name"] for who in session.without_tickets])
            # The answer says it all; message stays null, so names are only in the answer (contract 4.6).
            return session.finish("no_data", answer=sentence, people=session.without_tickets)

    # With a live model the analyst answers; a turned-off comparison is refused first, in code.
    if _analyst_available(settings, provider, store):
        if session.refuses_comparison:
            return session.finish("not_understood", message=COMPARISON_OFF, suggestions=suggestions)
        return _ask_analyst(session, process=process, rules=rules)

    # A question naming someone in its own words is about them; the board's figures would read as theirs.
    typed = not session.mentioned and names_someone(session.question, session.board())
    if typed and not session.at_people_level:
        return session.finish("not_understood", message=TEAM_DECLINE, suggestions=suggestions)
    # A question that evaluates people is planned and answered in code: never declined, never left to a model.
    judged = session.judged_plan() if session.at_people_level and not typed else None
    if judged is not None:
        return _answer_in_code(session, judged, process=process, rules=rules)

    # 1. Plan: the question and the catalogue (with the person tools at people level), never ticket data.
    plan_system = PEOPLE_PLAN_SYSTEM if session.at_people_level else PLAN_SYSTEM
    plan_prompt = plan_user(session.question, session.catalog, mentions=session.mentions_payload,
                            no_tickets=session.no_tickets_payload)
    try:
        planned = session.structured("plan", plan_system, plan_prompt, Plan, effort="low")
    except LLMError:
        return session.finish(
            "not_understood",
            message="The question could not be planned right now; try again in a moment.",
            suggestions=suggestions,
        )
    plan = planned.parsed
    assert isinstance(plan, Plan)
    session.plan = plan
    # Named by part of a name: only a ranking of the board's people answers it; otherwise point to @.
    if typed and not _ranks_the_board(plan):
        return session.finish("not_understood", message=PICK_WITH_AT, suggestions=suggestions)
    if (not plan.understood or not plan.calls) and session.at_people_level and not typed:
        # A lowercase first name where a person's name stands: that person, if only one goes by it.
        loose = session.resolve_loose_name()
        if isinstance(loose, str):
            return session.finish("not_understood", message=loose, suggestions=suggestions)
        if loose:
            if not session.mentioned:
                sentence = no_tickets_sentence([who["name"] for who in session.without_tickets])
                return session.finish("no_data", answer=sentence, people=session.without_tickets)
            judged = session.judged_plan()
            if judged is not None:
                return _answer_in_code(session, judged, process=process, rules=rules)
            plan_prompt = plan_user(session.question, session.catalog, mentions=session.mentions_payload,
                                    no_tickets=session.no_tickets_payload)
            try:
                planned = session.structured("plan_named", plan_system, plan_prompt, Plan, effort="low")
            except LLMError:
                return session.finish(
                    "not_understood",
                    message="The question could not be planned right now; try again in a moment.",
                    suggestions=suggestions,
                )
            plan = planned.parsed
            assert isinstance(plan, Plan)
            session.plan = plan
        else:
            # Never turn an admin away: an unknown name is said so; other people questions get the overall comparison.
            unknown = session.unknown_name()
            if unknown is not None:
                return session.finish("not_understood", message=no_one_called(unknown), suggestions=suggestions)
            fallback = last_resort(session.routed_question, [tool["name"] for tool in session.catalog], project="",
                                   mentions=[who["id"] for who in session.mentioned])
            if fallback is not None:
                session.lead = LAST_RESORT_SCORECARD if session.mentioned else LAST_RESORT_OVERALL
                log.info("ask_last_resort", qa_id=session.qa_id, mentions=len(session.mentioned))
                return _answer_in_code(session, fallback, process=process, rules=rules)
    if not plan.understood or not plan.calls:
        message = session.unchecked(plan.reason, _NOT_UNDERSTOOD_DEFAULT)
        return session.finish("not_understood", message=message, suggestions=suggestions)

    # 2. Validate every call before any query runs; an invalid plan gets one retry, told what was wrong.
    specs = specs_for(session.level, comparison=session.comparison)
    session.calls = list(plan.calls[: settings.ask_max_calls])
    checked, problem = _validated(session.calls, specs)
    if problem is not None:
        retry_user = plan_prompt + PLAN_RETRY_SUFFIX.format(problem=problem)
        try:
            replanned = session.structured("plan_retry", plan_system, retry_user, Plan, effort="low")
        except LLMError:
            replanned = None
        retry = replanned.parsed if replanned is not None else None
        if isinstance(retry, Plan) and retry.understood and retry.calls:
            session.plan = retry
            session.calls = list(retry.calls[: settings.ask_max_calls])
            checked, problem = _validated(session.calls, specs)
        if problem is not None:
            return session.finish("not_understood", message=f"The plan could not be used: {problem}.", suggestions=suggestions)
    # A turned-off comparison is refused per ask, not per call: two scorecards are a side-by-side comparison.
    if session.at_people_level and not session.comparison and len(_scored(
            params for call, params in checked if call.name == SCORECARD_TOOL)) > 1:
        return session.finish("not_understood", message=COMPARISON_OFF, suggestions=suggestions)
    prepared: list[tuple[QuerySpec, BaseModel, str]] = []
    for call, params in checked:
        call_project = getattr(params, "project", None) or session.project
        # Every answer reads the board it was asked on; another board here would leak data the person may not see.
        named = board_of(params)
        if named is not None and named != session.project:
            return session.finish(
                "not_understood",
                message=f"This question is about {named}, but only {session.project} is in scope here. Pick another board to ask about it.",
                suggestions=suggestions,
            )
        prepared.append((specs[call.name], params, call_project))

    # 3. Evidence: a stage Jira does not record cannot be measured, which is itself a finding.
    missing: list[str] = []
    where = session.project
    for spec, params, call_project in prepared:
        for name in spec.needs_evidence(params, rules):
            if not process.has_evidence(call_project, name) and name not in missing:
                missing.append(name)
                where = call_project
    if missing:
        named = ", ".join(missing)
        return session.finish(
            "not_measurable",
            missing=missing,
            message=(
                f"Jira holds no evidence for {named} in {where}, so this can't be measured. "
                "That gap is itself a finding."
            ),
        )

    # 4. Fetch: parameterised SQL on a read-only connection.
    ctx = CatalogContext(process=process, rules=rules, project=session.project, now=now(), row_limit=settings.ask_row_limit,
                         people=session.scope)
    try:
        with _readonly(store) as conn:
            for index, (spec, params, _call_project) in enumerate(prepared, start=1):
                facts = spec.run(conn, params, ctx)
                facts.ref = f"F{index}"
                session.facts.append(facts)
    except ComparisonOff:
        # A plan that sets people side by side where an organisation has turned comparing them off.
        return session.finish("not_understood", message=COMPARISON_OFF, suggestions=suggestions)
    if all(facts.empty for facts in session.facts):
        looked_at = "; ".join(_describe(spec, params) for spec, params, _ in prepared)
        return session.finish("no_data", message=f"Nothing matched in {session.project}. Looked at: {looked_at}.")

    # 5 and 6. Compose, then verify; one strict retry; otherwise the facts alone.
    payload = [facts.to_dict() for facts in session.facts]
    compose_system = PEOPLE_COMPOSE_SYSTEM if session.at_people_level else COMPOSE_SYSTEM
    problems: list[str] = []
    for purpose in ("compose", "compose_strict"):
        prompt = compose_user(session.question, payload, strict_problems=problems or None,
                              mentions=session.mentions_payload, people=session.at_people_level,
                              no_tickets=session.no_tickets_payload)
        try:
            composed = session.compose(purpose, compose_system, prompt)
        except LLMRefusal as exc:
            problems = [str(exc)]  # a failed composition: ask once more, strictly
            continue
        except LLMError:
            answered = session.answer_from_code(why="the model was unavailable")
            if answered is not None:
                return answered
            return session.finish(
                "not_verified",
                message="The model was unavailable, so only the facts are shown.",
                extra={"problems": problems},
            )
        if composed.stop_reason == "max_tokens":
            problems = ["the answer was cut off before it finished"]
            continue
        # Plain prose before the check, so every citation is a substring of what is shown.
        text = plain_prose(composed.text)
        check, named = session.verify(text, plan)
        if check.ok:
            return session.finish_answer(text, check, named, extra={"retried": purpose != "compose"})
        problems = check.problems
    # A scorecard or comparison the model could not write acceptably: the code-built answer instead.
    answered = session.answer_from_code(why="the written answer failed its checks")
    if answered is not None:
        return answered
    return session.finish("not_verified", message=NOT_VERIFIED_MESSAGE, extra={"problems": problems})


# --- one ask, start to finish ----------------------------------------------------------------


@dataclass
class _Session:
    """Everything one ask accumulates, so any step can finish with a complete record."""

    qa_id: str
    question: str
    project: str
    user: str
    asked_at: str
    started: float
    store: Store
    settings: Settings
    provider: LLMProvider
    now: Callable[[], datetime]
    level: str = "team"
    scope: PeopleScope | None = None  # the board's people, at people level only
    mention_ids: list[str] = field(default_factory=list)
    # The mentioned people found on the board: those who hold one of its tickets now, and the members who hold none.
    mentioned: list[Person] = field(default_factory=list)
    without_tickets: list[Person] = field(default_factory=list)
    plan: Plan | None = None
    calls: list[PlanCall] = field(default_factory=list)
    facts: list[Facts] = field(default_factory=list)
    scored: set[str] = field(default_factory=set)  # the people whose scorecards this ask has read
    model: str | None = None
    compose_hash: str | None = None
    note: str | None = None  # AskResponse.note: set when the day's model budget is used up
    # A sentence said before the answer: which figures the last resort answers with (prompts.LAST_RESORT_OVERALL).
    lead: str | None = None
    # The board's rules the facts are read with: their titles and descriptions are words, never figures (validate.py).
    rules: RulesConfig | None = None

    @property
    def provider_name(self) -> str:
        return str(getattr(self.provider, "name", None) or type(self.provider).__name__)

    @property
    def at_people_level(self) -> bool:
        return self.scope is not None

    @property
    def suggestions(self) -> list[str]:
        return PEOPLE_SUGGESTED_QUESTIONS if self.at_people_level else SUGGESTED_QUESTIONS

    @property
    def catalog(self) -> list[dict[str, Any]]:
        """The tools the plan may use, person tools included at people level."""
        if not self.at_people_level:
            return catalog_schema()
        return [*catalog_schema(), *people_catalog_schema(comparison=self.comparison)]

    @property
    def comparison(self) -> bool:
        """Whether people may be compared (PEOPLE_COMPARISON, as the API passed it on and as this service reads it)."""
        return self.scope is not None and self.scope.comparison and self.settings.people_comparison == "on"

    @property
    def routed_question(self) -> str:
        """The question without the names of the people it mentions, as the router reads it: a name is no topic."""
        return strip_names(self.question, [who["name"] for who in [*self.mentioned, *self.without_tickets]])

    @property
    def judged(self) -> str | None:
        """How the question evaluates people (router.judging): "personnel", "scorecard" or None; None at team level."""
        if self.scope is None:
            return None
        return judging(self.routed_question, [who["id"] for who in self.mentioned])

    def judged_plan(self) -> Plan | None:
        """The code-planned scorecard or comparison for a question that evaluates people; None otherwise."""
        if self.scope is None:
            return None
        names = [tool["name"] for tool in self.catalog]
        return judging_plan(self.routed_question, names, project="", mentions=[who["id"] for who in self.mentioned])

    @property
    def refuses_comparison(self) -> bool:
        """A question that evaluates people side by side (two or more, or everyone) where comparing them is off."""
        return self.judged is not None and not self.comparison and len(self.mentioned) != 1

    def resolve_typed_names(self) -> str | None:
        """Turn full names typed in the question into mentions; a message when a name is shared, else None."""
        assert self.scope is not None
        picked = {who["id"] for who in [*self.mentioned, *self.without_tickets]}
        board = self.board()
        named = [(None, group) for group in named_in(self.question, board)]
        for first, group in [*named, *first_named_in(self.question, board)]:
            if any(who["id"] in picked for who in group):
                continue  # someone picked with @: the chip is written into the question as @Name
            if len(group) > 1:
                names = [who["name"] for who in group]
                return ambiguous_name(names) if first is None else ambiguous_first_name(first, names)
            (who,) = group
            held = self.scope.person(who["id"]) is not None
            (self.mentioned if held else self.without_tickets).append(who)
            self.mention_ids.append(who["id"])
            picked.add(who["id"])
        if len(self.mention_ids) > MAX_MENTIONS:
            return TOO_MANY_PEOPLE
        return None

    def resolve_loose_name(self) -> str | bool:
        """Resolve a lowercase first name to a mention when exactly one person has it; a message when several do."""
        assert self.scope is not None
        if self.mention_ids or not asks_of_someone(self.question):
            return False
        found = loosely_named_in(self.question, self.board())
        if len(found) != 1:
            return False
        ((first, group),) = found
        if len(group) > 1:
            return ambiguous_first_name(first, [who["name"] for who in group])
        (who,) = group
        held = self.scope.person(who["id"]) is not None
        (self.mentioned if held else self.without_tickets).append(who)
        self.mention_ids.append(who["id"])
        return True

    def unknown_name(self) -> str | None:
        """A typed name no one on the board goes by, in a question that mentions no one; None otherwise."""
        if self.scope is None or self.mention_ids:
            return None
        return unknown_name(self.question, self.board(), asks=asks_of_someone(self.question))

    @property
    def mentions_payload(self) -> list[dict[str, str]] | None:
        """The mentioned people as the model is told them (name and person id); None when there are none."""
        return mentions_payload(self.mentioned) or None

    @property
    def no_tickets_payload(self) -> list[str] | None:
        """Names of mentioned people holding no ticket, for the model to leave out; None when there are none."""
        return [who["name"] for who in self.without_tickets] or None

    def finish_answer(self, text: str, check: Check, named: list[Person], *, extra: dict[str, Any]) -> AskResponse:
        """A checked answer plus the sentences code adds: no-tickets, the personnel decision and the caveat."""
        text, named = self.with_no_tickets(text, named)
        if self.lead:
            text = f"{self.lead}\n\n{text}"  # no figure and no name: nothing in it to verify
        return self.finish("answered", answer=self.with_scorecard_endings(text), citations=check.bindings,
                           people=named, extra=extra)

    def with_scorecard_endings(self, answer: str) -> str:
        if self.scope is None:
            return answer
        if self.judged == "personnel":
            answer += "\n\n" + DECISION_YOURS
        days = scorecard_days(self.facts)
        board = self.scope.scorecard
        if days and board is not None:
            answer += "\n\n" + caveat(days, records_code_review=board.records_code_review)
        return answer

    def answer_from_code(self, *, why: str) -> AskResponse | None:
        """The code-built scorecard or comparison answer from this ask's facts, checked; None without such facts."""
        built = built_answer(facts.to_dict() for facts in self.facts)
        if built is None:
            return None
        text, hints = built
        # Each figure cited to the row it was read from: two people's equal figures are each their own.
        check, named = self.verify(text, Plan.model_construct(understood=True, calls=list(self.calls), reason=None),
                                   hints=hints)
        if not check.ok:  # never expected: every figure and name comes from the facts themselves
            log.warning("ask_code_built_unverified", qa_id=self.qa_id, problems=len(check.problems))
            return None
        log.info("ask_code_built", qa_id=self.qa_id, why=why, calls=[call.name for call in self.calls])
        return self.finish_answer(text, check, named, extra={"answer": "code", "why": why})

    def with_no_tickets(self, answer: str, named: list[Person]) -> tuple[str, list[Person]]:
        """A verified answer with the no-tickets paragraph appended, and the people it names in order."""
        if not self.without_tickets:
            return answer, named
        sentence = no_tickets_sentence([who["name"] for who in self.without_tickets])
        return f"{answer}\n\n{sentence}", [*named, *(who for who in self.without_tickets if who not in named)]

    def board(self) -> list[Person]:
        """Everyone on the board: ticket holders and members holding none."""
        if self.scope is not None:
            return [*self.scope.people, *self.scope.members]
        with _readonly(self.store) as conn:
            return board_everyone(conn, self.project)

    def unchecked(self, text: str | None, fallback: str) -> str:
        """A model message with no facts behind it; the fallback if it names an unmentioned person or a label."""
        text = _declined(text)
        if not text:
            return fallback
        if self.scope is not None and (not self.names(text, []).ok or JUDGEMENT_DECLINE in text
                                       or character_labels(text, self.board_names)):
            return fallback  # a name no fact verifies, the old judgement decline, or a label
        return text

    @property
    def board_names(self) -> list[str]:
        """Display names of everyone on the board, taken out before the wording check (people level only)."""
        return [who["name"] for who in self.board()] if self.scope is not None else []

    def names(self, text: str, facts: Sequence[Facts]) -> NameCheck:
        """The people-level name check; members holding no ticket are looked for too, mentions pass."""
        assert self.scope is not None
        mentions = [*self.mentioned, *self.without_tickets]
        members = [*self.without_tickets, *(who for who in self.scope.members if who not in self.without_tickets)]
        return check_names(text, facts, self.scope, mentions, also=members)

    def verify(self, answer: str, plan: Plan | None, *, hints: Mapping[int, tuple[str, int, str]] | None = None
               ) -> tuple[Check, list[Person]]:
        """The number check plus, at people level, the name and wording checks; problems, citations and names."""
        if self.scope is None:
            return validate(answer, self.facts, plan, rules=self.rules), []
        names = self.names(answer, self.facts)
        # Names and measure labels are blanked first: neither is a figure, and a name is no label.
        check = validate(without_labels(names.masked), self.facts, plan,
                         hints if hints is not None else self.hints(answer), context=answer,
                         people=[(who["id"], who["name"]) for who in self.board()], rules=self.rules)
        labels = character_labels(names.masked, self.board_names)
        problems = list(dict.fromkeys([*names.problems, *check.problems, *([labels_problem(labels)] if labels else [])]))
        return Check(ok=not problems, problems=problems, bindings=check.bindings), names.people

    def hints(self, answer: str) -> dict[int, tuple[str, int, str]]:
        """Where the code-built answer's figures came from, wherever a people-level answer repeats it verbatim."""
        assert self.scope is not None
        built = built_answer(facts.to_dict() for facts in self.facts)
        if built is None or (at := answer.find(built[0])) < 0:
            return {}
        return {at + start: source for start, source in built[1].items()}

    def structured(self, purpose: str, system: str, user: str, schema: type[Plan], *, effort: str) -> StructuredResult:
        prompt_hash = llm_audit.prompt_hash(system, user)
        try:
            result = self.provider.structured(system, user, schema, model=self.settings.llm_model_qa, effort=effort)
        except LLMError as exc:
            self._record_failure(purpose, prompt_hash, exc)
            raise
        self.model = result.model
        self._record(purpose, prompt_hash, result.model, result, "ok")
        return result

    def compose(self, purpose: str, system: str, user: str) -> TextResult:
        prompt_hash = llm_audit.prompt_hash(system, user)
        self.compose_hash = prompt_hash
        try:
            result = self.provider.compose(system, user, model=self.settings.llm_model_qa, effort="medium", max_tokens=600)
        except LLMError as exc:
            self._record_failure(purpose, prompt_hash, exc)
            raise
        self.model = result.model
        self._record(purpose, prompt_hash, result.model, result, "ok")
        return result

    def converse(
        self, purpose: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
        tool_choice: dict[str, Any] | None = None,
    ) -> ConverseResult:
        # The conversation grows each turn: hash the question and the turn, not the transcript.
        prompt_hash = llm_audit.prompt_hash(system, f"{self.question}\n#turn {len(messages)}")
        self.compose_hash = prompt_hash
        try:
            result = self.provider.converse(  # type: ignore[attr-defined]
                system, messages, tools, model=self.settings.llm_model_qa, effort="medium", tool_choice=tool_choice
            )
        except LLMError as exc:
            self._record_failure(purpose, prompt_hash, exc)
            raise
        self.model = result.model
        self._record(purpose, prompt_hash, result.model, result, "ok")
        return result

    def _record(self, purpose: str, prompt_hash: str, model: str | None, result: Any, outcome: str, error: str | None = None) -> None:
        llm_audit.record_llm_call(
            self.store,
            purpose=purpose,
            provider=self.provider_name,
            model=model,
            prompt_hash=prompt_hash,
            usage=getattr(result, "usage", None),
            latency_ms=getattr(result, "latency_ms", None),
            stop_reason=getattr(result, "stop_reason", None),
            outcome=outcome,
            error=error,
            qa_id=self.qa_id,
            now=self.now,
        )

    def _record_failure(self, purpose: str, prompt_hash: str, exc: LLMError) -> None:
        """A failed call's row, with any tokens its response reported, so the day's budget counts what was billed."""
        refused = isinstance(exc, LLMRefusal)
        # The error text names the failure, never the prompt or a secret.
        error = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("llm_call_failed", qa_id=self.qa_id, purpose=purpose, error=error, retryable=exc.retryable)
        llm_audit.record_llm_call(
            self.store, purpose=purpose, provider=self.provider_name, model=self.settings.llm_model_qa,
            prompt_hash=prompt_hash, usage=exc.usage, latency_ms=None, stop_reason=exc.stop_reason,
            outcome="refused" if refused else "error", error=error, qa_id=self.qa_id, now=self.now,
        )

    def finish(
        self,
        outcome: Outcome,
        *,
        answer: str | None = None,
        citations: Sequence[Binding] = (),
        message: str | None = None,
        suggestions: Sequence[str] = (),
        missing: Sequence[str] = (),
        extra: dict[str, Any] | None = None,
        people: Sequence[Person] = (),
    ) -> AskResponse:
        latency_ms = int((time.perf_counter() - self.started) * 1000)
        response = AskResponse(
            qa_id=self.qa_id,
            question=self.question,
            outcome=outcome,
            answer=answer,
            citations=list(citations),
            facts=list(self.facts),
            plan=list(self.calls),
            missing_evidence=list(missing),
            message=message,
            suggestions=list(suggestions),
            latency_ms=latency_ms,
            model=self.model,
            asked_at=self.asked_at,
            project=self.project,
            extra=dict(extra or {}),
            level=self.level,
            mentions=list(self.mention_ids),
            # Only an answer names anyone (answered, or no_data for members with no tickets), at people level only.
            people=list(people) if answer is not None and self.at_people_level else [],
            note=self.note,
        )
        self.store.upsert(
            "qa_log",
            {
                "qa_id": self.qa_id,
                "asked_at": self.asked_at,
                "asked_by": self.user,
                "project_key": self.project,
                "question": self.question,
                "plan_json": self.plan.model_dump(mode="json") if self.plan is not None else None,
                "facts_json": [facts.to_dict() for facts in self.facts] if self.facts else None,
                "answer": answer,
                "citations_json": [asdict(binding) for binding in citations],
                "outcome": outcome,
                "missing_json": list(missing) if missing else None,
                "model": self.model,
                "prompt_hash": self.compose_hash,
                "latency_ms": latency_ms,
                # Ids only: who was asked about is auditable, and no name is kept with it.
                "level": self.level,
                "mentions_json": list(self.mention_ids) if self.mention_ids else None,
            },
        )
        log.info(
            "ask_finished",
            qa_id=self.qa_id,
            outcome=outcome,
            project=self.project,
            level=self.level,
            mentions=list(self.mention_ids),
            calls=[call.name for call in self.calls],
            rows=[len(facts.rows) for facts in self.facts],
            latency_ms=latency_ms,
        )
        return response


_EMPHASIS = re.compile(r"(\*\*|__)(.+?)\1")
_BULLET = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])[ \t]+", re.MULTILINE)
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_BRACKETED_KEY = re.compile(r"\[([A-Z][A-Z0-9_]{1,19}-[1-9]\d{0,8})\]")


def plain_prose(text: str) -> str:
    """The answer as plain prose with no Markdown and bare issue keys, whatever the model added."""
    text = _EMPHASIS.sub(r"\2", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _BRACKETED_KEY.sub(r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- the analyst: any question, answered by querying the snapshot ------------------------------


def _analyst_available(settings: Settings, provider: LLMProvider, store: Store) -> bool:
    """A live model that can hold a tool conversation, and a snapshot on disk to attach."""
    path = str(getattr(store, "path", "") or "")
    return (
        settings.ask_mode == "analyst"
        and callable(getattr(provider, "converse", None))
        and bool(path)
        and ":memory:" not in path
        and Path(path).exists()
    )


ANALYST_DECLINED = "Ask about the Jira data."


def _declined(text: str | None) -> str | None:
    """A decline as the contract words it: a reply holding one of its two sentences is that sentence alone."""
    for sentence in (TEAM_DECLINE, JUDGEMENT_DECLINE):
        if text and sentence in text:
            return sentence
    return text


def _ranks_the_board(plan: Plan) -> bool:
    """A plan that only ranks the board's people, so it never presents the board's figures as one person's."""
    return plan.understood and bool(plan.calls) and all(
        call.name == "oversight_people" and not call.params.get("person_ids") for call in plan.calls
    )


def _ask_analyst(session: _Session, *, process: ProcessConfig, rules: RulesConfig) -> AskResponse:
    settings = session.settings
    fields = FieldsConfig.load(settings.fields_file) if settings.fields_file.exists() else FieldsConfig()
    with _readonly(session.store) as conn:
        row = conn.execute("SELECT last_synced_at FROM sync_state WHERE scope = ?", (session.project,)).fetchone()
    as_of = row[0] if row and row[0] else None
    now = session.now()
    system = analyst_system(session.project, now.date().isoformat(), settings.ask_max_steps, session.level,
                            comparison=session.comparison)
    tools = analyst_tools(session.level, comparison=session.comparison)
    retry = PEOPLE_VERIFY_RETRY if session.at_people_level else VERIFY_RETRY
    # The question, and at people level the people it mentions: their names and person ids, as data.
    opening = (
        mentions_tag(session.mentions_payload)
        + no_tickets_tag(session.no_tickets_payload)
        + f"<question>{escape(session.question)}</question>"
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": opening}]
    try:
        with snapshot_tools(
            session.store.path, project=session.project, process=process, rules=rules, fields=fields,
            now=now, row_limit=settings.ask_row_limit, last_synced_at=as_of, people=session.scope,
            mentioned=[who["id"] for who in session.mentioned], comparison=session.comparison,
        ) as run_tool:
            budget = settings.ask_max_steps  # tool turns allowed before the model must write
            turns = 0
            rewrite = False
            while True:
                # Past the budget the model may only write, from what it has read so far.
                last = turns >= budget
                purpose = "analyst_strict" if rewrite else "analyst"
                result = session.converse(purpose, system, messages, tools, tool_choice={"type": "none"} if last else None)
                messages.append({"role": "assistant", "content": result.content})
                turns += 1
                if result.tool_uses and not last:
                    try:
                        answers = _run_tools(session, run_tool, result.tool_uses)
                    except ComparisonOff:
                        # A second scorecard where comparison is off ends the ask with the refusal.
                        session.facts.clear()
                        session.calls.clear()
                        return session.finish("not_understood", message=COMPARISON_OFF, suggestions=session.suggestions)
                    messages.append({"role": "user", "content": answers})
                    continue
                # Every lookup it made (a planned list caps at 3; the analyst may make more).
                plan = Plan.model_construct(understood=True, calls=list(session.calls), reason=None)
                session.plan = plan
                if not session.facts:
                    # Nothing looked up; a question that evaluates people still gets its figures, from code.
                    judged = session.judged_plan()
                    if judged is not None:
                        return _answer_in_code(session, judged, process=process, rules=rules)
                    message = session.unchecked(plain_prose(result.text), ANALYST_DECLINED)
                    return session.finish("not_understood", message=message, suggestions=session.suggestions)
                answer = tidy_markdown(result.text)
                check, named = session.verify(answer, plan)
                problems = check.problems if answer else ["the answer was empty"]
                if answer and check.ok:
                    return session.finish_answer(answer, check, named, extra={"mode": "analyst"})
                if rewrite:
                    return _unusable(session, problems, NOT_VERIFIED_MESSAGE, process=process, rules=rules)
                # One rewrite, told what failed, with room to query what it is missing.
                rewrite = True
                budget = max(budget, turns + 2)
                messages.append({"role": "user", "content": retry.format(problems="; ".join(problems[:5]))})
    except LLMError:
        if session.facts or session.judged_plan() is not None:
            return _unusable(session, [], "The model was unavailable, so only the facts are shown.",
                             process=process, rules=rules)
        return session.finish(
            "not_understood",
            message="The question could not be answered right now; try again in a moment.",
            suggestions=session.suggestions,
        )


def _unusable(session: _Session, problems: list[str], message: str, *, process: ProcessConfig,
              rules: RulesConfig) -> AskResponse:
    """The analyst's answer was unusable: answer scorecards in code, else keep only the facts (not_verified)."""
    answered = session.answer_from_code(why="the written answer could not be used")
    if answered is not None:
        return answered
    judged = session.judged_plan()
    if judged is not None:
        return _answer_in_code(session, judged, process=process, rules=rules)
    if not session.facts:
        return session.finish("not_understood", message=message, suggestions=session.suggestions)
    return session.finish("not_verified", message=message, extra={"problems": problems})


def _answer_in_code(session: _Session, judged: Plan, *, process: ProcessConfig, rules: RulesConfig) -> AskResponse:
    """Plan and answer a question that evaluates people in code; refused only where comparison is off."""
    if not judged.understood or not judged.calls:
        session.plan = judged
        return session.finish("not_understood", message=judged.reason or COMPARISON_OFF, suggestions=session.suggestions)
    specs = specs_for(session.level, comparison=session.comparison)
    checked, problem = _validated(list(judged.calls), specs)
    if problem is not None:  # never expected: the plan is code's own
        session.plan = judged
        return session.finish("not_understood", message=f"The plan could not be used: {problem}.",
                              suggestions=session.suggestions)
    ctx = CatalogContext(process=process, rules=rules, project=session.project, now=session.now(),
                         row_limit=session.settings.ask_row_limit, people=session.scope)
    try:
        with _readonly(session.store) as conn:
            for call, params in checked:
                facts = specs[call.name].run(conn, params, ctx)
                facts.ref = f"F{len(session.facts) + 1}"
                session.facts.append(facts)
                session.calls.append(call)
    except ComparisonOff:
        session.plan = judged
        return session.finish("not_understood", message=COMPARISON_OFF, suggestions=session.suggestions)
    session.plan = Plan.model_construct(understood=True, calls=list(session.calls), reason=None)
    if all(facts.empty for facts in session.facts):  # a board where no one holds a ticket: no one to set out
        return session.finish("no_data", message=f"Nothing in the {session.project} snapshot matched: no one holds a "
                                                 "ticket on this board.")
    answered = session.answer_from_code(why="a question that evaluates people")
    if answered is not None:
        return answered
    return session.finish("not_verified", message=NOT_VERIFIED_MESSAGE)


def _run_tools(session: _Session, run_tool: Callable[[str, Any], Facts], uses: list[Any]) -> list[dict[str, Any]]:
    """Each tool_use answered: a fact set read back to the model, or the error it can fix."""
    answers: list[dict[str, Any]] = []
    for use in uses:
        if use.name == SCORECARD_TOOL and not session.comparison:
            # Comparing people is off: one person's scorecard per ask, however the calls are split.
            asked = use.input.get("person_ids") if isinstance(use.input, dict) else None
            people = session.scored | {who for who in asked or [] if isinstance(who, str)}
            if len(people) > 1:
                raise ComparisonOff(COMPARISON_OFF)
        try:
            facts = run_tool(use.name, use.input)
        except (QueryError, ValueError) as exc:
            answers.append({"type": "tool_result", "tool_use_id": use.id, "content": str(exc), "is_error": True})
            continue
        facts.ref = f"F{len(session.facts) + 1}"
        session.facts.append(facts)
        session.calls.append(PlanCall(name=use.name, params=dict(facts.params)))
        if use.name == SCORECARD_TOOL:
            session.scored |= _scored([facts.params])
        answers.append({"type": "tool_result", "tool_use_id": use.id, "content": tool_result_text(facts)})
    return answers


def _scored(params: Iterable[Any]) -> set[str]:
    """The people these scorecard calls' parameters ask for (validated models, or a fact set's params)."""
    people: set[str] = set()
    for each in params:
        ids = each.get("person_ids") if isinstance(each, Mapping) else getattr(each, "person_ids", None)
        if isinstance(ids, (list, tuple)):
            people |= {who for who in ids if isinstance(who, str)}
    return people


def _validated(calls: list[PlanCall], specs: Mapping[str, QuerySpec]) -> tuple[list[tuple[PlanCall, BaseModel]], str | None]:
    """Each call with its parameters checked against the level's catalogue, or the first problem found."""
    checked: list[tuple[PlanCall, BaseModel]] = []
    for call in calls:
        try:
            checked.append((call, validate_params(call.name, call.params, specs)))
        except ValueError as exc:
            return [], str(exc)
    return checked, None


@contextmanager
def _readonly(store: Store) -> Iterator[sqlite3.Connection]:
    """A connection that cannot write: the store's file opened read-only, or the in-memory store's own connection."""
    if store.path is None:
        yield store.conn
        return
    conn = open_readonly(store.path)
    try:
        yield conn
    finally:
        conn.close()


def _describe(spec: QuerySpec, params: BaseModel) -> str:
    given = params.model_dump(mode="json", exclude_none=True)
    inside = ", ".join(f"{key}={value}" for key, value in given.items())
    return f"{spec.name}({inside})"


# --- console tables ---------------------------------------------------------------------------


def _render_facts(facts: Facts) -> list[str]:
    params = ", ".join(f"{key}={value}" for key, value in facts.params.items())
    header = f"{facts.ref} {facts.tool}({params})"
    if facts.as_of:
        header += f"  as of {facts.as_of}"
    lines = [header]
    if facts.note:
        lines.append(f"  {facts.note}")
    if not facts.rows:
        lines.append("  (no rows)")
        return lines
    table = [[column.label for column in facts.columns]]
    table.extend([_cell(row.get(column.key), column.kind) for column in facts.columns] for row in facts.rows)
    widths = [max(len(line[index]) for line in table) for index in range(len(facts.columns))]
    for number, line in enumerate(table):
        lines.append("  " + "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip())
        if number == 0:
            lines.append("  " + "  ".join("-" * width for width in widths))
    if facts.truncated:
        lines.append(f"  (more rows exist; showing {len(facts.rows)})")
    return lines


def _cell(value: Any, kind: str) -> str:
    if value is None:
        return _EMPTY_CELL
    if isinstance(value, bool):
        return "yes" if value else "no"
    if kind == "hours" and isinstance(value, (int, float)):
        return f"{value:,.1f}"
    if kind == "ratio" and isinstance(value, (int, float)):
        return f"{value:.2f}"
    if kind == "share" and isinstance(value, (int, float)):
        return f"{value * 100:.1f}%"
    if kind in ("count", "number") and isinstance(value, int):
        return f"{value:,}"
    if kind == "date" and isinstance(value, str):
        return value[:16].replace("T", " ")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    # One line per row: ticket text may hold newlines.
    return " ".join(str(value).split())
