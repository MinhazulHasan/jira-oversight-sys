"""Delivery channels"""
from __future__ import annotations

import json
import smtplib
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from oversight.errors import ConfigError
from oversight.logs import configure_logging
from oversight.notify.channels import (
    MAX_CARD_ACTIONS,
    MAX_CARD_TEXT_CHARS,
    ChannelError,
    EmailChannel,
    FileChannel,
    Message,
    TeamsChannel,
    build_card,
    get_channel,
    markdown_escape,
)
from oversight.settings import Settings

MESSAGE = Message(
    subject="Jira oversight — AIL — 4 Nov 2026",
    text=(
        "Since 3 Nov 2026: 1 new breach, 0 new warnings, 0 resolved.\n"
        "\n"
        "New breaches (1)\n"
        "AIL-1 · Skipped a stage · To Do → Done skipped development\n"
        "  Fix login redirect\n"
        "  https://fake.atlassian.net/browse/AIL-1"
    ),
    html="<p>Since 3 Nov 2026: 1 new breach.</p>",
    links=[("AIL-1", "https://fake.atlassian.net/browse/AIL-1")],
)
PASSWORD = "hunter2-secret"
WEBHOOK = "https://prod-00.westeurope.logic.azure.com/workflows/abc123/triggers/manual/paths/invoke?sig=TOPSECRET"


def test_file_channel_writes_the_documented_file(tmp_path: Path) -> None:
    channel = FileChannel(tmp_path / "reports" / "digests", project="AIL", date="2026-11-04")
    assert channel.name == "file"

    ref = channel.send(MESSAGE)

    path = Path(ref)
    assert path == tmp_path / "reports" / "digests" / "AIL_2026-11-04.md"
    assert path.read_text(encoding="utf-8") == f"# {MESSAGE.subject}\n\n{MESSAGE.text}\n"
    channel.send(Message(subject="Jira oversight — AIL — 4 Nov 2026", text="Nothing changed since 3 Nov 2026."))
    assert path.read_text(encoding="utf-8") == "# Jira oversight — AIL — 4 Nov 2026\n\nNothing changed since 3 Nov 2026.\n"


def test_file_channel_reports_an_unwritable_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "digests"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    with pytest.raises(ChannelError, match="cannot write the digest file") as err:
        FileChannel(blocker, project="AIL", date="2026-11-04").send(MESSAGE)
    assert not err.value.retryable


class FakeSMTP:
    """What smtplib.SMTP looks like from the channel's side."""

    def __init__(self, host: str, port: int, timeout: float | None = None, *, fail: Exception | None = None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.fail = fail
        self.calls: list[Any] = []
        self.sent: list[Any] = []

    def __enter__(self) -> FakeSMTP:
        self.calls.append("connect")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.calls.append("quit")

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))
        if self.fail is not None:
            raise self.fail

    def send_message(self, msg: Any) -> None:
        self.calls.append("send")
        self.sent.append(msg)


def _smtp_factory(created: list[FakeSMTP], *, fail: Exception | None = None, connect_error: Exception | None = None) -> Any:
    def factory(host: str, port: int, timeout: float | None = None) -> FakeSMTP:
        if connect_error is not None:
            raise connect_error
        smtp = FakeSMTP(host, port, timeout, fail=fail)
        created.append(smtp)
        return smtp

    return factory


def _email_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 2525,
        "smtp_from": "oversight@example.com",
        "digest_to": "james@example.com, tazbir@example.com",
        "smtp_user": "bot",
        "smtp_password": PASSWORD,
    }
    values.update(overrides)
    return Settings(**values)


def test_email_channel_sends_a_multipart_message_with_starttls_and_login() -> None:
    created: list[FakeSMTP] = []
    channel = EmailChannel(_email_settings(), smtp_factory=_smtp_factory(created))
    assert channel.name == "email"

    ref = channel.send(MESSAGE)

    (smtp,) = created
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.com", 2525, 30.0)
    assert smtp.calls == ["connect", "starttls", ("login", "bot", PASSWORD), "send", "quit"]
    (msg,) = smtp.sent
    assert msg["Subject"] == MESSAGE.subject and msg["From"] == "oversight@example.com"
    assert msg["To"] == "james@example.com, tazbir@example.com"
    assert msg.is_multipart() and msg.get_content_type() == "multipart/alternative"
    parts = {part.get_content_type(): part.get_content() for part in msg.iter_parts()}
    assert parts["text/plain"].rstrip("\n") == MESSAGE.text
    assert parts["text/html"].rstrip("\n") == MESSAGE.html
    assert ref == msg["Message-ID"] and ref.startswith("<") and ref.endswith("@example.com>")


