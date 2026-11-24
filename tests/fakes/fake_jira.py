from __future__ import annotations

import base64
import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from oversight.jira.client import JiraClient, route_of
from oversight.timeutil import UTC, epoch_ms

BASE_URL = "https://fake.atlassian.net"
EMAIL = "sync@example.com"
TOKEN = "tok-sync-5e2b-do-not-leak"
CHICAGO = "America/Chicago"
BUILT_FROM = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)

MYSELF = "/rest/api/3/myself"
FIELD = "/rest/api/3/field"
PROJECT = "/rest/api/3/project/{key}"
PROJECT_SEARCH = "/rest/api/3/project/search"
STATUSES = "/rest/api/3/project/{key}/statuses"
SEARCH = "/rest/api/3/search/jql"
BULK = "/rest/api/3/changelog/bulkfetch"
CHANGELOG = "/rest/api/3/issue/{key}/changelog"
WORKLOGS = "/rest/api/3/issue/{key}/worklog"
REMOTE_LINKS = "/rest/api/3/issue/{key}/remotelink"
UPDATED_FEED = "/rest/api/3/worklog/updated"
DELETED_FEED = "/rest/api/3/worklog/deleted"
WORKLOG_LIST = "/rest/api/3/worklog/list"
ASSIGNABLE = "/rest/api/3/user/assignable/search"
SITE_USERS = "/rest/api/3/users/search"

ASSIGNABLE_LIMIT = 1000

POINTS_FIELD = "customfield_10016"
SPRINT_FIELD = "customfield_10020"

STATUS_INFO = {
    "To Do": ("10084", "new", 2, "blue-gray"),
    "In Progress": ("10085", "indeterminate", 4, "yellow"),
    "Done": ("10086", "done", 3, "green"),
}
ISSUE_TYPES = {
    "Task": ("10081", 0),
    "Epic": ("10082", 1),
    "Subtask": ("10083", -1),
    "Bug": ("10084", 0),
    "Test": ("10085", 0),
}
LINK_TYPES = {
    "Blocks": ("10000", "is blocked by", "blocks"),
    "Cloners": ("10001", "is cloned by", "clones"),
    "Relates": ("10003", "relates to", "relates to"),
}
SPRINTS = {4400: ("Sample Sprint 9", "closed"), 4427: ("Sample Sprint 10", "active")}
PEOPLE = {"acc-dev-1": "Developer One", "acc-dev-2": "Developer Two"}
TEMPO = {
    "accountId": "app-tempo",
    "accountType": "app",
    "displayName": "Timesheets by Tempo - Jira Time Tracking",
    "active": True,
}

_GREENHOPPER = "com.pyxis.greenhopper.jira"
_PEOPLE_TYPE = "com.atlassian.jira.plugin.system.customfieldtypes:people"
SITE_FIELDS: list[tuple[str, str, str, str | None]] = [
    ("summary", "Summary", "string", None),
    ("status", "Status", "status", None),
    ("assignee", "Assignee", "user", None),
    ("parent", "Parent", "issuelink", None),
    ("timeoriginalestimate", "Original estimate", "number", None),
    ("timeestimate", "Remaining Estimate", "number", None),
    ("timespent", "Time Spent", "number", None),
    ("customfield_10000", "Development", "any",
     "com.atlassian.jira.plugins.jira-development-integration-plugin:devsummarycf"),
    (POINTS_FIELD, "Story point estimate", "number", f"{_GREENHOPPER}:jsw-story-points"),
    ("customfield_10019", "Rank", "any", f"{_GREENHOPPER}:gh-lexo-rank"),
    (SPRINT_FIELD, "Sprint", "array", f"{_GREENHOPPER}:gh-sprint"),
    ("customfield_10138", "Programmer", "array", _PEOPLE_TYPE),
    ("customfield_10139", "Code Reviewers", "array", _PEOPLE_TYPE),
    ("customfield_10140", "Testers", "array", _PEOPLE_TYPE),
    ("customfield_10141", "Test Reviewers", "array", _PEOPLE_TYPE),
]
PROBE_JQL = re.compile(r"project in \((?P<keys>[^)]*)\) AND cf\[(?P<number>[0-9]+)\] is not EMPTY")


