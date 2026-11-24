"""Jira JSON -> rows, checked against the real (sanitised) AIL samples."""
from __future__ import annotations

import json
from typing import Any

import pytest

from oversight.config import FieldRef, FieldsConfig
from oversight.errors import JiraError
from oversight.jira.mapping import (
    map_assignee,
    map_field_changes,
    map_issue,
    map_issue_links,
    map_member,
    map_remote_links,
    map_status_changes,
    map_statuses,
    map_worklog,
)
from oversight.store.db import Store

from tests.conftest import JIRA_FIXTURES

KEY = "AIL-1140"
T0 = "2026-11-03T05:00:00.000Z"
T1 = "2026-11-03T06:00:00.000Z"

# A company-managed site: points in "Story Points", epics through an Epic Link field.
COMPANY_FIELDS = FieldsConfig(
    fields={
        "story_points": FieldRef(id="customfield_10026", name="Story Points"),
        "sprint": FieldRef(id="customfield_10020", name="Sprint"),
        "epic_link": FieldRef(id="customfield_10014", name="Epic Link"),
    }
)

SAMPLE_STATUS_ROWS = [
    {
        "issue_key": KEY,
        "changelog_id": "334350",
        "changed_at": "2026-11-03T05:09:56.971Z",
        "author_id": "acc-dev-1",
        "from_status": "To Do",
        "to_status": "In Progress",
    },
    {
        "issue_key": KEY,
        "changelog_id": "335082",
        "changed_at": "2026-11-04T04:54:38.662Z",
        "author_id": "acc-dev-1",
        "from_status": "In Progress",
        "to_status": "Done",
    },
]

SAMPLE_FIELD_ROWS = [
    {
        "issue_key": KEY,
        "changelog_id": "334342",
        "field": "assignee",
        "changed_at": "2026-11-03T05:08:21.960Z",
        "from_value": None,
        "to_value": "acc-dev-1",
    },
    {
        "issue_key": KEY,
        "changelog_id": "334349",
        "field": "story_points",
        "changed_at": "2026-11-03T05:09:54.208Z",
        "from_value": None,
        "to_value": "0.5",
    },
    {
        "issue_key": KEY,
        "changelog_id": "334353",
        "field": "sprint",
        "changed_at": "2026-11-03T05:10:16.892Z",
        "from_value": None,
        "to_value": "4427",
    },
]


def _load(name: str) -> Any:
    """A fresh parse on every call, so a test can change what it gets."""
    return json.loads((JIRA_FIXTURES / name).read_text(encoding="utf-8"))


def _sample_issue() -> dict[str, Any]:
    return _load("issue_sample.json")


def _sample_histories() -> list[dict[str, Any]]:
    return _load("changelog_sample.json")["values"]


def _history(
    history_id: str, created: Any, *items: dict[str, Any], author: str | None = "acc-dev-1"
) -> dict[str, Any]:
    history: dict[str, Any] = {"id": history_id, "created": created, "items": list(items)}
    if author is not None:
        history["author"] = {"accountId": author}
    return history


def _status_item(from_status: str, to_status: str) -> dict[str, Any]:
    return {
        "field": "status",
        "fieldtype": "jira",
        "fieldId": "status",
        "fromString": from_status,
        "toString": to_status,
    }


def _points_item(to_string: str, from_string: str | None = None) -> dict[str, Any]:
    return {
        "field": "Story point estimate",
        "fieldtype": "custom",
        "fieldId": "customfield_10016",
        "from": None,
        "fromString": from_string,
        "to": None,
        "toString": to_string,
    }


def _columns(store: Store, table: str) -> set[str]:
    """The table's columns in the live schema, so schema drift fails these tests."""
    return {row["name"] for row in store.query("SELECT name FROM pragma_table_info(?)", (table,))}


def _without(fields: FieldsConfig, logical: str) -> FieldsConfig:
    return FieldsConfig(fields={name: ref for name, ref in fields.fields.items() if name != logical})


