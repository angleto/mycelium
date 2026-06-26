"""W1b (DB-backed): email verification, password reset, JWT revocation.

Ported from bitvision_phoenix, adapted to Mycelium's DomainError + i18n.
The SystemMailer seam is replaced by a capturing fake so the one-shot
token (which only ever leaves via email) can be asserted.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

import pytest

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.errors import AuthError, ForbiddenError
from mycelium_core.services import auth as A
from mycelium_core.services.mailer import LogMailer, OutboundEmail, set_mailer


class _FakeMailer:
    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []

    async def send(self, message: OutboundEmail) -> None:
        self.sent.append(message)


def _token(body: str, path: str) -> str:
    m = re.search(rf"/{path}\?token=(\S+)", body)
    assert m is not None
    return m.group(1)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_verify_reset_revoke(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeMailer()
    set_mailer(fake)
    try:
        monkeypatch.setattr(get_settings(), "require_email_verification", True)
        email = _email()

        async with admin_session() as db:
            r = await A.signup(db, email=email, password="pw-strong-123", org_name="P")
        assert r.token is None
        assert r.email_verification_required is True

        # Login is blocked (403) until the email is verified.
        async with admin_session() as db:
            with pytest.raises(ForbiddenError):
                await A.login(db, email=email, password="pw-strong-123")

        raw = _token(fake.sent[-1].body, "verify-email")
        async with admin_session() as db:
            assert await A.verify_email(db, raw_token=raw)
        # One-shot: a used token is rejected.
        async with admin_session() as db:
            with pytest.raises(AuthError):
                await A.verify_email(db, raw_token=raw)
        # Verified -> login works.
        async with admin_session() as db:
            assert await A.login(db, email=email, password="pw-strong-123")

        # Password reset.
        async with admin_session() as db:
            await A.request_password_reset(db, email=email, ip="1.2.3.4")
        rraw = _token(fake.sent[-1].body, "reset-password")
        async with admin_session() as db:
            await A.reset_password(db, raw_token=rraw, new_password="new-strong-456")
        async with admin_session() as db:
            with pytest.raises(AuthError):
                await A.login(db, email=email, password="pw-strong-123")
            assert await A.login(db, email=email, password="new-strong-456")
        # Reset token is single-use.
        async with admin_session() as db:
            with pytest.raises(AuthError):
                await A.reset_password(db, raw_token=rraw, new_password="z-strong-789")

        # JWT revocation by jti, idempotent.
        jti = uuid.uuid4()
        exp = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        async with admin_session() as db:
            await A.assert_token_not_revoked(db, jti=jti)
            await A.revoke_token(
                db, jti=jti, expires_at=exp, subject_id=r.user_id, revoked_by=r.user_id
            )
        async with admin_session() as db:
            with pytest.raises(AuthError):
                await A.assert_token_not_revoked(db, jti=jti)
            await A.revoke_token(
                db, jti=jti, expires_at=exp, subject_id=r.user_id, revoked_by=r.user_id
            )

        # Enumeration-safe: unknown address is silent (no raise).
        async with admin_session() as db:
            await A.request_password_reset(db, email="nobody@example.test")
            await A.resend_verification(db, email="nobody@example.test")
    finally:
        set_mailer(LogMailer())
