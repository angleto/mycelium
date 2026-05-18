"""Admin bootstrap: idempotent + refuses weak passwords."""

from __future__ import annotations

import uuid

import pytest

from flow_core.bootstrap_admin import _check_password, ensure_admin


def test_weak_password_rejected() -> None:
    for pw in ("short", "alllowercase1", "NOLOWER123", "aaaaaaaaaaaa1A"):
        with pytest.raises(SystemExit):
            _check_password(pw)


def test_strong_password_ok() -> None:
    _check_password("Str0ng-Passw0rd!")


async def test_ensure_admin_idempotent() -> None:
    email = f"admin_{uuid.uuid4().hex[:8]}@example.test"
    first = await ensure_admin(email, "Str0ng-Passw0rd!")
    assert "created" in first
    again = await ensure_admin(email, "Str0ng-Passw0rd!")
    assert "already exists" in again
