"""Read-only MCP server over stdio: one tool per catalogue entry plus oversight_catalog."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema

from oversight.ask.catalog import CATALOG, CatalogContext, QuerySpec, catalog_schema, validate_params
from oversight.config import ProcessConfig
from oversight.errors import ConfigError
from oversight.logs import get_logger
from oversight.rules.catalog import RulesConfig
from oversight.rules.settings import effective_rules, load_board_settings
from oversight.settings import Settings, get_settings
from oversight.store.db import open_readonly
from oversight.timeutil import utcnow

SERVER_NAME = "jira_oversight_mcp"
CATALOG_TOOL = "oversight_catalog"
EXIT_OK = 0

# FastMCP accepts exactly these level names; anything else in LOG_LEVEL falls back to INFO.
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

log = get_logger("mcp")


def build_server(settings: Settings, process: ProcessConfig, rules: RulesConfig, *, db_path: Path | None = None) -> FastMCP:
    """The server with one read-only tool per catalogue entry plus oversight_catalog (see the module docstring)."""
    if not settings.jira_projects:
        raise ConfigError("JIRA_PROJECTS is empty: the server needs a default project")
    runner = _Runner(
        path=Path(db_path) if db_path is not None else settings.db_file,
        settings=settings,
        process=process,
        rules=rules,
        default_project=settings.jira_projects[0],
    )
    server = FastMCP(SERVER_NAME, instructions=_instructions(runner), log_level=_log_level(settings))
    for spec in CATALOG.values():
        title = _title(spec.name)
        server.add_tool(
            _tool_function(spec, runner),
            name=spec.name,
            title=title,
            description=spec.description,
            annotations=_read_only(title),
            structured_output=False,
        )
    server.add_tool(
        _catalog_function(runner),
        name=CATALOG_TOOL,
        title="Catalogue",
        description=(
            "The vocabulary of this server: every tool with its parameter schema, the configured projects "
            "and the rule ids. Call it first."
        ),
        annotations=_read_only("Catalogue"),
        structured_output=False,
    )
    return server


def main(settings: Settings | None = None) -> int:
    """`python -m oversight mcp`: serve over stdio until the client disconnects."""
    settings = settings or get_settings()
    process = served_process(ProcessConfig.load(settings.process_file), settings)
    rules = RulesConfig.load(settings.rules_file)
    server = build_server(settings, process, rules)
    if not settings.db_file.exists():
        # Not fatal: every tool answers with an error until a sync or demo creates the file.
        log.warning("mcp_database_missing", path=str(settings.db_file))
    log.info(
        "mcp_server_starting",
        name=SERVER_NAME,
        transport="stdio",
        db=str(settings.db_file),
        project=settings.jira_projects[0],
        tools=len(CATALOG) + 1,
    )
    # stdout carries the protocol; logs go to stderr (oversight.logs).
    server.run(transport="stdio")
    return EXIT_OK


def served_process(process: ProcessConfig, settings: Settings) -> ProcessConfig:
    """process.yaml with every served board mapped, from the snapshot when one can be read."""
    from oversight.boards import board_process

    if settings.db_file.exists():
        try:
            conn = open_readonly(settings.db_file)
            try:
                return board_process(process, conn, settings)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("mcp_boards_unreadable", error_type=type(exc).__name__)
    return board_process(process, None, settings)


# --- one call --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Runner:
    """What every tool call needs: where the database is and the configuration the catalogue reads."""

    path: Path
    settings: Settings
    process: ProcessConfig
    rules: RulesConfig
    default_project: str

    def run(self, spec: QuerySpec, raw_params: Any) -> str:
        """The spec's facts as JSON text, or {"error": ...}; never raises (the client sees every failure as data)."""
        try:
            return self._run(spec, raw_params)
        except Exception as exc:  # noqa: BLE001 - a tool boundary: the failure is logged with its traceback and reported
            log.exception("mcp_tool_failed", tool=spec.name)
            return _error(spec.name, f"the query failed unexpectedly ({type(exc).__name__}: {exc})")

    def _run(self, spec: QuerySpec, raw_params: Any) -> str:
        try:
            params = validate_params(spec.name, {} if raw_params is None else raw_params)
        except ValueError as exc:
            return _error(spec.name, f"invalid parameters: {exc}")
        project = getattr(params, "project", None) or self.default_project
        if hasattr(params, "project") and project not in self.process.projects:
            configured = ", ".join(sorted(self.process.projects))
            return _error(spec.name, f"project {project} is not configured in process.yaml (configured: {configured})")
        if not self.path.exists():
            return _error(
                spec.name,
                f"no database at {self.path}: run `python -m oversight sync --full` "
                "(or `python -m oversight demo`) first",
            )
        try:
            conn = open_readonly(self.path)
        except sqlite3.Error as exc:
            return _error(spec.name, f"the database at {self.path} could not be opened read-only: {exc}")
        try:
            ctx = CatalogContext(
                process=self.process,
                rules=effective_rules(self.rules, load_board_settings(conn, project)),
                project=self.default_project,
                now=utcnow(),
                row_limit=self.settings.ask_row_limit,
            )
            facts = spec.run(conn, params, ctx)
        except sqlite3.Error as exc:
            return _error(
                spec.name,
                f"the database at {self.path} could not be read ({exc}); it may not be an oversight snapshot yet",
            )
        except ConfigError as exc:
            return _error(spec.name, str(exc))
        finally:
            conn.close()
        payload = facts.to_dict()
        payload.pop("ref", None)  # F1/F2 numbering belongs to ask(), not to a single tool call
        log.info("mcp_tool_called", tool=spec.name, project=project, rows=len(facts.rows), truncated=facts.truncated)
        return json.dumps(payload, ensure_ascii=False)

    def catalog(self) -> str:
        """The vocabulary: tools, projects and rules; without a database, every board's rules are rules.yaml's."""
        return json.dumps(
            {
                "server": SERVER_NAME,
                "default_project": self.default_project,
                "projects": sorted(self.process.projects),
                "row_limit": self.settings.ask_row_limit,
                "rules": [
                    {"id": rule.id, "title": rule.title, "kind": rule.kind, "severity": rule.severity}
                    for rule in self.rules.enabled_rules
                ],
                "board_rules": {
                    project: [
                        {"id": rule.id, "title": rule.title, "kind": rule.kind, "severity": rule.severity,
                         "enabled": rule.enabled, "custom": rule.custom}
                        for rule in rules.rules
                    ]
                    for project, rules in self.board_rules().items()
                },
                "tools": catalog_schema(),
            },
            ensure_ascii=False,
        )

    def board_rules(self) -> dict[str, RulesConfig]:
        """Each configured board's rules by project; rules.yaml's when there is no readable snapshot."""
        projects = sorted(self.process.projects)
        if not self.path.exists():
            return {project: self.rules for project in projects}
        try:
            conn = open_readonly(self.path)
            try:
                return {project: effective_rules(self.rules, load_board_settings(conn, project)) for project in projects}
            finally:
                conn.close()
        except (sqlite3.Error, ValueError) as exc:
            log.warning("mcp_board_rules_unreadable", error_type=type(exc).__name__)
            return {project: self.rules for project in projects}


