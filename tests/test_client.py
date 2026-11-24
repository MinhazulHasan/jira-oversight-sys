"""The read-only Jira client"""
from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

import oversight.jira.client as client_module
from oversight.errors import ConfigError, JiraAuthError, JiraBudgetSpent, JiraError, JiraUnavailable
from oversight.jira.client import JiraClient
from oversight.settings import Settings
from oversight.timeutil import UTC

JIRA_FIXTURES = Path(__file__).parent / "fixtures" / "jira"
BASE_URL = "https://fake.atlassian.net"
EMAIL = "dev@example.com"
TOKEN = "tok-3f9a1c-do-not-leak"
BASIC = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
SERVER_DATE = "Wed, 04 Nov 2026 10:00:00 GMT"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeJira:
    """MockTransport handler"""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    @classmethod
    def replying(cls, *replies: httpx.Response | Exception) -> FakeJira:
        """Answer with `replies` in order, raising the exceptions."""
        queue = list(replies)

        def handler(request: httpx.Request) -> httpx.Response:
            assert queue, f"unexpected extra request: {request.method} {request.url}"
            reply = queue.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return cls(handler)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def client(self, **options: Any) -> JiraClient:
        return JiraClient(
            BASE_URL, EMAIL, TOKEN, transport=httpx.MockTransport(self), sleep=self.sleeps.append, **options
        )

    def bodies(self) -> list[dict[str, Any]]:
        return [body_of(request) for request in self.requests]

    def params(self, name: str) -> list[str | None]:
        return [request.url.params.get(name) for request in self.requests]


def ok(body: Any) -> httpx.Response:
    return httpx.Response(200, json=body)


def body_of(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def load_fixture(name: str) -> Any:
    return json.loads((JIRA_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def logs() -> Iterator[list[dict[str, Any]]]:
    """Every structlog event of the test, debug included, as raw dicts."""
    saved = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        with capture_logs() as captured:
            yield captured
    finally:
        structlog.configure(**saved)


def test_429_waits_for_retry_after_then_succeeds() -> None:
    jira = FakeJira.replying(
        httpx.Response(429, headers={"Retry-After": "7"}), ok({"accountId": "acc-dev-1"})
    )
    client = jira.client()
    assert client.myself() == {"accountId": "acc-dev-1"}
    assert jira.sleeps == [7]
    assert len(jira.requests) == 2
    assert client.request_count == 2


def test_retry_after_as_an_http_date_is_measured_from_the_server_date() -> None:
    jira = FakeJira.replying(
        httpx.Response(429, headers={"Retry-After": "Wed, 04 Nov 2026 10:00:30 GMT", "Date": SERVER_DATE}),
        ok({}),
    )
    jira.client().myself()
    assert jira.sleeps == [30]


def test_retry_after_date_without_a_date_header_is_measured_from_our_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "utcnow", lambda: datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    jira = FakeJira.replying(
        httpx.Response(503, headers={"Retry-After": "Wed, 04 Nov 2026 10:00:45 GMT"}), ok({})
    )
    jira.client().myself()
    assert jira.sleeps == [45]


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        pytest.param("600", 120, id="seconds above the cap"),
        pytest.param("Wed, 04 Nov 2026 12:00:00 GMT", 120, id="date two hours ahead"),
        pytest.param("Wed, 04 Nov 2026 09:00:00 GMT", 0, id="date already passed"),
        pytest.param("-5", 0, id="negative seconds"),
        pytest.param("0", 0, id="zero"),
        pytest.param("2.5", 2.5, id="fractional seconds"),
    ],
)
def test_retry_after_is_clamped_to_zero_and_the_cap(retry_after: str, expected: float) -> None:
    jira = FakeJira.replying(
        httpx.Response(429, headers={"Retry-After": retry_after, "Date": SERVER_DATE}), ok({})
    )
    jira.client().myself()
    assert jira.sleeps == [expected]


def test_the_retry_after_cap_is_configurable() -> None:
    jira = FakeJira.replying(httpx.Response(429, headers={"Retry-After": "600"}), ok({}))
    jira.client(max_retry_after_s=10).myself()
    assert jira.sleeps == [10]


@pytest.mark.parametrize("retry_after", ["soon", "nan", "   "])
def test_an_unusable_retry_after_falls_back_to_backoff(retry_after: str) -> None:
    jira = FakeJira.replying(httpx.Response(429, headers={"Retry-After": retry_after}), ok({}))
    jira.client().myself()
    assert jira.sleeps == [1]


def test_5xx_backs_off_1_2_4_then_succeeds() -> None:
    jira = FakeJira.replying(*(httpx.Response(503) for _ in range(3)), ok({"id": "10023"}))
    client = jira.client()
    assert client.project("AIL") == {"id": "10023"}
    assert jira.sleeps == [1, 2, 4]
    assert client.request_count == 4


def test_any_5xx_is_retried() -> None:
    jira = FakeJira.replying(httpx.Response(500), httpx.Response(502), httpx.Response(504), ok({}))
    jira.client().myself()
    assert len(jira.requests) == 4


def test_gives_up_after_four_sends_with_no_sleep_after_the_last() -> None:
    jira = FakeJira.replying(*(httpx.Response(503) for _ in range(4)))
    client = jira.client()
    with pytest.raises(JiraUnavailable) as err:
        client.myself()
    assert err.value.status_code == 503
    assert err.value.path == "/rest/api/3/myself"
    assert "after 4 attempts" in str(err.value)
    assert len(jira.requests) == 4
    assert client.request_count == 4
    assert jira.sleeps == [1, 2, 4]


def test_a_rate_limit_that_never_lifts_ends_in_jira_unavailable_429() -> None:
    jira = FakeJira.replying(*(httpx.Response(429, headers={"Retry-After": "7"}) for _ in range(4)))
    with pytest.raises(JiraUnavailable) as err:
        jira.client().myself()
    assert err.value.status_code == 429
    assert jira.sleeps == [7, 7, 7]


def test_max_attempts_bounds_the_sends() -> None:
    jira = FakeJira.replying(httpx.Response(503), httpx.Response(503))
    with pytest.raises(JiraUnavailable):
        jira.client(max_attempts=2).myself()
    assert len(jira.requests) == 2
    assert jira.sleeps == [1]


def test_a_single_attempt_never_sleeps() -> None:
    jira = FakeJira.replying(httpx.Response(503))
    with pytest.raises(JiraUnavailable):
        jira.client(max_attempts=1).myself()
    assert jira.sleeps == []


def test_a_large_attempt_count_backs_off_no_longer_than_the_cap() -> None:
    jira = FakeJira.replying(*(httpx.Response(503) for _ in range(10)))
    with pytest.raises(JiraUnavailable):
        jira.client(max_attempts=10).myself()
    assert jira.sleeps == [1, 2, 4, 8, 16, 32, 64, 120, 120]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(httpx.ConnectError("connection refused"), id="connect"),
        pytest.param(httpx.ReadTimeout("read timed out"), id="read timeout"),
        pytest.param(httpx.RemoteProtocolError("peer closed the connection"), id="protocol"),
    ],
)
def test_transport_errors_are_retried(error: httpx.TransportError) -> None:
    jira = FakeJira.replying(error, ok({"accountId": "acc-dev-1"}))
    client = jira.client()
    assert client.myself() == {"accountId": "acc-dev-1"}
    assert jira.sleeps == [1]
    assert client.request_count == 2


