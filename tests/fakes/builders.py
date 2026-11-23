"""Realistic fake projects for FakeJira, built the way Jira records work.

AIL_PLAN mirrors the real AIL project: every issue type, epics with children,
subtasks, reopened and skipped work, story points, sprints, Tempo worklogs,
remote links and issue links. AIL_COUNTS is what a sync of it must store.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from oversight.timeutil import UTC

from tests.fakes.fake_jira import SPRINTS, FakeJira

# The fake's clock at the first sync of every sync test.
START = datetime(2026, 11, 4, 15, 0, 30, tzinfo=UTC)


# --- the fake projects --------------------------------------------------------------------

# One row per issue, in key order: (type, parent's number, plan). Plan letters:
# A assign, P set story points, S add a sprint, I start (To Do -> In Progress),
# D finish (-> Done), R reopen (Done -> In Progress), W log 2 h, X log 1 h and
# delete it, L add a remote link, N edit the description, K re-rank.
# Tracked changelog rows: status = I + D + R, field = A + P + S.
AIL_PLAN: list[tuple[str, int | None, str]] = [
    ("Epic", None, "AI"),  # AIL-1
    ("Epic", None, ""),  # AIL-2
    ("Epic", None, "ID"),  # AIL-3
    ("Task", 1, "APSIWDL"),  # AIL-4
    ("Task", 1, "APSIWWD"),  # AIL-5
    ("Task", 1, "APSIW"),  # AIL-6
    ("Task", 1, "APS"),  # AIL-7
    ("Task", 2, "AP"),  # AIL-8
    ("Task", 2, "APSIWDLL"),  # AIL-9
    ("Task", 3, "APSIWD"),  # AIL-10
    ("Task", 3, "APSIWDRW"),  # AIL-11: reopened
    ("Task", None, "D"),  # AIL-12: To Do straight to Done
    ("Task", None, "NK"),  # AIL-13: untracked changes only
    ("Task", None, "AX"),  # AIL-14: time logged then removed, so timespent 0
    ("Task", None, "APSPI"),  # AIL-15: points changed twice
    ("Task", None, ""),  # AIL-16
    ("Task", None, "SS"),  # AIL-17: two sprints
    ("Task", None, "AIWL"),  # AIL-18
    ("Bug", 3, "AIWD"),  # AIL-19
    ("Bug", 3, "AID"),  # AIL-20
    ("Bug", 3, "A"),  # AIL-21
    ("Bug", None, "IWD"),  # AIL-22
    ("Bug", None, ""),  # AIL-23
    ("Bug", None, "PI"),  # AIL-24
    ("Test", None, "ID"),  # AIL-25
    ("Test", None, ""),  # AIL-26
    ("Test", None, "I"),  # AIL-27
    ("Test", 2, "A"),  # AIL-28
    ("Subtask", 4, "IWD"),  # AIL-29
    ("Subtask", 4, "IW"),  # AIL-30
    ("Subtask", 5, "AIWD"),  # AIL-31
    ("Subtask", 5, ""),  # AIL-32
    ("Subtask", 6, "IWWD"),  # AIL-33
    ("Subtask", 9, "D"),  # AIL-34
]
# AIL-4 blocks and relates to AIL-5 (one row each way, titles merged).
AIL_LINKS = [("Blocks", 4, 5), ("Relates", 4, 5), ("Relates", 19, 10), ("Cloners", 25, 22)]
OPS_PLAN: list[tuple[str, int | None, str]] = [
    ("Task", None, "IW"),  # OPS-1
    ("Bug", None, "AIWD"),  # OPS-2
    ("Task", None, ""),  # OPS-3
]

# Rows a sync of AIL must store, counted from AIL_PLAN and AIL_LINKS.
AIL_COUNTS = {
    "issues": 34,
    "status_changes": 36,
    "field_changes": 38,
    "worklogs": 16,
    "links": 10,  # 6 issue-link rows (3 linked pairs, both ways) + 4 remote links
    "statuses": 3,
}
# Issues with time logged, so a full sync asks for their worklogs.
AIL_WITH_TIME = {f"AIL-{n}" for n in (4, 5, 6, 9, 10, 11, 18, 19, 22, 29, 30, 31, 33)}


def _plan_step(fake: FakeJira, key: str, step: str) -> None:
    issue = fake.issue(key)
    if step == "A":
        fake.assign(key, "acc-dev-2" if issue.assignee else "acc-dev-1")
    elif step == "P":
        fake.set_points(key, 0.5 if issue.points else 2)
    elif step == "S":
        fake.set_sprints(key, issue.sprints + [next(s for s in SPRINTS if s not in issue.sprints)])
    elif step in "IR":
        fake.transition(key, "In Progress")
    elif step == "D":
        fake.transition(key, "Done")
    elif step == "W":
        fake.log_work(key)
    elif step == "X":
        fake.delete_worklog(fake.log_work(key, 3600))
    elif step == "L":
        fake.add_remote_link(key)
    elif step == "N":
        fake.edit_description(key)
    elif step == "K":
        fake.rerank(key)
    else:
        raise ValueError(step)


def build(
    fake: FakeJira,
    project: str,
    plan: list[tuple[str, int | None, str]],
    links: list[tuple[str, int, int]] | None = None,
) -> None:
    """Create the project's issues from `plan`, three fake hours apart, then link them."""
    fake.add_project(project)
    for issue_type, parent, steps in plan:
        fake.clock += timedelta(hours=3)
        issue = fake.create(project, issue_type, parent=None if parent is None else f"{project}-{parent}")
        for step in steps:
            _plan_step(fake, issue.key, step)
    for type_name, source, target in links or []:
        fake.link(type_name, f"{project}-{source}", f"{project}-{target}")


def ail_and_ops(fake: FakeJira | None = None, *, now: datetime = START) -> FakeJira:
    """AIL (34 issues, every type) and OPS (3 issues) on one site, the clock then set to `now`.

    The projects are built from the fake's clock as it is (BUILT_FROM for a new
    fake); `now` must come after the last change.
    """
    jira = fake if fake is not None else FakeJira()
    build(jira, "AIL", AIL_PLAN, AIL_LINKS)
    build(jira, "OPS", OPS_PLAN)
    if jira.clock > now:
        raise ValueError(f"the projects were built up to {jira.clock}, after now ({now})")
    jira.clock = now
    return jira
