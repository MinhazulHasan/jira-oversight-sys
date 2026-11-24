from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from oversight.api.aggregates import TREND_METRICS
from oversight.api.app import create_app
from oversight.api.people import person_name
from oversight.config import FieldsConfig, ProcessConfig
from oversight.demo import ASSIGNEE_NAMES
from oversight.derive.run import run_derive
from oversight.ingest.avatars import PictureFetcher, refresh_pictures
from oversight.jira.mapping import map_issue, map_member
from oversight.llm.scripted import ScriptedProvider
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import UTC, to_iso
from tests.conftest import CONFIG_FIXTURES, VIEWER_HEADERS
from tests.test_api_findings import Api, check_finding, check_meta, check_page, check_person, fields_are, open_api

T = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
KEY = "test-key"
ZONE = "Pacific/Chatham"
ROW_FIELDS = ("person", "tickets", "open_tickets", "open_breaches", "open_warnings", "tickets_with_findings")
RULES = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")


def pid(account: str) -> str:
    return "p" + hashlib.sha256(account.encode("utf-8")).hexdigest()[:12]


NAMES: dict[str, str | None] = {
    "echo": "Echo Tester",
    "delta": "Delta Tester",
    "charlie": "Charlie Tester",
    "bravo": "Bravo Tester",
    "alpha": "alpha tester",  # lower case: the order is by name in any case
    "victor": "Victor Tester",
    "whiskey1": "Whiskey Tester",  # two accounts with one name: the order falls to the id
    "whiskey2": "Whiskey Tester",
    "unnamed": None,
    "zulu": "  " + "Zulu " * 40,  # 202 characters with spaces around: trimmed, then cut to 128
    "outsider": "Outsider Tester",  # only on AIL
}
MEMBER_NAMES: dict[str, str] = {
    "mike": "Mike Member",  # DEMO, with a picture
    "olof": "Ólöf Árnadóttir",  # DEMO: folded, she sorts among the O's, not after Zulu
    "oscar": "Oscar Member",  # AIL only
}
ALL_NAMES: dict[str, str | None] = {**NAMES, **MEMBER_NAMES}
BOARD_MEMBERS: dict[str, tuple[str, ...]] = {"DEMO": ("echo", "delta", "mike", "olof"), "AIL": ("outsider", "oscar")}
ACCOUNTS = {who: f"712020:{hashlib.sha256(who.encode()).hexdigest()[:8]}-0f0f-4a4a-8b8b-{n:012d}"
            for n, who in enumerate(ALL_NAMES, 1)}
SIZES = ("16x16", "24x24", "32x32", "48x48")
ATLASSIAN = "https://avatar-management--avatars.us-west-2.prod.public.atl-paas.net"
ON_GRAVATAR = ("delta", "bravo")


def email(who: str) -> str:
    return f"{who}.tester@example.invalid"


def initials(who: str) -> str:
    """Atlassian's initials picture, where Gravatar sends someone who has no Gravatar."""
    return f"{ATLASSIAN}/initials/{who[:2].upper()}-4.png"


def avatar(who: str, size: str) -> str:
    """Jira's picture URL for the account at one size, as avatarUrls holds it."""
    pixels = size.partition("x")[0]
    if who in ON_GRAVATAR:
        digest = hashlib.md5(email(who).encode(), usedforsecurity=False).hexdigest()  # Gravatar's own scheme
        return f"https://secure.gravatar.com/avatar/{digest}?d={quote(initials(who), safe='')}&s={pixels}"
    return f"{ATLASSIAN}/{ACCOUNTS[who]}/{hashlib.sha256(who.encode()).hexdigest()[:8]}-5e5e/{pixels}"


PICTURES: dict[str, tuple[bytes, str]] = {
    "echo": (b"\x89PNG\r\n\x1a\n" + b"echo's picture", "image/png"),
    "charlie": (b"\xff\xd8\xff\xe0" + b"charlie's picture", "image/jpeg"),
    "delta": (b"GIF89a" + b"delta's initials", "image/gif"),
    "outsider": (b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"outsider's picture", "image/webp"),
    "mike": (b"\x89PNG\r\n\x1a\n" + b"mike's picture", "image/png"),
}
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


VERSIONS = {who: version(data) for who, (data, _kind) in PICTURES.items()}


def picture_hosts(request: httpx.Request) -> httpx.Response:
    """Gravatar and Atlassian's picture store, as far as these accounts go: never sent a credential."""
    assert "authorization" not in request.headers and "cookie" not in request.headers
    for who in ALL_NAMES:
        if request.url == httpx.URL(avatar(who, "48x48")):
            if who in PICTURES and who not in ON_GRAVATAR:
                data, kind = PICTURES[who]
                return httpx.Response(200, content=data, headers={"Content-Type": kind})
            if who == "delta":
                return httpx.Response(302, headers={"Location": initials(who)})
            if who == "alpha":
                return httpx.Response(200, content=SVG, headers={"Content-Type": "image/svg+xml"})
            return httpx.Response(404)
    if request.url == httpx.URL(initials("delta")):
        return httpx.Response(200, content=PICTURES["delta"][0], headers={"Content-Type": "image/gif"})
    return httpx.Response(404)


def user(who: str) -> dict[str, Any]:
    """A Jira user object with everything it can carry."""
    found: dict[str, Any] = {
        "self": f"https://example.invalid/rest/api/3/user?accountId={ACCOUNTS[who]}",
        "accountId": ACCOUNTS[who],
        "accountType": "atlassian",
        "emailAddress": email(who),
        "avatarUrls": {size: avatar(who, size) for size in SIZES},
        "active": True,
        "timeZone": ZONE,
        "locale": "en_GB",
    }
    if ALL_NAMES[who] is not None:
        found["displayName"] = ALL_NAMES[who]
    return found


