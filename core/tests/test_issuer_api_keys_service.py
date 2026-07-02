"""Issuer-API-key service (phase 2 of task 19b7e874).

Security + robustness of the credential itself: system-generated secret, keyed
(peppered) hash, two-probe verify with bounded rotation-grace, total revocation,
mandatory-and-capped expiry, throttled + per-secret telemetry, injection safety.
Maps to the design's T01-T07, T10, T20-T21, T23-T24, T28, T33.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import inspect
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from mycelium_core.config import Settings, get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.issuer_api_key import IssuerApiKey
from mycelium_core.services import invoice as inv
from mycelium_core.services import issuer_api_keys as svc
from mycelium_core.services.auth import signup

_UTC = datetime.UTC


async def _org_and_issuer() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="IK",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        p = await inv.create_issuer_profile(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            label="P",
            legal_name="Acme Srl",
            vat_number="01234567890",
            address="Via Roma 1",
            postal_code="00100",
            city="Roma",
            is_default=True,
        )
        issuer_id = p.id
    return r.org_id, r.user_id, issuer_id


async def _mint(
    org: uuid.UUID,
    user: uuid.UUID,
    issuer: uuid.UUID,
    *,
    permissions: list[str] | None = None,
    ttl_days: int | None = None,
) -> tuple[uuid.UUID, str]:
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.mint(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            name="k",
            permissions=permissions if permissions is not None else [svc.PERM_READ],
            ttl_days=ttl_days,
        )
        return res.key.id, res.raw


async def _read(org: uuid.UUID, user: uuid.UUID, key_id: uuid.UUID) -> IssuerApiKey:
    async with tenant_session(str(org), str(user)) as s:
        return (await s.execute(select(IssuerApiKey).where(IssuerApiKey.id == key_id))).scalar_one()


async def _set_prev_expiry(
    org: uuid.UUID, user: uuid.UUID, key_id: uuid.UUID, when: datetime.datetime
) -> None:
    async with tenant_session(str(org), str(user)) as s:
        row = (await s.execute(select(IssuerApiKey).where(IssuerApiKey.id == key_id))).scalar_one()
        row.previous_secret_expires_at = when
        await s.flush()


# --- T01 / T02 / T07 / T23 / T33: unit (no DB) -----------------------------


def test_t01_mint_rotate_take_no_secret_material() -> None:
    forbidden = {"raw", "secret", "token", "secret_hash", "password", "pepper"}
    for fn in (svc.mint, svc.rotate):
        params = set(inspect.signature(fn).parameters)
        assert not (params & forbidden), f"{fn.__name__} exposes secret-material input"


def test_t02_hash_is_keyed_hmac_with_pepper() -> None:
    raw = "mycelium_ik_sample-value-123"
    pepper = get_settings().issuer_key_pepper.encode("utf-8")
    assert svc._hash(raw) == hmac.new(pepper, raw.encode("utf-8"), hashlib.sha256).digest()
    # Not a bare (unkeyed) sha256 -- a DB dump alone cannot recompute it.
    assert svc._hash(raw) != hashlib.sha256(raw.encode("utf-8")).digest()
    # Pepper isolation: a different pepper yields a different digest.
    other = hmac.new(b"a-different-pepper-000000000000000", raw.encode("utf-8"), hashlib.sha256)
    assert svc._hash(raw) != other.digest()


def test_t07_expiry_mandatory_and_capped() -> None:
    now = datetime.datetime.now(tz=_UTC)
    max_days = get_settings().issuer_key_max_lifetime_seconds // 86400
    # Default (None) -> non-null, ~max lifetime.
    default_exp = svc._resolve_expiry(None)
    assert default_exp > now
    assert (default_exp - now).days <= max_days
    assert (default_exp - now).days >= max_days - 1
    # Over-long request -> clamped to the cap.
    assert (svc._resolve_expiry(100_000) - now).days <= max_days
    # A normal request is honored.
    assert 28 <= (svc._resolve_expiry(30) - now).days <= 30


def test_t23_public_id_independent_of_secret() -> None:
    for _ in range(1000):
        raw = svc._generate_raw()
        pub = svc._generate_public_id()
        assert raw.startswith(svc.RAW_PREFIX)
        assert pub not in raw  # not a slice / substring of the secret


def test_t33_pepper_absent_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYCELIUM_ISSUER_KEY_PEPPER", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost/x",
            database_url_sync="postgresql+psycopg://u:p@localhost/x",
            jwt_secret="j" * 32,
            secret_key="A" * 44,
        )
    assert "issuer_key_pepper" in str(exc.value)


# --- T10 / T03: mint + authenticate + injection safety ---------------------


async def test_t10_mint_then_authenticate() -> None:
    org, user, issuer = await _org_and_issuer()
    key_id, raw = await _mint(org, user, issuer, permissions=[svc.PERM_READ, svc.PERM_SEND])
    # authenticate opens its own admin_session (no tenant GUC selected): the
    # SECURITY DEFINER function reads the row across the tenant boundary (RLS
    # ENABLE, not FORCE).
    auth = await svc.authenticate(raw)
    assert auth is not None
    assert auth.key_id == key_id
    assert auth.org_id == org
    assert auth.issuer_profile_id == issuer
    assert set(auth.permissions) == {svc.PERM_READ, svc.PERM_SEND}
    assert auth.matched_previous is False
    # Unknown secret / wrong prefix -> None (the latter never hits the DB).
    assert await svc.authenticate(f"{svc.RAW_PREFIX}{'x' * 43}") is None
    assert await svc.authenticate("mycelium_at_not-an-issuer-key") is None


async def test_t03_authenticate_injection_safe() -> None:
    org, user, issuer = await _org_and_issuer()
    _, raw = await _mint(org, user, issuer)
    # SQL metacharacters in the raw: the hash is a typed bytea bind param, so
    # this is inert data, and the collapsed-error path returns None (no oracle).
    injected = "mycelium_ik_'; DROP TABLE issuer_api_keys; --"
    assert await svc.authenticate(injected) is None
    # The table is intact and the real key still authenticates.
    assert await svc.authenticate(raw) is not None


# --- T04 / T05 / T06 / T28 / T21: rotation, grace, revoke, telemetry -------


async def test_t04_rotation_grace_bounded() -> None:
    org, user, issuer = await _org_and_issuer()
    key_id, _ = await _mint(org, user, issuer)
    async with tenant_session(str(org), str(user)) as s:
        await svc.rotate(s, org_id=org, actor_id=user, key_id=key_id, grace_seconds=10**9)
    row = await _read(org, user, key_id)
    ceiling = get_settings().issuer_key_rotation_grace_max_seconds
    assert row.previous_secret_expires_at is not None
    now = datetime.datetime.now(tz=_UTC)
    assert row.previous_secret_expires_at <= now + datetime.timedelta(seconds=ceiling + 5)


async def test_t05_expired_grace_does_not_block_current_secret() -> None:
    # The regression guard: grace-expiry is checked on the previous-hash branch,
    # NOT the shared gate -- an expired grace must never fail the CURRENT secret.
    org, user, issuer = await _org_and_issuer()
    key_id, _ = await _mint(org, user, issuer)
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.rotate(s, org_id=org, actor_id=user, key_id=key_id, grace_seconds=3600)
        new_raw = res.raw
    # Force the grace window into the past.
    await _set_prev_expiry(
        org, user, key_id, datetime.datetime.now(tz=_UTC) - datetime.timedelta(seconds=10)
    )
    auth = await svc.authenticate(new_raw)  # current secret must still work
    assert auth is not None
    assert auth.matched_previous is False


async def test_t06_revoke_kills_both_secrets() -> None:
    org, user, issuer = await _org_and_issuer()
    key_id, old_raw = await _mint(org, user, issuer)
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.rotate(s, org_id=org, actor_id=user, key_id=key_id, grace_seconds=3600)
        new_raw = res.raw
    # Both authenticate before revoke (current + grace).
    assert await svc.authenticate(new_raw) is not None
    assert await svc.authenticate(old_raw) is not None
    async with tenant_session(str(org), str(user)) as s:
        await svc.revoke(s, org_id=org, actor_id=user, key_id=key_id)
    # A single revoked_at kills both.
    assert await svc.authenticate(new_raw) is None
    assert await svc.authenticate(old_raw) is None


async def test_t28_two_probe_current_wins_and_grace_partial_unique() -> None:
    org, user, issuer = await _org_and_issuer()
    # Two freshly-minted keys both have previous_secret_hash NULL: the partial
    # unique index must not collide on NULLs.
    k1, _ = await _mint(org, user, issuer)
    k2, _ = await _mint(org, user, issuer)
    assert k1 != k2
    key_id, old_raw = await _mint(org, user, issuer)
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.rotate(s, org_id=org, actor_id=user, key_id=key_id, grace_seconds=3600)
        new_raw = res.raw
    cur = await svc.authenticate(new_raw)
    prev = await svc.authenticate(old_raw)
    assert cur is not None and cur.matched_previous is False  # current wins
    assert prev is not None and prev.matched_previous is True  # grace matched


async def test_t21_per_secret_grace_telemetry() -> None:
    org, user, issuer = await _org_and_issuer()
    key_id, old_raw = await _mint(org, user, issuer)
    async with tenant_session(str(org), str(user)) as s:
        await svc.rotate(s, org_id=org, actor_id=user, key_id=key_id, grace_seconds=3600)
    assert await svc.authenticate(old_raw) is not None  # authenticate via the GRACE secret
    row = await _read(org, user, key_id)
    assert row.previous_secret_last_used_at is not None  # grace telemetry bumped
    assert row.last_used_at is None  # the current-secret telemetry is untouched


# --- T20 / T24: throttle + pepper-bound verify -----------------------------


async def test_t20_last_used_bump_throttled() -> None:
    org, user, issuer = await _org_and_issuer()
    key_id, raw = await _mint(org, user, issuer)
    assert await svc.authenticate(raw) is not None
    first = (await _read(org, user, key_id)).last_used_at
    assert first is not None
    assert await svc.authenticate(raw) is not None  # again, within the 60s window
    second = (await _read(org, user, key_id)).last_used_at
    assert second == first  # not re-bumped (throttled); the gate still passed


async def test_t24_pepper_bound_verify() -> None:
    org, user, issuer = await _org_and_issuer()
    _, raw = await _mint(org, user, issuer)
    # A hash computed under a DIFFERENT pepper (a DB dump attacker without the
    # pepper) does not match the stored keyed hash.
    wrong = hmac.new(b"attacker-pepper-000000000000000000", raw.encode("utf-8"), hashlib.sha256)
    async with admin_session() as s:
        assert await svc._call_authenticate_fn(s, wrong.digest()) is None
    # The correct (peppered) hash authenticates.
    assert await svc.authenticate(raw) is not None