def _error(tool: str, message: str) -> str:
    return json.dumps({"tool": tool, "error": message}, ensure_ascii=False)


# --- registration helpers ---------------------------------------------------------------------


def _tool_function(spec: QuerySpec, runner: _Runner) -> Callable[..., str]:
    def tool(params: Any = None) -> str:
        return runner.run(spec, params)

    tool.__name__ = spec.name
    tool.__qualname__ = spec.name
    tool.__doc__ = spec.description
    # Real types, not postponed strings: FastMCP evaluates them to build the argument model.
    tool.__annotations__ = {
        "params": Annotated[Any, WithJsonSchema(spec.params_model.model_json_schema())],
        "return": str,
    }
    return tool


def _catalog_function(runner: _Runner) -> Callable[[], str]:
    def oversight_catalog() -> str:
        return runner.catalog()

    oversight_catalog.__annotations__ = {"return": str}
    return oversight_catalog


def _read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _title(name: str) -> str:
    """'oversight_time_in_status' -> 'Time in status'."""
    return name.removeprefix("oversight_").replace("_", " ").capitalize()


def _instructions(runner: _Runner) -> str:
    return (
        "Read-only questions about the Jira oversight snapshot: issues, status timelines, metrics, "
        f"rule findings and early warnings for the configured projects (default {runner.default_project}). "
        "Every number comes from the database, never from a model, and nothing here writes. "
        f"Call {CATALOG_TOOL} first to see the tools, their parameters and the rule ids. "
        "Answers stay at team and process level: no tool reports on individual people."
    )


def _log_level(settings: Settings) -> Any:
    level = settings.log_level.strip().upper()
    return level if level in _LOG_LEVELS else "INFO"
