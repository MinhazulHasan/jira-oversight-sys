"""Command line: `python -m oversight <command>`."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from oversight import __version__
from oversight.errors import ConfigError, JiraAuthError, JiraError, OversightError, ReadOnlyViolation
from oversight.logs import configure_logging, get_logger
from oversight.settings import Settings, get_settings, validate_project_keys
from oversight.store.db import Store
from oversight.timeutil import utcnow, zone

if TYPE_CHECKING:
    from oversight.config import FieldsConfig, ProcessConfig
    from oversight.jira.client import JiraClient
    from oversight.rules.catalog import RulesConfig

EXIT_OK = 0
EXIT_CHECKS_FAILED = 1
EXIT_CONFIG = 2
EXIT_JIRA_AUTH = 3
EXIT_JIRA = 4
EXIT_GUARD = 5
EXIT_SERVICE = 6

ClientFactory = Callable[[Settings], "JiraClient"]
# The clock every command reads; tests inject a fixed one, so a whole pipeline is deterministic.
Clock = Callable[[], datetime]


def _default_client_factory(settings: Settings) -> JiraClient:
    from oversight.jira.client import JiraClient

    return JiraClient.from_settings(settings)


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m oversight",
        description="Jira Oversight: read-only process oversight over Jira data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--log-level", help="override LOG_LEVEL (DEBUG, INFO, WARNING, ERROR)")
    parser.add_argument("--log-format", choices=["json", "console"], help="override LOG_FORMAT")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add_project(p: argparse.ArgumentParser, default: str = "JIRA_PROJECTS") -> None:
        p.add_argument(
            "--project",
            action="append",
            metavar="KEY",
            help=f"project key (repeatable); default: {default}",
        )

    kept = "JIRA_PROJECTS and every other board synced before"

    p = sub.add_parser("doctor", help="Phase 0: check access, fields and config (read-only)")
    add_project(p)
    p.add_argument("--no-write-fields", action="store_true", help="don't rewrite config/fields.yaml")
    p.add_argument(
        "--production",
        action="store_true",
        help="check this deployment's settings, config, snapshot and backups instead (offline: no Jira call)",
    )

    p = sub.add_parser("sync", help="Phase 1: copy issues, changelogs, worklogs and links locally")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_true", help="first load, or a complete refresh")
    mode.add_argument("--incremental", action="store_true", help="only what changed since the last sync")
    add_project(p, kept)

    p = sub.add_parser("derive", help="Phase 2: rebuild status timelines and metrics")
    add_project(p, kept)

    p = sub.add_parser("baseline", help="Phase 2: regenerate the baseline report")
    add_project(p, f"JIRA_PROJECTS; with --missing, {kept}")
    p.add_argument("--as-of", type=_iso_date, help="window end, exclusive (default: today)")
    p.add_argument("--window-days", type=int, default=90, help="window length in days (default: 90)")
    p.add_argument(
        "--tz",
        help="IANA zone for day boundaries (default: REPORT_TIMEZONE, else UTC)",
    )
    which = p.add_mutually_exclusive_group()
    which.add_argument(
        "--compare",
        action="store_true",
        help="use the project, window and zone in config/baseline_expected.yaml (the reference audit) and compare",
    )
    which.add_argument(
        "--missing",
        action="store_true",
        help="only the boards that have no baseline report yet (the cycle and each board's Sync now run this)",
    )

    p = sub.add_parser("rules", help="Phases 3-4: evaluate the rule catalogue, open and resolve findings")
    add_project(p, kept)

    p = sub.add_parser("digest", help="Phase 5: send the daily digest of new breaches, warnings and resolutions")
    add_project(p)
    p.add_argument("--channel", choices=["file", "email", "teams"], help="override DIGEST_CHANNEL")
    p.add_argument("--tz", help="IANA zone for the dates in the digest (default: REPORT_TIMEZONE, else UTC)")

    p = sub.add_parser("ask", help="Phase 6: ask a question about the data (answered from the snapshot, with citations)")
    p.add_argument("question", help='the question, e.g. "Where do tickets get stuck most?"')
    add_project(p)
    p.add_argument("--user", default="cli", help="who is asking (recorded in qa_log)")
    p.add_argument("--json", action="store_true", help="print the full response as JSON")

    sub.add_parser("mcp", help="Phase 6: run the read-only MCP server over stdio (Claude Desktop / Claude Code)")

    p = sub.add_parser("insights", help="Phase 7: write the weekly summary of hot spots and problems")
    add_project(p)
    p.add_argument("--tz", help="IANA zone for the week boundary (default: REPORT_TIMEZONE, else UTC)")
    p.add_argument("--json", action="store_true", help="print the insight as JSON")

    p = sub.add_parser("api", help="Phase 8: serve the dashboard API (uvicorn)")
    p.add_argument("--host", help="override API_HOST")
    p.add_argument("--port", type=int, help="override API_PORT")

    p = sub.add_parser("demo", help="Seed a synthetic DEMO project so everything runs without Jira")
    p.add_argument("--project", default="DEMO", help="project key (default: DEMO)")
    p.add_argument("--issues", type=int, default=1150, help="how many issues (default: 1150)")
    p.add_argument("--seed", type=int, help="override DEMO_SEED")
    p.add_argument("--reset", action="store_true", help="delete the project's rows first")

    p = sub.add_parser("eval", help="Run the Q&A eval set (tests/eval/questions.yaml) and write reports/qa_eval.md")
    p.add_argument("--live", action="store_true", help="use the configured LLM instead of the scripted provider")
    p.add_argument("--questions", help="path to a questions file (default: tests/eval/questions.yaml)")

    sub.add_parser(
        "backup",
        help="Copy the snapshot to data/backups (SQLite's online backup) and keep the BACKUP_KEEP newest",
    )

    sub.add_parser(
        "discover",
        help="JO-17: list every board (Jira project) the Jira account can browse, read-only (BOARD_DISCOVERY=on)",
    )

    p = sub.add_parser(
        "cycle",
        help="One cycle (discover, sync --incremental, derive, rules), or --loop: one every SYNC_INTERVAL_MINUTES",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="read every issue (sync --full) instead")
    mode.add_argument(
        "--loop",
        action="store_true",
        help="the sync service: a cycle every SYNC_INTERVAL_MINUTES, the first of each UTC day in full, then a "
        "backup once a day; stops on SIGTERM",
    )
    return parser


def _projects(args: argparse.Namespace, settings: Settings) -> list[str]:
    if args.project:
        try:
            return validate_project_keys(args.project)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
    return list(settings.jira_projects)


def _kept(args: argparse.Namespace, settings: Settings, store: Store) -> list[str]:
    from oversight.boards import kept_boards

    return _projects(args, settings) if args.project else kept_boards(store.conn, settings)


def _mapped(process: ProcessConfig, settings: Settings, store: Store) -> ProcessConfig:
    """process.yaml with every served board mapped (oversight.boards.board_process)."""
    from oversight.boards import board_process

    return board_process(process, store.conn, settings)


def _cmd_doctor(args: argparse.Namespace, settings: Settings, client_factory: ClientFactory, now: Clock) -> int:
    from oversight.doctor import run_doctor

    if args.production:
        from oversight.production import run_production_checks

        if args.project or args.no_write_fields:
            raise ConfigError("doctor --production checks the settings only: it takes no --project or --no-write-fields")
        production = run_production_checks(settings, now=now)
        print(production.render())
        return EXIT_OK if production.ok else EXIT_CHECKS_FAILED
    settings.require_jira()
    with Store.open(settings.db_file) as store, client_factory(settings) as client:
        report = run_doctor(
            client,
            store,
            projects=_projects(args, settings),
            process_path=settings.process_file,
            fields_path=settings.fields_file,
            site=settings.jira_base_url,
            write_fields=not args.no_write_fields,
        )
    print(report.render())
    return EXIT_OK if report.ok else EXIT_CHECKS_FAILED


def _cmd_sync(args: argparse.Namespace, settings: Settings, client_factory: ClientFactory, now: Clock) -> int:
    from oversight.config import FieldsConfig
    from oversight.ingest.sync import run_sync

    settings.require_jira()
    if not settings.fields_file.exists():
        raise ConfigError(f"{settings.fields_file} is missing: run `python -m oversight doctor` first")
    fields = FieldsConfig.load(settings.fields_file)
    mode = "full" if args.full else "incremental"
    with Store.open(settings.db_file) as store, client_factory(settings) as client:
        result = run_sync(client, store, fields, projects=_kept(args, settings, store), mode=mode, now=now)
    print(result.render())
    return EXIT_OK


def _cmd_derive(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    from oversight.config import ProcessConfig
    from oversight.derive.run import run_derive

    process = ProcessConfig.load(settings.process_file)
    with Store.open(settings.db_file) as store:
        result = run_derive(store, _mapped(process, settings, store), projects=_kept(args, settings, store), now=now)
    print(result.render())
    return EXIT_OK


def _cmd_baseline(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    """The baseline report of each board asked for."""
    from oversight.baseline import load_expected, report_paths, run_baseline
    from oversight.config import ProcessConfig

    process = ProcessConfig.load(settings.process_file)
    with Store.open(settings.db_file) as store:
        process = _mapped(process, settings, store)
        expected = None
        if args.compare:
            expected = load_expected(settings.baseline_expected_file)
            projects = [expected.project]
            as_of = expected.as_of
            window_days = expected.window_days
            tz_name = args.tz or expected.timezone
        else:
            projects = _projects(args, settings)
            if args.missing:
                projects = [project for project in (projects if args.project else _kept(args, settings, store))
                            if _has_synced(store, project) and not report_paths(settings.reports_path, project)]
            tz_name = args.tz or settings.report_zone
            as_of = args.as_of or now().astimezone(zone(tz_name)).date()
            window_days = args.window_days
        if window_days < 1:
            raise ConfigError("--window-days must be at least 1")
        failed: OversightError | None = None
        for project in projects:
            try:
                result = run_baseline(
                    store,
                    process,
                    project=project,
                    as_of=as_of,
                    window_days=window_days,
                    tz_name=tz_name,
                    out_dir=settings.reports_path,
                    expected=expected,
                    now=now,
                )
            except OversightError as exc:
                if not args.missing:
                    raise
                get_logger("baseline").warning("baseline_missing_failed", project=project, error_type=type(exc).__name__)
                failed = failed or exc
                continue
            print(result.render())
        if failed is not None:
            raise failed
    return EXIT_OK


def _has_synced(store: Store, project: str) -> bool:
    """Whether the board has synced successfully at least once (sync_state)."""
    cursor = store.get_cursor(project)
    return cursor is not None and bool(cursor.last_synced_at)


def _load_configs(settings: Settings) -> tuple[ProcessConfig, RulesConfig, FieldsConfig]:
    from oversight.config import FieldsConfig, ProcessConfig
    from oversight.rules.catalog import RulesConfig

    process = ProcessConfig.load(settings.process_file)
    rules = RulesConfig.load(settings.rules_file)
    fields = FieldsConfig.load(settings.fields_file) if settings.fields_file.exists() else FieldsConfig()
    return process, rules, fields


def _tz(args: argparse.Namespace, settings: Settings) -> str:
    return getattr(args, "tz", None) or settings.report_zone


def _cmd_rules(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    from oversight.rules.engine import run_rules

    process, rules, fields = _load_configs(settings)
    with Store.open(settings.db_file) as store:
        results = run_rules(store, _mapped(process, settings, store), rules, fields,
                            projects=_kept(args, settings, store), now=now)
    for result in results:
        print(result.render())
    return EXIT_OK


def _cmd_digest(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    from oversight.notify.digest import run_digest

    if args.channel:
        settings = settings.model_copy(update={"digest_channel": args.channel})
    process, rules, _fields = _load_configs(settings)
    with Store.open(settings.db_file) as store:
        results = run_digest(
            store, _mapped(process, settings, store), rules, settings, projects=_projects(args, settings),
            tz_name=_tz(args, settings), now=now,
        )
    for result in results:
        print(result.render())
    return EXIT_OK


def _cmd_ask(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    import json

    from oversight.ask.service import ask
    from oversight.llm.provider import get_provider
    from oversight.rules.settings import effective_rules, load_board_settings

    settings.require_llm()
    process, catalogue, _fields = _load_configs(settings)
    provider = get_provider(settings)
    projects = _projects(args, settings)
    with Store.open(settings.db_file) as store:
        rules = effective_rules(catalogue, load_board_settings(store.conn, projects[0]))
        response = ask(
            args.question,
            store=store,
            process=_mapped(process, settings, store),
            rules=rules,
            settings=settings,
            provider=provider,
            user=args.user,
            project=projects[0],
            now=now,
        )
    print(json.dumps(response.to_dict(), indent=2, ensure_ascii=False) if args.json else response.render())
    return EXIT_OK


def _cmd_mcp(_args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, _now: Clock) -> int:
    from oversight.mcp_server.server import main as mcp_main

    return mcp_main(settings)


def _cmd_insights(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    import json

    from oversight.insights.weekly import run_insights
    from oversight.llm.provider import get_provider

    settings.require_llm()
    process, rules, _fields = _load_configs(settings)
    provider = get_provider(settings)
    with Store.open(settings.db_file) as store:
        insights = run_insights(
            store,
            _mapped(process, settings, store),
            rules,
            provider,
            settings,
            projects=_projects(args, settings),
            tz_name=_tz(args, settings),
            now=now,
        )
    for insight in insights:
        print(json.dumps(insight.to_dict(), indent=2, ensure_ascii=False) if args.json else insight.render())
    return EXIT_OK


def _cmd_api(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, _now: Clock) -> int:
    import uvicorn

    from oversight.api.app import create_app, prepare_snapshot
    from oversight.logs import server_log_config

    settings.require_api_auth()
    # The routes never migrate: the snapshot at this version's schema first, as run.py does too.
    prepare_snapshot(settings.db_file, access_log_days=settings.access_log_days)
    app = create_app(settings)
    uvicorn.run(app, host=args.host or settings.api_host, port=args.port or settings.api_port, log_level="info",
                log_config=server_log_config(), access_log=False)
    return EXIT_OK


def _cmd_demo(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    from oversight.demo import seed_demo

    try:
        project = validate_project_keys([args.project])[0]
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    with Store.open(settings.db_file) as store:
        stats = seed_demo(
            store,
            now=now(),
            seed=args.seed if args.seed is not None else settings.demo_seed,
            project=project,
            issues=args.issues,
            reset=args.reset,
        )
    print(stats.render())
    print(
        f"\nNext: python -m oversight derive --project {project} && python -m oversight rules --project {project}"
        f" && python -m oversight digest --project {project}"
        f' && python -m oversight ask "Where do tickets get stuck most?" --project {project}'
    )
    return EXIT_OK


def _cmd_eval(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    from pathlib import Path

    from oversight.eval import build_dataset, load_dataset, run_qa_eval
    from oversight.llm.provider import get_provider
    from oversight.llm.scripted import ScriptedProvider
    from oversight.settings import PROJECT_ROOT

    process, rules, _fields = _load_configs(settings)
    if args.live:
        settings.require_llm()
        provider = get_provider(settings)
    else:
        provider = ScriptedProvider()
    questions = Path(args.questions) if args.questions else PROJECT_ROOT / "tests" / "eval" / "questions.yaml"
    dataset = load_dataset(questions)
    out_path = settings.reports_path / "qa_eval.md"
    if dataset is None:
        # A plain list of cases: asked of the snapshot as it is, at the real time.
        with Store.open(settings.db_file) as store:
            report = run_qa_eval(
                questions, store=store, process=process, rules=rules, settings=settings, provider=provider,
                out_path=out_path, now=now,
            )
    else:
        if dataset.project not in process.projects:
            raise ConfigError(f"{questions.name} needs project {dataset.project} in {settings.process_file.name}")
        with Store.open(":memory:") as store:
            build_dataset(store, process, rules, dataset)
            report = run_qa_eval(
                questions, store=store, process=process, rules=rules, settings=settings, provider=provider,
                out_path=out_path, now=lambda: dataset.now, project=dataset.project,
            )
    print(report.render())
    return EXIT_OK if report.passed else EXIT_CHECKS_FAILED


def _cmd_backup(_args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    from oversight.backup import BackupError, run_backup

    try:
        result = run_backup(settings.db_file, settings.backups_path, keep=settings.backup_keep, now=now)
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return EXIT_CHECKS_FAILED
    print(result.render())
    return EXIT_OK


def _cmd_discover(_args: argparse.Namespace, settings: Settings, client_factory: ClientFactory, now: Clock) -> int:
    from oversight.boards import discover_boards

    if not settings.discovers_boards:
        print("board discovery is off (BOARD_DISCOVERY=off): the service serves JIRA_PROJECTS only")
        return EXIT_OK
    settings.require_jira()
    with Store.open(settings.db_file) as store, client_factory(settings) as client:
        result = discover_boards(client, store, now=now)
    print(result.render())
    return EXIT_OK


def _cmd_cycle(args: argparse.Namespace, settings: Settings, _client_factory: ClientFactory, now: Clock) -> int:
    from oversight.cycle import StepRunner, loop_forever, run_cycle

    # Before the first step: a loop that can never sync must not start (the steps would each say so, every hour).
    settings.require_jira()
    if args.loop:
        return loop_forever(settings, now=now)
    return run_cycle(StepRunner(), full=args.full, discover=settings.discovers_boards)


COMMANDS: dict[str, Callable[[argparse.Namespace, Settings, ClientFactory, Clock], int]] = {
    "doctor": _cmd_doctor,
    "sync": _cmd_sync,
    "derive": _cmd_derive,
    "baseline": _cmd_baseline,
    "rules": _cmd_rules,
    "digest": _cmd_digest,
    "ask": _cmd_ask,
    "mcp": _cmd_mcp,
    "insights": _cmd_insights,
    "api": _cmd_api,
    "demo": _cmd_demo,
    "eval": _cmd_eval,
    "backup": _cmd_backup,
    "discover": _cmd_discover,
    "cycle": _cmd_cycle,
}


def _never_fail_on_output_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):  # a closed or unusual stream: leave it as it is
                pass


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
    now: Clock | None = None,
) -> int:
    """Run one command and return its exit code."""
    _never_fail_on_output_encoding()
    args = build_parser().parse_args(argv)
    try:
        settings = settings or get_settings()
    except ValidationError as exc:
        print(f"config error: invalid settings in the environment or .env:\n{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except UnicodeDecodeError as exc:
        # e.g. Notepad's "Unicode" (UTF-16) save
        print(f"config error: .env is not UTF-8 text (save it as UTF-8): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    configure_logging(args.log_level or settings.log_level, args.log_format or settings.log_format)
    log = get_logger("cli")
    try:
        return COMMANDS[args.command](args, settings, client_factory or _default_client_factory, now or utcnow)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except JiraAuthError as exc:
        print(f"Jira refused the credentials or the permission: {exc}", file=sys.stderr)
        return EXIT_JIRA_AUTH
    except ReadOnlyViolation as exc:
        log.error("read_only_violation", method=exc.method, path=exc.path)
        print(f"blocked a non-read-only Jira call (this is a bug): {exc}", file=sys.stderr)
        return EXIT_GUARD
    except JiraError as exc:
        print(f"Jira error: {exc}", file=sys.stderr)
        return EXIT_JIRA
    except OversightError as exc:
        # ChannelError, LLMError: their messages are already scrubbed of secrets.
        hint = " (temporary: the next run may succeed)" if getattr(exc, "retryable", False) else ""
        print(f"error: {exc}{hint}", file=sys.stderr)
        return EXIT_SERVICE
    except KeyboardInterrupt:
        return 130
