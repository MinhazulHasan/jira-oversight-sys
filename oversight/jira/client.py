"""Way 1: the read-only Jira REST client."""
from __future__ import annotations

import base64
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, TypeVar

import httpx

from oversight.errors import (
    ConfigError,
    JiraAuthError,
    JiraBudgetSpent,
    JiraError,
    JiraUnavailable,
    ReadOnlyViolation,
)
from oversight.logs import get_logger
from oversight.timeutil import UTC, utcnow

if TYPE_CHECKING:
    from oversight.settings import Settings

ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/rest/api/3/myself"),
        ("GET", "/rest/api/3/field"),
        ("GET", "/rest/api/3/project/{key}"),
        ("GET", "/rest/api/3/project/search"),  # board discovery: the projects this account can browse
        ("GET", "/rest/api/3/project/{key}/statuses"),
        ("POST", "/rest/api/3/search/jql"),  # a read, despite POST
        ("POST", "/rest/api/3/changelog/bulkfetch"),  # a read, despite POST
        ("GET", "/rest/api/3/issue/{key}/changelog"),
        ("GET", "/rest/api/3/issue/{key}/worklog"),
        ("GET", "/rest/api/3/issue/{key}/remotelink"),
        ("GET", "/rest/api/3/worklog/updated"),
        ("GET", "/rest/api/3/worklog/deleted"),
        ("POST", "/rest/api/3/worklog/list"),  # a read, despite POST
        ("GET", "/rest/agile/1.0/board/{id}/sprint"),
        ("GET", "/rest/api/3/user/assignable/search"),
        ("GET", "/rest/api/3/users/search"),  # only ever asked whether an account past ASSIGNABLE_SCAN_LIMIT exists
    }
)

# Jira Cloud limits for the bulk endpoints.
BULK_CHANGELOG_MAX_ISSUES = 1000
BULK_CHANGELOG_MAX_FIELDS = 10
WORKLOG_LIST_MAX_IDS = 1000

# Page sizes asked for on the other paged reads: each endpoint's documented maximum.
_BULK_CHANGELOG_PAGE_SIZE = 1000
_CHANGELOG_PAGE_SIZE = 100
_WORKLOG_PAGE_SIZE = 5000
_SPRINT_PAGE_SIZE = 50
_ASSIGNABLE_PAGE_SIZE = 100
_PROJECT_PAGE_SIZE = 50

ASSIGNABLE_SCAN_LIMIT = 1000

MAX_ASSIGNABLE_USERS = 2000

_ERROR_EXCERPT_CHARS = 300

_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_PLACEHOLDERS = frozenset({"{key}", "{id}"})
_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (route, tuple(route[1:].split("/")))
    for route in sorted({route for _, route in ALLOWED}, key=lambda r: ("{" in r, r))
)

_T = TypeVar("_T")


def route_of(path: str) -> str | None:
    """The ALLOWED route template that `path` matches (ignoring method), or None."""
    if not isinstance(path, str) or not path.startswith("/") or ".." in path:
        return None
    segments = path[1:].split("/")
    if not all(_SEGMENT.fullmatch(segment) and segment != "." for segment in segments):
        return None
    for route, parts in _ROUTES:
        if len(parts) == len(segments) and all(
            part in _PLACEHOLDERS or part == segment for part, segment in zip(parts, segments)
        ):
            return route
    return None


def check_allowed(method: str, path: str) -> str:
    """Return the matching route, or raise ReadOnlyViolation(method, path)."""
    verb = _verb(method)
    route = route_of(path)
    if route is None or (verb, route) not in ALLOWED:
        raise ReadOnlyViolation(verb, path)
    return route


