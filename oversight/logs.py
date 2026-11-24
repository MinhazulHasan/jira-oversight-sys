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
    """Whatever sys.stderr is when a line is written, not when logging was configured."""

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


_STDERR = _CurrentStderr()

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
    """A logger that reads the logging configuration each time it logs."""

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