def test_map_issue_maps_every_column_of_the_real_sample(fields: FieldsConfig) -> None:
    issue = _sample_issue()
    assert map_issue(issue, fields) == {
        "issue_key": "AIL-1140",
        "issue_id": "52507",
        "project_key": "AIL",
        "issue_type": "Task",
        "summary": "Sample task (sanitised)",
        "status": "Done",
        "status_category": "done",
        "assignee_id": "acc-dev-1",
        "parent_key": "AIL-1138",
        "epic_key": "AIL-1138",
        "created_at": "2026-11-03T05:08:12.832Z",
        "updated_at": "2026-11-04T04:54:38.662Z",
        "resolved_at": "2026-11-04T04:54:38.644Z",
        "original_estimate_s": None,
        "remaining_estimate_s": None,
        "time_spent_s": 14400,
        "aggregate_time_spent_s": 14400,
        "story_points": 0.5,
        "sprint_ids": [4427],
        "raw_json": issue,
    }
    assert issue == _sample_issue()


def test_map_issue_row_has_exactly_the_issues_columns(store: Store, fields: FieldsConfig) -> None:
    assert set(map_issue(_sample_issue(), fields)) == _columns(store, "issues")


def test_sample_rows_store_cleanly_and_a_second_pass_writes_nothing(
    store: Store, fields: FieldsConfig
) -> None:
    issue = _sample_issue()
    histories = _sample_histories()

    def write() -> int:
        stats = store.upsert_issue(map_issue(issue, fields))
        stats += store.upsert_many("status_changes", map_status_changes(KEY, histories))
        stats += store.upsert_many("field_changes", map_field_changes(KEY, histories, fields))
        return stats.written

    assert write() == 1 + 2 + 3
    assert write() == 0
    stored = store.query_one("SELECT sprint_ids, story_points, raw_json FROM issues")
    assert stored["sprint_ids"] == "[4427]"
    assert stored["story_points"] == 0.5
    assert json.loads(stored["raw_json"]) == issue


def test_subtask_keeps_its_parent_but_has_no_epic(fields: FieldsConfig) -> None:
    issue = _sample_issue()
    issue["fields"]["issuetype"] = {"id": "10083", "name": "Subtask", "subtask": True, "hierarchyLevel": -1}
    issue["fields"]["parent"] = {
        "id": "52506",
        "key": "AIL-1139",
        "fields": {
            "summary": "Sample task (sanitised)",
            "issuetype": {"id": "10081", "name": "Task", "subtask": False, "hierarchyLevel": 0},
        },
    }
    row = map_issue(issue, fields)
    assert (row["issue_type"], row["parent_key"], row["epic_key"]) == ("Subtask", "AIL-1139", None)


@pytest.mark.parametrize(
    ("parent_type", "epic_key"),
    [
        ({"name": "Epic"}, "AIL-1138"),
        ({"name": "Epic", "hierarchyLevel": None}, "AIL-1138"),
        ({"name": "Feature", "hierarchyLevel": 1}, "AIL-1138"),
        ({"name": "Epic", "hierarchyLevel": 0}, None),
        ({"name": "Task"}, None),
        (None, None),
    ],
)
def test_parent_is_an_epic_by_hierarchy_level_then_by_name(
    fields: FieldsConfig, parent_type: dict[str, Any] | None, epic_key: str | None
) -> None:
    issue = _sample_issue()
    issue["fields"]["parent"]["fields"]["issuetype"] = parent_type
    row = map_issue(issue, fields)
    assert (row["parent_key"], row["epic_key"]) == ("AIL-1138", epic_key)


def _company_issue(epic_link: Any) -> dict[str, Any]:
    issue = _sample_issue()
    del issue["fields"]["parent"]
    issue["fields"]["customfield_10014"] = epic_link
    issue["fields"]["customfield_10026"] = 3
    return issue


def test_company_managed_epic_link_gives_the_epic() -> None:
    row = map_issue(_company_issue("AIL-5"), COMPANY_FIELDS)
    assert (row["parent_key"], row["epic_key"]) == (None, "AIL-5")
    assert row["story_points"] == 3.0
    assert isinstance(row["story_points"], float)


