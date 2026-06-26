"""W1b (DB-backed): login lockout. DB-backed shared state (not
per-process): repeated failures lock the account; a correct password
is still refused while locked; success after expiry resets counters.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.errors import AuthError, LockedError
from mycelium_core.models.user import User
from mycelium_core.services import auth as A

_PW = "pw-strong-123"


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_lockout_then_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "login_max_failures", 2)
    email = _email()
    async with admin_session() as db:
        await A.signup(db, email=email, password=_PW, org_name="P")

    # Two bad attempts: the second one trips the lock.
    for _ in range(2):
        async with admin_session() as db:
            with pytest.raises(AuthError):
                await A.login(db, email=email, password="wrong-pass-xx")

    # Even the correct password is now refused (423), not 401.
    async with admin_session() as db:
        with pytest.raises(LockedError):
            await A.login(db, email=email, password=_PW)

    # Simulate the lockout window expiring.
    async with admin_session() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        user.locked_until = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)

    # Correct password now works and resets the counters.
    async with admin_session() as db:
        assert await A.login(db, email=email, password=_PW)
    async with admin_session() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        assert user.failed_login_count == 0
        assert user.locked_until is None
