# Jira Cloud REST shapes used by the oversight client

The reference for `oversight/jira/client.py`, `oversight/jira/mapping.py` and
`tests/fakes/fake_jira.py`. "Observed" means seen on the stellaint site
(project AIL, team-managed) through a read-only lookup on 4 Nov 2026.
"Documented" means taken from Atlassian's REST v3 documentation and not yet
observed here. Code must accept both forms wherever they could differ.

Sanitised real samples: `issue_sample.json` (one search result) and
`changelog_sample.json` (its changelog, oldest first).

## Site facts (observed)

- Site `https://stellaint.atlassian.net`. Project AIL (id 10023, name "AILED"):
  `simplified: true`, `style: "next-gen"`, so it is **team-managed**.
- Issue types: Task 10081 (level 0), Epic 10082 (level 1), Subtask 10083 (level -1, `subtask: true`), Bug 10084 (level 0), Test 10085 (level 0).
- Statuses: To Do 10084 (category `new`), In Progress 10085 (`indeterminate`), Done 10086 (`done`).
- Fields: Story point estimate `customfield_10016` (number, `com.pyxis.greenhopper.jira:jsw-story-points`);
  Sprint `customfield_10020` (`gh-sprint`); Rank `customfield_10019`; Development `customfield_10000`
  (`devsummarycf`); Programmer `customfield_10138`, Code Reviewers `customfield_10139`, Testers
  `customfield_10140`, Test Reviewers `customfield_10141` (all multi-user `people` fields).
  Epics are linked through the system `parent` field (there is no Epic Link field on team-managed projects).
- The account's time zone is `America/Chicago`. JQL dates are read in that zone. Timestamps come back
  with the offset written without a colon: `2026-11-03T22:54:38.662-0600`.
- Worklogs are written by the Tempo app account (`accountType: "app"`,
  displayName "Timesheets by Tempo - Jira Time Tracking").

## GET /rest/api/3/myself (documented, plus the observed zone)

```json
{"accountId": "acc-dev-1", "accountType": "atlassian", "emailAddress": "...", "displayName": "...",
 "active": true, "timeZone": "America/Chicago", "locale": "en_US"}
```

## GET /rest/api/3/field (documented, ids observed)

A JSON **list**:
```json
[{"id": "customfield_10016", "key": "customfield_10016", "name": "Story point estimate", "custom": true,
  "orderable": true, "navigable": true, "searchable": true, "clauseNames": ["cf[10016]", "Story point estimate"],
  "schema": {"type": "number", "custom": "com.pyxis.greenhopper.jira:jsw-story-points", "customId": 10016}},
 {"id": "status", "key": "status", "name": "Status", "custom": false, "schema": {"type": "status", "system": "status"}}]
```
Field names are not unique across a site: two custom fields can share a name.

## GET /rest/api/3/project/{key} (observed)

```json
{"id": "10023", "key": "AIL", "name": "AILED", "projectTypeKey": "software", "simplified": true, "style": "next-gen",
 "issueTypes": [{"id": "10081", "name": "Task", "subtask": false, "hierarchyLevel": 0}]}
```
404 when the project doesn't exist or the account can't browse it.

## GET /rest/api/3/project/{key}/statuses (documented)

A list with **one entry per issue type**; the same status repeats across types:
```json
[{"self": "...", "id": "10081", "name": "Task", "subtask": false,
  "statuses": [{"self": "...", "description": "", "iconUrl": "...", "name": "To Do", "untranslatedName": "To Do",
                "id": "10084", "statusCategory": {"self": "...", "id": 2, "key": "new", "colorName": "blue-gray", "name": "To Do"}}]}]
```

## POST /rest/api/3/search/jql (enhanced search; documented, issue shape observed)

Request: `{"jql": "...", "fields": ["summary", "customfield_10016"], "maxResults": 100, "nextPageToken": "..."}`
(omit `nextPageToken` on the first page). Response:
```json
{"issues": [ {"id": "52507", "key": "AIL-1140", "self": "...", "fields": {}} ], "nextPageToken": "CAEaAggD", "isLast": false}
```
There is **no `total`**. The last page has no `nextPageToken` and/or `isLast: true`. Jira may return fewer
issues than `maxResults`. Issue `fields` are as in `issue_sample.json`:
- `status.statusCategory.key` is `new` | `indeterminate` | `done`.
- `parent` (team-managed): `{"id", "key", "fields": {"summary", "status", "priority", "issuetype": {"name": "Epic", "hierarchyLevel": 1}}}`.
  A subtask's parent is a task (`hierarchyLevel` 0).
