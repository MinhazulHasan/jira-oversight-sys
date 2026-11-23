"""The daily digest (Phase 5): "notify us when things were not as they should be", without flooding anyone.

build_digest(store, process, rules, *, project, now, jira_base_url=None) -> DigestContent
- since = the sent_at of the project's last digest row (any channel), else None
  (first digest: everything open counts as new, capped at the 25 most recent
  per section with "and N more").
- new_breaches / new_warnings: open findings with first_seen_at > since (or
  reopened since), excluding finding ids that an earlier digest already carried
  (digests.finding_ids_json), so the same finding never appears as new twice.
  The findings table keeps no trace of a reopen (the engine clears resolved_at),
  so "new" is implemented as "open and never carried by a digest": that covers
  both cases and makes the guarantee hold by construction. A digest carries
  every finding it counted, including those beyond the cap (listed only as
  "and N more"): Planning.md's "only send what changed since the last digest",
  so old findings never come back as new and an unchanged day stays one line.
  A finding is the condition it names (its rule, issue and evidence key), so
  one whose condition a digest carried under another id is not new either: a
  severity changed in the board's rule settings (JO-15), changed back, or a
  catalogue version bump raises the same conditions again under new ids, and
  none of them is a new problem.
- resolved: findings with resolved_at > since and resolved_why "cleared"
  (superseded findings are a catalogue version bump or a severity change,
  aged_out ones left the look-back, and settings_changed ones closed because an
  admin changed the board's rule settings, JO-15: none of them is a fix). The
  first digest has no window, so it lists nothing as resolved.
- hot_spots: the top 5 epics by open findings (epic_key, epic_title from the
  epic issue's summary, open_breaches, open_warnings); issues with no epic
  group under "(no epic)", listed after the epics and outside the top 5: a hot
  spot is an epic (as in Ask's oversight_hot_spots and the weekly insights).
  Sprint-scoped findings are not issues and stay out.
- not_measurable: rules recorded as not measurable in the latest rules run,
  with the missing sources (a standing section, one line each).
- stale: True when the last successful sync is older than 2 hours, or when no
  sync is recorded at all (the digest says so at the top).
- unchanged: True when there is nothing new and nothing resolved.
Each item: finding_id, rule_id, rule title, issue_key, issue summary (plain,
untrusted text shown as-is but never interpreted), message, first_seen_at, and
url = f"{jira_base_url}/browse/{issue_key}" when a base URL is known. The rule
title is the board's (`rules` is the catalogue; the project's rule settings and
custom rules, JO-20, are applied to it), so a custom rule's findings, and a
deleted one's, carry its title.

render_digest(content, *, tz_name) -> Message
- Subject: "Jira oversight — <PROJECT> — <4 Nov 2026>" (dates in tz_name).
- Plain text first (f-strings, no template engine): a one-line summary, then
  sections "New breaches", "New warnings", "Resolved since <date>", "Hot spots",
  "Not measurable", each with counts; an item is "<KEY> · <rule title> · <message>"
  plus the issue summary and the URL on the next lines. Unchanged: one line,
  "Nothing changed since <date>." (after the stale warning, when the data is
  stale: a quiet day and a broken sync must never look the same). Team and
  process level: no person names, no account ids.
- html: the same content as simple semantic HTML (no CSS framework; links on keys).

run_digest(store, process, rules, settings, *, projects, now=utcnow, channel=None,
           channel_factory=None, tz_name=None, extra=None) -> list[DigestResult]
- One run record (kind "digest"). Per project: build, render, send through
  `channel` (tests inject one), `channel_factory(settings, project=, date=)` or
  get_channel(settings, project, date); insert a digests row (sent_at = now,
  since, subject, body, finding_ids_json = new + resolved ids (beyond the cap
  too), delivery_ref,
  run_id). An unchanged digest is still sent (one line), so a silent day is
  distinguishable from a broken scheduler. tz_name falls back to
  REPORT_TIMEZONE, then UTC; never the Jira account's zone.
- `extra` appends standing sections to every digest (heading -> text, or a
  list of lines), e.g. the weekly insights delivered with the Monday digest.
- A ChannelError marks the run failed for that project, records nothing in
  digests, and re-raises after the loop finishes other projects (so one bad
  channel never loses the others' digests).

Done when (Planning.md Phase 5): the same finding never appears as "new" twice
(test); Tazbir has reviewed one real digest (process, not code).
"""
from __future__ import annotations