def test_email_channel_without_html_tls_or_login_sends_plain_text() -> None:
    created: list[FakeSMTP] = []
    settings = _email_settings(smtp_starttls=False, smtp_user="", smtp_password="")
    EmailChannel(settings, smtp_factory=_smtp_factory(created)).send(Message(subject="s", text="plain only"))
    (smtp,) = created
    assert smtp.calls == ["connect", "send", "quit"]
    (msg,) = smtp.sent
    assert not msg.is_multipart() and msg.get_content_type() == "text/plain"
    assert msg.get_content() == "plain only\n"


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"smtp_host": ""}, "SMTP_HOST"),
        ({"smtp_from": ""}, "SMTP_FROM"),
        ({"digest_to": ""}, "DIGEST_TO"),
        ({"smtp_host": "", "smtp_from": "", "digest_to": ""}, "SMTP_HOST, SMTP_FROM, DIGEST_TO"),
    ],
)
def test_email_channel_needs_host_sender_and_recipients_before_connecting(overrides: dict[str, Any], missing: str) -> None:
    created: list[FakeSMTP] = []
    with pytest.raises(ConfigError, match=f"DIGEST_CHANNEL=email needs {missing}$"):
        EmailChannel(_email_settings(**overrides), smtp_factory=_smtp_factory(created))
    assert created == []


def test_email_errors_and_logs_never_carry_the_password() -> None:
    configure_logging("DEBUG", "json")
    created: list[FakeSMTP] = []
    refused = smtplib.SMTPAuthenticationError(535, f"5.7.8 bad credentials for {PASSWORD}".encode())
    channel = EmailChannel(_email_settings(), smtp_factory=_smtp_factory(created, fail=refused))

    with capture_logs() as logs:
        with pytest.raises(ChannelError, match="^email delivery failed: SMTPAuthenticationError: ") as err:
            channel.send(MESSAGE)

    assert PASSWORD not in str(err.value) and "***" in str(err.value)
    assert not err.value.retryable
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert PASSWORD not in repr(logs)
    assert created[0].calls[-1] == "quit"  # the connection is closed on the way out


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (ConnectionRefusedError(111, "connection refused"), True),
        (TimeoutError("timed out"), True),
        (smtplib.SMTPServerDisconnected("connection lost"), True),
        (smtplib.SMTPConnectError(421, b"service not available"), True),
        (smtplib.SMTPResponseException(451, b"try again later"), True),
        (smtplib.SMTPResponseException(550, b"mailbox unavailable"), False),
        (smtplib.SMTPRecipientsRefused({"james@example.com": (550, b"no such user")}), False),
    ],
)
def test_email_failures_say_whether_a_retry_could_help(error: Exception, retryable: bool) -> None:
    created: list[FakeSMTP] = []
    network = not isinstance(error, smtplib.SMTPException)
    factory = _smtp_factory(created, connect_error=error) if network else _smtp_factory(created, fail=error)
    with pytest.raises(ChannelError, match=f"email delivery failed: {type(error).__name__}") as err:
        EmailChannel(_email_settings(), smtp_factory=factory).send(MESSAGE)
    assert err.value.retryable is retryable


def test_email_channel_logs_the_delivery_without_addresses() -> None:
    configure_logging("DEBUG", "json")
    created: list[FakeSMTP] = []
    with capture_logs() as logs:
        EmailChannel(_email_settings(), smtp_factory=_smtp_factory(created)).send(MESSAGE)
    (event,) = [e for e in logs if e["event"] == "digest_email_sent"]
    assert event["recipients"] == 2 and event["host"] == "smtp.example.com"
    assert "james" not in repr(logs) and PASSWORD not in repr(logs)


