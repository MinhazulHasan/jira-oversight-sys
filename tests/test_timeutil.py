from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from oversight.errors import ConfigError
from oversight.timeutil import (
    UTC,
    epoch_ms,
    hours_between,
    iso,
    jql_datetime,
    local_midnight,
    parse_ts,
    to_iso,
    zone,
)


@pytest.mark.parametrize(
    "raw",
    [
        "2026-11-03T22:54:38.662-0600",
        "2026-11-03T22:54:38.662-06:00",
        "2026-11-04T04:54:38.662Z",
        "2026-11-04T04:54:38.662+0000",
        1793768078662,
        "1793768078662",
        1793768078.662,
    ],
)
def test_parse_ts_accepts_every_jira_form(raw: object) -> None:
    assert iso(raw) == "2026-11-04T04:54:38.662Z"


def test_parse_ts_empty_values() -> None:
    assert parse_ts(None) is None
    assert parse_ts("") is None


def test_parse_ts_rejects_naive_and_odd_values() -> None:
    with pytest.raises(ValueError):
        parse_ts("2026-11-03T22:54:38")
    with pytest.raises(TypeError):
        parse_ts(True)
    with pytest.raises(TypeError):
        parse_ts(["2026"])


def test_storage_format_sorts_as_text() -> None:
    a = to_iso(datetime(2026, 1, 2, 3, 4, 5, 6000, tzinfo=UTC))
    b = to_iso(datetime(2026, 1, 2, 3, 4, 5, 7000, tzinfo=UTC))
    assert a == "2026-01-02T03:04:05.006Z"
    assert a < b


def test_to_iso_rejects_naive() -> None:
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 1, 1))


def test_hours_between() -> None:
    assert hours_between("2026-11-01T00:00:00.000Z", "2026-11-01T12:30:00.000Z") == 12.5


def test_epoch_ms_round_trip() -> None:
    dt = datetime(2026, 11, 4, 4, 54, 38, 662000, tzinfo=UTC)
    assert epoch_ms(dt) == 1793768078662
    assert parse_ts(epoch_ms(dt)) == dt


def test_jql_datetime_uses_the_account_zone_and_drops_seconds() -> None:
    utc = datetime(2026, 11, 4, 4, 54, 59, tzinfo=UTC)
    assert jql_datetime(utc, "America/Chicago") == "2026-11-03 22:54"
    assert jql_datetime(utc, None) == "2026-11-04 04:54"


def test_local_midnight_matches_the_ticket4_window() -> None:
    start = local_midnight(date(2026, 11, 2) - timedelta(days=90), "America/Chicago")
    end = local_midnight(date(2026, 11, 2), "America/Chicago")
    assert to_iso(start) == "2026-08-04T05:00:00.000Z"
    assert to_iso(end) == "2026-11-02T06:00:00.000Z"


def test_zone_blank_is_utc_and_unknown_is_a_config_error() -> None:
    assert zone("") is UTC
    assert zone("UTC").utcoffset(None) == timezone.utc.utcoffset(None)
    with pytest.raises(ConfigError):
        zone("Mars/Olympus_Mons")