import html
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from oversight.config import ProcessConfig
from oversight.errors import ConfigError
from oversight.logs import get_logger
from oversight.notify.channels import Channel, ChannelError, Message, get_channel
from oversight.rules.catalog import RulesConfig
from oversight.rules.settings import effective_rules, load_board_settings
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import STALE_AFTER, parse_ts, to_iso, utcnow, zone

log = get_logger("digest")

# The digest says so at the top when the last successful sync is older than this.
HOT_SPOT_LIMIT = 5
PER_SECTION_CAP = 25
# Only a real issue key has a browse page; sprint-scoped findings use "sprint:<id>".
_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
# Fixed names: strftime's %b follows the locale, and the digest must read the same everywhere.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_SECTIONS: tuple[tuple[str, str], ...] = (
    ("new_breaches", "New breaches"),
    ("new_warnings", "New warnings"),
    ("resolved", "Resolved"),
)

_OPEN_FINDINGS_SQL = """
SELECT f.finding_id, f.rule_id, f.severity, f.issue_key, f.evidence_key, f.message, f.first_seen_at, f.resolved_at,
       i.summary AS issue_summary
FROM findings f
LEFT JOIN issues i ON i.issue_key = f.issue_key
WHERE f.project_key = ? AND f.resolved_at IS NULL
ORDER BY f.first_seen_at DESC, f.finding_id
"""

# What each of the project's findings names, whatever its id: its rule, issue and evidence key.
_CONDITIONS_SQL = "SELECT finding_id, rule_id, issue_key, evidence_key FROM findings WHERE project_key = ?"

_RESOLVED_FINDINGS_SQL = """
SELECT f.finding_id, f.rule_id, f.severity, f.issue_key, f.message, f.first_seen_at, f.resolved_at,
       i.summary AS issue_summary
FROM findings f
LEFT JOIN issues i ON i.issue_key = f.issue_key
WHERE f.project_key = ? AND f.resolved_at IS NOT NULL AND f.resolved_why = 'cleared' AND f.resolved_at > ?
ORDER BY f.resolved_at DESC, f.finding_id
"""

# Open findings per epic of the issues they are about. e.summary is constant
# within a group (one epic row per key), so the bare column is well defined.
_HOT_SPOTS_SQL = """
SELECT COALESCE(i.epic_key, p.epic_key) AS epic_key, e.summary AS epic_title,
       SUM(CASE WHEN f.severity = 'breach' THEN 1 ELSE 0 END) AS open_breaches,
       SUM(CASE WHEN f.severity <> 'breach' THEN 1 ELSE 0 END) AS open_warnings
FROM findings f
JOIN issues i ON i.issue_key = f.issue_key
-- A subtask's parent is a task: its epic is the parent's.
LEFT JOIN issues p ON p.issue_key = i.parent_key
LEFT JOIN issues e ON e.issue_key = COALESCE(i.epic_key, p.epic_key)
WHERE f.project_key = ? AND f.resolved_at IS NULL
GROUP BY COALESCE(i.epic_key, p.epic_key)
ORDER BY open_breaches + open_warnings DESC, open_breaches DESC, COALESCE(i.epic_key, p.epic_key)
"""

_LATEST_RULES_RUN_SQL = """
SELECT rs.run_id
FROM rule_status rs
JOIN runs r ON r.run_id = rs.run_id
WHERE rs.project_key = ? AND r.status = 'succeeded'
ORDER BY r.started_at DESC
LIMIT 1
"""


