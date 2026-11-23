"""POST /ask (contract 4.6): one question in, the Ask service's answer out.

The body is {"question": "<text>", "level": "team" | "people", "mentions": [person id]}
and nothing else (422 otherwise); level defaults to "team" and mentions to [].
The dashboard's server sets the level on every request (the browser never
chooses it): "people" for an admin, and wherever sign-in is off. mentions is
for a people-level question only (422 at team level): at most ten person ids,
each p and 12 lower-case hex characters, none twice. The interface refuses
bodies over 2,000 characters and so does this. A blank question, or one over
the service's own limit, comes back as not_understood without planning, and
so does a mention of someone who holds no ticket on the board.

At people level the route works out the board's people with the person rules
(routes/people.py: people_scope: with the board as its scorecards are worked
out, and PEOPLE_COMPARISON) and the service answers with them
(oversight/ask/people.py); the response's `people` lists the people the
answer names, verified, as person objects ({id, name, avatar}), and is []
at team level and whenever the answer names no one. Since JO-11 nothing about
people is declined at people level: a question that evaluates someone gets
their scorecard or the comparison (oversight/ask/scorecards.py).

It writes only its audit rows: the service records its own qa_log and
llm_calls rows through a Store opened for the request. A model that fails or
declines is an outcome the service reports, not an HTTP error.

Governance (JO-12):
- The viewer (X-Oversight-Viewer, oversight/api/access_log.py) must be named
  for a people-level question, and for any question while ASK_RATE_LIMIT is
  set: 422 otherwise, before anything is asked.
- ASK_RATE_LIMIT (oversight/api/ratelimit.py): over the viewer's limit, 429
  {"detail", "code": "ask_rate_limited"} with Retry-After in seconds; nothing
  is written to qa_log and no model is called.
- A people-level question that read person-level data is recorded in the
  access log as person_ask, with the qa_id and outcome, whatever the outcome
  (a not_verified answer still sends its facts). It read person-level data
  when it mentioned people (picked or typed), used a person tool, ran an
  analyst query over a view or column that holds people (the people and
  scorecard views, the assignee columns), or sent facts that hold a person on
  the board by id or by name. Subjects: the people mentioned, then those the
  answer names, then everyone its facts hold by person id or by display name
  (folded for case and accents; a name two people share counts as both).
  Recorded after the answer is made and before it is sent: when the entry
  cannot be written, the answer is not sent (500, access_log_unavailable);
  its qa_log row stays, as the record of the question.
- `note` (string or null): budget.BUDGET_NOTE when the day's model budget was
  used up and the answer was built without the model (service.py).
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from oversight.api.access_log import PERSON_ASK, AccessLog, Viewer, need_viewer, read_viewer, subject_ids
from oversight.api.deps import ApiContext, ApiError, get_ctx, read_conn
from oversight.api.meta import build_meta
from oversight.api.people import PERSON_ID, fold
from oversight.api.ratelimit import RATE_LIMITED, RateLimiter
from oversight.api.routes.people import people_scope
from oversight.ask.analyst import QUERY_TOOL
from oversight.ask.people import MAX_MENTIONS, PEOPLE_CATALOG, PeopleScope, mention_problem
from oversight.ask.service import AskResponse, ask
from oversight.logs import get_logger
from oversight.store.db import Store

router = APIRouter()
log = get_logger("api")

ASK_BODY_MAX_LENGTH = 2000
# Who asked, in qa_log: the dashboard has no reader identity of its own.
ASKED_BY = "dashboard"


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=ASK_BODY_MAX_LENGTH)
    level: Literal["team", "people"] = "team"
    mentions: list[StrictStr] = Field(default_factory=list, max_length=MAX_MENTIONS)

    @field_validator("mentions")
    @classmethod
    def _person_ids(cls, mentions: list[str]) -> list[str]:
        problem = mention_problem(mentions)
        if problem is not None:
            raise ValueError(problem)
        return mentions

    @model_validator(mode="after")
    def _mentions_need_people_level(self) -> AskBody:
        if self.mentions and self.level != "people":
            raise ValueError("mentions are for a people-level question only")
        return self


def _admit(request: Request, viewer: Viewer | None, ctx: ApiContext) -> None:
    """ASK_RATE_LIMIT, per viewer: 429 with Retry-After when the viewer has asked their fill; nothing else is done."""
    limiter: RateLimiter | None = request.app.state.ask_limiter
    if limiter is None or viewer is None:
        return
    wait = limiter.admit(viewer.hash, ctx.now().timestamp())
    if wait is not None:
        log.info("ask_rate_limited", viewer=viewer.hash, retry_after_s=wait)
        raise ApiError(429, RATE_LIMITED, f"too many questions in a short time: try again in {wait} seconds",
                       headers={"Retry-After": str(wait)})


# An analyst query (oversight/ask/analyst.py) that reads what holds people at people level: the people and scorecard
# views, and the issues and findings views' assignee columns. Read as a person read whatever its result holds.
_READS_PEOPLE = re.compile(r"\b(?:people|scorecard|assignee|assignee_id)\b", re.IGNORECASE)


class _BoardNames:
    """Everyone on the board (holding a ticket now, or a member) by display name folded for case and accents, to
    find the people a fact names: a cell that is a name, or that holds a name of two words or more as whole words
    (a list or a sentence a query put together). A one-word name counts only as the whole cell, so a summary's
    word is not taken for someone. A name two people share counts as both."""

    def __init__(self, scope: PeopleScope) -> None:
        self.ids: dict[str, list[str]] = {}
        for who in (*scope.people, *scope.members):
            name = " ".join(fold(who["name"]).split())
            if name and who["id"] not in self.ids.setdefault(name, []):
                self.ids[name].append(who["id"])
        long_names = sorted((name for name in self.ids if " " in name), key=len, reverse=True)
        self._within = (re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, long_names)) + r")(?!\w)")
                        if long_names else None)

    def named(self, cell: str) -> Iterator[str]:
        """The ids of the people the cell names, in the order they appear."""
        text = " ".join(fold(cell).split())
        if text in self.ids:
            yield from self.ids[text]
        elif self._within is not None:
            for found in self._within.finditer(text):
                yield from self.ids[found.group(0)]


def _people_read(response: AskResponse, data: dict[str, Any], scope: PeopleScope) -> tuple[bool, list[str]]:
    """Whether a people-level answer read person-level data, and whose (see the module docstring): the people
    mentioned, then those the answer names, then everyone its facts hold by person id or by name."""
    tools = {call["name"] for call in data["plan"]}
    queried = any(_READS_PEOPLE.search(str((call.get("params") or {}).get("sql") or ""))
                  for call in data["plan"] if call["name"] == QUERY_TOOL)
    names = _BoardNames(scope)
    in_facts: list[str] = []
    for facts in data["facts"]:
        for row in facts["rows"]:
            for cell in row.values():
                if not isinstance(cell, str):
                    continue
                if PERSON_ID.fullmatch(cell):
                    in_facts.append(cell)
                else:
                    in_facts.extend(names.named(cell))
    subjects = subject_ids([*response.mentions, *(who["id"] for who in response.people), *in_facts])
    read = bool(response.mentions) or bool(tools & set(PEOPLE_CATALOG)) or queried or bool(subjects)
    return read, subjects


@router.post("/ask")
def post_ask(
    request: Request,
    body: AskBody,
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    # The viewer is named for every question the access log may record (people level) or the rate limit counts.
    limited = request.app.state.ask_limiter is not None
    viewer = need_viewer(request) if body.level == "people" or limited else read_viewer(request)
    _admit(request, viewer, ctx)
    people = people_scope(conn, ctx) if body.level == "people" else None
    with Store.open(ctx.db_path) as store:
        response = ask(
            body.question,
            store=store,
            process=ctx.process,
            rules=ctx.rules,
            settings=ctx.settings,
            provider=ctx.provider,
            user=ASKED_BY,
            project=ctx.project,
            now=ctx.now,
            level=body.level,
            mentions=body.mentions,
            people=people,
        )
    data = response.to_dict()
    if people is not None:
        read, subjects = _people_read(response, data, people)
        if read and viewer is not None:
            access: AccessLog = request.app.state.access_log
            access.record(viewer=viewer, level=body.level, project=ctx.project, action=PERSON_ASK, subjects=subjects,
                          detail={"qa_id": response.qa_id, "outcome": response.outcome})
    return {
        "meta": build_meta(conn, ctx),
        "qa_id": data["qa_id"],
        "question": data["question"],
        "outcome": data["outcome"],
        "answer": data["answer"],
        "citations": data["citations"],
        "facts": data["facts"],
        # The plan names each catalogue call `name`; the contract calls it `tool`.
        "plan": [{"tool": step["name"], "params": step["params"]} for step in data["plan"]],
        "missing_evidence": data["missing_evidence"],
        "message": data["message"],
        "suggestions": data["suggestions"],
        "latency_ms": data["latency_ms"],
        "model": data["model"],
        "asked_at": data["asked_at"],
        "people": data["people"],
        "note": data["note"],
    }