def test_persistent_transport_errors_end_in_jira_unavailable_without_a_status() -> None:
    jira = FakeJira.replying(*(httpx.ConnectError("connection refused") for _ in range(4)))
    with pytest.raises(JiraUnavailable) as err:
        jira.client().myself()
    assert err.value.status_code is None
    assert "ConnectError: connection refused" in str(err.value)
    assert isinstance(err.value.__cause__, httpx.ConnectError)
    assert jira.sleeps == [1, 2, 4]


@pytest.mark.parametrize(
    ("replies", "status"),
    [
        pytest.param(
            [httpx.ConnectError("x"), httpx.Response(502), httpx.ConnectError("y"), httpx.Response(504)],
            504,
            id="last was a 504",
        ),
        pytest.param(
            [httpx.Response(502), httpx.Response(503), httpx.Response(504), httpx.ReadTimeout("z")],
            None,
            id="last was a timeout",
        ),
    ],
)
def test_jira_unavailable_reports_the_last_attempt(replies: list[Any], status: int | None) -> None:
    jira = FakeJira.replying(*replies)
    with pytest.raises(JiraUnavailable) as err:
        jira.client().myself()
    assert err.value.status_code == status


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_raise_at_once(status: int) -> None:
    jira = FakeJira.replying(httpx.Response(status, json={"errorMessages": ["Client must be authenticated"]}))
    client = jira.client()
    with pytest.raises(JiraAuthError) as err:
        client.myself()
    assert err.value.status_code == status
    assert err.value.path == "/rest/api/3/myself"
    assert len(jira.requests) == 1 and client.request_count == 1
    assert jira.sleeps == []


def test_404_raises_jira_error_with_status_path_and_body_excerpt() -> None:
    body = {"errorMessages": ["No project could be found with key 'NOPE'."], "errors": {}}
    jira = FakeJira.replying(httpx.Response(404, json=body))
    with pytest.raises(JiraError) as err:
        jira.client().project("NOPE")
    exc = err.value
    assert type(exc) is JiraError
    assert exc.status_code == 404
    assert exc.path == "/rest/api/3/project/NOPE"
    assert str(exc).startswith("GET /rest/api/3/project/NOPE returned HTTP 404: ")
    assert "No project could be found with key 'NOPE'." in str(exc)
    assert len(jira.requests) == 1
    assert jira.sleeps == []


@pytest.mark.parametrize("status", [400, 405, 409, 410, 422])
def test_other_4xx_raise_jira_error_with_their_status(status: int) -> None:
    jira = FakeJira.replying(httpx.Response(status, json={"errorMessages": ["nope"], "errors": {}}))
    with pytest.raises(JiraError) as err:
        list(jira.client().bulk_changelog(["AIL-1"], ["status"]))
    assert type(err.value) is JiraError
    assert err.value.status_code == status
    assert len(jira.requests) == 1


def test_the_error_excerpt_is_one_line_of_at_most_300_characters() -> None:
    body = "first line\nsecond\x1b[31m line\r\n" + "x" * 1000 + "TAIL"
    jira = FakeJira.replying(httpx.Response(400, text=body))
    with pytest.raises(JiraError) as err:
        jira.client().myself()
    prefix = "GET /rest/api/3/myself returned HTTP 400: "
    message = str(err.value)
    assert message.startswith(prefix)
    excerpt = message[len(prefix):]
    assert len(excerpt) <= 300
    assert excerpt.startswith("first line second [31m line xxx")
    assert "\n" not in message and "\r" not in message and "\x1b" not in message
    assert "TAIL" not in message


def test_an_empty_error_body_gives_a_plain_message() -> None:
    jira = FakeJira.replying(httpx.Response(404))
    with pytest.raises(JiraError) as err:
        jira.client().myself()
    assert str(err.value) == "GET /rest/api/3/myself returned HTTP 404"


def test_redirects_are_not_followed() -> None:
    jira = FakeJira.replying(httpx.Response(302, headers={"Location": "https://evil.example/collect"}))
    with pytest.raises(JiraError) as err:
        jira.client().myself()
    assert err.value.status_code == 302
    assert [request.url.host for request in jira.requests] == ["fake.atlassian.net"]


def test_a_body_that_cannot_be_decoded_raises_jira_error_without_a_retry(logs: list[dict[str, Any]]) -> None:
    corrupt = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip"))
    jira = FakeJira.replying(corrupt)
    with pytest.raises(JiraError, match="DecodingError") as err:
        jira.client().myself()
    assert isinstance(err.value.__cause__, httpx.DecodingError)
    assert len(jira.requests) == 1 and jira.sleeps == []
    [send] = [event for event in logs if event["event"] == "jira_request"]
    assert send["error"] == "DecodingError"


def test_a_2xx_body_that_is_not_json_raises_jira_error() -> None:
    jira = FakeJira.replying(httpx.Response(200, text="<html><title>Log in</title></html>"))
    with pytest.raises(JiraError, match="not JSON") as err:
        jira.client().myself()
    assert err.value.status_code == 200
    assert "Log in" in str(err.value)


@pytest.mark.parametrize(
    ("call", "body"),
    [
        pytest.param(lambda c: c.myself(), [], id="myself as a list"),
        pytest.param(lambda c: c.fields(), {"id": "status"}, id="fields as an object"),
        pytest.param(lambda c: c.remote_links("AIL-1"), None, id="remote links as null"),
        pytest.param(
            lambda c: c.issue_changelog("AIL-1"), {"values": {"id": "1"}}, id="changelog values as an object"
        ),
        pytest.param(lambda c: list(c.search_jql("project = AIL", ["id"])), [], id="search page as a list"),
    ],
)
def test_an_unexpected_response_shape_raises_jira_error(call: Callable[[JiraClient], Any], body: Any) -> None:
    jira = FakeJira.replying(ok(body))
    with pytest.raises(JiraError, match="unexpected response"):
        call(jira.client())


