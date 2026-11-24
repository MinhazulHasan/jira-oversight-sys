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


_READS_PEOPLE = re.compile(r"\b(?:people|scorecard|assignee|assignee_id)\b", re.IGNORECASE)


class _BoardNames:

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
    """Whether a people-level answer read person-level data, and whose (see the module docstring)"""
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
