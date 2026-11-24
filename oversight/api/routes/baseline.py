from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from oversight.api.deps import ApiContext, get_ctx, read_conn
from oversight.api.meta import build_meta
from oversight.baseline import COMPARED_METRICS, comparison_note, report_paths
from oversight.logs import get_logger
from oversight.timeutil import parse_ts, to_iso

router = APIRouter()
log = get_logger("api")

_COMPARISON_STATUSES = ("compared", "skipped", "not requested")
_OLD_ZONE_NOTE, _ZONE_NOTE = "ticket 4 used ", "the reference audit used "


@router.get("/baseline/latest")
def latest_baseline(ctx: ApiContext = Depends(get_ctx), conn: sqlite3.Connection = Depends(read_conn)) -> dict[str, Any]:
    report = latest_report(ctx.settings.reports_path, ctx.project, ctx.process.stages)
    if report is None:
        name = ctx.process.project(ctx.project).name or ctx.project
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} has no baseline yet. One is written after its next sync.")
    return {"meta": build_meta(conn, ctx), **report}


def latest_report(reports_dir: Path, project: str, stages: Sequence[str]) -> dict[str, Any] | None:
    """The newest usable report for `project`, in the contract's shape; None when there is none."""
    found: list[tuple[tuple[datetime, str], dict[str, Any]]] = []
    for path in report_paths(reports_dir, project):
        try:
            report = to_contract(json.loads(path.read_text(encoding="utf-8")), stages)
        except (OSError, UnicodeDecodeError, ValueError) as exc:  # JSONDecodeError and _Unusable are ValueErrors
            log.warning("api_baseline_skipped", file=path.name, problem=f"{type(exc).__name__}: {exc}"[:200])
            continue
        meta = report["metadata"]
        if meta["project"] != project:
            log.warning("api_baseline_skipped", file=path.name, problem=f"the report is for {meta['project']}")
            continue
        generated = parse_ts(meta["generated_at"])
        assert generated is not None  # _ts checked it
        found.append(((generated, meta["as_of"]), report))
    return max(found, key=lambda item: item[0])[1] if found else None


class _Unusable(ValueError):
    """The report lacks a value the contract requires, or holds one of the wrong kind."""


def to_contract(raw: Any, stages: Sequence[str]) -> dict[str, Any]:
    """A report as baseline.py writes it -> the contract's {metadata, figures, comparison}."""
    if not isinstance(raw, dict):
        raise _Unusable("the report is not an object")
    metadata = _metadata(_obj(raw, "metadata"))
    figures = _figures(_obj(raw, "figures"), stages)
    return {
        "metadata": metadata,
        "figures": figures,
        "comparison": _comparison(raw.get("comparison"), figures, date.fromisoformat(metadata["as_of"])),
    }


def _metadata(m: dict[str, Any]) -> dict[str, Any]:
    window = _obj(m, "window")
    window_days = _count(m, "window_days")
    if window_days < 1:
        raise _Unusable("window_days")
    if m.get("comparison_status") not in _COMPARISON_STATUSES:
        raise _Unusable("comparison_status")
    return {
        "project": _text(m, "project"),
        "as_of": _day(m, "as_of"),
        "window_days": window_days,
        "zone": _text(m, "zone"),
        "window": {
            "start": _ts(window, "start"),
            "end": _ts(window, "end"),
            "start_local": _local(window, "start_local"),
            "end_local": _local(window, "end_local"),
        },
        "generated_at": _ts(m, "generated_at"),
        "process_version": _text(m, "process_version"),
        "last_synced_at": _ts(m, "last_synced_at", nullable=True),
        "metrics_computed_at": _ts(m, "metrics_computed_at", nullable=True),
        "metrics_stale": _flag(m, "metrics_stale"),
        "issues_without_metrics": _count(m, "issues_without_metrics"),
        "comparison_status": m["comparison_status"],
        "comparison_note": _text(m, "comparison_note", empty=True).replace(_OLD_ZONE_NOTE, _ZONE_NOTE, 1),
        "run_id": _text(m, "run_id"),
    }