TICKETS: dict[str, tuple[str | None, str]] = {
    "DEMO-1": ("echo", "In Progress"),
    "DEMO-2": ("delta", "In Progress"),
    "DEMO-3": ("charlie", "In Progress"),
    "DEMO-4": ("bravo", "In Progress"),
    "DEMO-5": ("bravo", "To Do"),
    "DEMO-6": (None, "To Do"),
    "DEMO-7": ("alpha", "To Do"),
    "DEMO-8": ("victor", "In Progress"),
    "DEMO-9": ("whiskey1", "To Do"),
    "DEMO-10": ("whiskey2", "To Do"),
    "DEMO-11": ("unnamed", "In Progress"),
    "DEMO-12": ("zulu", "Done"),
    "DEMO-13": ("echo", "Done"),
    "AIL-1": ("outsider", "To Do"),
    "AIL-2": ("echo", "In Progress"),
}
STATUSES = {"To Do": ("10001", "new"), "In Progress": ("10002", "indeterminate"), "Done": ("10003", "done")}
FINDINGS: dict[str, tuple[str, str, bool]] = {
    "f1": ("R-EST-01", "DEMO-1", True),
    "f2": ("R-HYG-01", "DEMO-1", True),
    "f3": ("R-EFF-01", "DEMO-1", False),
    "f4": ("R-EST-01", "DEMO-2", True),
    "f5": ("W-AGE-01", "DEMO-2", True),
    "f6": ("R-EST-01", "DEMO-3", True),
    "f7": ("W-AGE-01", "DEMO-3", True),
    "f8": ("R-EST-02", "DEMO-3", True),
    "f9": ("R-EST-01", "DEMO-4", True),
    "f10": ("W-AGE-01", "DEMO-4", True),
    "f11": ("R-HYG-01", "DEMO-6", False),  # the unassigned ticket's only finding, resolved
    "f12": ("W-SPR-01", "sprint:4427", True),  # a sprint finding: no one's
    "f13": ("R-EST-01", "AIL-2", True),
}
SPRINT_EVIDENCE = {"sprint_id": 4427, "name": "Sample Sprint 10", "end_date": "2026-11-06T06:00:00.000Z",
                   "days_left": 1.8, "issues": 10, "not_started": 4, "share": 0.4}


def fid(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:32]


def board_of(key: str) -> str:
    return "AIL" if key.startswith("AIL-") else "DEMO"


def payload(key: str, n: int) -> dict[str, Any]:
    """A search result, as a sync stores it in raw_json (reporter and creator are people too)."""
    who, status = TICKETS[key]
    created = T - timedelta(days=40 - n)
    status_id, category = STATUSES[status]
    return {
        "id": str(20_000 + n),
        "key": key,
        "fields": {
            "project": {"key": board_of(key)},
            "issuetype": {"name": "Task", "hierarchyLevel": 0},
            "summary": f"Sample task {n}",
            "status": {"id": status_id, "name": status, "statusCategory": {"key": category}},
            "assignee": None if who is None else user(who),
            "reporter": user("echo"),
            "creator": user("delta"),
            "created": to_iso(created),
            "updated": to_iso(created + timedelta(days=1)),
            "resolutiondate": to_iso(created + timedelta(days=2)) if status == "Done" else None,
            "timespent": 3600,
            "aggregatetimespent": 3600,
            "customfield_10016": 3.0,
        },
    }


def build(path: Path) -> None:
    fields = FieldsConfig.load(CONFIG_FIXTURES / "fields.yaml")
    with Store.open(path) as store:
        with store.transaction():
            for board in ("DEMO", "AIL"):
                for name, (status_id, category) in STATUSES.items():
                    store.upsert("statuses", {"project_key": board, "status_id": status_id, "name": name, "category": category})
                store.set_cursor(board, last_synced_at=to_iso(T), run_id="r1", account_timezone="Europe/London")
            for n, key in enumerate(TICKETS, 1):
                store.upsert("issues", map_issue(payload(key, n), fields))
            moved = to_iso(T - timedelta(days=20))
            store.upsert("status_changes", {"issue_key": "DEMO-1", "changelog_id": "100", "changed_at": moved,
                                            "author_id": ACCOUNTS["delta"], "from_status": "To Do", "to_status": "In Progress"})
            store.upsert("field_changes", {"issue_key": "DEMO-1", "changelog_id": "100", "field": "assignee",
                                           "changed_at": moved, "from_value": None, "to_value": ACCOUNTS["echo"]})
            store.upsert("worklogs", {"worklog_id": "9001", "issue_key": "DEMO-1", "started_at": moved, "seconds": 3600,
                                      "author_id": ACCOUNTS["charlie"], "updated_at": moved})
            for name, (rule_id, stored_key, is_open) in FINDINGS.items():
                rule = RULES.by_id(rule_id)
                store.upsert("findings", {
                    "finding_id": fid(name), "rule_id": rule_id, "rule_version": RULES.version,
                    "issue_key": stored_key, "project_key": board_of(stored_key), "kind": rule.kind,
                    "severity": rule.severity, "evidence_key": name, "message": rule.title,
                    "evidence_json": SPRINT_EVIDENCE if stored_key.startswith("sprint:") else {},
                    "first_seen_at": to_iso(T - timedelta(days=5)),
                    "last_seen_at": to_iso(T if is_open else T - timedelta(days=2)),
                    "resolved_at": None if is_open else to_iso(T - timedelta(days=1)),
                    "resolved_why": None if is_open else "cleared", "run_id": "r1",
                })
        store.conn.executescript("DROP TABLE people; PRAGMA user_version = 3;")
    with Store.open(path) as store:
        assert store.count("people") == len(NAMES)
        with store.transaction():
            for board, who in BOARD_MEMBERS.items():
                rows = [map_member(user(member), to_iso(T)) for member in who]
                store.replace_rows("board_members", {"project_key": board},
                                   [{"project_key": board, "account_id": row["account_id"], "seen_at": to_iso(T)}
                                    for row in rows if row is not None])
                for row in rows:
                    assert row is not None
                    store.upsert("people", row)
        assert store.count("people") == len(ALL_NAMES)
        run_derive(store, ProcessConfig.load(CONFIG_FIXTURES / "process.yaml"), projects=["DEMO", "AIL"], now=lambda: T)
        with PictureFetcher(transport=httpx.MockTransport(picture_hosts)) as fetcher:
            stats = refresh_pictures(store, fetcher, projects=["DEMO", "AIL"], now=lambda: T)
        assert stats.as_dict() == {"fetched": len(PICTURES), "unchanged": 0, "failed": len(ALL_NAMES) - len(PICTURES)}


def serve(root: Path) -> TestClient:
    """The API over root/oversight.db, signed in with the service key."""
    settings = Settings(
        _env_file=None,
        jira_projects="DEMO,AIL",
        api_service_key=KEY,
        db_path=root / "oversight.db",
        config_dir=CONFIG_FIXTURES,
        reports_dir=root / "reports",
    )
    return TestClient(create_app(settings, provider=ScriptedProvider(), now=lambda: T),
                      headers={"X-Service-Key": KEY, **VIEWER_HEADERS})


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    root = tmp_path_factory.mktemp("api_people")
    build(root / "oversight.db")
    with serve(root) as client:
        yield client


