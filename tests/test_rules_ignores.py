from __future__ import annotations

import sqlite3
from datetime import timedelta

from oversight.config import FieldsConfig, ProcessConfig
from oversight.rules.engine import RuleStats, RulesResult, finding_id
from oversight.rules.ignores import (
    IgnoreInfo,
    Ignores,
    active_ignores,
    add_ignores,
    resolve_ignored,
    restore_ignores,
)
from oversight.store.db import Store
from tests.rules_helpers import NOW, add_issue, at, catalogue, derive, run_at

STARTED = [(-48.0, "To Do", "In Progress")]
ADMIN = "Morgan Reyes"


def _row(store: Store, issue_key: str, rule_id: str) -> sqlite3.Row:
    [row] = store.query("SELECT * FROM findings WHERE issue_key = ? AND rule_id = ?", (issue_key, rule_id))
    return row


def _stat(result: RulesResult, rule_id: str) -> RuleStats:
    return next(s for s in result.stats if s.rule_id == rule_id)


def _ignore(store: Store, *pairs: tuple[str, str | None], hours: float = 0.5) -> list[str]:
    """Ignore these (ticket, rule or None) pairs as the API does: the ignores, and their open findings resolved."""
    with store.transaction():
        made = add_ignores(store.conn, "AIL", pairs, reason=None, by=ADMIN, at=at(hours))
        held = active_ignores(store.conn, "AIL")
        open_rows = store.query("SELECT finding_id, issue_key, rule_id FROM findings WHERE resolved_at IS NULL")
        resolve_ignored(store.conn, "AIL", [row["finding_id"] for row in open_rows
                                            if held.covers(row["issue_key"], (row["rule_id"],))], at(hours))
    return made


def test_a_finding_that_starts_on_an_ignored_ticket_is_recorded_already_ignored(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, points=1)
    derive(store, process)
    run_at(store, process, fields)
    assert store.count("findings", {"issue_key": "AIL-1"}) == 0
    _ignore(store, ("AIL-1", None))

    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, points=1, assignee=None)  # unassigned now
    [result] = run_at(store, process, fields, now=NOW + timedelta(hours=1))

    row = _row(store, "AIL-1", "R-HYG-01")
    assert (row["resolved_at"], row["resolved_why"], row["first_seen_at"]) == (at(1), "ignored", at(1))
    hygiene = _stat(result, "R-HYG-01")
    assert (hygiene.hits, hygiene.opened, hygiene.refreshed, hygiene.ignored) == (0, 0, 0, 1)
    assert (result.opened, result.ignored) == (0, 1)
    status = store.query_one("SELECT hits FROM rule_status WHERE run_id = ? AND rule_id = 'R-HYG-01'", (result.run_id,))
    assert status is not None and status["hits"] == 0  # an ignored hit is no hit
    assert "0 hits (0 new) on 1 checked, 1 ignored" in result.render()

    [later] = run_at(store, process, fields, now=NOW + timedelta(hours=2))
    kept = _row(store, "AIL-1", "R-HYG-01")
    assert (kept["resolved_at"], kept["resolved_why"], kept["last_seen_at"]) == (at(1), "ignored", at(2))
    assert (later.opened, later.resolved, later.ignored) == (0, 0, 1)


def test_an_ignore_of_one_rule_leaves_the_tickets_other_rules_as_they_are(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, assignee=None)  # no assignee, no estimate
    derive(store, process)
    run_at(store, process, fields)
    assert {row["rule_id"] for row in store.query("SELECT rule_id FROM findings")} == {"R-HYG-01", "R-EST-01"}
    _ignore(store, ("AIL-1", "R-HYG-01"))

    run_at(store, process, fields, now=NOW + timedelta(hours=1))

    assert (_row(store, "AIL-1", "R-HYG-01")["resolved_why"], _row(store, "AIL-1", "R-EST-01")["resolved_at"]) == (
        "ignored", None)


def test_an_ignore_written_while_its_finding_was_open_is_applied_by_the_next_run(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, assignee=None, points=1)
    derive(store, process)
    run_at(store, process, fields)
    with store.transaction():
        add_ignores(store.conn, "AIL", [("AIL-1", "R-HYG-01")], reason=None, by=ADMIN, at=at(0.5))

    [result] = run_at(store, process, fields, now=NOW + timedelta(hours=1))

    row = _row(store, "AIL-1", "R-HYG-01")
    assert (row["resolved_at"], row["resolved_why"], result.opened, result.resolved) == (at(1), "ignored", 0, 0)


