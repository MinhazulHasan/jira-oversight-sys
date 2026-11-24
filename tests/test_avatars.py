from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from oversight.api.people import person_id
from oversight.cli import EXIT_OK, main
from oversight.config import FieldsConfig
from oversight.ingest import avatars
from oversight.ingest import sync as sync_module
from oversight.ingest.avatars import (
    MAX_BYTES,
    MAX_PER_RUN,
    PictureError,
    PictureFetcher,
    check_url,
    due,
    refresh_pictures,
    sniff,
    version_of,
)
from oversight.ingest.sync import SyncResult, run_sync
from oversight.jira.client import JiraClient
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import UTC, to_iso
from tests.fakes.builders import ail_and_ops
from tests.fakes.fake_jira import TOKEN, FakeJira

ATLASSIAN = "https://avatar-management--avatars.us-west-2.prod.public.atl-paas.net"
GRAVATAR = "https://secure.gravatar.com"
STORED = f"{ATLASSIAN}/712020:0f0f0f0f-aaaa-4bbb-8ccc-000000000001/5e5e5e5e-1111/48"
ON_GRAVATAR = f"{GRAVATAR}/avatar/0123456789abcdef0123456789abcdef?d={ATLASSIAN.replace(':', '%3A').replace('/', '%2F')}"
INITIALS = f"{ATLASSIAN}/initials/DO-4.png"
WITH_DEFAULT = f"{GRAVATAR}/avatar/0123456789abcdef0123456789abcdef?d={quote(INITIALS, safe='')}"
NO_DEFAULT = f"{GRAVATAR}/avatar/0123456789abcdef0123456789abcdef"
PROXY = f"https://i0.wp.com/{INITIALS.removeprefix('https://')}?ssl=1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x00" * 16
GIF = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 16
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)

Answer = Callable[[httpx.Request], httpx.Response]


def ok(data: bytes, content_type: str = "image/png", **headers: str) -> Answer:
    return lambda _request: httpx.Response(200, content=data, headers={"Content-Type": content_type, **headers})


def moved(location: str, status: int = 302, **headers: str) -> Answer:
    return lambda _request: httpx.Response(status, headers={"Location": location, **headers})


def status(code: int) -> Answer:
    return lambda _request: httpx.Response(code)


