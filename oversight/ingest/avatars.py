"""Jira pictures: fetched after every sync, kept in `people`, served by GET /people/{id}/avatar."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Literal

import httpx

from oversight.api.people import person_id
from oversight.jira.mapping import people_named
from oversight.logs import get_logger
from oversight.store.db import Store
from oversight.timeutil import to_iso

GRAVATAR_HOST = "secure.gravatar.com"
ATLASSIAN_HOST = re.compile(r"avatar-management--avatars\.[a-z0-9-]+\.prod\.public\.atl-paas\.net")
TIMEOUT_S = 5.0  # for the whole picture, redirects included
MAX_BYTES = 256 * 1024
MAX_REDIRECTS = 3
REFRESH_AFTER = timedelta(days=7)
MAX_PER_RUN = 200
RUN_BUDGET_S = 120.0  # the most one run spends on pictures; what is left waits for the next sync
# The kinds of picture kept and served, told apart by their bytes' magic numbers (sniff).
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# Every header a picture request carries: no Authorization, no Cookie.
_HEADERS = {
    "Accept": "image/png, image/jpeg, image/gif, image/webp",
    "Accept-Encoding": "identity",
    "User-Agent": "jira-oversight",
}

Outcome = Literal["fetched", "unchanged"]


class PictureError(Exception):
    """A picture refused or not fetched. `reason` is a few words, never the URL."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Picture:
    data: bytes
    content_type: str  # one of IMAGE_TYPES, from the bytes
    version: str  # version_of(data)


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def version_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def check_url(url: object) -> httpx.URL:
    if not isinstance(url, (str, httpx.URL)) or not str(url):
        raise PictureError("no picture URL")
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError, UnicodeError):
        raise PictureError("not a URL") from None
    if parsed.scheme != "https":
        raise PictureError("not https")
    if parsed.userinfo:
        raise PictureError("has a user name or password")
    if parsed.port is not None:  # httpx gives None for https's own 443
        raise PictureError("names a port")
    # The host as it goes on the wire: lower case, and an international name in its xn-- form.
    host = parsed.raw_host.decode("ascii", errors="replace")
    if _is_ip(host):
        raise PictureError("an IP address, not a host name")
    if host != GRAVATAR_HOST and not ATLASSIAN_HOST.fullmatch(host):
        raise PictureError("not a picture host")
    return parsed


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def gravatar_default(url: httpx.URL) -> httpx.URL | None:
    if url.host != GRAVATAR_HOST:
        return None
    default = url.params.get("d") or url.params.get("default")
    try:
        return check_url(default)
    except PictureError:
        return None


