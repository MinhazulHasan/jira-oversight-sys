"""Doctor against the real Jira site: opt-in and read-only.

Runs only with `pytest -m live`, and only when JIRA_BASE_URL, JIRA_EMAIL and
JIRA_API_TOKEN are set in the environment (tests never read .env). It leaves
config/fields.yaml alone and uses a temporary database.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from oversight.doctor import run_doctor
from oversight.jira.client import ALLOWED, JiraClient, route_of
from oversight.settings import Settings
from oversight.store.db import Store

REQUIRED_ENV = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")


@pytest.mark.live
@pytest.mark.skipif(
    not all(os.environ.get(name, "").strip() for name in REQUIRED_ENV),
    reason="needs JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN in the environment",
)
def test_doctor_passes_against_the_real_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    fields_path = tmp_path / "fields.yaml"
    sent: list[tuple[str, str]] = []

    with JiraClient.from_settings(settings) as client, Store.open(tmp_path / "live.db") as store:
        send = client.request

        # Recorded above the transport, so proxies from the environment still apply.
        def recording(method: str, path: str, **options: Any) -> Any:
            sent.append((method.upper(), path))
            return send(method, path, **options)

        monkeypatch.setattr(client, "request", recording)
        report = run_doctor(
            client,
            store,
            projects=list(settings.jira_projects),
            process_path=settings.process_file,
            fields_path=fields_path,
            site=settings.jira_base_url,
            write_fields=False,
        )

    rendered = report.render()
    print(rendered)
    assert sent, "doctor made no Jira calls"
    assert all((method, route_of(path)) in ALLOWED for method, path in sent), sent
    assert report.fields_written_to is None
    assert not fields_path.exists()
    assert report.ok, rendered