@dataclass
class DigestItem:
    finding_id: str
    rule_id: str
    rule_title: str
    severity: str
    issue_key: str
    issue_summary: str | None
    message: str | None
    first_seen_at: str | None
    resolved_at: str | None = None
    url: str | None = None


@dataclass
class HotSpot:
    epic_key: str | None
    epic_title: str | None
    open_breaches: int
    open_warnings: int


@dataclass
class DigestContent:
    project: str
    since: str | None
    until: str
    new_breaches: list[DigestItem] = field(default_factory=list)
    new_warnings: list[DigestItem] = field(default_factory=list)
    resolved: list[DigestItem] = field(default_factory=list)
    hot_spots: list[HotSpot] = field(default_factory=list)
    # (rule_id, rule title, missing sources)
    not_measurable: list[tuple[str, str, list[str]]] = field(default_factory=list)
    last_synced_at: str | None = None
    stale: bool = False
    # Items beyond the per-section cap, by section name.
    truncated: dict[str, int] = field(default_factory=dict)
    # Their finding ids: counted as "and N more", so carried like the items shown.
    beyond_cap: list[str] = field(default_factory=list)

    @property
    def unchanged(self) -> bool:
        return not (self.new_breaches or self.new_warnings or self.resolved)

    @property
    def finding_ids(self) -> list[str]:
        """Every finding this digest reported: the items shown, then those counted beyond the cap."""
        return [i.finding_id for i in self.new_breaches + self.new_warnings + self.resolved] + self.beyond_cap


def _total(content: DigestContent, section: str) -> int:
    """Items in a section including those beyond the cap."""
    return len(getattr(content, section)) + content.truncated.get(section, 0)


@dataclass
class DigestResult:
    project: str
    digest_id: str
    channel: str
    delivery_ref: str
    content: DigestContent
    message: Message

    def render(self) -> str:
        """Console summary: project, channel, delivery ref, counts (or 'nothing changed')."""
        content = self.content
        if content.unchanged:
            counts = "nothing changed"
        else:
            counts = ", ".join(
                (
                    _plural(_total(content, "new_breaches"), "new breach", "new breaches"),
                    _plural(_total(content, "new_warnings"), "new warning", "new warnings"),
                    f"{_total(content, 'resolved')} resolved",
                )
            )
        line = f"{self.project}: digest sent via {self.channel} -> {self.delivery_ref} ({counts})"
        if content.stale:
            line += "\n  warning: the data may be stale (see the top of the digest)"
        return line


# --- building -------------------------------------------------------------------


def build_digest(
    store: Store,
    process: ProcessConfig,
    rules: RulesConfig,
    *,
    project: str,
    now: datetime,
    jira_base_url: str | None = None,
    per_section_cap: int = PER_SECTION_CAP,
) -> DigestContent:
    """Everything the digest for `project` says, as of `now`. See the module docstring."""
    process.project(project)  # an unknown key is a config error, not an empty digest
    if per_section_cap < 1:
        raise ValueError("per_section_cap must be at least 1")
    until = to_iso(now)
    if until is None:
        raise ValueError("build_digest needs an aware datetime for now")
    # The board's own rules (rules.yaml with its rule settings and its custom rules, JO-20), so a custom rule's findings
    # carry its title, a deleted one's too.
    rules = rules if rules.is_board_rules else effective_rules(rules, load_board_settings(store.conn, project))
    titles = rules.title_of

    last = store.query_one("SELECT sent_at FROM digests WHERE project_key = ? ORDER BY sent_at DESC LIMIT 1", (project,))
    since = last["sent_at"] if last else None
    content = DigestContent(project=project, since=since, until=until)

    carried, shown = _carried(store, project)
    fresh = [
        _item(row, titles, jira_base_url)
        for row in store.query(_OPEN_FINDINGS_SQL, (project,))
        if row["finding_id"] not in carried and _condition(row) not in shown
    ]
    content.new_breaches = _keep(content, "new_breaches", [i for i in fresh if i.severity == "breach"], per_section_cap)
    content.new_warnings = _keep(content, "new_warnings", [i for i in fresh if i.severity != "breach"], per_section_cap)
    if since is not None:
        resolved = [_item(row, titles, jira_base_url) for row in store.query(_RESOLVED_FINDINGS_SQL, (project, since))]
        content.resolved = _keep(content, "resolved", resolved, per_section_cap)

    spots = [
        HotSpot(
            epic_key=row["epic_key"],
            epic_title=row["epic_title"],
            open_breaches=int(row["open_breaches"]),
            open_warnings=int(row["open_warnings"]),
        )
        for row in store.query(_HOT_SPOTS_SQL, (project,))
    ]
    # The top epics, then work outside any epic as context: it is never one of the five.
    content.hot_spots = [s for s in spots if s.epic_key is not None][:HOT_SPOT_LIMIT] + [
        s for s in spots if s.epic_key is None
    ]
    content.not_measurable = _not_measurable(store, rules, project, titles)
    content.last_synced_at, content.stale = _sync_freshness(store, project, now)
    return content