@pytest.mark.parametrize("value", [None, "", "   ", "not a key", 52505, {"key": "AIL-5"}])
def test_epic_link_without_a_key_string_gives_no_epic(value: Any) -> None:
    assert map_issue(_company_issue(value), COMPANY_FIELDS)["epic_key"] is None


def test_parent_epic_wins_over_the_epic_link() -> None:
    issue = _sample_issue()
    issue["fields"]["customfield_10014"] = "AIL-5"
    assert map_issue(issue, COMPANY_FIELDS)["epic_key"] == "AIL-1138"


def test_epic_link_is_ignored_when_fields_yaml_has_none(fields: FieldsConfig) -> None:
    assert fields.id_of("epic_link") is None
    assert map_issue(_company_issue("AIL-5"), fields)["epic_key"] is None


def test_missing_or_null_nested_objects_give_none(fields: FieldsConfig) -> None:
    issue = _sample_issue()
    values = issue["fields"]
    values["assignee"] = None
    del values["parent"]
    del values["status"]["statusCategory"]
    values["issuetype"] = None
    values["customfield_10016"] = None
    values["customfield_10020"] = None
    values["resolutiondate"] = None
    row = map_issue(issue, fields)
    assert row["assignee_id"] is None
    assert (row["parent_key"], row["epic_key"]) == (None, None)
    assert (row["status"], row["status_category"]) == ("Done", None)
    assert row["issue_type"] is None
    assert row["story_points"] is None
    assert row["sprint_ids"] == []
    assert row["resolved_at"] is None


@pytest.mark.parametrize(
    "issue",
    [
        {"id": 52507, "key": "AIL-1140"},
        {"id": 52507, "key": "AIL-1140", "fields": None},
        {"id": 52507, "key": "AIL-1140", "fields": {}},
        {"id": 52507, "key": "AIL-1140", "fields": {"project": None, "status": "Done", "assignee": []}},
    ],
)
def test_bare_issue_maps_without_raising(fields: FieldsConfig, issue: dict[str, Any]) -> None:
    row = map_issue(issue, fields)
    assert row["issue_id"] == "52507"
    assert row["project_key"] == "AIL"
    assert row["sprint_ids"] == []
    assert row["raw_json"] == issue
    rest = set(row) - {"issue_key", "issue_id", "project_key", "sprint_ids", "raw_json"}
    assert {column: row[column] for column in rest} == dict.fromkeys(rest)


LEGACY_SPRINTS = [
    "com.atlassian.greenhopper.service.sprint.Sprint@1a2b3c[id=12,rapidViewId=21,state=CLOSED,"
    "name=Sample Sprint 1,startDate=2026-01-05T08:00:00.000Z,endDate=2026-01-19T08:00:00.000Z,"
    "completeDate=2026-01-19T09:00:00.000Z,sequence=12,goal=]",
    # Sprint names are ticket text: one containing ",id=" must not change the id.
    "com.atlassian.greenhopper.service.sprint.Sprint@4d5e6f[id=15,rapidViewId=21,state=ACTIVE,"
    "name=Sprint ,id=99,startDate=<null>,endDate=<null>,completeDate=<null>,sequence=15,goal=]",
]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ([], []),
        ([{"id": 4427, "name": "Sample Sprint 10", "state": "active", "boardId": 21}], [4427]),
        ([{"id": 4427, "state": "active"}, {"id": 4400, "state": "closed"}], [4400, 4427]),
        ([{"id": "4427"}, {"id": 4427}], [4427]),
        ([{"name": "a sprint without an id"}, {"id": 4427}], [4427]),
        (LEGACY_SPRINTS, [12, 15]),
        ({"id": 4427}, [4427]),
    ],
)
def test_sprint_ids(fields: FieldsConfig, value: Any, expected: list[int]) -> None:
    issue = _sample_issue()
    issue["fields"]["customfield_10020"] = value
    assert map_issue(issue, fields)["sprint_ids"] == expected


