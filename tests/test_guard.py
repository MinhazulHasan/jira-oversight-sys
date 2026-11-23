"""The endpoint guard: only allowlisted reads ever reach Jira (Planning.md section 8, Guard layer)."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from oversight.errors import ReadOnlyViolation
from oversight.jira.client import ALLOWED, JiraClient, check_allowed, route_of

BASE_URL = "https://fake.atlassian.net"

# Planning.md, Phase 1, JO-10's board members (assignable users), whether the site has users past the 1,000 the
# assignable-user search can scan (users/search, one user at startAt 1000), and JO-17's board discovery
# (project/search). Growing the allowlist must be a deliberate, reviewed change.
PLANNED_ALLOWLIST = {
    ("GET", "/rest/api/3/myself"),
    ("GET", "/rest/api/3/field"),
    ("GET", "/rest/api/3/project/{key}"),
    ("GET", "/rest/api/3/project/search"),
    ("GET", "/rest/api/3/project/{key}/statuses"),
    ("POST", "/rest/api/3/search/jql"),
    ("POST", "/rest/api/3/changelog/bulkfetch"),
    ("GET", "/rest/api/3/issue/{key}/changelog"),
    ("GET", "/rest/api/3/issue/{key}/worklog"),
    ("GET", "/rest/api/3/issue/{key}/remotelink"),
    ("GET", "/rest/api/3/worklog/updated"),
    ("GET", "/rest/api/3/worklog/deleted"),
    ("POST", "/rest/api/3/worklog/list"),
    ("GET", "/rest/agile/1.0/board/{id}/sprint"),
    ("GET", "/rest/api/3/user/assignable/search"),
    ("GET", "/rest/api/3/users/search"),
}

# One realistic request per allowed (method, route): (method, path, route).
ALLOWED_CALLS = [
    ("GET", "/rest/api/3/myself", "/rest/api/3/myself"),
    ("GET", "/rest/api/3/field", "/rest/api/3/field"),
    ("GET", "/rest/api/3/project/AIL", "/rest/api/3/project/{key}"),
    ("GET", "/rest/api/3/project/search", "/rest/api/3/project/search"),
    ("GET", "/rest/api/3/project/AIL/statuses", "/rest/api/3/project/{key}/statuses"),
    ("POST", "/rest/api/3/search/jql", "/rest/api/3/search/jql"),
    ("POST", "/rest/api/3/changelog/bulkfetch", "/rest/api/3/changelog/bulkfetch"),
    ("GET", "/rest/api/3/issue/AIL-1140/changelog", "/rest/api/3/issue/{key}/changelog"),
    ("GET", "/rest/api/3/issue/AIL-1140/worklog", "/rest/api/3/issue/{key}/worklog"),
    ("GET", "/rest/api/3/issue/52507/remotelink", "/rest/api/3/issue/{key}/remotelink"),
    ("GET", "/rest/api/3/worklog/updated", "/rest/api/3/worklog/updated"),
    ("GET", "/rest/api/3/worklog/deleted", "/rest/api/3/worklog/deleted"),
    ("POST", "/rest/api/3/worklog/list", "/rest/api/3/worklog/list"),
    ("GET", "/rest/agile/1.0/board/21/sprint", "/rest/agile/1.0/board/{id}/sprint"),
    ("GET", "/rest/api/3/user/assignable/search", "/rest/api/3/user/assignable/search"),
    ("GET", "/rest/api/3/users/search", "/rest/api/3/users/search"),
]

# Writes, and reads that are simply not on the allowlist.
WRONG_METHOD_OR_ROUTE = [
    pytest.param("PUT", "/rest/api/3/issue/AIL-1", id="edit an issue"),
    pytest.param("PATCH", "/rest/api/3/issue/AIL-1", id="patch an issue"),
    pytest.param("DELETE", "/rest/api/3/issue/AIL-1", id="delete an issue"),
    pytest.param("POST", "/rest/api/3/issue", id="create an issue"),
    pytest.param("POST", "/rest/api/3/issue/AIL-1/transitions", id="transition"),
    pytest.param("POST", "/rest/api/3/issue/AIL-1/comment", id="comment"),
    pytest.param("POST", "/rest/api/3/issue/AIL-1/worklog", id="add a worklog on an allowed GET route"),
    pytest.param("PUT", "/rest/api/3/issue/AIL-1/worklog", id="PUT on an allowed GET route"),
    pytest.param("DELETE", "/rest/api/3/issue/AIL-1/remotelink", id="delete remote links"),
    pytest.param("POST", "/rest/api/3/myself", id="right route, wrong method"),
    pytest.param("DELETE", "/rest/api/3/project/AIL", id="DELETE on an allowed GET route"),
    pytest.param("PUT", "/rest/agile/1.0/board/21/sprint", id="PUT on the sprint list"),
    pytest.param("HEAD", "/rest/api/3/myself", id="HEAD"),
    pytest.param("OPTIONS", "/rest/api/3/myself", id="OPTIONS"),
    pytest.param("TRACE", "/rest/api/3/myself", id="TRACE"),
    pytest.param("", "/rest/api/3/myself", id="no method"),
    pytest.param("GET", "/rest/api/3/search/jql", id="GET on a POST-only read"),
    pytest.param("GET", "/rest/api/3/issue/AIL-1", id="a read that is not on the allowlist"),
    pytest.param("POST", "/rest/api/3/user/assignable/search", id="POST on the assignable users"),
    pytest.param("POST", "/rest/api/3/users/search", id="POST on the site's user list"),
    pytest.param("PUT", "/rest/api/3/users/search", id="PUT on the site's user list"),
    pytest.param("DELETE", "/rest/api/3/users/search", id="DELETE on the site's user list"),
    pytest.param("GET", "/rest/api/3/users", id="the site's user list by its other route"),
    pytest.param("GET", "/rest/api/3/user/search", id="the user search by name or email address"),
    pytest.param("GET", "/rest/api/3/user", id="one user, email address and all"),
    pytest.param("GET", "/rest/api/3/user/assignable/multiProjectSearch", id="assignable users of many boards"),
]

# Paths that are not plain "/rest/..." paths, so no method can use them.
PATH_TRICKS = [
    pytest.param("GET", "https://evil.example/rest/api/3/myself", id="absolute URL"),
    pytest.param("GET", "//evil.example/rest/api/3/myself", id="scheme-relative URL"),
    pytest.param("GET", "rest/api/3/myself", id="no leading slash"),
    pytest.param("GET", "/rest/api/3//myself", id="double slash"),
    pytest.param("GET", "/rest/api/3/myself/", id="trailing slash"),
    pytest.param("POST", "/rest/api/3/search/jql/../../issue", id="dot-dot onto create issue"),
    pytest.param("GET", "/rest/api/3/issue/AIL-1/../AIL-2/worklog", id="dot-dot between segments"),
    pytest.param("GET", "/rest/api/3/issue/../worklog", id="dot-dot as the key"),
    pytest.param("GET", "/rest/api/3/project/..", id="dot-dot as the last segment"),
    pytest.param("GET", "/rest/api/3/issue/AIL..1/worklog", id="dot-dot inside a key"),
    pytest.param("GET", "/rest/api/3/issue/./worklog", id="dot segment"),
    pytest.param("POST", "/rest/api/3/search/jql/%2e%2e/%2e%2e/issue", id="percent-encoded dot-dot"),
    pytest.param("GET", "/rest/api/3/issue/AIL-1%2Fworklog", id="percent-encoded slash"),
    pytest.param("GET", "/rest/api/3/project/AIL\\statuses", id="backslash"),
    pytest.param("GET", "/rest/api/3/myself?expand=groups", id="query string in the path"),
    pytest.param("POST", "/rest/api/3/search/jql?_method=DELETE", id="method override in the path"),
    pytest.param("GET", "/rest/api/3/myself#top", id="fragment"),
    pytest.param("GET", "/rest/api/3/my self", id="space"),
    pytest.param("GET", " /rest/api/3/myself", id="leading space"),
    pytest.param("GET", "/rest/api/3/myself\t", id="tab"),
    pytest.param("GET", "/rest/api/3/project/AIL\r\nX-Injected: 1", id="CRLF header injection"),
    pytest.param("GET", "/rest/api/3/project/AIL\x00", id="NUL"),
    pytest.param("GET", "/rest/api/3/issue/AIL/1/worklog", id="segment with an injected slash"),
    pytest.param("GET", "/rest/api/3/project/AIL/statuses/extra", id="extra segment"),
    pytest.param("GET", "/rest/api/3/myѕelf", id="look-alike Cyrillic letter"),
    pytest.param("GET", "/rest/agile/1.0/board/２１/sprint", id="full-width digits"),
    pytest.param("GET", "/REST/API/3/MYSELF", id="wrong case"),
    pytest.param("GET", "/rest/api/2/myself", id="another API version"),
    pytest.param("GET", "/rest/api/3/project/{key}", id="unfilled template"),
    pytest.param("GET", "", id="empty"),
    pytest.param("GET", "/", id="root"),
]


class Recorder:
    """MockTransport handler: records every request that reaches the transport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})


