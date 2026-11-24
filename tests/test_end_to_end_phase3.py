"""Phases 3-7 end to end, through the command line, on the synthetic DEMO project."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from oversight.cli import EXIT_OK, EXIT_SERVICE, main
from oversight.notify.channels import ChannelError, Message
from oversight.rules.catalog import RulesConfig
from oversight.rules.settings import Change, save_board_settings
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import UTC, to_iso

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
DEMO = "DEMO"
# Patterns for what would identify a person: account ids, e-mail addresses, two capitalised words.
ACCOUNT_ID = re.compile(r"\bacc-\d+|\bapp-tempo\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}")
NAME_LIKE = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")
NOT_NAMES = {"To Do", "In Progress"}

Cli = Callable[..., tuple[int, str, str]]


@pytest.fixture
def cli(settings: Settings, capsys: pytest.CaptureFixture[str]) -> Cli:
    """cli(minutes, *argv) -> (exit code, stdout, stderr): one command at T0 + minutes."""

    def run(minutes: float, *argv: str) -> tuple[int, str, str]:
        moment = T0 + timedelta(minutes=minutes)
        code = main(list(argv), settings=settings, now=lambda: moment)
        out, err = capsys.readouterr()
        run.outputs.append(out + err)  # type: ignore[attr-defined]
        return code, out, err

    run.outputs = []  # type: ignore[attr-defined]
    return run


def _open_counts(settings: Settings) -> dict[str, int]:
    with Store.open(settings.db_file) as store:
        rows = store.query(
            "SELECT severity, COUNT(*) AS n FROM findings WHERE project_key = ? AND resolved_at IS NULL GROUP BY severity",
            (DEMO,),
        )
    return {row["severity"]: row["n"] for row in rows}


def _assert_names_nobody(text: str) -> None:
    assert not ACCOUNT_ID.search(text), ACCOUNT_ID.search(text)
    assert not EMAIL.search(text), EMAIL.search(text)
    assert not set(NAME_LIKE.findall(text)) - NOT_NAMES, set(NAME_LIKE.findall(text)) - NOT_NAMES


def test_phases_3_to_7_through_the_command_line(cli: Cli, settings: Settings) -> None:
    assert settings.llm_mode == "stub"

    code, out, _ = cli(0, "demo", "--reset")
    assert code == EXIT_OK and "demo project DEMO: 1,150 issues" in out
    assert 'python -m oversight ask "Where do tickets get stuck most?" --project DEMO' in out

    code, out, _ = cli(0, "derive", "--project", DEMO)
    assert code == EXIT_OK and "DEMO: 1,150 issues" in out

    code, out, _ = cli(0, "rules", "--project", DEMO)
    assert code == EXIT_OK
    counts = _open_counts(settings)
    opened = counts["breach"] + counts["warning"]
    assert counts["breach"] > 0 and counts["warning"] > 0
    assert f"rules DEMO (catalogue 0.1.0): {opened} opened, 0 still open, 0 resolved" in out
    assert re.search(r"R-FLOW-01\s+not measurable: nothing in Jira shows code review and testing", out)
    with Store.open(settings.db_file) as store:
        status = store.query_one(
            "SELECT measurable, missing_json FROM rule_status WHERE project_key = ? AND rule_id = 'R-FLOW-01'", (DEMO,)
        )
    assert status is not None and status["measurable"] == 0
    assert json.loads(status["missing_json"]) == ["code_review", "testing"]

    code, out, _ = cli(1, "rules", "--project", DEMO)
    assert code == EXIT_OK
    assert f"0 opened, {opened} still open, 0 resolved, 0 superseded" in out
    assert _open_counts(settings) == counts

    digest_file = settings.digests_path / "DEMO_2026-11-04.md"
    code, out, _ = cli(2, "digest", "--project", DEMO, "--channel", "file")
    assert code == EXIT_OK
    assert f"({counts['breach']} new breaches, {counts['warning']} new warnings, 0 resolved)" in out
    first = digest_file.read_text(encoding="utf-8")
    assert first.startswith("# Jira oversight — DEMO — 4 Nov 2026\n")
    assert f"\nNew breaches ({counts['breach']})\n" in first and f"\nNew warnings ({counts['warning']})\n" in first
    hot_spots = first.split("\nHot spots (open findings by epic, top 5)\n")[1].split("\n\n")[0].splitlines()
    assert hot_spots[0] == "DEMO-36 · Audit trail · 55 breaches, 30 warnings"
    assert len(hot_spots) == 6 and hot_spots[-1] == "(no epic) · 65 breaches, 41 warnings"
    assert "\nNot measurable (2 rules)\n" in first and "R-FLOW-01 · Reached testing without code review" in first
    assert "http" not in first and "/browse/" not in first
    _assert_names_nobody(first)

    code, out, _ = cli(3, "digest", "--project", DEMO, "--channel", "file")
    assert code == EXIT_OK and "(nothing changed)" in out
    assert digest_file.read_text(encoding="utf-8") == (
        "# Jira oversight — DEMO — 4 Nov 2026\n\nNothing changed since 4 Nov 2026.\n"
    )
    with Store.open(settings.db_file) as store:
        carried = [json.loads(row["finding_ids_json"]) for row in store.query(
            "SELECT finding_ids_json FROM digests WHERE project_key = ? ORDER BY sent_at", (DEMO,)
        )]
    assert [len(ids) for ids in carried] == [opened, 0]

    code, out, _ = cli(4, "ask", "Where do tickets get stuck most?", "--project", DEMO, "--json")
    assert code == EXIT_OK
    answer = json.loads(out)
    assert answer["outcome"] == "answered" and answer["project"] == DEMO
    assert [call["name"] for call in answer["plan"]] == ["oversight_time_in_status"]
    # Time in Done (the wait before a reopen) is left out, so the answer names a real bottleneck.
    assert answer["answer"].startswith("Tickets in In Progress wait longest: a median of 47.2 hours")
    assert answer["citations"] and all(c["fact_ref"] == "F1" for c in answer["citations"])
    assert {c["text"] for c in answer["citations"]} >= {"47.2", "230.0", "324"}

    code, out, _ = cli(4, "ask", "Which tickets reached testing without code review?", "--project", DEMO, "--json")
    assert code == EXIT_OK
    review = json.loads(out)
    assert review["outcome"] == "not_measurable" and review["missing_evidence"] == ["code_review"]
    assert review["answer"] is None and review["citations"] == []

    code, out, _ = cli(5, "insights", "--project", DEMO, "--json")
    assert code == EXIT_OK
    insight = json.loads(out)
    assert insight["verified"] is True and insight["week_of"] == "2026-11-02" and len(insight["sentences"]) == 5
    with Store.open(settings.db_file) as store:
        rows = store.query("SELECT insight_id, verified FROM insights WHERE project_key = ?", (DEMO,))
        finding_ids = {row["finding_id"] for row in store.query("SELECT finding_id FROM findings")}
    assert [(row["insight_id"], row["verified"]) for row in rows] == [(insight["insight_id"], 1)]
    candidates = {candidate["id"]: candidate for candidate in insight["candidates"]}
    for sentence in insight["sentences"]:
        assert sentence["candidate_ids"] and set(sentence["candidate_ids"]) <= set(candidates)
        assert sentence["issue_keys"] and set(sentence["finding_ids"]) <= finding_ids
        kinds = {candidates[cid]["kind"] for cid in sentence["candidate_ids"]}
        if kinds & {"rising_rule", "breach_hot_spot"}:
            assert sentence["finding_ids"], sentence
    assert "Epic DEMO-36 is a breach hot spot" in insight["narrative"]

    code, out, _ = cli(6, "eval")
    assert code == EXIT_OK
    assert "30 cases, 30/30 plans right (100%), 30/30 outcomes right (100%)" in out and "**PASSED**" in out

    with Store.open(settings.db_file) as store:
        target = store.query_one(
            "SELECT finding_id, issue_key FROM findings WHERE project_key = ? AND rule_id = 'R-HYG-01' "
            "AND resolved_at IS NULL ORDER BY issue_key LIMIT 1",
            (DEMO,),
        )
        assert target is not None
        store.conn.execute("UPDATE issues SET assignee_id = 'acc-03' WHERE issue_key = ?", (target["issue_key"],))
    resolved_at = T0 + timedelta(minutes=30)
    assert cli(30, "derive", "--project", DEMO)[0] == EXIT_OK
    code, out, _ = cli(30, "rules", "--project", DEMO)
    assert code == EXIT_OK and re.search(r"\b1 resolved\b", out)
    with Store.open(settings.db_file) as store:
        row = store.query_one("SELECT resolved_at, resolved_why FROM findings WHERE finding_id = ?", (target["finding_id"],))
    assert row is not None and (row["resolved_at"], row["resolved_why"]) == (to_iso(resolved_at), "cleared")

    code, out, _ = cli(31, "digest", "--project", DEMO, "--channel", "file")
    assert code == EXIT_OK and "1 resolved)" in out
    third = digest_file.read_text(encoding="utf-8")
    resolved = third.split("\nResolved since 4 Nov 2026 (1)\n")[1].split("\n\n")[0]
    assert resolved.startswith(f"{target['issue_key']} · In progress with no assignee · In 'In Progress' with nobody assigned")
    assert "http" not in third
    _assert_names_nobody(third)

    for output in cli.outputs:  # type: ignore[attr-defined]
        _assert_names_nobody(output)


def test_the_recheck_step_and_ask_follow_a_boards_rule_settings(cli: Cli, settings: Settings) -> None:
    for argv in (("demo", "--reset", "--issues", "60"), ("derive", "--project", DEMO), ("rules", "--project", DEMO)):
        assert cli(0, *argv)[0] == EXIT_OK
    with Store.open(settings.db_file) as store:
        missing = store.query_one("SELECT COUNT(*) FROM findings WHERE rule_id = 'R-EST-01' AND resolved_at IS NULL")
        save_board_settings(store, RulesConfig.load(settings.rules_file), DEMO, revision=0,
                            changes=[Change("R-EST-01", enabled=False)], now=T0 + timedelta(minutes=1),
                            viewer_hash="local", viewer_name="Local (sign-in off)")
    assert missing is not None and missing[0] > 0

    code, out, _ = cli(2, "rules", "--project", DEMO)

    assert code == EXIT_OK
    assert (f"board rule settings revision 1 (customised: R-EST-01): {missing[0]} findings closed because the settings "
            "changed") in out
    with Store.open(settings.db_file) as store:
        whys = [row[0] for row in store.query("SELECT DISTINCT resolved_why FROM findings WHERE rule_id = 'R-EST-01'")]
        latest = store.query_one("SELECT stats_json FROM runs WHERE kind = 'rules' ORDER BY started_at DESC LIMIT 1")
    assert whys == ["settings_changed"]
    assert latest is not None and json.loads(latest["stats_json"])[DEMO]["settings_revision"] == 1

    code, out, _ = cli(3, "ask", "Which rules can't be measured in Jira today?", "--project", DEMO, "--json")
    assert code == EXIT_OK
    listed = {row["rule_id"] for fact in json.loads(out)["facts"] for row in fact["rows"]}
    assert listed and "R-EST-01" not in listed


class _FailingChannel:
    name = "teams"

    def send(self, message: Message) -> str:
        raise ChannelError("the Teams workflow answered 503", retryable=True)


def test_a_failed_delivery_exits_with_one_line_and_records_the_failed_run(
    cli: Cli, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    for argv in (("demo", "--reset", "--issues", "60"), ("derive", "--project", DEMO), ("rules", "--project", DEMO)):
        assert cli(0, *argv)[0] == EXIT_OK

    def failing_channel(*_args: Any, **_kwargs: Any) -> _FailingChannel:
        return _FailingChannel()

    monkeypatch.setattr("oversight.notify.digest.get_channel", failing_channel)
    code, out, err = cli(1, "digest", "--project", DEMO)

    assert code == EXIT_SERVICE and out == ""
    assert err.strip().splitlines()[-1] == (
        "error: DEMO: the Teams workflow answered 503 (temporary: the next run may succeed)"
    )
    assert "Traceback" not in err
    with Store.open(settings.db_file) as store:
        runs = store.query("SELECT status FROM runs WHERE kind = 'digest'")
        assert [row["status"] for row in runs] == ["failed"] and store.count("digests") == 0