def _carried(store: Store, project: str) -> tuple[set[str], set[tuple[str, str, str]]]:
    """Every finding id an earlier digest of this project already showed, and the conditions they name (_condition):
    the same condition raised again under another id is not new either."""
    carried: set[str] = set()
    for row in store.query("SELECT finding_ids_json FROM digests WHERE project_key = ?", (project,)):
        carried.update(_json_list(row["finding_ids_json"]))
    shown = {_condition(row) for row in store.query(_CONDITIONS_SQL, (project,)) if row["finding_id"] in carried}
    return carried, shown


def _condition(row: Any) -> tuple[str, str, str]:
    """What a finding names, whatever its id: its rule, its issue (or sprint) and its evidence key."""
    return row["rule_id"], row["issue_key"], row["evidence_key"]


def _json_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return [str(v) for v in data] if isinstance(data, list) else []


def _item(row: Any, titles: Callable[[str], str], base_url: str | None) -> DigestItem:
    rule_id = row["rule_id"]
    return DigestItem(
        finding_id=row["finding_id"],
        rule_id=rule_id,
        rule_title=titles(rule_id),
        severity=row["severity"],
        issue_key=row["issue_key"],
        issue_summary=row["issue_summary"],
        message=row["message"],
        first_seen_at=row["first_seen_at"],
        resolved_at=row["resolved_at"],
        url=_browse_url(base_url, row["issue_key"]),
    )


def _browse_url(base_url: str | None, issue_key: str) -> str | None:
    base = (base_url or "").strip().rstrip("/")
    if not base or not _ISSUE_KEY.match(issue_key):
        return None
    return f"{base}/browse/{issue_key}"


def _keep(content: DigestContent, section: str, items: list[DigestItem], cap: int) -> list[DigestItem]:
    """The first `cap` items to show; the rest are counted ("and N more") and carried by id."""
    rest = items[cap:]
    if rest:
        content.truncated[section] = len(rest)
        content.beyond_cap.extend(item.finding_id for item in rest)
    return items[:cap]


def _not_measurable(
    store: Store, rules: RulesConfig, project: str, titles: Callable[[str], str]
) -> list[tuple[str, str, list[str]]]:
    """Rules the latest successful rules run could not measure, in catalogue order."""
    run = store.query_one(_LATEST_RULES_RUN_SQL, (project,))
    if run is None:
        return []
    rows = store.query(
        "SELECT rule_id, missing_json FROM rule_status WHERE run_id = ? AND project_key = ? AND measurable = 0",
        (run["run_id"], project),
    )
    position = {rule.id: n for n, rule in enumerate(rules.rules)}
    out = [(row["rule_id"], titles(row["rule_id"]), _json_list(row["missing_json"])) for row in rows]
    out.sort(key=lambda entry: (position.get(entry[0], len(position)), entry[0]))
    return out