def test_sprint_ids_are_none_when_the_site_has_no_sprint_field(fields: FieldsConfig) -> None:
    removed = _without(fields, "sprint")
    nulled = FieldsConfig(fields={**fields.fields, "sprint": None})
    for no_sprint in (removed, nulled):
        assert map_issue(_sample_issue(), no_sprint)["sprint_ids"] is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.5, 0.5), (3, 3.0), (0, 0.0), (None, None), ("2.5", 2.5), ("", None), ("many", None), (True, None)],
)
def test_story_points_are_a_float_or_none(fields: FieldsConfig, value: Any, expected: float | None) -> None:
    issue = _sample_issue()
    issue["fields"]["customfield_10016"] = value
    points = map_issue(issue, fields)["story_points"]
    assert points == expected
    assert points is None or isinstance(points, float)


def test_story_points_need_the_field_in_fields_yaml(fields: FieldsConfig) -> None:
    assert map_issue(_sample_issue(), _without(fields, "story_points"))["story_points"] is None


def test_estimates_are_integer_seconds_or_none(fields: FieldsConfig) -> None:
    issue = _sample_issue()
    issue["fields"].update(
        timeoriginalestimate=28800, timeestimate=7200.0, timespent="3600", aggregatetimespent=None
    )
    row = map_issue(issue, fields)
    columns = ("original_estimate_s", "remaining_estimate_s", "time_spent_s", "aggregate_time_spent_s")
    seconds = [row[column] for column in columns]
    assert seconds == [28800, 7200, 3600, None]
    assert all(isinstance(value, int) for value in seconds[:3])


@pytest.mark.parametrize("missing", ["key", "id"])
def test_issue_without_its_key_or_id_is_rejected(fields: FieldsConfig, missing: str) -> None:
    issue = _sample_issue()
    del issue[missing]
    with pytest.raises(JiraError, match="unexpected Jira payload"):
        map_issue(issue, fields)


@pytest.mark.parametrize("value", ["yesterday", "2026-11-02T23:08:12.832", {"date": "2026-11-02"}])
def test_unparseable_issue_timestamp_is_rejected(fields: FieldsConfig, value: Any) -> None:
    issue = _sample_issue()
    issue["fields"]["created"] = value
    with pytest.raises(JiraError, match="'created' of AIL-1140 is not a timestamp"):
        map_issue(issue, fields)


def test_status_changes_from_the_real_changelog(store: Store) -> None:
    histories = _sample_histories()
    rows = map_status_changes(KEY, histories)
    assert rows == SAMPLE_STATUS_ROWS
    assert all(set(row) == _columns(store, "status_changes") for row in rows)
    assert histories == _sample_histories()


def test_status_changes_are_sorted_when_histories_come_newest_first() -> None:
    assert map_status_changes(KEY, list(reversed(_sample_histories()))) == SAMPLE_STATUS_ROWS


def test_bulk_fetch_form_maps_like_the_issue_changelog(fields: FieldsConfig) -> None:
    entries = _load("mapping_bulk_changelog.json")["issueChangeLogs"]
    histories = [h for entry in entries if entry["issueId"] == "52507" for h in entry["changeHistories"]]
    assert map_status_changes(KEY, histories) == SAMPLE_STATUS_ROWS
    assert map_field_changes(KEY, histories, fields) == SAMPLE_FIELD_ROWS


def test_epoch_millisecond_created_becomes_utc() -> None:
    [row] = map_status_changes(KEY, [_history("334350", 1793682596971, _status_item("To Do", "In Progress"))])
    assert row["changed_at"] == "2026-11-03T05:09:56.971Z"


def test_status_item_without_field_id_is_matched_by_field_and_fieldtype() -> None:
    item = _status_item("To Do", "In Progress")
    del item["fieldId"]
    rows = map_status_changes("AIL-1", [_history("100", T0, item)])
    assert [(row["from_status"], row["to_status"]) for row in rows] == [("To Do", "In Progress")]


@pytest.mark.parametrize(
    "item",
    [
        {"field": "status", "fieldtype": "custom", "fromString": "a", "toString": "b"},
        {"field": "status", "fieldtype": "custom", "fieldId": "customfield_10500", "fromString": "a", "toString": "b"},
        {"field": "resolution", "fieldtype": "jira", "fieldId": "resolution", "to": "10000", "toString": "Done"},
    ],
)
def test_other_items_are_not_status_changes(item: dict[str, Any]) -> None:
    assert map_status_changes("AIL-1", [_history("100", T0, item)]) == []


