"""Token-free inline-body writes: read a streamed text payload off the
HTTP body instead of a tool argument.

The attachment gateway streams raw bytes into object storage (zero
tokens); this is the inline-text analogue. A large markdown body (a note
part, a comment, a suggestion's proposed text) rides the request body as
``--data-binary @file`` and lands straight in the Postgres TEXT column,
so the write costs no LLM tokens and needs no S3 backend. The MCP
``*_instructions`` tools hand back the matching ``curl``.
"""

from __future__ import annotations

from fastapi import Request

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode


async def read_capped_text(request: Request, *, max_bytes: int) -> str:
    """Read the raw request body as UTF-8 text, guarding the size.

    The body is consumed chunk by chunk and the running total is checked
    after each chunk, so an oversize payload is rejected without ever
    fully buffering it (bounded by ``max_bytes`` + one chunk). A body
    that is not valid UTF-8 is a clean ``body.invalid_encoding`` domain
    error rather than a 500. Mirrors ``attachments.read_capped`` for the
    inline-body path."""
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    try:
        return buf.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DomainError(MessageCode.BODY_INVALID_ENCODING) from exc