def _sync_freshness(store: Store, project: str, now: datetime) -> tuple[str | None, bool]:
    row = store.query_one("SELECT last_synced_at FROM sync_state WHERE scope = ?", (project,))
    last = row["last_synced_at"] if row else None
    synced = parse_ts(last)
    if synced is None:
        return None, True
    return last, now - synced > STALE_AFTER


# --- rendering ------------------------------------------------------------------


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _parse(iso: str) -> datetime:
    moment = parse_ts(iso)
    if moment is None:
        raise ValueError("the digest needs a timestamp here")
    return moment


def _day(iso: str, tz: tzinfo) -> str:
    """A storage timestamp as a date in the zone: "4 Nov 2026"."""
    local = _parse(iso).astimezone(tz)
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year}"


def _moment(iso: str, tz: tzinfo) -> str:
    """A storage timestamp as date and time in the zone: "4 Nov 2026 03:12"."""
    local = _parse(iso).astimezone(tz)
    return f"{_day(iso, tz)} {local:%H:%M}"


def _stale_line(content: DigestContent, tz: tzinfo, tz_name: str) -> str:
    if content.last_synced_at is None:
        return f"Data may be stale: no successful sync is recorded for {content.project}."
    hours = (_parse(content.until) - _parse(content.last_synced_at)).total_seconds() / 3600.0
    return (
        f"Data may be stale: the last sync was at {_moment(content.last_synced_at, tz)} {tz_name}, "
        f"{hours:.1f} h before this digest."
    )


def _nothing_changed(content: DigestContent, tz: tzinfo) -> str:
    if content.since is None:
        return "Nothing to report: no open findings."
    return f"Nothing changed since {_day(content.since, tz)}."


def _summary_line(content: DigestContent, tz: tzinfo) -> str:
    breaches = _total(content, "new_breaches")
    warnings = _total(content, "new_warnings")
    if content.since is None:
        return (
            f"First digest: {_plural(breaches, 'open breach', 'open breaches')}, "
            f"{_plural(warnings, 'open warning', 'open warnings')} (all count as new)."
        )
    return (
        f"Since {_day(content.since, tz)}: {_plural(breaches, 'new breach', 'new breaches')}, "
        f"{_plural(warnings, 'new warning', 'new warnings')}, {_total(content, 'resolved')} resolved."
    )


def _section_heading(content: DigestContent, section: str, label: str, tz: tzinfo) -> str:
    if section == "resolved" and content.since is not None:
        label = f"Resolved since {_day(content.since, tz)}"
    return f"{label} ({_total(content, section)})"


def _item_line(item: DigestItem) -> str:
    parts = [item.issue_key, item.rule_title]
    if item.message:
        parts.append(item.message)
    return " · ".join(parts)


def _hot_spot_label(spot: HotSpot) -> str:
    if spot.epic_key is None:
        return "(no epic)"
    return f"{spot.epic_key} · {spot.epic_title}" if spot.epic_title else spot.epic_key


def _hot_spot_counts(spot: HotSpot) -> str:
    return f"{_plural(spot.open_breaches, 'breach', 'breaches')}, {_plural(spot.open_warnings, 'warning', 'warnings')}"


def _not_measurable_line(rule_id: str, title: str, missing: list[str]) -> str:
    line = f"{rule_id} · {title}"
    if missing:
        line += f" · not recorded in Jira: {', '.join(missing)}"
    return line