def test_tied_timestamps_order_ids_as_numbers() -> None:
    histories = [
        _history("10", T0, _status_item("In Progress", "Done")),
        _history("x7", T0, _status_item("Done", "In Progress")),
        _history("9", T0, _status_item("To Do", "In Progress")),
        _history("5", T1, _status_item("In Progress", "Done")),
    ]
    ids = [row["changelog_id"] for row in map_status_changes("AIL-1", histories)]
    assert ids == ["9", "10", "x7", "5"]


def test_the_first_status_item_of_a_history_counts() -> None:
    history = _history("100", T0, _status_item("To Do", "In Progress"), _status_item("In Progress", "Done"))
    [row] = map_status_changes("AIL-1", [history])
    assert (row["from_status"], row["to_status"]) == ("To Do", "In Progress")


@pytest.mark.parametrize("author", [None, {}, {"displayName": "no account id"}])
def test_history_without_an_author_account_gives_none(author: dict[str, Any] | None) -> None:
    history = _history("100", T0, _status_item("To Do", "Done"), author=None)
    history["author"] = author
    [row] = map_status_changes("AIL-1", [history])
    assert row["author_id"] is None


def test_a_history_delivered_twice_gives_one_row() -> None:
    history = _history("100", T0, _status_item("To Do", "Done"))
    assert len(map_status_changes("AIL-1", [history, json.loads(json.dumps(history))])) == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": None}, "without an id"),
        ({"created": None}, "without a created time"),
        ({"created": "2026-11-03 05:00"}, "is not a timestamp"),
    ],
)
def test_a_history_without_id_or_time_is_rejected(
    fields: FieldsConfig, change: dict[str, Any], message: str
) -> None:
    status = {**_history("100", T0, _status_item("To Do", "Done")), **change}
    with pytest.raises(JiraError, match=message):
        map_status_changes("AIL-1", [status])
    points = {**_history("101", T0, _points_item("3")), **change}
    with pytest.raises(JiraError, match=message):
        map_field_changes("AIL-1", [points], fields)


def test_histories_that_give_no_row_need_no_id_or_time(fields: FieldsConfig) -> None:
    description = {"field": "description", "fieldtype": "jira", "fieldId": "description", "toString": "<redacted>"}
    untracked = {"items": [description]}
    assert map_status_changes("AIL-1", [untracked, {}]) == []
    assert map_field_changes("AIL-1", [untracked, {}], fields) == []


def test_field_changes_from_the_real_changelog(store: Store, fields: FieldsConfig) -> None:
    rows = map_field_changes(KEY, _sample_histories(), fields)
    assert rows == SAMPLE_FIELD_ROWS
    assert all(set(row) == _columns(store, "field_changes") for row in rows)


def test_untracked_items_of_the_real_changelog_are_ignored(fields: FieldsConfig) -> None:
    mapped = {row["changelog_id"] for row in SAMPLE_FIELD_ROWS}
    rest = [history for history in _sample_histories() if history["id"] not in mapped]
    names = {item["field"] for history in rest for item in history["items"]}
    assert names == {"description", "WorklogId", "timespent", "Rank", "RemoteWorkItemLink", "status", "resolution"}
    assert map_field_changes(KEY, rest, fields) == []


def test_only_fields_in_fields_yaml_are_tracked(fields: FieldsConfig) -> None:
    rows = map_field_changes(KEY, _sample_histories(), _without(fields, "story_points"))
    assert [row["field"] for row in rows] == ["assignee", "sprint"]


def test_estimate_items_read_seconds(fields: FieldsConfig) -> None:
    history = _history(
        "400",
        T0,
        {"field": "timeoriginalestimate", "fieldtype": "jira", "fieldId": "timeoriginalestimate",
         "from": None, "fromString": None, "to": "3600", "toString": "3600"},
        {"field": "timeestimate", "fieldtype": "jira", "fieldId": "timeestimate",
         "from": "3600", "fromString": "3600", "to": "1800", "toString": "1800"},
    )
    rows = map_field_changes("AIL-1", [history], fields)
    assert [(row["field"], row["from_value"], row["to_value"]) for row in rows] == [
        ("original_estimate", None, "3600"),
        ("remaining_estimate", "3600", "1800"),
    ]


