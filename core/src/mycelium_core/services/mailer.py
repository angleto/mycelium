"""System (transactional) mailer seam (W1b; ADR-0024).

Distinct from the F5 email connector (tenant IMAP/SMTP accounts): this
sends *platform* mail (email verification, password reset). Pluggable
Protocol with a logging default so a self-hosted dev install works
without SMTP; tests inject a fake. A real SMTP/provider impl plugs in
the same way.

The default ``_mailer`` is the ``LogMailer``; the real app/worker swap
in the configured transport at startup via ``build_system_mailer`` +
``set_mailer`` (see ``mycelium_api.app`` lifespan and ``mycelium_worker.main``).
The SMTP transport is stdlib-only (``smtplib`` + ``email.message``),
run off the event loop with ``asyncio.to_thread`` because ``smtplib``
is blocking. Production uses Scaleway TEM over STARTTLS on port 587.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mycelium_core.config import Settings

logger = logging.getLogger("mycelium.mailer")


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    to: str
    subject: str
    body: str


class SystemMailer(Protocol):
    async def send(self, message: OutboundEmail) -> None: ...


class LogMailer:
    """Default: log instead of sending. The verification/reset link is
    in the body, so a dev without SMTP can still complete the flow."""

    async def send(self, message: OutboundEmail) -> None:
        logger.info("system-email to=%s subject=%s", message.to, message.subject)


class SmtpMailer:
    """Real transport: stdlib SMTP (Scaleway TEM relay in prod).

    ``smtplib`` is blocking, so the network call runs in a worker thread
    via ``asyncio.to_thread``. STARTTLS is used when ``starttls`` (587 /
    TEM); ``login`` is issued only when a username is configured (an
    unauthenticated relay is valid). Constructed only when SMTP is
    configured (``build_system_mailer``); the existing ``LogMailer``
    path and every test that injects a fake are untouched.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        starttls: bool,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._starttls = starttls

    def _build(self, message: OutboundEmail) -> EmailMessage:
        mime = EmailMessage()
        mime["From"] = self._sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        return mime

    def _send_sync(self, message: OutboundEmail) -> None:  # pragma: no cover - network
        mime = self._build(message)
        with smtplib.SMTP(self._host, self._port) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(mime)

    async def send(self, message: OutboundEmail) -> None:
        await asyncio.to_thread(self._send_sync, message)


def build_system_mailer(settings: Settings) -> SystemMailer:
    """Select the transport from config. SMTP iff host+from are set
    (validated fail-closed in ``config``); otherwise the safe dev/OSS
    default ``LogMailer``. Does not touch the process-global -- the
    caller (startup wiring or a test) decides via ``set_mailer``."""
    if settings.smtp_configured:
        return SmtpMailer(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            sender=settings.smtp_from,
            starttls=settings.smtp_starttls,
        )
    return LogMailer()


_mailer: SystemMailer = LogMailer()


def get_mailer() -> SystemMailer:
    return _mailer


def set_mailer(mailer: SystemMailer) -> None:
    """Injection seam (tests, or a real SMTP/provider implementation)."""
    global _mailer
    _mailer = mailer
