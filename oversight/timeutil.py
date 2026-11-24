"""Timestamps: Jira's input formats, one storage format, and time-zone helpers."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from oversight.errors import ConfigError

UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
STALE_AFTER = timedelta(hours=2)


def utcnow() -> datetime:
    return datetime.now(UTC)


def is_stale(last_synced_at: object, now: datetime) -> bool:
    synced = parse_ts(last_synced_at)
    return synced is None or now - synced > STALE_AFTER


def parse_ts(value: object) -> datetime | None:
    """Parse a Jira timestamp into an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TypeError(f"unsupported timestamp: {value!r}")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = _from_epoch(float(value))
    elif isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").replace(".", "", 1).isdigit():
            parsed = _from_epoch(float(text))
        else:
            parsed = datetime.fromisoformat(text)
    else:
        raise TypeError(f"unsupported timestamp: {value!r}")
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def _from_epoch(number: float) -> datetime:
    if abs(number) >= 1e11:
        number /= 1000.0
    return _EPOCH + timedelta(seconds=number)


def to_iso(value: datetime | None) -> str | None:
    """Format an aware datetime in the storage format (UTC, milliseconds)."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("naive datetime: attach a time zone first")
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def iso(value: object) -> str | None:
    """Any Jira timestamp to the storage format."""
    return to_iso(parse_ts(value))


def hours_between(start: object, end: object) -> float:
    """Hours from start to end; either may be a datetime or a timestamp string."""
    a, b = parse_ts(start), parse_ts(end)
    if a is None or b is None:
        raise ValueError("hours_between needs two timestamps")
    return (b - a).total_seconds() / 3600.0


def epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("naive datetime: attach a time zone first")
    return (value - _EPOCH) // timedelta(milliseconds=1)


@lru_cache(maxsize=32)
def zone(name: str | None) -> tzinfo:
    """An IANA zone by name; blank means UTC."""
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown time zone: {name!r}") from exc


def jql_datetime(value: datetime, tz_name: str | None) -> str:
    """Format for a JQL date clause."""
    return value.astimezone(zone(tz_name)).strftime("%Y-%m-%d %H:%M")


def local_midnight(day: date, tz_name: str | None) -> datetime:
    """Local midnight at the start of `day` in the zone, as an aware UTC datetime."""
    return datetime.combine(day, time(0), tzinfo=zone(tz_name)).astimezone(UTC)
