"""System mailer SMTP transport (W1b; ADR-0024).

Mirrors test_migrate_attachments' config/factory style: explicit
``Settings(...)`` kwargs (which override env, so an ambient
MYCELIUM_SMTP_* dev export cannot make these non-deterministic), a
fail-closed validator assertion, and a fake-driven unit test of the
real transport with **no network** (``smtplib.SMTP`` monkeypatched).

The default LogMailer path is exercised by the whole rest of the
suite; here we only assert the new transport + selection + validator.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from mycelium_core.config import Settings
from mycelium_core.services.mailer import (
    LogMailer,
    OutboundEmail,
    SmtpMailer,
    build_system_mailer,
)

_JWT = "x" * 40
_FERNET = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def _smtp_settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "jwt_secret": _JWT,
        "secret_key": _FERNET,
        "smtp_host": "smtp.tem.scw.cloud",
        "smtp_port": 587,
        "smtp_username": "tem-user",
        "smtp_password": "tem-pass",
        "smtp_from": "Mycelium <no-reply@mycelium.xeno.garden>",
        "smtp_starttls": True,
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_build_returns_smtp_when_configured() -> None:
    m = build_system_mailer(_smtp_settings())
    assert isinstance(m, SmtpMailer)


def test_build_returns_logmailer_when_unset() -> None:
    # Explicit empty host/from overrides any ambient MYCELIUM_SMTP_* env.
    s = Settings(jwt_secret=_JWT, secret_key=_FERNET, smtp_host="", smtp_from="")
    assert s.smtp_configured is False
    assert isinstance(build_system_mailer(s), LogMailer)


def test_validator_fails_closed_when_host_without_from() -> None:
    with pytest.raises(ValueError, match="MYCELIUM_SMTP_FROM is required"):
        Settings(
            jwt_secret=_JWT,
            secret_key=_FERNET,
            smtp_host="smtp.tem.scw.cloud",
            smtp_from="",  # host set, from missing -> rejected at startup
        )


def test_validator_allows_empty_credentials_for_relay() -> None:
    # An unauthenticated relay (no username/password) is valid as long
    # as host+from are present; the validator must not require creds.
    s = _smtp_settings(smtp_username="", smtp_password="")
    assert s.smtp_configured is True
    assert isinstance(build_system_mailer(s), SmtpMailer)


class _FakeSMTP:
    """Records the SMTP conversation without any socket. Mirrors the
    smtplib.SMTP context-manager surface used by SmtpMailer._send_sync."""

    last: _FakeSMTP | None = None

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.sent: EmailMessage | None = None
        _FakeSMTP.last = self

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, msg: EmailMessage) -> None:
        self.sent = msg


async def test_smtp_send_uses_starttls_login_and_sendmessage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mycelium_core.services.mailer.smtplib.SMTP", _FakeSMTP)
    mailer = SmtpMailer(
        host="smtp.tem.scw.cloud",
        port=587,
        username="tem-user",
        password="tem-pass",
        sender="Mycelium <no-reply@mycelium.xeno.garden>",
        starttls=True,
    )

    # Goes through asyncio.to_thread (no network: SMTP is the fake).
    await mailer.send(
        OutboundEmail(to="user@example.test", subject="Reset", body="link: https://x/r?token=t")
    )

    fake = _FakeSMTP.last
    assert fake is not None
    assert (fake.host, fake.port) == ("smtp.tem.scw.cloud", 587)
    assert fake.starttls_called is True
    assert fake.login_args == ("tem-user", "tem-pass")
    assert fake.sent is not None
    assert fake.sent["From"] == "Mycelium <no-reply@mycelium.xeno.garden>"
    assert fake.sent["To"] == "user@example.test"
    assert fake.sent["Subject"] == "Reset"
    assert "https://x/r?token=t" in fake.sent.get_content()


async def test_smtp_send_skips_login_without_username_and_starttls_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mycelium_core.services.mailer.smtplib.SMTP", _FakeSMTP)
    mailer = SmtpMailer(
        host="relay.internal",
        port=25,
        username="",  # unauthenticated relay
        password="",
        sender="ops@mycelium.xeno.garden",
        starttls=False,  # plain relay, no STARTTLS
    )

    await mailer.send(OutboundEmail(to="a@b.test", subject="S", body="B"))

    fake = _FakeSMTP.last
    assert fake is not None
    assert fake.starttls_called is False
    assert fake.login_args is None  # login() never issued
    assert fake.sent is not None
    assert fake.sent["From"] == "ops@mycelium.xeno.garden"
