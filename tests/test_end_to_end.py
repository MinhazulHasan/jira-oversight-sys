"""Phases 0-2 end to end, through the command line, against FakeJira.

doctor -> sync --full -> sync --incremental -> derive -> baseline, then a change
made in Jira travels through sync and derive into issue_metrics. Every command
runs through oversight.cli.main with the test settings (every path in tmp_path)
and a real JiraClient on FakeJira's transport: nothing leaves the process.

The CLI reads the real clock, so the fake projects are built in the days before
now, and the Jira change is stamped after the sync it must be picked up after.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from oversight.cli import EXIT_CHECKS_FAILED, EXIT_CONFIG, EXIT_JIRA, EXIT_JIRA_AUTH, EXIT_OK, main
from oversight.config import FieldsConfig
from oversight.jira.client import ALLOWED, JiraClient, route_of
from oversight.settings import Settings
from oversight.store.db import SCHEMA_VERSION, Store
from oversight.timeutil import hours_between, to_iso, utcnow

from tests.fakes.builders import AIL_COUNTS, ail_and_ops
from tests.fakes.fake_jira import CHICAGO, MYSELF, SEARCH, TOKEN, FakeJira

Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def fake() -> FakeJira:
    """AIL and OPS built over the last few days (the build spans about 4.6 days)."""
    jira = FakeJira()
    jira.clock = utcnow() - timedelta(days=6)
    return ail_and_ops(jira, now=utcnow())


@pytest.fixture
def run(settings: Settings, capsys: pytest.CaptureFixture[str]) -> Callable[..., Run]:
    """run(fake)(*argv) -> (exit code, stdout, stderr), through cli.main."""

    def bind(fake: FakeJira, **client_overrides: object) -> Run:
        def factory(_settings: Settings) -> JiraClient:
            return JiraClient(**(fake.client_kwargs() | client_overrides), sleep=lambda _: None)

        def command(*argv: str) -> tuple[int, str, str]:
            code = main(list(argv), settings=settings, client_factory=factory)
            out, err = capsys.readouterr()
            return code, out, err

        return command

    return bind


def metrics_of(settings: Settings, key: str) -> dict:
    with Store.open(settings.db_file) as store:
        row = store.query_one("SELECT * FROM issue_metrics WHERE issue_key = ?", (key,))
        return dict(row)


def counts(settings: Settings) -> dict[str, int]:
    with Store.open(settings.db_file) as store:
        return {table: store.count(table) for table in AIL_COUNTS}


def assert_read_only(fake: FakeJira) -> None:
    assert fake.requests
    for request in fake.requests:
        assert (request.method, route_of(request.url.path)) in ALLOWED, (request.method, request.url.path)


def test_the_pipeline_from_doctor_to_baseline_and_a_change_reaching_the_metrics(
    fake: FakeJira, run: Callable[..., Run], settings: Settings
) -> None:
    cli = run(fake)
    settings.fields_file.unlink()  # doctor writes it

    # --- Phase 0: doctor ---------------------------------------------------------------
    code, out, _ = cli("doctor")
    assert code == EXIT_OK, out
    assert out.splitlines()[:4] == [
        "[ OK ] jira auth: signed in; account time zone America/Chicago",
        "[ OK ] process config: process.yaml version 0.1.0-test; configured for AIL",
        '[ OK ] project AIL visible: "AIL (sample)", team-managed',
        '[ OK ] statuses AIL mapped: 3 statuses: "To Do" -> backlog, "In Progress" -> development, '
        '"Done" -> done',
    ]
    assert "[WARN] fields: story_points=customfield_10016, sprint=customfield_10020" in out
    assert "not found (optional): epic_link (epics come from the parent field)" in out
    assert (
        "[WARN] evidence: AIL not measurable (no evidence source): "
        "code_review, dev_signoff, testing, test_review, test_signoff" in out
    )
    assert f"[ OK ] database: SQLite schema version {SCHEMA_VERSION}" in out
    assert out.rstrip().endswith("Verdict: PASS with 2 warnings")
    written = FieldsConfig.load(settings.fields_file)
    assert (written.id_of("story_points"), written.id_of("sprint")) == ("customfield_10016", "customfield_10020")
    assert written.site == settings.jira_base_url
    assert_read_only(fake)

    # --- Phase 1: a full sync, then an incremental one with nothing new ------------------
    code, out, _ = cli("sync", "--full")
    assert code == EXIT_OK
    assert re.search(r"^sync run \w+ \(full requested\): AIL full$", out, re.M)
    assert "  issues: 34 seen, 0 deleted; worklogs from the feed: 0" in out
    assert f"  rows written: {sum(AIL_COUNTS.values())}" in out
    assert counts(settings) == AIL_COUNTS
    assert all('"OPS"' not in jql for jql in fake.jqls())  # on the site, but not in JIRA_PROJECTS

    code, out, _ = cli("sync", "--incremental")
    assert code == EXIT_OK
    assert "(incremental requested): AIL incremental" in out
    assert "  issues: 0 seen, 0 deleted; worklogs from the feed: 0" in out
    assert "  rows written: 0\n" in out
    assert "warnings: none" in out
    assert counts(settings) == AIL_COUNTS

    # --- Phase 2: derive and the baseline report ----------------------------------------
    code, out, _ = cli("derive")
    assert code == EXIT_OK
    intervals = AIL_COUNTS["status_changes"] + AIL_COUNTS["issues"]  # one more interval than changes, per issue
    assert re.search(rf"^derive run \w+: {intervals} status intervals in [0-9.]+ s$", out, re.M)
    assert "  AIL: 34 issues\n  warnings: none" in out

    before = metrics_of(settings, "AIL-15")  # In Progress, 0.5 points, no time logged
    assert (before["done_at"], before["effort_ratio"], before["estimate_basis"]) == (None, 0.0, "points")

    as_of = datetime.now(ZoneInfo(CHICAGO)).date() + timedelta(days=1)  # the window ends after now
    code, out, _ = cli("baseline", "--as-of", as_of.isoformat(), "--tz", CHICAGO)
    assert code == EXIT_OK
    resolved = [issue for issue in fake.project_issues("AIL") if issue.resolved is not None]
    assert len(resolved) == 14  # by hand from AIL_PLAN: every D not followed by an R
    assert f"baseline AIL as of {as_of.isoformat()}: 90 days in {CHICAGO}" in out
    assert "  issues: 34 in the snapshot, 34 created before the window end" in out
    assert "  resolved in the window: 14 (" in out
    reports = settings.reports_path
    stem = f"baseline_AIL_{as_of.isoformat()}"
    assert sorted(p.name for p in reports.iterdir()) == [f"{stem}.json", f"{stem}.md"]
    figures = json.loads((reports / f"{stem}.json").read_text(encoding="utf-8"))["figures"]
    assert figures["snapshot"]["total_issues"] == 34
    assert figures["resolved"]["in_window"] == 14
    assert figures["resolved"]["straight_todo_to_done"]["count"] == 2  # AIL-12 and AIL-34
    assert figures["resolved"]["with_story_points"]["count"] == sum(1 for i in resolved if i.points)
    logged = figures["resolved"]["with_logged_time"]["timespent"]["count"]
    assert logged == sum(1 for i in resolved if i.time_spent)
    missing = ["code_review", "dev_signoff", "testing", "test_review", "test_signoff"]
    assert figures["evidence"]["not_measurable"] == missing
    markdown = (reports / f"{stem}.md").read_text(encoding="utf-8")
    assert markdown.startswith(f"# Baseline report: AIL, as of {as_of.isoformat()}")
    for account in ("acc-dev-1", "acc-dev-2", "app-tempo"):  # team and process level only
        assert account not in markdown

    # --- a change in Jira reaches issue_metrics ------------------------------------------
    fake.clock = utcnow()  # after the last sync started
    fake.log_work("AIL-15", 4 * 3600)
    fake.transition("AIL-15", "Done")
    done_at = to_iso(fake.issue("AIL-15").resolved)

    code, out, _ = cli("sync", "--incremental")
    assert code == EXIT_OK
    assert "  issues: 1 seen, 0 deleted; worklogs from the feed: 1" in out
    assert "  rows written: 3\n" in out  # the issue updated, its new status change, its new worklog
    code, out, _ = cli("derive")
    assert code == EXIT_OK

    after = metrics_of(settings, "AIL-15")
    assert after["done_at"] == done_at
    assert after["effort_ratio"] == pytest.approx(1.0)  # 4 h logged / (0.5 points x 8 h)
    assert after["cycle_time_h"] == pytest.approx(hours_between(after["started_at"], done_at))
    assert after["computed_at"] > before["computed_at"]

    with Store.open(settings.db_file) as store:
        runs = [(r["kind"], r["status"]) for r in store.query("SELECT kind, status FROM runs ORDER BY rowid")]
    assert runs == [
        ("sync", "succeeded"),
        ("sync", "succeeded"),
        ("derive", "succeeded"),
        ("baseline", "succeeded"),
        ("sync", "succeeded"),
        ("derive", "succeeded"),
    ]
    assert_read_only(fake)


def test_wrong_credentials_exit_3_from_sync_and_fail_the_doctor(
    fake: FakeJira, run: Callable[..., Run], settings: Settings
) -> None:
    cli = run(fake, api_token="not-the-token")

    code, out, err = cli("sync", "--full")
    assert code == EXIT_JIRA_AUTH
    assert out == ""
    assert "Jira refused the credentials or the permission: GET /rest/api/3/myself returned HTTP 401" in err
    assert len(fake.requests) == 1  # no retry, no further call
    with Store.open(settings.db_file) as store:
        assert store.last_run("sync", status="failed") is not None
        assert store.get_cursor("AIL") is None

    # Doctor reports the same failure as a failed check (exit 1), with the rest skipped.
    code, out, err = cli("doctor")
    assert code == EXIT_CHECKS_FAILED
    assert "[FAIL] jira auth: GET /rest/api/3/myself returned HTTP 401" in out
    assert "[FAIL] project AIL visible: skipped: auth failed" in out
    assert TOKEN not in out + err and "not-the-token" not in out + err


def test_jira_unavailable_exits_4_after_the_retries(
    fake: FakeJira, run: Callable[..., Run], settings: Settings
) -> None:
    fake.fail(SEARCH, status=503)

    code, out, err = run(fake)("sync", "--full")

    assert code == EXIT_JIRA
    assert out == ""
    assert "Jira error: POST /rest/api/3/search/jql failed after 4 attempts (last: HTTP 503)" in err
    assert len(fake.calls(SEARCH)) == 4
    with Store.open(settings.db_file) as store:
        failed = store.last_run("sync", status="failed")
        assert failed is not None and "HTTP 503" in json.loads(failed["stats_json"])["error"]
        assert store.get_cursor("AIL") is None


def test_config_errors_exit_2_before_any_jira_call(
    fake: FakeJira, run: Callable[..., Run], settings: Settings, tmp_path: Path
) -> None:
    cli = run(fake)

    settings.fields_file.unlink()
    code, _, err = cli("sync", "--incremental")
    assert code == EXIT_CONFIG
    assert "fields.yaml is missing: run `python -m oversight doctor` first" in err

    code, _, err = cli("derive", "--project", "XYZ")
    assert (code, "project 'XYZ' is not configured in process.yaml" in err) == (EXIT_CONFIG, True)

    code, _, err = cli("baseline", "--window-days", "0")
    assert (code, "--window-days must be at least 1" in err) == (EXIT_CONFIG, True)

    settings.process_file.write_text(settings.process_file.read_text(encoding="utf-8"), encoding="utf-16")
    code, _, err = cli("derive")
    assert (code, "is not UTF-8 text" in err) == (EXIT_CONFIG, True)

    insecure = settings.model_copy(update={"jira_base_url": "http://fake.atlassian.net"})
    code = main(["doctor"], settings=insecure, client_factory=lambda s: JiraClient(**fake.client_kwargs()))
    assert code == EXIT_CONFIG

    assert fake.requests == []  # every one of them stopped before Jira
    assert not fake.calls(MYSELF)
