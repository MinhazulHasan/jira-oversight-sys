from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oversight.ask.analyst import SnapshotQuery
from oversight.ask.catalog import CatalogContext, run_query
from oversight.config import ProcessConfig
from oversight.insights.weekly import build_candidates
from oversight.mcp_server.server import build_server
from oversight.notify.digest import DigestContent, build_digest
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store.db import Store, open_readonly
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES
from tests.test_api_ask_insights import _seed
from tests.test_api_overview import KEY, make_settings
from tests.test_api_rule_settings import ADMIN, ADMIN_NAME, Clock, Steps, headers, run_the_rules, serve, settle
from tests.test_mcp_server import call, payload

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)  # the seed's clock: its rules run
LATER = NOW + timedelta(minutes=5)  # the API's clock, unless a test moves it
PREVIEW = "/api/v1/findings/ignore/preview?level=people"
IGNORE = "/api/v1/findings/ignore?level=people"
RESTORE = "/api/v1/findings/restore?level=people"
IGNORED = "/api/v1/findings/ignored?level=people"
CATALOGUE = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")
PROCESS = ProcessConfig.load(CONFIG_FIXTURES / "process.yaml")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
PREVIEW_KEYS = {"meta", "findings", "tickets", "by_rule", "already_ignored"}
IGNORE_KEYS = {"ignore_id", "scope", "issue_key", "issue_summary", "issue_type", "status", "status_category", "rule_id",
               "rule_title", "findings", "reason", "by", "at"}
OPEN = "resolved_at IS NULL AND issue_key NOT LIKE 'sprint:%'"
TABLES = ("findings", "finding_ignores", "rule_status", "runs", "warning_links", "access_log")


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    settings = make_settings(tmp_path_factory.mktemp("ignore_seed"))
    _seed(settings, briefing=False)
    return settings.db_file


@pytest.fixture
def board(seeded: Path, tmp_path: Path) -> Settings:
    """A copy of the seeded DEMO snapshot for this test alone (it writes to it)."""
    settings = Settings(_env_file=None, jira_projects="DEMO", api_service_key=KEY, db_path=tmp_path / "oversight.db",
                        config_dir=CONFIG_FIXTURES, reports_dir=tmp_path / "reports")
    for suffix in ("", "-wal"):
        source = Path(f"{seeded}{suffix}")
        if source.exists():
            shutil.copyfile(source, Path(f"{settings.db_file}{suffix}"))
    return settings