def _teams(handler: Any, url: str = WEBHOOK) -> tuple[TeamsChannel, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return TeamsChannel(url, http=httpx.Client(transport=httpx.MockTransport(transport))), seen


def test_teams_channel_posts_an_adaptive_card() -> None:
    channel, seen = _teams(lambda request: httpx.Response(202, text="1"))
    assert channel.name == "teams"

    assert channel.send(MESSAGE) == "202"

    (request,) = seen
    assert request.method == "POST" and str(request.url) == WEBHOOK
    assert request.headers["content-type"] == "application/json"
    payload = json.loads(request.content)
    assert payload["type"] == "message"
    (attachment,) = payload["attachments"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive" and attachment["contentUrl"] is None
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert card["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
    title, *sections = card["body"]
    assert title == {"type": "TextBlock", "size": "Large", "weight": "Bolder", "wrap": True, "text": MESSAGE.subject}
    assert [block["text"] for block in sections] == [
        "Since 3 Nov 2026: 1 new breach, 0 new warnings, 0 resolved.",
        "New breaches (1)\nAIL-1 · Skipped a stage · To Do → Done skipped development\n"
        "  Fix login redirect\n  https://fake.atlassian.net/browse/AIL-1",
    ]
    assert all(block["type"] == "TextBlock" and block["wrap"] for block in sections)
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "AIL-1", "url": "https://fake.atlassian.net/browse/AIL-1"}
    ]


def test_teams_card_treats_the_text_as_data() -> None:
    hostile = Message(
        subject="Jira oversight — AIL — 4 Nov 2026",
        text="needs code_review and test_signoff\n\n*bold* [click me](https://evil.example) `x`\\",
    )
    card = build_card(hostile)["attachments"][0]["content"]
    assert [block["text"] for block in card["body"][1:]] == [
        "needs code\\_review and test\\_signoff",
        "\\*bold\\* \\[click me\\](https://evil.example) \\`x\\`\\\\",
    ]
    assert "actions" not in card
    assert markdown_escape("a_b*c[d]e`f\\g") == "a\\_b\\*c\\[d\\]e\\`f\\\\g"


def test_teams_card_skips_blank_sections_and_caps_its_size() -> None:
    links = [(f"AIL-{n}", f"https://x/browse/AIL-{n}") for n in range(10)]
    long = "x" * (MAX_CARD_TEXT_CHARS // 2 + 1)
    message = Message(subject="s", text=f"a\n\n\n\n{long}\n\n{long}\n\nlast", links=links)

    card = build_card(message)["attachments"][0]["content"]

    texts = [block["text"] for block in card["body"]]
    assert texts[:3] == ["s", "a", long]
    assert texts[3] == "… the rest did not fit in this card." and len(texts) == 4
    assert card["body"][3]["isSubtle"] is True
    assert len(card["actions"]) == MAX_CARD_ACTIONS
    assert [a["title"] for a in card["actions"]] == [f"AIL-{n}" for n in range(MAX_CARD_ACTIONS)]


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (502, True), (503, True), (400, False), (401, False), (404, False), (302, False)],
)
def test_teams_failures_map_status_to_retryable_without_the_url(status: int, retryable: bool) -> None:
    configure_logging("DEBUG", "json")
    channel, _seen = _teams(lambda request: httpx.Response(status, text=f"rejected  {WEBHOOK}  \n try later"))

    with capture_logs() as logs:
        with pytest.raises(ChannelError, match=f"^Teams webhook returned HTTP {status}: rejected \\*\\*\\* try later$") as err:
            channel.send(MESSAGE)

    assert err.value.retryable is retryable
    assert "TOPSECRET" not in str(err.value) and "logic.azure.com" not in str(err.value)
    assert "TOPSECRET" not in repr(logs)


def test_teams_transport_errors_are_retryable_and_keep_the_url_private() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    channel, _seen = _teams(explode)
    with pytest.raises(ChannelError, match="^Teams webhook request failed: ConnectError: cannot reach \\*\\*\\*$") as err:
        channel.send(MESSAGE)
    assert err.value.retryable
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_teams_channel_refuses_a_missing_or_insecure_url() -> None:
    with pytest.raises(ConfigError, match="needs TEAMS_WEBHOOK_URL"):
        TeamsChannel("   ")
    with pytest.raises(ConfigError, match="must start with https://") as err:
        TeamsChannel("http://prod-00.westeurope.logic.azure.com/workflows/abc123?sig=TOPSECRET")
    assert "TOPSECRET" not in str(err.value)


def test_teams_channel_makes_its_own_client_when_none_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.Client
    made: list[dict[str, Any]] = []
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="1")

    def fake_client(**kwargs: Any) -> httpx.Client:
        made.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    assert TeamsChannel(WEBHOOK).send(MESSAGE) == "200"
    assert len(seen) == 1 and str(seen[0].url) == WEBHOOK
    (kwargs,) = made
    assert isinstance(kwargs["timeout"], httpx.Timeout) and kwargs["timeout"].connect == 5.0


def test_get_channel_picks_by_settings(tmp_path: Path) -> None:
    file_channel = get_channel(Settings(digest_channel="file", reports_dir=tmp_path / "reports"), project="AIL", date="2026-11-04")
    assert isinstance(file_channel, FileChannel)
    assert file_channel.path == tmp_path / "reports" / "digests" / "AIL_2026-11-04.md"

    email = get_channel(_email_settings(digest_channel="email"), project="AIL", date="2026-11-04")
    assert isinstance(email, EmailChannel)

    teams = get_channel(Settings(digest_channel="teams", teams_webhook_url=WEBHOOK), project="AIL", date="2026-11-04")
    assert isinstance(teams, TeamsChannel)

    with pytest.raises(ConfigError, match="TEAMS_WEBHOOK_URL"):
        get_channel(Settings(digest_channel="teams", teams_webhook_url=""), project="AIL", date="2026-11-04")
    with pytest.raises(ConfigError, match="SMTP_HOST, SMTP_FROM, DIGEST_TO"):
        get_channel(Settings(digest_channel="email", smtp_host="", smtp_from="", digest_to=""), project="AIL", date="2026-11-04")


def test_the_default_channel_needs_no_setup(tmp_path: Path) -> None:
    settings = Settings(reports_dir=tmp_path / "reports")
    assert settings.digest_channel == "file"
    channel = get_channel(settings, project="DEMO", date="2026-11-04")
    ref = channel.send(Message(subject="Jira oversight — DEMO — 4 Nov 2026", text="Nothing to report: no open findings."))
    assert Path(ref).read_text(encoding="utf-8").endswith("Nothing to report: no open findings.\n")