def get(client: TestClient, path: str, params: Any = None) -> Any:
    response = client.get(f"/api/v1{path}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def shown(who: str | None) -> dict[str, str | None] | None:
    if who is None:
        return None
    raw = ALL_NAMES[who]
    return {"id": pid(ACCOUNTS[who]), "name": raw.strip()[:128].rstrip() if raw else "Unnamed account",
            "avatar": VERSIONS.get(who)}


def all_ids(client: TestClient, params: Sequence[tuple[str, Any]]) -> set[str]:
    body = get(client, "/findings", [*params, ("page_size", 100)])
    assert body["total"] == len(body["items"]) <= 100
    return {item["finding_id"] for item in body["items"]}


def facet(body: Any, name: str) -> dict[str, int]:
    return {entry["value"]: entry["count"] for entry in body["facets"][name]}


def of(*names: str) -> set[str]:
    return {fid(name) for name in names}


WHISKEYS = sorted(("whiskey1", "whiskey2"), key=lambda who: pid(ACCOUNTS[who]))
DEMO_ROWS: list[tuple[str | None, int, int, int, int, int]] = [
    ("echo", 2, 1, 2, 0, 1),  # most breaches
    ("charlie", 1, 1, 1, 2, 1),  # then most warnings
    ("bravo", 2, 2, 1, 1, 1),  # then most open tickets
    ("delta", 1, 1, 1, 1, 1),
    ("alpha", 1, 1, 0, 0, 0),  # then by name, any case: "alpha tester" ...
    (None, 1, 1, 0, 0, 0),  # ... "Unassigned" ...
    ("unnamed", 1, 1, 0, 0, 0),  # ... "Unnamed account", "Victor Tester", "Whiskey Tester" twice by id
    ("victor", 1, 1, 0, 0, 0),
    (WHISKEYS[0], 1, 1, 0, 0, 0),
    (WHISKEYS[1], 1, 1, 0, 0, 0),
    ("zulu", 1, 0, 0, 0, 0),  # its one ticket is done
]


def rows_of(body: Any) -> list[tuple[Any, ...]]:
    return [tuple(row[name] for name in ROW_FIELDS) for row in body["people"]]


def test_people_counts_everyone_on_the_board_in_the_contracts_order(built: TestClient) -> None:
    body = get(built, "/people")
    fields_are(body, "meta", "people")
    check_meta(body["meta"])
    for row in body["people"]:
        fields_are(row, *ROW_FIELDS)
        check_person(row["person"])
        assert all(isinstance(row[name], int) and row[name] >= 0 for name in ROW_FIELDS[1:])
    assert rows_of(body) == [(shown(who), *counts) for who, *counts in DEMO_ROWS]


def test_names_are_trimmed_capped_and_unnamed_accounts_say_so(built: TestClient) -> None:
    names = {row["person"]["id"]: row["person"]["name"] for row in get(built, "/people")["people"] if row["person"]}
    zulu = names[pid(ACCOUNTS["zulu"])]
    assert len(zulu) == 128 and zulu == ("Zulu " * 40).strip()[:128] and zulu.startswith("Zulu Zulu")
    assert names[pid(ACCOUNTS["unnamed"])] == "Unnamed account"
    assert names[pid(ACCOUNTS["alpha"])] == "alpha tester"  # the case Jira gave
    assert {name for name in names.values()} >= {"Whiskey Tester"}


@pytest.mark.parametrize(
    "raw",
    ["someone@example.com", "  Someone.Else+jira@Example.co.uk ", "Jo Bloggs (jo@corp.example)", "<jo@corp.example>",
     "x" * 120 + " jo@corp.example"],  # the address past the 128-character cut still counts
)
def test_a_display_name_holding_an_email_address_is_never_sent(raw: str) -> None:
    assert person_name(raw) == "Unnamed account"


@pytest.mark.parametrize("raw", ["Ops @ Night", "Team@Home", "Ada Brooks", "Anne-Marie O'Neill"])
def test_an_at_sign_alone_is_not_an_address(raw: str) -> None:
    assert person_name(raw) == raw


def test_people_reads_only_the_picked_board(built: TestClient) -> None:
    body = get(built, "/people", {"project": "AIL"})
    assert body["meta"]["projects"] == ["AIL"]
    assert rows_of(body) == [(shown("echo"), 1, 1, 1, 0, 1), (shown("outsider"), 1, 1, 0, 0, 0)]
    assert shown("outsider") not in [row["person"] for row in get(built, "/people")["people"]]


@pytest.mark.parametrize(
    ("query", "status"),
    [("?state=open", 422), ("?person=unassigned", 422), ("?project=DEMO&project=DEMO", 422), ("?project=XYZ", 404)],
)
def test_people_takes_no_parameter_but_project(built: TestClient, query: str, status: int) -> None:
    assert built.get(f"/api/v1/people{query}").status_code == status
    assert built.get("/api/v1/people", headers={"X-Service-Key": "wrong"}).status_code == 401


def clicks_through(client: TestClient) -> None:
    """Each row of /people is exactly its /findings filter, and its facet entry counts as many."""
    people = facet(get(client, "/findings"), "people")
    for row in get(client, "/people")["people"]:
        value = row["person"]["id"] if row["person"] else "unassigned"
        body = get(client, "/findings", [("person", value), ("page_size", 100)])  # open, the default
        assert body["total"] == row["open_breaches"] + row["open_warnings"] == people[value]
        assert Counter(item["severity"] for item in body["items"]) == Counter(
            {"breach": row["open_breaches"], "warning": row["open_warnings"]})
        assert len({item["issue_key"] for item in body["items"]}) == row["tickets_with_findings"]
        assert row["tickets_with_findings"] <= min(row["tickets"], body["total"])  # schemas.ts superRefine


def test_each_row_is_exactly_its_findings_filter(built: TestClient) -> None:
    clicks_through(built)


def test_findings_whose_issue_has_left_the_snapshot_are_no_ones(tmp_path: Path) -> None:
    build(tmp_path / "oversight.db")
    with Store.open(tmp_path / "oversight.db") as store, store.transaction():
        store.delete_issue("DEMO-1")
        store.delete_issue("DEMO-2")
    gone = of("f1", "f2", "f3", "f4", "f5")
    with serve(tmp_path) as client:
        body = get(client, "/findings", {"state": "all", "page_size": 100})
        check_page(body)
        listed = {item["finding_id"]: item for item in body["items"]}
        assert gone <= set(listed)
        assert {(listed[f]["issue_key"], listed[f]["assignee"]) for f in gone} == {("DEMO-1", None), ("DEMO-2", None)}
        assert get(client, f"/findings/{fid('f1')}")["finding"]["assignee"] is None
        assert all_ids(client, [("person", "unassigned"), ("state", "all")]) == of("f11")  # only the unassigned ticket's
        everyone = [("person", entry["value"]) for entry in body["facets"]["people"]]
        assert all_ids(client, [*everyone, ("state", "all")]) == set(listed) - gone - of("f12")
        open_body = get(client, "/findings")
        assert sum(facet(open_body, "people").values()) == open_body["total"] - 1 - 4  # less the sprint one, the gone ones
        assert pid(ACCOUNTS["delta"]) not in facet(open_body, "people")  # DEMO-2 was all delta had
        assert rows_of(get(client, "/people")) == [
            (shown("charlie"), 1, 1, 1, 2, 1),
            (shown("bravo"), 2, 2, 1, 1, 1),
            (shown("alpha"), 1, 1, 0, 0, 0),
            (None, 1, 1, 0, 0, 0),  # DEMO-6 alone: none of the gone findings
            (shown("unnamed"), 1, 1, 0, 0, 0),
            (shown("victor"), 1, 1, 0, 0, 0),
            (shown(WHISKEYS[0]), 1, 1, 0, 0, 0),
            (shown(WHISKEYS[1]), 1, 1, 0, 0, 0),
            (shown("echo"), 1, 0, 0, 0, 0),  # DEMO-13, done: DEMO-1's findings went with it
            (shown("zulu"), 1, 0, 0, 0, 0),
        ]
        clicks_through(client)


def test_the_person_filter_takes_several_people_and_unassigned(built: TestClient) -> None:
    echo, charlie = pid(ACCOUNTS["echo"]), pid(ACCOUNTS["charlie"])
    assert all_ids(built, [("person", echo)]) == of("f1", "f2")
    assert all_ids(built, [("person", echo), ("person", charlie)]) == of("f1", "f2", "f6", "f7", "f8")
    assert all_ids(built, [("person", "unassigned")]) == set()  # its only finding is resolved
    assert all_ids(built, [("person", "unassigned"), ("state", "all")]) == of("f11")
    assert all_ids(built, [("person", echo), ("person", "unassigned"), ("state", "all")]) == of("f1", "f2", "f3", "f11")
    assert all_ids(built, [("person", echo), ("project", "AIL")]) == of("f13")


def test_a_person_filter_never_matches_a_sprint_finding(built: TestClient) -> None:
    everyone = [("person", entry["value"]) for entry in get(built, "/findings")["facets"]["people"]]
    every_finding = all_ids(built, [("state", "all")])
    assert fid("f12") in every_finding
    assert all_ids(built, [*everyone, ("state", "all")]) == every_finding - of("f12")


@pytest.mark.parametrize("nobody", ["p000000000000", pid(ACCOUNTS["outsider"])])  # the outsider is only on AIL
def test_an_id_nobody_on_the_board_has_matches_nothing(built: TestClient, nobody: str) -> None:
    body = get(built, "/findings", {"person": nobody, "state": "all"})
    check_page(body)
    assert (body["items"], body["total"]) == ([], 0)
    assert len(body["facets"]["people"]) == 11  # still every entry


def test_the_people_facet_lists_everyone_by_name_then_unassigned_even_at_zero(built: TestClient) -> None:
    body = get(built, "/findings")
    check_page(body)
    board = [row[0] for row in DEMO_ROWS if row[0] is not None]
    expected = sorted((shown(who) for who in board), key=lambda person: (person["name"].lower(), person["id"]))
    assert [(e["value"], e["label"], e["avatar"]) for e in body["facets"]["people"]] == [
        *((person["id"], person["name"], person["avatar"]) for person in expected), ("unassigned", "Unassigned", None)]
    assert sum(entry["avatar"] is not None for entry in body["facets"]["people"]) == 3  # echo, charlie, delta
    counts = facet(body, "people")
    assert counts == {
        **{pid(ACCOUNTS[who]): 0 for who in board},
        pid(ACCOUNTS["echo"]): 2, pid(ACCOUNTS["charlie"]): 3, pid(ACCOUNTS["bravo"]): 2, pid(ACCOUNTS["delta"]): 2,
        "unassigned": 0,
    }
    assert sum(counts.values()) == body["total"] - 1  # every open finding but the sprint one has a person entry
    assert facet(get(built, "/findings", {"state": "all"}), "people")["unassigned"] == 1


def test_person_facet_counts_apply_every_filter_but_their_own(built: TestClient) -> None:
    echo = pid(ACCOUNTS["echo"])
    body = get(built, "/findings", [("person", echo), ("severity", "breach")])
    assert body["total"] == 2
    people = facet(body, "people")
    assert {value: n for value, n in people.items() if n} == {
        echo: 2, pid(ACCOUNTS["charlie"]): 1, pid(ACCOUNTS["bravo"]): 1, pid(ACCOUNTS["delta"]): 1}
    assert facet(body, "severity") == {"breach": 2, "warning": 0}  # the person filter narrows the others
    assert facet(body, "rules")["R-EST-01"] == 1 and facet(body, "rules")["R-HYG-01"] == 1


@pytest.mark.parametrize(
    "values",
    [["P0123456789ab"], ["p0123456789a"], ["p0123456789abc"], ["pxyz456789abc"], ["Unassigned"], ["none"],
     ["ada.tester@example.invalid"], ["712020:5f0c0001-a1b2-4c3d-8e9f-0123456789ab"], ["p0123456789ab"] * 51],
)
def test_bad_person_values_are_refused(built: TestClient, values: list[str]) -> None:
    assert built.get("/api/v1/findings", params=[("person", value) for value in values]).status_code == 422


def test_fifty_people_and_empty_values_are_accepted(built: TestClient) -> None:
    fifty = [("person", f"p{n:012x}") for n in range(50)]
    assert built.get("/api/v1/findings", params=fifty).status_code == 200
    assert get(built, "/findings", [("person", "")])["total"] == get(built, "/findings")["total"]  # empty: not given


def test_every_finding_carries_its_issues_current_assignee(built: TestClient) -> None:
    items = get(built, "/findings", {"state": "all", "page_size": 100})["items"]
    expected = {fid(name): shown(TICKETS[key][0]) if key in TICKETS else None
                for name, (_rule, key, _open) in FINDINGS.items() if board_of(key) == "DEMO"}
    assert {item["finding_id"]: item["assignee"] for item in items} == expected
    for item in items:
        check_finding(item)
        assert get(built, f"/findings/{item['finding_id']}")["finding"] == item  # identical to the list's
    assert expected[fid("f11")] is None and expected[fid("f12")] is None  # unassigned, and a sprint finding


def test_the_issue_page_names_its_assignee(built: TestClient) -> None:
    for key, (who, _status) in TICKETS.items():
        body = get(built, f"/issues/{key}/timeline", {"project": board_of(key)})
        assert body["issue"]["assignee"] == shown(who), key
        check_person(body["issue"]["assignee"])
        assert all(finding["assignee"] == shown(who) for finding in body["findings"])
    changes = get(built, "/issues/DEMO-1/timeline")["changes"]
    assert [(c["field"], c["from"], c["to"]) for c in changes if c["field"] == "assignee"] == [
        ("assignee", "Unassigned", "Assigned")]  # changes still never say whom


PICTURE_HEADERS = {
    "cache-control": "private, max-age=86400",
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'; sandbox",
}


def picture(client: TestClient, who_id: str, params: Any = None) -> httpx.Response:
    return client.get(f"/api/v1/people/{who_id}/avatar", params=params)


def test_a_persons_picture_is_the_stored_bytes_with_their_type_and_the_contracts_headers(built: TestClient) -> None:
    for who, (data, kind) in PICTURES.items():  # mike is a member of DEMO who holds none of its tickets
        board = "AIL" if who == "outsider" else "DEMO"
        response = picture(built, pid(ACCOUNTS[who]), {"project": board, "v": VERSIONS[who]})
        assert response.status_code == 200, who
        assert response.content == data and response.headers["content-type"] == kind
        assert {name: response.headers.get(name) for name in PICTURE_HEADERS} == PICTURE_HEADERS
    assert {kind for _data, kind in PICTURES.values()} == {"image/png", "image/jpeg", "image/gif", "image/webp"}


def test_every_person_objects_avatar_is_the_version_of_the_picture_served(built: TestClient) -> None:
    for board in ("DEMO", "AIL"):
        everyone = [row["person"] for row in get(built, "/people", {"project": board})["people"] if row["person"]]
        entries = get(built, "/findings", {"project": board})["facets"]["people"]
        assert {entry["value"]: entry["avatar"] for entry in entries if entry["value"] != "unassigned"} == {
            who["id"]: who["avatar"] for who in everyone}
        for who in everyone:
            response = picture(built, who["id"], {"project": board})
            if who["avatar"] is None:
                assert response.status_code == 404, who
            else:
                assert response.status_code == 200 and version(response.content) == who["avatar"], who


def test_v_and_any_parameter_but_project_are_ignored(built: TestClient) -> None:
    echo = pid(ACCOUNTS["echo"])
    for params in (None, {"v": "000000000000"}, {"v": "not a version"}, [("v", "1"), ("v", "2")], {"size": "16"}):
        response = picture(built, echo, params)
        assert response.status_code == 200 and response.content == PICTURES["echo"][0], params


@pytest.mark.parametrize(
    "who_id",
    [
        "p0123456789a", "p0123456789abc", "P0123456789ab", "pABCDEF012345", "pxyz456789abc", "unassigned", "p",
        quote(ACCOUNTS["echo"], safe=""),  # the account id itself
        pid(ACCOUNTS["echo"]).upper(),
        pid(ACCOUNTS["echo"]) + "%0A",
        " " + pid(ACCOUNTS["echo"]),
    ],
)
def test_a_malformed_person_id_is_a_404(built: TestClient, who_id: str) -> None:
    response = picture(built, who_id)
    assert response.status_code == 404 and response.json() == {"detail": "no picture of that person on this board"}
    assert response.headers["cache-control"] == "no-store"  # an answer without a picture is never cached


@pytest.mark.parametrize(
    ("who", "board"),
    [
        ("bravo", "DEMO"),  # Gravatar has no picture of them, so none is held
        ("alpha", "DEMO"),  # their picture was an SVG, refused
        ("unnamed", "DEMO"),
        ("outsider", "DEMO"),  # a picture is held, but they hold no ticket on DEMO
        ("charlie", "AIL"),  # the same the other way round
        ("mike", "AIL"),  # a picture is held, but they are a member of DEMO only
        ("olof", "DEMO"),  # a member of DEMO, with no picture
        ("oscar", "AIL"),
    ],
)
def test_no_picture_held_or_a_person_off_the_board_is_a_404(built: TestClient, who: str, board: str) -> None:
    response = picture(built, pid(ACCOUNTS[who]), {"project": board})
    assert response.status_code == 404 and response.json() == {"detail": "no picture of that person on this board"}


def test_a_picture_is_board_scoped_like_people(built: TestClient) -> None:
    outsider, echo = pid(ACCOUNTS["outsider"]), pid(ACCOUNTS["echo"])
    assert picture(built, outsider, {"project": "AIL"}).status_code == 200
    assert picture(built, outsider).status_code == 404  # the default board, DEMO
    assert picture(built, echo, {"project": "DEMO"}).content == picture(built, echo, {"project": "AIL"}).content
    assert picture(built, "p000000000000").status_code == 404  # an id nobody has
    assert picture(built, echo, {"project": "XYZ"}).status_code == 404  # a board this service does not serve
    assert built.get(f"/api/v1/people/{echo}/avatar", headers={"X-Service-Key": "wrong"}).status_code == 401


def test_a_members_picture_is_served_while_they_are_on_the_board(tmp_path: Path) -> None:
    build(tmp_path / "oversight.db")
    mike = pid(ACCOUNTS["mike"])
    with serve(tmp_path) as client:
        assert picture(client, mike).content == PICTURES["mike"][0]
    with Store.open(tmp_path / "oversight.db") as store, store.transaction():
        store.delete_rows("board_members", {"project_key": "DEMO", "account_id": ACCOUNTS["mike"]})
    with serve(tmp_path) as client:
        assert picture(client, mike).status_code == 404
        assert mike not in [entry["person"]["id"] for entry in get(client, "/people/directory")["people"]]


def test_a_stored_picture_that_is_not_one_of_the_four_types_is_never_served(tmp_path: Path) -> None:
    build(tmp_path / "oversight.db")
    with Store.open(tmp_path / "oversight.db") as store, store.transaction():
        for who, data, kind in (("echo", SVG, "image/png"), ("charlie", PICTURES["charlie"][0], "image/svg+xml"),
                                ("delta", PICTURES["echo"][0], "image/gif")):
            store.conn.execute("UPDATE people SET avatar = ?, avatar_type = ? WHERE account_id = ?", (data, kind, ACCOUNTS[who]))
    with serve(tmp_path) as client:
        for who in ("echo", "charlie", "delta"):
            assert picture(client, pid(ACCOUNTS[who])).status_code == 404, who
        assert picture(client, pid(ACCOUNTS["outsider"]), {"project": "AIL"}).status_code == 200


def folded(name: str) -> str:
    bare = "".join(char for char in unicodedata.normalize("NFD", name) if not unicodedata.category(char).startswith("M"))
    return bare.lower()


def directory(client: TestClient, board: str | None = None) -> Any:
    body = get(client, "/people/directory", {"project": board} if board else None)
    fields_are(body, "meta", "people")
    for entry in body["people"]:
        fields_are(entry, "person", "tickets")
        check_person(entry["person"])
        assert entry["person"] is not None and isinstance(entry["tickets"], int) and entry["tickets"] >= 0
    return body


def test_the_directory_lists_the_assignees_and_the_members_with_no_tickets_by_folded_name(built: TestClient) -> None:
    body = directory(built)
    check_meta(body["meta"])
    on_board = {holder for key, (holder, _status) in TICKETS.items() if holder is not None and board_of(key) == "DEMO"}
    expected = [(shown(who), sum(1 for key, (holder, _s) in TICKETS.items() if holder == who and board_of(key) == "DEMO"))
                for who in (*on_board, "mike", "olof")]
    expected.sort(key=lambda entry: (folded(entry[0]["name"]), entry[0]["id"]))
    assert [(entry["person"], entry["tickets"]) for entry in body["people"]] == expected
    names = [entry["person"]["name"] for entry in body["people"]]
    assert names.index("Mike Member") < names.index("Ólöf Árnadóttir") < names.index("Unnamed account")  # folded
    assert names.count("Whiskey Tester") == 2 and "Unassigned" not in names  # by id; nobody is no one's entry
    assert [entry["tickets"] for entry in body["people"] if entry["person"]["name"] in MEMBER_NAMES.values()] == [0, 0]


def test_the_directorys_tickets_are_get_peoples_and_everyone_else_is_a_member_with_none(built: TestClient) -> None:
    for board in ("DEMO", "AIL"):
        people = {row["person"]["id"]: row for row in get(built, "/people", {"project": board})["people"] if row["person"]}
        entries = {entry["person"]["id"]: entry for entry in directory(built, board)["people"]}
        assert len(entries) == len(directory(built, board)["people"])  # each person once
        for who, row in people.items():
            assert entries[who] == {"person": row["person"], "tickets": row["tickets"]}
        members = {pid(ACCOUNTS[who]) for who in BOARD_MEMBERS[board]}
        assert set(entries) == set(people) | members
        assert all(entries[who]["tickets"] == 0 for who in members - set(people))
    ail = [(entry["person"]["name"], entry["tickets"]) for entry in directory(built, "AIL")["people"]]
    assert ail == [("Echo Tester", 1), ("Oscar Member", 0), ("Outsider Tester", 1)]


def test_a_member_with_no_tickets_has_an_empty_findings_list(built: TestClient) -> None:
    for who in ("mike", "olof"):
        body = get(built, "/findings", {"person": pid(ACCOUNTS[who]), "state": "all"})
        check_page(body)
        assert (body["items"], body["total"]) == ([], 0)
    assert pid(ACCOUNTS["mike"]) not in facet(get(built, "/findings"), "people")  # the facet is GET /people's
    assert pid(ACCOUNTS["mike"]) not in [row["person"]["id"] for row in get(built, "/people")["people"] if row["person"]]


@pytest.mark.parametrize(
    ("query", "status"),
    [("?state=open", 422), ("?person=unassigned", 422), ("?project=DEMO&project=DEMO", 422), ("?q=mi", 422),
     ("?project=XYZ", 404)],
)
def test_the_directory_takes_no_parameter_but_project(built: TestClient, query: str, status: int) -> None:
    assert built.get(f"/api/v1/people/directory{query}").status_code == status
    assert built.get("/api/v1/people/directory", headers={"X-Service-Key": "wrong"}).status_code == 401
    assert built.get("/api/v1/people/directory").headers["cache-control"] == "no-store"


def test_a_board_with_no_member_fetch_yet_lists_its_assignees(tmp_path: Path) -> None:
    build(tmp_path / "oversight.db")
    with Store.open(tmp_path / "oversight.db") as store, store.transaction():
        store.conn.execute("DELETE FROM board_members")
    with serve(tmp_path) as client:
        people = [row for row in get(client, "/people")["people"] if row["person"]]
        listed = directory(client)["people"]
        assert sorted((entry["person"]["id"], entry["tickets"]) for entry in listed) == sorted(
            (row["person"]["id"], row["tickets"]) for row in people)


GET_ROUTES = {
    "/api/v1/health", "/api/v1/projects", "/api/v1/refresh", "/api/v1/overview", "/api/v1/findings",
    "/api/v1/findings/{finding_id}", "/api/v1/issues/{key}/timeline", "/api/v1/people", "/api/v1/insights/latest",
    "/api/v1/trends", "/api/v1/rules", "/api/v1/baseline/latest", "/api/v1/people/{person_id}/avatar",
    "/api/v1/people/directory", "/api/v1/people/scorecard", "/api/v1/portfolio", "/api/v1/access-log", "/api/v1/usage",
    "/api/v1/people/{person_id}/card", "/api/v1/rules/settings",
}
POST_ROUTES = {"/api/v1/ask", "/api/v1/refresh", "/api/v1/rules/settings", "/api/v1/rules/settings/preview",
               "/api/v1/rules/settings/draft"}
JIRA_USER_KEYS = ("accountId", "emailAddress", "avatarUrls", "timeZone", "displayName")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def every_response(client: TestClient, board: str, finding_ids: Sequence[str], issue_keys: Sequence[str]) -> dict[str, str]:
    operations = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert {path for path, methods in operations.items() if "get" in methods} == GET_ROUTES
    assert {path for path, methods in operations.items() if set(methods) - {"get"}} == POST_ROUTES
    paths = [
        "/health", "/projects", "/refresh", "/overview", "/rules", "/people", "/people/directory", "/insights/latest",
        "/baseline/latest",
        "/findings", "/findings?state=all&page_size=100", "/findings?state=all&person=unassigned",
        *(f"/trends?metric={metric}" for metric in TREND_METRICS),
        *(f"/findings/{finding_id}" for finding_id in finding_ids),
        *(f"/issues/{key}/timeline" for key in issue_keys),
    ]
    bodies: dict[str, str] = {}
    for path in paths:
        response = client.get(f"/api/v1{path}{'&' if '?' in path else '?'}project={board}")
        no_data_yet = path in ("/insights/latest", "/baseline/latest")
        assert response.status_code == 200 or (no_data_yet and response.status_code == 404), (path, response.text)
        bodies[path] = response.text
    for row in client.get(f"/api/v1/people/directory?project={board}").json()["people"]:  # members too
        path = f"/people/{row['person']['id']}/avatar"
        response = client.get(f"/api/v1{path}?project={board}&v={row['person']['avatar']}")
        if row["person"]["avatar"] is None:
            assert response.status_code == 404 and response.json() == {"detail": "no picture of that person on this board"}
        else:  # the bytes alone: an image type, and no JSON around them
            assert response.status_code == 200 and response.headers["content-type"] in IMAGE_TYPES, path
        bodies[path] = response.content.decode("latin-1")  # every byte, searchable as text
        path = f"/people/{row['person']['id']}/card"
        response = client.get(f"/api/v1{path}?level=people&project={board}")
        assert response.status_code == 200, (path, response.text)
        bodies[path] = response.text
    someone = next(row["person"] for row in client.get(f"/api/v1/people?project={board}").json()["people"] if row["person"])
    for path in ("/people/scorecard?level=people", "/people/scorecard?level=people&days=30",
                 f"/people/scorecard?level=people&person={someone['id']}"):
        response = client.get(f"/api/v1{path}&project={board}")
        assert response.status_code == 200, (path, response.text)
        bodies[path] = response.text
    for path, body in (
        ("/ask", {"question": "Which epics have the most open breaches?"}),
        ("/ask people", {"question": "Who has the most open breaches?", "level": "people"}),
        ("/ask mention", {"question": f"What are @{someone['name']}'s open findings?", "level": "people",
                          "mentions": [someone["id"]]}),
        ("/ask scorecard", {"question": f"Is @{someone['name']} doing well?", "level": "people",
                            "mentions": [someone["id"]]}),
        ("/ask compare", {"question": "Who follows the process most closely? Rank everyone.", "level": "people"}),
    ):
        answer = client.post(f"/api/v1/ask?project={board}", json=body)
        assert answer.status_code == 200, answer.text
        bodies[path] = answer.text
    for path in ("/portfolio", "/usage?level=people", "/access-log?level=people&limit=200"):
        response = client.get(f"/api/v1{path}{'&' if '?' in path else '?'}project={board}")
        assert response.status_code == 200, (path, response.text)
        bodies[path] = response.text
    settings = client.get(f"/api/v1/rules/settings?level=people&project={board}")
    assert settings.status_code == 200, settings.text
    bodies["/rules/settings"] = settings.text
    preview = client.post(f"/api/v1/rules/settings/preview?level=people&project={board}",
                          json={"changes": [{"rule_id": "R-HYG-01", "severity": "warning"},
                                            {"rule_id": "R-EFF-01", "params": {"threshold": 2.5}}]})
    assert preview.status_code == 200, preview.text
    bodies["/rules/settings/preview"] = preview.text
    rule = {"title": "Open over 30 days", "check": "open_longer_than", "params": {"days": 30}, "severity": "warning",
            "applies_to": {"status_category": ["new", "indeterminate"], "issue_type": None}}
    added = client.post(f"/api/v1/rules/settings/preview?level=people&project={board}",
                        json={"changes": [{"op": "create", "rule": rule}]})
    assert added.status_code == 200, added.text
    bodies["/rules/settings/preview create"] = added.text
    draft = client.post(f"/api/v1/rules/settings/draft?level=people&project={board}",
                        json={"text": "Work in progress for more than 3 days"})
    assert draft.status_code == 200, draft.text
    bodies["/rules/settings/draft"] = draft.text
    assert json.loads(bodies["/ask"])["people"] == [] and someone["name"] not in bodies["/ask"]
    assert json.loads(bodies["/ask mention"])["people"] in ([], [someone])
    return bodies


def test_no_response_holds_an_account_id_email_picture_url_or_time_zone(built: TestClient) -> None:
    picture_urls = [*(avatar(who, size) for who in ALL_NAMES for size in SIZES), *(initials(who) for who in ON_GRAVATAR)]
    secrets = [*ACCOUNTS.values(), "712020:", *(email(who) for who in ALL_NAMES), "example.invalid", *picture_urls,
               *(url.split("?")[0] for url in picture_urls), "atl-paas.net", "gravatar.com", "avatar-management",
               "avatar_url", "avatar_source", ZONE, *JIRA_USER_KEYS, "en_GB",
               "Europe/London"]  # the syncing account's zone (sync_state): it stays in the service
    for board in ("DEMO", "AIL"):
        findings = [fid(name) for name, (_rule, key, _open) in FINDINGS.items() if board_of(key) == board]
        issues = [key for key in TICKETS if board_of(key) == board]
        people = get(built, "/people/directory", {"project": board})["people"]
        bodies = every_response(built, board, findings, issues)
        assert len(bodies) == 27 + len(TREND_METRICS) + len(findings) + len(issues) + 2 * len(people)
        assert json.loads(bodies["/ask people"])["outcome"] == "answered"  # it does name people, by display name
        assert json.loads(bodies["/ask scorecard"])["outcome"] == json.loads(bodies["/ask compare"])["outcome"] == "answered"
        assert sum(path.endswith("/avatar") for path in bodies) == len(people) >= 2
        assert sum(path.endswith("/card") for path in bodies) == len(people)
        for path, text in bodies.items():
            assert not [secret for secret in secrets if secret in text], (board, path)
    for row in get(built, "/people")["people"]:
        if row["person"]:
            assert re.fullmatch(r"p[0-9a-f]{12}", row["person"]["id"])


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Api]:
    yield from open_api(tmp_path_factory.mktemp("api_people_demo"))


