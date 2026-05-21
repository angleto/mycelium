"""OAuth authorization-code mint / consume.

Backs the MCP OAuth 2.1 + PKCE shim in
``mcp/src/flow_mcp/oauth_shim.py`` (ported from bitvision_phoenix
``bvmcp.oauth_shim``). The shim stores the code in Postgres rather
than a per-process dict so multiple backend replicas can hand off
the request between ``/authorize`` and ``/token``.

Codes are single-use, expire in 10 minutes by default, opaquely
base64url-encoded (43 chars from ``secrets.token_urlsafe(32)``).
"""

from __future__ import annotations

import datetime as dt
import secrets

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.oauth_code import OAuthCode

_CODE_TTL_SECONDS = 10 * 60


async def mint(
    session: AsyncSession,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
) -> str:
    """Persist a new code and return it. Caller already validated
    PKCE method (``S256``) and redirect_uri scheme."""
    code = secrets.token_urlsafe(32)[:64]
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=_CODE_TTL_SECONDS)
    session.add(
        OAuthCode(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=expires_at,
        )
    )
    await session.commit()
    return code


async def consume(session: AsyncSession, *, code: str) -> OAuthCode | None:
    """Look up + atomically delete a code. Returns ``None`` for an
    unknown or expired code; the row is deleted either way so a stale
    code cannot be reused after a successful auth attempt."""
    row = (
        await session.execute(select(OAuthCode).where(OAuthCode.code == code))
    ).scalar_one_or_none()
    if row is None:
        return None
    await session.execute(delete(OAuthCode).where(OAuthCode.code == code))
    # Lazy GC: anytime we land here, wipe everything that already
    # expired. Cheap (the index supports it) and avoids a separate
    # cron job. Stays under the per-request scope.
    now = dt.datetime.now(dt.UTC)
    await session.execute(delete(OAuthCode).where(OAuthCode.expires_at < now))
    await session.commit()
    if row.expires_at < now:
        return None
    return row


__all__ = ["consume", "mint"]
