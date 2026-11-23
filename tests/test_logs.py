"""Logging: JSON on stderr whenever and wherever a logger was created."""
from __future__ import annotations

import io
import json
import logging
import logging.config
import sys
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

import oversight.baseline
import oversight.derive.run
import oversight.store.db
from oversight.cli import main
from oversight.logs import configure_logging, get_logger, server_log_config
from oversight.settings import Settings


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_a_logger_made_before_configuration_follows_it(capsys: pytest.CaptureFixture[str]) -> None:
    # Regression: get_logger() used to fix structlog's configuration at creation,
    # so module-level loggers kept the defaults (console text on stdout, debug included).
    structlog.reset_defaults()
    early = get_logger("early")
    configure_logging("INFO", "json")

    early.info("hello", n=1)
    early.debug("not at INFO")

    out, err = capsys.readouterr()
    assert out == ""
    events = [(e["event"], e["logger"], e["level"], e["n"]) for e in json_lines(err)]
    assert events == [("hello", "early", "info", 1)]


@pytest.mark.parametrize(
    ("module", "name"),
    [(oversight.store.db, "store"), (oversight.derive.run, "derive"), (oversight.baseline, "baseline")],
)
def test_module_level_loggers_write_json_to_stderr(
    capsys: pytest.CaptureFixture[str], module: object, name: str
) -> None:
    configure_logging("WARNING", "json")
    module.log.info("filtered out")  # type: ignore[attr-defined]
    module.log.warning("kept", key="AIL-1")  # type: ignore[attr-defined]
    out, err = capsys.readouterr()
    assert out == ""
    assert [(e["event"], e["logger"], e["key"]) for e in json_lines(err)] == [("kept", name, "AIL-1")]


def test_capture_logs_sees_module_level_loggers() -> None:
    configure_logging("DEBUG", "json")
    with capture_logs() as captured:
        oversight.derive.run.log.warning("unmapped_statuses", project="AIL")
    assert [(e["event"], e["logger"], e["log_level"]) for e in captured] == [
        ("unmapped_statuses", "derive", "warning")
    ]


def test_logging_follows_a_replaced_and_closed_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: the stream was captured at configure time; once pytest closed
    # a test's capture stream, every later warning raised "I/O operation on closed file".
    first = io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    configure_logging("INFO", "json")
    get_logger("x").warning("one")
    assert json_lines(first.getvalue())[0]["event"] == "one"

    first.close()
    second = io.StringIO()
    monkeypatch.setattr(sys, "stderr", second)
    get_logger("x").warning("two")
    assert [e["event"] for e in json_lines(second.getvalue())] == ["two"]