DEMO_PICTURED = ("acc-01", "acc-05")  # the demo's two accounts with a picture drawn in code


def demo_pictures(demo: Api) -> dict[str, bytes]:
    return {row[0]: row[1] for row in demo.rows("SELECT account_id, avatar FROM people WHERE avatar IS NOT NULL")}


def demo_people(demo: Api) -> dict[str | None, dict[str, str | None] | None]:
    versions = {account: version(data) for account, data in demo_pictures(demo).items()}
    return {None: None, **{account: {"id": pid(account), "name": name, "avatar": versions.get(account)}
                           for account, name in ASSIGNEE_NAMES.items()}}


def test_the_demos_two_pictures_are_served_and_the_others_get_none(demo: Api) -> None:
    stored = demo_pictures(demo)
    assert sorted(stored) == list(DEMO_PICTURED)
    for account in ASSIGNEE_NAMES:
        response = demo.client.get(f"/api/v1/people/{pid(account)}/avatar")
        if account in DEMO_PICTURED:
            assert response.status_code == 200 and response.content == stored[account]
            assert response.headers["content-type"] == "image/png"
        else:
            assert response.status_code == 404, account
    named = {row["person"]["id"]: row["person"] for row in demo.get("/people")["people"] if row["person"]}
    assert named == {pid(account): person for account, person in demo_people(demo).items() if account}