def rows(settings: Settings, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    conn = open_readonly(settings.db_file)
    try:
        return [tuple(row) for row in conn.execute(sql, tuple(params))]
    finally:
        conn.close()


def ids_of(settings: Settings, where: str, params: Sequence[Any] = ()) -> set[str]:
    return {row[0] for row in rows(settings, f"SELECT finding_id FROM findings WHERE {where}", params)}


def everything(settings: Settings) -> dict[str, list[tuple[Any, ...]]]:
    return {table: rows(settings, f"SELECT * FROM {table} ORDER BY 1") for table in TABLES}


def post(client: TestClient, path: str, body: dict[str, Any], status: int = 200) -> dict[str, Any]:
    response = client.post(path, json=body)
    assert response.status_code == status, response.text
    found: dict[str, Any] = response.json()
    return found


def get(client: TestClient, path: str, params: Any = None) -> dict[str, Any]:
    response = client.get(f"/api/v1{path}", params=params)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def every_page(client: TestClient, params: dict[str, Any] | Sequence[tuple[str, Any]]) -> list[dict[str, Any]]:
    pairs = list(params.items()) if isinstance(params, dict) else list(params)
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        body = get(client, "/findings", [*pairs, ("page", page), ("page_size", 100)])
        items += body["items"]
        if len(items) >= body["total"]:
            return items
        page += 1


# --- ignoring ---------------------------------------------------------------------------------------------------------


def test_ignoring_findings_by_id_resolves_them_as_ignored_at_once(board: Settings) -> None:
    client, _ = serve(board)
    picked = sorted(ids_of(board, f"rule_id = 'R-EST-01' AND {OPEN}"))[:3]
    tickets = {row[0] for row in rows(board, "SELECT issue_key FROM findings WHERE finding_id IN (?, ?, ?)", picked)}
    body = {"target": {"finding_ids": picked}, "scope": "finding"}
    before = everything(board)

    preview = post(client, PREVIEW, body)
    assert set(preview) == PREVIEW_KEYS
    assert (preview["findings"], preview["tickets"], preview["already_ignored"]) == (3, len(tickets), 0)
    assert preview["by_rule"] == [{"rule_id": "R-EST-01", "rule_title": CATALOGUE.title_of("R-EST-01"), "findings": 3}]
    assert everything(board) == before  # a preview writes nothing

    done = post(client, IGNORE, {**body, "reason": "Closed before estimates were asked for"})
    assert set(done) == {"meta", "ignored", "ignore_ids"}
    assert done["ignored"] == {"findings": preview["findings"], "tickets": preview["tickets"]}
    assert len(done["ignore_ids"]) == 3 and all(HEX32.match(ignore_id) for ignore_id in done["ignore_ids"])
    stored = rows(board, "SELECT finding_id, resolved_at, resolved_why FROM findings WHERE resolved_at IS NOT NULL")
    assert sorted(stored) == [(finding_id, to_iso(LATER), "ignored") for finding_id in picked]
    kept = rows(board, "SELECT project_key, rule_id, scope, reason, created_by, created_at, restored_by, restored_at"
                       " FROM finding_ignores")
    assert kept == [("DEMO", "R-EST-01", "finding", "Closed before estimates were asked for", ADMIN_NAME,
                     to_iso(LATER), None, None)] * 3

    listed = every_page(client, {"state": "ignored", "level": "people"})
    assert sorted(item["finding_id"] for item in listed) == picked
    by_id = dict(zip(done["ignore_ids"], rows(board, "SELECT issue_key FROM finding_ignores"), strict=False))
    for item in listed:
        assert (item["state"], item["resolved_why"], item["resolved_at"]) == ("ignored", "ignored", to_iso(LATER))
        ignore = item["ignore"]
        assert ignore == {"ignore_id": ignore["ignore_id"], "scope": "finding", "rule_id": "R-EST-01",
                          "reason": "Closed before estimates were asked for", "by": ADMIN_NAME, "at": to_iso(LATER)}
        assert ignore["ignore_id"] in by_id
    assert all(item["ignore"]["by"] is None for item in every_page(client, {"state": "ignored"}))  # team level
    drawer = get(client, f"/findings/{picked[0]}", {"level": "people"})["finding"]
    assert drawer["state"] == "ignored" and drawer["ignore"]["by"] == ADMIN_NAME
    assert get(client, f"/findings/{picked[0]}")["finding"]["ignore"]["by"] is None
    assert all(item["ignore"] is None for item in every_page(client, {"state": "all"}) if item["state"] != "ignored")

    again = post(client, PREVIEW, body)
    assert (again["findings"], again["tickets"], again["already_ignored"]) == (0, 0, 3)
    assert post(client, IGNORE, body)["ignore_ids"] == []  # the same pairs: reused, never doubled
    assert rows(board, "SELECT COUNT(*) FROM finding_ignores")[0][0] == 3


def test_ignoring_a_ticket_ignores_every_rule_on_it(board: Settings) -> None:
    client, _ = serve(board)
    on_ticket = ids_of(board, "issue_key = 'DEMO-541' AND resolved_at IS NULL")
    rules = Counter(row[0] for row in rows(board, "SELECT rule_id FROM findings WHERE issue_key = 'DEMO-541'"))
    assert len(rules) >= 3
    refused = client.post(IGNORE, json={"target": {"issue_keys": ["DEMO-541"]}, "scope": "finding"})
    assert refused.status_code == 422 and refused.json()["code"] == "ignore_invalid"
    assert refused.json()["errors"] == [{"field": "scope", "message": refused.json()["detail"]}]

    body = {"target": {"issue_keys": ["DEMO-541", "DEMO-99999"]}, "scope": "ticket"}  # the second: not on the board
    preview = post(client, PREVIEW, body)
    assert (preview["findings"], preview["tickets"], preview["already_ignored"]) == (len(on_ticket), 1, 0)
    order = [rule.id for rule in CATALOGUE.rules if rule.id in rules]
    assert [(entry["rule_id"], entry["findings"]) for entry in preview["by_rule"]] == [(r, rules[r]) for r in order]

    done = post(client, IGNORE, body)
    assert done["ignored"] == {"findings": len(on_ticket), "tickets": 1} and len(done["ignore_ids"]) == 1
    assert rows(board, "SELECT ignore_id, issue_key, rule_id, scope FROM finding_ignores") == [
        (done["ignore_ids"][0], "DEMO-541", None, "ticket")]
    assert ids_of(board, "resolved_why = 'ignored'") == on_ticket
    timeline = get(client, "/issues/DEMO-541/timeline", {"level": "people"})["findings"]
    assert {item["finding_id"] for item in timeline} == on_ticket
    assert all(item["state"] == "ignored" and item["ignore"]["scope"] == "ticket" and item["ignore"]["rule_id"] is None
               and item["ignore"]["by"] == ADMIN_NAME for item in timeline)
    assert all(item["ignore"]["by"] is None for item in get(client, "/issues/DEMO-541/timeline")["findings"])


def test_a_ticket_with_open_and_ignored_findings_lists_the_open_first(board: Settings) -> None:
    client, _ = serve(board)
    [(first, *_rest)] = rows(board, "SELECT finding_id FROM findings WHERE issue_key = 'DEMO-541' ORDER BY rule_id LIMIT 1")
    post(client, IGNORE, {"target": {"finding_ids": [first]}, "scope": "finding"})
    states = [item["state"] for item in get(client, "/issues/DEMO-541/timeline")["findings"]]
    assert states[-1] == "ignored" and states.count("ignored") == 1 and set(states[:-1]) == {"open"}


def test_ignoring_the_findings_on_finished_tickets(board: Settings) -> None:
    client, _ = serve(board)
    done_sql = "issue_key IN (SELECT issue_key FROM issues WHERE status_category = 'done')"
    finished = ids_of(board, f"{OPEN} AND {done_sql}")
    unfinished = ids_of(board, f"{OPEN} AND NOT {done_sql}")
    assert len(finished) > 400 and unfinished
    assert get(client, "/findings", {"ticket": "finished"})["total"] == len(finished)
    assert get(client, "/findings", {"ticket": "open"})["total"] == len(unfinished)  # a sprint finding is no ticket's
    sprint = ids_of(board, "issue_key LIKE 'sprint:%'")
    assert sprint and get(client, "/findings")["total"] == len(finished) + len(unfinished) + len(sprint)

    body = {"target": {"query": {"ticket": "finished"}}, "scope": "finding"}
    preview = post(client, PREVIEW, body)
    done = post(client, IGNORE, body)
    assert done["ignored"] == {"findings": preview["findings"], "tickets": preview["tickets"]}
    assert preview["findings"] == len(finished)
    assert preview["tickets"] == len({row[0] for row in rows(board, f"SELECT issue_key FROM findings WHERE {done_sql}"
                                                                    " AND resolved_why = 'ignored'")})
    by_rule = Counter(row[0] for row in rows(board, "SELECT rule_id FROM findings WHERE resolved_why = 'ignored'"))
    assert {entry["rule_id"]: entry["findings"] for entry in preview["by_rule"]} == by_rule
    assert ids_of(board, "resolved_why = 'ignored'") == finished
    assert ids_of(board, "resolved_at IS NULL") == unfinished | sprint

    assert get(client, "/findings", {"ticket": "finished"})["total"] == 0
    assert get(client, "/findings", {"ticket": "finished", "state": "ignored"})["total"] == len(finished)
    page = get(client, "/findings", {"state": "all"})
    states = {entry["value"]: entry["count"] for entry in page["facets"]["state"]}
    assert states == {"open": len(unfinished) + len(sprint), "resolved": 0, "ignored": len(finished)}
    assert page["total"] == sum(states.values())
    assert get(client, "/findings", {"state": "resolved"})["total"] == 0  # ignored is never resolved
    after = post(client, PREVIEW, body)
    assert (after["findings"], after["already_ignored"]) == (0, len(finished))


def test_a_query_target_names_the_findings_lists_filters(board: Settings) -> None:
    client, _ = serve(board)
    query = {"rule": ["R-EFF-01", "W-EFF-01"], "severity": "breach", "first_seen": "7d", "q": None, "epic": []}
    expected = ids_of(board, f"rule_id = 'R-EFF-01' AND {OPEN}")
    listed = {item["finding_id"] for item in every_page(
        client, [("rule", "R-EFF-01"), ("rule", "W-EFF-01"), ("severity", "breach"), ("first_seen", "7d")])}
    assert listed == expected
    assert post(client, IGNORE, {"target": {"query": query}, "scope": "finding"})["ignored"]["findings"] == len(expected)
    assert ids_of(board, "resolved_why = 'ignored'") == expected
    nothing = post(client, IGNORE, {"target": {"query": {"q": "no summary holds these words"}}, "scope": "ticket"})
    assert nothing["ignored"] == {"findings": 0, "tickets": 0} and nothing["ignore_ids"] == []

    for bad, field in (({"rule": "R-EFF-01"}, "target.query.rule"), ({"state": "open"}, "target.query.state"),
                       ({"ticket": "closed"}, "target.query.ticket"), ({"person": ["someone"]}, "target.query.person.0")):
        refused = client.post(PREVIEW, json={"target": {"query": bad}, "scope": "finding"})
        assert refused.status_code == 422 and refused.json()["code"] == "ignore_invalid", bad
        assert field in [error["field"] for error in refused.json()["errors"]], refused.json()


# --- every count ------------------------------------------------------------------------------------------------------


def snapshot_counts(client: TestClient, settings: Settings) -> dict[str, Any]:
    """Every open, new, resolved and fixed figure the service shows of DEMO, from every reader."""
    overview = get(client, "/overview")
    totals = {name: total["current"] for name, total in overview["totals"].items()}
    row = get(client, "/portfolio")["boards"][0]
    people = get(client, "/people")["people"]
    findings = get(client, "/findings")
    states = {entry["value"]: entry["count"] for entry in get(client, "/findings", {"state": "all"})["facets"]["state"]}
    scorecard = get(client, "/people/scorecard", {"level": "people"})["board"]["values"]["open_findings_per_10"]
    conn = open_readonly(settings.db_file)
    try:
        ctx = CatalogContext(process=PROCESS, rules=CATALOGUE, project="DEMO", now=LATER, row_limit=200)
        rule_status = run_query(conn, "oversight_rule_status", {}, ctx, ref="F1").rows
        resolved = run_query(conn, "oversight_list_findings", {"state": "resolved", "n": 50}, ctx, ref="F2").rows
        trend = run_query(conn, "oversight_trend", {"metric": "open_breaches", "last_n": 2}, ctx, ref="F3").rows
    finally:
        conn.close()
    analyst = SnapshotQuery(settings.db_file, project="DEMO", sprint_field=None, rules=CATALOGUE)
    try:
        model = analyst.run("SELECT SUM(is_open), SUM(1 - is_open), COUNT(*) FROM findings")[1][0]
    finally:
        analyst.close()
    mcp = payload(call(build_server(settings, PROCESS, CATALOGUE, db_path=settings.db_file), "oversight_rule_status", {}))
    with Store.open(settings.db_file) as store:
        digest = build_digest(store, PROCESS, CATALOGUE, project="DEMO", now=LATER)
        candidates = build_candidates(store, PROCESS, CATALOGUE, project="DEMO", now=LATER)
    return {
        "open_breaches": totals["open_breaches"], "open_warnings": totals["open_warnings"],
        "new_7d": totals["new_7d"], "resolved_7d": totals["resolved_7d"],
        "by_rule": {rule["rule_id"]: rule["open"] for rule in overview["by_rule"]},
        "hot_spot_ids": {spot["epic_key"]: spot["open_breaches"] + spot["open_warnings"] for spot in overview["hot_spots"]},
        "trend_breaches": get(client, "/trends", {"metric": "open_breaches"})["series"][-1]["value"],
        "trend_warnings": get(client, "/trends", {"metric": "open_warnings"})["series"][-1]["value"],
        "portfolio": (row["open_breaches"], row["open_warnings"], row["new_findings_7d"], row["resolved_findings_7d"],
                      row["open_findings_trend"][-1]),
        "attention": {(item["issue_key"], item["rule_id"]) for item in get(client, "/portfolio")["attention"]},
        "people": (sum(p["open_breaches"] for p in people), sum(p["open_warnings"] for p in people)),
        "findings_total": findings["total"],
        "states": states,
        "scorecard": (scorecard["numerator"], scorecard["denominator"]),
        "ask_rule_status": {r["rule_id"]: r["open_findings"] for r in rule_status},
        "ask_resolved": len(resolved),
        "ask_trend": trend[-1]["value"],
        "analyst": tuple(int(value or 0) for value in model),
        "mcp_rule_status": {r["rule_id"]: r["open_findings"] for r in mcp["rows"]},
        "digest": (_section(digest, "new_breaches"), _section(digest, "new_warnings"), _section(digest, "resolved")),
        "digest_spots": {spot.epic_key: spot.open_breaches + spot.open_warnings for spot in digest.hot_spots},
        "insight_ids": {finding_id for c in candidates if c.kind == "breach_hot_spot" for finding_id in c.finding_ids},
    }


def _section(content: DigestContent, name: str) -> int:
    return len(getattr(content, name)) + content.truncated.get(name, 0)


def test_every_open_count_drops_by_exactly_the_ignored_findings_and_no_other_count_moves(board: Settings) -> None:
    client, _ = serve(board)
    before = snapshot_counts(client, board)
    epic = next(iter(before["hot_spot_ids"]))
    tickets = [row[0] for row in rows(board, "SELECT issue_key FROM issues WHERE epic_key = ? ORDER BY issue_key LIMIT 6",
                                      (epic,))]
    post(client, IGNORE, {"target": {"issue_keys": tickets}, "scope": "ticket"})
    post(client, IGNORE, {"target": {"query": {"rule": ["W-AGE-01", "R-FLOW-02"], "ticket": "open"}}, "scope": "finding"})
    ignored = rows(board, "SELECT finding_id, rule_id, severity, issue_key FROM findings WHERE resolved_why = 'ignored'")
    breaches = sum(severity == "breach" for _id, _rule, severity, _key in ignored)
    warnings = len(ignored) - breaches
    assert breaches and warnings
    on_open_tickets = rows(board, "SELECT COUNT(*) FROM findings AS f JOIN issues AS i ON i.issue_key = f.issue_key"
                                  " WHERE f.resolved_why = 'ignored' AND i.status_category != 'done'")[0][0]
    whole_open = rows(board, "SELECT COUNT(*) FROM issues WHERE issue_key IN (SELECT issue_key FROM finding_ignores"
                             " WHERE rule_id IS NULL) AND status_category != 'done' AND issue_type != 'Epic'")[0][0]
    per_rule = Counter(rule for _id, rule, _severity, _key in ignored)
    after = snapshot_counts(client, board)

    assert after["open_breaches"] == before["open_breaches"] - breaches
    assert after["open_warnings"] == before["open_warnings"] - warnings
    assert after["new_7d"] == before["new_7d"] - len(ignored)  # every finding was first seen at the seed's run
    assert after["resolved_7d"] == before["resolved_7d"] == 0  # never fixed
    assert after["by_rule"] == {rule: count - per_rule[rule] for rule, count in before["by_rule"].items()}
    assert after["trend_breaches"] == before["trend_breaches"] - breaches
    assert after["trend_warnings"] == before["trend_warnings"] - warnings
    assert after["portfolio"] == (before["portfolio"][0] - breaches, before["portfolio"][1] - warnings,
                                  before["portfolio"][2] - len(ignored), before["portfolio"][3],
                                  before["portfolio"][4] - len(ignored))
    assert not after["attention"] & {(key, rule) for _id, rule, _severity, key in ignored}
    assert after["people"] == (before["people"][0] - breaches, before["people"][1] - warnings)
    assert after["findings_total"] == before["findings_total"] - len(ignored)
    assert after["states"] == {"open": before["states"]["open"] - len(ignored), "resolved": before["states"]["resolved"],
                               "ignored": len(ignored)}
    # Ignored findings are not open; a whole ticket's ignore also leaves the ticket out of the open tickets counted.
    assert after["scorecard"] == (before["scorecard"][0] - on_open_tickets, before["scorecard"][1] - whole_open)
    assert after["ask_rule_status"] == {rule: count - per_rule[rule] for rule, count in before["ask_rule_status"].items()}
    assert after["mcp_rule_status"] == after["ask_rule_status"]
    assert after["ask_resolved"] == before["ask_resolved"] == 0
    assert after["ask_trend"] == before["ask_trend"] - breaches
    assert after["analyst"] == (before["analyst"][0] - len(ignored), before["analyst"][1], before["analyst"][2] - len(ignored))
    assert after["digest"] == (before["digest"][0] - breaches, before["digest"][1] - warnings, 0)
    by_epic = Counter(row[0] for row in rows(
        board, "SELECT COALESCE(i.epic_key, p.epic_key) FROM findings AS f JOIN issues AS i ON i.issue_key = f.issue_key"
               " LEFT JOIN issues AS p ON p.issue_key = i.parent_key WHERE f.resolved_why = 'ignored'"))
    shared = set(before["digest_spots"]) & set(after["digest_spots"])
    assert any(by_epic[epic] for epic in shared)  # a hot spot the digest lists before and after held ignored findings
    assert {epic: after["digest_spots"][epic] for epic in shared} == {
        epic: before["digest_spots"][epic] - by_epic[epic] for epic in shared}
    assert before["insight_ids"] & {finding_id for finding_id, *_ in ignored}  # the hot spot held ignored breaches
    assert not after["insight_ids"] & {finding_id for finding_id, *_ in ignored}


def test_a_rules_run_keeps_ignored_findings_ignored(board: Settings) -> None:
    client, _ = serve(board)
    post(client, IGNORE, {"target": {"issue_keys": ["DEMO-541"]}, "scope": "ticket"})
    post(client, IGNORE, {"target": {"query": {"rule": ["R-HYG-01"]}}, "scope": "finding"})
    held = rows(board, "SELECT finding_id, first_seen_at, resolved_at FROM findings WHERE resolved_why = 'ignored' ORDER BY 1")
    open_now = ids_of(board, "resolved_at IS NULL")

    run_the_rules(board, ["DEMO"], LATER + timedelta(hours=1))
    assert rows(board, "SELECT finding_id, first_seen_at, resolved_at FROM findings WHERE resolved_why = 'ignored'"
                       " ORDER BY 1") == held  # since when, unchanged
    assert {row[0] for row in rows(board, "SELECT last_seen_at FROM findings WHERE resolved_why = 'ignored'")} == {
        to_iso(LATER + timedelta(hours=1))}  # the rules still hold them
    assert ids_of(board, "resolved_at IS NULL") == open_now
    stats = json.loads(rows(board, "SELECT stats_json FROM runs WHERE kind = 'rules' ORDER BY started_at DESC")[0][0])
    assert (stats["DEMO"]["ignored"], stats["DEMO"]["opened"]) == (len(held), 0)


# --- restore ----------------------------------------------------------------------------------------------------------


def test_restoring_rechecks_the_board_and_reopens_the_same_findings(board: Settings) -> None:
    clock = Clock(LATER)
    steps = Steps(board, clock)
    client, refresher = serve(board, clock=clock, steps=steps)
    first_seen = dict(rows(board, "SELECT finding_id, first_seen_at FROM findings WHERE rule_id = 'R-EFF-01'"))
    done = post(client, IGNORE, {"target": {"query": {"rule": ["R-EFF-01"]}}, "scope": "finding"})
    assert done["ignored"]["findings"] == len(first_seen) and not ids_of(board, "rule_id = 'R-EFF-01' AND resolved_at IS NULL")

    clock.at = LATER + timedelta(minutes=10)
    restored = post(client, RESTORE, {"ignore_ids": [*done["ignore_ids"], "f" * 32]})  # one unknown: not counted
    assert set(restored) == {"meta", "restored", "recheck"} and restored["restored"] == len(done["ignore_ids"])
    assert set(restored["recheck"]) == {"state", "applied_revision", "started_at", "finished_at", "message", "result"}
    assert restored["recheck"]["state"] in ("running", "applied")
    settle(refresher)
    assert steps.calls == [("rules", "--project", "DEMO")]

    assert rows(board, "SELECT finding_id, first_seen_at, resolved_at, resolved_why FROM findings"
                       " WHERE rule_id = 'R-EFF-01' ORDER BY 1") == [
        (finding_id, seen, None, None) for finding_id, seen in sorted(first_seen.items())]
    assert set(rows(board, "SELECT restored_by, restored_at FROM finding_ignores")) == {
        (ADMIN_NAME, to_iso(LATER + timedelta(minutes=10)))}
    assert get(client, "/findings/ignored", {"level": "people"})["total"] == 0
    state = get(client, "/rules/settings", {"level": "people"})["apply"]
    assert state["state"] == "applied"

    again = post(client, RESTORE, {"ignore_ids": done["ignore_ids"]})
    assert again["restored"] == 0 and steps.calls == [("rules", "--project", "DEMO")]  # nothing to re-check


def test_two_ignores_at_once_make_one_ignore_for_each_ticket_and_rule(board: Settings) -> None:
    client, _ = serve(board)
    twin = TestClient(client.app, headers=headers())
    wanted = ids_of(board, f"rule_id = 'R-EST-01' AND {OPEN}")
    bodies = [{"target": {"query": {"rule": ["R-EST-01"]}}, "scope": "finding"},
              {"target": {"finding_ids": sorted(wanted)}, "scope": "finding"}]
    start = threading.Barrier(2)
    answers: list[Any] = [None, None]

    def ignore(index: int, through: TestClient) -> None:
        start.wait()
        answers[index] = through.post(IGNORE, json=bodies[index])

    threads = [threading.Thread(target=ignore, args=(n, c)) for n, c in enumerate((client, twin))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert [answer.status_code for answer in answers] == [200, 200], [answer.text for answer in answers]
    found = [answer.json() for answer in answers]
    assert sum(body["ignored"]["findings"] for body in found) == len(wanted)  # each finding ignored once
    made = [ignore_id for body in found for ignore_id in body["ignore_ids"]]
    pairs = rows(board, "SELECT issue_key, rule_id FROM finding_ignores WHERE restored_at IS NULL")
    assert len(made) == len(set(made)) == len(pairs) == len(set(pairs))
    assert ids_of(board, "resolved_why = 'ignored'") == wanted


@pytest.mark.slow
def test_ten_thousand_findings_are_ignored_in_under_three_seconds(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, jira_projects="DEMO", api_service_key=KEY, db_path=tmp_path / "oversight.db",
                        config_dir=CONFIG_FIXTURES, reports_dir=tmp_path / "reports")
    seen = to_iso(NOW)
    with Store.open(settings.db_file) as store, store.transaction():
        store.set_cursor("DEMO", last_synced_at=seen or "", run_id="r1", account_timezone=None)
        store.conn.executemany(
            "INSERT INTO issues (issue_key, issue_id, project_key, issue_type, summary, status, status_category)"
            " VALUES (?, ?, 'DEMO', 'Task', ?, 'Done', 'done')",
            [(f"DEMO-{n}", str(n), f"Closed ticket {n}") for n in range(1, 5001)])
        store.conn.executemany(
            "INSERT INTO findings (finding_id, rule_id, rule_version, issue_key, project_key, kind, severity, evidence_key,"
            " message, evidence_json, first_seen_at, last_seen_at, run_id) VALUES (?, ?, '0.1.0', ?, 'DEMO', 'rule',"
            " 'breach', 'k', 'No estimate', '{}', ?, ?, 'r1')",
            [(f"{n:016x}{rule:016x}", ("R-EST-01", "R-EFF-01")[rule], f"DEMO-{n}", seen, seen)
             for n in range(1, 5001) for rule in (0, 1)])
    client, _ = serve(settings, clock=Clock(LATER))
    body = {"target": {"query": {"ticket": "finished"}}, "scope": "finding", "reason": "Closed long ago"}
    started = time.perf_counter()
    done = post(client, IGNORE, body)
    elapsed = time.perf_counter() - started
    assert done["ignored"] == {"findings": 10_000, "tickets": 5_000} and len(done["ignore_ids"]) == 10_000
    assert elapsed < 3.0, f"ignoring 10,000 findings took {elapsed:.2f} s"
    started = time.perf_counter()  # 10,000 ids are more than 256 KB of JSON: one undo takes them all
    restored = post(client, RESTORE, {"ignore_ids": done["ignore_ids"]})["restored"]
    assert restored == 10_000 and time.perf_counter() - started < 3.0


# --- what a request may hold --------------------------------------------------------------------------------------------


def test_the_reason_is_kept_as_plain_text(board: Settings) -> None:
    client, _ = serve(board)
    picked = sorted(ids_of(board, f"rule_id = 'W-EST-01' AND {OPEN}"))
    messy = "  Closed\tbefore\r\nthe estimates rule \x00 existed‏  "
    post(client, IGNORE, {"target": {"finding_ids": picked[:1]}, "scope": "finding", "reason": messy})
    post(client, IGNORE, {"target": {"finding_ids": picked[1:2]}, "scope": "finding", "reason": " \n\t "})
    post(client, IGNORE, {"target": {"finding_ids": picked[2:3]}, "scope": "finding", "reason": "a" * 500})
    reasons = {row[0]: row[1] for row in rows(
        board, "SELECT f.finding_id, g.reason FROM findings AS f JOIN finding_ignores AS g"
               " ON g.issue_key = f.issue_key AND g.rule_id = f.rule_id")}
    assert [reasons[finding_id] for finding_id in picked[:3]] == [
        "Closed before the estimates rule existed", None, "a" * 500]
    for reason, words in (("a" * 501, "at most 500 characters"), ("Ask jo@example.com", "email address"),
                          ("5b10ac8d82e05b22cc7d4ef5", "account id"), (12, "must be text")):
        refused = client.post(IGNORE, json={"target": {"finding_ids": picked[3:4]}, "scope": "finding", "reason": reason})
        assert refused.status_code == 422, reason
        assert refused.json()["errors"] == [{"field": "reason", "message": refused.json()["detail"]}]
        assert words in refused.json()["detail"]
    assert rows(board, "SELECT COUNT(*) FROM finding_ignores")[0][0] == 3  # a refused request writes nothing


def test_the_viewer_is_kept_by_display_name_never_an_email(board: Settings) -> None:
    client, _ = serve(board)
    picked = sorted(ids_of(board, f"rule_id = 'W-EST-01' AND {OPEN}"))
    for n, (viewer, name) in enumerate(((ADMIN, "jo@example.com"), ("local", "Anyone"), (ADMIN, "Ana Lima"))):
        response = client.post(IGNORE, json={"target": {"finding_ids": [picked[n]]}, "scope": "finding"},
                               headers=headers(viewer, name))
        assert response.status_code == 200, response.text
    assert sorted(row[0] for row in rows(board, "SELECT created_by FROM finding_ignores")) == [
        "Ana Lima", "Local (sign-in off)", "Signed-in user"]
    ids = [row[0] for row in rows(board, "SELECT ignore_id FROM finding_ignores")]
    response = client.post(RESTORE, json={"ignore_ids": ids}, headers=headers(ADMIN, "Ben Okafor"))
    assert response.status_code == 200 and {row[0] for row in rows(board, "SELECT restored_by FROM finding_ignores")} == {
        "Ben Okafor"}


@pytest.mark.parametrize("path", [PREVIEW, IGNORE, RESTORE])
def test_every_post_is_for_admins_at_people_level_with_a_body_the_contract_allows(board: Settings, path: str) -> None:
    client, _ = serve(board)
    body = ({"ignore_ids": ["a" * 32]} if path == RESTORE
            else {"target": {"finding_ids": ["a" * 32]}, "scope": "finding"})
    before = everything(board)
    team = client.post(path.replace("level=people", "level=team"), json=body)
    assert team.status_code == 403 and team.json()["code"] == "people_level_only"
    assert client.post(path.split("?")[0], json=body).status_code == 403  # no level: team
    nameless = TestClient(client.app, headers={"X-Service-Key": KEY})
    assert nameless.post(path, json=body).status_code == 422  # no viewer
    assert client.post(f"{path}&page=2", json=body).status_code == 422
    assert client.post(path, json=body, headers={"X-Service-Key": "wrong"}).status_code == 401
    for bad in (b"[]", b"not json", b'{"target": NaN}', json.dumps({**body, "extra": 1}).encode()):
        refused = client.post(path, content=bad, headers={"Content-Type": "application/json"})
        assert refused.status_code == 422 and refused.json()["code"] == "ignore_invalid", (bad, refused.text)
    cap = 1024 if path == RESTORE else 256
    huge = json.dumps({**body, "reason": "a" * (cap * 1024)}).encode()
    refused = client.post(path, content=huge, headers={"Content-Type": "application/json"})
    assert refused.status_code == 422 and f"{cap} KB" in refused.json()["detail"]
    assert everything(board) == before


@pytest.mark.parametrize(("body", "field"), [
    ({"scope": "finding"}, "target"),
    ({"target": {"finding_ids": []}, "scope": "finding"}, "target.finding_ids"),
    ({"target": {"issue_keys": [f"DEMO-{n}" for n in range(1, 10_002)]}, "scope": "ticket"}, "target.issue_keys"),
    ({"target": {"finding_ids": ["DEMO-1"]}, "scope": "finding"}, "target.finding_ids"),
    ({"target": {"issue_keys": ["demo-1"]}, "scope": "ticket"}, "target.issue_keys"),
    ({"target": {"finding_ids": ["a" * 32], "issue_keys": ["DEMO-1"]}, "scope": "ticket"}, "target"),
    ({"target": {"tickets": ["DEMO-1"]}, "scope": "ticket"}, "target.tickets"),
    ({"target": {"query": []}, "scope": "ticket"}, "target.query"),
    ({"target": {"finding_ids": ["a" * 32]}, "scope": "rule"}, "scope"),
    ({"target": {"finding_ids": ["a" * 32]}}, "scope"),
])
def test_a_body_the_contract_does_not_allow_is_refused_at_its_field(board: Settings, body: dict[str, Any],
                                                                    field: str) -> None:
    client, _ = serve(board)
    for path in (PREVIEW, IGNORE):
        refused = client.post(path, json=body)
        assert refused.status_code == 422, refused.text
        assert field in [error["field"] for error in refused.json()["errors"]], refused.json()


def test_a_restore_takes_ignore_ids_only(board: Settings) -> None:
    client, _ = serve(board)
    for body in ({}, {"ignore_ids": []}, {"ignore_ids": ["A" * 32]}, {"ignore_ids": ["a" * 32] * 25_001},
                 {"ignore_ids": ["a" * 32], "reason": "x"}):
        refused = client.post(RESTORE, json=body)
        assert refused.status_code == 422 and refused.json()["code"] == "ignore_invalid", body


# --- the list of ignores ----------------------------------------------------------------------------------------------


def test_the_ignored_list_says_what_each_ignore_holds(board: Settings) -> None:
    clock = Clock(LATER)
    client, _ = serve(board, clock=clock)
    on_ticket = ids_of(board, "issue_key = 'DEMO-541'")
    post(client, IGNORE, {"target": {"issue_keys": ["DEMO-541"]}, "scope": "ticket", "reason": "Old work"})
    clock.at = LATER + timedelta(minutes=1)
    hygiene = ids_of(board, f"rule_id = 'R-HYG-01' AND {OPEN}")
    post(client, IGNORE, {"target": {"query": {"rule": ["R-HYG-01"]}}, "scope": "finding"})

    page = get(client, "/findings/ignored", {"level": "people"})
    assert set(page) == {"meta", "items", "total", "page", "page_size"}
    hygiene_tickets = {row[0] for row in rows(board, "SELECT issue_key FROM finding_ignores WHERE rule_id = 'R-HYG-01'")}
    assert (page["total"], page["page"], page["page_size"]) == (1 + len(hygiene_tickets), 1, 25)
    first = page["items"][-1]  # newest first: the ticket's ignore was made first
    assert set(first) == IGNORE_KEYS
    summary, issue_type, status, category = rows(
        board, "SELECT summary, issue_type, status, status_category FROM issues WHERE issue_key = 'DEMO-541'")[0]
    assert first == {
        "ignore_id": first["ignore_id"], "scope": "ticket", "issue_key": "DEMO-541", "issue_summary": summary,
        "issue_type": issue_type, "status": status, "status_category": category, "rule_id": None, "rule_title": None,
        "findings": len(on_ticket), "reason": "Old work", "by": ADMIN_NAME, "at": to_iso(LATER)}
    assert all(item["scope"] == "finding" and item["rule_id"] == "R-HYG-01" and item["rule_title"]
               == CATALOGUE.title_of("R-HYG-01") and item["at"] == to_iso(LATER + timedelta(minutes=1))
               for item in page["items"][:-1])
    assert sum(item["findings"] for item in page["items"]) == len(on_ticket | hygiene)

    by_rule = get(client, "/findings/ignored", {"level": "people", "rule": "R-HYG-01"})
    held_hygiene = bool(ids_of(board, "issue_key = 'DEMO-541' AND rule_id = 'R-HYG-01'"))
    assert by_rule["total"] == len(hygiene_tickets) + held_hygiene  # the whole ticket's, when it holds one
    held = {row[0] for row in rows(board, "SELECT rule_id FROM findings WHERE issue_key = 'DEMO-541'")}
    whole = get(client, "/findings/ignored", [("level", "people"), ("rule", sorted(held)[0])])
    assert "DEMO-541" in {item["issue_key"] for item in whole["items"]}  # a whole ticket's ignore holds its rule's
    assert [item["issue_key"] for item in get(client, "/findings/ignored", {"level": "people", "q": "demo-541"})[
        "items"]] == ["DEMO-541"]
    assert get(client, "/findings/ignored", {"level": "people", "q": "541"})["total"] >= 1
    keys = [item["issue_key"] for item in get(client, "/findings/ignored", {"level": "people", "sort": "issue_key",
                                                                             "order": "asc", "page_size": 100})["items"]]
    assert keys == sorted(keys, key=lambda key: int(key.rsplit("-", 1)[1]))
    second = get(client, "/findings/ignored", {"level": "people", "page": 2, "page_size": 1})
    assert len(second["items"]) == 1 and second["items"][0] == page["items"][1]

    assert client.get(IGNORED.replace("people", "team")).status_code == 403
    assert TestClient(client.app, headers={"X-Service-Key": KEY}).get(IGNORED).status_code == 422  # no viewer
    for bad in ({"page_size": 101}, {"sort": "reason"}, {"order": "up"}, {"rule": "R-1"}, {"state": "open"}):
        assert client.get("/api/v1/findings/ignored", params={"level": "people", **bad}).status_code == 422, bad
    assert client.get("/api/v1/findings/ignored", params=[("level", "people"), ("q", "a"), ("q", "b")]).status_code == 422


def test_a_finding_that_clears_while_its_ignore_holds_stays_ignored_under_it(board: Settings) -> None:
    client, _ = serve(board)
    [(finding, key)] = rows(board, f"SELECT finding_id, issue_key FROM findings WHERE rule_id = 'R-HYG-01' AND {OPEN}"
                                   " ORDER BY finding_id LIMIT 1")
    done = post(client, IGNORE, {"target": {"finding_ids": [finding]}, "scope": "finding"})
    with Store.open(board.db_file) as store, store.transaction():  # someone is assigned: R-HYG-01 no longer holds
        store.conn.execute("UPDATE issues SET assignee_id = 'acc-01' WHERE issue_key = ?", (key,))

    run_the_rules(board, ["DEMO"], LATER + timedelta(hours=1))

    assert rows(board, "SELECT resolved_at, resolved_why FROM findings WHERE finding_id = ?", (finding,)) == [
        (to_iso(LATER), "ignored")]  # neither fixed nor open: still ignored, since when it was
    [listed] = get(client, "/findings/ignored", {"level": "people"})["items"]
    assert (listed["ignore_id"], listed["findings"]) == (done["ignore_ids"][0], 1)


def test_a_rule_settings_preview_never_counts_an_ignored_ticket_as_opening(board: Settings) -> None:
    clock = Clock(LATER)
    client, refresher = serve(board, clock=clock, steps=Steps(board, clock))
    stuck = sorted({row[0] for row in rows(board, f"SELECT issue_key FROM findings WHERE rule_id = 'W-AGE-01' AND {OPEN}")})
    assert len(stuck) > 2
    off = post(client, "/api/v1/rules/settings?level=people", {"revision": 0,
                                                              "changes": [{"rule_id": "W-AGE-01", "enabled": False}]})
    settle(refresher)
    assert not ids_of(board, f"rule_id = 'W-AGE-01' AND {OPEN}")
    post(client, IGNORE, {"target": {"issue_keys": stuck[:2]}, "scope": "ticket"})

    change = {"revision": off["revision"], "changes": [{"rule_id": "W-AGE-01", "enabled": True}]}
    preview = post(client, "/api/v1/rules/settings/preview?level=people", change)
    [impact] = [entry for entry in preview["impact"] if entry["rule_id"] == "W-AGE-01"]
    assert impact["would_open"] == len(stuck) - 2

    clock.at = LATER + timedelta(minutes=5)
    post(client, "/api/v1/rules/settings?level=people", change)
    settle(refresher)
    assert len(ids_of(board, f"rule_id = 'W-AGE-01' AND {OPEN}")) == impact["would_open"]
    assert {row[0] for row in rows(board, "SELECT issue_key FROM findings WHERE rule_id = 'W-AGE-01'"
                                          " AND resolved_why = 'ignored'")} == set(stuck[:2])