@dataclass
class FakeIssue:
    id: str
    key: str
    project: str
    type: str
    summary: str
    created: datetime
    updated: datetime
    parent: str | None = None
    status: str = "To Do"
    assignee: str | None = None
    points: float | None = None
    sprints: list[int] = field(default_factory=list)
    resolved: datetime | None = None
    time_spent: int | None = None
    histories: list[dict[str, Any]] = field(default_factory=list)
    remote_links: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FakeWorklog:
    id: int
    issue_id: str
    seconds: int
    started: datetime
    updated: datetime


Fault = Callable[[httpx.Request], httpx.Response | None]


class FakeJira:
    def __init__(self) -> None:
        self.clock = BUILT_FROM
        self.zone = ZoneInfo(CHICAGO)
        self.reported_zone: str | None = CHICAGO
        self.authorization = "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode("ascii")
        self.fields: list[dict[str, Any]] = [_field(*spec) for spec in SITE_FIELDS]
        self.project_ids: dict[str, str] = {}
        self.project_names: dict[str, str] = {}
        self.project_types: dict[str, str] = {}
        self.archived_projects: set[str] = set()
        self.hidden_projects: set[str] = set()
        self.project_search_cap = 50
        self.issues: dict[str, FakeIssue] = {}
        self.worklogs: dict[int, FakeWorklog] = {}
        self.deleted_worklogs: list[tuple[int, datetime]] = []
        self.issue_links: list[tuple[str, str, str, str]] = []
        self.members: dict[str, list[dict[str, Any]]] = {}
        self.members_first_place = 0
        self.members_spread = 1
        self.members_ignore_start = False
        self.site_user_count = 25
        self.browse_users = True
        self.search_page_cap = 100
        self.bulk_page_size = 1000
        self.changelog_page_cap = 100
        self.feed_page_size = 1000
        self.request_tick = timedelta(0)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.faults: list[Fault] = []
        self.after_request: list[Callable[[httpx.Request], None]] = []
        self._numbers: dict[str, int] = {}
        self._issue_ids = itertools.count(50001)
        self._history_ids = itertools.count(400001)
        self._worklog_ids = itertools.count(70001)
        self._link_ids = itertools.count(20001)
        self._remote_ids = itertools.count(80001)
        self._pr_numbers = itertools.count(1)
        self._tokens: dict[str, Any] = {}

    def client_kwargs(self) -> dict[str, Any]:
        return {
            "base_url": BASE_URL,
            "email": EMAIL,
            "api_token": TOKEN,
            "transport": httpx.MockTransport(self),
        }

    def client(self) -> JiraClient:
        return JiraClient(**self.client_kwargs(), sleep=self.sleeps.append)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.clock += self.request_tick
        response = None
        if request.headers.get("Authorization") != self.authorization:
            response = _error(401, "Client must be authenticated to access this resource.")
        for fault in self.faults if response is None else ():
            response = fault(request)
            if response is not None:
                break
        if response is None:
            response = self._answer(request)
        for hook in list(self.after_request):
            hook(request)
        return response

    def fail(
        self, route: str, *, status: int = 503, when: Callable[[httpx.Request], bool] = lambda _r: True
    ) -> Callable[[], None]:

        def fault(request: httpx.Request) -> httpx.Response | None:
            if route_of(request.url.path) == route and when(request):
                return _error(status, "injected failure")
            return None

        self.faults.append(fault)
        return lambda: self.faults.remove(fault)

    def bulkfetch_unavailable(self, status: int = 404) -> None:
        self.fail(BULK, status=status)

    def calls(self, route: str) -> list[httpx.Request]:
        return [r for r in self.requests if route_of(r.url.path) == route]

    def keys_called(self, route: str) -> list[str]:
        return [r.url.path.split("/")[5] for r in self.calls(route)]

    def jqls(self) -> list[str]:
        return [json.loads(r.content)["jql"] for r in self.calls(SEARCH)]

    def add_project(self, key: str, name: str | None = None, project_type: str = "software") -> None:
        self.project_ids[key] = str(10023 + len(self.project_ids))
        self.project_names[key] = name or f"{key} (sample)"
        self.project_types[key] = project_type
        self._numbers[key] = 0

    def issue(self, key: str) -> FakeIssue:
        return next(issue for issue in self.issues.values() if issue.key == key)

    def add_member(self, project: str, account: str, name: str | None = None, **extra: Any) -> dict[str, Any]:
        user: dict[str, Any] = {
            "self": f"{BASE_URL}/rest/api/3/user?accountId={account}",
            "accountId": account,
            "accountType": "atlassian",
            "emailAddress": f"{account}@example.invalid",
            "displayName": name if name is not None else PEOPLE.get(account, f"Member {account}"),
            "active": True,
            "timeZone": "Europe/Berlin",
            "locale": "de_DE",
            **extra,
        }
        self.members.setdefault(project, []).append(user)
        return user

    def project_issues(self, project: str) -> list[FakeIssue]:
        return [issue for issue in self.issues.values() if issue.project == project]

    def worklogs_of(self, key: str) -> list[FakeWorklog]:
        issue_id = self.issue(key).id
        return [w for w in self.worklogs.values() if w.issue_id == issue_id]

    def _stamp(self) -> datetime:
        at = self.clock
        self.clock += timedelta(seconds=1)
        return at

    def _change(self, issue: FakeIssue, *items: dict[str, Any], author: str = "acc-dev-1") -> datetime:
        at = self._stamp()
        history_id = str(next(self._history_ids))
        issue.histories.append({"id": history_id, "author": author, "created": at, "items": list(items)})
        issue.updated = at
        return at

    def create(self, project: str, issue_type: str, *, parent: str | None = None) -> FakeIssue:
        self._numbers[project] += 1
        key = f"{project}-{self._numbers[project]}"
        at = self._stamp()
        issue = FakeIssue(
            id=str(next(self._issue_ids)),
            key=key,
            project=project,
            type=issue_type,
            summary=f"Sample {issue_type.lower()} (sanitised)",
            created=at,
            updated=at,
            parent=parent,
        )
        self.issues[issue.id] = issue
        return issue

    def assign(self, key: str, account: str) -> None:
        issue = self.issue(key)
        old_name = PEOPLE.get(issue.assignee or "")
        item = _item("assignee", "jira", "assignee", issue.assignee, old_name, account, PEOPLE[account])
        issue.assignee = account
        self._change(issue, item)

    def set_points(self, key: str, points: float) -> None:
        issue = self.issue(key)
        item = _item(
            "Story point estimate", "custom", POINTS_FIELD, None, _num(issue.points), None, _num(points)
        )
        issue.points = points
        self._change(issue, item)

    def set_sprints(self, key: str, sprint_ids: list[int]) -> None:
        issue = self.issue(key)
        item = _item(
            "Sprint",
            "custom",
            SPRINT_FIELD,
            ", ".join(map(str, issue.sprints)),
            ", ".join(SPRINTS[s][0] for s in issue.sprints),
            ", ".join(map(str, sprint_ids)),
            ", ".join(SPRINTS[s][0] for s in sprint_ids),
        )
        issue.sprints = list(sprint_ids)
        self._change(issue, item)

    def transition(self, key: str, status: str) -> None:
        issue = self.issue(key)
        old_id, new_id = STATUS_INFO[issue.status][0], STATUS_INFO[status][0]
        items = [_item("status", "jira", "status", old_id, issue.status, new_id, status)]
        if status == "Done":
            items.append(_item("resolution", "jira", "resolution", None, None, "10000", "Done"))
        elif issue.status == "Done":
            items.append(_item("resolution", "jira", "resolution", "10000", "Done", None, None))
        issue.status = status
        at = self._change(issue, *items)
        issue.resolved = at if status == "Done" else None

    def log_work(self, key: str, seconds: int = 7200) -> int:
        issue = self.issue(key)
        worklog_id = next(self._worklog_ids)
        total = (issue.time_spent or 0) + seconds
        at = self._change(
            issue,
            _item("WorklogId", "jira", None, None, None, str(worklog_id), str(worklog_id)),
            _timespent_item(issue.time_spent, total),
            author="app-tempo",
        )
        self.worklogs[worklog_id] = FakeWorklog(worklog_id, issue.id, seconds, at - timedelta(hours=3), at)
        issue.time_spent = total
        return worklog_id

    def delete_worklog(self, worklog_id: int) -> None:
        worklog = self.worklogs.pop(worklog_id)
        issue = self.issues[worklog.issue_id]
        total = (issue.time_spent or 0) - worklog.seconds
        at = self._change(
            issue,
            _item("WorklogId", "jira", None, str(worklog_id), str(worklog_id), None, None),
            _timespent_item(issue.time_spent, total),
            author="app-tempo",
        )
        issue.time_spent = total
        self.deleted_worklogs.append((worklog_id, at))

    def add_remote_link(self, key: str) -> None:
        issue = self.issue(key)
        number = next(self._pr_numbers)
        link_id = next(self._remote_ids)
        if number % 2:
            app, relationship = {"type": "com.github", "name": "GitHub"}, "pull request"
            url, title = f"https://github.com/example/repo/pull/{number}", f"PR #{number}"
        else:
            app, relationship = {"type": "com.atlassian.confluence", "name": "Confluence"}, "Wiki Page"
            url, title = f"https://example.atlassian.net/wiki/pages/viewpage.action?pageId={number}", "Page"
        issue.remote_links.append(
            {
                "id": link_id,
                "self": f"{BASE_URL}/rest/api/3/issue/{key}/remotelink/{link_id}",
                "application": app,
                "relationship": relationship,
                "object": {"url": url, "title": title, "icon": {}},
            }
        )
        note = "This work item links to a page"
        self._change(issue, _item("RemoteWorkItemLink", "jira", None, None, None, str(link_id), note))

    def link(self, type_name: str, from_key: str, to_key: str) -> None:
        source, target = self.issue(from_key), self.issue(to_key)
        self.issue_links.append((str(next(self._link_ids)), type_name, source.id, target.id))
        _, inward, outward = LINK_TYPES[type_name]
        forwards = f"This work item {outward} {to_key}"
        backwards = f"This work item {inward} {from_key}"
        self._change(source, _item("Link", "jira", None, None, None, to_key, forwards))
        self._change(target, _item("Link", "jira", None, None, None, from_key, backwards))

    def edit_description(self, key: str) -> None:
        edit = _item("description", "jira", "description", None, None, None, "<redacted>")
        self._change(self.issue(key), edit)

    def rerank(self, key: str) -> None:
        rank = _item("Rank", "custom", "customfield_10019", "", "", "", "Ranked higher")
        self._change(self.issue(key), rank)

    def delete_issue(self, key: str) -> None:
        issue = self.issue(key)
        at = self._stamp()
        for worklog in self.worklogs_of(key):
            del self.worklogs[worklog.id]
            self.deleted_worklogs.append((worklog.id, at))
        del self.issues[issue.id]

    def move(self, key: str, project: str) -> str:
        issue = self.issue(key)
        self._numbers[project] += 1
        new_key = f"{project}-{self._numbers[project]}"
        old_project = issue.project
        old_id, new_id = self.project_ids[old_project], self.project_ids[project]
        issue.key, issue.project, issue.parent = new_key, project, None
        self._change(
            issue,
            _item("Key", "jira", "issuekey", None, key, None, new_key),
            _item("project", "jira", "project", old_id, old_project, new_id, project),
        )
        return new_key

    def ts(self, at: datetime) -> str:
        local = at.astimezone(self.zone)
        millis = f"{local.microsecond // 1000:03d}"
        return local.strftime("%Y-%m-%dT%H:%M:%S.") + millis + local.strftime("%z")

    def status_object(self, name: str) -> dict[str, Any]:
        status_id, category, category_id, colour = STATUS_INFO[name]
        return {
            "self": f"{BASE_URL}/rest/api/3/status/{status_id}",
            "description": "",
            "iconUrl": f"{BASE_URL}/",
            "name": name,
            "untranslatedName": name,
            "id": status_id,
            "statusCategory": {
                "self": f"{BASE_URL}/rest/api/3/statuscategory/{category_id}",
                "id": category_id,
                "key": category,
                "colorName": colour,
                "name": name,
            },
        }

    def type_object(self, name: str) -> dict[str, Any]:
        type_id, level = ISSUE_TYPES[name]
        return {
            "self": f"{BASE_URL}/rest/api/3/issuetype/{type_id}",
            "id": type_id,
            "description": "",
            "name": name,
            "subtask": level == -1,
            "hierarchyLevel": level,
        }

    def brief(self, issue: FakeIssue) -> dict[str, Any]:
        return {
            "id": issue.id,
            "key": issue.key,
            "self": f"{BASE_URL}/rest/api/3/issue/{issue.id}",
            "fields": {
                "summary": issue.summary,
                "status": self.status_object(issue.status),
                "priority": {"name": "Medium", "id": "3"},
                "issuetype": self.type_object(issue.type),
            },
        }

    def issuelinks(self, issue: FakeIssue) -> list[dict[str, Any]]:
        links = []
        for link_id, type_name, from_id, to_id in self.issue_links:
            type_id, inward, outward = LINK_TYPES[type_name]
            link: dict[str, Any] = {
                "id": link_id,
                "self": f"{BASE_URL}/rest/api/3/issueLink/{link_id}",
                "type": {"id": type_id, "name": type_name, "inward": inward, "outward": outward},
            }
            if from_id == issue.id and to_id in self.issues:
                links.append(link | {"outwardIssue": self.brief(self.issues[to_id])})
            elif to_id == issue.id and from_id in self.issues:
                links.append(link | {"inwardIssue": self.brief(self.issues[from_id])})
        return links

    def payload(self, issue: FakeIssue, fields: list[str] | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {
            "project": {
                "self": f"{BASE_URL}/rest/api/3/project/{self.project_ids[issue.project]}",
                "id": self.project_ids[issue.project],
                "key": issue.project,
                "name": issue.project,
                "projectTypeKey": "software",
                "simplified": True,
            },
            "issuetype": self.type_object(issue.type),
            "summary": issue.summary,
            "status": self.status_object(issue.status),
            "assignee": None
            if issue.assignee is None
            else {
                "self": f"{BASE_URL}/rest/api/3/user?accountId={issue.assignee}",
                "accountId": issue.assignee,
                "displayName": PEOPLE[issue.assignee],
                "active": True,
                "timeZone": CHICAGO,
                "accountType": "atlassian",
            },
            "created": self.ts(issue.created),
            "updated": self.ts(issue.updated),
            "resolutiondate": None if issue.resolved is None else self.ts(issue.resolved),
            "timeoriginalestimate": None,
            "timeestimate": None,
            "timespent": issue.time_spent,
            "aggregatetimespent": issue.time_spent,
            POINTS_FIELD: issue.points,
            SPRINT_FIELD: [self.sprint_object(s) for s in issue.sprints] or None,
            "customfield_10000": "{}",
            "customfield_10138": None,
            "customfield_10139": None,
            "customfield_10140": None,
            "customfield_10141": None,
            "issuelinks": self.issuelinks(issue),
            "subtasks": [],
        }
        if issue.parent is not None:
            values["parent"] = self.brief(self.issue(issue.parent))
        if fields is not None:
            values = {name: value for name, value in values.items() if name in fields}
        return {
            "expand": "renderedFields,names,schema,operations,editmeta,changelog,versionedRepresentations",
            "id": issue.id,
            "self": f"{BASE_URL}/rest/api/3/issue/{issue.id}",
            "key": issue.key,
            "fields": values,
        }

    def sprint_object(self, sprint_id: int) -> dict[str, Any]:
        name, state = SPRINTS[sprint_id]
        return {
            "id": sprint_id,
            "name": name,
            "state": state,
            "boardId": 21,
            "goal": "",
            "startDate": "2026-10-19T08:15:10.000Z",
            "endDate": "2026-11-16T06:00:00.000Z",
        }

    def history(self, history: dict[str, Any], *, epoch: bool) -> dict[str, Any]:
        if history["author"] == "app-tempo":
            author = TEMPO
        else:
            author = {
                "accountId": history["author"],
                "displayName": PEOPLE[history["author"]],
                "active": True,
                "timeZone": CHICAGO,
                "accountType": "atlassian",
            }
        created = history["created"]
        return {
            "id": history["id"],
            "author": author,
            "created": epoch_ms(created) if epoch else self.ts(created),
            "items": history["items"],
        }

    def worklog(self, worklog: FakeWorklog) -> dict[str, Any]:
        return {
            "self": f"{BASE_URL}/rest/api/3/issue/{worklog.issue_id}/worklog/{worklog.id}",
            "author": TEMPO,
            "updateAuthor": TEMPO,
            "created": self.ts(worklog.updated),
            "updated": self.ts(worklog.updated),
            "started": self.ts(worklog.started),
            "timeSpent": f"{worklog.seconds // 3600}h",
            "timeSpentSeconds": worklog.seconds,
            "id": str(worklog.id),
            "issueId": worklog.issue_id,
        }

    def project_statuses(self) -> list[dict[str, Any]]:
        return [
            {
                "self": f"{BASE_URL}/rest/api/3/issuetype/{type_id}",
                "id": type_id,
                "name": name,
                "subtask": level == -1,
                "statuses": [self.status_object(status) for status in STATUS_INFO],
            }
            for name, (type_id, level) in ISSUE_TYPES.items()
        ]

    def _answer(self, request: httpx.Request) -> httpx.Response:
        handlers: dict[tuple[str, str | None], Callable[[httpx.Request], httpx.Response]] = {
            ("GET", MYSELF): self._myself,
            ("GET", FIELD): lambda _r: httpx.Response(200, json=self.fields),
            ("GET", PROJECT): self._project,
            ("GET", PROJECT_SEARCH): self._project_search,
            ("GET", STATUSES): self._statuses,
            ("POST", SEARCH): self._search,
            ("POST", BULK): self._bulkfetch,
            ("GET", CHANGELOG): self._changelog,
            ("GET", WORKLOGS): self._issue_worklogs,
            ("GET", REMOTE_LINKS): self._remote_links,
            ("GET", UPDATED_FEED): self._updated_feed,
            ("GET", DELETED_FEED): lambda r: self._feed(r, self.deleted_worklogs),
            ("POST", WORKLOG_LIST): self._worklog_list,
            ("GET", ASSIGNABLE): self._assignable,
            ("GET", SITE_USERS): self._site_users,
        }
        handler = handlers.get((request.method, route_of(request.url.path)))
        if handler is None:
            return _error(404, "not served by the fake")
        return handler(request)

    def _by_ref(self, ref: str) -> FakeIssue | None:
        return next((i for i in self.issues.values() if ref in (i.key, i.id)), None)

    def _find(self, request: httpx.Request) -> FakeIssue | None:
        return self._by_ref(request.url.path.split("/")[5])

    def _myself(self, _request: httpx.Request) -> httpx.Response:
        me: dict[str, Any] = {
            "self": f"{BASE_URL}/rest/api/3/user?accountId=acc-sync",
            "accountId": "acc-sync",
            "accountType": "atlassian",
            "emailAddress": EMAIL,
            "displayName": "Sync Account",
            "active": True,
            "locale": "en_US",
        }
        if self.reported_zone is not None:
            me["timeZone"] = self.reported_zone
        return httpx.Response(200, json=me)

    def _project(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.split("/")[5]
        if key not in self.project_ids:
            return _error(404, f"No project could be found with key '{key}'.")
        project_id = self.project_ids[key]
        return httpx.Response(
            200,
            json={
                "self": f"{BASE_URL}/rest/api/3/project/{project_id}",
                "id": project_id,
                "key": key,
                "name": self.project_names[key],
                "projectTypeKey": "software",
                "simplified": True,
                "style": "next-gen",
            },
        )

    def _project_search(self, request: httpx.Request) -> httpx.Response:
        start, size = _offset_params(request, self.project_search_cap)
        live = request.url.params.get("status") == "live"
        keys = sorted(key for key in self.project_ids
                      if key not in self.hidden_projects and not (live and key in self.archived_projects))
        page = keys[start : start + size]
        values = [
            {
                "self": f"{BASE_URL}/rest/api/3/project/{self.project_ids[key]}",
                "id": self.project_ids[key],
                "key": key,
                "name": self.project_names[key],
                "projectTypeKey": self.project_types.get(key, "software"),
                "simplified": True,
                "style": "next-gen",
                "isPrivate": False,
                "avatarUrls": {"48x48": f"{BASE_URL}/rest/api/3/universal_avatar/view/type/project/avatar/10400"},
            }
            for key in page
        ]
        return httpx.Response(200, json={
            "self": f"{BASE_URL}{request.url.path}",
            "maxResults": size,
            "startAt": start,
            "total": len(keys),
            "isLast": start + size >= len(keys),
            "values": values,
        })

    def _statuses(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.split("/")[5] not in self.project_ids:
            return _error(404, "No project could be found")
        return httpx.Response(200, json=self.project_statuses())

    def _probe(self, body: dict[str, Any], match: re.Match[str]) -> httpx.Response:
        keys = re.findall(r'"([^"]+)"', match["keys"])
        unknown = [key for key in keys if key not in self.project_ids]
        if not keys or unknown:
            return _error(400, f"The value '{(unknown or [''])[0]}' does not exist for the field 'project'.")
        field_id = f"customfield_{match['number']}"
        if all(item["id"] != field_id for item in self.fields):
            number = match["number"]
            return _error(400, f"Field 'cf[{number}]' does not exist or you do not have permission to view it.")
        hits = [
            {"id": issue.id}
            for issue in self.issues.values()
            if issue.project in keys and self.payload(issue)["fields"].get(field_id) not in (None, [], "")
        ]
        size = body["maxResults"]
        return httpx.Response(200, json={"issues": hits[:size], "isLast": len(hits) <= size})

    def _search(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        probe = PROBE_JQL.fullmatch(body["jql"])
        if probe is not None:
            return self._probe(body, probe)
        match = JQL.fullmatch(body["jql"])
        if match is None:
            return _error(400, "Error in the JQL Query")
        if match["project"] not in self.project_ids:
            return _error(400, f"The value '{match['project']}' does not exist for the field 'project'.")
        issues = self.project_issues(match["project"])
        if match["since"]:
            since = datetime.strptime(match["since"], "%Y-%m-%d %H:%M").replace(tzinfo=self.zone)
            issues = [i for i in issues if i.updated >= since]
        order = sorted(issues, key=lambda i: (i.updated, int(i.id)))
        if "nextPageToken" in body:
            after = self._tokens[body["nextPageToken"]]
            order = [i for i in order if (i.updated, int(i.id)) > after]
        size = min(body["maxResults"], self.search_page_cap)
        page, rest = order[:size], order[size:]
        answer: dict[str, Any] = {
            "issues": [self.payload(i, body["fields"]) for i in page],
            "isLast": not rest,
        }
        if rest:
            answer["nextPageToken"] = self._token((page[-1].updated, int(page[-1].id)))
        return httpx.Response(200, json=answer)

    def _bulkfetch(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if len(body["issueIdsOrKeys"]) > 1000 or len(body["fieldIds"]) > 10:
            return _error(400, "At most 1000 issues and 10 fields")
        wanted = set(body["fieldIds"])
        flat: list[tuple[str, dict[str, Any]]] = []
        for ref in body["issueIdsOrKeys"]:
            issue = self._by_ref(ref)
            if issue is None:
                continue
            for history in reversed(issue.histories):
                items = [item for item in history["items"] if item.get("fieldId") in wanted]
                if items:
                    flat.append((issue.id, self.history(history, epoch=True) | {"items": items}))
        start = self._tokens[body["nextPageToken"]] if "nextPageToken" in body else 0
        size = min(body["maxResults"], self.bulk_page_size)
        entries: list[dict[str, Any]] = []
        for issue_id, history in flat[start : start + size]:
            if entries and entries[-1]["issueId"] == issue_id:
                entries[-1]["changeHistories"].append(history)
            else:
                entries.append({"issueId": issue_id, "changeHistories": [history]})
        answer: dict[str, Any] = {"issueChangeLogs": entries}
        if start + size < len(flat):
            answer["nextPageToken"] = self._token(start + size)
        return httpx.Response(200, json=answer)

    def _changelog(self, request: httpx.Request) -> httpx.Response:
        issue = self._find(request)
        if issue is None:
            return _error(404, "Issue does not exist or you do not have permission to see it.")
        start, size = _offset_params(request, self.changelog_page_cap)
        values = [self.history(h, epoch=False) for h in issue.histories]
        return httpx.Response(
            200,
            json={
                "self": f"{BASE_URL}{request.url.path}",
                "maxResults": size,
                "startAt": start,
                "total": len(values),
                "isLast": start + size >= len(values),
                "values": values[start : start + size],
            },
        )

    def _issue_worklogs(self, request: httpx.Request) -> httpx.Response:
        issue = self._find(request)
        if issue is None:
            return _error(404, "Issue does not exist or you do not have permission to see it.")
        start, size = _offset_params(request, 5000)
        logs = [self.worklog(w) for w in self.worklogs.values() if w.issue_id == issue.id]
        page = logs[start : start + size]
        return httpx.Response(
            200, json={"startAt": start, "maxResults": size, "total": len(logs), "worklogs": page}
        )

    def _remote_links(self, request: httpx.Request) -> httpx.Response:
        issue = self._find(request)
        if issue is None:
            return _error(404, "Issue does not exist or you do not have permission to see it.")
        return httpx.Response(200, json=issue.remote_links)

    def _updated_feed(self, request: httpx.Request) -> httpx.Response:
        return self._feed(request, [(w.id, w.updated) for w in self.worklogs.values()])

    def _feed(self, request: httpx.Request, entries: list[tuple[int, datetime]]) -> httpx.Response:
        since = int(request.url.params["since"])
        newer = sorted((at, worklog_id) for worklog_id, at in entries if epoch_ms(at) > since)
        page, rest = newer[: self.feed_page_size], newer[self.feed_page_size :]
        until = epoch_ms(page[-1][0]) if page else since
        answer: dict[str, Any] = {
            "values": [{"worklogId": wid, "updatedTime": epoch_ms(at), "properties": []} for at, wid in page],
            "since": since,
            "until": until,
            "self": f"{BASE_URL}{request.url.path}?since={since}",
            "lastPage": not rest,
        }
        if rest:
            answer["nextPage"] = f"{BASE_URL}{request.url.path}?since={until}"
        return httpx.Response(200, json=answer)

    def _worklog_list(self, request: httpx.Request) -> httpx.Response:
        ids = [int(i) for i in json.loads(request.content)["ids"]]
        if len(ids) > 1000:
            return _error(400, "At most 1000 ids")
        return httpx.Response(200, json=[self.worklog(self.worklogs[i]) for i in ids if i in self.worklogs])

    def _assignable(self, request: httpx.Request) -> httpx.Response:
        project = request.url.params.get("project", "")
        if project not in self.project_ids:
            return _error(404, f"No project could be found with key '{project}'.")
        start, size = _offset_params(request, 1000)
        users = self.members.get(project, [])
        start = 0 if self.members_ignore_start else start
        end = min(start + size, ASSIGNABLE_LIMIT)
        first, spread = self.members_first_place, self.members_spread
        return httpx.Response(200, json=[user for n, user in enumerate(users) if start <= first + n * spread < end])

    def _site_users(self, request: httpx.Request) -> httpx.Response:
        if not self.browse_users:
            return _error(403, "You do not have the permission to browse users and groups.")
        start = int(request.url.params.get("startAt", 0))
        end = min(start + min(int(request.url.params.get("maxResults", 50)), 1000), self.site_user_count)
        return httpx.Response(200, json=[_site_user(place) for place in range(start, end)])

    def _token(self, position: Any) -> str:
        token = f"tok{len(self._tokens) + 1:04d}"
        self._tokens[token] = position
        return token


JQL = re.compile(
    r'project = "(?P<project>[A-Z][A-Z0-9_]*)"'
    r'(?: AND updated >= "(?P<since>\d{4}-\d{2}-\d{2} \d{2}:\d{2})")?'
    r" ORDER BY updated ASC"
)


def _item(
    name: str, kind: str, field_id: str | None, raw_from: Any, text_from: Any, raw_to: Any, text_to: Any
) -> dict[str, Any]:
    item = {
        "field": name,
        "fieldtype": kind,
        "from": raw_from,
        "fromString": text_from,
        "to": raw_to,
        "toString": text_to,
    }
    if field_id is not None:
        item["fieldId"] = field_id
    return item


def _timespent_item(before: int | None, after: int) -> dict[str, Any]:
    return _item("timespent", "jira", "timespent", _num(before), _num(before), str(after), str(after))


def _num(value: float | int | None) -> str | None:
    if value is None:
        return None
    return str(int(value)) if float(value).is_integer() else str(value)


def _field(field_id: str, name: str, schema_type: str, custom_type: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": field_id,
        "key": field_id,
        "name": name,
        "custom": custom_type is not None,
        "orderable": True,
        "navigable": True,
        "searchable": True,
    }
    if custom_type is None:
        item |= {"clauseNames": [field_id], "schema": {"type": schema_type, "system": field_id}}
    else:
        number = int(field_id.rsplit("_", 1)[1])
        item |= {
            "untranslatedName": name,
            "clauseNames": [f"cf[{number}]", name],
            "schema": {"type": schema_type, "custom": custom_type, "customId": number},
        }
    return item


def _site_user(place: int) -> dict[str, Any]:
    account = f"acc-site-{place:04d}"
    return {
        "self": f"{BASE_URL}/rest/api/3/user?accountId={account}",
        "accountId": account,
        "accountType": "atlassian",
        "emailAddress": f"{account}@example.invalid",
        "displayName": f"Site User {place}",
        "active": True,
    }


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"errorMessages": [message], "errors": {}})


def _offset_params(request: httpx.Request, cap: int) -> tuple[int, int]:
    return int(request.url.params.get("startAt", 0)), min(int(request.url.params.get("maxResults", cap)), cap)