def _text(content: DigestContent, tz: tzinfo, tz_name: str) -> str:
    lines: list[str] = []
    if content.stale:
        lines.append(_stale_line(content, tz, tz_name))
    if content.unchanged:
        lines.append(_nothing_changed(content, tz))
        return "\n".join(lines)
    lines.append(_summary_line(content, tz))
    for section, label in _SECTIONS:
        items: list[DigestItem] = getattr(content, section)
        lines += ["", _section_heading(content, section, label, tz)]
        for item in items:
            lines.append(_item_line(item))
            if item.issue_summary:
                lines.append(f"  {item.issue_summary}")
            if item.url:
                lines.append(f"  {item.url}")
        if not items:
            lines.append("  none")
        elif content.truncated.get(section):
            lines.append(f"  and {content.truncated[section]} more")
    lines += ["", f"Hot spots (open findings by epic, top {HOT_SPOT_LIMIT})"]
    lines += [f"{_hot_spot_label(spot)} · {_hot_spot_counts(spot)}" for spot in content.hot_spots] or ["  none"]
    lines += ["", f"Not measurable ({_plural(len(content.not_measurable), 'rule', 'rules')})"]
    lines += [_not_measurable_line(*entry) for entry in content.not_measurable] or ["  none"]
    return "\n".join(lines)


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _html(content: DigestContent, subject: str, tz: tzinfo, tz_name: str) -> str:
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        f'<head><meta charset="utf-8"><title>{_esc(subject)}</title></head>',
        "<body>",
        f"<h1>{_esc(subject)}</h1>",
    ]
    if content.stale:
        parts.append(f"<p><strong>{_esc(_stale_line(content, tz, tz_name))}</strong></p>")
    if content.unchanged:
        parts.append(f"<p>{_esc(_nothing_changed(content, tz))}</p>")
    else:
        parts.append(f"<p>{_esc(_summary_line(content, tz))}</p>")
        for section, label in _SECTIONS:
            items: list[DigestItem] = getattr(content, section)
            parts.append(f"<h2>{_esc(_section_heading(content, section, label, tz))}</h2>")
            if items:
                parts.append("<ul>")
                parts += [_html_item(item) for item in items]
                parts.append("</ul>")
                if content.truncated.get(section):
                    parts.append(f"<p>and {content.truncated[section]} more</p>")
            else:
                parts.append("<p>none</p>")
        parts.append(f"<h2>Hot spots (open findings by epic, top {HOT_SPOT_LIMIT})</h2>")
        if content.hot_spots:
            parts.append("<ul>")
            parts += [f"<li>{_esc(_hot_spot_label(spot))} · {_esc(_hot_spot_counts(spot))}</li>" for spot in content.hot_spots]
            parts.append("</ul>")
        else:
            parts.append("<p>none</p>")
        parts.append(f"<h2>Not measurable ({_plural(len(content.not_measurable), 'rule', 'rules')})</h2>")
        if content.not_measurable:
            parts.append("<ul>")
            parts += [f"<li>{_esc(_not_measurable_line(*entry))}</li>" for entry in content.not_measurable]
            parts.append("</ul>")
        else:
            parts.append("<p>none</p>")
    parts += ["</body>", "</html>"]
    return "\n".join(parts)


def _html_item(item: DigestItem) -> str:
    key = f'<a href="{_esc(item.url)}">{_esc(item.issue_key)}</a>' if item.url else _esc(item.issue_key)
    line = f"{key} · {_esc(item.rule_title)}"
    if item.message:
        line += f" · {_esc(item.message)}"
    if item.issue_summary:
        line += f"<br><em>{_esc(item.issue_summary)}</em>"
    return f"<li>{line}</li>"


def render_digest(content: DigestContent, *, tz_name: str) -> Message:
    """The subject, plain text, HTML and ticket links for one digest. See the module docstring."""
    tz = zone(tz_name)
    subject = f"Jira oversight — {content.project} — {_day(content.until, tz)}"
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in content.new_breaches + content.new_warnings + content.resolved:
        if item.url and item.url not in seen:
            seen.add(item.url)
            links.append((item.issue_key, item.url))
    return Message(
        subject=subject,
        text=_text(content, tz, tz_name),
        html=_html(content, subject, tz, tz_name),
        links=links,
    )


def _append_extra(message: Message, extra: dict[str, Any]) -> None:
    """Standing sections after the digest proper, in the text and the HTML."""
    for heading, value in extra.items():
        lines = [str(line) for line in value] if isinstance(value, (list, tuple)) else [str(value)]
        message.text += f"\n\n{heading}\n" + "\n".join(lines)
        if message.html is not None:
            section = f"<h2>{_esc(str(heading))}</h2>\n<p>{'<br>'.join(_esc(line) for line in lines)}</p>\n"
            head, marker, tail = message.html.rpartition("</body>")
            message.html = f"{head}{section}{marker}{tail}" if marker else message.html + section