def _figures(f: dict[str, Any], stages: Sequence[str]) -> dict[str, Any]:
    snapshot, resolved, estimates = _obj(f, "snapshot"), _obj(f, "resolved"), _obj(f, "estimates")
    logged, straight = _obj(resolved, "with_logged_time"), _obj(resolved, "straight_todo_to_done")
    per_point, late = _obj(estimates, "hours_per_point"), _obj(estimates, "estimate_set_after_start")
    within = None if estimates.get("within_25pct") is None else _obj(estimates, "within_25pct")
    if within is not None and not (_number(within, "hours_per_point") or 0) > 0:
        raise _Unusable("within_25pct.hours_per_point")
    worklogs, evidence, links = _obj(f, "worklogs"), _obj(f, "evidence"), _obj(f, "issue_links")
    total_hours = _number(worklogs, "total_hours", nullable=False)
    if total_hours is None or total_hours < 0:
        raise _Unusable("worklogs.total_hours")
    work: dict[str, Any] = {
        "count": _count(worklogs, "count"),
        "total_hours": total_hours,
        "authors": _count(worklogs, "authors"),
        "top_author_share": _author_share(_obj(worklogs, "top_author_share")),
    }
    if worklogs.get("tempo_share") is not None:
        work["tempo_share"] = _author_share(_obj(worklogs, "tempo_share"))
    in_status = _obj(f, "time_in_status")
    return {
        "snapshot": {
            "last_synced_at": _ts(snapshot, "last_synced_at", nullable=True),
            "total_issues": _count(snapshot, "total_issues"),
            "created_before_window_end": _count(snapshot, "created_before_window_end"),
            **{key: _counts(snapshot, key) for key in ("by_type", "by_status", "by_status_category")},
        },
        "resolved": {
            "in_window": _count(resolved, "in_window"),
            "with_original_estimate": _count_share(resolved, "with_original_estimate"),
            "with_story_points": _count_share(resolved, "with_story_points"),
            "with_logged_time": {key: _count_share(logged, key) for key in ("timespent", "aggregate", "worklog_rows")},
            "straight_todo_to_done": _count_share(resolved, "straight_todo_to_done")
            | {"from_statuses": _names(straight, "from_statuses"), "to_statuses": _names(straight, "to_statuses")},
            "created_in_done": _count_share(resolved, "created_in_done"),
            "done_entered_in_window": _count(resolved, "done_entered_in_window"),
        },
        "estimates": {
            "hours_per_point": {"n": _count(per_point, "n")}
            | {key: _number(per_point, key) for key in ("median", "p25", "p75")},
            "within_25pct": None if within is None else {
                "hours_per_point": _number(within, "hours_per_point"),
                "count": _count(within, "count"),
                "share": _share(within, "share"),
            },
            "estimate_set_after_start": {
                "count": _count(late, "count"), "of": _count(late, "of"), "share": _share(late, "share")
            },
        },
        # Keyed by status in stage order, as written; a stage process.yaml does not know is sent as null.
        "time_in_status": {
            str(name): _spread(_obj(in_status, name)) | {"stage": _known(in_status[name].get("stage"), stages)}
            for name in in_status
        },
        "cycle_time": _spread(_obj(f, "cycle_time")),
        "reopens": _count_share(f, "reopens"),
        "worklogs": work,
        "evidence": {"not_measurable": _names(evidence, "not_measurable"), "configured": _names(evidence, "configured")},
        "issue_links": {
            "issues_with_issue_links": _count(links, "issues_with_issue_links"),
            "issue_links": _count(links, "issue_links"),
            "issue_links_by_title": _counts(links, "issue_links_by_title"),
            "remote_links": _count(links, "remote_links"),
            "remote_links_by_host": _counts(links, "remote_links_by_host"),
        },
    }


def _comparison(rows: Any, figures: dict[str, Any], as_of: date) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise _Unusable("comparison")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("match"), bool):
            raise _Unusable("comparison row")
        metric = _text(row, "metric")
        written = _text(row, "note", empty=True)
        out.append(
            {
                "metric": metric,
                "expected": _number(row, "expected", nullable=False),
                "actual": _number(row, "actual"),
                "match": row["match"],
                "note": comparison_note(metric, figures, as_of, match=row["match"])
                if metric in COMPARED_METRICS else written,
            }
        )
    return out


# --- one value each: checked, and shaped as the contract sends it ----------------------------------


def _obj(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise _Unusable(key)
    return value


def _count(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Unusable(key)
    return value


def _number(data: dict[str, Any], key: str, *, nullable: bool = True) -> float | None:
    value = data.get(key)
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _Unusable(key)
    return value if isinstance(value, int) else round(value, 4)


def _share(data: dict[str, Any], key: str) -> float | None:
    value = _number(data, key)
    if value is not None and not 0 <= value <= 1:
        raise _Unusable(key)
    return value


def _count_share(data: dict[str, Any], key: str) -> dict[str, Any]:
    figure = _obj(data, key)
    return {"count": _count(figure, "count"), "share": _share(figure, "share")}


def _author_share(figure: dict[str, Any]) -> dict[str, Any]:
    return {"seconds": _share(figure, "seconds"), "count": _share(figure, "count")}


def _spread(figure: dict[str, Any]) -> dict[str, Any]:
    return {"n": _count(figure, "n")} | {key: _number(figure, key) for key in ("median", "p75", "p90")}


def _counts(data: dict[str, Any], key: str) -> dict[str, int]:
    counts = _obj(data, key)
    return {str(name): _count(counts, name) for name in counts}


def _names(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _Unusable(key)
    return list(value)


def _text(data: dict[str, Any], key: str, *, empty: bool = False) -> str:
    value = data.get(key)
    if value is None and empty:
        return ""
    if not isinstance(value, str) or (not value and not empty):
        raise _Unusable(key)
    return value


def _flag(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise _Unusable(key)
    return value


def _day(data: dict[str, Any], key: str) -> str:
    """A calendar day, exactly YYYY-MM-DD (date.fromisoformat also takes 20261104)."""
    text = _text(data, key)
    try:
        written = date.fromisoformat(text).isoformat()
    except ValueError:
        raise _Unusable(key) from None
    if written != text:
        raise _Unusable(key)
    return text


def _ts(data: dict[str, Any], key: str, *, nullable: bool = False) -> str | None:
    """A UTC timestamp with Z (the storage format)."""
    value = data.get(key)
    if value is None and nullable:
        return None
    try:
        moment = parse_ts(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None:
        raise _Unusable(key)
    return to_iso(moment)


def _local(data: dict[str, Any], key: str) -> str:
    """A local time with its offset, kept as written once it parses."""
    text = _text(data, key)
    try:
        if parse_ts(text) is not None:
            return text
    except ValueError:
        pass
    raise _Unusable(key)


def _known(stage: Any, stages: Sequence[str]) -> str | None:
    return stage if isinstance(stage, str) and stage in stages else None
