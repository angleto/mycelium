"""Issuer-key ops hardening (task d3dd69c3): per-key IP allowlist (T37),
dual-pepper rotation window, and the structured security events.

Only the AUTH layer is under test here; the fiscal transmit path is
deliberately untouched by the feature.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import UnprocessableError
from mycelium_core.services import issuer_api_keys as svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="HRD")
    return r.org_id, r.user_id


async def _issuer(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        prof = await svc_inv().create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="P",
            legal_name="Acme Srl",
            vat_number="01234567890",
            address="Via Roma 1",
            postal_code="00100",
            city="Roma",
            is_default=True,
        )
    return prof.id


def svc_inv():
    from mycelium_core.services import invoice as inv

    return inv


async def _mint(
    org: uuid.UUID,
    user: uuid.UUID,
    issuer: uuid.UUID,
    *,
    ip_allowlist: list[str] | None = None,
) -> tuple[uuid.UUID, str]:
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.mint(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            name="k",
            permissions=[svc.PERM_READ],
            ip_allowlist=ip_allowlist,
        )
        return res.key.id, res.raw


# --- allowlist validation ----------------------------------------------------


def test_allowlist_normalization_and_rejection() -> None:
    norm = svc._normalize_ip_allowlist
    assert norm(None) is None
    assert norm([]) is None
    assert norm(["", "  "]) is None
    # Single address -> /32 network; host bits tolerated (strict=False).
    assert norm(["203.0.113.7"]) == ["203.0.113.7/32"]
    assert norm(["203.0.113.9/24"]) == ["203.0.113.0/24"]
    # IPv6 accepted; output canonical + sorted + deduped.
    out = norm(["2001:db8::1", "203.0.113.0/24", "203.0.113.0/24"])
    assert out == sorted(out or []) and len(out or []) == 2
    with pytest.raises(UnprocessableError):
        norm(["not-an-ip"])
    with pytest.raises(UnprocessableError):
        norm([f"10.0.{i}.0/24" for i in range(64)])  # over the cap


def test_ip_allowed_matching_is_fail_closed() -> None:
    allowed = svc._ip_allowed
    assert allowed("203.0.113.7", ["203.0.113.0/24"]) is True
    assert allowed("203.0.114.7", ["203.0.113.0/24"]) is False
    assert allowed(None, ["203.0.113.0/24"]) is False
    assert allowed("garbage", ["203.0.113.0/24"]) is False
    assert allowed("2001:db8::1", ["2001:db8::/32"]) is True
    # IPv4-mapped IPv6 source (dual-stack edge) matches v4 entries.
    assert allowed("::ffff:203.0.113.7", ["203.0.113.0/24"]) is True


# --- enforcement at authenticate ---------------------------------------------


async def test_t37_allowlist_enforced_at_authenticate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    org, user = await _org()
    issuer = await _issuer(org, user)
    _key_id, raw = await _mint(org, user, issuer, ip_allowlist=["203.0.113.0/24"])

    ok = await svc.authenticate(raw, client_ip="203.0.113.55")
    assert ok is not None and ok.ip_allowlist == ["203.0.113.0/24"]

    with caplog.at_level("WARNING", logger="mycelium.security"):
        denied = await svc.authenticate(raw, client_ip="198.51.100.9")
    assert denied is None
    assert any("issuer_key.ip_denied" in r.message for r in caplog.records)

    # Restricted key with no resolvable source: fail-closed.
    assert await svc.authenticate(raw, client_ip=None) is None

    # Unrestricted key: the source address is irrelevant.
    _kid2, raw2 = await _mint(org, user, issuer)
    assert await svc.authenticate(raw2, client_ip="198.51.100.9") is not None
    assert await svc.authenticate(raw2, client_ip=None) is not None


async def test_t37b_set_ip_allowlist_updates_without_remint() -> None:
    org, user = await _org()
    issuer = await _issuer(org, user)
    key_id, raw = await _mint(org, user, issuer)
    async with tenant_session(str(org), str(user)) as s:
        row = await svc.set_ip_allowlist(
            s, org_id=org, actor_id=user, key_id=key_id, ip_allowlist=["198.51.100.7"]
        )
        assert row.ip_allowlist == ["198.51.100.7/32"]
    assert await svc.authenticate(raw, client_ip="198.51.100.7") is not None
    assert await svc.authenticate(raw, client_ip="203.0.113.1") is None
    # Lifting the restriction restores unrestricted auth (same secret).
    async with tenant_session(str(org), str(user)) as s:
        await svc.set_ip_allowlist(s, org_id=org, actor_id=user, key_id=key_id, ip_allowlist=None)
    assert await svc.authenticate(raw, client_ip="203.0.113.1") is not None


# --- security events ----------------------------------------------------------


async def test_auth_failed_event_on_unknown_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="mycelium.security"):
        out = await svc.authenticate(f"{svc.RAW_PREFIX}definitely-not-a-key", client_ip="1.2.3.4")
    assert out is None
    assert any("issuer_key.auth_failed" in r.message for r in caplog.records)


async def test_dormant_key_event(caplog: pytest.LogCaptureFixture) -> None:
    org, user = await _org()
    issuer = await _issuer(org, user)
    key_id, raw = await _mint(org, user, issuer)
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(
            text(
                "UPDATE issuer_api_keys SET last_used_at = now() - interval '40 days' "
                "WHERE id = :kid"
            ),
            {"kid": str(key_id)},
        )
    with caplog.at_level("WARNING", logger="mycelium.security"):
        ok = await svc.authenticate(raw, client_ip="1.2.3.4")
    assert ok is not None
    assert any("issuer_key.dormant_key_used" in r.message for r in caplog.records)


# --- dual-pepper rotation window ----------------------------------------------


async def test_pepper_rotation_window(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    org, user = await _org()
    issuer = await _issuer(org, user)
    old_pepper = get_settings().issuer_key_pepper
    _key_id, raw = await _mint(org, user, issuer)  # hashed under OLD pepper
    new_pepper = "n" * 40
    try:
        # Flip the pepper WITHOUT the window: existing key stops working.
        monkeypatch.setenv("MYCELIUM_ISSUER_KEY_PEPPER", new_pepper)
        get_settings.cache_clear()
        assert await svc.authenticate(raw, client_ip="1.2.3.4") is None

        # Open the window: the key authenticates via the previous probe and
        # the rotation-telemetry event fires.
        monkeypatch.setenv("MYCELIUM_ISSUER_KEY_PEPPER_PREVIOUS", old_pepper)
        get_settings.cache_clear()
        with caplog.at_level("WARNING", logger="mycelium.security"):
            princ = await svc.authenticate(raw, client_ip="1.2.3.4")
        assert princ is not None and princ.matched_previous_pepper is True
        assert any("issuer_key.previous_pepper_used" in r.message for r in caplog.records)

        # Re-mint (rotate) inside the window: the NEW secret hashes under the
        # CURRENT pepper and keeps working after the window closes.
        async with tenant_session(str(org), str(user)) as s:
            rotated = await svc.rotate(s, org_id=org, actor_id=user, key_id=_key_id)
        monkeypatch.delenv("MYCELIUM_ISSUER_KEY_PEPPER_PREVIOUS")
        get_settings.cache_clear()
        assert await svc.authenticate(rotated.raw, client_ip="1.2.3.4") is not None
        assert await svc.authenticate(raw, client_ip="1.2.3.4") is None  # old raw dead
    finally:
        monkeypatch.setenv("MYCELIUM_ISSUER_KEY_PEPPER", old_pepper)
        monkeypatch.delenv("MYCELIUM_ISSUER_KEY_PEPPER_PREVIOUS", raising=False)
        get_settings.cache_clear()


# --- migration 0080 structure gate ---------------------------------------------


def test_migration_0080_structure() -> None:
    import sqlalchemy as sa

    engine = sa.create_engine(get_settings().database_url_sync)
    try:
        with engine.connect() as conn:
            col = conn.execute(
                sa.text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'issuer_api_keys' AND column_name = 'ip_allowlist'"
                )
            ).scalar_one()
            assert col == "ARRAY"
            args = conn.execute(
                sa.text(
                    "SELECT pg_get_function_arguments(p.oid) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.proname = 'authenticate_issuer_api_key'"
                )
            ).scalar_one()
            for col_name in ("out_ip_allowlist", "out_last_used_at"):
                assert col_name in args, f"missing {col_name} in {args}"
            grant = conn.execute(
                sa.text(
                    "SELECT has_function_privilege('mycelium_app', "
                    "'public.authenticate_issuer_api_key(bytea)', 'EXECUTE')"
                )
            ).scalar_one()
            assert grant is True
    finally:
        engine.dispose()


# --- trusted-proxy source resolution (the X-Forwarded-For anti-spoof) ---------


class _FakeReq:
    """Minimal stand-in for starlette Request: only what the resolver reads."""

    def __init__(self, *, xff: str | None, peer: str | None) -> None:
        self.headers = {} if xff is None else {"x-forwarded-for": xff}

        class _C:
            host = peer

        self.client = _C() if peer is not None else None


def _resolve(xff: str | None, peer: str | None, proxies: list[str]):
    from mycelium_api.deps import _resolve_issuer_client_ip

    get_settings().issuer_key_trusted_proxies = proxies  # type: ignore[misc]
    try:
        return _resolve_issuer_client_ip(_FakeReq(xff=xff, peer=peer))  # type: ignore[arg-type]
    finally:
        get_settings.cache_clear()


def test_resolver_direct_connection_uses_peer() -> None:
    # No forwarding header: the TCP peer IS the client (direct / test transport).
    assert _resolve(None, "203.0.113.9", []) == "203.0.113.9"
    assert _resolve(None, None, []) is None


def test_resolver_fails_closed_without_trusted_proxies() -> None:
    # A forwarded request but no configured trust anchor is unattributable.
    assert _resolve("203.0.113.7", "10.0.0.5", []) is None


def test_resolver_returns_rightmost_untrusted_defeating_a_left_spoof() -> None:
    # The anti-spoof core: nginx appends the real remote address on the RIGHT.
    # An attacker prepending an allowlisted IP on the left cannot outrun it --
    # the rightmost NON-proxy hop (their real address) is what wins.
    proxies = ["10.0.0.0/8"]
    # legit: real client 203.0.113.7, then the trusted proxy hop.
    assert _resolve("203.0.113.7, 10.0.0.5", "10.0.0.5", proxies) == "203.0.113.7"
    # spoof: attacker puts an allowlisted IP left, but their real IP is appended
    # right (not trusted) -> that real IP is returned, not the spoof.
    assert _resolve("203.0.113.7, 198.51.100.9", "10.0.0.5", proxies) == "198.51.100.9"
    # every hop trusted -> the edge never carried a client -> unattributable.
    assert _resolve("10.0.0.5, 10.0.0.6", "10.0.0.6", proxies) is None
    # a malformed hop poisons the chain (fail closed).
    assert _resolve("garbage, 10.0.0.5", "10.0.0.5", proxies) is None