def test_several_sprints_stay_one_string_of_ids(fields: FieldsConfig) -> None:
    history = _history(
        "401",
        T0,
        {"field": "Sprint", "fieldtype": "custom", "fieldId": "customfield_10020", "from": "4400",
         "fromString": "Sample Sprint 9", "to": "4400, 4427", "toString": "Sample Sprint 9, Sample Sprint 10"},
    )
    [row] = map_field_changes("AIL-1", [history], fields)
    assert (row["field"], row["from_value"], row["to_value"]) == ("sprint", "4400", "4400, 4427")


@pytest.mark.parametrize(
    ("name", "logical"),
    [
        ("Story point estimate", "story_points"),
        ("STORY POINT ESTIMATE", "story_points"),
        ("Sprint", "sprint"),
        ("Code Reviewers", "code_reviewers"),
        ("timeoriginalestimate", "original_estimate"),
        ("timeestimate", "remaining_estimate"),
        ("Assignee", "assignee"),
    ],
)
def test_item_without_field_id_is_matched_by_display_name(
    fields: FieldsConfig, name: str, logical: str
) -> None:
    history = _history("402", T0, {"field": name, "fieldtype": "custom", "to": "v"})
    rows = map_field_changes("AIL-1", [history], fields)
    assert [(row["field"], row["to_value"]) for row in rows] == [(logical, "v")]


@pytest.mark.parametrize(
    "name", ["WorklogId", "RemoteWorkItemLink", "Rank", "Development", "timespent", "status", "resolution", ""]
)
def test_item_without_field_id_and_an_untracked_name_is_ignored(fields: FieldsConfig, name: str) -> None:
    history = _history("403", T0, {"field": name, "fieldtype": "jira", "to": "1", "toString": "1"})
    assert map_field_changes("AIL-1", [history], fields) == []


def test_a_display_name_shared_by_two_tracked_fields_matches_neither() -> None:
    clash = FieldsConfig(
        fields={
            "story_points": FieldRef(id="customfield_10016", name="Points"),
            "programmer": FieldRef(id="customfield_10138", name="points"),
        }
    )
    nameless = {"field": "Points", "fieldtype": "custom", "toString": "3"}
    assert map_field_changes("AIL-1", [_history("404", T0, nameless)], clash) == []
    with_id = {**nameless, "fieldId": "customfield_10016"}
    rows = map_field_changes("AIL-1", [_history("405", T0, with_id)], clash)
    assert [row["field"] for row in rows] == ["story_points"]


def test_same_field_twice_in_one_history_keeps_the_last(fields: FieldsConfig) -> None:
    second = {"field": "Story point estimate", "fieldtype": "custom", "fromString": "1", "toString": "2"}
    history = _history("406", T0, _points_item("1"), second)
    rows = map_field_changes("AIL-1", [history], fields)
    assert [(row["field"], row["from_value"], row["to_value"]) for row in rows] == [("story_points", "1", "2")]


@pytest.mark.parametrize(
    ("change", "values"),
    [
        ({"from": "acc-dev-1", "fromString": "Developer One", "to": "acc-dev-2", "toString": "Developer Two"},
         ("acc-dev-1", "acc-dev-2")),
        ({"from": None, "fromString": "0.5", "to": None, "toString": "1"}, ("0.5", "1")),
        ({"from": "", "fromString": "", "to": "4427", "toString": "Sample Sprint 10"}, (None, "4427")),
        ({"from": "", "fromString": "Old", "to": "", "toString": ""}, ("Old", None)),
        ({"from": 3600, "to": 1800}, ("3600", "1800")),
        ({}, (None, None)),
    ],
)
def test_change_values_prefer_from_and_to_over_the_strings(
    fields: FieldsConfig, change: dict[str, Any], values: tuple[str | None, str | None]
) -> None:
    history = _history("407", T0, {"field": "timeestimate", "fieldtype": "jira", "fieldId": "timeestimate", **change})
    [row] = map_field_changes("AIL-1", [history], fields)
    assert (row["from_value"], row["to_value"]) == values


