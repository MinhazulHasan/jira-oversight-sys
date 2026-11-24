"""Delivery channels behind one interface (Phase 5)."""
from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Protocol

import httpx

from oversight.errors import ConfigError, OversightError
from oversight.logs import get_logger
from oversight.settings import Settings

log = get_logger("notify")

SMTP_TIMEOUT_S = 30.0
TEAMS_TIMEOUT_S = 30.0
TEAMS_CONNECT_TIMEOUT_S = 5.0
MAX_CARD_ACTIONS = 6
# Workflows webhooks reject large payloads (about 28 KB); stop adding text well before.
MAX_CARD_TEXT_CHARS = 20_000
ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
_MARKDOWN_SPECIAL = "\\*_[]`"


class ChannelError(OversightError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Message:
    subject: str
    text: str
    html: str | None = None
    # (label, url) pairs the Teams card may render as buttons.
    links: list[tuple[str, str]] = field(default_factory=list)


class Channel(Protocol):
    name: str

    def send(self, message: Message) -> str:
        """Deliver and return a delivery reference (path, message id or status)."""
        ...


def _scrub(text: str, *secrets: str) -> str:
    """Replace every non-empty secret in `text`, so no error or log line carries it."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# --- file -----------------------------------------------------------------------


class FileChannel:
    name = "file"

    def __init__(self, directory: Path, *, project: str, date: str) -> None:
        self.directory = Path(directory)
        self.path = self.directory / f"{project}_{date}.md"

    def send(self, message: Message) -> str:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path.write_text(f"# {message.subject}\n\n{message.text}\n", encoding="utf-8")
        except OSError as exc:
            raise ChannelError(f"cannot write the digest file {self.path}: {exc}") from exc
        log.info("digest_file_written", path=str(self.path))
        return str(self.path)


# --- email ----------------------------------------------------------------------


def _smtp_retryable(exc: OSError) -> bool:
    """Whether a later attempt could succeed"""
    if not isinstance(exc, smtplib.SMTPException):
        return True
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, smtplib.SMTPHeloError)):
        return True
    code = getattr(exc, "smtp_code", None)
    return isinstance(code, int) and 400 <= code < 500


class EmailChannel:
    name = "email"

    def __init__(self, settings: Settings, *, smtp_factory: Any = None) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._starttls = settings.smtp_starttls
        self._user = settings.smtp_user
        self._password = settings.smtp_password.get_secret_value()
        self._from = settings.smtp_from
        self._to = list(settings.digest_to)
        self._smtp_factory = smtp_factory or smtplib.SMTP
        missing = [
            name
            for name, value in (("SMTP_HOST", self._host), ("SMTP_FROM", self._from), ("DIGEST_TO", self._to))
            if not value
        ]
        if missing:
            raise ConfigError(f"DIGEST_CHANNEL=email needs {', '.join(missing)}")

    def build(self, message: Message) -> EmailMessage:
        """The MIME message: text/plain alone, or multipart/alternative with text/html."""
        msg = EmailMessage()
        msg["Subject"] = message.subject
        msg["From"] = self._from
        msg["To"] = ", ".join(self._to)
        # A domain avoids the hostname lookup make_msgid does otherwise.
        domain = self._from.rpartition("@")[2] or None
        msg["Message-ID"] = make_msgid(domain=domain)
        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")
        return msg

    def send(self, message: Message) -> str:
        msg = self.build(message)
        message_id = msg["Message-ID"]
        try:
            with self._smtp_factory(self._host, self._port, timeout=SMTP_TIMEOUT_S) as smtp:
                if self._starttls:
                    smtp.starttls()
                if self._user:
                    smtp.login(self._user, self._password)
                smtp.send_message(msg)
        except OSError as exc:  # smtplib.SMTPException is an OSError too
            detail = _scrub(f"{type(exc).__name__}: {exc}", self._password)
            raise ChannelError(f"email delivery failed: {detail}", retryable=_smtp_retryable(exc)) from None
        log.info("digest_email_sent", host=self._host, recipients=len(self._to), message_id=message_id)
        return message_id


# --- teams ----------------------------------------------------------------------


def markdown_escape(text: str) -> str:
    """Make text literal inside an Adaptive Card TextBlock, which renders a Markdown subset"""
    for char in _MARKDOWN_SPECIAL:
        text = text.replace(char, "\\" + char)
    return text


def build_card(message: Message) -> dict[str, Any]:
    """The Workflows webhook payload: one Adaptive Card from the text body."""
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder", "wrap": True, "text": markdown_escape(message.subject)}
    ]
    used = 0
    for section in message.text.split("\n\n"):
        if not section.strip():
            continue
        if used + len(section) > MAX_CARD_TEXT_CHARS:
            body.append({"type": "TextBlock", "wrap": True, "isSubtle": True, "text": "… the rest did not fit in this card."})
            break
        body.append({"type": "TextBlock", "wrap": True, "text": markdown_escape(section)})
        used += len(section)
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    actions = [
        {"type": "Action.OpenUrl", "title": label, "url": url} for label, url in message.links[:MAX_CARD_ACTIONS]
    ]
    if actions:
        card["actions"] = actions
    return {
        "type": "message",
        "attachments": [{"contentType": ADAPTIVE_CARD_CONTENT_TYPE, "contentUrl": None, "content": card}],
    }


class TeamsChannel:
    name = "teams"

    def __init__(self, webhook_url: str, *, http: Any = None) -> None:
        url = (webhook_url or "").strip()
        if not url:
            raise ConfigError("DIGEST_CHANNEL=teams needs TEAMS_WEBHOOK_URL")
        if not url.lower().startswith("https://"):
            raise ConfigError("TEAMS_WEBHOOK_URL must start with https://")
        self._url = url
        self._http = http

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        if self._http is not None:
            return self._http.post(self._url, json=payload)
        timeout = httpx.Timeout(TEAMS_TIMEOUT_S, connect=TEAMS_CONNECT_TIMEOUT_S)
        with httpx.Client(timeout=timeout) as client:
            return client.post(self._url, json=payload)

    def send(self, message: Message) -> str:
        payload = build_card(message)
        try:
            response = self._post(payload)
        except httpx.HTTPError as exc:
            # Not chained: the httpx exception holds the request, and with it the URL.
            detail = _scrub(f"{type(exc).__name__}: {exc}", self._url)
            raise ChannelError(f"Teams webhook request failed: {detail}", retryable=True) from None
        status = response.status_code
        if not 200 <= status < 300:
            detail = _scrub(" ".join(response.text.split())[:200], self._url)
            suffix = f": {detail}" if detail else ""
            raise ChannelError(f"Teams webhook returned HTTP {status}{suffix}", retryable=status == 429 or status >= 500)
        log.info("digest_teams_sent", status=status)
        return str(status)


# --- selection ------------------------------------------------------------------


def get_channel(settings: Settings, *, project: str, date: str) -> Channel:
    """The configured channel (file needs the project and date for its file name)."""
    kind = settings.digest_channel
    if kind == "file":
        return FileChannel(settings.digests_path, project=project, date=date)
    if kind == "email":
        return EmailChannel(settings)
    if kind == "teams":
        return TeamsChannel(settings.teams_webhook_url.get_secret_value())
    raise ConfigError(f"unknown DIGEST_CHANNEL {kind!r}: use file, email or teams")