# --- running --------------------------------------------------------------------


def run_digest(
    store: Store,
    process: ProcessConfig,
    rules: RulesConfig,
    settings: Settings,
    *,
    projects: list[str],
    now: Callable[[], datetime] = utcnow,
    channel: Channel | None = None,
    channel_factory: Callable[..., Channel] | None = None,
    tz_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> list[DigestResult]:
    """Build, render, send and record one digest per project. See the module docstring."""
    if not projects:
        raise ConfigError("no projects to digest: set JIRA_PROJECTS or pass --project")
    zone_name = tz_name or settings.report_zone
    tz = zone(zone_name)  # an unknown zone fails here, before any run record
    moment = now()
    sent_at = to_iso(moment)
    date = moment.astimezone(tz).date().isoformat()
    make_channel = channel_factory or get_channel

    run = store.start_run("digest", started_at=moment)
    results: list[DigestResult] = []
    failures: list[tuple[str, ChannelError]] = []
    stats: dict[str, dict[str, Any]] = {}
    try:
        for project in projects:
            target = channel or make_channel(settings, project=project, date=date)
            # A synthetic project's issues don't exist on the Jira site: no links to it.
            base_url = None if process.project(project).synthetic else (settings.jira_base_url or None)
            content = build_digest(store, process, rules, project=project, now=moment, jira_base_url=base_url)
            message = render_digest(content, tz_name=zone_name)
            if extra:
                _append_extra(message, extra)
            try:
                delivery_ref = target.send(message)
            except ChannelError as exc:
                log.error("digest_failed", project=project, channel=target.name, error=str(exc), retryable=exc.retryable)
                failures.append((project, exc))
                stats[project] = {"status": "failed", "channel": target.name, "error": str(exc)}
                continue
            digest_id = uuid.uuid4().hex
            store.upsert(
                "digests",
                {
                    "digest_id": digest_id,
                    "project_key": project,
                    "channel": target.name,
                    "sent_at": sent_at,
                    "since": content.since,
                    "subject": message.subject,
                    "body": message.text,
                    "finding_ids_json": content.finding_ids,
                    "delivery_ref": delivery_ref,
                    "run_id": run.run_id,
                },
            )
            stats[project] = {
                "status": "sent",
                "channel": target.name,
                "delivery_ref": delivery_ref,
                "digest_id": digest_id,
                "new_breaches": _total(content, "new_breaches"),
                "new_warnings": _total(content, "new_warnings"),
                "resolved": _total(content, "resolved"),
                "unchanged": content.unchanged,
                "stale": content.stale,
            }
            log.info("digest_sent", project=project, channel=target.name, digest_id=digest_id, **_counts(content))
            results.append(
                DigestResult(
                    project=project,
                    digest_id=digest_id,
                    channel=target.name,
                    delivery_ref=delivery_ref,
                    content=content,
                    message=message,
                )
            )
    except Exception:
        # Anything but a channel failure is a bug or a config problem: stop at once.
        store.finish_run(run, status="failed", stats={"projects": stats}, finished_at=now())
        raise
    store.finish_run(run, status="failed" if failures else "succeeded", stats={"projects": stats}, finished_at=now())
    if failures:
        # The other projects' digests went out and are recorded; now report the first failure.
        project, error = failures[0]
        raise ChannelError(f"{project}: {error}", retryable=error.retryable) from error
    return results


def _counts(content: DigestContent) -> dict[str, Any]:
    return {
        "new_breaches": _total(content, "new_breaches"),
        "new_warnings": _total(content, "new_warnings"),
        "resolved": _total(content, "resolved"),
        "unchanged": content.unchanged,
        "stale": content.stale,
    }
