from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from structlog.testing import capture_logs

from oversight.api import boards as api_boards
from oversight.api.app import create_app
from oversight.api.boards import Discoverer, discovery_for
from oversight.api.routes.health import NEVER_SYNCED, SYNC_FAILED
from oversight.demo import seed_demo
from oversight.llm.scripted import ScriptedProvider
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import to_iso
from tests.test_api_projects import KEY, NOW, make_settings, seed


def discovered(settings: Settings, *boards: tuple[str, str, str], gone: tuple[str, ...] = ()) -> None:
    with Store.open(settings.db_file) as store:
        for key, name, kind in boards:
            store.upsert("boards", {"project_key": key, "name": name, "project_type": kind,
                                    "discovered_at": to_iso(NOW), "last_seen_at": to_iso(NOW),
                                    "disappeared_at": to_iso(NOW) if key in gone else None})


class Discovery:
    """A stand-in for discovery: counts its runs, can be held or made to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.runs = 0
        self.fail = fail
        self.release = threading.Event()
        self.release.set()

    def __call__(self) -> None:
        self.release.wait(5)
        self.runs += 1
        if self.fail:
            raise RuntimeError("Jira is down")


def client_of(settings: Settings, discoverer: Discoverer | None = None) -> TestClient:
    app = create_app(settings, provider=ScriptedProvider(), now=lambda: NOW, discoverer=discoverer)
    return TestClient(app, headers={"X-Service-Key": KEY})


@pytest.fixture(scope="module")
def boards(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    settings = make_settings(tmp_path_factory.mktemp("every_board"), "DEMO,AIL")
    seed(settings, {"DEMO": 60, "AIL": 40})
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=NOW, seed=20261104, project="OPS", issues=30)  # synced once, from the dashboard
    discovered(settings, ("AIL", "AILED", "software"), ("OPS", "Operations", "business"),
               ("ZED", "Alpha board", "service_desk"), ("GONE", "Gone board", "software"), gone=("GONE",))
    return settings


@pytest.fixture(scope="module")
def client(boards: Settings) -> TestClient:
    return client_of(boards)


def test_projects_lists_every_board_the_default_first_then_synced_then_not_synced_each_by_name(
    client: TestClient,
) -> None:
    body = client.get("/api/v1/projects").json()
    assert body["default"] == "DEMO"
    listed = {board["key"]: board for board in body["projects"]}
    assert list(listed) == ["DEMO", "AIL", "OPS", "ZED"]  # "AILED" before "Operations"; GONE left Jira
    assert listed["DEMO"] == {"key": "DEMO", "name": "Demo project", "issues": 60, "last_synced_at": to_iso(NOW),
                              "synced": True, "type": None}  # process.yaml's name; Jira's search does not list it
    assert (listed["AIL"]["name"], listed["AIL"]["type"]) == ("AILED", "software")  # process.yaml gives no name
    assert (listed["OPS"]["name"], listed["OPS"]["type"], listed["OPS"]["synced"]) == ("Operations", "business", True)
    assert listed["ZED"] == {"key": "ZED", "name": "Alpha board", "issues": 0, "last_synced_at": None, "synced": False,
                             "type": "service_desk"}


def test_a_discovered_board_is_served_with_a_mapping_from_its_status_categories(client: TestClient) -> None:
    overview = client.get("/api/v1/overview", params={"project": "OPS"})
    assert overview.status_code == 200 and overview.json()["meta"]["projects"] == ["OPS"]
    rules = client.get("/api/v1/rules", params={"project": "ops"}).json()
    assert rules["meta"]["projects"] == ["OPS"]
    findings = client.get("/api/v1/findings", params={"project": "OPS", "page_size": 100})
    assert findings.status_code == 200


@pytest.mark.parametrize("path", ["/overview", "/findings", "/rules", "/people", "/people/directory",
                                  "/trends?metric=open_breaches", "/insights/latest", "/baseline/latest",
                                  "/issues/ZED-1/timeline", "/rules/settings?level=people"])
def test_every_board_scoped_route_on_a_board_never_synced_is_board_not_synced(client: TestClient, path: str) -> None:
    sep = "&" if "?" in path else "?"
    response = client.get(f"/api/v1{path}{sep}project=ZED", headers={"X-Oversight-Viewer": "local",
                                                                      "X-Oversight-Viewer-Name": "Local"})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "Alpha board hasn't been synced yet.", "code": "board_not_synced"}


def test_ask_on_a_board_never_synced_is_board_not_synced(client: TestClient) -> None:
    response = client.post("/api/v1/ask", params={"project": "ZED"}, json={"question": "How many open breaches?"})
    assert (response.status_code, response.json()["code"]) == (409, "board_not_synced")


@pytest.mark.parametrize("project", ["GONE", "NOPE"])
def test_a_board_the_service_does_not_serve_stays_unknown(client: TestClient, project: str) -> None:
    response = client.get("/api/v1/overview", params={"project": project})
    assert (response.status_code, response.json()["code"]) == (404, "unknown_board")


def test_with_discovery_off_only_jira_projects_are_served(boards: Settings) -> None:
    client = client_of(boards.model_copy(update={"board_discovery": "off"}))
    assert [board["key"] for board in client.get("/api/v1/projects").json()["projects"]] == ["DEMO", "AIL"]
    assert client.get("/api/v1/overview", params={"project": "OPS"}).status_code == 404


def test_health_still_answers_for_a_board_never_synced(client: TestClient) -> None:
    body = client.get("/api/v1/health", params={"project": "ZED"})
    assert body.status_code == 200
    assert (body.json()["status"], body.json()["message"], body.json()["projects"]) == ("degraded", NEVER_SYNCED,
                                                                                        ["ZED"])


def test_health_reads_each_boards_own_syncs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "DEMO")
    seed(settings, {"DEMO": 30})
    discovered(settings, ("OPS", "Operations", "business"))
    with Store.open(settings.db_file) as store:
        seed_demo(store, now=NOW, seed=20261104, project="OPS", issues=10)
        run = store.start_run("sync", started_at=NOW + timedelta(minutes=5))
        store.finish_run(run, status="failed", stats={"projects": ["OPS"], "error_type": "JiraError"},
                         finished_at=NOW + timedelta(minutes=6))
    client = client_of(settings)
    assert client.get("/api/v1/health").json()["message"] is None
    assert client.get("/api/v1/health", params={"project": "OPS"}).json()["message"] == SYNC_FAILED


def test_the_portfolio_lists_the_synced_boards_and_counts_the_others(client: TestClient) -> None:
    body = client.get("/api/v1/portfolio").json()
    assert [board["project"]["key"] for board in body["boards"]] == ["DEMO", "AIL", "OPS"]
    assert body["boards_not_synced"] == 1  # ZED
    assert body["meta"]["projects"] == ["DEMO", "AIL", "ZED", "OPS"]  # JIRA_PROJECTS, then by name
    mine = client.get("/api/v1/portfolio", params=[("board", "AIL"), ("board", "ZED")]).json()
    assert ([board["project"]["key"] for board in mine["boards"]], mine["boards_not_synced"]) == (["AIL"], 1)
    assert client.get("/api/v1/portfolio", params={"project": "ZED"}).status_code == 200  # not board-scoped


def test_a_boards_mapping_is_worked_out_once_after_each_of_its_syncs(
    boards: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worked_out: list[str] = []
    real = api_boards.automatic_project

    def counted(*args: Any, **kwargs: Any) -> Any:
        worked_out.append(args[2])
        return real(*args, **kwargs)

    monkeypatch.setattr(api_boards, "automatic_project", counted)
    client = client_of(boards)
    for _ in range(3):
        assert client.get("/api/v1/overview", params={"project": "OPS"}).status_code == 200
    assert worked_out.count("OPS") == 1
    later = to_iso(NOW + timedelta(hours=1))
    with Store.open(boards.db_file) as store:
        before = store.get_cursor("OPS")
        store.set_cursor("OPS", last_synced_at=later, run_id="r2", account_timezone=None)
    try:
        client.get("/api/v1/overview", params={"project": "OPS"})
        assert worked_out.count("OPS") == 2  # its next sync: worked out again
    finally:
        with Store.open(boards.db_file) as store:
            assert before is not None and before.last_synced_at is not None
            store.set_cursor("OPS", last_synced_at=before.last_synced_at, run_id=before.last_run_id or "r1",
                             account_timezone=None)


def test_the_api_starts_a_discovery_when_it_starts_serving(boards: Settings) -> None:
    discovery = Discovery()
    discoverer = Discoverer(discovery, now=lambda: NOW)
    with client_of(boards, discoverer):  # the app's lifespan: what uvicorn runs
        discoverer.wait(5)
    assert discovery.runs == 1


def test_projects_starts_a_discovery_when_the_list_is_older_than_thirty_minutes_and_never_waits_for_it(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, "AIL")
    seed(settings, {"AIL": 10})
    discovery = Discovery()
    discovery.release.clear()  # held: the request must not wait for it
    clock = {"now": NOW}
    discoverer = Discoverer(discovery, now=lambda: clock["now"])
    client = client_of(settings, discoverer)

    assert client.get("/api/v1/projects").status_code == 200  # no discovery yet: one starts, in the background
    assert client.get("/api/v1/projects").status_code == 200  # one is running: no second
    discovery.release.set()
    discoverer.wait(5)
    assert discovery.runs == 1

    with Store.open(settings.db_file) as store:  # a discovery that worked, 10 minutes ago
        run = store.start_run("discover", started_at=NOW - timedelta(minutes=10))
        store.finish_run(run, status="succeeded", stats={}, finished_at=NOW - timedelta(minutes=10))
    clock["now"] = NOW + timedelta(minutes=10)
    client.get("/api/v1/projects")
    discoverer.wait(5)
    assert discovery.runs == 1  # fresh: nothing to do

    clock["now"] = NOW + timedelta(minutes=45)  # 55 minutes old
    client.get("/api/v1/projects")
    discoverer.wait(5)
    assert discovery.runs == 2


def test_a_failed_discovery_is_logged_and_not_asked_again_at_once(tmp_path: Path) -> None:
    clock = {"now": NOW}
    discovery = Discovery(fail=True)
    discoverer = Discoverer(discovery, now=lambda: clock["now"])
    with capture_logs() as events:
        assert discoverer.start()
        discoverer.wait(5)
    failed = [event for event in events if event["event"] == "board_discovery_failed"]
    assert [event["error_type"] for event in failed] == ["RuntimeError"]
    assert not discoverer.start()  # within DISCOVERY_RETRY_AFTER of the last attempt
    clock["now"] = NOW + api_boards.DISCOVERY_RETRY_AFTER
    assert discoverer.start()
    discoverer.wait(5)
    assert discovery.runs == 2


def test_discovery_never_runs_with_it_off_or_without_the_jira_settings(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "AIL")
    assert discovery_for(settings, settings.db_file, lambda: NOW) is None  # no Jira settings
    configured = settings.model_copy(update={"jira_base_url": "https://fake.atlassian.net",
                                             "jira_email": "sync@example.com", "jira_api_token": SecretStr("t")})
    assert discovery_for(configured.model_copy(update={"board_discovery": "off"}), settings.db_file,
                         lambda: NOW) is None
    assert callable(discovery_for(configured, settings.db_file, lambda: NOW))  # never called here: no Jira in tests
    off = Discoverer(None)
    assert not off.enabled and not off.start() and not off.refresh_if_stale(None)
