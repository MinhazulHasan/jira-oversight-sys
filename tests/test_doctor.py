"""Doctor (Phase 0): every check, its failure paths, the report and the CLI command.

Offline: an AIL-shaped fake Jira site answers the real JiraClient through
httpx.MockTransport, and retries sleep into a list instead of waiting.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
import yaml
from structlog.testing import capture_logs

from oversight.cli import EXIT_CHECKS_FAILED, EXIT_OK, main
from oversight.config import FIELDS_HEADER, FieldsConfig, ProcessConfig
from oversight.doctor import Check, DoctorReport, run_doctor
from oversight.errors import ConfigError
from oversight.jira.client import ALLOWED, JiraClient, route_of
from oversight.settings import Settings
from oversight.store.db import SCHEMA_VERSION, Store
from oversight.timeutil import UTC

from tests.conftest import CONFIG_FIXTURES, JIRA_FIXTURES

BASE_URL = "https://fake.atlassian.net"
EMAIL = "dev@example.com"
# Distinctive, so a leak check can't pass by accident.
TOKEN = "tok-doctor-8e21b4-do-not-leak"
NOW = datetime(2026, 11, 4, 9, 30, tzinfo=UTC)

MYSELF = "/rest/api/3/myself"
FIELD = "/rest/api/3/field"
SEARCH = "/rest/api/3/search/jql"
PROJECT_AIL = "/rest/api/3/project/AIL"
STATUSES_AIL = "/rest/api/3/project/AIL/statuses"

GREENHOPPER = "com.pyxis.greenhopper.jira"
PEOPLE = "com.atlassian.jira.plugin.system.customfieldtypes:people"
FLOAT = "com.atlassian.jira.plugin.system.customfieldtypes:float"
DEVSUMMARY = "com.atlassian.jira.plugins.jira-development-integration-plugin:devsummarycf"

CHECK_ORDER = [
    "jira auth",
    "process config",
    "project AIL visible",
    "statuses AIL mapped",
    "fields",
    "evidence",
    "database",
]
JIRA_CHECKS = ("project AIL visible", "statuses AIL mapped", "fields")
MISSING_EVIDENCE = ["code_review", "dev_signoff", "testing", "test_review", "test_signoff"]
EVIDENCE_PROBES = [
    f'project in ("AIL") AND cf[{number}] is not EMPTY' for number in (10138, 10139, 10140, 10141)
]


# --- the fake site ---------------------------------------------------------------------


def load_json(name: str) -> Any:
    return json.loads((JIRA_FIXTURES / name).read_text(encoding="utf-8"))


def jira_error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"errorMessages": [message], "errors": {}})


def status(name: str, status_id: str, category: str = "indeterminate") -> dict[str, Any]:
    """One status as GET project/{key}/statuses lists it."""
    category_ids = {"new": 2, "indeterminate": 4, "done": 3}
    return {
        "self": f"{BASE_URL}/rest/api/3/status/{status_id}",
        "description": "",
        "iconUrl": f"{BASE_URL}/",
        "name": name,
        "untranslatedName": name,
        "id": status_id,
        "statusCategory": {"id": category_ids[category], "key": category, "name": category},
    }


def custom_field(number: int, name: str, custom_type: str, schema_type: str = "array") -> dict[str, Any]:
    """One custom field as GET /field lists it."""
    field_id = f"customfield_{number}"
    return {
        "id": field_id,
        "key": field_id,
        "name": name,
        "untranslatedName": name,
        "custom": True,
        "orderable": True,
        "navigable": True,
        "searchable": True,
        "clauseNames": [f"cf[{number}]", name],
        "schema": {"type": schema_type, "custom": custom_type, "customId": number},
    }


def system_field(field_id: str, name: str, schema_type: str) -> dict[str, Any]:
    return {
        "id": field_id,
        "key": field_id,
        "name": name,
        "custom": False,
        "orderable": True,
        "navigable": True,
        "searchable": True,
        "clauseNames": [field_id],
        "schema": {"type": schema_type, "system": field_id},
    }


class FakeJiraSite:
    """An AIL-shaped Jira Cloud site (tests/fixtures/jira/SHAPES.md) for httpx.MockTransport.

    It checks Basic auth like Jira, answers only the reads doctor makes, and
    records every request. `fail()` makes one route answer with an error;
    `unsearchable` holds field ids that JQL rejects with HTTP 400.
    """

    _PROBE = re.compile(r"project in \((?P<keys>[^)]*)\) AND cf\[(?P<number>[0-9]+)\] is not EMPTY")

    def __init__(self, email: str = EMAIL, token: str = TOKEN) -> None:
        self.authorization = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        self.myself: dict[str, Any] = {
            "accountId": "acc-dev-1",
            "accountType": "atlassian",
            "active": True,
            "timeZone": "America/Chicago",
            "locale": "en_US",
        }
        self.projects: dict[str, dict[str, Any]] = {
            "AIL": {
                "id": "10023",
                "key": "AIL",
                "name": "AILED",
                "projectTypeKey": "software",
                "simplified": True,
                "style": "next-gen",
            }
        }
        # One entry per issue type, the same three statuses each time (documented shape).
        self.statuses: dict[str, list[dict[str, Any]]] = {
            "AIL": load_json("mapping_project_statuses.json")["response"]
        }
        # AIL's ids. No Epic Link: the project is team-managed.
        self.fields: list[dict[str, Any]] = [
            system_field("summary", "Summary", "string"),
            system_field("status", "Status", "status"),
            system_field("parent", "Parent", "issuelink"),
            system_field("timeoriginalestimate", "Original estimate", "number"),
            custom_field(10000, "Development", DEVSUMMARY, "any"),
            custom_field(10016, "Story point estimate", f"{GREENHOPPER}:jsw-story-points", "number"),
            custom_field(10019, "Rank", f"{GREENHOPPER}:gh-lexo-rank", "any"),
            custom_field(10020, "Sprint", f"{GREENHOPPER}:gh-sprint"),
            custom_field(10138, "Programmer", PEOPLE),
            custom_field(10139, "Code Reviewers", PEOPLE),
            custom_field(10140, "Testers", PEOPLE),
            custom_field(10141, "Test Reviewers", PEOPLE),
        ]
        # AIL-1140 has story points and a sprint but no evidence fields; as on the
        # real site, Programmer is set on a few issues and the rest are empty.
        sample = load_json("issue_sample.json")
        with_programmer = copy.deepcopy(sample)
        with_programmer.update(id="52510", key="AIL-1143")
        with_programmer["fields"]["customfield_10138"] = [
            {"accountId": "acc-dev-1", "accountType": "atlassian"}
        ]
        self.issues: list[dict[str, Any]] = [sample, with_programmer]
        self.unsearchable: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self._failures: dict[tuple[str, str], tuple[int, Any]] = {}

    def fail(self, method: str, path: str, status_code: int, body: Any = None) -> None:
        """From now on, answer `method path` with this status (and JSON body, if given)."""
        self._failures[(method, path)] = (status_code, body)

    def set_everywhere(self, field_id: str, value: Any) -> None:
        for issue in self.issues:
            issue["fields"][field_id] = value

    def client(self) -> JiraClient:
        return JiraClient(
            BASE_URL, EMAIL, TOKEN, transport=httpx.MockTransport(self), sleep=self.sleeps.append
        )

    def sent(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]

    def probes(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests if request.url.path == SEARCH]

    def probe_jql(self) -> list[str]:
        return [body["jql"] for body in self.probes()]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != self.authorization:
            return jira_error(401, "Client must be authenticated to access this resource.")
        failure = self._failures.get((request.method, request.url.path))
        if failure is not None:
            status_code, body = failure
            return httpx.Response(status_code) if body is None else httpx.Response(status_code, json=body)
        return self._answer(request)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "GET" and path == MYSELF:
            return httpx.Response(200, json=self.myself)
        if method == "GET" and path == FIELD:
            return httpx.Response(200, json=self.fields)
        if method == "POST" and path == SEARCH:
            return self._search(json.loads(request.content))
        project = re.fullmatch(r"/rest/api/3/project/([^/]+)(/statuses)?", path)
        if method == "GET" and project:
            key, statuses = project.groups()
            if key not in self.projects:
                return jira_error(404, f"No project could be found with key '{key}'.")
            return httpx.Response(200, json=self.statuses[key] if statuses else self.projects[key])
        return jira_error(404, f"the fake site has no {method} {path}")

    def _search(self, body: dict[str, Any]) -> httpx.Response:
        match = self._PROBE.fullmatch(body.get("jql", ""))
        if match is None:
            return jira_error(400, "the fake site only understands doctor's probe query")
        keys = re.findall(r'"([^"]+)"', match["keys"])
        for key in keys:
            if key not in self.projects:
                return jira_error(400, f"The value '{key}' does not exist for the field 'project'.")
        field_id = f"customfield_{match['number']}"
        if not keys or field_id in self.unsearchable or all(item["id"] != field_id for item in self.fields):
            return jira_error(
                400, f"Field 'cf[{match['number']}]' does not exist or you do not have permission to view it."
            )
        hits = [
            {"id": issue["id"]}
            for issue in self.issues
            if issue["fields"]["project"]["key"] in keys
            and issue["fields"].get(field_id) not in (None, [], "")
        ]
        size = body["maxResults"]
        page: dict[str, Any] = {"issues": hits[:size], "isLast": len(hits) <= size}
        if len(hits) > size:
            page["nextPageToken"] = "next-page"
        return httpx.Response(200, json=page)


# --- fixtures and helpers -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def logs() -> Iterator[list[dict[str, Any]]]:
    """Every structlog event of the test, as dicts; the configuration is restored afterwards,
    since the CLI tests reconfigure logging."""
    saved = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        with capture_logs() as captured:
            yield captured
    finally:
        structlog.configure(**saved)


@pytest.fixture
def site() -> FakeJiraSite:
    return FakeJiraSite()


@pytest.fixture
def client(site: FakeJiraSite) -> Iterator[JiraClient]:
    with site.client() as jira:
        yield jira


def doctor(client: JiraClient, store: Store, tmp_path: Path, **options: Any) -> DoctorReport:
    arguments: dict[str, Any] = {
        "projects": ["AIL"],
        "process_path": CONFIG_FIXTURES / "process.yaml",
        "fields_path": tmp_path / "config" / "fields.yaml",
        "site": BASE_URL,
        "now": lambda: NOW,
    }
    arguments.update(options)
    return run_doctor(client, store, **arguments)


def check(report: DoctorReport, name: str) -> Check:
    found = [item for item in report.checks if item.name == name]
    assert len(found) == 1, f"expected one {name!r} check in {[item.name for item in report.checks]}"
    return found[0]


def result(item: Check) -> str:
    return "ok" if item.ok else "warn" if item.warning else "fail"


def results(report: DoctorReport) -> dict[str, str]:
    return {item.name: result(item) for item in report.checks}


def write_process(tmp_path: Path, change: Callable[[dict[str, Any]], None]) -> Path:
    """The AIL process fixture with one change, as a file in tmp_path."""
    data = yaml.safe_load((CONFIG_FIXTURES / "process.yaml").read_text(encoding="utf-8"))
    change(data)
    path = tmp_path / "process.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def without_fields(site: FakeJiraSite, *names: str) -> None:
    site.fields = [item for item in site.fields if item["name"] not in names]


def add_status(site: FakeJiraSite, name: str) -> None:
    site.statuses["AIL"][0]["statuses"].append(status(name, "10090"))


def add_classic_story_points(site: FakeJiraSite) -> None:
    """A second story points field, as a company-managed setup adds it."""
    site.fields.append(custom_field(10030, "Story Points", FLOAT, "number"))


# --- the AIL site passes -----------------------------------------------------------------------


def test_ail_site_passes_with_only_warnings(client: JiraClient, store: Store, tmp_path: Path) -> None:
    report = doctor(client, store, tmp_path)

    assert report.ok
    assert [item.name for item in report.checks] == CHECK_ORDER
    assert results(report) == {
        "jira auth": "ok",
        "process config": "ok",
        "project AIL visible": "ok",
        "statuses AIL mapped": "ok",
        # Team-managed: no Epic Link field, which is optional.
        "fields": "warn",
        # No evidence source for any stage after development.
        "evidence": "warn",
        "database": "ok",
    }
    assert report.account_timezone == "America/Chicago"
    assert report.unmapped_statuses == {}
    assert check(report, "project AIL visible").detail == '"AILED", team-managed'
    assert check(report, "statuses AIL mapped").detail == (
        '3 statuses: "To Do" -> backlog, "In Progress" -> development, "Done" -> done'
    )


def test_fields_yaml_is_written_and_matches_the_ail_fixture(
    client: JiraClient, store: Store, tmp_path: Path
) -> None:
    report = doctor(client, store, tmp_path)

    path = tmp_path / "config" / "fields.yaml"
    assert report.fields_written_to == path
    written = FieldsConfig.load(path)
    expected = FieldsConfig.load(CONFIG_FIXTURES / "fields.yaml")
    assert written.id_of("story_points") == "customfield_10016"
    assert written.id_of("sprint") == "customfield_10020"
    ignored = {"generated_at": None, "site": None}
    assert written.model_copy(update=ignored) == expected.model_copy(update=ignored)
    assert list(written.fields) == list(expected.fields)
    assert written.generated_at == "2026-11-04T09:30:00.000Z"
    assert written.site == BASE_URL
    assert path.read_text(encoding="utf-8").startswith(FIELDS_HEADER)
    assert report.fields == written


def test_happy_path_makes_exactly_these_reads(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    doctor(client, store, tmp_path)

    assert site.sent() == [
        ("GET", MYSELF),
        ("GET", PROJECT_AIL),
        ("GET", STATUSES_AIL),
        ("GET", FIELD),
        # One probe per candidate evidence field; no duplicates to decide.
        *[("POST", SEARCH)] * 4,
    ]
    assert site.probe_jql() == EVIDENCE_PROBES
    for body in site.probes():
        assert body["fields"] == ["id"]
        assert body["maxResults"] == 1
        assert "nextPageToken" not in body
    assert site.sleeps == []


SCENARIOS: dict[str, Callable[[FakeJiraSite], object]] = {
    "ail": lambda site: None,
    "auth failed": lambda site: site.fail("GET", MYSELF, 401),
    "project hidden": lambda site: site.projects.pop("AIL"),
    "unmapped status": lambda site: add_status(site, "In Review"),
    "duplicate fields": add_classic_story_points,
    "jira down": lambda site: site.fail("GET", FIELD, 503),
}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_request_is_an_allowlisted_read(
    scenario: str, site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    SCENARIOS[scenario](site)

    doctor(client, store, tmp_path)

    assert site.requests
    for request in site.requests:
        assert (request.method, route_of(request.url.path)) in ALLOWED
    # The only POST is the search, which is a read.
    assert {path for method, path in site.sent() if method != "GET"} <= {SEARCH}


# --- 1. jira auth ---------------------------------------------------------------------------------


def test_wrong_credentials_skip_the_jira_checks_and_make_no_more_calls(store: Store, tmp_path: Path) -> None:
    site = FakeJiraSite(token="a-different-token")
    with site.client() as client:
        report = doctor(client, store, tmp_path)

    assert not report.ok
    assert site.sent() == [("GET", MYSELF)]
    auth = check(report, "jira auth")
    assert result(auth) == "fail"
    assert "HTTP 401" in auth.detail and "JIRA_API_TOKEN" in auth.detail
    for name in JIRA_CHECKS:
        assert result(check(report, name)) == "fail"
        assert check(report, name).detail == "skipped: auth failed"
    # The checks that don't need Jira still run.
    assert result(check(report, "process config")) == "ok"
    assert result(check(report, "database")) == "ok"
    evidence = check(report, "evidence")
    assert result(evidence) == "warn"
    assert report.missing_evidence == {"AIL": MISSING_EVIDENCE}
    assert evidence.detail.endswith("candidate evidence fields not checked (auth failed)")
    assert report.fields is None and report.fields_written_to is None
    assert not (tmp_path / "config" / "fields.yaml").exists()
    assert report.account_timezone is None


def test_forbidden_is_an_auth_failure_too(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    site.fail("GET", MYSELF, 403, {"errorMessages": ["You do not have permission."], "errors": {}})

    report = doctor(client, store, tmp_path)

    assert "HTTP 403" in check(report, "jira auth").detail
    assert all(check(report, name).detail == "skipped: auth failed" for name in JIRA_CHECKS)
    assert site.sent() == [("GET", MYSELF)]


def test_jira_unavailable_on_myself_skips_the_jira_checks(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    site.fail("GET", MYSELF, 503)

    report = doctor(client, store, tmp_path)

    # The client retried (1, 2 and 4 s, recorded, not slept); nothing else was asked.
    assert site.sent() == [("GET", MYSELF)] * 4
    assert site.sleeps == [1, 2, 4]
    assert "HTTP 503" in check(report, "jira auth").detail
    assert all(check(report, name).detail == "skipped: Jira unavailable" for name in JIRA_CHECKS)
    assert not report.ok


def test_other_jira_error_on_myself_skips_the_jira_checks(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    # What a wrong JIRA_BASE_URL path looks like.
    site.fail("GET", MYSELF, 404, {"errorMessages": ["Not found"], "errors": {}})

    report = doctor(client, store, tmp_path)

    assert "HTTP 404" in check(report, "jira auth").detail
    assert all(check(report, name).detail == "skipped: jira auth failed" for name in JIRA_CHECKS)
    assert site.sent() == [("GET", MYSELF)]


@pytest.mark.parametrize(
    ("time_zone", "expected"),
    [
        (None, "Jira reported no account time zone"),
        ("  ", "Jira reported no account time zone"),
        ("Mars/Olympus_Mons", 'the account time zone "Mars/Olympus_Mons" is not a known IANA zone'),
    ],
)
def test_missing_or_unknown_account_zone_is_a_warning(
    time_zone: str | None, expected: str, site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    if time_zone is None:
        del site.myself["timeZone"]
    else:
        site.myself["timeZone"] = time_zone

    report = doctor(client, store, tmp_path)

    auth = check(report, "jira auth")
    assert result(auth) == "warn"
    assert expected in auth.detail
    assert report.ok
    # Signed in, so the Jira checks ran.
    assert result(check(report, "project AIL visible")) == "ok"


def test_credentials_never_reach_the_report_or_the_logs(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path, logs: list[dict[str, Any]]
) -> None:
    basic = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    site.fail("GET", MYSELF, 401, {"errorMessages": [f"token {TOKEN} rejected for Basic {basic}"]})

    report = doctor(client, store, tmp_path)

    rendered = report.render()
    assert "***" in check(report, "jira auth").detail
    for secret in (TOKEN, basic):
        assert secret not in rendered
        assert secret not in json.dumps(logs, default=str)


# --- 2. process config -----------------------------------------------------------------------------


def test_invalid_process_yaml_fails_with_the_config_error_message(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    path = write_process(tmp_path, lambda data: data["projects"]["AIL"]["estimate"].update(hours_per_point=0))
    with pytest.raises(ConfigError) as error:
        ProcessConfig.load(path)

    report = doctor(client, store, tmp_path, process_path=path)

    assert not report.ok
    process = check(report, "process config")
    assert result(process) == "fail"
    # The same message, on one line.
    assert process.detail == " ".join(str(error.value).split())
    assert "hours_per_point" in process.detail
    # The mapping and the evidence can't be judged without the config, and a
    # check that couldn't run hasn't passed ...
    for name in ("statuses AIL mapped", "evidence"):
        assert result(check(report, name)) == "fail"
        assert check(report, name).detail == "skipped: process config did not load"
    assert ("GET", STATUSES_AIL) not in site.sent()
    # ... but the rest still runs, and story points can't be called required.
    assert result(check(report, "project AIL visible")) == "ok"
    assert result(check(report, "fields")) == "warn"
    assert report.fields_written_to is not None
    assert result(check(report, "database")) == "ok"


def test_missing_process_yaml_fails(client: JiraClient, store: Store, tmp_path: Path) -> None:
    report = doctor(client, store, tmp_path, process_path=tmp_path / "nowhere.yaml")

    process = check(report, "process config")
    assert result(process) == "fail"
    assert "config file not found" in process.detail and "nowhere.yaml" in process.detail


def test_unreadable_process_yaml_fails_instead_of_crashing(
    client: JiraClient, store: Store, tmp_path: Path
) -> None:
    folder = tmp_path / "folder.yaml"
    folder.mkdir()
    utf16 = tmp_path / "utf16.yaml"
    # Notepad's "Unicode" encoding.
    utf16.write_bytes((CONFIG_FIXTURES / "process.yaml").read_text(encoding="utf-8").encode("utf-16"))

    for path in (folder, utf16):
        process = check(doctor(client, store, tmp_path, process_path=path), "process config")
        assert result(process) == "fail"
        assert process.detail.startswith(f"cannot read {path}: ")


def test_project_missing_from_process_yaml_is_mapped_from_its_status_categories(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    """JO-17: a board process.yaml leaves out is no failure. The service maps it from Jira's status categories, and
    doctor shows that mapping (a warning), naming any status in no category."""
    site.projects["OPS"] = {
        "id": "10040",
        "key": "OPS",
        "name": "Operations",
        "simplified": False,
        "style": "classic",
    }
    parked = status("Parked", "10099") | {"statusCategory": {"id": 1, "key": "undefined", "name": "No Category"}}
    site.statuses["OPS"] = [
        {
            "id": "10001",
            "name": "Task",
            "subtask": False,
            "statuses": [
                status("Open", "1", "new"),
                status("In Review", "10090"),
                status("Closed", "6", "done"),
                parked,
            ],
        }
    ]

    report = doctor(client, store, tmp_path, projects=["AIL", "OPS"])

    assert [item.name for item in report.checks] == [
        "jira auth",
        "process config",
        "project AIL visible",
        "statuses AIL mapped",
        "project OPS visible",
        "statuses OPS mapped",
        "fields",
        "evidence",
        "database",
    ]
    process = check(report, "process config")
    assert result(process) == "warn"
    assert "not configured for OPS: mapped from Jira's status categories" in process.detail
    assert check(report, "project OPS visible").detail == '"Operations", company-managed'
    ops = check(report, "statuses OPS mapped")
    assert result(ops) == "warn"
    assert ops.detail == (
        'OPS is not in process.yaml; mapped from Jira\'s status categories: "Open" -> backlog, '
        '"In Review" -> development, "Closed" -> done; no category for "Parked"'
    )
    assert report.unmapped_statuses == {"OPS": ["Parked"]}
    assert result(check(report, "statuses AIL mapped")) == "ok"
    # Probes name every visible project.
    assert site.probe_jql()
    assert all(jql.startswith('project in ("AIL", "OPS") AND ') for jql in site.probe_jql())
    evidence = check(report, "evidence")
    assert "OPS not in process.yaml, so not measurable (no evidence source): code_review" in evidence.detail
    assert report.missing_evidence["OPS"] == ["code_review", "dev_signoff", "testing", "test_review", "test_signoff"]
    assert report.ok  # warnings only: nothing here stops the service


# --- 3. project visible --------------------------------------------------------------------------------


def test_project_that_is_not_visible_fails_and_skips_its_statuses(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    del site.projects["AIL"]

    report = doctor(client, store, tmp_path)

    assert not report.ok
    visible = check(report, "project AIL visible")
    assert result(visible) == "fail"
    assert visible.detail == "not visible: the project does not exist or this account cannot browse it"
    assert check(report, "statuses AIL mapped").detail == "skipped: project not visible"
    assert ("GET", STATUSES_AIL) not in site.sent()
    # JQL naming a project Jira can't see fails outright, so nothing is probed.
    assert site.probe_jql() == []
    assert check(report, "evidence").detail.endswith(
        "candidate evidence fields not checked (no visible project)"
    )


def test_project_lookup_that_keeps_failing_reports_the_error(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    site.fail("GET", PROJECT_AIL, 502)

    report = doctor(client, store, tmp_path)

    visible = check(report, "project AIL visible")
    assert result(visible) == "fail"
    assert "failed after 4 attempts" in visible.detail and "HTTP 502" in visible.detail
    assert site.sleeps == [1, 2, 4]
    # Later checks still ran.
    assert result(check(report, "fields")) == "warn"


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ({"simplified": True, "style": "next-gen"}, '"AILED", team-managed'),
        ({"simplified": False, "style": "classic"}, '"AILED", company-managed'),
        ({"simplified": True}, '"AILED", team-managed'),
        ({"simplified": False}, '"AILED", company-managed'),
        ({"style": "next-gen"}, '"AILED", team-managed'),
        ({"style": "classic"}, '"AILED", company-managed'),
        ({}, '"AILED", management style not reported'),
    ],
)
def test_project_detail_says_how_it_is_managed(
    style: dict[str, Any], expected: str, site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    project = site.projects["AIL"]
    project.pop("simplified")
    project.pop("style")
    project.update(style)

    assert check(doctor(client, store, tmp_path), "project AIL visible").detail == expected


# --- 4. statuses mapped -------------------------------------------------------------------------------


def test_unmapped_status_fails_and_is_listed(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    add_status(site, "In Review")

    report = doctor(client, store, tmp_path)

    assert not report.ok
    statuses = check(report, "statuses AIL mapped")
    assert result(statuses) == "fail"
    assert statuses.detail == 'no stage for "In Review" (add to status_to_stage for AIL in process.yaml)'
    assert report.unmapped_statuses == {"AIL": ["In Review"]}
    # A failed mapping doesn't stop the later checks.
    assert result(check(report, "fields")) == "warn"
    assert report.fields_written_to is not None
    assert result(check(report, "database")) == "ok"


def test_status_mapping_ignores_case(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    for issue_type in site.statuses["AIL"]:
        for item in issue_type["statuses"]:
            item["name"] = item["name"].upper()

    statuses = check(doctor(client, store, tmp_path), "statuses AIL mapped")

    assert result(statuses) == "ok"
    assert statuses.detail == '3 statuses: "TO DO" -> backlog, "IN PROGRESS" -> development, "DONE" -> done'


def test_no_statuses_at_all_fails(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    site.statuses["AIL"] = []

    statuses = check(doctor(client, store, tmp_path), "statuses AIL mapped")

    assert result(statuses) == "fail"
    assert "no statuses" in statuses.detail


def test_status_names_cannot_break_or_fake_report_lines(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    hostile = "QA\n[ OK ] everything is fine\x1b[2J"
    add_status(site, hostile)

    report = doctor(client, store, tmp_path)

    rendered = report.render()
    assert len(rendered.splitlines()) == len(report.checks) + 1
    assert "\x1b" not in rendered
    assert '"QA [ OK ] everything is fine [2J"' in check(report, "statuses AIL mapped").detail
    # The data keeps Jira's exact name.
    assert report.unmapped_statuses == {"AIL": [hostile]}


# --- 5. fields -----------------------------------------------------------------------------------------


def test_duplicate_story_point_fields_pick_the_populated_one(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    add_classic_story_points(site)
    site.set_everywhere("customfield_10016", None)
    site.set_everywhere("customfield_10030", 3.0)

    report = doctor(client, store, tmp_path)

    assert report.fields is not None
    assert report.fields.id_of("story_points") == "customfield_10030"
    assert report.fields.fields["story_points"].name == "Story Points"
    assert report.fields.fields["story_points"].custom_type == FLOAT
    assert FieldsConfig.load(tmp_path / "config" / "fields.yaml").id_of("story_points") == "customfield_10030"
    # Preferred name first; the populated one wins.
    assert site.probe_jql()[:2] == [
        'project in ("AIL") AND cf[10016] is not EMPTY',
        'project in ("AIL") AND cf[10030] is not EMPTY',
    ]
    assert (
        "story_points: 2 fields match (customfield_10016, customfield_10030); "
        "using customfield_10030, the one set on in-scope issues"
    ) in check(report, "fields").detail
    # Many issues match, but one page of one issue answers the question.
    assert all(body["maxResults"] == 1 and "nextPageToken" not in body for body in site.probes())
    assert report.ok


def test_populated_preferred_field_wins_after_one_probe(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    add_classic_story_points(site)
    site.set_everywhere("customfield_10030", 5.0)

    report = doctor(client, store, tmp_path)

    assert report.fields is not None and report.fields.id_of("story_points") == "customfield_10016"
    assert "cf[10030]" not in " ".join(site.probe_jql())
    assert site.probe_jql()[0] == 'project in ("AIL") AND cf[10016] is not EMPTY'


def test_repeated_name_picks_the_populated_field(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    site.fields.append(
        custom_field(10050, "Story point estimate", f"{GREENHOPPER}:jsw-story-points", "number")
    )
    site.set_everywhere("customfield_10016", None)
    site.set_everywhere("customfield_10050", 2.0)

    report = doctor(client, store, tmp_path)

    assert report.fields is not None and report.fields.id_of("story_points") == "customfield_10050"


def test_unpopulated_duplicates_go_to_preference_order_whatever_the_listing_order(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    # Listed first, matched case-insensitively, empty everywhere, like the original.
    site.fields.insert(
        0, custom_field(10050, "STORY POINT ESTIMATE", f"{GREENHOPPER}:jsw-story-points", "number")
    )
    add_classic_story_points(site)
    site.set_everywhere("customfield_10016", None)

    report = doctor(client, store, tmp_path)

    assert report.fields is not None and report.fields.id_of("story_points") == "customfield_10016"
    assert (
        "story_points: 3 fields match (customfield_10016, customfield_10050, customfield_10030); "
        "using customfield_10016, none is set on in-scope issues, so the first by preference"
    ) in check(report, "fields").detail


def test_probe_rejected_with_400_counts_as_not_set(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    add_classic_story_points(site)
    site.unsearchable.add("customfield_10016")
    site.set_everywhere("customfield_10030", 1.0)

    report = doctor(client, store, tmp_path)

    assert report.fields is not None and report.fields.id_of("story_points") == "customfield_10030"
    assert report.ok


def test_non_custom_duplicates_are_never_probed(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    site.fields.append(system_field("sprint", "Sprint", "array"))
    site.set_everywhere("customfield_10020", None)

    report = doctor(client, store, tmp_path)

    assert report.fields is not None and report.fields.id_of("sprint") == "customfield_10020"
    assert "cf[10020]" in " ".join(site.probe_jql())
    assert all("sprint" not in jql.lower() for jql in site.probe_jql())


def test_failed_probe_fails_the_fields_check_and_writes_nothing(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    add_classic_story_points(site)
    site.fail("POST", SEARCH, 503)

    report = doctor(client, store, tmp_path)

    fields = check(report, "fields")
    assert result(fields) == "fail"
    assert "HTTP 503" in fields.detail and fields.detail.endswith("fields.yaml left unchanged")
    assert report.fields is None and report.fields_written_to is None
    assert not (tmp_path / "config" / "fields.yaml").exists()
    assert check(report, "evidence").detail.endswith(
        "candidate evidence fields not checked (fields unavailable)"
    )


def test_field_list_error_fails_the_fields_check(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    site.fail("GET", FIELD, 400, {"errorMessages": ["Bad request"], "errors": {}})

    report = doctor(client, store, tmp_path)

    fields = check(report, "fields")
    assert result(fields) == "fail" and "HTTP 400" in fields.detail
    assert report.fields is None
    assert not (tmp_path / "config" / "fields.yaml").exists()


def test_empty_field_list_fails_and_keeps_the_old_file(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    path = tmp_path / "config" / "fields.yaml"
    path.parent.mkdir()
    path.write_text("previous\n", encoding="utf-8")
    site.fields = [{"id": "no-name"}, "not a field"]  # nothing usable

    report = doctor(client, store, tmp_path)

    fields = check(report, "fields")
    assert result(fields) == "fail"
    assert fields.detail == "Jira returned no fields; fields.yaml left unchanged"
    assert path.read_text(encoding="utf-8") == "previous\n"


def test_missing_story_points_fails_when_estimates_are_points(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    without_fields(site, "Story point estimate")

    report = doctor(client, store, tmp_path)

    fields = check(report, "fields")
    assert result(fields) == "fail"
    assert "missing: story_points (required: an in-scope project estimates in points)" in fields.detail
    assert not report.ok
    # The report still records what the site has; the file is not written.
    assert report.fields is not None and report.fields.id_of("story_points") is None
    assert fields.detail.endswith("; fields.yaml left unchanged")
    assert report.fields_written_to is None
    assert not (tmp_path / "config" / "fields.yaml").exists()


def test_missing_story_points_keeps_the_last_good_fields_yaml(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    # Regression: a doctor run while the field was invisible (renamed, or a
    # permission change) wrote story_points: null over the good file, and the
    # next sync would have blanked story points on every issue.
    path = tmp_path / "config" / "fields.yaml"
    path.parent.mkdir()
    good = (CONFIG_FIXTURES / "fields.yaml").read_text(encoding="utf-8")
    path.write_text(good, encoding="utf-8")
    without_fields(site, "Story point estimate")

    report = doctor(client, store, tmp_path)

    assert result(check(report, "fields")) == "fail"
    assert path.read_text(encoding="utf-8") == good
    assert FieldsConfig.load(path).id_of("story_points") == "customfield_10016"


def test_missing_story_points_is_a_warning_when_estimates_are_hours(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    path = write_process(
        tmp_path,
        lambda data: data["projects"]["AIL"].update(
            estimate={"basis": "hours", "field": "original_estimate"}
        ),
    )
    without_fields(site, "Story point estimate")

    report = doctor(client, store, tmp_path, process_path=path)

    fields = check(report, "fields")
    assert result(fields) == "warn"
    assert "not found (optional): story_points, epic_link" in fields.detail
    assert report.ok


def test_missing_optional_fields_are_named_in_a_warning(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    without_fields(site, "Development")

    fields = check(doctor(client, store, tmp_path), "fields")

    assert result(fields) == "warn"
    assert "not found (optional): epic_link (epics come from the parent field), development" in fields.detail


def test_fields_check_passes_when_every_field_is_found(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    site.fields.append(custom_field(10014, "Epic Link", f"{GREENHOPPER}:gh-epic-link", "any"))

    report = doctor(client, store, tmp_path)

    fields = check(report, "fields")
    assert result(fields) == "ok"
    assert "epic_link=customfield_10014" in fields.detail
    assert report.fields is not None and report.fields.id_of("epic_link") == "customfield_10014"


def test_write_fields_false_leaves_the_file_untouched(
    client: JiraClient, store: Store, tmp_path: Path
) -> None:
    path = tmp_path / "config" / "fields.yaml"
    path.parent.mkdir()
    original = "# hand-edited; keep me\nfields: {}\n"
    path.write_text(original, encoding="utf-8")

    report = doctor(client, store, tmp_path, write_fields=False)

    assert path.read_text(encoding="utf-8") == original
    assert report.fields_written_to is None
    assert report.fields is not None and report.fields.id_of("story_points") == "customfield_10016"
    assert check(report, "fields").detail.endswith("; fields.yaml not written")


def test_unwritable_fields_file_fails_the_fields_check(
    client: JiraClient, store: Store, tmp_path: Path
) -> None:
    path = tmp_path / "fields.yaml"
    path.mkdir()

    report = doctor(client, store, tmp_path, fields_path=path)

    fields = check(report, "fields")
    assert result(fields) == "fail"
    assert f"could not write {path}: " in fields.detail
    assert report.fields_written_to is None
    assert report.fields is not None


# --- 6. evidence ---------------------------------------------------------------------------------------


def test_missing_evidence_is_listed_by_name_as_a_warning(
    client: JiraClient, store: Store, tmp_path: Path
) -> None:
    report = doctor(client, store, tmp_path)

    evidence = check(report, "evidence")
    assert result(evidence) == "warn"
    assert report.missing_evidence == {"AIL": MISSING_EVIDENCE}
    assert evidence.detail.startswith(
        "AIL not measurable (no evidence source): "
        "code_review, dev_signoff, testing, test_review, test_signoff; "
    )
    assert report.ok


def test_candidate_evidence_fields_are_reported_as_existing_but_empty(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    report = doctor(client, store, tmp_path)

    assert check(report, "evidence").detail.endswith(
        "candidate evidence fields in use: Programmer; "
        "existing but empty: Code Reviewers, Testers, Test Reviewers"
    )
    assert site.probe_jql() == EVIDENCE_PROBES


def test_each_field_is_probed_once_even_when_both_checks_need_it(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    # A second "Code Reviewers" field, the one actually in use: the fields check
    # probes both, and the evidence check reuses the answer for the chosen one.
    site.fields.append(custom_field(10150, "Code Reviewers", PEOPLE))
    site.set_everywhere("customfield_10150", [{"accountId": "acc-dev-2"}])

    report = doctor(client, store, tmp_path)

    assert report.fields is not None and report.fields.id_of("code_reviewers") == "customfield_10150"
    jql = site.probe_jql()
    assert len(jql) == len(set(jql)) == 5
    assert check(report, "evidence").detail.endswith(
        "candidate evidence fields in use: Programmer, Code Reviewers; "
        "existing but empty: Testers, Test Reviewers"
    )


def test_candidate_evidence_fields_missing_from_the_site_are_named(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    without_fields(site, "Testers", "Test Reviewers")

    report = doctor(client, store, tmp_path)

    assert check(report, "evidence").detail.endswith(
        "candidate evidence fields in use: Programmer; existing but empty: Code Reviewers; "
        "not on this site: Testers, Test Reviewers"
    )
    assert len(site.probe_jql()) == 2


def test_evidence_probe_failure_is_reported_without_failing(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    site.fail("POST", SEARCH, 500)

    report = doctor(client, store, tmp_path)

    evidence = check(report, "evidence")
    assert result(evidence) == "warn"
    assert "not checked: Programmer, Code Reviewers, Testers, Test Reviewers (" in evidence.detail
    assert "HTTP 500" in evidence.detail
    # The first probe's four attempts, then no more: the others would fail the same way.
    assert site.sent().count(("POST", SEARCH)) == 4
    assert site.sleeps == [1, 2, 4]
    # No duplicates, so the fields themselves were settled without a probe.
    assert result(check(report, "fields")) == "warn"
    assert report.ok


def test_evidence_check_passes_when_every_source_is_configured(
    client: JiraClient, store: Store, tmp_path: Path
) -> None:
    path = write_process(
        tmp_path,
        lambda data: data["projects"]["AIL"].update(
            evidence={stage: {"label": f"{stage}-done"} for stage in MISSING_EVIDENCE}
            | {"code_review": {"remote_link_contains": "pull-requests"}}
        ),
    )

    report = doctor(client, store, tmp_path, process_path=path)

    evidence = check(report, "evidence")
    assert result(evidence) == "ok"
    assert evidence.detail.startswith("AIL has every evidence source configured; ")
    assert report.missing_evidence == {}


def test_stages_left_out_of_the_evidence_map_are_named_too(
    client: JiraClient, store: Store, tmp_path: Path
) -> None:
    # Regression: only code_review configured, the other stages simply left out,
    # used to pass as "every evidence source configured".
    path = write_process(
        tmp_path,
        lambda data: data["projects"]["AIL"].update(
            evidence={"code_review": {"remote_link_contains": "pull-requests"}}
        ),
    )

    report = doctor(client, store, tmp_path, process_path=path)

    evidence = check(report, "evidence")
    assert result(evidence) == "warn"
    assert report.missing_evidence == {"AIL": ["dev_signoff", "testing", "test_review", "test_signoff"]}
    assert evidence.detail.startswith(
        "AIL not measurable (no evidence source): dev_signoff, testing, test_review, test_signoff; "
    )


# --- 7. database ---------------------------------------------------------------------------------------


def test_database_check_reports_the_schema_version(client: JiraClient, store: Store, tmp_path: Path) -> None:
    database = check(doctor(client, store, tmp_path), "database")

    assert result(database) == "ok"
    assert database.detail == f"SQLite schema version {SCHEMA_VERSION} ({store.path})"


def test_in_memory_database_is_named_as_such(client: JiraClient, tmp_path: Path) -> None:
    with Store.open(":memory:") as memory:
        database = check(doctor(client, memory, tmp_path), "database")

    assert database.detail == f"SQLite schema version {SCHEMA_VERSION} (in memory)"


def test_unusable_database_fails_its_check(client: JiraClient, tmp_path: Path) -> None:
    closed = Store.open(tmp_path / "closed.db")
    closed.close()

    report = doctor(client, closed, tmp_path)

    database = check(report, "database")
    assert result(database) == "fail"
    assert database.detail.startswith("SQLite error: ")
    assert not report.ok


def test_wrong_schema_version_fails(client: JiraClient, tmp_path: Path) -> None:
    with Store.open(tmp_path / "old.db") as old:
        old.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        database = check(doctor(client, old, tmp_path), "database")

    assert result(database) == "fail"
    assert database.detail.startswith(f"schema version {SCHEMA_VERSION + 1}, expected {SCHEMA_VERSION}")


# --- project keys ----------------------------------------------------------------------------------------


def test_project_keys_are_upper_cased_and_deduplicated(
    site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    report = doctor(client, store, tmp_path, projects=["ail", " AIL "])

    assert [item.name for item in report.checks] == CHECK_ORDER
    assert site.sent().count(("GET", PROJECT_AIL)) == 1


@pytest.mark.parametrize("projects", [[], [" "], ["A-B"], ['AIL") OR ("X']])
def test_bad_project_keys_are_a_config_error_before_any_call(
    projects: list[str], site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError):
        doctor(client, store, tmp_path, projects=projects)

    assert site.requests == []


# --- the report ----------------------------------------------------------------------------------------


def test_render_shows_one_tagged_line_per_check_then_the_verdict(
    client: JiraClient, store: Store, tmp_path: Path
) -> None:
    report = doctor(client, store, tmp_path)

    fields_path = tmp_path / "config" / "fields.yaml"
    assert report.render().splitlines() == [
        "[ OK ] jira auth: signed in; account time zone America/Chicago",
        "[ OK ] process config: process.yaml version 0.1.0-test; configured for AIL",
        '[ OK ] project AIL visible: "AILED", team-managed',
        '[ OK ] statuses AIL mapped: 3 statuses: "To Do" -> backlog, '
        '"In Progress" -> development, "Done" -> done',
        "[WARN] fields: story_points=customfield_10016, sprint=customfield_10020, "
        "development=customfield_10000, programmer=customfield_10138, "
        "code_reviewers=customfield_10139, testers=customfield_10140, test_reviewers=customfield_10141; "
        "not found (optional): epic_link (epics come from the parent field); "
        f"written to {fields_path}",
        "[WARN] evidence: AIL not measurable (no evidence source): code_review, dev_signoff, testing, "
        "test_review, test_signoff; candidate evidence fields in use: Programmer; "
        "existing but empty: Code Reviewers, Testers, Test Reviewers",
        f"[ OK ] database: SQLite schema version {SCHEMA_VERSION} ({store.path})",
        "Verdict: PASS with 2 warnings",
    ]


def test_render_of_a_failed_run(site: FakeJiraSite, client: JiraClient, store: Store, tmp_path: Path) -> None:
    add_status(site, "In Review")

    lines = doctor(client, store, tmp_path).render().splitlines()

    assert lines[3].startswith('[FAIL] statuses AIL mapped: no stage for "In Review"')
    assert lines[-1] == "Verdict: FAIL (1 of 7 checks failed, 2 warnings)"


def test_render_and_ok_for_hand_made_reports() -> None:
    report = DoctorReport(checks=[Check("a", True, "fine"), Check("b", True)])
    assert report.ok
    assert report.render().splitlines() == ["[ OK ] a: fine", "[ OK ] b", "Verdict: PASS"]

    report.checks.append(Check("c", False, "careful", warning=True))
    assert report.ok
    assert report.render().splitlines()[-1] == "Verdict: PASS with 1 warning"

    report.checks.append(Check("d", False, "broken\nline"))
    report.checks.append(Check("e", False, warning=True))
    assert not report.ok
    lines = report.render().splitlines()
    assert lines[2:] == [
        "[WARN] c: careful",
        "[FAIL] d: broken line",
        "[WARN] e",
        "Verdict: FAIL (1 of 5 checks failed, 2 warnings)",
    ]
    assert DoctorReport().render() == "Verdict: PASS"


def test_report_text_is_ascii_for_any_console(client: JiraClient, store: Store, tmp_path: Path) -> None:
    # Windows writes redirected output in cp1252; the report's own words must fit any code page.
    assert doctor(client, store, tmp_path).render().isascii()


def test_each_check_is_logged(
    client: JiraClient, store: Store, tmp_path: Path, logs: list[dict[str, Any]]
) -> None:
    report = doctor(client, store, tmp_path)

    checks = [entry for entry in logs if entry["event"] == "doctor_check"]
    assert [entry["check"] for entry in checks] == CHECK_ORDER
    assert [entry["result"] for entry in checks] == ["ok", "ok", "ok", "ok", "warn", "warn", "ok"]
    (finished,) = [entry for entry in logs if entry["event"] == "doctor_finished"]
    assert finished["ok"] is True and report.ok
    assert finished["warnings"] == ["fields", "evidence"] and finished["failed"] == []
    assert finished["jira_requests"] == 8


# --- the CLI command --------------------------------------------------------------------------------------


def cli_site(settings: Settings) -> tuple[FakeJiraSite, Callable[[Settings], JiraClient]]:
    site = FakeJiraSite(email=settings.jira_email, token=settings.jira_api_token.get_secret_value())

    def factory(options: Settings) -> JiraClient:
        return JiraClient.from_settings(
            options, transport=httpx.MockTransport(site), sleep=site.sleeps.append
        )

    return site, factory


def test_cli_doctor_prints_the_report_and_rewrites_fields_yaml(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    site, factory = cli_site(settings)

    code = main(["doctor"], settings=settings, client_factory=factory)

    output = capsys.readouterr().out
    assert code == EXIT_OK
    assert output.splitlines()[0] == "[ OK ] jira auth: signed in; account time zone America/Chicago"
    assert output.splitlines()[-1] == "Verdict: PASS with 2 warnings"
    # The settings fixture's copy was replaced by doctor's own file.
    assert settings.fields_file.read_text(encoding="utf-8").startswith(FIELDS_HEADER)
    assert FieldsConfig.load(settings.fields_file).site == settings.jira_base_url
    assert all((request.method, route_of(request.url.path)) in ALLOWED for request in site.requests)


def test_cli_doctor_exits_1_on_a_failed_check_and_can_leave_fields_yaml_alone(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    site, factory = cli_site(settings)
    add_status(site, "In Review")
    before = settings.fields_file.read_text(encoding="utf-8")

    code = main(["doctor", "--no-write-fields"], settings=settings, client_factory=factory)

    assert code == EXIT_CHECKS_FAILED
    assert settings.fields_file.read_text(encoding="utf-8") == before
    assert '[FAIL] statuses AIL mapped: no stage for "In Review"' in capsys.readouterr().out
