"""Exceptions shared across the package."""
from __future__ import annotations


class OversightError(Exception):
    """Base class for errors this package raises on purpose."""


class ConfigError(OversightError):
    """A setting or config file is missing or invalid."""


class ReadOnlyViolation(OversightError):
    """A Jira call outside the read-only allowlist."""

    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"{method} {path} is not on the read-only allowlist")
        self.method = method
        self.path = path


class JiraError(OversightError):
    """Jira answered with an error we don't retry."""

    def __init__(self, message: str, *, status_code: int | None = None, path: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path


class JiraAuthError(JiraError):
    """401 or 403: the credentials are wrong or lack Browse Projects."""


class JiraUnavailable(JiraError):
    """Jira kept failing (429, 5xx or network errors) after the last attempt."""


class JiraBudgetSpent(JiraError):
    """A read stopped part way because its time budget ran out."""