def test_204_returns_none() -> None:
    jira = FakeJira.replying(httpx.Response(204))
    assert jira.client().request("GET", "/rest/api/3/myself") is None


@pytest.mark.parametrize("content", [b"", b" \r\n"], ids=["empty", "whitespace"])
def test_an_empty_200_body_returns_none(content: bytes) -> None:
    jira = FakeJira.replying(httpx.Response(200, content=content))
    assert jira.client().request("GET", "/rest/api/3/myself") is None


def _echo_credentials(status: int) -> Handler:
    """A misbehaving proxy that echoes the auth header and token into its error body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"header={request.headers['Authorization']} token={TOKEN}")

    return handler


@pytest.mark.parametrize(
    ("handler", "error_type"),
    [
        pytest.param(_echo_credentials(404), JiraError, id="404 echoing credentials"),
        pytest.param(_echo_credentials(401), JiraAuthError, id="401 echoing credentials"),
        pytest.param(_echo_credentials(200), JiraError, id="non-JSON 200 echoing credentials"),
        pytest.param(_echo_credentials(503), JiraUnavailable, id="503 echoing credentials"),
    ],
)
def test_credentials_never_appear_in_errors_or_logs(
    logs: list[dict[str, Any]], handler: Handler, error_type: type[Exception]
) -> None:
    jira = FakeJira(handler)
    with pytest.raises(error_type) as err:
        jira.client().myself()
    exposed = f"{err.value} {err.value!r} {err.value.args!r} {logs!r}"
    assert TOKEN not in exposed
    assert BASIC not in exposed
    assert logs, "the sends should have been logged"


def test_a_transport_error_mentioning_the_token_is_scrubbed(logs: list[dict[str, Any]]) -> None:
    jira = FakeJira.replying(*(httpx.ConnectError(f"proxy refused {TOKEN}") for _ in range(4)))
    with pytest.raises(JiraUnavailable) as err:
        jira.client().myself()
    assert TOKEN not in str(err.value)
    assert "proxy refused ***" in str(err.value)
    assert TOKEN not in repr(logs)


def test_each_send_logs_one_debug_line_with_the_route(logs: list[dict[str, Any]]) -> None:
    jira = FakeJira.replying(httpx.Response(503), ok([]))
    jira.client().project_statuses("AIL")
    sends = [event for event in logs if event["event"] == "jira_request"]
    assert [
        (e["log_level"], e["logger"], e["method"], e["route"], e["status"], e["attempt"]) for e in sends
    ] == [
        ("debug", "jira", "GET", "/rest/api/3/project/{key}/statuses", 503, 1),
        ("debug", "jira", "GET", "/rest/api/3/project/{key}/statuses", 200, 2),
    ]
    assert all(isinstance(e["elapsed_ms"], int) and e["elapsed_ms"] >= 0 for e in sends)
    retries = [event for event in logs if event["event"] == "jira_retry"]
    assert [(e["log_level"], e["status"], e["wait_s"]) for e in retries] == [("warning", 503, 1)]


def test_a_transport_error_is_logged_with_its_type(logs: list[dict[str, Any]]) -> None:
    jira = FakeJira.replying(httpx.ConnectError("connection refused"), ok({}))
    jira.client().myself()
    first = next(event for event in logs if event["event"] == "jira_request")
    assert (first["status"], first["error"]) == (None, "ConnectError")


def test_basic_auth_and_the_json_accept_header_are_sent() -> None:
    jira = FakeJira.replying(ok({}))
    jira.client().myself()
    [sent] = jira.requests
    assert sent.headers["Authorization"] == f"Basic {BASIC}"
    assert sent.headers["Accept"] == "application/json"
    assert str(sent.url) == "https://fake.atlassian.net/rest/api/3/myself"


def test_timeouts_are_applied_to_every_request() -> None:
    jira = FakeJira.replying(ok({}))
    jira.client(timeout_s=12.0, connect_timeout_s=3.0).myself()
    timeout = jira.requests[0].extensions["timeout"]
    assert timeout == {"connect": 3.0, "read": 12.0, "write": 12.0, "pool": 12.0}


def test_a_base_url_with_a_path_keeps_it() -> None:
    jira = FakeJira.replying(ok({}))
    transport = httpx.MockTransport(jira)
    JiraClient(f"{BASE_URL}/jira/", EMAIL, TOKEN, transport=transport, sleep=jira.sleeps.append).myself()
    assert jira.requests[0].url.path == "/jira/rest/api/3/myself"


def test_from_settings_builds_a_working_client(settings: Settings) -> None:
    jira = FakeJira.replying(ok({"accountId": "acc-dev-1", "timeZone": "America/Chicago"}))
    transport = httpx.MockTransport(jira)
    with JiraClient.from_settings(settings, transport=transport, sleep=jira.sleeps.append) as client:
        assert client.myself()["timeZone"] == "America/Chicago"
        assert (client.max_attempts, client.page_size) == (4, 100)
    [sent] = jira.requests
    assert str(sent.url) == "https://fake.atlassian.net/rest/api/3/myself"
    expected = base64.b64encode(b"dev@example.com:test-token").decode()
    assert sent.headers["Authorization"] == f"Basic {expected}"
    assert sent.extensions["timeout"] == {"connect": 5.0, "read": 30.0, "write": 30.0, "pool": 30.0}


def test_from_settings_uses_attempts_and_page_size_and_overrides_win(settings: Settings) -> None:
    tuned = settings.model_copy(update={"jira_max_attempts": 2, "jira_page_size": 7})
    jira = FakeJira.replying(httpx.Response(503), httpx.Response(503))
    client = JiraClient.from_settings(tuned, transport=httpx.MockTransport(jira), sleep=jira.sleeps.append)
    with pytest.raises(JiraUnavailable):
        client.myself()
    assert len(jira.requests) == 2
    assert client.page_size == 7
    assert JiraClient.from_settings(tuned, page_size=3, transport=httpx.MockTransport(jira)).page_size == 3


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "fake.atlassian.net",
        "ftp://fake.atlassian.net",
        "https://",
        "https://dev:hunter2@fake.atlassian.net",
        "https://fake.atlassian.net/?jql=x",
        "https://fake.atlassian.net/#frag",
    ],
)
def test_a_bad_base_url_is_a_config_error_that_does_not_echo_it(base_url: str) -> None:
    with pytest.raises(ConfigError, match="JIRA_BASE_URL") as err:
        JiraClient(base_url, EMAIL, TOKEN)
    assert "hunter2" not in str(err.value)


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 0},
        {"page_size": 0},
        {"timeout_s": 0},
        {"connect_timeout_s": -1},
        {"max_retry_after_s": -1},
    ],
)
def test_nonsense_limits_are_config_errors(options: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        JiraClient(BASE_URL, EMAIL, TOKEN, **options)


def test_the_context_manager_closes_the_client() -> None:
    jira = FakeJira.replying(ok({}))
    with jira.client() as client:
        client.myself()
    with pytest.raises(RuntimeError, match="closed"):
        client.myself()
    assert len(jira.requests) == 1


@pytest.mark.parametrize(
    ("call", "path", "body"),
    [
        pytest.param(lambda c: c.myself(), "/rest/api/3/myself", {"accountId": "acc-dev-1"}, id="myself"),
        pytest.param(
            lambda c: c.fields(), "/rest/api/3/field", [{"id": "customfield_10016"}], id="fields"
        ),
        pytest.param(
            lambda c: c.project("AIL"),
            "/rest/api/3/project/AIL",
            {"id": "10023", "simplified": True},
            id="project",
        ),
        pytest.param(
            lambda c: c.project_statuses("AIL"),
            "/rest/api/3/project/AIL/statuses",
            [{"name": "Task", "statuses": [{"name": "To Do"}]}],
            id="project statuses",
        ),
        pytest.param(
            lambda c: c.remote_links("AIL-1140"),
            "/rest/api/3/issue/AIL-1140/remotelink",
            [{"id": 79589, "object": {"url": "https://example.atlassian.net/wiki/x"}}],
            id="remote links",
        ),
    ],
)
def test_simple_reads_return_the_body(call: Callable[[JiraClient], Any], path: str, body: Any) -> None:
    jira = FakeJira.replying(ok(body))
    assert call(jira.client()) == body
    [sent] = jira.requests
    assert (sent.method, sent.url.path, sent.url.query) == ("GET", path, b"")


def test_search_jql_follows_next_page_token_over_three_pages() -> None:
    pages = {
        None: {"issues": [{"key": "AIL-1"}, {"key": "AIL-2"}], "nextPageToken": "t1", "isLast": False},
        "t1": {"issues": [{"key": "AIL-3"}, {"key": "AIL-4"}], "nextPageToken": "t2", "isLast": False},
        "t2": {"issues": [{"key": "AIL-5"}], "isLast": True},
    }
    jira = FakeJira(lambda request: ok(pages[body_of(request).get("nextPageToken")]))
    jql = 'project = "AIL" ORDER BY updated ASC'
    got = list(jira.client().search_jql(jql, ["summary", "status"], page_size=2))
    keys = [[issue["key"] for issue in page] for page in got]
    assert keys == [["AIL-1", "AIL-2"], ["AIL-3", "AIL-4"], ["AIL-5"]]
    first, second, third = jira.bodies()
    assert first == {"jql": jql, "fields": ["summary", "status"], "maxResults": 2}
    assert second == {**first, "nextPageToken": "t1"}
    assert third == {**first, "nextPageToken": "t2"}
    assert {(r.method, r.url.path) for r in jira.requests} == {("POST", "/rest/api/3/search/jql")}


def test_search_jql_passes_real_issue_json_through_untouched() -> None:
    issue = load_fixture("issue_sample.json")
    jira = FakeJira.replying(ok({"issues": [issue], "isLast": True}))
    assert list(jira.client().search_jql('project = "AIL"', ["summary"])) == [[issue]]


def test_search_jql_uses_the_client_page_size_by_default() -> None:
    jira = FakeJira.replying(ok({"issues": [{"key": "AIL-1"}], "isLast": True}))
    list(jira.client(page_size=50).search_jql('project = "AIL"', ["id"]))
    assert jira.bodies()[0]["maxResults"] == 50


def test_search_jql_stops_on_is_last_even_with_a_token() -> None:
    jira = FakeJira.replying(ok({"issues": [{"key": "AIL-1"}], "nextPageToken": "t1", "isLast": True}))
    assert len(list(jira.client().search_jql('project = "AIL"', ["id"]))) == 1
    assert len(jira.requests) == 1


def test_search_jql_stops_when_the_token_is_missing() -> None:
    jira = FakeJira.replying(ok({"issues": [{"key": "AIL-1"}], "isLast": False}))
    assert len(list(jira.client().search_jql('project = "AIL"', ["id"]))) == 1
    assert len(jira.requests) == 1


def test_search_jql_stops_on_an_empty_page_without_yielding_it() -> None:
    jira = FakeJira.replying(ok({"issues": [], "nextPageToken": "t1", "isLast": False}))
    assert list(jira.client().search_jql('project = "AIL"', ["id"])) == []
    assert len(jira.requests) == 1


@pytest.mark.parametrize(
    "tokens", [["t1", "t1"], ["t1", "t2", "t1"]], ids=["same token twice", "token cycle"]
)
def test_search_jql_refuses_a_repeated_token(tokens: list[str]) -> None:
    jira = FakeJira.replying(
        *(ok({"issues": [{"key": f"AIL-{n}"}], "nextPageToken": token}) for n, token in enumerate(tokens))
    )
    pages = []
    with pytest.raises(JiraError, match="same nextPageToken twice"):
        for page in jira.client().search_jql('project = "AIL"', ["id"]):
            pages.append(page)
    assert len(pages) == len(tokens)
    assert len(jira.requests) == len(tokens)


def test_search_jql_fetches_a_page_only_when_it_is_needed() -> None:
    jira = FakeJira.replying(ok({"issues": [{"id": "52507"}], "nextPageToken": "t1"}))
    pages = jira.client().search_jql('project in (AIL) AND cf[10016] is not EMPTY', ["id"], page_size=1)
    assert jira.requests == []
    assert next(pages) == [{"id": "52507"}]
    assert len(jira.requests) == 1


def test_search_jql_refuses_a_negative_page_size_before_any_request() -> None:
    jira = FakeJira.replying()
    with pytest.raises(ValueError, match="page_size"):
        jira.client().search_jql('project = "AIL"', ["id"], page_size=-1)
    assert jira.requests == []


def test_bulk_changelog_chunks_issues_by_1000_and_fields_by_10() -> None:
    ids = [str(50000 + n) for n in range(2500)]
    field_ids = ["status", "assignee", "timeoriginalestimate", "timeestimate"] + [
        f"customfield_{10130 + n}" for n in range(8)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        first_id = body_of(request)["issueIdsOrKeys"][0]
        return ok({"issueChangeLogs": [{"issueId": first_id, "changeHistories": []}]})

    jira = FakeJira(handler)
    entries = list(jira.client().bulk_changelog(ids, field_ids))
    bodies = jira.bodies()
    assert [(len(b["issueIdsOrKeys"]), len(b["fieldIds"])) for b in bodies] == [
        (1000, 10), (1000, 2), (1000, 10), (1000, 2), (500, 10), (500, 2),
    ]
    assert [issue for b in bodies[::2] for issue in b["issueIdsOrKeys"]] == ids
    assert bodies[0]["fieldIds"] + bodies[1]["fieldIds"] == field_ids
    assert all(b["maxResults"] == 1000 and "nextPageToken" not in b for b in bodies)
    assert {(r.method, r.url.path) for r in jira.requests} == {("POST", "/rest/api/3/changelog/bulkfetch")}
    assert len(entries) == 6


def test_bulk_changelog_follows_next_page_token_and_yields_every_entry() -> None:
    history = {"id": "334350", "created": 1762146596971, "items": [{"field": "status", "fieldId": "status"}]}
    pages = {
        None: {
            "issueChangeLogs": [{"issueId": "52507", "changeHistories": [history]}],
            "nextPageToken": "p2",
        },
        "p2": {
            "issueChangeLogs": [
                {"issueId": "52507", "changeHistories": [{**history, "id": "334351"}]},
                {"issueId": "52508", "changeHistories": [{**history, "id": "334360"}]},
            ]
        },
    }
    jira = FakeJira(lambda request: ok(pages[body_of(request).get("nextPageToken")]))
    entries = list(jira.client().bulk_changelog(["AIL-1140", "AIL-1141"], ["status", "customfield_10016"]))
    assert [entry["issueId"] for entry in entries] == ["52507", "52507", "52508"]
    first, second = jira.bodies()
    assert first == {
        "issueIdsOrKeys": ["AIL-1140", "AIL-1141"],
        "fieldIds": ["status", "customfield_10016"],
        "maxResults": 1000,
    }
    assert second == {**first, "nextPageToken": "p2"}


def test_bulk_changelog_refuses_a_repeated_token() -> None:
    page = {"issueChangeLogs": [{"issueId": "52507", "changeHistories": []}], "nextPageToken": "p1"}
    jira = FakeJira.replying(ok(page), ok(page), ok(page))
    with pytest.raises(JiraError, match="nextPageToken"):
        list(jira.client().bulk_changelog(["AIL-1140"], ["status"]))
    assert len(jira.requests) == 2


def test_bulk_changelog_without_field_ids_is_refused_at_the_call() -> None:
    jira = FakeJira.replying()
    with pytest.raises(ValueError, match="field id"):
        jira.client().bulk_changelog(["AIL-1140"], [])
    assert jira.requests == []


def test_bulk_changelog_with_no_issues_sends_nothing() -> None:
    jira = FakeJira.replying()
    assert list(jira.client().bulk_changelog([], ["status"])) == []
    assert jira.requests == []


def test_bulk_changelog_drops_repeated_ids() -> None:
    jira = FakeJira.replying(ok({"issueChangeLogs": []}))
    list(jira.client().bulk_changelog(["AIL-1", "AIL-2", "AIL-1"], ["status", "status"]))
    assert jira.bodies()[0]["issueIdsOrKeys"] == ["AIL-1", "AIL-2"]
    assert jira.bodies()[0]["fieldIds"] == ["status"]


def start_at_pages(
    items: list[Any], items_key: str, per_page: int, *, total: bool = True, is_last: bool = True
) -> Handler:
    """Serve `items` by startAt, at most `per_page` at a time whatever maxResults asks for."""

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startAt"])
        page = items[start : start + per_page]
        body: dict[str, Any] = {"startAt": start, "maxResults": per_page, items_key: page}
        if total:
            body["total"] = len(items)
        if is_last:
            body["isLast"] = start + len(page) >= len(items)
        return ok(body)

    return handler


def test_issue_changelog_follows_start_at_until_total() -> None:
    histories = [{"id": str(334340 + n)} for n in range(5)]
    jira = FakeJira(start_at_pages(histories, "values", 2, is_last=False))
    assert jira.client().issue_changelog("AIL-1140") == histories
    assert jira.params("startAt") == ["0", "2", "4"]
    assert jira.params("maxResults") == ["100", "100", "100"]
    assert {r.url.path for r in jira.requests} == {"/rest/api/3/issue/AIL-1140/changelog"}


def test_issue_changelog_stops_on_is_last_without_a_total() -> None:
    histories = [{"id": str(n)} for n in range(3)]
    jira = FakeJira(start_at_pages(histories, "values", 2, total=False))
    assert jira.client().issue_changelog("AIL-1140") == histories
    assert jira.params("startAt") == ["0", "2"]


def test_issue_changelog_reads_the_real_sample_in_one_request() -> None:
    sample = load_fixture("changelog_sample.json")
    jira = FakeJira.replying(ok(sample))
    assert jira.client().issue_changelog("AIL-1140") == sample["values"]
    assert len(jira.requests) == 1


def test_issue_worklogs_page_by_5000_until_total() -> None:
    worklogs = [{"id": str(67746 + n), "issueId": "52507", "timeSpentSeconds": 3600} for n in range(3)]
    jira = FakeJira(start_at_pages(worklogs, "worklogs", 2, is_last=False))
    assert jira.client().issue_worklogs("AIL-1140") == worklogs
    assert jira.params("startAt") == ["0", "2"]
    assert jira.params("maxResults") == ["5000", "5000"]
    assert {r.url.path for r in jira.requests} == {"/rest/api/3/issue/AIL-1140/worklog"}


def test_board_sprints_follow_start_at_until_is_last() -> None:
    sprints = [{"id": 4400 + n, "state": "closed"} for n in range(5)]
    jira = FakeJira(start_at_pages(sprints, "values", 2, total=False))
    assert jira.client().board_sprints(21) == sprints
    assert jira.params("startAt") == ["0", "2", "4"]
    assert jira.params("maxResults") == ["50", "50", "50"]
    assert {r.url.path for r in jira.requests} == {"/rest/agile/1.0/board/21/sprint"}


def test_start_at_paging_stops_on_an_empty_page() -> None:
    jira = FakeJira.replying(ok({"values": [], "isLast": False}))
    assert jira.client().board_sprints("21") == []
    assert len(jira.requests) == 1


FEEDS = [
    pytest.param("worklog_updated", "/rest/api/3/worklog/updated", id="updated"),
    pytest.param("worklog_deleted", "/rest/api/3/worklog/deleted", id="deleted"),
]


@pytest.mark.parametrize(("method_name", "path"), FEEDS)
def test_worklog_feeds_page_by_until_and_never_follow_next_page(method_name: str, path: str) -> None:
    pages = {
        "1762140000000": {
            "values": [{"worklogId": 67746, "updatedTime": 1762140500000, "properties": []}],
            "since": 1762140000000,
            "until": 1762140500000,
            "lastPage": False,
            "nextPage": f"{BASE_URL}{path}?since=1762140500000&from=nextPage",
        },
        "1762140500000": {
            "values": [{"worklogId": 67747, "updatedTime": 1762146591525, "properties": []}],
            "since": 1762140500000,
            "until": 1762146591525,
            "lastPage": True,
        },
    }
    jira = FakeJira(lambda request: ok(pages[request.url.params["since"]]))
    values = getattr(jira.client(), method_name)(1762140000000)
    assert [value["worklogId"] for value in values] == [67746, 67747]
    assert jira.params("since") == ["1762140000000", "1762140500000"]
    assert jira.params("from") == [None, None]
    sent = {(r.method, r.url.host, r.url.path) for r in jira.requests}
    assert sent == {("GET", "fake.atlassian.net", path)}


@pytest.mark.parametrize(("method_name", "path"), FEEDS)
@pytest.mark.parametrize("until", [1000, 999, None, "2000"], ids=["same", "backwards", "missing", "text"])
def test_a_worklog_feed_that_does_not_advance_raises(method_name: str, path: str, until: Any) -> None:
    page: dict[str, Any] = {"values": [{"worklogId": 1}], "since": 1000, "lastPage": False}
    if until is not None:
        page["until"] = until
    jira = FakeJira.replying(ok(page), ok(page))
    with pytest.raises(JiraError, match="did not advance"):
        getattr(jira.client(), method_name)(1000)
    assert len(jira.requests) == 1


def test_a_worklog_feed_without_a_last_page_flag_stops_after_one_page() -> None:
    jira = FakeJira.replying(ok({"values": [{"worklogId": 1}], "since": 0, "until": 5}))
    assert jira.client().worklog_deleted(0) == [{"worklogId": 1}]
    assert len(jira.requests) == 1


@pytest.mark.parametrize("since", [-1, True, 1.5, "1762140000000"])
def test_worklog_feeds_need_epoch_milliseconds(since: Any) -> None:
    jira = FakeJira.replying()
    with pytest.raises(ValueError, match="since_ms"):
        jira.client().worklog_updated(since)
    assert jira.requests == []


def test_worklog_list_posts_ids_in_chunks_of_1000() -> None:
    ids = list(range(1, 2501))

    def handler(request: httpx.Request) -> httpx.Response:
        return ok([{"id": str(i), "issueId": "52507"} for i in body_of(request)["ids"]])

    jira = FakeJira(handler)
    worklogs = jira.client().worklog_list(ids)
    assert [len(body["ids"]) for body in jira.bodies()] == [1000, 1000, 500]
    assert [worklog["id"] for worklog in worklogs] == [str(i) for i in ids]
    assert {(r.method, r.url.path) for r in jira.requests} == {("POST", "/rest/api/3/worklog/list")}


def test_worklog_list_drops_repeated_ids() -> None:
    jira = FakeJira.replying(ok([]))
    jira.client().worklog_list([67746, 67747, 67746])
    assert jira.bodies() == [{"ids": [67746, 67747]}]


def test_worklog_list_of_nothing_sends_nothing() -> None:
    jira = FakeJira.replying()
    assert jira.client().worklog_list([]) == []
    assert jira.requests == []


ASSIGNABLE = "/rest/api/3/user/assignable/search"
TEN_RANGES = ["0", "100", "200", "300", "400", "500", "600", "700", "800", "900"]


def users(*accounts: str) -> list[dict[str, Any]]:
    return [
        {"self": f"{BASE_URL}/rest/api/3/user?accountId={account}", "accountId": account, "accountType": "atlassian",
         "emailAddress": f"{account}@example.invalid", "displayName": f"Member {account}", "active": True,
         "timeZone": "Europe/Berlin", "locale": "de_DE"}
        for account in accounts
    ]


def assignable_pages(
    site: list[dict[str, Any]], *, assignable: Callable[[int], bool] = lambda _n: True, ignore_start: bool = False
) -> Handler:
    """The read as Atlassian documents it"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("GET", ASSIGNABLE)
        start = 0 if ignore_start else int(request.url.params["startAt"])
        end = min(start + int(request.url.params["maxResults"]), 1000)
        return ok([user for place, user in enumerate(site[start:end], start) if assignable(place)])

    return handler


