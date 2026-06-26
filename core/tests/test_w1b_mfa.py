"""W1b (DB-backed): TOTP MFA enrolment, gated login, backup codes,
disable. Ported from bitvision_phoenix; adapted to Mycelium's services.
"""

from __future__ import annotations

import uuid

import pyotp
import pytest

from mycelium_core.db import admin_session
from mycelium_core.errors import AuthError, ConflictError
from mycelium_core.services import auth as A
from mycelium_core.services import mfa as M


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_totp_enrolment_gated_login_backup_and_disable() -> None:
    email = _email()
    async with admin_session() as db:
        r = await A.signup(db, email=email, password="pw-strong-123", org_name="P")
    uid = r.user_id

    # setup -> pending secret
    async with admin_session() as db:
        s = M.setup(user=await A.get_user(db, user_id=uid))
        secret = s.secret
    async with admin_session() as db:
        assert M.status(await A.get_user(db, user_id=uid)).pending is True

    # activate with a live TOTP code -> 10 backup codes
    async with admin_session() as db:
        res = M.activate(
            user=await A.get_user(db, user_id=uid),
            totp_code=pyotp.TOTP(secret).now(),
        )
    assert len(res.backup_codes) == 10
    async with admin_session() as db:
        assert M.status(await A.get_user(db, user_id=uid)).enabled is True

    # re-setup is refused while enabled
    async with admin_session() as db:
        with pytest.raises(ConflictError):
            M.setup(user=await A.get_user(db, user_id=uid))

    # plain login now demands MFA
    async with admin_session() as db:
        with pytest.raises(AuthError):
            await A.login(db, email=email, password="pw-strong-123")

    # login-mfa with a TOTP code works
    async with admin_session() as db:
        assert await A.login_mfa(
            db, email=email, password="pw-strong-123", totp_code=pyotp.TOTP(secret).now()
        )

    # a wrong code is rejected
    async with admin_session() as db:
        with pytest.raises(AuthError):
            await A.login_mfa(db, email=email, password="pw-strong-123", totp_code="000000")

    # a backup code works once and is then consumed
    backup = res.backup_codes[0]
    async with admin_session() as db:
        assert await A.login_mfa(db, email=email, password="pw-strong-123", totp_code=backup)
    async with admin_session() as db:
        assert M.status(await A.get_user(db, user_id=uid)).backup_codes_remaining == 9
        with pytest.raises(AuthError):
            await A.login_mfa(db, email=email, password="pw-strong-123", totp_code=backup)

    # disable requires a valid factor; afterwards plain login works
    async with admin_session() as db:
        M.disable(
            user=await A.get_user(db, user_id=uid),
            code=pyotp.TOTP(secret).now(),
        )
    async with admin_session() as db:
        assert M.status(await A.get_user(db, user_id=uid)).enabled is False
        assert await A.login(db, email=email, password="pw-strong-123")