def _never_sleep(seconds: float) -> None:
    raise AssertionError(f"the guard tests never retry, but the client slept {seconds}s")


@pytest.fixture(autouse=True)
def logs() -> Iterator[list[dict[str, Any]]]:
    """Capture structlog events for the test and restore the configuration after.

    Capturing also keeps the client's log lines off any stream an earlier CLI
    test left structlog writing to.
    """
    saved = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        with capture_logs() as captured:
            yield captured
    finally:
        structlog.configure(**saved)


@pytest.fixture
def transport() -> Recorder:
    return Recorder()


@pytest.fixture
def client(transport: Recorder) -> Iterator[JiraClient]:
    with JiraClient(
        BASE_URL,
        "dev@example.com",
        "test-token",
        transport=httpx.MockTransport(transport),
        sleep=_never_sleep,
    ) as jira:
        yield jira


# --- the allowlist itself ------------------------------------------------------------


def test_the_allowlist_is_the_planned_set() -> None:
    assert ALLOWED == PLANNED_ALLOWLIST


def test_only_the_three_documented_read_endpoints_use_post() -> None:
    assert {method for method, _ in ALLOWED} == {"GET", "POST"}
    assert {route for method, route in ALLOWED if method == "POST"} == {
        "/rest/api/3/search/jql",
        "/rest/api/3/changelog/bulkfetch",
        "/rest/api/3/worklog/list",
    }