class JiraClient:
    """Read-only Jira Cloud REST client. Use as a context manager."""

    request_count: int

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 30.0,
        connect_timeout_s: float = 5.0,
        max_attempts: int = 4,
        page_size: int = 100,
        max_retry_after_s: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        _check_base_url(base_url)
        if max_attempts < 1:
            raise ConfigError(f"max_attempts must be at least 1, not {max_attempts}")
        if page_size < 1:
            raise ConfigError(f"page_size must be at least 1, not {page_size}")
        if timeout_s <= 0 or connect_timeout_s <= 0:
            raise ConfigError("Jira timeouts must be positive")
        if max_retry_after_s < 0:
            raise ConfigError("max_retry_after_s must not be negative")
        self.page_size = page_size
        self.max_attempts = max_attempts
        self._timeout_s = timeout_s
        self._connect_timeout_s = connect_timeout_s
        self.max_retry_after_s = max_retry_after_s
        self.request_count = 0
        self._sleep = sleep
        # Kept only to scrub them out of error text, should Jira or a proxy echo them.
        self._secrets: tuple[str, ...] = (
            (api_token, _basic_credentials(email, api_token)) if api_token else ()
        )
        self._http = httpx.Client(
            base_url=base_url,
            auth=httpx.BasicAuth(email, api_token),
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            follow_redirects=False,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> JiraClient:
        """Build from Settings (base URL, email, token, timeouts, attempts, page size)."""
        options: dict[str, Any] = {
            "base_url": settings.jira_base_url,
            "email": settings.jira_email,
            "api_token": settings.jira_api_token.get_secret_value(),
            "timeout_s": settings.jira_timeout_s,
            "connect_timeout_s": settings.jira_connect_timeout_s,
            "max_attempts": settings.jira_max_attempts,
            "page_size": settings.jira_page_size,
        }
        options.update(overrides)
        return cls(**options)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- core -----------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Any:
        """Guard, send with retries, return parsed JSON."""
        route = check_allowed(method, path)
        verb = _verb(method)
        response: httpx.Response | None = None
        error: httpx.TransportError | None = None
        for attempt in range(1, self.max_attempts + 1):
            timeout = self._timeout_until(deadline, clock, verb, path, attempt)
            response, error = self._send(verb, path, route, attempt, params, json, timeout)
            if response is not None:
                status = response.status_code
                if 200 <= status < 300:
                    return self._parse(verb, path, response)
                if status in (401, 403):
                    raise JiraAuthError(self._describe(verb, path, response), status_code=status, path=path)
                if status != 429 and not 500 <= status < 600:
                    raise JiraError(self._describe(verb, path, response), status_code=status, path=path)
            if attempt < self.max_attempts:
                wait = self._wait_before_retry(response, attempt)
                if deadline is not None and clock() + wait >= deadline:
                    # Sleeping would spend the caller's whole budget on one wait: stop now instead.
                    raise JiraBudgetSpent(
                        f"{verb} {path} stopped after {attempt} attempts: its time ran out before the next",
                        status_code=None if response is None else response.status_code,
                        path=path,
                    ) from error
                _log().warning(
                    "jira_retry",
                    method=verb,
                    route=route,
                    status=None if response is None else response.status_code,
                    attempt=attempt,
                    wait_s=wait,
                )
                self._sleep(wait)
        if response is not None:
            last, status_code = f"HTTP {response.status_code}", response.status_code
        else:
            last, status_code = f"{type(error).__name__}: {self._scrub(str(error))}", None
        raise JiraUnavailable(
            f"{verb} {path} failed after {self.max_attempts} attempts (last: {last})",
            status_code=status_code,
            path=path,
        ) from error

    def _send(
        self,
        verb: str,
        path: str,
        route: str,
        attempt: int,
        params: dict[str, Any] | None,
        body: Any,
        timeout: httpx.Timeout | None = None,
    ) -> tuple[httpx.Response | None, httpx.TransportError | None]:
        """One send: the response, or the transport error raised instead of one."""
        self.request_count += 1
        started = time.perf_counter()
        try:
            if timeout is None:
                response = self._http.request(verb, path, params=params, json=body)
            else:
                response = self._http.request(verb, path, params=params, json=body, timeout=timeout)
        except httpx.TransportError as exc:
            _log_send(verb, route, attempt, started, error=exc)
            return None, exc
        except httpx.HTTPError as exc:
            _log_send(verb, route, attempt, started, error=exc)
            # e.g. a body that fails to decompress: not transient, so not retried.
            raise JiraError(
                f"{verb} {path} failed: {type(exc).__name__}: {self._scrub(str(exc))}", path=path
            ) from exc
        _log_send(verb, route, attempt, started, status=response.status_code)
        return response, None

    def _timeout_until(
        self, deadline: float | None, clock: Callable[[], float], verb: str, path: str, attempt: int
    ) -> httpx.Timeout | None:
        """The client's timeouts, cut to the time left before the deadline."""
        if deadline is None:
            return None
        left = deadline - clock()
        if left <= 0:
            raise JiraBudgetSpent(f"{verb} {path} stopped before attempt {attempt}: its time ran out", path=path)
        return httpx.Timeout(min(self._timeout_s, left), connect=min(self._connect_timeout_s, left))

    def _wait_before_retry(self, response: httpx.Response | None, attempt: int) -> float:
        asked = None if response is None else _retry_after_s(response)
        if asked is not None:
            return min(max(asked, 0.0), self.max_retry_after_s)
        # The same cap keeps a high JIRA_MAX_ATTEMPTS from backing off for hours.
        return min(2 ** (attempt - 1), self.max_retry_after_s)

    def _parse(self, verb: str, path: str, response: httpx.Response) -> Any:
        """The JSON of a 2xx response; None for an empty body (204)."""
        if not response.content or response.content.isspace():
            return None
        try:
            return response.json()
        except ValueError:
            # Typically an HTML page from a proxy or login redirect.
            raise JiraError(
                f"{verb} {path} returned HTTP {response.status_code} with a body that is "
                f"not JSON: {self._excerpt(response)}",
                status_code=response.status_code,
                path=path,
            ) from None

    def _describe(self, verb: str, path: str, response: httpx.Response) -> str:
        message = f"{verb} {path} returned HTTP {response.status_code}"
        excerpt = self._excerpt(response)
        return f"{message}: {excerpt}" if excerpt else message

    def _excerpt(self, response: httpx.Response) -> str:
        """At most 300 characters of the body, on one line, without credentials."""
        # Scrub before cutting, so a secret can't survive by straddling the cut.
        text = self._scrub(response.text)[: _ERROR_EXCERPT_CHARS * 4]
        # The body is untrusted: no newlines or terminal control codes in messages.
        text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
        if len(text) <= _ERROR_EXCERPT_CHARS:
            return text
        return text[: _ERROR_EXCERPT_CHARS - 3] + "..."

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    # --- simple reads -----------------------------------------------------------

    def myself(self) -> dict[str, Any]:
        """GET /rest/api/3/myself (accountId, timeZone, ...)."""
        path = "/rest/api/3/myself"
        return _as_dict(self.request("GET", path), path)

    def fields(self) -> list[dict[str, Any]]:
        """GET /rest/api/3/field: every field on the site."""
        path = "/rest/api/3/field"
        return _as_list(self.request("GET", path), path)

    def project(self, key: str) -> dict[str, Any]:
        """GET /rest/api/3/project/{key}. 404 means not visible to this account."""
        path = _path_for("GET", "/rest/api/3/project/{key}", key)
        return _as_dict(self.request("GET", path), path)

    def project_statuses(self, key: str) -> list[dict[str, Any]]:
        """GET /rest/api/3/project/{key}/statuses: one entry per issue type, each with its statuses."""
        path = _path_for("GET", "/rest/api/3/project/{key}/statuses", key)
        return _as_list(self.request("GET", path), path)

    def remote_links(self, key: str) -> list[dict[str, Any]]:
        """GET /rest/api/3/issue/{key}/remotelink (a plain list, not paged)."""
        path = _path_for("GET", "/rest/api/3/issue/{key}/remotelink", key)
        return _as_list(self.request("GET", path), path)

    # --- paged reads ------------------------------------------------------------

    def project_search(self) -> list[dict[str, Any]]:
        path = "/rest/api/3/project/search"
        params = {"action": "browse", "status": "live", "orderBy": "key"}
        return self._offset_pages(path, "values", _PROJECT_PAGE_SIZE, params=params)

    def search_jql(
        self, jql: str, fields: list[str], *, page_size: int | None = None
    ) -> Iterator[list[dict[str, Any]]]:
        """POST /rest/api/3/search/jql; yield each page's `issues` list."""
        size = page_size or self.page_size
        if size < 1:
            raise ValueError(f"page_size must be at least 1, not {size}")
        body = {"jql": jql, "fields": list(fields), "maxResults": size}
        # Returned rather than yielded from, so bad arguments fail at the call.
        return self._token_pages("/rest/api/3/search/jql", body, "issues")

    def bulk_changelog(
        self, issue_ids_or_keys: list[str], field_ids: list[str]
    ) -> Iterator[dict[str, Any]]:
        """POST /rest/api/3/changelog/bulkfetch; yield each `issueChangeLogs` entry."""
        if not field_ids:
            raise ValueError(
                "bulk_changelog needs at least one field id: without a filter Jira "
                "returns every field's history"
            )
        return self._bulk_changelog(_unique(issue_ids_or_keys), _unique(field_ids))

    def _bulk_changelog(self, issues: list[str], field_ids: list[str]) -> Iterator[dict[str, Any]]:
        path = "/rest/api/3/changelog/bulkfetch"
        for issue_chunk in _chunks(issues, BULK_CHANGELOG_MAX_ISSUES):
            for field_chunk in _chunks(field_ids, BULK_CHANGELOG_MAX_FIELDS):
                body = {
                    "issueIdsOrKeys": issue_chunk,
                    "fieldIds": field_chunk,
                    "maxResults": _BULK_CHANGELOG_PAGE_SIZE,
                }
                for page in self._token_pages(path, body, "issueChangeLogs"):
                    yield from page

    def issue_changelog(self, key: str) -> list[dict[str, Any]]:
        """GET /rest/api/3/issue/{key}/changelog: every history, following startAt."""
        path = _path_for("GET", "/rest/api/3/issue/{key}/changelog", key)
        return self._offset_pages(path, "values", _CHANGELOG_PAGE_SIZE)

    def issue_worklogs(self, key: str) -> list[dict[str, Any]]:
        """GET /rest/api/3/issue/{key}/worklog: every worklog, following startAt."""
        path = _path_for("GET", "/rest/api/3/issue/{key}/worklog", key)
        return self._offset_pages(path, "worklogs", _WORKLOG_PAGE_SIZE)

    def worklog_updated(self, since_ms: int) -> list[dict[str, Any]]:
        """GET /rest/api/3/worklog/updated?since=ms: every `values` entry."""
        return self._worklog_feed("/rest/api/3/worklog/updated", since_ms)

    def worklog_deleted(self, since_ms: int) -> list[dict[str, Any]]:
        """GET /rest/api/3/worklog/deleted?since=ms, paged like worklog_updated."""
        return self._worklog_feed("/rest/api/3/worklog/deleted", since_ms)

    def worklog_list(self, ids: list[int | str]) -> list[dict[str, Any]]:
        """POST /rest/api/3/worklog/list with {"ids": [...]}, in chunks of 1000."""
        path = "/rest/api/3/worklog/list"
        worklogs: list[dict[str, Any]] = []
        for chunk in _chunks(_unique(ids), WORKLOG_LIST_MAX_IDS):
            worklogs.extend(_as_list(self.request("POST", path, json={"ids": chunk}), path))
        return worklogs

    def board_sprints(self, board_id: int | str) -> list[dict[str, Any]]:
        """GET /rest/agile/1.0/board/{id}/sprint: every sprint, following startAt until isLast is true."""
        path = _path_for("GET", "/rest/agile/1.0/board/{id}/sprint", board_id)
        return self._offset_pages(path, "values", _SPRINT_PAGE_SIZE)

    def assignable_users(
        self,
        project_key: str,
        *,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> list[dict[str, Any]]:
        """GET /rest/api/3/user/assignable/search?project=<key>: every user assignable on the board."""
        if not isinstance(project_key, str) or not _SEGMENT.fullmatch(project_key):
            raise ValueError(f"not a project key: {project_key!r}")
        path = "/rest/api/3/user/assignable/search"
        users: dict[str, dict[str, Any]] = {}
        repeated = 0  # pages that were not empty but brought no account not already seen
        capped = False
        for start in range(0, ASSIGNABLE_SCAN_LIMIT, _ASSIGNABLE_PAGE_SIZE):
            if deadline is not None and clock() >= deadline:
                raise JiraBudgetSpent(f"GET {path} stopped after {len(users)} users: its time ran out", path=path)
            params = {"project": project_key, "startAt": start, "maxResults": _ASSIGNABLE_PAGE_SIZE}
            page = _as_list(self.request("GET", path, params=params, deadline=deadline, clock=clock), path)
            known = len(users)
            for entry in page:
                account = _account_id(entry, path)
                if account in users:
                    continue
                if len(users) >= MAX_ASSIGNABLE_USERS:
                    capped = True
                    break
                users[account] = entry
            if capped:
                _log().warning("board_members_capped", board=project_key, kept=len(users), cap=MAX_ASSIGNABLE_USERS)
                break
            if page and len(users) == known:  # counted, never an end: the next range may hold members still
                repeated += 1
        if repeated:
            _log().warning("board_members_page_repeated", board=project_key, pages=repeated, kept=len(users))
        return list(users.values())

    def site_has_users_beyond_scan(
        self, *, deadline: float | None = None, clock: Callable[[], float] = time.monotonic
    ) -> bool | None:
        path = "/rest/api/3/users/search"
        params = {"startAt": ASSIGNABLE_SCAN_LIMIT, "maxResults": 1}
        try:
            page = _as_list(self.request("GET", path, params=params, deadline=deadline, clock=clock), path)
        except JiraBudgetSpent:
            raise
        except JiraError as exc:
            _log().warning("site_user_count_unknown", status=exc.status_code)
            return None
        return bool(page)

    # --- paging -----------------------------------------------------------------

    def _token_pages(self, path: str, body: dict[str, Any], items_key: str) -> Iterator[list[Any]]:
        """POST `body` page by page, following nextPageToken; yield each non-empty page."""
        sent_tokens: set[str] = set()
        token: str | None = None
        while True:
            page_body = body if token is None else {**body, "nextPageToken": token}
            page = _as_dict(self.request("POST", path, json=page_body), path)
            items = _items(page, items_key, path)
            if not items:
                return
            yield items
            token = page.get("nextPageToken")
            if not token or page.get("isLast") is True:
                return
            if token in sent_tokens:
                raise JiraError(
                    f"POST {path} returned the same nextPageToken twice; stopping instead of looping forever",
                    path=path,
                )
            sent_tokens.add(token)

    def _offset_pages(
        self, path: str, items_key: str, page_size: int, *, params: dict[str, Any] | None = None
    ) -> list[Any]:
        items: list[Any] = []
        while True:
            page_params = {**(params or {}), "startAt": len(items), "maxResults": page_size}
            page = _as_dict(self.request("GET", path, params=page_params), path)
            batch = _items(page, items_key, path)
            items.extend(batch)
            total = page.get("total")
            # Jira may return fewer than maxResults, so the next startAt is what arrived so far.
            if not batch or page.get("isLast") is True or (isinstance(total, int) and len(items) >= total):
                return items

    def _worklog_feed(self, path: str, since_ms: int) -> list[dict[str, Any]]:
        """Every entry of the worklog updated/deleted feed from `since_ms` on."""
        if isinstance(since_ms, bool) or not isinstance(since_ms, int) or since_ms < 0:
            raise ValueError(f"since_ms must be epoch milliseconds (an int >= 0), not {since_ms!r}")
        values: list[dict[str, Any]] = []
        since = since_ms
        while True:
            page = _as_dict(self.request("GET", path, params={"since": since}), path)
            values.extend(_items(page, "values", path))
            # Only an explicit `lastPage: false` asks for more.
            if page.get("lastPage") is not False:
                return values
            until = page.get("until")
            if isinstance(until, bool) or not isinstance(until, int) or until <= since:
                raise JiraError(
                    f"GET {path} did not advance past since={since} (until={until!r}); "
                    "stopping instead of looping forever",
                    path=path,
                )
            since = until


# --- helpers ------------------------------------------------------------------------


def _log() -> Any:
    # Looked up per call: a logger bound at import would miss configure_logging().
    return get_logger("jira")


def _log_send(
    verb: str,
    route: str,
    attempt: int,
    started: float,
    *,
    status: int | None = None,
    error: Exception | None = None,
) -> None:
    """The one debug line per send. The route, not the path, keeps issue keys out."""
    fields: dict[str, Any] = {
        "method": verb,
        "route": route,
        "status": status,
        "attempt": attempt,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }
    if error is not None:
        fields["error"] = type(error).__name__
    _log().debug("jira_request", **fields)


def _verb(method: object) -> str:
    return method.upper() if isinstance(method, str) else repr(method)


def _path_for(method: str, template: str, value: object) -> str:
    """Fill a one-placeholder route; the value must stay one plain segment."""
    path = template.format(key=value, id=value)
    # A value such as "AIL/statuses" would otherwise aim the call at another route.
    if route_of(path) != template:
        raise ReadOnlyViolation(method, path)
    return path


def _check_base_url(base_url: str) -> None:
    try:
        url: httpx.URL | None = httpx.URL(base_url)
    except (TypeError, httpx.InvalidURL):
        url = None
    if (
        url is None
        or url.scheme not in ("http", "https")
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        # The value is not echoed: it may carry credentials.
        raise ConfigError(
            "JIRA_BASE_URL must be the site address, e.g. https://<site>.atlassian.net, "
            "with no credentials, query or fragment"
        )


def _basic_credentials(email: str, api_token: str) -> str:
    """The base64 part of the Basic auth header, encoded as httpx encodes it."""
    return base64.b64encode(f"{email}:{api_token}".encode()).decode("ascii")


def _retry_after_s(response: httpx.Response) -> float | None:
    """Seconds the response asks us to wait (possibly negative), or None if it doesn't say."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        retry_at = _parse_http_date(value)
        if retry_at is None:
            return None
        server_now = _parse_http_date(response.headers.get("Date", ""))
        seconds = (retry_at - (server_now or utcnow())).total_seconds()
    return None if math.isnan(seconds) else seconds


def _parse_http_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    # HTTP dates are GMT; parsedate leaves the "-0000" and asctime forms naive.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_dict(data: Any, path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise JiraError(
            f"unexpected response from {path}: expected a JSON object, got {type(data).__name__}",
            path=path,
        )
    return data


def _as_list(data: Any, path: str) -> list[Any]:
    if not isinstance(data, list):
        raise JiraError(
            f"unexpected response from {path}: expected a JSON array, got {type(data).__name__}",
            path=path,
        )
    return data


def _account_id(user: Any, path: str) -> str:
    """A user object's accountId; JiraError for anything else (the message names no account)."""
    account = user.get("accountId") if isinstance(user, dict) else None
    if not isinstance(account, str) or not account:
        raise JiraError(f"unexpected response from {path}: expected user objects with an accountId", path=path)
    return account


def _items(page: dict[str, Any], key: str, path: str) -> list[Any]:
    """A page's item list; a missing or null list is an empty page."""
    items = page.get(key)
    return [] if items is None else _as_list(items, path)


def _unique(items: Iterable[_T]) -> list[_T]:
    """Drop repeats, keeping first-seen order."""
    return list(dict.fromkeys(items))


def _chunks(items: list[_T], size: int) -> Iterator[list[_T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
