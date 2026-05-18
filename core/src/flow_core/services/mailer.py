"""System (transactional) mailer seam (W1b; ADR-0024).

Distinct from the F5 email connector (tenant IMAP/SMTP accounts): this
sends *platform* mail (email verification, password reset). Pluggable
Protocol with a logging default so a self-hosted dev install works
without SMTP; tests inject a fake. A real SMTP/provider impl plugs in
the same way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger("flow.mailer")


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


_mailer: SystemMailer = LogMailer()


def get_mailer() -> SystemMailer:
    return _mailer


def set_mailer(mailer: SystemMailer) -> None:
    """Injection seam (tests, or a real SMTP/provider implementation)."""
    global _mailer
    _mailer = mailer