class Hosts:
    """Picture hosts in memory: each URL's answer, and every request they were sent."""

    def __init__(self, routes: dict[str, Answer] | None = None) -> None:
        self.routes = {httpx.URL(url): answer for url, answer in (routes or {}).items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get(request.url)
        return answer(request) if answer is not None else httpx.Response(404)

    def asked(self) -> list[str]:
        return [str(request.url) for request in self.requests]


def fetcher(hosts: Hosts, **options: Any) -> PictureFetcher:
    return PictureFetcher(transport=httpx.MockTransport(hosts), **options)


def fetch(hosts: Hosts, url: str = STORED, **options: Any) -> avatars.Picture:
    with fetcher(hosts, **options) as pictures:
        return pictures.fetch(url)


def refused(hosts: Hosts, url: str = STORED, **options: Any) -> str:
    with pytest.raises(PictureError) as caught:
        fetch(hosts, url, **options)
    return caught.value.reason


class Clock:
    """A monotonic clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def logs() -> Iterator[list[dict[str, Any]]]:
    """Every structlog event of the test, as dicts; the configuration is restored after."""
    saved = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        with capture_logs() as captured:
            yield captured
    finally:
        structlog.configure(**saved)


@pytest.mark.parametrize(
    "url",
    [
        STORED,
        ON_GRAVATAR,
        INITIALS,
        "https://avatar-management--avatars.eu-central-1.prod.public.atl-paas.net/712020:x/y/48",  # another region
        "HTTPS://SECURE.GRAVATAR.COM/avatar/0123",  # host names compare in lower case
        "https://avatar-management--avatars.US-WEST-2.prod.public.atl-paas.net/a",
        "https://secure.gravatar.com:443/avatar/0123",  # https's own port, written out
        f"{GRAVATAR}/avatar/0123?d=https%3A%2F%2Fevil.example%2Fx",  # where Gravatar sends it is checked then
    ],
)
def test_the_two_hosts_are_allowed(url: str) -> None:
    assert check_url(url).scheme == "https"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://secure.gravatar.com/avatar/0123", "not https"),
        ("ftp://secure.gravatar.com/avatar/0123", "not https"),
        ("//secure.gravatar.com/avatar/0123", "not https"),
        ("secure.gravatar.com/avatar/0123", "not https"),
        ("not a url at all", "not https"),
        ("https://www.gravatar.com/avatar/0123", "not a picture host"),
        ("https://gravatar.com/avatar/0123", "not a picture host"),
        ("https://xsecure.gravatar.com/avatar/0123", "not a picture host"),
        ("https://secure.gravatar.com.evil.example/avatar/0123", "not a picture host"),
        ("https://secure.gravatar.com./avatar/0123", "not a picture host"),  # the root's dot: not exactly the host
        ("https://evil.example/secure.gravatar.com/avatar/0123", "not a picture host"),
        ("https://evil.example/?u=https://secure.gravatar.com/", "not a picture host"),
        ("https://sécure.gravatar.com/avatar/0123", "not a picture host"),  # a look-alike, as xn--
        ("https://%73ecure.gravatar.com/avatar/0123", "not a picture host"),
        ("https://avatar-management--avatars.us-west-2.prod.public.atl-paas.net.evil.example/a", "not a picture host"),
        ("https://xavatar-management--avatars.us-west-2.prod.public.atl-paas.net/a", "not a picture host"),
        ("https://avatar-management--avatars..prod.public.atl-paas.net/a", "not a picture host"),
        ("https://avatar-management--avatars.us_west.prod.public.atl-paas.net/a", "not a picture host"),
        ("https://avatar-management--avatars.us-west-2.stg.public.atl-paas.net/a", "not a picture host"),
        ("https://avatar-management--avatars.a.b.prod.public.atl-paas.net/a", "not a picture host"),
        ("https://avatars.atl-paas.net/a", "not a picture host"),
        ("https://api.media.atlassian.com/file/x/image", "not a picture host"),
        ("https://", "not a picture host"),
        ("https:secure.gravatar.com/avatar/0123", "not a picture host"),
        ("https://2130706433/avatar/0123", "not a picture host"),  # 127.0.0.1 as a number
        ("https://user:secret@secure.gravatar.com/avatar/0123", "has a user name or password"),
        ("https://user@secure.gravatar.com/avatar/0123", "has a user name or password"),
        ("https://secure.gravatar.com@evil.example/avatar/0123", "has a user name or password"),
        ("https://evil.example\\@secure.gravatar.com/avatar/0123", "has a user name or password"),
        ("https://secure.gravatar.com\\@evil.example/avatar/0123", "has a user name or password"),
        ("https://secure.gravatar.com:8443/avatar/0123", "names a port"),
        ("https://secure.gravatar.com:80/avatar/0123", "names a port"),
        ("https://secure.gravatar.com:0/avatar/0123", "names a port"),
        ("https://127.0.0.1/avatar/0123", "an IP address, not a host name"),
        ("https://104.16.132.229/avatar/0123", "an IP address, not a host name"),
        ("https://[::1]/avatar/0123", "an IP address, not a host name"),
        ("https://[2606:4700::6810:84e5]/avatar/0123", "an IP address, not a host name"),
        ("", "no picture URL"),
        (None, "no picture URL"),
        (48, "no picture URL"),
    ],
)
def test_any_other_url_is_refused_before_anything_is_sent(url: Any, reason: str) -> None:
    with pytest.raises(PictureError) as caught:
        check_url(url)
    assert caught.value.reason == reason
    hosts = Hosts()
    assert refused(hosts, url) == reason and hosts.requests == []


@pytest.mark.parametrize(
    ("data", "kind"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (b"GIF87a" + b"\x00" * 8, "image/gif"), (WEBP, "image/webp")],
)
def test_the_four_kinds_are_known_by_their_bytes_whatever_the_server_says(data: bytes, kind: str) -> None:
    assert sniff(data) == kind
    for said in ("image/png", "application/octet-stream", "text/html", "image/svg+xml"):
        picture = fetch(Hosts({STORED: ok(data, said)}))
        assert (picture.data, picture.content_type, picture.version) == (data, kind, version_of(data))
    assert version_of(data) == hashlib.sha256(data).hexdigest()[:12]


@pytest.mark.parametrize(
    "data",
    [
        SVG,
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"\xef\xbb\xbf<svg/>",
        b"<!DOCTYPE html><html><body>Sign in</body></html>",
        b"",
        b"\x89PNG",  # a signature cut short
        b"GIF88a" + b"\x00" * 8,
        b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 8,  # RIFF, but sound
        b"BM" + b"\x00" * 30,  # a bitmap
        b"\x00\x00\x01\x00" + b"\x00" * 30,  # an icon
        b"%PDF-1.7",
    ],
)
def test_svg_and_everything_else_are_refused(data: bytes) -> None:
    assert sniff(data) is None
    for said in ("image/png", "image/svg+xml"):
        assert refused(Hosts({STORED: ok(data, said)})) == "not a PNG, JPEG, GIF or WebP picture"


def test_a_picture_of_exactly_256_kb_is_kept() -> None:
    data = PNG + b"\x00" * (MAX_BYTES - len(PNG))
    assert len(fetch(Hosts({STORED: ok(data)})).data) == MAX_BYTES == 256 * 1024


def test_a_bigger_one_is_refused_and_reading_stops_past_the_limit() -> None:
    pulled: list[int] = []

    def endless(_request: httpx.Request) -> httpx.Response:
        def chunks() -> Iterator[bytes]:
            yield PNG
            while True:  # a server that never stops: no Content-Length, and no end
                pulled.append(1)
                yield b"\x00" * 65536

        return httpx.Response(200, content=chunks(), headers={"Content-Type": "image/png"})

    assert refused(Hosts({STORED: endless})) == "larger than 256 KB"
    assert len(pulled) == 4  # 32 bytes and four 64 KB chunks pass 256 KB: no fifth chunk was read


def test_a_declared_length_over_the_limit_is_refused_without_reading_the_body() -> None:
    pulled: list[int] = []

    def declared(_request: httpx.Request) -> httpx.Response:
        def chunks() -> Iterator[bytes]:
            pulled.append(1)
            yield PNG

        return httpx.Response(200, content=chunks(), headers={"Content-Length": str(MAX_BYTES + 1)})

    assert refused(Hosts({STORED: declared})) == "larger than 256 KB" and pulled == []


def test_a_body_longer_than_its_declared_length_still_stops_at_the_limit() -> None:
    def lying(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter([PNG, b"\x00" * MAX_BYTES]), headers={"Content-Length": "40"})

    assert refused(Hosts({STORED: lying})) == "larger than 256 KB"


@pytest.mark.parametrize("declared", [b"\xb2", b"1e9", b"-1", b"40, 40", b""])  # \xb2: a superscript two, as sent
def test_a_declared_length_that_is_not_a_plain_number_is_ignored_and_the_count_decides(declared: bytes) -> None:
    def odd(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter([PNG]), headers={"Content-Length": declared})

    assert fetch(Hosts({STORED: odd})).data == PNG

    def odd_and_big(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter([PNG, b"\x00" * MAX_BYTES]), headers={"Content-Length": declared})

    assert refused(Hosts({STORED: odd_and_big})) == "larger than 256 KB"


def test_a_compressed_body_is_refused() -> None:
    for coding in ("gzip", "br", "deflate", "gzip, identity"):
        def coded(_request: httpx.Request, coding: str = coding) -> httpx.Response:
            return httpx.Response(200, content=iter([PNG]), headers={"Content-Encoding": coding})

        assert refused(Hosts({STORED: coded})) == "compressed body", coding
    assert fetch(Hosts({STORED: ok(PNG, "image/png", **{"Content-Encoding": "identity"})})).data == PNG


def test_gravatar_sends_someone_without_one_on_to_their_atlassian_initials() -> None:
    hosts = Hosts({ON_GRAVATAR: moved(INITIALS), INITIALS: ok(PNG)})
    assert fetch(hosts, ON_GRAVATAR).data == PNG
    assert hosts.asked() == [str(httpx.URL(ON_GRAVATAR)), INITIALS]


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_every_redirect_status_is_followed(code: int) -> None:
    assert fetch(Hosts({STORED: moved(INITIALS, code), INITIALS: ok(GIF)})).data == GIF


def test_a_relative_redirect_stays_on_its_host() -> None:
    hosts = Hosts({STORED: moved("/initials/DO-4.png"), INITIALS: ok(PNG)})
    assert fetch(hosts).data == PNG and hosts.asked()[-1] == INITIALS


def test_three_redirects_are_followed_and_a_fourth_is_not() -> None:
    hops = [f"{ATLASSIAN}/hop/{n}" for n in range(5)]
    three = Hosts({hops[0]: moved(hops[1]), hops[1]: moved(hops[2]), hops[2]: moved(hops[3]), hops[3]: ok(PNG)})
    assert fetch(three, hops[0]).data == PNG and len(three.requests) == 4
    four = Hosts({hops[0]: moved(hops[1]), hops[1]: moved(hops[2]), hops[2]: moved(hops[3]), hops[3]: moved(hops[4]),
                  hops[4]: ok(PNG)})
    assert refused(four, hops[0]) == "more than 3 redirects"
    assert four.asked() == hops[:4]  # the fourth redirect's target is never asked


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("https://evil.example/avatar.png", "redirected: not a picture host"),
        ("http://avatar-management--avatars.us-west-2.prod.public.atl-paas.net/initials/DO-4.png", "redirected: not https"),
        ("https://user:pw@avatar-management--avatars.us-west-2.prod.public.atl-paas.net/i.png",
         "redirected: has a user name or password"),
        ("https://secure.gravatar.com:8443/avatar/0123", "redirected: names a port"),
        ("https://169.254.169.254/latest/meta-data/", "redirected: an IP address, not a host name"),
        ("https://[::1]/admin", "redirected: an IP address, not a host name"),
        ("//evil.example/avatar.png", "redirected: not a picture host"),  # scheme-relative
        ("https://secure.gravatar.com.evil.example/avatar.png", "redirected: not a picture host"),
    ],
)
def test_a_redirect_to_a_url_the_rules_refuse_is_never_followed(location: str, reason: str) -> None:
    for start in (NO_DEFAULT, STORED):
        hosts = Hosts({start: moved(location)})
        assert refused(hosts, start) == reason
        assert len(hosts.requests) == 1  # only the first URL was asked


def test_gravatar_s_image_proxy_is_never_asked_and_the_default_picture_is_fetched_instead() -> None:
    hosts = Hosts({WITH_DEFAULT: moved(PROXY), INITIALS: ok(PNG)})
    picture = fetch(hosts, WITH_DEFAULT)
    assert picture.data == PNG and picture.content_type == "image/png"
    assert hosts.asked() == [str(httpx.URL(WITH_DEFAULT)), INITIALS]  # never the proxy
    long_name = WITH_DEFAULT.replace("?d=", "?s=48&default=")
    hosts = Hosts({long_name: moved("https://evil.example/x.png"), INITIALS: ok(GIF)})
    assert fetch(hosts, long_name).data == GIF and hosts.asked()[-1] == INITIALS


@pytest.mark.parametrize(
    "default",
    [
        "identicon",  # one of Gravatar's own patterns: not a URL
        "http://avatar-management--avatars.us-west-2.prod.public.atl-paas.net/initials/DO-4.png",
        "https://evil.example/initials/DO-4.png",
        "https://169.254.169.254/latest/meta-data/",
        "https://user:pw@avatar-management--avatars.us-west-2.prod.public.atl-paas.net/initials/DO-4.png",
    ],
)
def test_a_default_picture_the_rules_refuse_is_never_asked_either(default: str) -> None:
    start = f"{NO_DEFAULT}?d={quote(default, safe='')}"
    hosts = Hosts({start: moved(PROXY)})
    assert refused(hosts, start) == "redirected: not a picture host"
    assert len(hosts.requests) == 1


def test_the_default_picture_is_tried_once_and_held_to_the_same_rules() -> None:
    hosts = Hosts({WITH_DEFAULT: moved(PROXY), INITIALS: moved(PROXY)})
    assert refused(hosts, WITH_DEFAULT) == "redirected: not a picture host"
    assert hosts.asked() == [str(httpx.URL(WITH_DEFAULT)), INITIALS]
    assert refused(Hosts({WITH_DEFAULT: moved(PROXY), INITIALS: ok(SVG, "image/svg+xml")}), WITH_DEFAULT) == (
        "not a PNG, JPEG, GIF or WebP picture")
    assert refused(Hosts({WITH_DEFAULT: moved(PROXY)}), WITH_DEFAULT) == "HTTP 404"


def test_only_gravatar_s_own_redirect_falls_back_to_the_default() -> None:
    hosts = Hosts({WITH_DEFAULT: moved(f"{ATLASSIAN}/elsewhere"), f"{ATLASSIAN}/elsewhere": moved(PROXY), INITIALS: ok(PNG)})
    assert refused(hosts, WITH_DEFAULT) == "redirected: not a picture host"
    assert INITIALS not in hosts.asked()


def test_a_chain_that_wanders_off_the_two_hosts_on_its_third_hop_is_refused_there() -> None:
    hosts = Hosts({ON_GRAVATAR: moved(INITIALS), INITIALS: moved(f"{ATLASSIAN}/elsewhere"),
                   f"{ATLASSIAN}/elsewhere": moved("https://evil.example/x.png")})
    assert refused(hosts, ON_GRAVATAR) == "redirected: not a picture host"
    assert [request.url.host for request in hosts.requests] == [
        "secure.gravatar.com", "avatar-management--avatars.us-west-2.prod.public.atl-paas.net",
        "avatar-management--avatars.us-west-2.prod.public.atl-paas.net"]


def test_a_redirect_without_a_location_is_refused() -> None:
    assert refused(Hosts({STORED: lambda _r: httpx.Response(302)})) == "HTTP 302 without a Location"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (status(404), "HTTP 404"),
        (status(500), "HTTP 500"),
        (status(204), "HTTP 204"),
        (status(304), "HTTP 304"),
        (status(429), "HTTP 429"),
    ],
)
def test_an_error_status_is_a_failure(answer: Answer, reason: str) -> None:
    assert refused(Hosts({STORED: answer})) == reason


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ConnectTimeout, "timed out"),
        (httpx.ReadTimeout, "timed out"),
        (httpx.PoolTimeout, "timed out"),
        (httpx.ConnectError, "failed (ConnectError)"),
        (httpx.RemoteProtocolError, "failed (RemoteProtocolError)"),
    ],
)
def test_a_timeout_or_a_broken_connection_is_a_failure_that_names_no_url(error: type[Exception], reason: str) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise error(f"while fetching {request.url}", request=request)  # type: ignore[call-arg]

    assert refused(Hosts({STORED: broken})) == reason
    assert "atl-paas" not in reason and "http" not in reason.replace("HTTP", "")


def test_the_five_seconds_cover_the_whole_picture_redirects_included() -> None:
    clock = Clock()
    seen: list[dict[str, float]] = []

    def slow_redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        clock.now += 3.0
        return httpx.Response(302, headers={"Location": INITIALS})

    def initials(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        clock.now += 3.0
        return httpx.Response(302, headers={"Location": STORED})

    hosts = Hosts({ON_GRAVATAR: slow_redirect, INITIALS: initials, STORED: ok(PNG)})
    assert refused(hosts, ON_GRAVATAR, clock=clock) == "took longer than 5 s"
    assert len(hosts.requests) == 2  # 6 seconds gone: the third is never sent
    assert seen == [dict.fromkeys(("connect", "read", "write", "pool"), 5.0),
                    dict.fromkeys(("connect", "read", "write", "pool"), 2.0)]


def test_a_picture_that_trickles_in_past_five_seconds_is_refused() -> None:
    clock = Clock()

    def trickle(_request: httpx.Request) -> httpx.Response:
        def chunks() -> Iterator[bytes]:
            for piece in (PNG, b"\x00" * 10, b"\x00" * 10, b"\x00" * 10):
                clock.now += 2.0
                yield piece

        return httpx.Response(200, content=chunks())

    assert refused(Hosts({STORED: trickle}), clock=clock) == "took longer than 5 s"


def test_a_picture_request_carries_no_credential_and_keeps_no_cookie() -> None:
    hosts = Hosts({
        ON_GRAVATAR: moved(INITIALS, **{"Set-Cookie": "gravatar=1; Domain=.gravatar.com; Path=/"}),
        INITIALS: moved(STORED, **{"Set-Cookie": "session=abc; Path=/"}),
        STORED: ok(PNG, "image/png", **{"Set-Cookie": "seen=1; Path=/"}),
    })
    with fetcher(hosts) as pictures:
        pictures.fetch(ON_GRAVATAR)
        pictures.fetch(STORED)  # again, after the hosts set their cookies
    assert len(hosts.requests) == 4
    for request in hosts.requests:
        assert {name.lower() for name in request.headers} == {"accept", "accept-encoding", "user-agent", "host"}
        assert request.headers["accept-encoding"] == "identity"
        assert TOKEN not in str(request.url) + json.dumps(dict(request.headers))
        assert request.url.userinfo == b""


def test_a_fetcher_without_a_stub_never_reaches_the_internet_in_tests(_no_picture_from_the_internet: list[str]) -> None:
    with PictureFetcher() as pictures, pytest.raises(PictureError, match="ConnectError"):
        pictures.fetch(STORED)
    assert _no_picture_from_the_internet == ["avatar-management--avatars.us-west-2.prod.public.atl-paas.net"]
    _no_picture_from_the_internet.clear()  # asked on purpose: the net held


def board(store: Store, project: str, *accounts: str) -> None:
    """One ticket on the board for each account."""
    with store.transaction():
        for n, account in enumerate(accounts, 1):
            key = f"{project}-{n}"
            store.upsert("issues", {"issue_key": key, "issue_id": f"{project}{n}", "project_key": project,
                                    "status": "To Do", "assignee_id": account})


def person_row(store: Store, account: str, url: str | None, **picture: Any) -> None:
    with store.transaction():
        store.upsert("people", {"account_id": account, "display_name": account.title(), "active": 1,
                                "updated_at": to_iso(NOW), "avatar_url": url, **picture})


def stored_picture(store: Store, account: str) -> dict[str, Any]:
    row = store.query_one("SELECT avatar, avatar_type, avatar_hash, avatar_source, avatar_fetched_at"
                          " FROM people WHERE account_id = ?", (account,))
    return dict(row) if row is not None else {}


def url_of(account: str) -> str:
    return f"{ATLASSIAN}/{account}/5e5e/48"


def refresh(store: Store, hosts: Hosts, *projects: str, now: datetime = NOW, **options: Any) -> dict[str, int]:
    with fetcher(hosts) as pictures:
        return refresh_pictures(store, pictures, projects=list(projects), now=lambda: now, **options).as_dict()


def test_a_picture_is_fetched_when_none_is_stored_and_kept_with_its_type_hash_source_and_time(store: Store) -> None:
    board(store, "AIL", "acc-a", "acc-b", "acc-c")
    person_row(store, "acc-a", url_of("acc-a"))
    person_row(store, "acc-b", url_of("acc-b"))
    person_row(store, "acc-c", None)  # Jira gave no picture URL: nothing to fetch, initials
    hosts = Hosts({url_of("acc-a"): ok(PNG), url_of("acc-b"): ok(WEBP, "image/webp")})

    assert refresh(store, hosts, "AIL") == {"fetched": 2, "unchanged": 0, "failed": 0}
    assert stored_picture(store, "acc-a") == {"avatar": PNG, "avatar_type": "image/png", "avatar_hash": version_of(PNG),
                                              "avatar_source": url_of("acc-a"), "avatar_fetched_at": to_iso(NOW)}
    assert stored_picture(store, "acc-b")["avatar_type"] == "image/webp"
    assert stored_picture(store, "acc-c")["avatar"] is None
    assert sorted(hosts.asked()) == [url_of("acc-a"), url_of("acc-b")]


def test_a_picture_is_due_only_when_missing_changed_or_a_week_old(store: Store) -> None:
    board(store, "AIL", "acc-a")
    person_row(store, "acc-a", url_of("acc-a"))
    hosts = Hosts({url_of("acc-a"): ok(PNG), url_of("acc-a") + "/new": ok(JPEG, "image/jpeg")})
    refresh(store, hosts, "AIL")

    later = NOW + timedelta(days=6, hours=23)
    assert refresh(store, hosts, "AIL", now=later) == {"fetched": 0, "unchanged": 0, "failed": 0}  # fresh
    assert len(hosts.requests) == 1

    week = NOW + timedelta(days=7, minutes=1)  # older than 7 days: fetched again, the same bytes
    assert refresh(store, hosts, "AIL", now=week) == {"fetched": 0, "unchanged": 1, "failed": 0}
    assert stored_picture(store, "acc-a")["avatar_fetched_at"] == to_iso(week)
    assert stored_picture(store, "acc-a")["avatar_hash"] == version_of(PNG)

    person_row(store, "acc-a", url_of("acc-a") + "/new")  # a new picture in Jira: a new URL, due at once
    assert refresh(store, hosts, "AIL", now=week) == {"fetched": 1, "unchanged": 0, "failed": 0}
    assert stored_picture(store, "acc-a") == {
        "avatar": JPEG, "avatar_type": "image/jpeg", "avatar_hash": version_of(JPEG),
        "avatar_source": url_of("acc-a") + "/new", "avatar_fetched_at": to_iso(week)}


def test_a_failure_keeps_the_stored_picture_and_logs_the_person_and_reason_never_the_url(
    store: Store, logs: list[dict[str, Any]]
) -> None:
    board(store, "AIL", "acc-a", "acc-b")
    person_row(store, "acc-a", url_of("acc-a"))
    person_row(store, "acc-b", "https://evil.example/acc-b.png")
    refresh(store, Hosts({url_of("acc-a"): ok(PNG)}), "AIL")
    kept = stored_picture(store, "acc-a")

    person_row(store, "acc-a", url_of("acc-a") + "/new")
    failing = Hosts({url_of("acc-a") + "/new": status(503)})
    assert refresh(store, failing, "AIL") == {"fetched": 0, "unchanged": 0, "failed": 2}
    assert stored_picture(store, "acc-a") == kept  # the old picture, source and time stay
    assert stored_picture(store, "acc-b")["avatar"] is None
    assert failing.asked() == [url_of("acc-a") + "/new"]  # the other host was never asked

    warnings = [event for event in logs if event["event"] == "picture_not_fetched"]
    assert {(event["person"], event["reason"]) for event in warnings} >= {
        (person_id("acc-a"), "HTTP 503"), (person_id("acc-b"), "not a picture host")}
    assert all(set(event) == {"event", "log_level", "logger", "person", "reason"} for event in warnings)
    assert all(event["log_level"] == "warning" for event in warnings)
    text = json.dumps(logs, default=str)
    assert "atl-paas" not in text and "evil.example" not in text and "acc-a/" not in text
    assert "acc-a" not in text  # the person id, not the account id


def test_only_the_synced_boards_people_and_at_most_the_limit_missing_ones_first(store: Store) -> None:
    board(store, "AIL", *(f"acc-{n}" for n in range(5)))
    board(store, "OPS", "acc-ops")
    for n in range(5):
        person_row(store, f"acc-{n}", url_of(f"acc-{n}"))
    person_row(store, "acc-ops", url_of("acc-ops"))
    person_row(store, "acc-gone", url_of("acc-gone"))  # no ticket on any board now
    hosts = Hosts({url_of(f"acc-{n}"): ok(PNG) for n in range(5)} | {url_of("acc-ops"): ok(PNG)})
    refresh(store, Hosts({url_of("acc-0"): ok(PNG), url_of("acc-1"): ok(PNG)}), "AIL")  # two have pictures, from NOW

    later = NOW + timedelta(days=8)  # all five due now: three with none, two a week old
    assert [row["account_id"] for row in due(store, ["AIL"], later)] == ["acc-2", "acc-3", "acc-4", "acc-0", "acc-1"]
    assert refresh(store, hosts, "AIL", now=later, limit=3) == {"fetched": 3, "unchanged": 0, "failed": 0}
    assert sorted(hosts.asked()) == [url_of("acc-2"), url_of("acc-3"), url_of("acc-4")]  # the missing ones first
    assert MAX_PER_RUN == 200
    assert all(url_of(other) not in hosts.asked() for other in ("acc-ops", "acc-gone"))


def members(store: Store, project: str, *accounts: str) -> None:
    with store.transaction():
        for account in accounts:
            store.upsert("board_members", {"project_key": project, "account_id": account, "seen_at": to_iso(NOW)})


def test_a_boards_members_pictures_are_due_as_its_assignees_are(store: Store) -> None:
    board(store, "AIL", "acc-a")
    members(store, "AIL", "acc-a", "acc-m")  # acc-m holds no ticket: a member only
    members(store, "OPS", "acc-o")  # another board's member
    for account in ("acc-a", "acc-m", "acc-o", "acc-gone"):
        person_row(store, account, url_of(account))
    assert sorted(row["account_id"] for row in due(store, ["AIL"], NOW)) == ["acc-a", "acc-m"]
    assert sorted(row["account_id"] for row in due(store, ["AIL", "OPS"], NOW)) == ["acc-a", "acc-m", "acc-o"]
    hosts = Hosts({url_of("acc-a"): ok(PNG), url_of("acc-m"): ok(GIF, "image/gif")})
    assert refresh(store, hosts, "AIL") == {"fetched": 2, "unchanged": 0, "failed": 0}
    assert stored_picture(store, "acc-m")["avatar_type"] == "image/gif"
    assert url_of("acc-o") not in hosts.asked() and url_of("acc-gone") not in hosts.asked()
    assert due(store, ["AIL"], NOW) == []  # fresh now; the limit and the week stand as for anyone


def test_a_run_spends_at_most_its_budget_and_leaves_the_rest_for_the_next(
    store: Store, logs: list[dict[str, Any]]
) -> None:
    board(store, "AIL", *(f"acc-{n}" for n in range(4)))
    for n in range(4):
        person_row(store, f"acc-{n}", url_of(f"acc-{n}"))
    clock = Clock()

    def slow(_request: httpx.Request) -> httpx.Response:
        clock.now += 4.0  # each picture takes 4 s of a 10 s budget
        return httpx.Response(200, content=PNG)

    hosts = Hosts({url_of(f"acc-{n}"): slow for n in range(4)})
    assert refresh(store, hosts, "AIL", budget_s=10.0, clock=clock) == {"fetched": 3, "unchanged": 0, "failed": 0}
    assert len(hosts.requests) == 3
    assert [event["waiting"] for event in logs if event["event"] == "pictures_deferred"] == [1]
    assert refresh(store, Hosts({url_of("acc-3"): ok(PNG)}), "AIL") == {"fetched": 1, "unchanged": 0, "failed": 0}


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("no answer", request=request)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (status(500), "HTTP 500"),
        (timeout, "timed out"),
        (ok(SVG, "image/svg+xml"), "not a PNG, JPEG, GIF or WebP picture"),
        (ok(PNG + b"\x00" * MAX_BYTES), "larger than 256 KB"),
        (moved("https://evil.example/x.png"), "redirected: not a picture host"),
    ],
)
def test_whatever_goes_wrong_the_stored_picture_stays(
    store: Store, logs: list[dict[str, Any]], answer: Answer, reason: str
) -> None:
    board(store, "AIL", "acc-a")
    person_row(store, "acc-a", url_of("acc-a"))
    refresh(store, Hosts({url_of("acc-a"): ok(PNG)}), "AIL")
    kept = stored_picture(store, "acc-a")

    later = NOW + timedelta(days=8)  # due again
    assert refresh(store, Hosts({url_of("acc-a"): answer}), "AIL", now=later) == {"fetched": 0, "unchanged": 0, "failed": 1}
    assert stored_picture(store, "acc-a") == kept
    assert [(event["person"], event["reason"]) for event in logs if event["event"] == "picture_not_fetched"] == [
        (person_id("acc-a"), reason)]


def test_something_unforeseen_with_one_picture_does_not_stop_the_others(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    board(store, "AIL", "acc-a", "acc-b")
    person_row(store, "acc-a", url_of("acc-a"))
    person_row(store, "acc-b", url_of("acc-b"))
    real = PictureFetcher.fetch

    def flaky(self: PictureFetcher, url: object) -> avatars.Picture:
        if url == url_of("acc-a"):
            raise RuntimeError("a bug")
        return real(self, url)

    monkeypatch.setattr(PictureFetcher, "fetch", flaky)
    assert refresh(store, Hosts({url_of("acc-b"): ok(PNG)}), "AIL") == {"fetched": 1, "unchanged": 0, "failed": 1}


def carrying_pictures(fake: FakeJira, monkeypatch: pytest.MonkeyPatch, urls: dict[str, str]) -> None:
    payload = fake.payload

    def with_pictures(issue: Any, fields: list[str] | None = None) -> dict[str, Any]:
        body = payload(issue, fields)
        assignee = body["fields"].get("assignee")
        if assignee and assignee["accountId"] in urls:
            url = urls[assignee["accountId"]]
            assignee["avatarUrls"] = {"16x16": url + "/16", "24x24": url + "/24", "32x32": url + "/32", "48x48": url}
        return body

    monkeypatch.setattr(fake, "payload", with_pictures)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeJira:
    """AIL and OPS (tests/fakes/builders.py), every assignee with a picture URL on Atlassian's store."""
    jira = ail_and_ops()
    carrying_pictures(jira, monkeypatch, {"acc-dev-1": url_of("acc-dev-1"), "acc-dev-2": url_of("acc-dev-2")})
    return jira


def sync(fake: FakeJira, store: Store, fields: FieldsConfig, hosts: Hosts, mode: str = "full",
         projects: tuple[str, ...] = ("AIL",)) -> SyncResult:
    with fetcher(hosts) as pictures:
        return run_sync(fake.client(), store, fields, projects=list(projects), mode=mode, now=lambda: fake.clock,
                        pictures=pictures)


def test_a_sync_keeps_the_picture_url_and_then_fetches_the_picture(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    hosts = Hosts({url_of("acc-dev-1"): ok(PNG)})
    result = sync(fake, store, fields, hosts)

    assert store.query_one("SELECT avatar_url FROM people WHERE account_id = 'acc-dev-1'")[0] == url_of("acc-dev-1")
    assert stored_picture(store, "acc-dev-1")["avatar_hash"] == version_of(PNG)
    assert result.pictures.as_dict() == {"fetched": 1, "unchanged": 0, "failed": 0}
    assert result.rows_written == 137  # the snapshot as before: pictures are counted apart
    run = json.loads(store.query_one("SELECT stats_json FROM runs WHERE run_id = ?", (result.run_id,))[0])
    assert run["pictures"] == {"failed": 0, "fetched": 1, "unchanged": 0, "failures": []}
    assert "  pictures: 1 fetched, 0 unchanged, 0 failed\n  requests:" in result.render()
    assert all(request.url.host == "fake.atlassian.net" for request in fake.requests)
    assert all("authorization" not in request.headers for request in hosts.requests)


def test_every_sync_brings_the_pictures_up_to_date(
    fake: FakeJira, store: Store, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = {"acc-dev-1": url_of("acc-dev-1")}
    carrying_pictures(fake, monkeypatch, urls)
    hosts = Hosts({url_of("acc-dev-1"): ok(PNG), url_of("acc-dev-1") + "/v2": ok(GIF, "image/gif")})
    sync(fake, store, fields, hosts)

    fake.clock += timedelta(minutes=30)
    quiet = sync(fake, store, fields, hosts, "incremental")  # nothing new, and the picture is fresh
    assert quiet.pictures.as_dict() == {"fetched": 0, "unchanged": 0, "failed": 0} and len(hosts.requests) == 1

    fake.clock += timedelta(days=8)
    weekly = sync(fake, store, fields, hosts, "incremental")  # a week on: fetched again, the same picture
    assert weekly.pictures.as_dict() == {"fetched": 0, "unchanged": 1, "failed": 0}

    urls["acc-dev-1"] = url_of("acc-dev-1") + "/v2"  # a new picture in Jira ...
    fake.set_points("AIL-7", 5)  # ... seen when one of their tickets is read again
    fake.clock += timedelta(minutes=5)
    changed = sync(fake, store, fields, hosts, "incremental")
    assert changed.pictures.as_dict() == {"fetched": 1, "unchanged": 0, "failed": 0}
    assert stored_picture(store, "acc-dev-1")["avatar_type"] == "image/gif"


def test_a_picture_that_fails_never_fails_the_sync(
    fake: FakeJira, store: Store, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = sync(fake, store, fields, Hosts({url_of("acc-dev-1"): status(500)}))
    assert result.pictures.as_dict() == {"fetched": 0, "unchanged": 0, "failed": 1}
    assert store.query_one("SELECT status FROM runs WHERE run_id = ?", (result.run_id,))[0] == "succeeded"
    assert store.get_cursor("AIL") is not None and result.warnings == []
    run = json.loads(store.query_one("SELECT stats_json FROM runs WHERE run_id = ?", (result.run_id,))[0])
    assert run["pictures"] == {"fetched": 0, "unchanged": 0, "failed": 1,
                               "failures": [{"person": person_id("acc-dev-1"), "reason": "HTTP 500"}]}
    assert "acc-dev-1" not in json.dumps(run["pictures"]) and "atl-paas" not in json.dumps(run["pictures"])

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the picture step itself broke")

    monkeypatch.setattr(sync_module, "refresh_pictures", broken)
    fake.clock += timedelta(hours=1)
    again = sync(fake, store, fields, Hosts())
    assert store.query_one("SELECT status FROM runs WHERE run_id = ?", (again.run_id,))[0] == "succeeded"
    assert again.warnings == ["the people's pictures were not refreshed (RuntimeError); the stored ones stay"]
    assert again.pictures.as_dict() == {"fetched": 0, "unchanged": 0, "failed": 0}


def test_an_interrupted_picture_step_still_finishes_the_run_it_follows(
    fake: FakeJira, store: Store, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(sync_module, "refresh_pictures", interrupted)
    with pytest.raises(KeyboardInterrupt):
        sync(fake, store, fields, Hosts())
    run = store.query_one("SELECT status, finished_at FROM runs WHERE kind = 'sync'")
    assert run["status"] == "succeeded" and run["finished_at"] is not None
    assert store.get_cursor("AIL") is not None


def test_a_failed_sync_fetches_no_picture(fake: FakeJira, store: Store, fields: FieldsConfig) -> None:
    fake.fail("/rest/api/3/search/jql", status=400)
    hosts = Hosts({url_of("acc-dev-1"): ok(PNG)})
    with pytest.raises(Exception, match="HTTP 400"):
        sync(fake, store, fields, hosts)
    assert hosts.requests == []


def test_a_sync_fetches_only_its_own_boards_people(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    fake.assign("OPS-1", "acc-dev-2")  # someone with work on OPS only
    sync(fake, store, fields, Hosts(), projects=("AIL", "OPS"))  # both boards, no pictures to be had
    fake.clock += timedelta(hours=1)
    hosts = Hosts({url_of("acc-dev-1"): ok(PNG), url_of("acc-dev-2"): ok(PNG)})
    assert sync(fake, store, fields, hosts, "incremental").pictures.fetched == 1  # AIL alone
    assert hosts.asked() == [url_of("acc-dev-1")]


def test_a_sync_fetches_the_pictures_of_members_with_no_tickets(
    fake: FakeJira, store: Store, fields: FieldsConfig
) -> None:
    fake.add_member("AIL", "acc-m1", "Noor Haddad", avatarUrls={"48x48": url_of("acc-m1"), "16x16": url_of("acc-m1") + "/16"})
    fake.add_member("OPS", "acc-o1", "Yuki Tanabe", avatarUrls={"48x48": url_of("acc-o1")})
    hosts = Hosts({url_of("acc-dev-1"): ok(PNG), url_of("acc-m1"): ok(WEBP, "image/webp"), url_of("acc-o1"): ok(PNG)})
    result = sync(fake, store, fields, hosts)  # AIL alone

    assert result.pictures.as_dict() == {"fetched": 2, "unchanged": 0, "failed": 0}
    assert stored_picture(store, "acc-m1")["avatar_type"] == "image/webp"
    assert url_of("acc-o1") not in hosts.asked()  # OPS was not synced
    assert all("authorization" not in request.headers for request in hosts.requests)


def test_the_sync_command_that_sync_now_runs_fetches_the_pictures(
    fake: FakeJira, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hosts = Hosts({url_of("acc-dev-1"): ok(PNG)})
    made: list[PictureFetcher] = []

    def own_fetcher() -> PictureFetcher:
        made.append(fetcher(hosts))
        return made[-1]

    monkeypatch.setattr(sync_module, "PictureFetcher", own_fetcher)

    def client(_settings: Settings) -> JiraClient:
        return JiraClient(**fake.client_kwargs(), sleep=lambda _seconds: None)

    assert main(["sync", "--incremental"], settings=settings, client_factory=client) == EXIT_OK
    assert "pictures: 1 fetched, 0 unchanged, 0 failed" in capsys.readouterr().out
    assert len(made) == 1 and hosts.asked() == [url_of("acc-dev-1")]
    with Store.open(settings.db_file) as store:
        assert stored_picture(store, "acc-dev-1")["avatar"] == PNG