def test_once_restored_the_rules_reopen_what_they_still_hold_and_settle_the_rest(
    store: Store, process: ProcessConfig, fields: FieldsConfig
) -> None:
    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, assignee=None)  # R-HYG-01 and R-EST-01
    add_issue(store, "AIL-2", status="Done", changes=[(-30.0, "To Do", "Done")], points=1)  # R-FLOW-02
    derive(store, process)
    run_at(store, process, fields)
    before = {(row["issue_key"], row["rule_id"]): row for row in store.query("SELECT * FROM findings")}
    assert set(before) == {("AIL-1", "R-HYG-01"), ("AIL-1", "R-EST-01"), ("AIL-2", "R-FLOW-02")}
    made = _ignore(store, ("AIL-1", None), ("AIL-2", None))
    # A finding of a rule that cannot run here, ignored too: nothing can say it cleared.
    unmeasured = finding_id("R-FLOW-01", "0.1.0", "AIL-1", "no-review")
    with store.transaction():
        store.upsert("findings", {
            "finding_id": unmeasured, "rule_id": "R-FLOW-01", "rule_version": "0.1.0", "issue_key": "AIL-1",
            "project_key": "AIL", "kind": "rule", "severity": "breach", "evidence_key": "no-review",
            "first_seen_at": at(0), "last_seen_at": at(0), "resolved_at": at(0.5), "resolved_why": "ignored"})

    add_issue(store, "AIL-1", status="In Progress", changes=STARTED, assignee="acct-team-2")  # R-HYG-01 stops
    with store.transaction():
        assert restore_ignores(store.conn, "AIL", made, by=ADMIN, at=at(0.75)) == 2
    [result] = run_at(store, process, fields, catalogue(lookback_days=1), now=NOW + timedelta(hours=1))

    estimate = _row(store, "AIL-1", "R-EST-01")
    assert estimate["finding_id"] == before[("AIL-1", "R-EST-01")]["finding_id"]  # the same finding, reopened
    assert (estimate["first_seen_at"], estimate["resolved_at"], estimate["resolved_why"]) == (at(0), None, None)
    hygiene = _row(store, "AIL-1", "R-HYG-01")
    assert (hygiene["resolved_at"], hygiene["resolved_why"]) == (at(1), "cleared")  # no longer held
    skipped = _row(store, "AIL-2", "R-FLOW-02")
    assert (skipped["resolved_at"], skipped["resolved_why"]) == (at(1), "aged_out")  # finished before the look-back
    review = _row(store, "AIL-1", "R-FLOW-01")
    assert (review["resolved_at"], review["resolved_why"]) == (None, None)  # open again, as before its ignore
    assert (_stat(result, "R-EST-01").opened, result.resolved, result.aged_out, result.untouched) == (1, 1, 1, 1)
    assert store.count("findings", {"resolved_why": "ignored"}) == 0


def test_the_store_keeps_one_active_ignore_per_pair_and_restores_only_the_boards_own(store: Store) -> None:
    with store.transaction():
        first = add_ignores(store.conn, "AIL", [("AIL-1", "R-EST-01"), ("AIL-1", None), ("AIL-1", "R-EST-01")],
                            reason="Old", by=ADMIN, at=at(0))
        again = add_ignores(store.conn, "AIL", [("AIL-1", None), ("AIL-2", "R-EST-01")], reason=None, by=ADMIN,
                            at=at(1))
        other = add_ignores(store.conn, "MOB", [("MOB-1", None)], reason=None, by=ADMIN, at=at(1))
    assert len(first) == 2 and len(again) == 1 and len(other) == 1  # the whole AIL-1 was held already: reused
    held = active_ignores(store.conn, "AIL")
    assert len(held) == 3
    whole = held.ticket("AIL-1")
    assert whole is not None and (whole.scope, whole.rule_id, whole.reason, whole.by, whole.at) == (
        "ticket", None, "Old", ADMIN, at(0))
    assert held.covers("AIL-1", ["W-AGE-01"]) == whole  # the ticket's own first
    one = held.covers("AIL-2", ["W-AGE-01", "R-EST-01"])
    assert one is not None and (one.scope, one.rule_id) == ("finding", "R-EST-01")
    assert held.covers("AIL-2", ["W-AGE-01"]) is None and held.ticket("AIL-2") is None
    assert one.body(people=False) == {"scope": "finding", "rule_id": "R-EST-01", "reason": None, "by": None,
                                      "at": at(1)}
    with store.transaction():
        assert restore_ignores(store.conn, "AIL", [*other, *first, *first], by=ADMIN, at=at(2)) == 2
        assert restore_ignores(store.conn, "AIL", first, by=ADMIN, at=at(3)) == 0  # restored already
    assert len(active_ignores(store.conn, "AIL")) == 1 and len(active_ignores(store.conn, "MOB")) == 1
    assert store.query_one("SELECT restored_at FROM finding_ignores WHERE ignore_id = ?", (first[0],))[0] == at(2)


def test_a_snapshot_without_the_table_has_no_ignores() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        assert active_ignores(conn, "AIL") == Ignores()
    finally:
        conn.close()
    info = IgnoreInfo("a" * 32, "ticket", None, "Old", ADMIN, at(0))
    assert info.body() == {"scope": "ticket", "rule_id": None, "reason": "Old", "by": ADMIN, "at": at(0)}
