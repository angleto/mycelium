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

from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.services import text_patch


def text_block_headers(*, version: int, body: str) -> dict[str, str]:
    """The base-gate contract emitted by every raw-text download
    (``GET .../body/raw`` and friends): ``X-Version`` plus ``X-Body-SHA256``
    (the hex sha256 of the exact UTF-8 body via the shared
    :func:`text_patch.body_sha256`). The client passes both back as
    ``expected_version`` + ``base_sha256`` to the patch route, which hashes
    the LIVE body the same way -- so client and server compare digests of
    byte-identical input. ``X-Content-Type-Options: nosniff`` mirrors the
    attachment-download hardening."""
    return {
        "X-Version": str(version),
        "X-Body-SHA256": text_patch.body_sha256(body),
        "X-Content-Type-Options": "nosniff",
    }


async def read_patch_payload(request: Request) -> str:
    """Read a POSTed unified diff off the request body, capped at
    ``note_patch_max_bytes`` (a full-replace diff carries both the old and
    new body, so it can exceed the body cap). Streamed + UTF-8 guarded like
    :func:`read_capped_text`."""
    return await read_capped_text(request, max_bytes=get_settings().note_patch_max_bytes)


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
