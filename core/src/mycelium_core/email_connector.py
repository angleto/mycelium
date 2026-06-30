"""Email connector abstraction (docs/adr/0023, FR-7).

A single Protocol with neutral DTOs; a DB-driven factory selects the
concrete connector by provider. The concrete IMAP/SMTP connector
covers generic IMAP, Gmail (XOAUTH2) and Proton Bridge (local IMAP);
it is the external network boundary and is not exercised in CI. The
sync/send service injects the connector, so tests use an in-memory
one implementing this Protocol (the same legitimate seam as the LLM
provider, ADR-0012).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email
import imaplib
import smtplib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage as PyEmailMessage
from email.utils import parsedate_to_datetime
from typing import Protocol, runtime_checkable

from mycelium_core.models.email import EmailAccount, EmailProvider


@dataclass(frozen=True)
class FetchedMessage:
    provider_message_id: str
    from_addr: str
    to_addrs: list[str]
    received_at: dt.datetime
    thread_id: str | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    subject: str | None = None
    body_text: str | None = None
    raw_size: int | None = None
    # Automated / list / bulk mail, decided from RFC headers at fetch time
    # (the raw message is only available here). The memory-ingest hygiene
    # filter drops these upstream; everything else flows through.
    is_bulk: bool = False


@dataclass(frozen=True)
class OutgoingMessage:
    to_addrs: list[str]
    subject: str
    body_text: str
    in_reply_to: str | None = None
    references: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class EmailConnector(Protocol):
    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]: ...

    async def send(self, message: OutgoingMessage) -> str: ...


def _xoauth2(user: str, token: str) -> str:
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


def _addr_list(value: str) -> list[str]:
    return [a.strip() for a in value.split(",") if a.strip()]


def _is_bulk(msg: email.message.Message) -> bool:
    """True for automated / mailing-list / bulk mail, from the standard
    headers: ``List-Id`` (any list), ``Precedence: bulk|list|junk``, or
    ``Auto-Submitted`` other than ``no`` (RFC 3834). The one upstream
    ingest filter: it removes the bulk of the noise (and the embedding
    cost) before paying for it; content hygiene stays downstream."""
    if msg.get("List-Id"):
        return True
    if (msg.get("Precedence") or "").strip().lower() in {"bulk", "list", "junk"}:
        return True
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    return bool(auto) and auto != "no"


def _body_text(msg: email.message.Message) -> str | None:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", "replace")
        return None
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", "replace")
    return None


class ImapSmtpConnector:
    """Generic IMAP/SMTP, Gmail (XOAUTH2) and Proton Bridge. Blocking
    stdlib calls are offloaded to a thread. Not exercised in CI."""

    def __init__(self, account: EmailAccount, secret: str) -> None:
        self._a = account
        self._secret = secret

    def _imap_login(self, imap: imaplib.IMAP4) -> None:
        if self._a.provider is EmailProvider.gmail:
            imap.authenticate(
                "XOAUTH2",
                lambda _: _xoauth2(self._a.email_address, self._secret).encode(),
            )
        else:
            imap.login(self._a.email_address, self._secret)

    def _fetch_sync(self, limit: int) -> list[FetchedMessage]:
        host = self._a.imap_host or ""
        port = self._a.imap_port or 993
        out: list[FetchedMessage] = []
        with imaplib.IMAP4_SSL(host, port) as imap:
            self._imap_login(imap)
            imap.select("INBOX")
            # Idempotency key = "UIDVALIDITY:UID", never sequence numbers:
            # a sequence number shifts on any expunge (e.g. archiving a
            # message), which both re-ingests old mail under fresh ids and
            # silently *drops* new mail whose shifted number is already
            # stored. UIDs are stable within a UIDVALIDITY epoch; a rare
            # mailbox rebuild bumps UIDVALIDITY and re-ingests under the
            # new prefix (duplicates over loss).
            uv = imap.response("UIDVALIDITY")[1]
            uv0 = uv[0] if uv else None
            uidvalidity = uv0.decode() if isinstance(uv0, bytes) else "0"
            _typ, data = imap.uid("search", "ALL")
            listing = data[0] if data else None
            uids = listing.split()[-limit:] if isinstance(listing, bytes) else []
            for raw_uid in uids:
                uid = raw_uid.decode()
                _t, raw = imap.uid("fetch", uid, "(RFC822)")
                if not raw or not isinstance(raw[0], tuple):
                    continue
                msg = email.message_from_bytes(raw[0][1])
                received = msg.get("Date")
                try:
                    received_at = (
                        parsedate_to_datetime(received) if received else dt.datetime.now(tz=dt.UTC)
                    )
                except (TypeError, ValueError):
                    received_at = dt.datetime.now(tz=dt.UTC)
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=dt.UTC)
                out.append(
                    FetchedMessage(
                        provider_message_id=f"{uidvalidity}:{uid}",
                        message_id=msg.get("Message-ID"),
                        in_reply_to=msg.get("In-Reply-To"),
                        thread_id=msg.get("In-Reply-To") or msg.get("Message-ID"),
                        from_addr=str(msg.get("From", "")),
                        to_addrs=_addr_list(str(msg.get("To", ""))),
                        subject=msg.get("Subject"),
                        body_text=_body_text(msg),
                        received_at=received_at,
                        raw_size=len(raw[0][1]),
                        is_bulk=_is_bulk(msg),
                    )
                )
        return out

    def _send_sync(self, message: OutgoingMessage) -> str:
        mime = PyEmailMessage()
        mime["From"] = self._a.email_address
        mime["To"] = ", ".join(message.to_addrs)
        mime["Subject"] = message.subject
        if message.in_reply_to:
            mime["In-Reply-To"] = message.in_reply_to
        if message.references:
            mime["References"] = message.references
        for k, v in message.headers.items():
            mime[k] = v
        mime.set_content(message.body_text)
        host = self._a.smtp_host or ""
        port = self._a.smtp_port or 587
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            if self._a.provider is EmailProvider.gmail:
                smtp.auth(
                    "XOAUTH2",
                    lambda challenge=None: _xoauth2(self._a.email_address, self._secret),
                )
            else:
                smtp.login(self._a.email_address, self._secret)
            smtp.send_message(mime)
        return str(mime["Message-ID"] or "")

    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]:
        return await asyncio.to_thread(self._fetch_sync, limit)

    async def send(self, message: OutgoingMessage) -> str:
        return await asyncio.to_thread(self._send_sync, message)


_FactoryFn = Callable[[EmailAccount, str], EmailConnector]
_override: _FactoryFn | None = None


def set_connector_override(fn: _FactoryFn | None) -> None:
    """Test seam: replace the network factory with an in-memory
    connector. Prod leaves this None and uses the real IMAP/SMTP
    connector. Analogous to the conftest DB-engine reset; never set in
    production code."""
    global _override
    _override = fn


def connector_for(account: EmailAccount, secret: str) -> EmailConnector:
    """DB-driven factory. All current providers speak IMAP/SMTP; they
    differ only in authentication, handled inside the connector."""
    if _override is not None:
        return _override(account, secret)
    if account.provider in (
        EmailProvider.imap_generic,
        EmailProvider.proton_bridge,
        EmailProvider.gmail,
    ):
        return ImapSmtpConnector(account, secret)
    raise ValueError(f"unsupported provider: {account.provider}")  # pragma: no cover