class PictureFetcher:
    """Fetches pictures under the module's rules; one client for a whole run. Use as a context manager."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
        max_bytes: int = MAX_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self._clock = clock
        # No auth, and a cookie jar that takes nothing: a picture request carries no credential of any kind.
        self._http = httpx.Client(
            transport=transport,
            auth=None,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PictureFetcher:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def fetch(self, url: object) -> Picture:
        """The picture at `url`; PictureError when a rule refuses it or the fetch fails."""
        deadline = self._clock() + self.timeout_s
        target = check_url(url)
        default = gravatar_default(target)
        redirects = 0
        while True:
            response = self._send(target, deadline)
            try:
                if response.status_code not in _REDIRECTS:
                    return self._read(response, deadline)
                if redirects == self.max_redirects:
                    raise PictureError(f"more than {self.max_redirects} redirects")
                try:
                    target = self._redirect(target, response)
                except PictureError:
                    if default is None or target.host != GRAVATAR_HOST:
                        raise
                    target, default = default, None
                redirects += 1
            finally:
                response.close()

    def _send(self, url: httpx.URL, deadline: float) -> httpx.Response:
        left = deadline - self._clock()
        if left <= 0:
            raise PictureError(f"took longer than {self.timeout_s:g} s")
        request = httpx.Request(
            "GET", url, headers=_HEADERS, extensions={"timeout": httpx.Timeout(min(left, self.timeout_s)).as_dict()}
        )
        try:
            return self._http.send(request, stream=True)
        except httpx.TimeoutException:
            raise PictureError("timed out") from None
        except httpx.HTTPError as exc:
            raise PictureError(f"failed ({type(exc).__name__})") from None

    def _redirect(self, current: httpx.URL, response: httpx.Response) -> httpx.URL:
        """Where a redirect points, once it has met the same rules as the first URL."""
        location = response.headers.get("Location")
        if not location:
            raise PictureError(f"HTTP {response.status_code} without a Location")
        try:
            return check_url(current.join(location))
        except PictureError as exc:
            raise PictureError(f"redirected: {exc.reason}") from None
        except (httpx.InvalidURL, TypeError, ValueError, UnicodeError):
            raise PictureError("redirected: not a URL") from None

    def _read(self, response: httpx.Response, deadline: float) -> Picture:
        if response.status_code != 200:
            raise PictureError(f"HTTP {response.status_code}")
        coding = response.headers.get("Content-Encoding", "").strip().lower()
        if coding not in ("", "identity"):
            raise PictureError("compressed body")
        too_big = f"larger than {self.max_bytes // 1024} KB"
        declared = response.headers.get("Content-Length", "").strip()
        if declared.isascii() and declared.isdigit() and int(declared) > self.max_bytes:
            raise PictureError(too_big)  # said up front: not a byte read
        body = bytearray()
        try:
            # With no content coding (refused above) these are the bytes as they arrive, a chunk at a time.
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > self.max_bytes:
                    raise PictureError(too_big)  # and nothing more is read
                if self._clock() > deadline:
                    raise PictureError(f"took longer than {self.timeout_s:g} s")
        except httpx.TimeoutException:
            raise PictureError("timed out") from None
        except (httpx.HTTPError, httpx.StreamError) as exc:
            raise PictureError(f"failed ({type(exc).__name__})") from None
        data = bytes(body)
        content_type = sniff(data)
        if content_type is None:
            raise PictureError("not a PNG, JPEG, GIF or WebP picture")
        return Picture(data=data, content_type=content_type, version=version_of(data))


# --- after a sync ------------------------------------------------------------------------------


@dataclass
class PictureStats:
    fetched: int = 0  # a new or changed picture stored
    unchanged: int = 0  # fetched again: the same bytes as the one stored
    failed: int = 0  # refused or not fetched: whatever was stored stays
    # Each picture not fetched, in order: {"person": person id, "reason": a few words}; never a URL or a name.
    failures: list[dict[str, str]] = field(default_factory=list)

    def add(self, outcome: Outcome) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)

    def fail(self, person: str, reason: str) -> None:
        """One picture not fetched: counted, kept with its reason, and logged as a warning."""
        self.failed += 1
        self.failures.append({"person": person, "reason": reason})
        _log().warning("picture_not_fetched", person=person, reason=reason)

    def as_dict(self) -> dict[str, int]:
        return {"fetched": self.fetched, "unchanged": self.unchanged, "failed": self.failed}

    def record(self) -> dict[str, Any]:
        return {**self.as_dict(), "failures": [dict(failure) for failure in self.failures]}


_DUE_SQL = """
    SELECT account_id, avatar_url, avatar_hash
    FROM people
    WHERE avatar_url IS NOT NULL AND avatar_url != ''
      AND (account_id IN (SELECT assignee_id FROM issues WHERE project_key IN ({marks}))
           OR account_id IN (SELECT account_id FROM board_members WHERE project_key IN ({marks}))
           OR account_id IN (SELECT value FROM json_each(?)))
      AND (avatar_hash IS NULL OR avatar_source IS NOT avatar_url
           OR avatar_fetched_at IS NULL OR avatar_fetched_at < ?)
    ORDER BY avatar_hash IS NOT NULL, avatar_fetched_at, account_id
    LIMIT ?
