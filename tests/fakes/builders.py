from __future__ import annotations

from datetime import datetime, timedelta

from oversight.timeutil import UTC

from tests.fakes.fake_jira import SPRINTS, FakeJira

START = datetime(2026, 11, 4, 15, 0, 30, tzinfo=UTC)


AIL_PLAN: list[tuple[str, int | None, str]] = [
    ("Epic", None, "AI"),
    ("Epic", None, ""),
    ("Epic", None, "ID"),
    ("Task", 1, "APSIWDL"),
    ("Task", 1, "APSIWWD"),
    ("Task", 1, "APSIW"),
    ("Task", 1, "APS"),
    ("Task", 2, "AP"),
    ("Task", 2, "APSIWDLL"),
    ("Task", 3, "APSIWD"),
    ("Task", 3, "APSIWDRW"),
    ("Task", None, "D"),
    ("Task", None, "NK"),
    ("Task", None, "AX"),
    ("Task", None, "APSPI"),
    ("Task", None, ""),
    ("Task", None, "SS"),
    ("Task", None, "AIWL"),
    ("Bug", 3, "AIWD"),
    ("Bug", 3, "AID"),
    ("Bug", 3, "A"),
    ("Bug", None, "IWD"),
    ("Bug", None, ""),
    ("Bug", None, "PI"),
    ("Test", None, "ID"),
    ("Test", None, ""),
    ("Test", None, "I"),
    ("Test", 2, "A"),
    ("Subtask", 4, "IWD"),
    ("Subtask", 4, "IW"),
    ("Subtask", 5, "AIWD"),
    ("Subtask", 5, ""),
    ("Subtask", 6, "IWWD"),
    ("Subtask", 9, "D"),
]
AIL_LINKS = [("Blocks", 4, 5), ("Relates", 4, 5), ("Relates", 19, 10), ("Cloners", 25, 22)]
OPS_PLAN: list[tuple[str, int | None, str]] = [
    ("Task", None, "IW"),
    ("Bug", None, "AIWD"),
    ("Task", None, ""),
]

AIL_COUNTS = {
    "issues": 34,
    "status_changes": 36,
    "field_changes": 38,
    "worklogs": 16,
    "links": 10,
    "statuses": 3,
}
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
    fake.add_project(project)
    for issue_type, parent, steps in plan:
        fake.clock += timedelta(hours=3)
        issue = fake.create(project, issue_type, parent=None if parent is None else f"{project}-{parent}")
        for step in steps:
            _plan_step(fake, issue.key, step)
    for type_name, source, target in links or []:
        fake.link(type_name, f"{project}-{source}", f"{project}-{target}")


def ail_and_ops(fake: FakeJira | None = None, *, now: datetime = START) -> FakeJira:
    jira = fake if fake is not None else FakeJira()
    build(jira, "AIL", AIL_PLAN, AIL_LINKS)
    build(jira, "OPS", OPS_PLAN)
    if jira.clock > now:
        raise ValueError(f"the projects were built up to {jira.clock}, after now ({now})")
    jira.clock = now
    return jira
