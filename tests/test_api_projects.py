from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oversight.api.app import create_app
from oversight.config import ConfigError, FieldsConfig, ProcessConfig
from oversight.demo import seed_demo
from oversight.derive.run import run_derive
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import UTC
from tests.conftest import CONFIG_FIXTURES

KEY = "test-key"
NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


def make_settings(root: Path, projects: str) -> Settings:
    return Settings(
        _env_file=None,
        jira_projects=projects,
        api_service_key=KEY,
        db_path=root / "oversight.db",
        config_dir=CONFIG_FIXTURES,
        reports_dir=root / "reports",
    )


def make_client(settings: Settings) -> TestClient:
    app = create_app(settings, provider=ScriptedProvider(), now=lambda: NOW)
    return TestClient(app, headers={"X-Service-Key": KEY})


def seed(settings: Settings, boards: dict[str, int]) -> None:
    process = ProcessConfig.load(settings.process_file)
    rules = RulesConfig.load(settings.rules_file)
    fields = FieldsConfig.load(settings.fields_file)
    with Store.open(settings.db_file) as store:
        for project, issues in boards.items():
            seed_demo(store, now=NOW, seed=20261104, project=project, issues=issues)
        run_derive(store, process, projects=list(boards), now=lambda: NOW)
        run_rules(store, process, rules, fields, projects=list(boards), now=lambda: NOW)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    settings = make_settings(tmp_path_factory.mktemp("two_boards"), "DEMO,AIL")
    seed(settings, {"DEMO": 120, "AIL": 80})
    return make_client(settings)


def test_projects_lists_every_board_in_order_with_its_name_size_and_sync(client: TestClient) -> None:
    response = client.get("/api/v1/projects")
    assert response.status_code == 200
    body = response.json()
    assert body["default"] == "DEMO"
    assert [p["key"] for p in body["projects"]] == ["DEMO", "AIL"]
    demo, ail = body["projects"]
    assert demo["name"] == "Demo project"
    assert ail["name"] == "AIL"  # no name in process.yaml: the key
    assert demo["issues"] == 120 and ail["issues"] == 80
    assert demo["last_synced_at"] and ail["last_synced_at"]


def test_projects_is_the_same_whichever_board_is_picked(client: TestClient) -> None:
    assert client.get("/api/v1/projects", params={"project": "AIL"}).json() == client.get("/api/v1/projects").json()


@pytest.mark.parametrize("path", ["/health", "/overview", "/findings", "/rules", "/trends?metric=open_breaches"])
def test_without_a_pick_the_default_board_is_served(client: TestClient, path: str) -> None:
    body = client.get(f"/api/v1{path}").json()
    assert (body.get("meta") or body)["projects"] == ["DEMO"]


@pytest.mark.parametrize("path", ["/health", "/overview", "/findings", "/rules", "/trends?metric=open_breaches"])
def test_a_picked_board_is_served_instead(client: TestClient, path: str) -> None:
    sep = "&" if "?" in path else "?"
    body = client.get(f"/api/v1{path}{sep}project=AIL").json()
    assert (body.get("meta") or body)["projects"] == ["AIL"]


def test_findings_and_overview_read_only_the_picked_board(client: TestClient) -> None:
    for project in ("DEMO", "AIL"):
        page = client.get("/api/v1/findings", params={"project": project, "page_size": 100}).json()
        assert page["items"], f"{project} has findings"
        keys = [item["issue_key"] for item in page["items"] if item["issue_key"]]  # project-level findings have none
        assert keys and all(key.startswith(f"{project}-") for key in keys)
    demo = client.get("/api/v1/overview").json()["totals"]["issues_in_scope"]
    ail = client.get("/api/v1/overview", params={"project": "AIL"}).json()["totals"]["issues_in_scope"]
    assert demo != ail


def test_an_issue_is_found_only_on_its_own_board(client: TestClient) -> None:
    assert client.get("/api/v1/issues/AIL-1/timeline", params={"project": "AIL"}).status_code == 200
    assert client.get("/api/v1/issues/AIL-1/timeline").status_code == 404  # the default board is DEMO


def test_the_pick_is_case_insensitive(client: TestClient) -> None:
    assert client.get("/api/v1/overview", params={"project": "ail"}).json()["meta"]["projects"] == ["AIL"]


@pytest.mark.parametrize("project", ["XYZ", "", "AIL;DROP", "A" * 21])
def test_a_board_outside_jira_projects_is_refused(client: TestClient, project: str) -> None:
    response = client.get("/api/v1/overview", params={"project": project})
    assert response.status_code in (404, 422)
    assert "AIL" not in response.text or response.status_code == 422


def test_a_board_in_process_yaml_but_not_in_jira_projects_is_refused(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "AIL")  # DEMO is mapped in process.yaml, but not served
    seed(settings, {"AIL": 40})
    assert make_client(settings).get("/api/v1/overview", params={"project": "DEMO"}).status_code == 404


