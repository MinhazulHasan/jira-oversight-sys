from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from oversight.config import FieldsConfig, ProcessConfig
from oversight.derive.run import run_derive
from oversight.rules.catalog import RulesConfig
from oversight.rules.engine import run_rules
from oversight.store.db import Store
from oversight.timeutil import parse_ts, to_iso

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures" / "rules"
LABELS_FILE = TESTS / "labels" / "findings.yaml"
RULES_FILE = TESTS.parent / "config" / "rules.yaml"
FIELDS_FILE = TESTS / "fixtures" / "config" / "fields.yaml"

Finding = tuple[str, str, str, str]


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


_PROJECTS_FILE: dict[str, Any] = _load(FIXTURES / "projects.yaml")
PROCESS = ProcessConfig.model_validate(_PROJECTS_FILE["process"])
STATUSES: list[dict[str, Any]] = _PROJECTS_FILE["statuses"]
_SHIPPED = RulesConfig.load(RULES_FILE)
RULES = _SHIPPED.model_copy(update={"defaults": _SHIPPED.defaults.model_copy(update={"lookback_days": None})})
FIELDS = FieldsConfig.load(FIELDS_FILE)
LABELS: dict[str, Any] = _load(LABELS_FILE)
NOW: datetime = datetime.fromisoformat(LABELS["now"])
PROJECTS: list[str] = list(LABELS["projects"])
RULE_IDS: list[str] = [rule.id for rule in RULES.rules]


@dataclass
class Snapshot:
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    status_changes: list[dict[str, Any]] = field(default_factory=list)
    field_changes: list[dict[str, Any]] = field(default_factory=list)
    sprint_keys: set[str] = field(default_factory=set)

    def add(self, issue: dict[str, Any], changes: list[dict[str, Any]], field_changes: list[dict[str, Any]]) -> None:
        key = issue["issue_key"]
        assert key not in self.issues, f"{key} is defined twice"
        self.issues[key] = issue
        self.status_changes += [{"issue_key": key, **row} for row in changes]
        self.field_changes += [{"issue_key": key, **row} for row in field_changes]


def _category(project: str, status: str) -> str:
    for row in STATUSES:
        if row["project_key"] == project and str(row["name"]).casefold() == status.casefold():
            return str(row["category"])
    raise AssertionError(f"{project} has no status {status!r} in projects.yaml")


def _issue_row(values: dict[str, Any], sprints: list[dict[str, Any]]) -> dict[str, Any]:
    row = dict(values)
    key = str(row["issue_key"])
    row.setdefault("project_key", key.split("-")[0])
    row.setdefault("issue_id", f"id-{key}")
    if "status_category" not in row:
        row["status_category"] = _category(row["project_key"], row["status"])
    if sprints:
        row["sprint_ids"] = [sprint["id"] for sprint in sprints]
        row["raw_json"] = {"key": key, "fields": {FIELDS.id_of("sprint"): sprints}}
    return row


def _history(block: dict[str, Any]) -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
    defaults = block.get("defaults") or {}
    for item in block["issues"]:
        key = str(item["issue_key"])
        names, hours = item["path"][0::2], item["path"][1::2]
        assert len(names) == len(hours) + 1, f"{key}: path must alternate status and hours"
        at = parse_ts(item["created_at"])
        assert at is not None, key
        number = key.split("-")[1]
        changes: list[dict[str, Any]] = []
        for n, (source, target, spent) in enumerate(zip(names[:-1], names[1:], hours, strict=True), start=1):
            at += timedelta(hours=spent)
            changes.append(
                {"changelog_id": f"{number}{n:02d}", "changed_at": to_iso(at),
                 "from_status": source, "to_status": target}
            )
        yield {**defaults, "issue_key": key, "created_at": item["created_at"], "status": names[-1]}, changes


def load_snapshot() -> Snapshot:
    snapshot = Snapshot()
    for path in sorted(FIXTURES.glob("*.yaml")):
        if path.name == "projects.yaml":
            continue
        data = _load(path) or {}
        defaults = data.get("defaults") or {}
        sprints = {int(sprint_id): spec for sprint_id, spec in (data.get("sprints") or {}).items()}
        snapshot.sprint_keys |= {f"sprint:{sprint_id}" for sprint_id in sprints}
        for case in data.get("cases") or []:
            objects = [{"id": sprint_id, **sprints[sprint_id]} for sprint_id in case.get("sprints") or []]
            issue = _issue_row({**defaults, **case["issue"]}, objects)
            snapshot.add(issue, case.get("status_changes") or [], case.get("field_changes") or [])
        if data.get("history"):
            for issue, changes in _history(data["history"]):
                snapshot.add(_issue_row(issue, []), changes, [])
    return snapshot


@dataclass(frozen=True)
class Outcome:
    findings: dict[str, set[Finding]]
    evidence: dict[tuple[str, str, str], dict[str, Any]]
    rule_status: dict[tuple[str, str], dict[str, Any]]