"""
# What one people field holds on each of the boards' issues now (JSON), where it holds anything.
_FIELD_SQL = """
    SELECT CASE WHEN json_valid(raw_json) THEN CASE WHEN json_type(raw_json, ?) IN ('array', 'object')
           THEN json_extract(raw_json, ?) END END AS value
    FROM issues WHERE project_key IN ({marks})
"""


def due(store: Store, projects: Sequence[str], now: datetime, limit: int = MAX_PER_RUN,
        people_fields: Sequence[str] = ()) -> list[sqlite3.Row]:
    if not projects:
        return []
    named = json.dumps(sorted(named_in_fields(store, projects, people_fields)))
    sql = _DUE_SQL.format(marks=", ".join("?" * len(projects)))
    return store.query(sql, (*projects, *projects, named, to_iso(now - REFRESH_AFTER), limit))


def named_in_fields(store: Store, projects: Sequence[str], people_fields: Sequence[str]) -> set[str]:
    """The accounts the boards' people fields (by Jira field id: Programmer, Testers, ...) name now"""
    found: set[str] = set()
    for field_id in people_fields:
        path = f'$.fields."{field_id}"'
        for row in store.query(_FIELD_SQL.format(marks=", ".join("?" * len(projects))), (path, path, *projects)):
            if not row["value"]:
                continue
            try:
                found.update(people_named(json.loads(row["value"])))
            except (ValueError, RecursionError):
                continue
    return found


def refresh_pictures(
    store: Store,
    fetcher: PictureFetcher,
    *,
    projects: Sequence[str],
    now: Callable[[], datetime],
    limit: int = MAX_PER_RUN,
    budget_s: float = RUN_BUDGET_S,
    clock: Callable[[], float] = time.monotonic,
    people_fields: Sequence[str] = (),
) -> PictureStats:
    """Fetch the pictures due for these boards' people, each kept in its own short transaction."""
    stats = PictureStats()
    people = due(store, projects, now(), limit, people_fields)
    ends = clock() + budget_s
    for index, row in enumerate(people):
        if clock() >= ends:
            _log().warning("pictures_deferred", waiting=len(people) - index, budget_s=budget_s)
            break
        who = person_id(row["account_id"])
        try:
            picture = fetcher.fetch(row["avatar_url"])
            outcome = _keep(store, row, picture, to_iso(now()))
        except PictureError as exc:
            stats.fail(who, exc.reason)
            continue
        except sqlite3.Error as exc:
            stats.fail(who, f"not stored ({type(exc).__name__})")
            continue
        except Exception as exc:  # anything unforeseen: one person's picture never stops the rest, or the sync
            stats.fail(who, f"unexpected {type(exc).__name__}")
            continue
        stats.add(outcome)
    _log().info("pictures_refreshed", due=len(people), **stats.as_dict())
    return stats


def _keep(store: Store, row: sqlite3.Row, picture: Picture, fetched_at: str) -> Outcome:
    """Store a fetched picture, or note that the one stored is still the person's."""
    with store.transaction():
        if picture.version == row["avatar_hash"]:
            store.conn.execute(
                "UPDATE people SET avatar_source = ?, avatar_fetched_at = ? WHERE account_id = ?",
                (row["avatar_url"], fetched_at, row["account_id"]),
            )
            return "unchanged"
        store.conn.execute(
            "UPDATE people SET avatar = ?, avatar_type = ?, avatar_hash = ?, avatar_source = ?, avatar_fetched_at = ?"
            " WHERE account_id = ?",
            (picture.data, picture.content_type, picture.version, row["avatar_url"], fetched_at, row["account_id"]),
        )
        return "fetched"


def _log() -> Any:
    # Looked up per call: a logger bound at import would miss configure_logging().
    return get_logger("pictures")