class Clock:
    """A fake monotonic clock"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def timed_client(handler: Handler, clock: Clock) -> tuple[FakeJira, JiraClient]:
    """A client whose sleeps move `clock` on."""
    jira = FakeJira(handler)
    return jira, JiraClient(BASE_URL, EMAIL, TOKEN, transport=httpx.MockTransport(jira), sleep=clock.sleep)


def test_assignable_users_scan_every_range_jira_serves() -> None:
    site = users(*(f"acc-{n:03d}" for n in range(300)))
    jira = FakeJira(assignable_pages(site, assignable=lambda n: n < 10 or 150 <= n < 160))
    assert jira.client().assignable_users("AIL") == site[:10] + site[150:160]  # as Jira sent them, in order
    assert jira.params("startAt") == TEN_RANGES  # the next range, whatever came of the last one
    assert jira.params("maxResults") == ["100"] * 10
    assert jira.params("project") == ["AIL"] * 10
    assert {(r.method, r.url.host, r.url.path) for r in jira.requests} == {("GET", "fake.atlassian.net", ASSIGNABLE)}
    assert client_module.ASSIGNABLE_SCAN_LIMIT == 1000


def test_empty_ranges_in_the_middle_never_end_the_read(logs: list[dict[str, Any]]) -> None:
    site = users(*(f"acc-{n:03d}" for n in range(600)))
    jira = FakeJira(assignable_pages(site, assignable=lambda n: n < 10 or 450 <= n < 460))
    found = jira.client().assignable_users("AIL")
    assert found == site[:10] + site[450:460] and len(found) == 20
    assert jira.params("startAt") == TEN_RANGES  # ten requests: four empty ranges between, four past the site's end
    assert not [event for event in logs if str(event["event"]).startswith("board_members")]


def test_assignable_users_read_on_past_short_pages() -> None:
    site = users(*(f"acc-{n:03d}" for n in range(700)))
    jira = FakeJira(assignable_pages(site, assignable=lambda n: n % 7 == 0))  # some fifteen of each hundred
    assert jira.client().assignable_users("AIL") == site[::7]
    assert len(jira.requests) == 10


@pytest.mark.parametrize(
    ("assignable", "places"),
    [
        pytest.param(lambda _n: True, range(1000), id="everyone"),
        pytest.param(lambda n: n % 3 == 0, range(0, 1000, 3), id="a third of them"),
        pytest.param(lambda n: n >= 1000, range(0), id="only people past the 1,000th"),
    ],
)
def test_a_site_of_more_than_1000_users_is_read_to_its_1000th_and_no_further(
    assignable: Callable[[int], bool], places: range
) -> None:
    site = users(*(f"acc-{n:04d}" for n in range(1500)))
    jira = FakeJira(assignable_pages(site, assignable=assignable))
    assert jira.client().assignable_users("AIL") == [site[place] for place in places]  # Jira never sends the rest
    assert jira.params("startAt") == TEN_RANGES  # exactly ten requests: never startAt 1000 or more


def test_a_server_that_ignores_start_at_costs_the_ten_requests_and_no_more_and_is_logged_once(
    logs: list[dict[str, Any]],
) -> None:
    everyone = users("acc-1", "acc-2")
    jira = FakeJira(assignable_pages(everyone, ignore_start=True))  # it sends the first page every time
    assert jira.client().assignable_users("AIL") == everyone
    assert jira.params("startAt") == TEN_RANGES  # the ten ranges end the read, never a repeat
    repeated = [event for event in logs if event["event"] == "board_members_page_repeated"]
    assert repeated == [{"event": "board_members_page_repeated", "logger": "jira", "board": "AIL", "pages": 9,
                         "kept": 2, "log_level": "warning"}]


def test_a_member_sent_again_as_the_sites_users_move_never_ends_the_read(logs: list[dict[str, Any]]) -> None:
    site = users(*(f"acc-{n:03d}" for n in range(400)))
    board = {"acc-099", "acc-250"}
    pages = assignable_pages(site, assignable=lambda place: site[place]["accountId"] in board)

    def answer(request: httpx.Request) -> httpx.Response:
        response = pages(request)
        if request.url.params["startAt"] == "0":
            site.insert(0, users("acc-joined")[0])  # someone joins, ahead of everyone, once the first page is sent
        return response

    jira = FakeJira(answer)
    assert [user["accountId"] for user in jira.client().assignable_users("AIL")] == ["acc-099", "acc-250"]
    assert jira.params("startAt") == TEN_RANGES
    repeated = [event for event in logs if event["event"] == "board_members_page_repeated"]
    assert repeated == [{"event": "board_members_page_repeated", "logger": "jira", "board": "AIL", "pages": 1,
                         "kept": 2, "log_level": "warning"}]


def test_empty_pages_never_end_the_read_nor_warn(logs: list[dict[str, Any]]) -> None:
    jira = FakeJira(assignable_pages(users("acc-1", "acc-2")))
    assert len(jira.client().assignable_users("AIL")) == 2
    assert len(jira.requests) == 10  # nine empty pages after the first, each asked for all the same
    assert not [event for event in logs if str(event["event"]).startswith("board_members")]


def test_assignable_users_keep_each_account_once_and_read_on_past_an_overlap() -> None:
    jira = FakeJira.replying(ok(users("acc-a", "acc-b")), ok(users("acc-b", "acc-c")), *[ok([])] * 8)
    assert [user["accountId"] for user in jira.client().assignable_users("AIL")] == ["acc-a", "acc-b", "acc-c"]
    assert jira.params("startAt") == TEN_RANGES


def test_assignable_users_stop_at_the_cap_and_say_so(logs: list[dict[str, Any]]) -> None:
    everyone = users(*(f"acc-{n:04d}" for n in range(2400)))
    jira = FakeJira.replying(ok(everyone[:1200]), ok(everyone[1200:]))
    assert jira.client().assignable_users("AIL") == everyone[:2000]
    assert len(jira.requests) == 2
    capped = [event for event in logs if str(event["event"]).startswith("board_members")]
    assert capped == [{"event": "board_members_capped", "logger": "jira", "board": "AIL", "kept": 2000, "cap": 2000,
                       "log_level": "warning"}]
    assert client_module.MAX_ASSIGNABLE_USERS == 2000


def test_a_read_ended_by_the_cap_still_reports_the_pages_that_repeated(logs: list[dict[str, Any]]) -> None:
    everyone = users(*(f"acc-{n:04d}" for n in range(2400)))
    jira = FakeJira.replying(ok(everyone[:1200]), ok(everyone[:1200]), ok(everyone[1200:]))
    assert jira.client().assignable_users("AIL") == everyone[:2000]
    assert len(jira.requests) == 3
    reported = [(event["event"], event.get("pages"), event["kept"]) for event in logs
                if str(event["event"]).startswith("board_members")]
    assert reported == [("board_members_capped", None, 2000), ("board_members_page_repeated", 1, 2000)]


def test_exactly_the_cap_is_kept_whole_and_not_called_capped(logs: list[dict[str, Any]]) -> None:
    everyone = users(*(f"acc-{n:04d}" for n in range(2000)))
    jira = FakeJira.replying(ok(everyone[:1000]), ok(everyone[1000:]), *[ok([])] * 8)  # pages larger than asked
    assert jira.client().assignable_users("AIL") == everyone
    assert jira.params("startAt") == TEN_RANGES  # two oversized pages, then the eight ranges left, each empty
    assert not [event for event in logs if event["event"] == "board_members_capped"]


def test_assignable_users_are_retried_like_every_read() -> None:
    jira = FakeJira.replying(httpx.Response(503), httpx.ReadTimeout("slow"), ok(users("acc-a")), *[ok([])] * 9)
    assert [user["accountId"] for user in jira.client().assignable_users("AIL")] == ["acc-a"]
    assert jira.sleeps == [1, 2]


@pytest.mark.parametrize(
    ("replies", "error", "status"),
    [
        pytest.param([httpx.Response(403, json={"errorMessages": ["no permission"]})], JiraAuthError, 403, id="403"),
        pytest.param([httpx.Response(404, json={"errorMessages": ["no project"]})], JiraError, 404, id="404"),
        pytest.param([httpx.ReadTimeout("slow")] * 4, JiraUnavailable, None, id="timeouts"),
        pytest.param([ok({"values": users("acc-a")})], JiraError, None, id="an object, not the array"),
        pytest.param([ok(["acc-a"])], JiraError, None, id="an id, not a user object"),
        pytest.param([ok([{"displayName": "Someone", "active": True}])], JiraError, None, id="no accountId"),
        pytest.param([ok(users("acc-a")), ok([{"accountId": ""}])], JiraError, None, id="a blank accountId"),
        pytest.param([httpx.Response(200, text="<html>login</html>")], JiraError, 200, id="not JSON"),
    ],
)
def test_assignable_users_fail_as_every_read_fails(
    replies: list[httpx.Response | Exception], error: type[Exception], status: int | None
) -> None:
    jira = FakeJira.replying(*replies)
    with pytest.raises(error) as raised:
        jira.client().assignable_users("AIL")
    assert isinstance(raised.value, JiraError) and raised.value.status_code == status
    assert "Someone" not in str(raised.value) and "acc-a" not in str(raised.value)  # no one is named


def test_assignable_users_stop_when_their_time_is_spent() -> None:
    clock = Clock()
    site = assignable_pages(users(*(f"acc-{n:03d}" for n in range(300))), assignable=lambda n: n % 100 == 0)

    def slow(request: httpx.Request) -> httpx.Response:
        clock.sleep(6)  # each page takes six seconds; the deadline is 10
        return site(request)

    jira, client = timed_client(slow, clock)
    with pytest.raises(JiraBudgetSpent, match="its time ran out"):
        client.assignable_users("AIL", deadline=10.0, clock=clock)
    assert len(jira.requests) == 2


def test_a_retry_wait_past_the_deadline_is_never_slept() -> None:
    clock = Clock()
    jira, client = timed_client(lambda _request: httpx.Response(429, headers={"Retry-After": "120"}), clock)
    with pytest.raises(JiraBudgetSpent, match="its time ran out") as raised:
        client.assignable_users("AIL", deadline=60.0, clock=clock)
    assert clock.now == 0.0  # Jira asked for two minutes of a one-minute budget: stopped at once
    assert len(jira.requests) == 1
    assert raised.value.status_code == 429


def test_retry_waits_that_fit_are_slept_until_the_next_would_reach_the_deadline() -> None:
    clock = Clock()
    jira, client = timed_client(lambda _request: httpx.Response(429, headers={"Retry-After": "20"}), clock)
    with pytest.raises(JiraBudgetSpent):
        client.assignable_users("AIL", deadline=60.0, clock=clock)
    assert clock.now == 40.0 and len(jira.requests) == 3  # the third wait would end at the deadline


def test_each_attempt_times_out_by_the_deadline_at_the_latest() -> None:
    clock = Clock()
    timeouts: list[tuple[float, float]] = []

    def hangs(request: httpx.Request) -> httpx.Response:
        asked = request.extensions["timeout"]
        timeouts.append((asked["connect"], asked["read"]))
        clock.sleep(asked["read"])
        raise httpx.ReadTimeout("slow", request=request)

    _jira, client = timed_client(hangs, clock)
    with pytest.raises(JiraBudgetSpent):
        client.assignable_users("AIL", deadline=45.0, clock=clock)
    assert timeouts == [(5.0, 30.0), (5.0, 14.0)]  # 30 s, a 1 s wait, then only the 14 s left
    assert clock.now == 45.0


def test_a_read_without_a_deadline_keeps_the_clients_timeouts() -> None:
    seen: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return ok([])

    FakeJira(answer).client().assignable_users("AIL")
    assert [(timeout["connect"], timeout["read"]) for timeout in seen] == [(5.0, 30.0)] * 10


@pytest.mark.parametrize("key", ["", "AIL/x", "AIL&project=OPS", "A I L", None, 7])
def test_assignable_users_need_a_plain_project_key(key: Any) -> None:
    jira = FakeJira.replying()
    with pytest.raises(ValueError, match="not a project key"):
        jira.client().assignable_users(key)
    assert jira.requests == []


SITE_USERS = "/rest/api/3/users/search"


def test_the_site_probe_asks_for_one_user_past_the_scan_and_says_true_when_there_is_one(
    logs: list[dict[str, Any]],
) -> None:
    jira = FakeJira.replying(ok(users("acc-1001st")))
    assert jira.client().site_has_users_beyond_scan() is True
    [request] = jira.requests
    assert (request.method, request.url.host, request.url.path) == ("GET", "fake.atlassian.net", SITE_USERS)
    assert dict(request.url.params) == {"startAt": "1000", "maxResults": "1"}
    text = json.dumps(logs, default=str)
    assert "acc-1001st" not in text and "@example.invalid" not in text  # nothing of the user is logged
    assert not [event for event in logs if event["event"] == "site_user_count_unknown"]


def test_the_site_probe_says_false_when_no_user_is_past_the_scan() -> None:
    jira = FakeJira.replying(ok([]))
    assert jira.client().site_has_users_beyond_scan() is False
    assert len(jira.requests) == 1


@pytest.mark.parametrize(
    ("replies", "status"),
    [
        pytest.param([httpx.Response(403, json={"errorMessages": ["no permission"]})], 403,
                     id="403: no Browse users and groups"),
        pytest.param([httpx.Response(404, json={"errorMessages": ["not found"]})], 404, id="404"),
        pytest.param([httpx.ReadTimeout("slow")] * 4, None, id="timeouts after the retries"),
        pytest.param([httpx.Response(503) for _ in range(4)], 503, id="503 after the retries"),
        pytest.param([ok({"values": users("acc-a")})], None, id="an object, not the array"),
        pytest.param([httpx.Response(204)], None, id="no body"),
        pytest.param([httpx.Response(200, text="<html>login</html>")], 200, id="not JSON"),
    ],
)
def test_the_site_probe_says_none_when_jira_will_not_say(
    replies: list[httpx.Response | Exception], status: int | None, logs: list[dict[str, Any]]
) -> None:
    jira = FakeJira.replying(*replies)
    assert jira.client().site_has_users_beyond_scan() is None
    unknown = [event for event in logs if event["event"] == "site_user_count_unknown"]
    assert unknown == [{"event": "site_user_count_unknown", "logger": "jira", "status": status, "log_level": "warning"}]
    assert "acc-a" not in json.dumps(logs, default=str)


def test_the_site_probe_lets_its_spent_time_through(logs: list[dict[str, Any]]) -> None:
    clock = Clock()
    jira, client = timed_client(lambda _request: httpx.Response(429, headers={"Retry-After": "120"}), clock)
    with pytest.raises(JiraBudgetSpent, match="its time ran out"):
        client.site_has_users_beyond_scan(deadline=10.0, clock=clock)  # Jira asked for a wait past the deadline
    assert len(jira.requests) == 1 and clock.now == 0.0
    clock.now = 10.0
    with pytest.raises(JiraBudgetSpent, match="its time ran out"):
        client.site_has_users_beyond_scan(deadline=10.0, clock=clock)  # no time left: nothing is sent
    assert len(jira.requests) == 1
    assert not [event for event in logs if event["event"] == "site_user_count_unknown"]  # the caller decides
