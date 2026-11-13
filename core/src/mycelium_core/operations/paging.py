"""Keyset pages and caller-relative dates, shared by every operation that
lists (ADR-0068).

These were private helpers of the MCP server, so a list read over REST had
no cursor and no date filters. They live here now; the MCP server's
remaining hand-written list tools call the same functions.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.timewindow import resolve_tz, split_due


def encode_cursor(values: list[Any]) -> str:
    """Opaque keyset cursor: base64 of the last row's sort-key values."""
    enc = [
        v.isoformat() if isinstance(v, dt.datetime) else str(v) if isinstance(v, uuid.UUID) else v
        for v in values
    ]
    return base64.urlsafe_b64encode(json.dumps(enc, separators=(",", ":")).encode()).decode()


def decode_cursor(token: str) -> list[Any]:
    """The raw value list of a cursor. A malformed one is the caller's
    error, raised as ``ValueError`` for the caller to turn into its own."""
    try:
        out = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(out, list):
        raise ValueError("invalid cursor")
    return out


def page(
    rows: list[Any], limit: int, key: Callable[[Any], list[Any]]
) -> tuple[list[Any], str | None, bool]:
    """``rows`` fetched as limit+1 -> ``(items, next_cursor, truncated)``.
    ``limit`` <= 0 means no paging."""
    truncated = limit > 0 and len(rows) > limit
    items = rows[:limit] if limit > 0 else rows
    next_cursor = encode_cursor(key(items[-1])) if (truncated and items) else None
    return items, next_cursor, truncated


def invalid(operation: str, detail: str) -> UnprocessableError:
    return UnprocessableError(
        MessageCode.OPERATION_INVALID_ARGUMENT, operation=operation, detail=detail
    )


async def caller_tz(session: AsyncSession, user_id: uuid.UUID) -> dt.tzinfo:
    """The caller's IANA timezone (UTC fallback), which a bare date means."""
    from mycelium_core.models.user import User

    return resolve_tz(await session.scalar(select(User.timezone).where(User.id == user_id)))


def to_instant(raw: str, tz: dt.tzinfo) -> dt.datetime:
    """An ISO instant keeps its instant (a naive one is read in ``tz``); a
    bare date is that day's start in ``tz``."""
    v = split_due(raw)
    if isinstance(v, dt.datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=tz)
    return dt.datetime.combine(v, dt.time(0, 0), tzinfo=tz)


__all__ = ["caller_tz", "decode_cursor", "encode_cursor", "invalid", "page", "to_instant"]