def test_a_cli_run_under_capture_does_not_break_later_logging(
    settings: Settings, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The order-dependent failure: main() configures logging under capsys, then
    # a warning is logged once that capture stream has been replaced.
    assert main(["derive", "--project", "A-B"], settings=settings) == 2
    capsys.readouterr()
    later = io.StringIO()
    monkeypatch.setattr(sys, "stderr", later)
    get_logger("later").warning("still fine")
    assert "still fine" in later.getvalue()


def test_bound_values_and_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    log = get_logger("sync").bind(run_id="r1")
    log.info("sync_started", api_token="abc", authorization="Basic xyz", projects=["AIL"])
    (event,) = json_lines(capsys.readouterr().err)
    assert (event["logger"], event["run_id"], event["projects"]) == ("sync", "r1", ["AIL"])
    assert event["api_token"] == event["authorization"] == "***"


def test_the_web_servers_own_lines_are_json_and_its_access_lines_are_off(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # uvicorn's defaults wrote plain text ("INFO:     Started server process") and an access line per request with
    # the full query string, a Findings search as typed among it.
    configure_logging("INFO", "json")
    saved = {name: (logging.getLogger(name).handlers[:], logging.getLogger(name).level, logging.getLogger(name).propagate)
             for name in ("uvicorn", "uvicorn.error", "uvicorn.access")}
    try:
        logging.config.dictConfig(server_log_config())
        logging.getLogger("uvicorn.error").info("Started server process [%d]", 1234)
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET",
                                                 "/api/v1/findings?q=Fictional%20Name", "1.1", 200)
    finally:
        for name, (handlers, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
    out, err = capsys.readouterr()
    assert out == "" and "Fictional" not in err
    assert [(e["event"], e["logger"], e["level"]) for e in json_lines(err)] == [
        ("Started server process [1234]", "uvicorn.error", "info")]

    # `python -m oversight api` runs uvicorn with that config and its access lines off.
    import uvicorn

    ran: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: ran.update(options))
    monkeypatch.setattr("oversight.api.app.create_app", lambda settings: object())
    monkeypatch.setattr("oversight.api.app.prepare_snapshot", lambda *args, **kwargs: None)
    settings = Settings(_env_file=None, api_service_key="k" * 32)
    from oversight.cli import _cmd_api

    _cmd_api(type("Args", (), {"host": None, "port": None})(), settings, None, None)  # type: ignore[arg-type]
    assert ran["access_log"] is False and ran["log_config"] == server_log_config()


def test_run_py_logs_as_python_m_oversight_api_does(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (the JO-12 check): run.py, the README's other way to start the API (how a local or Windows install
    # runs it), kept uvicorn's defaults: plain-text lines, and an access line per request with the full query string,
    # a Findings search as typed among it.
    import runpy

    import uvicorn

    from oversight.cli import _cmd_api
    from oversight.settings import PROJECT_ROOT

    settings = Settings(_env_file=None, api_service_key="k" * 32, log_format="json")
    served: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda target, **options: served.update(options, target=target))
    monkeypatch.setattr("oversight.settings.get_settings", lambda: settings)
    monkeypatch.setattr("oversight.api.app.prepare_snapshot", lambda *args, **kwargs: None)
    structlog.reset_defaults()  # a fresh process: console text on stdout until something configures it

    runpy.run_path(str(PROJECT_ROOT / "run.py"), run_name="__main__")

    assert served["target"] == "oversight.api.app:serve_app" and served["factory"] is True
    assert served["access_log"] is False and served["log_config"] == server_log_config()
    get_logger("api").info("api_request", method="GET", route="/api/v1/findings", status=200)
    out, err = capsys.readouterr()
    assert out == "" and [(e["event"], e["route"]) for e in json_lines(err)] == [("api_request", "/api/v1/findings")]
    # The same logging as `python -m oversight api`, option for option.
    cli: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: cli.update(options))
    monkeypatch.setattr("oversight.api.app.create_app", lambda settings: object())
    _cmd_api(type("Args", (), {"host": None, "port": None})(), settings, None, None)  # type: ignore[arg-type]
    assert {key: served[key] for key in ("log_level", "log_config", "access_log")} == {
        key: cli[key] for key in ("log_level", "log_config", "access_log")}


def test_each_worker_run_py_starts_logs_json(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A reloaded worker (API_RELOAD) is a process of its own that run.py's set-up never reached: serve_app, the
    factory uvicorn calls in it, configures the log itself."""
    from oversight.api import app as app_module

    settings = Settings(_env_file=None, api_service_key="k" * 32, log_format="json")
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "prepare_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "create_app", lambda settings: object())
    structlog.reset_defaults()

    assert app_module.serve_app() is not None
    get_logger("api").info("api_request", method="GET", route="/api/v1/findings", status=200)
    out, err = capsys.readouterr()
    assert out == "" and [e["event"] for e in json_lines(err)] == ["api_request"]


def test_each_api_request_is_one_structured_line_without_its_query(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from oversight.api.app import create_app
    from oversight.llm.scripted import ScriptedProvider
    from tests.test_api_overview import KEY, make_settings

    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings, provider=ScriptedProvider()), headers={"X-Service-Key": KEY})
    configure_logging("INFO", "json")
    with capture_logs() as captured:
        client.get("/api/v1/findings", params={"q": "Fictional Name payroll"})
        client.get("/api/v1/people/p0123456789ab/avatar")
        client.get("/no/such/path")
    lines = [line for line in captured if line["event"] == "api_request"]
    assert [(line["method"], line["route"]) for line in lines] == [
        ("GET", "/api/v1/findings"), ("GET", "/api/v1/people/{person_id}/avatar"), ("GET", "unmatched")]
    assert all(isinstance(line["status"], int) and isinstance(line["ms"], int) for line in lines)
    assert "Fictional" not in repr(captured) and "p0123456789ab" not in repr(lines)


def test_the_http_layer_never_logs_request_urls(caplog: pytest.LogCaptureFixture) -> None:
    # A webhook URL is a credential: httpx's INFO "HTTP Request: POST <url>" must stay out,
    # even when something sets stdlib logging to INFO after the CLI configured it.
    configure_logging("DEBUG", "console")
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("HTTP Request: POST https://example.webhook.office.com/secret-path")
        logging.getLogger("httpcore").info("connect_tcp.started host='example.webhook.office.com'")
        logging.getLogger("httpx").warning("a warning still gets through")
    assert "secret-path" not in caplog.text and "connect_tcp" not in caplog.text
    assert "a warning still gets through" in caplog.text