def test_ask_answers_about_the_picked_board(client: TestClient) -> None:
    response = client.post("/api/v1/ask", params={"project": "AIL"}, json={"question": "How many open breaches?"})
    assert response.status_code == 200
    assert response.json()["meta"]["projects"] == ["AIL"]


@pytest.mark.parametrize(
    "question",
    ["How many issues are in AIL?", "List open breaches in AIL", "Tell me about AIL-12"],
)
def test_ask_never_reads_another_board_named_in_the_question(client: TestClient, question: str) -> None:
    response = client.post("/api/v1/ask", params={"project": "DEMO"}, json={"question": question})
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["projects"] == ["DEMO"]
    assert body["outcome"] == "not_understood"
    assert body["facts"] == [] and body["answer"] is None and body["citations"] == []
    read = {name: value for name, value in body.items() if name not in ("question", "plan", "message")}
    assert "AIL" not in json.dumps(read)


def test_the_analysts_tools_never_read_another_boards_ticket(tmp_path: Path) -> None:
    from oversight.ask.analyst import QueryError, snapshot_tools

    settings = make_settings(tmp_path, "DEMO,AIL")
    seed(settings, {"DEMO": 6, "AIL": 6})
    with snapshot_tools(
        settings.db_file,
        project="DEMO",
        process=ProcessConfig.load(settings.process_file),
        rules=RulesConfig.load(settings.rules_file),
        fields=FieldsConfig.load(settings.fields_file),
        now=datetime(2026, 11, 4, 14, 30, tzinfo=UTC),
        row_limit=50,
        last_synced_at=None,
    ) as run:
        assert run("oversight_issue_timeline", {"issue_key": "DEMO-1"}).tool == "oversight_issue_timeline"
        with pytest.raises(QueryError, match="only project DEMO is in scope"):
            run("oversight_issue_timeline", {"issue_key": "AIL-1"})
        with pytest.raises(QueryError, match="only project DEMO is in scope"):
            run("oversight_count_issues", {"project": "AIL"})


def test_ask_says_which_board_is_in_scope_when_it_refuses(client: TestClient) -> None:
    body = client.post("/api/v1/ask", params={"project": "AIL"}, json={"question": "How many issues are in DEMO?"}).json()
    assert body["message"] == "This question is about DEMO, but only AIL is in scope here. Pick another board to ask about it."


def test_a_board_that_never_synced_is_listed_empty(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "DEMO,AIL")
    seed(settings, {"DEMO": 40})
    body = make_client(settings).get("/api/v1/projects").json()
    assert body["projects"][1] == {"key": "AIL", "name": "AIL", "issues": 0, "last_synced_at": None, "synced": False,
                                   "type": None}


def test_projects_without_a_snapshot_still_lists_the_boards(tmp_path: Path) -> None:
    body = make_client(make_settings(tmp_path, "AIL,DEMO")).get("/api/v1/projects").json()
    assert [(p["key"], p["issues"], p["last_synced_at"]) for p in body["projects"]] == [
        ("AIL", 0, None),
        ("DEMO", 0, None),
    ]


def test_a_board_process_yaml_leaves_out_never_stops_the_api_starting(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "AIL,XYZ")
    seed(settings, {"AIL": 20})
    client = make_client(settings)
    assert [board["key"] for board in client.get("/api/v1/projects").json()["projects"]] == ["AIL", "XYZ"]
    refused = client.get("/api/v1/overview", params={"project": "XYZ"})
    assert refused.status_code == 409 and refused.json() == {"detail": "XYZ hasn't been synced yet.",
                                                              "code": "board_not_synced"}
    with pytest.raises(ConfigError, match="XYZ"):  # process.yaml alone still does not map it
        ProcessConfig.load(settings.process_file).project("XYZ")


def test_projects_needs_the_service_key(client: TestClient) -> None:
    assert client.get("/api/v1/projects", headers={"X-Service-Key": "wrong"}).status_code == 401


def test_a_request_connection_works_from_the_route_thread(tmp_path: Path) -> None:
    import threading
    from dataclasses import replace

    from oversight.api.deps import read_conn

    settings = make_settings(tmp_path, "AIL")
    seed(settings, {"AIL": 20})
    dependency = read_conn(replace(make_client(settings).app.state.ctx))  # type: ignore[attr-defined]
    conn = next(dependency)
    errors: list[BaseException] = []

    def route() -> None:
        try:
            conn.execute("SELECT COUNT(*) FROM issues").fetchone()
        except BaseException as exc:  # noqa: BLE001 - the failure is the finding
            errors.append(exc)

    worker = threading.Thread(target=route)
    worker.start()
    worker.join()
    dependency.close()
    assert errors == []
