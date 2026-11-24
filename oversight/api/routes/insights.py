from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from oversight.api.deps import ApiContext, get_ctx, read_conn
from oversight.api.meta import build_meta
from oversight.insights.weekly import Insight, latest_insight
from oversight.llm.scripted import MODEL_NAME as SCRIPTED_MODEL

router = APIRouter()

_GATHERED = (
    "Rules in code decided what counts as a problem. Code then gathered the candidates (epics with the most "
    "open breaches, rules firing more often, week-on-week changes, outliers and sprint coverage) and scored "
    "each as issues affected × severity weight × week-on-week trend, keeping the top five."
)


def method_text(insight: Insight) -> str:
    """How the briefing was written: by which writer, and whether it passed the number check."""
    scripted = not insight.model or insight.model == SCRIPTED_MODEL
    writer = "A script (LLM_MODE=stub)" if scripted else f"The model {insight.model}"
    if not insight.verified:
        return (
            f"{_GATHERED} {writer} wrote a briefing from those candidates only, but it did not pass the check of "
            "every number and issue key against them, so the ranked candidates are shown instead."
        )
    return (
        f"{_GATHERED} {writer} wrote the briefing from those candidates only, and every number and issue key "
        "in it was checked against them before it was shown."
    )


@router.get("/insights/latest")
def get_latest_insights(
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    insight = latest_insight(conn, ctx.project)
    if insight is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no weekly briefing yet: run `python -m oversight insights`"
        )
    return {
        "meta": build_meta(conn, ctx),
        "week_of": insight.week_of,
        "generated_at": insight.generated_at,
        "verified": insight.verified,
        "sentences": [sentence.to_dict() for sentence in insight.sentences],
        "candidates": [candidate.to_dict() for candidate in insight.candidates],
        "method": method_text(insight),
        "model": insight.model,
    }
