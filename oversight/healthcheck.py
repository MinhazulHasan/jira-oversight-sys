"""The API container's HEALTHCHECK (Dockerfile): `python -m oversight.healthcheck`.

Asks this container's own API for GET /api/v1/health on 127.0.0.1:API_PORT, with the service key, and exits 0 when
it answers 200 with a status of ok or degraded, else 1. Degraded is healthy here: no snapshot yet, or a failed sync,
is a data problem the dashboard shows, not a process to restart. Prints one short line and never the key.

The request never goes through a proxy (HTTP_PROXY is often set in a corporate container, and 127.0.0.1 is not
always in NO_PROXY), and gives up after a few seconds, so a hung API is reported unhealthy instead of holding the
check open. Standard library only, besides the settings.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from oversight.settings import get_settings

# oversight/api/deps.py's header, not imported from there: the check runs every 30 seconds and should not load the
# web framework to send one request. tests/test_healthcheck.py keeps the two equal.
SERVICE_KEY_HEADER = "X-Service-Key"
TIMEOUT_S = 5.0
_MAX_BODY = 64 * 1024

Opener = Callable[..., Any]


def direct_opener() -> Opener:
    """urlopen without any proxy, whatever the environment says."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def check(url: str, key: str, *, opener: Opener | None = None, timeout: float = TIMEOUT_S) -> tuple[bool, str]:
    """(healthy, what to print) for one GET of the API's health."""
    request = urllib.request.Request(url, headers={SERVICE_KEY_HEADER: key, "Accept": "application/json"})
    try:
        with (opener or direct_opener())(request, timeout=timeout) as response:
            code = response.status
            body = json.loads(response.read(_MAX_BODY) or b"null")
    except urllib.error.HTTPError as exc:
        return False, f"unhealthy: HTTP {exc.code}"
    except (OSError, ValueError) as exc:  # refused, timed out (URLError and TimeoutError are OSErrors), not JSON
        return False, f"unhealthy: no usable answer ({type(exc).__name__})"
    status = body.get("status") if isinstance(body, dict) else None
    if code != 200 or status not in ("ok", "degraded"):
        return False, f"unhealthy: HTTP {code}, status {status!r}"
    return True, f"healthy: {status}"


def main(opener: Opener | None = None) -> int:
    try:
        settings = get_settings()
    except (ValidationError, UnicodeDecodeError):
        print("unhealthy: the settings are invalid (see the API's log)")
        return 1
    healthy, said = check(
        f"http://127.0.0.1:{settings.api_port}/api/v1/health",
        settings.api_service_key.get_secret_value(),
        opener=opener,
    )
    print(said)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
