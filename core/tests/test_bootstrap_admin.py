"""Admin bootstrap: idempotent, refuses weak passwords, and actually
sets the admin capability (the whole point of the bootstrap)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from flow_core.bootstrap_admin import _check_password, ensure_admin
from flow_core.db import admin_session
from flow_core.models.user import User
from flow_core.services.auth import signup


def test_weak_password_rejected() -> None:
    for pw in ("short", "alllowercase1", "NOLOWER123", "aaaaaaaaaaaa1A"):
        with pytest.raises(SystemExit):
            _check_password(pw)


def test_strong_password_ok() -> None:
    _check_password("Str0ng-Passw0rd!")


async def _is_admin(email: str) -> bool:
    async with admin_session() as s:
        u = (await s.execute(select(User).where(User.email == email))).scalar_one()
        return u.is_admin


async def test_ensure_admin_idempotent_and_sets_flag() -> None:
    email = f"admin_{uuid.uuid4().hex[:8]}@example.test"
    first = await ensure_admin(email, "Str0ng-Passw0rd!")
    assert "created" in first
    assert await _is_admin(email) is True
    again = await ensure_admin(email, "Str0ng-Passw0rd!")
    assert "already exists" in again
    assert await _is_admin(email) is True


async def test_ensure_admin_promotes_existing_normal_user() -> None:
    email = f"user_{uuid.uuid4().hex[:8]}@example.test"
    async with admin_session() as s:
        await signup(s, email=email, password="pw-strong-123", org_name="Personal")
    assert await _is_admin(email) is False
    msg = await ensure_admin(email, "unused-existing-user")
    assert "promoted" in msg
    assert await _is_admin(email) is True