def test_the_allowed_calls_below_cover_every_allowed_route() -> None:
    assert {(method, route) for method, _, route in ALLOWED_CALLS} == ALLOWED


# --- what passes ---------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "route"), ALLOWED_CALLS)
def test_every_allowed_call_passes_and_is_sent_unchanged(
    client: JiraClient, transport: Recorder, method: str, path: str, route: str
) -> None:
    assert route_of(path) == route
    assert check_allowed(method, path) == route
    client.request(method, path)
    [sent] = transport.requests
    assert sent.method == method
    assert sent.url.host == "fake.atlassian.net"
    # What the guard checked is exactly what went out.
    assert sent.url.raw_path == path.encode()


def test_a_lowercase_method_is_upper_cased_and_passes(client: JiraClient, transport: Recorder) -> None:
    assert check_allowed("get", "/rest/api/3/myself") == "/rest/api/3/myself"
    assert check_allowed("post", "/rest/api/3/search/jql") == "/rest/api/3/search/jql"
    client.request("get", "/rest/api/3/myself")
    assert [request.method for request in transport.requests] == ["GET"]


@pytest.mark.parametrize("key", ["AIL", "AIL-1140", "52507", "my_proj.v2", "A"])
def test_a_placeholder_matches_one_segment_of_letters_digits_dot_dash_underscore(key: str) -> None:
    assert route_of(f"/rest/api/3/issue/{key}/worklog") == "/rest/api/3/issue/{key}/worklog"


