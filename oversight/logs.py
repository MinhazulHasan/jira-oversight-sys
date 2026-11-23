"""Structured logging: JSON lines on stderr, so stdout stays free for reports."""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

# Defence in depth: values under these keys never reach a log line.
_SECRET_MARKERS = ("token", "password", "authorization", "secret", "api_key", "cookie")


def _redact(_logger: Any, _method: str, event: dict[str, Any]) -> dict[str, Any]:
    for key in list(event):
        if any(marker in key.lower() for marker in _SECRET_MARKERS):
            event[key] = "***"
    return event


class _CurrentStderr:
    """Whatever sys.stderr is when a line is written, not when logging was configured.

    Something may swap sys.stderr after configure_logging() (pytest does, per
    test, and closes the old stream); writing to the stream captured at
    configure time would then fail with "I/O operation on closed file".
    """

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


_STDERR = _CurrentStderr()

# Stdlib loggers of the HTTP layer. httpx logs every request as "HTTP Request: POST
# <full url>" at INFO, and a Teams webhook URL is the channel's credential: whatever
# configures stdlib logging later (logging.basicConfig, a library), those lines stay out.
_QUIET_LIBRARIES = ("httpx", "httpcore")


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    for name in _QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
    renderer: Any
    if fmt == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=_STDERR),  # type: ignore[arg-type]
        cache_logger_on_first_use=False,
    )


class StructlogHandler(logging.Handler):
    """A stdlib record (the web server's own lines: start-up, shut-down, an error) as a structured line, so the API's
    log is one JSON object per line throughout with LOG_FORMAT=json."""

    _LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            level = record.levelname.lower()
            logger = structlog.get_logger().bind(logger=record.name)
            extra = {"exc_info": record.exc_info} if record.exc_info else {}
            getattr(logger, level if level in self._LEVELS else "info")(message, **extra)
        except Exception:  # a log line must never take the server down
            self.handleError(record)


def server_log_config() -> dict[str, Any]:
    """uvicorn's logging for `python -m oversight api`: its own lines through structlog (StructlogHandler), and its
    per-request access lines off. Those carried the full query string (Findings' free-text search among it) in plain
    text; the API logs each request itself as `api_request` (method, route template, status, ms), never the query."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"structlog": {"class": "oversight.logs.StructlogHandler"}},
        "loggers": {
            "uvicorn": {"handlers": ["structlog"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
        },
    }


class _LazyLogger:
    """A logger that reads the logging configuration each time it logs.

    structlog's `get_logger().bind(...)` builds a logger from the configuration
    of that moment. A module-level `log = get_logger("store")` is created at
    import, before the CLI calls configure_logging(), so it would keep
    structlog's defaults for good: console text on stdout, debug included, and
    invisible to structlog.testing.capture_logs(). This proxy builds the real
    logger on every call instead, so it is safe to create at import time.
    """

    __slots__ = ("_values",)

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def bind(self, **values: Any) -> _LazyLogger:
        return _LazyLogger({**self._values, **values})

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            # Never resolve private or special names through structlog (copy, pickle).
            raise AttributeError(name)
        return getattr(structlog.get_logger().bind(**self._values), name)

    def __repr__(self) -> str:
        return f"<lazy logger {self._values!r}>"


def get_logger(name: str | None = None) -> Any:
    """A structlog logger, bound to `logger=name`, that follows the current configuration."""
    return _LazyLogger({"logger": name} if name else {})