def test_field_changes_sort_by_time_then_numeric_id_then_field(fields: FieldsConfig) -> None:
    assignee = {"field": "assignee", "fieldtype": "jira", "fieldId": "assignee", "to": "acc-dev-1"}
    histories = [
        _history("5", T1, assignee),
        _history("10", T0, _points_item("2"), assignee),
        _history("9", T0, _points_item("1")),
    ]
    order = [(row["changelog_id"], row["field"]) for row in map_field_changes("AIL-1", histories, fields)]
    assert order == [("9", "story_points"), ("10", "assignee"), ("10", "story_points"), ("5", "assignee")]


def test_worklog_from_the_documented_shape(store: Store) -> None:
    [worklog] = _load("mapping_worklogs.json")["worklogs"]
    row = map_worklog(KEY, worklog)
    assert row == {
        "worklog_id": "67746",
        "issue_key": KEY,
        "started_at": "2026-11-02T15:00:00.000Z",
        "seconds": 14400,
        "author_id": "app-tempo",
        "updated_at": "2026-11-03T05:09:51.525Z",
    }
    assert set(row) == _columns(store, "worklogs")


def test_worklog_with_missing_parts_gives_none() -> None:
    row = map_worklog("AIL-1", {"id": 67746, "author": None, "timeSpentSeconds": None})
    assert row == {
        "worklog_id": "67746",
        "issue_key": "AIL-1",
        "started_at": None,
        "seconds": None,
        "author_id": None,
        "updated_at": None,
    }


def test_worklog_without_an_id_is_rejected() -> None:
    with pytest.raises(JiraError, match="worklog of AIL-1 without an id"):
        map_worklog("AIL-1", {"timeSpentSeconds": 60})


def test_remote_links_skip_missing_urls_and_keep_the_first_per_target(store: Store) -> None:
    rows = map_remote_links(KEY, _load("mapping_remote_links.json")["response"])
    assert rows == [
        {"issue_key": KEY, "kind": "remote_link",
         "target": "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=1", "title": "Page"},
        {"issue_key": KEY, "kind": "remote_link",
         "target": "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=2", "title": "Page"},
        {"issue_key": KEY, "kind": "remote_link",
         "target": "https://github.com/example/repo/pull/42", "title": "PR #42: sample change"},
    ]
    assert all(set(row) == _columns(store, "links") for row in rows)


def test_remote_link_edge_cases() -> None:
    assert map_remote_links(KEY, []) == []
    rows = map_remote_links(KEY, [{"object": {"url": "  https://git.example/pr/1  "}}])
    assert rows == [{"issue_key": KEY, "kind": "remote_link", "target": "https://git.example/pr/1", "title": None}]


def test_issue_links_use_the_direction_text_and_merge_per_target(store: Store) -> None:
    issue = _sample_issue()
    issue["fields"]["issuelinks"] = _load("mapping_issuelinks.json")["issuelinks"]
    rows = map_issue_links(issue)
    assert rows == [
        {"issue_key": KEY, "kind": "issue_link", "target": "AIL-1143", "title": "blocks; relates to"},
        {"issue_key": KEY, "kind": "issue_link", "target": "AIL-1120", "title": "is blocked by"},
        {"issue_key": KEY, "kind": "issue_link", "target": "AIL-990", "title": "clones"},
    ]
    assert all(set(row) == _columns(store, "links") for row in rows)


def test_issue_without_links_gives_no_rows() -> None:
    assert map_issue_links(_sample_issue()) == []
    assert map_issue_links({"id": "1", "key": "AIL-1", "fields": {"issuelinks": None}}) == []
    assert map_issue_links({"id": "1", "key": "AIL-1"}) == []


def test_link_type_without_direction_text_gives_no_title() -> None:
    issue = {"key": "AIL-1", "fields": {"issuelinks": [{"type": {"name": "Odd"}, "inwardIssue": {"key": "AIL-2"}}]}}
    assert map_issue_links(issue) == [{"issue_key": "AIL-1", "kind": "issue_link", "target": "AIL-2", "title": None}]