def test_demo_people_match_sql_with_the_fictional_names(demo: Api) -> None:
    tickets = {row[0]: (row[1], row[2]) for row in demo.rows(
        "SELECT assignee_id, COUNT(*), SUM(status_category != 'done') FROM issues WHERE project_key = 'DEMO'"
        " AND issue_type != 'Epic' GROUP BY 1")}
    findings = {(row[0], row[1]): row[2] for row in demo.rows(
        "SELECT i.assignee_id, f.severity, COUNT(*) FROM findings AS f JOIN issues AS i ON i.issue_key = f.issue_key"
        " WHERE f.project_key = 'DEMO' AND f.resolved_at IS NULL AND i.issue_type != 'Epic' GROUP BY 1, 2")}
    touched = {row[0]: row[1] for row in demo.rows(
        "SELECT i.assignee_id, COUNT(DISTINCT f.issue_key) FROM findings AS f JOIN issues AS i ON i.issue_key = f.issue_key"
        " WHERE f.project_key = 'DEMO' AND f.resolved_at IS NULL AND i.issue_type != 'Epic' GROUP BY 1")}
    epics = demo.rows("SELECT COUNT(*) FROM issues WHERE project_key = 'DEMO' AND issue_type = 'Epic'"
                      " AND assignee_id IS NOT NULL")[0][0]
    assert epics > 0, "the demo assigns Epics, so leaving them out is tested"
    assert set(tickets) == {*ASSIGNEE_NAMES, None}  # all eight have work, and some has nobody
    people = demo_people(demo)
    expected = [
        (people[account], n, open_n, findings.get((account, "breach"), 0), findings.get((account, "warning"), 0),
         touched.get(account, 0))
        for account, (n, open_n) in tickets.items()
    ]
    expected.sort(key=lambda row: (-row[3], -row[4], -row[2], *((row[0]["name"].lower(), row[0]["id"]) if row[0]
                                                               else ("unassigned", "unassigned"))))
    body = demo.get("/people")
    check_meta(body["meta"])
    assert rows_of(body) == expected
    ids = {row["person"]["name"]: row["person"]["id"] for row in body["people"] if row["person"]}
    assert ids["Ada Brooks"] == "pbc7fe62842ca" and ids["Hana Mori"] == "pe0f1fe0c54ec"  # stable: the hash, pinned