@pytest.fixture(scope="module")
def outcome(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Outcome]:
    snapshot = load_snapshot()
    store = Store.open(tmp_path_factory.mktemp("rules_labels") / "oversight.db")
    try:
        with store.transaction():
            store.upsert_many("statuses", STATUSES)
            for issue in snapshot.issues.values():
                store.upsert("issues", issue)
            store.upsert_many("status_changes", snapshot.status_changes)
            store.upsert_many("field_changes", snapshot.field_changes)
        run_derive(store, PROCESS, projects=PROJECTS, now=lambda: NOW)
        run_rules(store, PROCESS, RULES, FIELDS, projects=PROJECTS, now=lambda: NOW)

        findings: dict[str, set[Finding]] = {project: set() for project in PROJECTS}
        evidence: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in store.query("SELECT * FROM findings"):
            assert row["resolved_at"] is None, dict(row)
            findings.setdefault(row["project_key"], set()).add(
                (row["rule_id"], row["issue_key"], row["evidence_key"], row["severity"])
            )
            evidence[(row["rule_id"], row["issue_key"], row["evidence_key"])] = json.loads(row["evidence_json"] or "{}")
        rule_status = {
            (row["project_key"], row["rule_id"]): dict(row) for row in store.query("SELECT * FROM rule_status")
        }
        yield Outcome(findings=findings, evidence=evidence, rule_status=rule_status)
    finally:
        store.close()


def labelled(project: str) -> set[Finding]:
    return {
        (label["rule_id"], label["issue_key"], label["evidence_key"], label["severity"])
        for label in LABELS["projects"][project].get("findings") or []
    }


def negatives(project: str) -> dict[str, list[str]]:
    return LABELS["projects"][project].get("negatives") or {}


def _produced(outcome: Outcome, project: str, rule_id: str) -> set[Finding]:
    return {finding for finding in outcome.findings.get(project, set()) if finding[0] == rule_id}


def _expected(project: str, rule_id: str) -> set[Finding]:
    return {finding for finding in labelled(project) if finding[0] == rule_id}


def _diff(got: set[Finding], want: set[Finding]) -> dict[str, list[Finding]]:
    return {"unlabelled": sorted(got - want), "missed": sorted(want - got)}


_COMPARISONS = [
    pytest.param(project, rule_id, id=f"{project}-{rule_id}") for project in PROJECTS for rule_id in RULE_IDS
]


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_rule_has_three_positive_and_three_negative_labelled_cases(rule_id: str) -> None:
    positive = [finding for project in PROJECTS for finding in labelled(project) if finding[0] == rule_id]
    negative = [key for project in PROJECTS for key in negatives(project).get(rule_id, [])]
    assert len(positive) >= 3, f"{rule_id} has {len(positive)} positive labels"
    assert len(negative) >= 3, f"{rule_id} has {len(negative)} negative labels"


@pytest.mark.parametrize(("project", "rule_id"), _COMPARISONS)
def test_engine_matches_the_labels(outcome: Outcome, project: str, rule_id: str) -> None:
    got, want = _produced(outcome, project, rule_id), _expected(project, rule_id)
    assert _diff(got, want) == {"unlabelled": [], "missed": []}


def test_every_finding_is_in_a_labelled_project_and_rule(outcome: Outcome) -> None:
    assert set(outcome.findings) <= set(PROJECTS)
    assert {finding[0] for found in outcome.findings.values() for finding in found} <= set(RULE_IDS)


@pytest.mark.parametrize("project", PROJECTS)
def test_measurability_matches_the_labels(outcome: Outcome, project: str) -> None:
    unmeasured: dict[str, list[str]] = {}
    for rule_id in RULE_IDS:
        row = outcome.rule_status[(project, rule_id)]
        if not row["measurable"]:
            unmeasured[rule_id] = json.loads(row["missing_json"] or "[]")
    assert unmeasured == (LABELS["projects"][project].get("not_measurable") or {})


@pytest.mark.parametrize("project", PROJECTS)
def test_rule_notes_match_the_labels(outcome: Outcome, project: str) -> None:
    for rule_id, text in (LABELS["projects"][project].get("notes") or {}).items():
        note = outcome.rule_status[(project, rule_id)]["note"]
        if text is None:
            assert note is None, (rule_id, note)
        else:
            assert note is not None and text in note, (rule_id, note)


def test_labelled_evidence_values_match(outcome: Outcome) -> None:
    for project in PROJECTS:
        for label in LABELS["projects"][project].get("findings") or []:
            expected = label.get("evidence")
            key = (label["rule_id"], label["issue_key"], label["evidence_key"])
            if expected and key in outcome.evidence:
                actual = outcome.evidence[key]
                assert {name: actual.get(name) for name in expected} == expected, key


def test_labels_name_real_cases_rules_and_severities() -> None:
    snapshot = load_snapshot()
    known = set(snapshot.issues) | snapshot.sprint_keys
    severity = {rule.id: rule.severity for rule in RULES.rules}
    for project in PROJECTS:
        unmeasured = set(LABELS["projects"][project].get("not_measurable") or {})
        positives = labelled(project)
        for rule_id, key, _, level in positives:
            assert key in known, key
            assert key.startswith("sprint:") or snapshot.issues[key]["project_key"] == project, key
            assert level == severity[rule_id], (rule_id, key)
            assert rule_id not in unmeasured, (rule_id, key)
        for rule_id, keys in negatives(project).items():
            assert rule_id in severity, rule_id
            assert rule_id not in unmeasured, f"{rule_id} cannot run in {project}: it has no negative cases there"
            assert len(keys) == len(set(keys)), (rule_id, keys)
            for key in keys:
                assert key in known, key
                assert key.startswith("sprint:") or snapshot.issues[key]["project_key"] == project, key
                assert not any(f[0] == rule_id and f[1] == key for f in positives), (rule_id, key)
