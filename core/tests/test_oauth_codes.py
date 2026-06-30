"""Unit tests for ``services.oauth_codes``: mint + consume of the
MCP OAuth shim's PKCE-bound authorization codes.

Single-use, TTL = 10 min, lazy-GC on consume. The codes are NOT
RLS-scoped (they are bound to a ``client_id`` = AI assistant uuid,
not to an org) and the shim hits them via an admin_session;
storage is intentionally plain Postgres, no tenant guards.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session
from mycelium_core.models.oauth_code import OAuthCode
from mycelium_core.services import oauth_codes as svc


def _challenge() -> str:
    """Fixed PKCE challenge; ``consume`` never verifies S256 itself
    (that lives in the shim's /token endpoint)."""
    return "abcdef0123456789abcdef0123456789abcdef0_"


@pytest.mark.asyncio
async def test_mint_persists_a_row_and_returns_short_code() -> None:
    async with admin_session() as s:
        code = await svc.mint(
            s,
            client_id="client-A",
            redirect_uri="https://claude.ai/cb",
            code_challenge=_challenge(),
            code_challenge_method="S256",
        )
    # Returned code is a non-empty url-safe token, <= 64 chars (the
    # column ceiling).
    assert code
    assert len(code) <= 64
    async with admin_session() as s:
        row = (
            await s.execute(select(OAuthCode).where(OAuthCode.code == code))
        ).scalar_one_or_none()
        assert row is not None
        assert row.client_id == "client-A"
        assert row.redirect_uri == "https://claude.ai/cb"
        assert row.code_challenge == _challenge()
        assert row.code_challenge_method == "S256"
        assert row.expires_at > dt.datetime.now(dt.UTC)


@pytest.mark.asyncio
async def test_mint_two_codes_have_different_values() -> None:
    # ``mint`` calls session.commit() internally; back-to-back calls
    # need separate session contexts.
    async with admin_session() as s:
        a = await svc.mint(
            s,
            client_id="X",
            redirect_uri="https://claude.ai/cb",
            code_challenge=_challenge(),
            code_challenge_method="S256",
        )
    async with admin_session() as s:
        b = await svc.mint(
            s,
            client_id="X",
            redirect_uri="https://claude.ai/cb",
            code_challenge=_challenge(),
            code_challenge_method="S256",
        )
    assert a != b


@pytest.mark.asyncio
async def test_consume_returns_row_then_deletes_it() -> None:
    async with admin_session() as s:
        code = await svc.mint(
            s,
            client_id="client-B",
            redirect_uri="https://claude.ai/cb",
            code_challenge=_challenge(),
            code_challenge_method="S256",
        )
    async with admin_session() as s:
        row = await svc.consume(s, code=code)
        assert row is not None
        assert row.client_id == "client-B"
    # Row gone after consume.
    async with admin_session() as s:
        again = (
            await s.execute(select(OAuthCode).where(OAuthCode.code == code))
        ).scalar_one_or_none()
        assert again is None


@pytest.mark.asyncio
async def test_consume_unknown_code_returns_none() -> None:
    async with admin_session() as s:
        row = await svc.consume(s, code="does-not-exist")
    assert row is None


@pytest.mark.asyncio
async def test_consume_is_single_use() -> None:
    async with admin_session() as s:
        code = await svc.mint(
            s,
            client_id="C",
            redirect_uri="https://claude.ai/cb",
            code_challenge=_challenge(),
            code_challenge_method="S256",
        )
    async with admin_session() as s:
        first = await svc.consume(s, code=code)
        assert first is not None
    async with admin_session() as s:
        second = await svc.consume(s, code=code)
        assert second is None


@pytest.mark.asyncio
async def test_consume_expired_code_returns_none() -> None:
    # Insert a pre-expired row directly (the production role only has
    # SELECT/INSERT/DELETE on oauth_codes by design — no UPDATE — so
    # we can't backdate via UPDATE; INSERT-with-past-expiry is
    # equivalent and exercises the same code path in ``consume``).
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    code_value = "fixed-test-expired-code-abc"
    async with admin_session() as s:
        s.add(
            OAuthCode(
                code=code_value,
                client_id="D",
                redirect_uri="https://claude.ai/cb",
                code_challenge=_challenge(),
                code_challenge_method="S256",
                expires_at=past,
            )
        )
        await s.commit()
    async with admin_session() as s:
        row = await svc.consume(s, code=code_value)
        assert row is None
