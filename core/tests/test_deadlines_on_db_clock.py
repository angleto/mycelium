"""A deadline the database enforces is written on the database's clock.

Each of these is compared with ``now()`` inside a SQL function
(``authenticate_capability_token``, ``authenticate_agent_token``,
``authenticate_issuer_api_key``, ``consume_telegram_link_code``,
``resolve_payment_connector``). Written from the process's clock, the skew
between the two is added to the lifetime: observed as a capability token
living 3600.022 s against a ceiling of 3600.

The tests make that skew large instead of waiting for it: the service
module's clock is moved an hour ahead, and the stored deadline, read
against the database's ``now()``, must still be the lifetime and not the
lifetime plus an hour.
"""

from __future__ import annotations

import datetime
import types
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import (
    agent_tokens,
    capability_tokens,
    issuer_api_keys,
    payment_connectors,
    telegram_link,
)
from mycelium_core.services import invoice as inv
from mycelium_core.services.auth import signup

_SKEW = datetime.timedelta(hours=1)
# Slack for the time the test itself takes between the write and the read.
_SLACK = datetime.timedelta(seconds=30)


class _AheadDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz: datetime.tzinfo | None = None) -> _AheadDatetime:  # type: ignore[override]
        return super().now(tz) + _SKEW  # type: ignore[return-value]


def _clock_ahead(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType) -> None:
    """Move ``module``'s view of the process clock an hour ahead."""
    fake = types.SimpleNamespace(**{k: getattr(datetime, k) for k in dir(datetime)})
    fake.datetime = _AheadDatetime
    monkeypatch.setattr(module, "datetime", fake)


async def _signup() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="DC",
        )
    return r.org_id, r.user_id


async def _issuer(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        p = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="P",
            legal_name="Esempio Servizi SRL",
            vat_number="01234567890",
            address="Via Roma",
            civic_number="1",
            postal_code="00100",
            city="Roma",
            province="RM",
            is_default=True,
        )
        return p.id


async def _remaining(
    org: uuid.UUID, user: uuid.UUID, sql: str, **params: Any
) -> datetime.timedelta:
    async with tenant_session(str(org), str(user)) as s:
        left: datetime.timedelta = (await s.execute(text(sql), params)).scalar_one()
    return left


def _assert_lifetime(left: datetime.timedelta, lifetime: datetime.timedelta) -> None:
    assert lifetime - _SLACK < left <= lifetime


async def test_a_capability_token_lives_at_most_its_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock_ahead(monkeypatch, capability_tokens)
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        res = await capability_tokens.mint(
            s,
            org_id=org,
            actor_id=user,
            action=capability_tokens.ACTION_NOTE_PART_BODY_WRITE,
            resource_kind=capability_tokens.RESOURCE_NOTE_PART,
            resource_id=uuid.uuid4(),
            ttl_seconds=365 * 24 * 3600,
        )
    left = await _remaining(
        org, user, "SELECT expires_at - now() FROM capability_tokens WHERE id = :i", i=res.token_id
    )
    _assert_lifetime(left, datetime.timedelta(seconds=capability_tokens.MAX_TTL_SECONDS))
    # What the caller is told is what the database stored.
    async with tenant_session(str(org), str(user)) as s:
        stored = (
            await s.execute(
                text("SELECT expires_at FROM capability_tokens WHERE id = :i"), {"i": res.token_id}
            )
        ).scalar_one()
    assert res.expires_at == stored


async def test_an_agent_token_expires_on_the_clock_that_checks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock_ahead(monkeypatch, agent_tokens)
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        res = await agent_tokens.mint(s, org_id=org, actor_id=user, name="t", ttl_days=1)
    left = await _remaining(
        org, user, "SELECT expires_at - now() FROM agent_tokens WHERE id = :i", i=res.token.id
    )
    _assert_lifetime(left, datetime.timedelta(days=1))


async def test_a_never_expiring_minter_still_lends_its_authority() -> None:
    """``authenticate_agent_token`` reads a NULL expiry as never expiring;
    ``minter_authority`` read it as expired, so a capability minted by such
    a credential was refused at redemption although its minter was live."""
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        res = await agent_tokens.mint(s, org_id=org, actor_id=user, name="t", ttl_days=None)
        assert res.token.expires_at is None
        authority = await agent_tokens.minter_authority(s, res.token.id)
    assert authority is not None


async def test_an_expired_minter_lends_nothing() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        res = await agent_tokens.mint(s, org_id=org, actor_id=user, name="t", ttl_days=1)
        await s.execute(
            text("UPDATE agent_tokens SET expires_at = now() - interval '1 second' WHERE id = :i"),
            {"i": res.token.id},
        )
        assert await agent_tokens.minter_authority(s, res.token.id) is None


async def test_a_telegram_link_code_expires_on_the_clock_that_consumes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock_ahead(monkeypatch, telegram_link)
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await telegram_link.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
    left = await _remaining(
        org,
        user,
        "SELECT expires_at - now() FROM telegram_link_codes WHERE code = :c",
        c=issued.code,
    )
    _assert_lifetime(left, telegram_link._CODE_TTL)


async def test_an_issuer_key_and_its_grace_window_use_the_database_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock_ahead(monkeypatch, issuer_api_keys)
    org, user = await _signup()
    issuer = await _issuer(org, user)
    async with tenant_session(str(org), str(user)) as s:
        res = await issuer_api_keys.mint(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            name="k",
            permissions=[issuer_api_keys.PERM_READ],
            ttl_days=1,
        )
        key_id = res.key.id
    left = await _remaining(
        org, user, "SELECT expires_at - now() FROM issuer_api_keys WHERE id = :i", i=key_id
    )
    _assert_lifetime(left, datetime.timedelta(days=1))

    async with tenant_session(str(org), str(user)) as s:
        await issuer_api_keys.rotate(s, org_id=org, actor_id=user, key_id=key_id, grace_seconds=600)
    left = await _remaining(
        org,
        user,
        "SELECT previous_secret_expires_at - now() FROM issuer_api_keys WHERE id = :i",
        i=key_id,
    )
    _assert_lifetime(left, datetime.timedelta(seconds=600))


async def test_a_payment_connector_grace_window_uses_the_database_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock_ahead(monkeypatch, payment_connectors)
    org, user = await _signup()
    issuer = await _issuer(org, user)
    grace = datetime.timedelta(
        hours=payment_connectors.get_settings().payment_connector_secret_grace_hours
    )
    async with tenant_session(str(org), str(user)) as s:
        row, _, _ = await payment_connectors.create_connector(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            label=f"native-{uuid.uuid4().hex[:6]}",
            provider="mycelium",
            enabled=True,
            with_api_key=True,
        )
        cid = row.id
        await payment_connectors.rotate_signing_secret(
            s, org_id=org, actor_id=user, connector_id=cid
        )
        await payment_connectors.rotate_api_key(s, org_id=org, actor_id=user, connector_id=cid)
    for sql in (
        "SELECT previous_signing_secret_expires_at - now() FROM payment_connectors WHERE id = :i",
        "SELECT previous_api_key_expires_at - now() FROM payment_connectors WHERE id = :i",
    ):
        _assert_lifetime(await _remaining(org, user, sql, i=cid), grace)