def test_statuses_appear_once_across_issue_types(store: Store) -> None:
    rows = map_statuses("AIL", _load("mapping_project_statuses.json")["response"])
    assert rows == [
        {"project_key": "AIL", "status_id": "10084", "name": "To Do", "category": "new"},
        {"project_key": "AIL", "status_id": "10085", "name": "In Progress", "category": "indeterminate"},
        {"project_key": "AIL", "status_id": "10086", "name": "Done", "category": "done"},
    ]
    assert all(set(row) == _columns(store, "statuses") for row in rows)


def test_status_edge_cases() -> None:
    project_statuses = [
        {"name": "Task", "statuses": [{"id": 10087, "name": "In Review"}]},
        {"name": "Bug", "statuses": None},
        {"name": "Test"},
    ]
    assert map_statuses("AIL", project_statuses) == [
        {"project_key": "AIL", "status_id": "10087", "name": "In Review", "category": None}
    ]
    assert map_statuses("AIL", []) == []


@pytest.mark.parametrize("status", [{"name": "To Do"}, {"id": "10084"}, {"id": "10084", "name": ""}])
def test_status_without_an_id_or_a_name_is_rejected(status: dict[str, Any]) -> None:
    with pytest.raises(JiraError, match="of project AIL without"):
        map_statuses("AIL", [{"statuses": [status]}])


def member(account: str = "712020:aaaa-0001", **fields: Any) -> dict[str, Any]:
    """A user object as the assignable-user search returns one: everything Jira can send (made up)."""
    user: dict[str, Any] = {
        "self": f"https://example.invalid/rest/api/3/user?accountId={account}",
        "accountId": account,
        "accountType": "atlassian",
        "emailAddress": "noor.haddad@example.invalid",
        "avatarUrls": {size: f"https://avatar-management--avatars.us-west-2.prod.public.atl-paas.net/{account}/{size}"
                       for size in ("16x16", "24x24", "32x32", "48x48")},
        "displayName": "Noor Haddad",
        "active": True,
        "timeZone": "Asia/Dubai",
        "locale": "en_GB",
    }
    user.update(fields)
    return user


def test_a_member_is_a_people_row_as_an_assignee_is_and_nothing_more() -> None:
    row = map_member(member(), T0)
    assert row == {
        "account_id": "712020:aaaa-0001",
        "display_name": "Noor Haddad",
        "active": 1,
        "updated_at": T0,
        "avatar_url": "https://avatar-management--avatars.us-west-2.prod.public.atl-paas.net/712020:aaaa-0001/48x48",
    }
    text = json.dumps(row)
    for kept_out in ("noor.haddad@example.invalid", "Asia/Dubai", "en_GB", "example.invalid/rest", "16x16", "32x32"):
        assert kept_out not in text
    assert row == map_assignee({"fields": {"assignee": member()}}, T0)
    with Store.open(":memory:") as store:
        store.upsert("people", row)


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"accountType": "app", "displayName": "Timesheets app"}, id="an app (Tempo, Automation)"),
        pytest.param({"accountType": "customer"}, id="a customer account"),
        pytest.param({"accountType": "unknown"}, id="any other type"),
        pytest.param({"accountType": None}, id="no type"),
        pytest.param({"active": False}, id="inactive"),
        pytest.param({"active": None}, id="activity not said"),
        pytest.param({"active": "true"}, id="active as text"),
        pytest.param({"accountId": ""}, id="a blank account id"),
        pytest.param({"accountId": None}, id="no account id"),
    ],
)
def test_apps_customers_and_inactive_accounts_are_never_members(fields: dict[str, Any]) -> None:
    assert map_member(member(**fields), T0) is None


def test_a_member_without_a_name_or_a_picture_still_counts() -> None:
    user = member()
    del user["displayName"], user["avatarUrls"]
    assert map_member(user, T0) == {"account_id": "712020:aaaa-0001", "display_name": None, "active": 1,
                                    "updated_at": T0, "avatar_url": None}
