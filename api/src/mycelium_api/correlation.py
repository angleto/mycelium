"""One identifier per request, minted here and nowhere else.

An error a person reports is only actionable if the same string is on
their screen and in the server's log. Without one, a support exchange is
"it said something went wrong" against a log with no way to find which
request that was -- and the reflex repair is to put the exception text on
the screen, which leaks schema names and filesystem paths to whoever
asked.

So: every request carries an identifier, every response echoes it, every
error body includes it, and the internal detail of a 500 is logged under
it instead of being sent. The value is for correlation only. It is NEVER
an authorization input, and nothing may branch on it.

Adopted, not just minted. A caller that already has one (the browser
extension mints one per request, so that a timeout -- which has no
response to read -- still resolves in the access log) sends it and we
keep it, so one user action that fans out into several requests is one
thread through the log. That makes the header UNTRUSTED INPUT reaching a
log file, which is why it is validated rather than passed through: an
unbounded value would blow up log lines, and one containing a newline
could forge a log entry. A value that fails the check is replaced, not
rejected -- the request is fine, only the label was bad.

Written as raw ASGI rather than as a ``BaseHTTPMiddleware``: that base
class consumes the response body to re-emit it, which turns the
streaming endpoints (attachment upload/download, the description and
note-part streams) from constant memory into buffered. Wrapping ``send``
touches only the response-start message and leaves every body chunk
alone.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "X-Correlation-Id"
_HEADER_BYTES = HEADER.lower().encode("latin-1")

# Deliberately narrow: what a UUID hex, a ULID or a short random token
# needs, and nothing that can break a log line. No spaces, no control
# characters, bounded length.
_ACCEPTABLE = re.compile(r"\A[A-Za-z0-9._-]{8,64}\Z")


def mint() -> str:
    return uuid.uuid4().hex


def adopt(raw: str | None) -> str:
    """The caller's identifier if it is safe to write into a log, else a
    fresh one."""
    if raw is not None and _ACCEPTABLE.match(raw):
        return raw
    return mint()


def current(request: Request) -> str:
    """The identifier for this request. Falls back to a fresh value so a
    handler reached outside the middleware -- a unit test calling it
    directly -- gets a string rather than an AttributeError."""
    existing = getattr(request.state, "correlation_id", None)
    return existing if isinstance(existing, str) else mint()


def _inbound(scope: Scope) -> str | None:
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers") or ()
    for name, value in headers:
        if name.lower() == _HEADER_BYTES:
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = adopt(_inbound(scope))
        state: dict[str, Any] = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id
        raw = correlation_id.encode("latin-1")

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                # A handler that already set it wins: an error response
                # carries the same value and must not gain a duplicate
                # header, which some clients read as a comma-joined pair.
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != _HEADER_BYTES
                ]
                headers.append((_HEADER_BYTES, raw))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)
