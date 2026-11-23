"""GET /trends (contract 4.8): the weekly series for one metric.

Parameters as the interface validates them (src/lib/api/params.ts): `metric`,
one of the six and required; `period`, "week" (the default and only value);
`last_n`, 1 to 12 weeks ending with the current one, default 12. A name the
endpoint does not take, or one given twice, is refused as params.ts refuses it.
Every refusal is a 422. The points are the ones the Overview's trend card for
the same metric shows (oversight.api.aggregates.trend_series).
"""
from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from oversight.api.aggregates import SERIES_WEEKS, TREND_DEFINITIONS, TrendMetric, trend_series
from oversight.api.deps import PROJECT_PARAM, ApiContext, get_ctx, read_conn
from oversight.api.meta import build_meta

router = APIRouter()

_PARAMETERS = frozenset({"metric", "period", "last_n", PROJECT_PARAM})
_UNPROCESSABLE = 422


def _known_parameters(request: Request) -> None:
    """Refuse a parameter name this endpoint does not take, and a name given twice."""
    problems = []
    for name in dict.fromkeys(request.query_params.keys()):
        if name not in _PARAMETERS:
            problems.append({"loc": ["query", name], "msg": "is not a parameter of this endpoint", "type": "unknown"})
        elif len(request.query_params.getlist(name)) > 1:
            problems.append({"loc": ["query", name], "msg": "may be given only once", "type": "repeated"})
    if problems:
        raise HTTPException(_UNPROCESSABLE, problems)


@router.get("/trends", dependencies=[Depends(_known_parameters)])
def get_trends(
    metric: Annotated[TrendMetric, Query()],
    period: Annotated[Literal["week"], Query()] = "week",
    last_n: Annotated[int, Query(ge=1, le=SERIES_WEEKS)] = SERIES_WEEKS,
    ctx: ApiContext = Depends(get_ctx),
    conn: sqlite3.Connection = Depends(read_conn),
) -> dict[str, Any]:
    definition = TREND_DEFINITIONS[metric]
    return {
        "meta": build_meta(conn, ctx),
        "metric": metric,
        "period": period,
        "last_n": last_n,
        "label": definition.label,
        "unit": definition.unit,
        "direction_good": definition.direction_good,
        "note": definition.note,
        "series": trend_series(conn, ctx.project, metric, ctx.now(), last_n),
    }
