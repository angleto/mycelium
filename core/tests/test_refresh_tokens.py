"""Rotating refresh tokens with reuse detection.

Covers ``services.auth.refresh_session`` and
``revoke_refresh_family``: login mints a pair, refresh rotates with
the prior row marked ``used_at``, a replay revokes the whole family
(theft signal), and logout-style family revocation kills both the
parent and its successor.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from flow_core.db import admin_session
from flow_core.errors import AuthError
from flow_core.models.auth_tokens import RefreshToken
from flow_core.services.auth import (
    login,
    refresh_session,
    revoke_refresh_family,
    signup,
)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup_login() -> tuple[uuid.UUID, str]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="RT")
    async with admin_session() as s:
        pair = await login(s, email=(await _email_of(r.user_id)), password="pw-strong-123")
    return r.user_id, pair.refresh_token


async def _email_of(user_id: uuid.UUID) -> str:
    from flow_core.models.user import User

    async with admin_session() as s:
        u = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
    return u.email


async def test_login_returns_refresh_and_persists_hash() -> None:
    user_id, raw = await _signup_login()
    assert raw.startswith("flow_rt_")
    async with admin_session() as s:
        rows = (
            (await s.execute(select(RefreshToken).where(RefreshToken.user_id == user_id)))
            .scalars()
            .all()
        )
    # signup itself mints one family, login mints another, hence 2 here.
    assert len(rows) == 2
    # Plaintext is NOT persisted: row only has the SHA-256 hash.
    assert all(r.token_hash != raw for r in rows)
    assert all(len(r.token_hash) == 64 for r in rows)


async def test_refresh_rotates_and_marks_parent_used() -> None:
    user_id, raw = await _signup_login()
    async with admin_session() as s:
        new_pair = await refresh_session(s, raw_refresh=raw)
    assert new_pair.refresh_token != raw
    assert new_pair.access_token  # access JWT is fresh

    async with admin_session() as s:
        rows = (
            (
                await s.execute(
                    select(RefreshToken)
                    .where(RefreshToken.user_id == user_id)
                    .order_by(RefreshToken.created_at)
                )
            )
            .scalars()
            .all()
        )
    # The login row should now be used and point at its successor; the
    # successor exists in the same family.
    family_ids = {r.family_id for r in rows}
    used = [r for r in rows if r.used_at is not None]
    assert len(used) == 1
    parent = used[0]
    assert parent.replaced_by_id is not None
    assert parent.family_id in family_ids


async def test_replay_revokes_whole_family() -> None:
    user_id, raw = await _signup_login()
    async with admin_session() as s:
        await refresh_session(s, raw_refresh=raw)
    # Replay the original (now-used) refresh: must fail AND revoke the
    # whole family so the legitimate successor also stops working.
    with pytest.raises(AuthError):
        async with admin_session() as s:
            await refresh_session(s, raw_refresh=raw)

    async with admin_session() as s:
        rows = (
            (await s.execute(select(RefreshToken).where(RefreshToken.user_id == user_id)))
            .scalars()
            .all()
        )
    families_used_for_login = {r.family_id for r in rows if r.used_at is not None}
    # Every row inside that family is now revoked.
    for r in rows:
        if r.family_id in families_used_for_login:
            assert r.revoked_at is not None


async def test_revoke_family_kills_successor() -> None:
    _user_id, raw = await _signup_login()
    async with admin_session() as s:
        new_pair = await refresh_session(s, raw_refresh=raw)
    async with admin_session() as s:
        await revoke_refresh_family(s, raw_refresh=new_pair.refresh_token)
    with pytest.raises(AuthError):
        async with admin_session() as s:
            await refresh_session(s, raw_refresh=new_pair.refresh_token)


async def test_expired_refresh_rejected() -> None:
    import hashlib

    _user_id, raw = await _signup_login()
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    async with admin_session() as s:
        row = (
            await s.execute(select(RefreshToken).where(RefreshToken.token_hash == h))
        ).scalar_one()
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await s.flush()
    with pytest.raises(AuthError):
        async with admin_session() as s:
            await refresh_session(s, raw_refresh=raw)


async def test_unknown_refresh_rejected_silently_on_logout() -> None:
    # Logout-style revoke on an unknown token must NOT raise (no oracle).
    async with admin_session() as s:
        await revoke_refresh_family(s, raw_refresh="flow_rt_nope-nope-nope")
