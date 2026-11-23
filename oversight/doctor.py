"""`python -m oversight doctor` (Phase 0): access, fields and config, read-only.

CONTRACT (ticket 1). Done when: doctor passes and makes
zero write calls; every status in each in-scope project maps to a stage;
missing evidence sources are listed by name (they become "not measurable").

run_doctor(client, store, projects=..., process_path=..., fields_path=..., site=...)
Checks, in this order (a failing check doesn't stop the later ones unless
they can't run):
1. "jira auth": client.myself(); detail gives the account zone (timeZone).
   On JiraAuthError, fail it and skip every other Jira check (report them
   as failed: "skipped: auth failed").
2. "process config": ProcessConfig.load(process_path). An in-scope project it
   does not configure is a warning, not a failure (JO-17): the service maps it
   from Jira's status categories (oversight/boards.py).
3. Per project "project <KEY> visible": client.project(key) (a 404 JiraError
   means not visible); detail says team-managed (simplified/next-gen) or
   company-managed.
4. Per project "statuses <KEY> mapped": client.project_statuses(key) gives the
   status names; every one must map to a stage (case-insensitive). Unmapped
   names fail the check and are listed. A project process.yaml does not
   configure is a warning that shows the mapping its status categories give
   (To Do to backlog, In Progress to development, Done to done), listing any
   status in no category as unmapped.
5. "fields": client.fields(), resolved by config.FIELD_CANDIDATES (exact name,
   case-insensitive; candidates in preference order). When several fields
   match a logical name (one name repeated, or several candidate names
   present), prefer the one populated in the in-scope projects: probe with
   client.search_jql(f'project in ({keys}) AND cf[<n>] is not EMPTY', ["id"],
   page_size=1) and take the first non-empty result; ties go to preference
   order. `story_points` is required when any in-scope project's estimate
   basis is "points"; everything else is optional (missing gives a warning).
   Build a FieldsConfig (generated_at = to_iso(now), site) and, if
   write_fields, dump it to fields_path. Not when a required field is
   missing: sync trusts fields.yaml, so the last good file stays.
6. "evidence": per project, process.missing_evidence(project). A warning, not a
   failure: these become "not measurable". Also report which candidate
   evidence fields (programmer, code_reviewers, testers, test_reviewers) exist
   and whether any in-scope issue has them set (same one-result probe).
7. "database": store.query_one("SELECT 1") and the schema version.
Only allowlisted read calls; tests check that every recorded request is in ALLOWED.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oversight.boards import category_stages
from oversight.config import FIELD_CANDIDATES, FieldRef, FieldsConfig, ProcessConfig
from oversight.errors import ConfigError, JiraAuthError, JiraError, JiraUnavailable
from oversight.logs import get_logger
from oversight.settings import validate_project_keys
from oversight.store.db import SCHEMA_VERSION, Store
from oversight.timeutil import to_iso, utcnow, zone

if TYPE_CHECKING:
    from oversight.jira.client import JiraClient

# Candidate fields for James's later stages (logical names in FIELD_CANDIDATES).
_EVIDENCE_FIELDS: tuple[str, ...] = ("programmer", "code_reviewers", "testers", "test_reviewers")

# Only custom fields can be probed in JQL, as cf[<number>].
_CUSTOM_FIELD_ID = re.compile(r"customfield_([0-9]+)")

# Jira's text is data: a name or an error message gets at most this much of a line.
_NAME_LIMIT = 80
_ERROR_LIMIT = 500


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    # A warning is reported but doesn't fail the doctor.
    warning: bool = False
    # A setting reported as it is ("[INFO]", `doctor --production`): it passes, and is neither good nor bad.
    info: bool = False


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    fields: FieldsConfig | None = None
    unmapped_statuses: dict[str, list[str]] = field(default_factory=dict)
    missing_evidence: dict[str, list[str]] = field(default_factory=dict)
    account_timezone: str | None = None
    fields_written_to: Path | None = None

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if not check.warning)

    def render(self) -> str:
        """One line per check ("[ OK ]", "[WARN]", "[FAIL]" + name + detail), then a verdict line."""
        lines = []
        for check in self.checks:
            # Details built here are one line already; this also holds for hand-made checks.
            line = f"{_tag(check)} {_printable(check.name)}"
            detail = _printable(check.detail).strip()
            lines.append(f"{line}: {detail}" if detail else line)
        lines.append(_verdict(self.checks))
        return "\n".join(lines)


def run_doctor(
    client: JiraClient,
    store: Store,
    *,
    projects: list[str],
    process_path: Path,
    fields_path: Path,
    site: str,
    write_fields: bool = True,
    now: Callable[[], datetime] = utcnow,
) -> DoctorReport:
    """Run every check and return the report. See the module docstring."""
    doctor = _Doctor(client, store, _project_keys(projects), Path(process_path), now)
    doctor.check_auth()
    doctor.check_process()
    for key in doctor.keys:
        visible = doctor.check_project(key)
        doctor.check_statuses(key, visible)
    doctor.check_fields(Path(fields_path), site, write_fields)
    doctor.check_evidence()
    doctor.check_database()
    return doctor.finish()


class _Doctor:
    """The checks of one run, in order; later checks use what earlier ones found."""

    def __init__(
        self,
        client: JiraClient,
        store: Store,
        keys: list[str],
        process_path: Path,
        now: Callable[[], datetime],
    ) -> None:
        self.client = client
        self.store = store
        self.keys = keys
        self.process_path = process_path
        self.now = now
        self.report = DoctorReport()
        # Fetched per run: a logger bound at import would miss configure_logging().
        self.log = get_logger("doctor")
        self.process: ProcessConfig | None = None
        # Why the remaining Jira checks are skipped, once /myself has failed.
        self.jira_skipped: str | None = None
        # Projects this account can see: the only keys safe to put in a probe,
        # since JQL naming an unknown project fails the whole query.
        self.visible: list[str] = []
        # Field id -> whether an in-scope issue has it set; each field is probed once.
        self.populated: dict[str, bool] = {}

    # --- results ------------------------------------------------------------------

    def passed(self, name: str, detail: str) -> None:
        self._add(Check(name, True, detail))

    def warned(self, name: str, detail: str) -> None:
        self._add(Check(name, False, detail, warning=True))

    def failed(self, name: str, detail: str) -> None:
        self._add(Check(name, False, detail))

    def _add(self, check: Check) -> None:
        self.report.checks.append(check)
        log = self.log.info if check.ok else self.log.warning
        log("doctor_check", check=check.name, result=_result(check), detail=check.detail)

    def finish(self) -> DoctorReport:
        checks = self.report.checks
        self.log.info(
            "doctor_finished",
            ok=self.report.ok,
            failed=[check.name for check in checks if not check.ok and not check.warning],
            warnings=[check.name for check in checks if not check.ok and check.warning],
            jira_requests=getattr(self.client, "request_count", None),
        )
        return self.report

    # --- 1. jira auth -----------------------------------------------------------------

    def check_auth(self) -> None:
        name = "jira auth"
        try:
            me = self.client.myself()
        except JiraAuthError as exc:
            self.jira_skipped = "auth failed"
            self.failed(name, f"{_error(exc)} (check JIRA_EMAIL, JIRA_API_TOKEN and the account's access)")
            return
        except JiraError as exc:
            # The contract names only auth errors. Any other failure here (Jira down,
            # a wrong site, a proxy's login page) would repeat on every later call,
            # each after its own retries, so those calls are skipped too.
            self.jira_skipped = "Jira unavailable" if isinstance(exc, JiraUnavailable) else "jira auth failed"
            self.failed(name, _error(exc))
            return
        tz_name = me.get("timeZone")
        if not isinstance(tz_name, str) or not tz_name.strip():
            # Incremental sync writes JQL dates in this zone; without it they would be read wrongly.
            self.warned(name, "signed in, but Jira reported no account time zone (JQL dates are read in it)")
            return
        tz_name = tz_name.strip()
        self.report.account_timezone = tz_name
        try:
            zone(tz_name)
        except ConfigError:
            self.warned(
                name, f"signed in, but the account time zone {_quote(tz_name)} is not a known IANA zone"
            )
            return
        self.passed(name, f"signed in; account time zone {_clean(tz_name)}")

    # --- 2. process config ---------------------------------------------------------------

    def check_process(self) -> None:
        name = "process config"
        path = self.process_path
        try:
            process = ProcessConfig.load(path)
        except ConfigError as exc:
            self.failed(name, _error(exc))
            return
        except (OSError, UnicodeDecodeError) as exc:
            # ProcessConfig.load only translates a missing file; a directory, a
            # locked file or a UTF-16 save from Notepad lands here.
            self.failed(name, f"cannot read {path}: {_error(exc)}")
            return
        self.process = process
        summary = f"{path.name} version {_clean(process.process_version)}"
        missing = [key for key in self.keys if key not in process.projects]
        if missing:
            # Not a failure (JO-17): the service maps them from Jira's status categories.
            self.warned(name, f"{summary}; not configured for {', '.join(missing)}: mapped from Jira's status "
                              "categories, with no evidence sources (add them under projects to map them yourself)")
        else:
            self.passed(name, f"{summary}; configured for {', '.join(self.keys)}")

    # --- 3. project visible ------------------------------------------------------------------

    def check_project(self, key: str) -> bool:
        """Whether the account can see the project; the statuses check needs it."""
        name = f"project {key} visible"
        if self.jira_skipped:
            self.failed(name, f"skipped: {self.jira_skipped}")
            return False
        try:
            project = self.client.project(key)
        except JiraError as exc:
            if exc.status_code == 404:
                self.failed(name, "not visible: the project does not exist or this account cannot browse it")
            else:
                self.failed(name, _error(exc))
            return False
        self.visible.append(key)
        self.passed(name, _describe_project(project))
        return True

    # --- 4. statuses mapped ----------------------------------------------------------------------

    def check_statuses(self, key: str, visible: bool) -> None:
        name = f"statuses {key} mapped"
        if self.jira_skipped:
            self.failed(name, f"skipped: {self.jira_skipped}")
            return
        if not visible:
            self.failed(name, "skipped: project not visible")
            return
        if self.process is None:
            self.failed(name, "skipped: process config did not load")
            return
        try:
            payload = self.client.project_statuses(key)
        except JiraError as exc:
            self.failed(name, _error(exc))
            return
        statuses = _status_names(payload)
        if not statuses:
            self.failed(name, "Jira returned no statuses for this project, so the mapping cannot be checked")
            return
        project = self.process.projects.get(key)
        if project is None:
            # Mapped from Jira's status categories (JO-17): shown, so the user can check it or write their own.
            stages = category_stages(self.process)
            categories = _status_categories(payload)
            pairs = [(status, stages.get(categories.get(status.casefold(), ""))) for status in statuses]
            unmapped = [status for status, stage in pairs if stage is None]
            mapped = ", ".join(f"{_quote(status)} -> {stage}" for status, stage in pairs if stage is not None)
            detail = (f"{key} is not in {self.process_path.name}; mapped from Jira's status categories: "
                      f"{mapped or 'none'}")
            if unmapped:
                self.report.unmapped_statuses[key] = unmapped
                detail += f"; no category for {_quoted(unmapped)}"
            self.warned(name, detail)
            return
        unmapped = [status for status in statuses if project.stage_of(status) is None]
        if unmapped:
            self.report.unmapped_statuses[key] = unmapped
            self.failed(
                name,
                f"no stage for {_quoted(unmapped)} "
                f"(add to status_to_stage for {key} in {self.process_path.name})",
            )
            return
        pairs = ", ".join(f"{_quote(status)} -> {project.stage_of(status)}" for status in statuses)
        self.passed(name, f"{len(statuses)} statuses: {pairs}")

    # --- 5. fields ---------------------------------------------------------------------------------

    def check_fields(self, path: Path, site: str, write: bool) -> None:
        name = "fields"
        if self.jira_skipped:
            self.failed(name, f"skipped: {self.jira_skipped}")
            return
        unchanged = f"; {path.name} left unchanged" if write else ""
        try:
            site_fields = [item for item in self.client.fields() if _usable_field(item)]
            if not site_fields:
                self.failed(name, f"Jira returned no fields{unchanged}")
                return
            resolved, notes = self._resolve(site_fields)
        except JiraError as exc:
            # A failed probe leaves a duplicate undecided: better no file than a wrong field.
            self.failed(name, f"{_error(exc)}{unchanged}")
            return
        config = FieldsConfig(generated_at=to_iso(self.now()), site=site, fields=resolved)
        self.report.fields = config

        found = [f"{logical}={_clean(ref.id)}" for logical, ref in resolved.items() if ref is not None]
        missing = [logical for logical, ref in resolved.items() if ref is None]
        required = [logical for logical in missing if logical == "story_points" and self._points_required()]
        optional = [logical for logical in missing if logical not in required]
        parts = [", ".join(found) if found else "none of the known fields found", *notes]
        if required:
            parts.append("missing: story_points (required: an in-scope project estimates in points)")
        if optional:
            labels = ", ".join(_optional_label(logical) for logical in optional)
            parts.append(f"not found (optional): {labels}")
        write_failed = False
        if not write:
            parts.append(f"{path.name} not written")
        elif required:
            # sync trusts fields.yaml: a required field written as missing would be
            # blanked on every issue at the next sync, so the last good file stays.
            parts.append(f"{path.name} left unchanged")
        else:
            try:
                config.dump(path)
            except OSError as exc:
                write_failed = True
                parts.append(f"could not write {path}: {_error(exc)}")
            else:
                self.report.fields_written_to = path
                parts.append(f"written to {path}")
        detail = "; ".join(parts)
        if required or write_failed:
            self.failed(name, detail)
        elif optional:
            self.warned(name, detail)
        else:
            self.passed(name, detail)

    def _resolve(self, site_fields: list[dict[str, Any]]) -> tuple[dict[str, FieldRef | None], list[str]]:
        """Logical name -> the site's field (or None), plus a note per duplicate decided."""
        by_name: dict[str, list[dict[str, Any]]] = {}
        # Sorted, so a name used twice lists its fields in a stable order (oldest id first).
        for item in sorted(site_fields, key=_field_order):
            by_name.setdefault(_fold(item["name"]), []).append(item)
        resolved: dict[str, FieldRef | None] = {}
        notes: list[str] = []
        for logical, candidates in FIELD_CANDIDATES.items():
            matches: list[dict[str, Any]] = []
            for candidate in candidates:
                for item in by_name.get(_fold(candidate), []):
                    if all(item["id"] != match["id"] for match in matches):
                        matches.append(item)
            if not matches:
                resolved[logical] = None
                continue
            chosen = matches[0]
            if len(matches) > 1:
                chosen, note = self._choose(logical, matches)
                notes.append(note)
            resolved[logical] = _field_ref(chosen)
        return resolved, notes

    def _choose(self, logical: str, matches: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        """Of several fields matching one logical name, the first one in use, else the first."""
        listed = ", ".join(_clean(item["id"]) for item in matches)
        chosen, reason = matches[0], "none is set on in-scope issues, so the first by preference"
        if not self.visible:
            reason = "not probed (no visible project), so the first by preference"
        else:
            for item in matches:
                if self._is_populated(item["id"]):
                    chosen, reason = item, "the one set on in-scope issues"
                    break
        used = _clean(chosen["id"])
        return chosen, f"{logical}: {len(matches)} fields match ({listed}); using {used}, {reason}"

    def _is_populated(self, field_id: str) -> bool | None:
        """Whether any issue in the visible in-scope projects has the field set.

        One search returning at most one issue. None when the field can't be
        probed (not a custom field, or no visible project). A JiraError other
        than HTTP 400 propagates.
        """
        number = _CUSTOM_FIELD_ID.fullmatch(field_id)
        if number is None or not self.visible:
            return None
        if field_id not in self.populated:
            scope = ", ".join(f'"{key}"' for key in self.visible)
            # Keys are quoted: a key such as USER or DATE is a reserved word in JQL.
            jql = f"project in ({scope}) AND cf[{number.group(1)}] is not EMPTY"
            try:
                first = next(iter(self.client.search_jql(jql, ["id"], page_size=1)), None)
            except JiraError as exc:
                # 400: the field isn't usable in these projects, so no issue there has it set.
                if exc.status_code != 400:
                    raise
                first = None
            self.populated[field_id] = bool(first)
        return self.populated[field_id]

    def _points_required(self) -> bool:
        if self.process is None:
            return False
        return any(
            self.process.projects[key].estimate.basis == "points"
            for key in self.keys
            if key in self.process.projects
        )

    # --- 6. evidence -------------------------------------------------------------------------------

    def check_evidence(self) -> None:
        name = "evidence"
        if self.process is None:
            self.failed(name, "skipped: process config did not load")
            return
        parts: list[str] = []
        for key in self.keys:
            if key not in self.process.projects:
                # Mapped from Jira's status categories (JO-17): no evidence source at all.
                missing = self.process.work_stages()[1:]
                self.report.missing_evidence[key] = missing
                stages = ", ".join(_clean(stage) for stage in missing)
                parts.append(f"{key} not in {self.process_path.name}, so not measurable (no evidence source): {stages}")
                continue
            missing = self.process.missing_evidence(key)
            if missing:
                self.report.missing_evidence[key] = missing
                stages = ", ".join(_clean(stage) for stage in missing)
                parts.append(f"{key} not measurable (no evidence source): {stages}")
            else:
                parts.append(f"{key} has every evidence source configured")
        parts.append(self._evidence_fields())
        detail = "; ".join(parts)
        if self.report.missing_evidence:
            self.warned(name, detail)
        else:
            self.passed(name, detail)

    def _evidence_fields(self) -> str:
        """Which candidate evidence fields exist and whether in-scope issues use them."""
        if self.jira_skipped:
            return f"candidate evidence fields not checked ({self.jira_skipped})"
        if self.report.fields is None:
            return "candidate evidence fields not checked (fields unavailable)"
        if not self.visible:
            return "candidate evidence fields not checked (no visible project)"
        groups: dict[str, list[str]] = {
            "in use": [],
            "existing but empty": [],
            "not on this site": [],
            "not checked": [],
        }
        problem: str | None = None
        for logical in _EVIDENCE_FIELDS:
            ref = self.report.fields.fields.get(logical)
            if ref is None:
                groups["not on this site"].append(FIELD_CANDIDATES[logical][0])
                continue
            populated: bool | None = None
            # After one failed probe the rest would fail the same way, each after its retries.
            if problem is None:
                try:
                    populated = self._is_populated(ref.id)
                except JiraError as exc:
                    problem = _error(exc)
            label = _clean(ref.name)
            if populated is None:
                groups["not checked"].append(label)
            else:
                groups["in use" if populated else "existing but empty"].append(label)
        text = "candidate evidence fields " + "; ".join(
            f"{title}: {', '.join(labels)}" for title, labels in groups.items() if labels
        )
        return f"{text} ({problem})" if problem else text

    # --- 7. database -----------------------------------------------------------------------------

    def check_database(self) -> None:
        name = "database"
        try:
            alive = self.store.query_one("SELECT 1")
            version_row = self.store.query_one("PRAGMA user_version")
        except sqlite3.Error as exc:
            self.failed(name, f"SQLite error: {_error(exc)}")
            return
        where = str(self.store.path) if self.store.path is not None else "in memory"
        if alive is None or alive[0] != 1:
            self.failed(name, f"SELECT 1 returned no row ({where})")
            return
        version = version_row[0] if version_row is not None else None
        if version != SCHEMA_VERSION:
            self.failed(name, f"schema version {version}, expected {SCHEMA_VERSION} ({where})")
            return
        self.passed(name, f"SQLite schema version {version} ({where})")


# --- helpers ------------------------------------------------------------------------------


def _project_keys(projects: list[str]) -> list[str]:
    """Checked, upper-cased and de-duplicated keys: they go into URL paths and JQL."""
    try:
        keys = validate_project_keys(list(projects))
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    if not keys:
        raise ConfigError("no Jira projects to check: set JIRA_PROJECTS or pass --project")
    return list(dict.fromkeys(keys))


def _result(check: Check) -> str:
    # Only a check that didn't pass is graded; a passing one is "ok" even if flagged as a warning, or "info".
    if check.ok:
        return "info" if check.info else "ok"
    return "warn" if check.warning else "fail"


def _tag(check: Check) -> str:
    return {"ok": "[ OK ]", "info": "[INFO]", "warn": "[WARN]", "fail": "[FAIL]"}[_result(check)]


def _verdict(checks: list[Check]) -> str:
    failed = sum(1 for check in checks if not check.ok and not check.warning)
    warnings = sum(1 for check in checks if not check.ok and check.warning)
    if failed:
        summary = f"{failed} of {len(checks)} checks failed"
        if warnings:
            summary += f", {_count(warnings, 'warning')}"
        return f"Verdict: FAIL ({summary})"
    if warnings:
        return f"Verdict: PASS with {_count(warnings, 'warning')}"
    return "Verdict: PASS"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _describe_project(project: dict[str, Any]) -> str:
    simplified = project.get("simplified")
    style = project.get("style")
    if isinstance(simplified, bool):
        team_managed: bool | None = simplified
    elif style in ("next-gen", "classic"):
        team_managed = style == "next-gen"
    else:
        team_managed = None
    if team_managed is None:
        kind = "management style not reported"
    else:
        kind = "team-managed" if team_managed else "company-managed"
    title = project.get("name")
    return f"{_quote(title)}, {kind}" if isinstance(title, str) and title.strip() else kind


def _status_categories(issue_types: list[Any]) -> dict[str, str]:
    """Each status name (case folded) -> its category key (new, indeterminate, done), first seen first."""
    found: dict[str, str] = {}
    for issue_type in issue_types:
        statuses = issue_type.get("statuses") if isinstance(issue_type, dict) else None
        for status in statuses if isinstance(statuses, list) else ():
            status_name = status.get("name") if isinstance(status, dict) else None
            category = status.get("statusCategory") if isinstance(status, dict) else None
            key = category.get("key") if isinstance(category, dict) else None
            if isinstance(status_name, str) and status_name.strip() and isinstance(key, str):
                found.setdefault(status_name.casefold(), key)
    return found


def _status_names(issue_types: list[Any]) -> list[str]:
    """Unique status names, first seen first: every issue type lists the statuses it uses."""
    names: dict[str, str] = {}
    for issue_type in issue_types:
        statuses = issue_type.get("statuses") if isinstance(issue_type, dict) else None
        for status in statuses if isinstance(statuses, list) else ():
            status_name = status.get("name") if isinstance(status, dict) else None
            if isinstance(status_name, str) and status_name.strip():
                names.setdefault(status_name.casefold(), status_name)
    return list(names.values())


def _usable_field(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and bool(item["id"].strip())
        and isinstance(item.get("name"), str)
        and bool(item["name"].strip())
    )


def _field_order(item: dict[str, Any]) -> tuple[int, int, str]:
    """Custom fields by number (customfield_9 before customfield_10), then the rest by id."""
    number = _CUSTOM_FIELD_ID.fullmatch(item["id"])
    return (0, int(number.group(1)), item["id"]) if number else (1, 0, item["id"])


def _field_ref(item: dict[str, Any]) -> FieldRef:
    schema = item.get("schema")
    custom_type = schema.get("custom") if isinstance(schema, dict) else None
    return FieldRef(
        id=item["id"],
        name=item["name"],
        custom_type=custom_type if isinstance(custom_type, str) and custom_type else None,
    )


def _fold(name: str) -> str:
    return name.strip().casefold()


def _optional_label(logical: str) -> str:
    if logical == "epic_link":
        # Team-managed projects (and current company-managed ones) link epics as the parent.
        return "epic_link (epics come from the parent field)"
    return logical


def _error(exc: BaseException) -> str:
    return _clean(str(exc), _ERROR_LIMIT) or type(exc).__name__


def _printable(text: str) -> str:
    """Newlines, escape codes and other control characters become spaces, so
    Jira's text can neither break a report line nor fake one."""
    return "".join(char if char.isprintable() else " " for char in text)


def _clean(value: object, limit: int = _NAME_LIMIT) -> str:
    """Free text (an error, a project name) as one short line."""
    return _shorten(" ".join(_printable(str(value)).split()), limit)


def _quote(value: object, limit: int = _NAME_LIMIT) -> str:
    """A name in double quotes, its spaces kept: a stray trailing space is worth seeing."""
    return f'"{_shorten(_printable(str(value)), limit)}"'


def _quoted(names: list[str]) -> str:
    return ", ".join(_quote(name) for name in names)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
