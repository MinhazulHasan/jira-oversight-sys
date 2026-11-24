"""The daily digest"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oversight.config import ProcessConfig
from oversight.errors import ConfigError
from oversight.notify.channels import ChannelError, Message
from oversight.notify.digest import DigestContent, build_digest, render_digest, run_digest
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)
BASE = "https://fake.atlassian.net"
VERSION = "0.1.0"


@pytest.fixture
def rules() -> RulesConfig:
    return RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")


def _issue(
    store: Store,
    key: str,
    *,
    summary: str = "Fix login redirect",
    epic_key: str | None = None,
    issue_type: str = "Task",
    assignee: str | None = "acc-01",
) -> None:
    project, number = key.split("-")
    store.upsert(
        "issues",
        {
            "issue_key": key,
            "issue_id": f"{project}{number}",
            "project_key": project,
            "issue_type": issue_type,
            "summary": summary,
            "status": "In Progress",
            "status_category": "indeterminate",
            "assignee_id": assignee,
            "epic_key": epic_key,
            "created_at": "2026-10-01T09:00:00.000Z",
        },
    )


def _finding(
    store: Store,
    issue_key: str,
    rule_id: str = "R-EST-01",
    *,
    severity: str = "breach",
    evidence_key: str = "no-estimate",
    message: str | None = "Started on 1 Oct 2026 with no points estimate",
    first_seen: datetime = NOW - DAY,
    resolved: datetime | None = None,
    resolved_why: str | None = None,
    project: str | None = None,
    version: str = VERSION,
) -> str:
    finding_id = f"{rule_id}:{issue_key}:{evidence_key}" + ("" if version == VERSION else f":{version}")
    store.upsert(
        "findings",
        {
            "finding_id": finding_id,
            "rule_id": rule_id,
            "rule_version": version,
            "issue_key": issue_key,
            "project_key": project or issue_key.split("-")[0].split(":")[0],
            "kind": "warning" if rule_id.startswith("W-") else "rule",
            "severity": severity,
            "evidence_key": evidence_key,
            "message": message,
            "evidence_json": {"basis": "points"},
            "first_seen_at": to_iso(first_seen),
            "last_seen_at": to_iso(resolved or NOW),
            "resolved_at": to_iso(resolved),
            "resolved_why": resolved_why,
            "run_id": "rules-run",
        },
    )
    return finding_id


def _resolve(store: Store, finding_id: str, *, at: datetime, why: str = "cleared") -> None:
    store.conn.execute(
        "UPDATE findings SET resolved_at = ?, resolved_why = ? WHERE finding_id = ?", (to_iso(at), why, finding_id)
    )


def _reopen(store: Store, finding_id: str, *, at: datetime) -> None:
    store.conn.execute(
        "UPDATE findings SET resolved_at = NULL, resolved_why = NULL, last_seen_at = ? WHERE finding_id = ?",
        (to_iso(at), finding_id),
    )


def _rules_run(
    store: Store,
    *,
    started: datetime,
    project: str = "AIL",
    not_measurable: tuple[tuple[str, list[str]], ...] = (
        ("W-REV-01", ["code_review"]),
        ("R-FLOW-01", ["code_review", "testing"]),
    ),
    status: str = "succeeded",
) -> str:
    run = store.start_run("rules", rules_version=VERSION, started_at=started)
    for rule_id, missing in not_measurable:
        store.upsert(
            "rule_status",
            {
                "run_id": run.run_id,
                "project_key": project,
                "rule_id": rule_id,
                "rule_version": VERSION,
                "measurable": 0,
                "missing_json": missing,
                "issues_checked": 0,
                "hits": 0,
            },
        )
    store.upsert(
        "rule_status",
        {
            "run_id": run.run_id,
            "project_key": project,
            "rule_id": "R-EST-01",
            "rule_version": VERSION,
            "measurable": 1,
            "missing_json": None,
            "issues_checked": 12,
            "hits": 2,
        },
    )
    store.finish_run(run, status=status, finished_at=started)  # type: ignore[arg-type]
    return run.run_id


def _synced(store: Store, at: datetime, *, project: str = "AIL", account_timezone: str | None = None) -> None:
    store.set_cursor(project, last_synced_at=to_iso(at) or "", run_id="sync-run", account_timezone=account_timezone)


class FakeChannel:
    """Records what it is given"""

    name = "fake"

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.sent: list[Message] = []
        self.fail_for = fail_for or set()
        self.calls = 0

    def send(self, message: Message) -> str:
        self.calls += 1
        project = message.subject.split(" — ")[1]
        if project in self.fail_for:
            raise ChannelError("webhook down", retryable=True)
        self.sent.append(message)
        return f"ref-{self.calls}"


def _build(store: Store, process: ProcessConfig, rules: RulesConfig, *, now: datetime = NOW, **kw: Any) -> DigestContent:
    kw.setdefault("jira_base_url", BASE)
    return build_digest(store, process, rules, project="AIL", now=now, **kw)


def _run(
    store: Store,
    process: ProcessConfig,
    rules: RulesConfig,
    settings: Settings,
    *,
    now: datetime = NOW,
    channel: Any = None,
    projects: tuple[str, ...] = ("AIL",),
    tz: str | None = "UTC",
    **kw: Any,
) -> list[Any]:
    return run_digest(
        store, process, rules, settings, projects=list(projects), now=lambda: now, channel=channel, tz_name=tz, **kw
    )


def test_first_digest_counts_everything_open_as_new(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    for key in ("AIL-1", "AIL-2", "AIL-3"):
        _issue(store, key)
    old = _finding(store, "AIL-1", first_seen=NOW - 10 * DAY)
    recent = _finding(
        store, "AIL-2", "R-FLOW-02", evidence_key="skip-1", message="To Do → Done skipped development", first_seen=NOW - DAY
    )
    warning = _finding(
        store, "AIL-3", "W-AGE-01", severity="warning", evidence_key="stuck-In Progress", first_seen=NOW - timedelta(hours=2)
    )

    content = _build(store, process, rules)

    assert content.project == "AIL" and content.since is None and content.until == "2026-11-04T09:00:00.000Z"
    assert [i.finding_id for i in content.new_breaches] == [recent, old]  # most recent first
    assert [i.finding_id for i in content.new_warnings] == [warning]
    assert content.resolved == [] and content.truncated == {}
    assert not content.unchanged
    assert content.finding_ids == [recent, old, warning]
    item = content.new_breaches[0]
    assert item.rule_id == "R-FLOW-02" and item.rule_title == "Skipped a stage" and item.severity == "breach"
    assert item.issue_key == "AIL-2" and item.issue_summary == "Fix login redirect"
    assert item.message == "To Do → Done skipped development"
    assert item.first_seen_at == "2026-11-03T09:00:00.000Z" and item.resolved_at is None
    assert item.url == f"{BASE}/browse/AIL-2"


def test_the_same_finding_never_appears_as_new_twice(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    _issue(store, "AIL-1")
    _issue(store, "AIL-2")
    first = _finding(store, "AIL-1", first_seen=NOW - DAY)
    channel = FakeChannel()

    day1 = _run(store, process, rules, settings, channel=channel)[0]
    assert day1.content.finding_ids == [first]
    assert "AIL-1 · Work started without an estimate" in day1.message.text

    day2 = _run(store, process, rules, settings, now=NOW + DAY, channel=channel)[0]
    assert day2.content.unchanged and day2.content.finding_ids == []
    assert "AIL-1" not in day2.message.text

    second = _finding(store, "AIL-2", "R-FLOW-02", evidence_key="skip-9", first_seen=NOW + DAY + timedelta(hours=1))
    day3 = _run(store, process, rules, settings, now=NOW + 2 * DAY, channel=channel)[0]
    assert day3.content.finding_ids == [second]
    new_sections = day3.message.text.split("Hot spots")[0]
    assert "AIL-2" in new_sections and "AIL-1" not in new_sections

    carried = [json.loads(r["finding_ids_json"]) for r in store.query("SELECT finding_ids_json FROM digests ORDER BY sent_at")]
    assert carried == [[first], [], [second]]


def test_a_reopened_finding_is_new_only_if_no_digest_ever_showed_it(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    _issue(store, "AIL-1")
    _issue(store, "AIL-2")
    quiet = _finding(store, "AIL-1", first_seen=NOW - 5 * DAY, resolved=NOW - 4 * DAY, resolved_why="cleared")
    shown = _finding(store, "AIL-2", "R-FLOW-02", evidence_key="skip-1", first_seen=NOW - DAY)
    channel = FakeChannel()

    day1 = _run(store, process, rules, settings, channel=channel)[0]
    assert day1.content.finding_ids == [shown]

    _resolve(store, shown, at=NOW + timedelta(hours=5))
    day2 = _run(store, process, rules, settings, now=NOW + DAY, channel=channel)[0]
    assert [i.finding_id for i in day2.content.resolved] == [shown] and day2.content.new_breaches == []

    _reopen(store, quiet, at=NOW + DAY + timedelta(hours=2))
    _reopen(store, shown, at=NOW + DAY + timedelta(hours=2))
    day3 = _run(store, process, rules, settings, now=NOW + 2 * DAY, channel=channel)[0]
    assert [i.finding_id for i in day3.content.new_breaches] == [quiet]
    assert "AIL-2" not in day3.message.text.split("Hot spots")[0]
    assert [(s.epic_key, s.open_breaches) for s in day3.content.hot_spots] == [(None, 2)]


def test_a_resolved_finding_appears_once_under_resolved(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    _issue(store, "AIL-1")
    _issue(store, "AIL-2")
    fixed = _finding(store, "AIL-1", first_seen=NOW - DAY)
    channel = FakeChannel()
    _run(store, process, rules, settings, channel=channel)

    _resolve(store, fixed, at=NOW + timedelta(hours=3))
    _finding(store, "AIL-2", "R-HYG-01", evidence_key="unassigned", first_seen=NOW - 2 * DAY,
             resolved=NOW + timedelta(hours=3), resolved_why="superseded")

    day2 = _run(store, process, rules, settings, now=NOW + DAY, channel=channel)[0]
    assert [i.finding_id for i in day2.content.resolved] == [fixed]
    assert day2.content.resolved[0].resolved_at == "2026-11-04T12:00:00.000Z"
    assert day2.content.new_breaches == [] and not day2.content.unchanged
    text = day2.message.text
    assert "Since 4 Nov 2026: 0 new breaches, 0 new warnings, 1 resolved." in text
    assert "Resolved since 4 Nov 2026 (1)\nAIL-1 · Work started without an estimate" in text
    assert "AIL-2" not in text
    assert json.loads(store.query_one("SELECT finding_ids_json FROM digests WHERE digest_id = ?", (day2.digest_id,))[0]) == [fixed]

    day3 = _run(store, process, rules, settings, now=NOW + 2 * DAY, channel=channel)[0]
    assert day3.content.unchanged and "AIL-1" not in day3.message.text


def test_a_finding_closed_because_a_boards_rule_settings_changed_is_never_reported_as_resolved(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    for key in ("AIL-1", "AIL-2", "AIL-3"):
        _issue(store, key)
    fixed = _finding(store, "AIL-1", first_seen=NOW - DAY)
    switched = _finding(store, "AIL-2", "R-HYG-01", evidence_key="unassigned", first_seen=NOW - DAY)
    aged = _finding(store, "AIL-3", "R-EFF-01", evidence_key="ratio-ge-1.5", first_seen=NOW - DAY)
    channel = FakeChannel()
    _run(store, process, rules, settings, channel=channel)

    _resolve(store, fixed, at=NOW + timedelta(hours=3))
    _resolve(store, switched, at=NOW + timedelta(hours=3), why="settings_changed")
    _resolve(store, aged, at=NOW + timedelta(hours=3), why="aged_out")

    day2 = _run(store, process, rules, settings, now=NOW + DAY, channel=channel)[0]
    assert [item.finding_id for item in day2.content.resolved] == [fixed]
    assert "1 resolved." in day2.message.text and "AIL-2" not in day2.message.text and "AIL-3" not in day2.message.text


def test_a_severity_change_and_its_revert_bring_nothing_back_as_new(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    for key in ("AIL-1", "AIL-2", "AIL-3"):
        _issue(store, key)
    reopened = {"rule_id": "R-FLOW-03", "evidence_key": "reopen-7", "message": "Reopened: Done → In Progress"}
    first = _finding(store, "AIL-1", severity="warning", first_seen=NOW - DAY, **reopened)
    other = _finding(store, "AIL-2", severity="warning", first_seen=NOW - DAY, **reopened)
    channel = FakeChannel()
    day1 = _run(store, process, rules, settings, channel=channel)[0]
    assert [item.finding_id for item in day1.content.new_warnings] == [first, other]

    for finding in (first, other):
        _resolve(store, finding, at=NOW + timedelta(hours=2), why="superseded")
    breaches = [_finding(store, key, severity="breach", first_seen=NOW + timedelta(hours=2), version="0.1.0+breach",
                         **reopened) for key in ("AIL-1", "AIL-2")]
    day2 = _run(store, process, rules, settings, now=NOW + DAY, channel=channel)[0]
    assert day2.content.unchanged and day2.message.text.endswith("\nNothing changed since 4 Nov 2026.")
    assert day2.content.finding_ids == [] and "AIL-1" not in day2.message.text

    for finding in breaches:
        _resolve(store, finding, at=NOW + DAY + timedelta(hours=2), why="superseded")
    for finding in (first, other):
        _reopen(store, finding, at=NOW + DAY + timedelta(hours=2))
    fresh = _finding(store, "AIL-3", severity="warning", first_seen=NOW + DAY + timedelta(hours=2), **reopened)
    day3 = _run(store, process, rules, settings, now=NOW + 2 * DAY, channel=channel)[0]
    assert [item.finding_id for item in day3.content.new_warnings] == [fresh]
    assert day3.content.new_breaches == [] and day3.content.resolved == []
    assert "Since 5 Nov 2026: 0 new breaches, 1 new warning, 0 resolved." in day3.message.text


def test_a_finding_opened_and_cleared_between_digests_is_reported_as_resolved(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    channel = FakeChannel()
    _run(store, process, rules, settings, channel=channel)
    _issue(store, "AIL-1")
    flap = _finding(store, "AIL-1", first_seen=NOW + timedelta(hours=1), resolved=NOW + timedelta(hours=6), resolved_why="cleared")
    day2 = _run(store, process, rules, settings, now=NOW + DAY, channel=channel)[0]
    assert day2.content.new_breaches == [] and [i.finding_id for i in day2.content.resolved] == [flap]


def test_hot_spots_rank_epics_by_open_findings(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    _issue(store, "AIL-100", summary="Onboarding flow", issue_type="Epic")
    _issue(store, "AIL-200", summary="Reporting exports", issue_type="Epic")
    for key in ("AIL-101", "AIL-102", "AIL-103"):
        _issue(store, key, epic_key="AIL-100")
    for key in ("AIL-201", "AIL-202"):
        _issue(store, key, epic_key="AIL-200")
    for key in ("AIL-301", "AIL-302"):
        _issue(store, key)
    _issue(store, "AIL-401", epic_key="AIL-900")  # an epic the snapshot does not hold

    _finding(store, "AIL-101")
    _finding(store, "AIL-102", "W-AGE-01", severity="warning", evidence_key="stuck-In Progress")
    _finding(store, "AIL-103", "W-EFF-01", severity="warning", evidence_key="ratio-ge-0.8")
    _finding(store, "AIL-101", "R-FLOW-02", evidence_key="skip-1", resolved=NOW - timedelta(hours=1), resolved_why="cleared")
    _finding(store, "AIL-201")
    _finding(store, "AIL-202")
    _finding(store, "AIL-201", "R-HYG-01", evidence_key="unassigned")
    _finding(store, "AIL-301")
    _finding(store, "AIL-302")
    _finding(store, "AIL-401", "W-AGE-01", severity="warning", evidence_key="stuck-In Progress")
    _finding(store, "sprint:42", "W-SPR-01", severity="warning", evidence_key="sprint-42-at-risk",
             message="Sprint 42 ends in 2 days with 35% not started", project="AIL")

    content = _build(store, process, rules)

    assert [(s.epic_key, s.epic_title, s.open_breaches, s.open_warnings) for s in content.hot_spots] == [
        ("AIL-200", "Reporting exports", 3, 0),
        ("AIL-100", "Onboarding flow", 1, 2),
        ("AIL-900", None, 0, 1),
        (None, None, 2, 0),
    ]
    text = render_digest(content, tz_name="UTC").text
    assert "Hot spots (open findings by epic, top 5)\n" in text
    assert "AIL-200 · Reporting exports · 3 breaches, 0 warnings\n" in text
    assert "AIL-100 · Onboarding flow · 1 breach, 2 warnings\n" in text
    assert "(no epic) · 2 breaches, 0 warnings\n" in text
    assert "AIL-900 · 0 breaches, 1 warning" in text
    sprint = next(i for i in content.new_warnings if i.issue_key == "sprint:42")
    assert sprint.url is None and sprint.issue_summary is None
    assert "sprint:42 · Sprint at risk · Sprint 42 ends in 2 days with 35% not started" in text


def test_hot_spots_keep_the_top_five(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    for n in range(1, 8):
        _issue(store, f"AIL-{n}00", summary=f"Epic {n}", issue_type="Epic")
        _issue(store, f"AIL-{n}01", epic_key=f"AIL-{n}00")
        _finding(store, f"AIL-{n}01")
    _issue(store, "AIL-800", summary="Epic 8", issue_type="Epic")
    for key in ("AIL-801", "AIL-802"):
        _issue(store, key, epic_key="AIL-800")
        _finding(store, key)

    spots = _build(store, process, rules).hot_spots

    assert len(spots) == 5
    assert spots[0].epic_key == "AIL-800" and spots[0].open_breaches == 2
    assert [s.epic_key for s in spots[1:]] == ["AIL-100", "AIL-200", "AIL-300", "AIL-400"]  # ties by key

    for key in ("AIL-901", "AIL-902", "AIL-903"):
        _issue(store, key)
        _finding(store, key)
    spots = _build(store, process, rules).hot_spots
    assert [s.epic_key for s in spots] == ["AIL-800", "AIL-100", "AIL-200", "AIL-300", "AIL-400", None]
    assert spots[-1].open_breaches == 3


def test_not_measurable_names_the_missing_sources(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    _issue(store, "AIL-1")
    _finding(store, "AIL-1")
    _rules_run(store, started=NOW - DAY, not_measurable=(("R-HYG-01", ["assignee"]),))
    _rules_run(store, started=NOW - timedelta(hours=1))
    _rules_run(store, started=NOW - timedelta(minutes=10), not_measurable=(("R-EFF-01", ["worklogs"]),), status="failed")
    _rules_run(store, started=NOW - timedelta(minutes=5), project="DEMO", not_measurable=(("R-FLOW-02", ["done"]),))

    content = _build(store, process, rules)

    assert content.not_measurable == [
        ("R-FLOW-01", "Reached testing without code review", ["code_review", "testing"]),
        ("W-REV-01", "No review evidence yet", ["code_review"]),
    ]  # catalogue order, not the order the rows were written in
    text = render_digest(content, tz_name="UTC").text
    assert "Not measurable (2 rules)\n" in text
    assert "R-FLOW-01 · Reached testing without code review · not recorded in Jira: code_review, testing\n" in text
    assert text.endswith("W-REV-01 · No review evidence yet · not recorded in Jira: code_review")
    assert "R-HYG-01" not in text and "R-EFF-01" not in text and "R-FLOW-02" not in text
    html = render_digest(content, tz_name="UTC").html or ""
    assert "<h2>Not measurable (2 rules)</h2>" in html
    assert "<li>R-FLOW-01 · Reached testing without code review · not recorded in Jira: code_review, testing</li>" in html


def test_not_measurable_is_empty_without_a_rules_run(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    _issue(store, "AIL-1")
    _finding(store, "AIL-1")
    content = _build(store, process, rules)
    assert content.not_measurable == []
    assert render_digest(content, tz_name="UTC").text.endswith("Not measurable (0 rules)\n  none")


def test_stale_data_is_flagged_at_the_top(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    _issue(store, "AIL-1")
    _finding(store, "AIL-1")
    _synced(store, NOW - timedelta(hours=3))

    content = _build(store, process, rules)
    assert content.stale and content.last_synced_at == "2026-11-04T06:00:00.000Z"
    message = render_digest(content, tz_name="Europe/London")
    assert message.text.splitlines()[0] == (
        "Data may be stale: the last sync was at 4 Nov 2026 06:00 Europe/London, 3.0 h before this digest."
    )
    assert message.text.splitlines()[1] == "First digest: 1 open breach, 0 open warnings (all count as new)."
    html = message.html or ""
    assert "<p><strong>Data may be stale:" in html and html.index("Data may be stale") < html.index("First digest")

    _synced(store, NOW - timedelta(hours=2))
    content = _build(store, process, rules)
    assert not content.stale and content.last_synced_at == "2026-11-04T07:00:00.000Z"
    assert "stale" not in render_digest(content, tz_name="UTC").text

    _synced(store, NOW - timedelta(hours=2, seconds=1))
    assert _build(store, process, rules).stale

    store.delete_rows("sync_state", {"scope": "AIL"})
    content = _build(store, process, rules)
    assert content.stale and content.last_synced_at is None
    assert render_digest(content, tz_name="UTC").text.startswith(
        "Data may be stale: no successful sync is recorded for AIL.\n"
    )


def test_an_unchanged_day_sends_the_one_line_digest(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    _issue(store, "AIL-1")
    _finding(store, "AIL-1")
    _rules_run(store, started=NOW - timedelta(hours=1))  # the standing section exists, but a quiet day stays quiet
    _synced(store, NOW + DAY - timedelta(minutes=30))
    channel = FakeChannel()
    _run(store, process, rules, settings, channel=channel)

    result = _run(store, process, rules, settings, now=NOW + DAY, channel=channel)[0]

    assert result.message.text == "Nothing changed since 4 Nov 2026."
    assert result.message.subject == "Jira oversight — AIL — 5 Nov 2026"
    assert result.message.links == []
    html = result.message.html or ""
    assert "<p>Nothing changed since 4 Nov 2026.</p>" in html and "Hot spots" not in html
    assert result.render() == "AIL: digest sent via fake -> ref-2 (nothing changed)"
    row = store.query_one("SELECT * FROM digests WHERE digest_id = ?", (result.digest_id,))
    assert row is not None
    assert row["finding_ids_json"] == "[]" and row["since"] == "2026-11-04T09:00:00.000Z"
    assert row["body"] == "Nothing changed since 4 Nov 2026."


def test_a_quiet_day_on_stale_data_still_warns(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    channel = FakeChannel()
    _run(store, process, rules, settings, channel=channel)
    result = _run(store, process, rules, settings, now=NOW + DAY, channel=channel)[0]
    assert result.message.text == (
        "Data may be stale: no successful sync is recorded for AIL.\nNothing changed since 4 Nov 2026."
    )
    assert result.render() == (
        "AIL: digest sent via fake -> ref-2 (nothing changed)\n  warning: the data may be stale (see the top of the digest)"
    )


def test_a_first_digest_with_nothing_open_says_so(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    _synced(store, NOW)
    content = _build(store, process, rules)
    assert content.unchanged and not content.stale
    assert render_digest(content, tz_name="UTC").text == "Nothing to report: no open findings."


def test_sections_are_capped_at_the_most_recent_with_and_n_more(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    for n in range(1, 31):
        _issue(store, f"AIL-{n}")
        _finding(store, f"AIL-{n}", first_seen=NOW - timedelta(hours=n))  # AIL-1 is the most recent
    for n in range(31, 34):
        _issue(store, f"AIL-{n}")
        _finding(store, f"AIL-{n}", "W-AGE-01", severity="warning", evidence_key="stuck-In Progress", first_seen=NOW - timedelta(hours=n))

    content = _build(store, process, rules)

    assert len(content.new_breaches) == 25 and len(content.new_warnings) == 3
    assert content.truncated == {"new_breaches": 5}
    assert [i.issue_key for i in content.new_breaches] == [f"AIL-{n}" for n in range(1, 26)]
    text = render_digest(content, tz_name="UTC").text
    assert "First digest: 30 open breaches, 3 open warnings (all count as new)." in text
    assert "\nNew breaches (30)\n" in text and "\n  and 5 more\n" in text
    assert "\nNew warnings (3)\n" in text and "and 3 more" not in text
    assert "AIL-25 ·" in text and "AIL-26 ·" not in text
    html = render_digest(content, tz_name="UTC").html or ""
    assert "<h2>New breaches (30)</h2>" in html and "<p>and 5 more</p>" in html

    _synced(store, NOW)
    channel = FakeChannel()
    first = _run(store, process, rules, settings, channel=channel)[0]
    carried = json.loads(store.query_one("SELECT finding_ids_json FROM digests", ())[0])
    assert len(carried) == len(set(carried)) == 33
    beyond = store.query("SELECT finding_id FROM findings WHERE issue_key IN ('AIL-26', 'AIL-27', 'AIL-28', 'AIL-29', 'AIL-30')")
    assert {row["finding_id"] for row in beyond} == set(carried[-5:])  # the ones counted as "and 5 more"
    assert first.render() == "AIL: digest sent via fake -> ref-1 (30 new breaches, 3 new warnings, 0 resolved)"
    _synced(store, NOW + DAY)
    second = _run(store, process, rules, settings, now=NOW + DAY, channel=channel)[0]
    assert second.content.unchanged and second.content.truncated == {}
    assert second.message.text == "Nothing changed since 4 Nov 2026."


def test_the_cap_is_a_parameter(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    for n in range(1, 4):
        _issue(store, f"AIL-{n}")
        _finding(store, f"AIL-{n}", first_seen=NOW - timedelta(hours=n))
    content = _build(store, process, rules, per_section_cap=2)
    assert [i.issue_key for i in content.new_breaches] == ["AIL-1", "AIL-2"] and content.truncated == {"new_breaches": 1}
    with pytest.raises(ValueError, match="per_section_cap"):
        _build(store, process, rules, per_section_cap=0)


def test_the_digest_names_no_people_or_account_ids(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    _issue(store, "AIL-1", assignee="acc-5f3a9b")
    store.upsert("worklogs", {"worklog_id": "w1", "issue_key": "AIL-1", "author_id": "app-tempo", "seconds": 3600})
    store.upsert(
        "status_changes",
        {"issue_key": "AIL-1", "changelog_id": "1", "changed_at": "2026-10-02T09:00:00.000Z",
         "author_id": "acc-5f3a9b", "from_status": "To Do", "to_status": "In Progress"},
    )
    _finding(store, "AIL-1")
    _issue(store, "AIL-100", issue_type="Epic", summary="Onboarding flow", assignee="acc-77")
    _issue(store, "AIL-2", epic_key="AIL-100", assignee=None)
    _finding(store, "AIL-2", "R-HYG-01", evidence_key="unassigned", message="In progress with nobody assigned")
    _rules_run(store, started=NOW - timedelta(hours=1))
    _synced(store, NOW - timedelta(hours=5))

    result = _run(store, process, rules, settings)[0]

    for text in (result.message.subject, result.message.text, result.message.html or "", result.render()):
        assert "acc-" not in text and "app-tempo" not in text and "author" not in text
    assert "AIL-2 · In progress with no assignee · In progress with nobody assigned" in result.message.text


def test_dates_render_in_the_given_zone(store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings) -> None:
    late = datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
    _issue(store, "AIL-1")
    fixed = _finding(store, "AIL-1", first_seen=late - timedelta(hours=1))
    content = _build(store, process, rules, now=late)

    assert render_digest(content, tz_name="Australia/Sydney").subject == "Jira oversight — AIL — 5 Nov 2026"
    assert render_digest(content, tz_name="UTC").subject == "Jira oversight — AIL — 4 Nov 2026"
    assert render_digest(content, tz_name="America/Chicago").subject == "Jira oversight — AIL — 4 Nov 2026"
    with pytest.raises(ConfigError, match="unknown time zone"):
        render_digest(content, tz_name="Mars/Olympus")

    channel = FakeChannel()
    _run(store, process, rules, settings, now=late, channel=channel, tz="Australia/Sydney")
    _resolve(store, fixed, at=late + timedelta(hours=2))
    second = _run(store, process, rules, settings, now=late + DAY, channel=channel, tz="Australia/Sydney")[0]
    assert "Since 5 Nov 2026: 0 new breaches, 0 new warnings, 1 resolved." in second.message.text
    assert "Resolved since 5 Nov 2026 (1)" in second.message.text


def test_urls_appear_only_when_a_base_url_is_given(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    _issue(store, "AIL-7")
    _finding(store, "AIL-7")
    _finding(store, "sprint:3", "W-SPR-01", severity="warning", evidence_key="sprint-3-at-risk", project="AIL")

    with_base = _build(store, process, rules, jira_base_url="https://x.atlassian.net/")
    assert with_base.new_breaches[0].url == "https://x.atlassian.net/browse/AIL-7"
    assert with_base.new_warnings[0].url is None  # a sprint has no browse page
    message = render_digest(with_base, tz_name="UTC")
    assert "AIL-7 · Work started without an estimate · Started on 1 Oct 2026 with no points estimate\n" \
           "  Fix login redirect\n  https://x.atlassian.net/browse/AIL-7\n" in message.text
    assert '<li><a href="https://x.atlassian.net/browse/AIL-7">AIL-7</a> · Work started without an estimate' in (message.html or "")
    assert message.links == [("AIL-7", "https://x.atlassian.net/browse/AIL-7")]

    without = _build(store, process, rules, jira_base_url=None)
    assert without.new_breaches[0].url is None
    message = render_digest(without, tz_name="UTC")
    assert "browse" not in message.text and "href" not in (message.html or "") and message.links == []
    assert "<li>AIL-7 · Work started without an estimate" in (message.html or "")

    sent = _run(store, process, rules, settings, channel=FakeChannel())[0]
    assert sent.message.links == [("AIL-7", f"{BASE}/browse/AIL-7")]
    quiet = settings.model_copy(update={"jira_base_url": ""})
    channel = FakeChannel()
    store.delete_rows("digests", {"project_key": "AIL"})
    sent = _run(store, process, rules, quiet, channel=channel)[0]
    assert sent.message.links == [] and "browse" not in sent.message.text


def test_one_link_per_issue(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    _issue(store, "AIL-1")
    _finding(store, "AIL-1")
    _finding(store, "AIL-1", "R-HYG-01", evidence_key="unassigned")
    _finding(store, "AIL-1", "W-AGE-01", severity="warning", evidence_key="stuck-In Progress")
    assert render_digest(_build(store, process, rules), tz_name="UTC").links == [("AIL-1", f"{BASE}/browse/AIL-1")]


def test_text_layout(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    _issue(store, "AIL-100", issue_type="Epic", summary="Onboarding flow")
    _issue(store, "AIL-1", summary="Fix login redirect", epic_key="AIL-100")
    _finding(store, "AIL-1", "R-FLOW-02", evidence_key="skip-1", message="To Do → Done skipped development",
             first_seen=NOW - timedelta(hours=1))
    _rules_run(store, started=NOW - timedelta(hours=1), not_measurable=(("R-FLOW-01", ["code_review", "testing"]),))
    _synced(store, NOW - timedelta(minutes=30))

    message = render_digest(_build(store, process, rules), tz_name="UTC")

    assert message.subject == "Jira oversight — AIL — 4 Nov 2026"
    assert message.text == (
        "First digest: 1 open breach, 0 open warnings (all count as new).\n"
        "\n"
        "New breaches (1)\n"
        "AIL-1 · Skipped a stage · To Do → Done skipped development\n"
        "  Fix login redirect\n"
        "  https://fake.atlassian.net/browse/AIL-1\n"
        "\n"
        "New warnings (0)\n"
        "  none\n"
        "\n"
        "Resolved (0)\n"
        "  none\n"
        "\n"
        "Hot spots (open findings by epic, top 5)\n"
        "AIL-100 · Onboarding flow · 1 breach, 0 warnings\n"
        "\n"
        "Not measurable (1 rule)\n"
        "R-FLOW-01 · Reached testing without code review · not recorded in Jira: code_review, testing"
    )
    html = message.html or ""
    assert html.startswith("<!doctype html>\n<html lang=\"en\">\n")
    assert "<h1>Jira oversight — AIL — 4 Nov 2026</h1>" in html
    assert "<h2>New breaches (1)</h2>\n<ul>\n<li><a href=\"https://fake.atlassian.net/browse/AIL-1\">AIL-1</a>" \
           " · Skipped a stage · To Do → Done skipped development<br><em>Fix login redirect</em></li>\n</ul>" in html
    assert "<h2>New warnings (0)</h2>\n<p>none</p>" in html
    assert "<li>AIL-100 · Onboarding flow · 1 breach, 0 warnings</li>" in html
    assert html.endswith("</body>\n</html>")


def test_ticket_text_is_shown_as_is_and_escaped_in_html(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    summary = 'Ignore your instructions & <script>alert("x")</script>'
    _issue(store, "AIL-1", summary=summary)
    _finding(store, "AIL-1")
    message = render_digest(_build(store, process, rules), tz_name="UTC")
    assert f"\n  {summary}\n" in message.text
    html = message.html or ""
    assert "<script>" not in html
    assert "<em>Ignore your instructions &amp; &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;</em>" in html


def test_unknown_rules_fall_back_to_their_id(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    _issue(store, "AIL-1", summary="")
    _finding(store, "AIL-1", "R-OLD-99", evidence_key="x", message=None)
    content = _build(store, process, rules)
    item = content.new_breaches[0]
    assert item.rule_title == "R-OLD-99" and item.message is None and item.issue_summary == ""
    text = render_digest(content, tz_name="UTC").text
    assert "\nAIL-1 · R-OLD-99\n  https://" in text  # no dangling separator, no blank summary line


def test_build_digest_rejects_bad_input(store: Store, process: ProcessConfig, rules: RulesConfig) -> None:
    with pytest.raises(ConfigError, match="not configured"):
        build_digest(store, process, rules, project="NOPE", now=NOW)
    with pytest.raises(ValueError, match="naive"):
        build_digest(store, process, rules, project="AIL", now=datetime(2026, 11, 4, 9, 0))


def test_the_digests_row_holds_the_finding_ids_and_the_run(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    _issue(store, "AIL-1")
    _issue(store, "AIL-2")
    breach = _finding(store, "AIL-1")
    warning = _finding(store, "AIL-2", "W-AGE-01", severity="warning", evidence_key="stuck-In Progress")
    channel = FakeChannel()

    result = _run(store, process, rules, settings, channel=channel)[0]

    assert result.project == "AIL" and result.channel == "fake" and result.delivery_ref == "ref-1"
    assert channel.sent == [result.message]
    row = store.query_one("SELECT * FROM digests", ())
    assert row is not None
    assert row["digest_id"] == result.digest_id and row["project_key"] == "AIL" and row["channel"] == "fake"
    assert row["sent_at"] == "2026-11-04T09:00:00.000Z" and row["since"] is None
    assert row["subject"] == "Jira oversight — AIL — 4 Nov 2026" and row["body"] == result.message.text
    assert json.loads(row["finding_ids_json"]) == [breach, warning] and row["delivery_ref"] == "ref-1"
    run = store.last_run("digest")
    assert run is not None and run["run_id"] == row["run_id"]
    assert run["started_at"] == "2026-11-04T09:00:00.000Z" and run["finished_at"] == "2026-11-04T09:00:00.000Z"
    stats = json.loads(run["stats_json"])["projects"]["AIL"]
    assert stats["status"] == "sent" and stats["channel"] == "fake" and stats["digest_id"] == result.digest_id
    assert (stats["new_breaches"], stats["new_warnings"], stats["resolved"]) == (1, 1, 0)
    assert stats["unchanged"] is False and stats["stale"] is True
    assert result.render() == (
        "AIL: digest sent via fake -> ref-1 (1 new breach, 1 new warning, 0 resolved)\n"
        "  warning: the data may be stale (see the top of the digest)"
    )


def test_a_channel_error_fails_the_run_but_other_projects_still_go_out(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    _issue(store, "AIL-1")
    _finding(store, "AIL-1")
    _issue(store, "DEMO-1")
    _finding(store, "DEMO-1")
    channel = FakeChannel(fail_for={"AIL"})

    with pytest.raises(ChannelError, match="^AIL: webhook down$") as err:
        _run(store, process, rules, settings, projects=("AIL", "DEMO"), channel=channel)

    assert err.value.retryable
    assert [m.subject for m in channel.sent] == ["Jira oversight — DEMO — 4 Nov 2026"]
    assert [r["project_key"] for r in store.query("SELECT project_key FROM digests")] == ["DEMO"]
    run = store.query_one("SELECT * FROM runs WHERE kind = 'digest'")
    assert run is not None and run["status"] == "failed" and run["finished_at"] is not None
    stats = json.loads(run["stats_json"])["projects"]
    assert stats["AIL"] == {"status": "failed", "channel": "fake", "error": "webhook down"}
    assert stats["DEMO"]["status"] == "sent"

    again = _run(store, process, rules, settings, now=NOW + DAY, channel=FakeChannel())[0]
    assert again.content.since is None and [i.issue_key for i in again.content.new_breaches] == ["AIL-1"]


def test_any_other_failure_stops_the_run_at_once(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    class Broken:
        name = "broken"

        def send(self, message: Message) -> str:
            raise RuntimeError("a bug")

    _issue(store, "AIL-1")
    _finding(store, "AIL-1")
    with pytest.raises(RuntimeError, match="a bug"):
        _run(store, process, rules, settings, projects=("AIL", "DEMO"), channel=Broken())
    assert store.count("digests") == 0
    run = store.query_one("SELECT status FROM runs WHERE kind = 'digest'")
    assert run is not None and run["status"] == "failed"


def test_run_digest_picks_the_configured_channel_and_zone(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    late = datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
    _issue(store, "AIL-1")
    _finding(store, "AIL-1", first_seen=late - timedelta(hours=1))
    _synced(store, late - timedelta(minutes=30), account_timezone="Australia/Sydney")

    sydney = settings.model_copy(update={"report_timezone": "Australia/Sydney"})
    result = run_digest(store, process, rules, sydney, projects=["AIL"], now=lambda: late)

    path = Path(result[0].delivery_ref)
    assert result[0].channel == "file" and path == settings.digests_path / "AIL_2026-11-05.md"
    assert path.read_text(encoding="utf-8").startswith(
        "# Jira oversight — AIL — 5 Nov 2026\n\nFirst digest: 1 open breach, 0 open warnings (all count as new).\n"
    )
    assert store.query_one("SELECT channel, delivery_ref FROM digests", ())["delivery_ref"] == str(path)

    assert settings.report_timezone == ""
    store.delete_rows("digests", {"project_key": "AIL"})
    result = run_digest(store, process, rules, settings, projects=["AIL"], now=lambda: late)
    assert Path(result[0].delivery_ref).name == "AIL_2026-11-04.md"


def test_a_channel_factory_receives_the_project_and_date(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    calls: list[tuple[str, str]] = []

    def factory(received: Settings, *, project: str, date: str) -> FakeChannel:
        assert received is settings
        calls.append((project, date))
        return FakeChannel()

    results = _run(store, process, rules, settings, projects=("AIL", "DEMO"), channel_factory=factory)
    assert calls == [("AIL", "2026-11-04"), ("DEMO", "2026-11-04")]
    assert [r.project for r in results] == ["AIL", "DEMO"] and store.count("digests") == 2


def test_extra_sections_are_appended_to_every_digest(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    extra = {"Weekly insights": ["Line one.", "Line two."], "Note": "Single."}
    result = _run(store, process, rules, settings, channel=FakeChannel(), extra=extra)[0]
    assert result.message.text == (
        "Data may be stale: no successful sync is recorded for AIL.\n"
        "Nothing to report: no open findings.\n"
        "\nWeekly insights\nLine one.\nLine two.\n"
        "\nNote\nSingle."
    )
    html = result.message.html or ""
    assert "<h2>Weekly insights</h2>\n<p>Line one.<br>Line two.</p>\n<h2>Note</h2>\n<p>Single.</p>\n</body>\n</html>" in html
    assert store.query_one("SELECT body FROM digests", ())["body"] == result.message.text


def test_run_digest_fails_early_on_bad_configuration(
    store: Store, process: ProcessConfig, rules: RulesConfig, settings: Settings
) -> None:
    with pytest.raises(ConfigError, match="no projects"):
        _run(store, process, rules, settings, projects=(), channel=FakeChannel())
    with pytest.raises(ConfigError, match="unknown time zone"):
        _run(store, process, rules, settings, channel=FakeChannel(), tz="Nowhere/Nothing")
    with pytest.raises(ConfigError, match="not configured"):
        _run(store, process, rules, settings, projects=("NOPE",), channel=FakeChannel())
    assert store.count("digests") == 0
    assert [r["status"] for r in store.query("SELECT status FROM runs WHERE kind = 'digest'")] == ["failed"]