- Sprint field: a list of `{"id": 4427, "name", "state": "active"|"closed"|"future", "boardId": 21, "goal", "startDate", "endDate"}`, or null.
- Story points: a number (e.g. `0.5`), or null.
- `timeoriginalestimate`, `timeestimate`, `timespent`, `aggregatetimespent`: integer seconds or null.
- `issuelinks` (documented): `[{"id": "10001", "type": {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
  "outwardIssue": {"id": "52510", "key": "AIL-1143", "fields": {"status": {}, "issuetype": {}}}}]`.
  The linked issue is `outwardIssue` or `inwardIssue`.
- `resolutiondate` is set when the issue reaches Done in AIL and cleared if it is reopened.

## POST /rest/api/3/changelog/bulkfetch (documented)

Request: `{"issueIdsOrKeys": ["AIL-1140"], "fieldIds": ["status", "customfield_10016"], "maxResults": 1000, "nextPageToken": "..."}`.
At most 1000 issues and **10 field ids** per request. Response:
```json
{"issueChangeLogs": [{"issueId": "52507", "changeHistories": [
   {"id": "334350", "author": {"accountId": "acc-dev-1"}, "created": 1762146596971,
    "items": [{"field": "status", "fieldtype": "jira", "fieldId": "status", "from": "10084", "fromString": "To Do", "to": "10085", "toString": "In Progress"}]}]}],
 "nextPageToken": "UxAQBFRF"}
```
- Entries are keyed by **`issueId`, not the key**.
- The documentation example shows `created` as an epoch number. The other changelog endpoints send ISO
  strings. Accept both (`oversight.timeutil.parse_ts` does).
- One issue's histories may be split across pages; merge by `issueId`.
- Order isn't guaranteed: always sort by `(created, int(id))`.

## GET /rest/api/3/issue/{key}/changelog (documented; histories observed via expand)

`{"self", "maxResults": 100, "startAt": 0, "total": 10, "isLast": true, "values": [histories, oldest first]}`.
Changelog item conventions (**observed**):
- status: `from`/`to` hold status ids; `fromString`/`toString` hold names.
- Story point estimate: `from`/`to` are **null**; the value is in `fromString`/`toString` (`"0.5"`).
- Sprint: `from`/`to` hold sprint ids as strings (`"4427"`, or `"4400, 4427"` for several); `""` when none.
- assignee: `from`/`to` hold account ids; the strings hold display names.
- timeoriginalestimate / timeestimate / timespent: `from`/`to` hold seconds as strings.
- Some items have **no `fieldId`** (`WorklogId`, `RemoteWorkItemLink`).

## GET /rest/api/3/issue/{key}/worklog (documented)

`{"startAt": 0, "maxResults": 5000, "total": 1, "worklogs": [worklog]}`, where a worklog is
```json
{"self": "...", "author": {"accountId": "app-tempo", "accountType": "app"}, "updateAuthor": {},
 "created": "2026-11-02T23:09:51.525-0600", "updated": "2026-11-02T23:09:51.525-0600",
 "started": "2026-11-02T09:00:00.000-0600", "timeSpent": "4h", "timeSpentSeconds": 14400, "id": "67746", "issueId": "52507"}
```

## GET /rest/api/3/worklog/updated?since={ms} and /worklog/deleted?since={ms} (documented)

```json
{"values": [{"worklogId": 67746, "updatedTime": 1762146591525, "properties": []}],
 "since": 1762140000000, "until": 1762146591525, "self": "...", "nextPage": "https://.../worklog/updated?since=1762146591525", "lastPage": true}
```
At most 1000 per page. Page by calling again with `since` set to the response's `until` while
`lastPage` is false. `nextPage` is an absolute URL, which the client guard rejects. Worklogs updated in
the minute before the request are not returned (the sync's 5-minute overlap covers this).

## POST /rest/api/3/worklog/list (documented)

Request `{"ids": [67746, 67747]}` (at most 1000). The response is a **list** of worklogs (shape as above, each with `issueId`).

## GET /rest/api/3/issue/{key}/remotelink (documented; observed in changelog as RemoteWorkItemLink)

A **list**:
```json
[{"id": 79589, "self": "...", "globalId": "appId=...&pageId=...", "application": {"type": "com.atlassian.confluence", "name": "Confluence"},
  "relationship": "Wiki Page", "object": {"url": "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=1", "title": "Page", "icon": {}}}]
```

## GET /rest/agile/1.0/board/{id}/sprint (documented)

`{"maxResults": 50, "startAt": 0, "isLast": true, "values": [{"id": 4427, "state": "active", "name": "...", "startDate": "...", "endDate": "...", "originBoardId": 21, "goal": ""}]}`

## Errors and limits (documented)

- 429 has `Retry-After` (seconds). 5xx is retryable. 401/403 means bad credentials or no permission.
  Other 4xx carry `{"errorMessages": [...], "errors": {}}`.