def test_each_path_shape_resolves_to_its_own_route() -> None:
    assert route_of("/rest/api/3/project/AIL") == "/rest/api/3/project/{key}"
    assert route_of("/rest/api/3/project/AIL/statuses") == "/rest/api/3/project/{key}/statuses"
    # "statuses" is a valid key too, so this is the project route, not the statuses route.
    assert route_of("/rest/api/3/project/statuses") == "/rest/api/3/project/{key}"
    # Board discovery's search is its own route; a project keyed SEARCH (keys are upper case) is still a project.
    assert route_of("/rest/api/3/project/search") == "/rest/api/3/project/search"
    assert route_of("/rest/api/3/project/SEARCH") == "/rest/api/3/project/{key}"


# --- what never reaches Jira ---------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), WRONG_METHOD_OR_ROUTE + PATH_TRICKS)
def test_calls_outside_the_allowlist_never_reach_the_transport(
    client: JiraClient, transport: Recorder, method: str, path: str
) -> None:
    with pytest.raises(ReadOnlyViolation) as err:
        client.request(method, path)
    assert err.value.method == method.upper()
    assert err.value.path == path
    assert transport.requests == []
    assert client.request_count == 0


@pytest.mark.parametrize(("method", "path"), WRONG_METHOD_OR_ROUTE + PATH_TRICKS)
def test_check_allowed_refuses_the_same_calls(method: str, path: str) -> None:
    with pytest.raises(ReadOnlyViolation, match="read-only allowlist"):
        check_allowed(method, path)


@pytest.mark.parametrize(("method", "path"), PATH_TRICKS)
def test_route_of_rejects_anything_but_a_plain_rest_path(method: str, path: str) -> None:
    assert route_of(path) is None


def test_a_lowercase_write_is_refused_and_reported_upper_cased(
    client: JiraClient, transport: Recorder
) -> None:
    with pytest.raises(ReadOnlyViolation) as err:
        client.request("delete", "/rest/api/3/issue/AIL-1")
    assert (err.value.method, err.value.path) == ("DELETE", "/rest/api/3/issue/AIL-1")
    assert str(err.value) == "DELETE /rest/api/3/issue/AIL-1 is not on the read-only allowlist"
    assert transport.requests == []


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.project("AIL/statuses"), id="key that lands on another allowed route"),
        pytest.param(lambda c: c.project_statuses("AIL/../XYZ"), id="dot-dot in a key"),
        pytest.param(lambda c: c.issue_changelog("AIL-1/../../issue"), id="traversal from a changelog"),
        pytest.param(lambda c: c.issue_worklogs("AIL-1?expand=properties"), id="query string in a key"),
        pytest.param(lambda c: c.remote_links("AIL-1#x"), id="fragment in a key"),
        pytest.param(lambda c: c.board_sprints("21/sprint"), id="slash in a board id"),
        pytest.param(lambda c: c.project(""), id="empty key"),
        pytest.param(lambda c: c.project("."), id="dot key"),
        pytest.param(lambda c: c.issue_worklogs(".."), id="dot-dot key"),
        pytest.param(lambda c: c.project("AIL "), id="trailing space in a key"),
    ],
)
def test_helpers_refuse_a_key_that_is_not_one_plain_segment(
    client: JiraClient, transport: Recorder, call: Callable[[JiraClient], Any]
) -> None:
    with pytest.raises(ReadOnlyViolation):
        call(client)
    assert transport.requests == []


def test_the_guard_refuses_input_that_is_not_text() -> None:
    assert route_of(None) is None  # type: ignore[arg-type]
    assert route_of(b"/rest/api/3/myself") is None  # type: ignore[arg-type]
    with pytest.raises(ReadOnlyViolation):
        check_allowed("GET", b"/rest/api/3/myself")  # type: ignore[arg-type]
    with pytest.raises(ReadOnlyViolation):
        check_allowed(None, "/rest/api/3/myself")  # type: ignore[arg-type]
