"""Daily model token budget (per UTC day) and the GET /usage figures, read from the audit tables."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from oversight.settings import Settings
from oversight.timeutil import UTC, to_iso

# The provider name of the code-built answers (oversight/llm/scripted.py): no model behind it.
SCRIPTED = "scripted"
# Ask's note when the day's budget is used up (contract 4.6, `note`), word for word.
BUDGET_NOTE = "Today's AI budget is used up, so this answer was built without the AI model."
USAGE_DAYS = 7

_CALLS_SQL = """
    SELECT substr(called_at, 1, 10) AS day, COUNT(*) AS calls,
           COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens
    FROM llm_calls
    WHERE called_at >= ? AND called_at < ? AND provider != ?
    GROUP BY day
"""
_QUESTIONS_SQL = """
    SELECT substr(asked_at, 1, 10) AS day, COUNT(*) AS questions
    FROM qa_log
    WHERE asked_at >= ? AND asked_at < ?
    GROUP BY day
"""


def utc_day(moment: datetime) -> date:
    """The UTC calendar day a moment falls in."""
    return moment.astimezone(UTC).date()


def _start(day: date) -> str:
    return to_iso(datetime.combine(day, time(0), tzinfo=UTC)) or ""


@dataclass(frozen=True)
class DayUsage:
    day: date
    questions: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def row(self) -> dict[str, Any]:
        """One day of GET /usage's last_7_days."""
        return {"date": self.day.isoformat(), "questions": self.questions, "model_calls": self.model_calls,
                "tokens": self.tokens}


def usage_by_day(conn: sqlite3.Connection, first: date, last: date) -> list[DayUsage]:
    """Every UTC day from first to last, oldest first, days with nothing included: two grouped queries."""
    span = (_start(first), _start(last + timedelta(days=1)))
    calls = {row["day"]: row for row in _rows(conn, _CALLS_SQL, (*span, SCRIPTED))}
    questions = {row["day"]: row["questions"] for row in _rows(conn, _QUESTIONS_SQL, span)}
    days = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        key = day.isoformat()
        call = calls.get(key)
        days.append(DayUsage(
            day=day,
            questions=int(questions.get(key, 0)),
            model_calls=int(call["calls"]) if call is not None else 0,
            input_tokens=int(call["input_tokens"]) if call is not None else 0,
            output_tokens=int(call["output_tokens"]) if call is not None else 0,
        ))
    return days


def spent_today(conn: sqlite3.Connection, now: datetime) -> int:
    """The tokens the model has used today (UTC)."""
    today = utc_day(now)
    return usage_by_day(conn, today, today)[0].tokens


def budget_reached(conn: sqlite3.Connection, settings: Settings, now: datetime) -> bool:
    """Whether today's budget is used up: always False when LLM_DAILY_TOKEN_BUDGET is 0 (no limit)."""
    budget = settings.llm_daily_token_budget
    return budget > 0 and spent_today(conn, now) >= budget


def used_pct(tokens: int, budget: int) -> float | None:
    """Percentage of the budget used, one decimal; None without a budget."""
    return round(100 * tokens / budget, 1) if budget > 0 else None


def usage_report(conn: sqlite3.Connection, settings: Settings, now: datetime) -> dict[str, Any]:
    """GET /usage's figures: today's, and the last seven UTC days', oldest first, today last."""
    today = utc_day(now)
    days = usage_by_day(conn, today - timedelta(days=USAGE_DAYS - 1), today)
    current = days[-1]
    budget = settings.llm_daily_token_budget
    return {
        "today": {
            "questions": current.questions,
            "model_calls": current.model_calls,
            "input_tokens": current.input_tokens,
            "output_tokens": current.output_tokens,
            "budget_tokens": budget if budget > 0 else None,
            "budget_used_pct": used_pct(current.tokens, budget),
        },
        "last_7_days": [day.row() for day in days],
        "ai_model": "on" if settings.llm_mode == "live" else "off",
    }


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Rows by column name, whatever row factory the connection has (a Store's, a read-only one's, or none)."""
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]
