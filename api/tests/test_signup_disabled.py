"""Public self-service signup can be disabled (single-user prod).

The gate is HTTP-only: with ``allow_signup=False`` the
``POST /auth/signup`` endpoint returns 403 + ``auth.signup_disabled``;
the default (True) still works. The ``signup`` SERVICE is intentionally
NOT gated -- ``test_bootstrap_admin.py`` (which calls the service
directly) stays green regardless of the flag. Settings are overridden
on the cached singleton, the same seam the auth/MFA tests use."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.config import get_settings


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_signup_allowed_by_default() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})
        assert r.status_code == 200, r.text
        assert "token" in r.json()


async def test_signup_disabled_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "allow_signup", False, raising=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "auth.signup_disabled"


async def test_signup_disabled_does_not_block_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bootstrap path uses the service, not the HTTP endpoint, so it
    must keep working even with public signup disabled."""
    monkeypatch.setattr(get_settings(), "allow_signup", False, raising=True)
    from mycelium_core.db import admin_session
    from mycelium_core.services.auth import signup

    email = _email()
    async with admin_session() as s:
        result = await signup(s, email=email, password="pw-strong-123", org_name="Personal")
    assert result.user_id is not None
