"""The API container's HEALTHCHECK, with a stand-in opener so nothing goes on the network."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from oversight import healthcheck
from oversight.healthcheck import check, direct_opener, main
from oversight.settings import Settings

KEY = "s" * 40


class Answer(io.BytesIO):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = status


class Opener:
    def __init__(self, answer: Answer | BaseException) -> None:
        self.answer = answer
        self.requests: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Answer:
        self.requests.append((request, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.mark.parametrize("status", ["ok", "degraded"])
def test_ok_and_degraded_are_healthy(status: str) -> None:
    opener = Opener(Answer(200, {"status": status, "message": None}))
    assert check("http://127.0.0.1:3300/api/v1/health", KEY, opener=opener) == (True, f"healthy: {status}")
    request, timeout = opener.requests[0]
    assert request.full_url == "http://127.0.0.1:3300/api/v1/health"
    assert request.get_header("X-service-key") == KEY
    assert timeout == healthcheck.TIMEOUT_S


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (urllib.error.HTTPError("u", 401, "Unauthorized", {}, None), "unhealthy: HTTP 401"),  # type: ignore[arg-type]
        (urllib.error.URLError(ConnectionRefusedError()), "unhealthy: no usable answer (URLError)"),
        (TimeoutError(), "unhealthy: no usable answer (TimeoutError)"),
        (Answer(200, b"<html>a proxy's page</html>"), "unhealthy: no usable answer (JSONDecodeError)"),
        (Answer(200, {"status": "starting"}), "unhealthy: HTTP 200, status 'starting'"),
        (Answer(204, b""), "unhealthy: HTTP 204, status None"),
    ],
)
def test_anything_else_is_unhealthy(answer: Answer | BaseException, said: str) -> None:
    assert check("http://127.0.0.1:3300/api/v1/health", KEY, opener=Opener(answer)) == (False, said)


def test_the_key_goes_in_the_api_s_own_header() -> None:
    from oversight.api.deps import SERVICE_KEY_HEADER

    assert healthcheck.SERVICE_KEY_HEADER == SERVICE_KEY_HEADER


def test_the_check_never_uses_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")

    def proxied(handlers: list[Any]) -> list[Any]:
        return [h for h in handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies]

    # urllib's default opener would send the request to the proxy; this one has no proxy at all.
    assert proxied(urllib.request.build_opener().handlers)
    assert proxied(direct_opener().__self__.handlers) == []  # type: ignore[attr-defined]


def test_main_asks_its_own_port_with_the_key_and_never_prints_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configured = Settings.model_validate({**settings.model_dump(), "api_service_key": KEY, "api_port": 3311})
    monkeypatch.setattr(healthcheck, "get_settings", lambda: configured)
    opener = Opener(Answer(200, {"status": "ok"}))
    assert main(opener) == 0
    assert opener.requests[0][0].full_url == "http://127.0.0.1:3311/api/v1/health"
    printed = capsys.readouterr()
    assert printed.out == "healthy: ok\n"
    assert KEY not in printed.out + printed.err
    assert main(Opener(urllib.error.HTTPError("u", 503, "x", {}, None))) == 1  # type: ignore[arg-type]


def test_invalid_settings_are_unhealthy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def broken() -> Settings:
        return Settings(access_log_days=1)

    monkeypatch.setattr(healthcheck, "get_settings", broken)
    assert main(Opener(Answer(200, {"status": "ok"}))) == 1
    assert capsys.readouterr().out == "unhealthy: the settings are invalid (see the API's log)\n"