def test_demo_findings_carry_their_assignee_and_click_through_from_people(demo: Api) -> None:
    current = {row[0]: row[1] for row in demo.rows(
        "SELECT f.finding_id, i.assignee_id FROM findings AS f LEFT JOIN issues AS i ON i.issue_key = f.issue_key"
        " WHERE f.project_key = 'DEMO'")}
    sprint = {row[0] for row in demo.rows("SELECT finding_id FROM findings WHERE issue_key LIKE 'sprint:%'")}
    assert sprint, "the lifecycle has a sprint finding"
    seen: dict[str, Any] = {}
    page = 1
    while True:
        body = demo.get("/findings", {"state": "all", "page_size": 100, "page": page})
        check_page(body)
        seen.update({item["finding_id"]: item["assignee"] for item in body["items"]})
        if not body["items"]:
            break
        page += 1
    people = demo_people(demo)
    assert seen == {finding_id: people[account] if finding_id not in sprint else None
                    for finding_id, account in current.items()}
    by_person = Counter(seen[finding_id]["id"] if seen[finding_id] else "unassigned"
                        for finding_id in seen if finding_id not in sprint)
    open_body = demo.get("/findings")
    for row in demo.get("/people")["people"]:
        value = row["person"]["id"] if row["person"] else "unassigned"
        filtered = demo.get("/findings", [("person", value), ("page_size", 1)])
        assert filtered["total"] == row["open_breaches"] + row["open_warnings"] == facet(open_body, "people")[value]
        assert demo.get("/findings", [("person", value), ("state", "all"), ("page_size", 1)])["total"] == by_person[value]
    everyone = [("person", entry["value"]) for entry in open_body["facets"]["people"]]
    assert [entry["label"] for entry in open_body["facets"]["people"]] == [*sorted(ASSIGNEE_NAMES.values()), "Unassigned"]
    total = demo.get("/findings", {"state": "all", "page_size": 1})["total"]
    assert demo.get("/findings", [*everyone, ("state", "all"), ("page_size", 1)])["total"] == total - len(sprint)


def test_demo_responses_name_people_only_by_display_name(demo: Api) -> None:
    open_items = demo.get("/findings", {"page_size": 30})["items"]
    keys = sorted({item["issue_key"] for item in open_items if item["issue_key"]})
    keys += [row[0] for row in demo.rows("SELECT issue_key FROM issues WHERE project_key = 'DEMO' AND assignee_id IS NULL LIMIT 5")]
    bodies = every_response(demo.client, "DEMO", [item["finding_id"] for item in open_items], keys)
    account = re.compile(r"\bacc-\d{2}\b|\bapp-tempo\b")
    for path, text in bodies.items():
        assert not account.search(text) and not [key for key in JIRA_USER_KEYS if key in text], path
    names = " ".join(bodies.values())
    assert all(name in names for name in ASSIGNEE_NAMES.values())  # shown, by display name
