"""Issuer-API-key management REST (phase 4 of task 19b7e874).

Owner-gated mint / list / rotate / revoke under
``/issuer-profiles/{id}/api-keys``, verified end-to-end against the /api/v1
surface (a freshly minted key authenticates; a rotated/revoked one behaves), the
issuer-nesting 404, and the expiry-warning scan (G2 / T35).
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from mycelium_api.main import app
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.notification import Notification, NotificationChannelKind
from mycelium_core.services import invoice as inv
from mycelium_core.services import issuer_api_keys as svc
from mycelium_core.services import notifications as notif
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _setup(c: AsyncClient) -> tuple[dict[str, str], str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "K"},
        )
    ).json()
    h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }
    p = (
        await c.post(
            "/issuer-profiles",
            headers=h,
            json={
                "label": "P",
                "legal_name": "Acme Srl",
                "vat_number": "01234567890",
                "address": "Via Roma 1",
                "postal_code": "00100",
                "city": "Roma",
            },
        )
    ).json()
    return h, p["id"]


def _bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def test_issuer_api_key_lifecycle() -> None:
    async with _client() as c:
        h, issuer = await _setup(c)
        m = await c.post(
            f"/issuer-profiles/{issuer}/api-keys",
            headers=h,
            json={"name": "integration", "permissions": ["invoice:read", "invoice:compose"]},
        )
        assert m.status_code == 200, m.text
        key = m.json()
        assert key["raw"].startswith("mycelium_ik_")
        assert key["prefix"].startswith("mycelium_ik_") and key["prefix"] != key["raw"]
        assert sorted(key["permissions"]) == ["invoice:compose", "invoice:read"]
        assert key["days_to_expiry"] >= 360  # mandatory expiry defaults to ~365d
        raw1, kid = key["raw"], key["id"]

        # Listed without the secret.
        lst = await c.get(f"/issuer-profiles/{issuer}/api-keys", headers=h)
        assert lst.status_code == 200
        listed = next(k for k in lst.json() if k["id"] == kid)
        assert "raw" not in listed

        # The minted secret authenticates the public API.
        assert (await c.get("/api/v1/invoices", headers=_bearer(raw1))).status_code == 200

        # Rotate (hard, grace 0): a new secret; the old one dies, the new works.
        rot = await c.post(f"/issuer-profiles/{issuer}/api-keys/{kid}/rotate", headers=h)
        assert rot.status_code == 200
        raw2 = rot.json()["raw"]
        assert raw2 != raw1
        assert (await c.get("/api/v1/invoices", headers=_bearer(raw1))).status_code == 401
        assert (await c.get("/api/v1/invoices", headers=_bearer(raw2))).status_code == 200

        # Revoke: the current secret dies. Idempotent second delete.
        assert (
            await c.delete(f"/issuer-profiles/{issuer}/api-keys/{kid}", headers=h)
        ).status_code == 204
        assert (await c.get("/api/v1/invoices", headers=_bearer(raw2))).status_code == 401
        assert (
            await c.delete(f"/issuer-profiles/{issuer}/api-keys/{kid}", headers=h)
        ).status_code == 204


async def test_mint_is_owner_gated() -> None:
    async with _client() as c:
        h, issuer = await _setup(c)
        member = {**h, "X-Workspace-Role": "member"}  # sudo-downgrade to member
        r = await c.post(f"/issuer-profiles/{issuer}/api-keys", headers=member, json={"name": "x"})
        assert r.status_code == 403


async def test_rotate_revoke_wrong_issuer_is_404() -> None:
    async with _client() as c:
        h, issuer_a = await _setup(c)
        pb = (
            await c.post(
                "/issuer-profiles",
                headers=h,
                json={
                    "label": "B",
                    "legal_name": "Beta Srl",
                    "vat_number": "01234567890",
                    "address": "Via B 2",
                    "postal_code": "00100",
                    "city": "Roma",
                },
            )
        ).json()
        issuer_b = pb["id"]
        kid = (
            await c.post(f"/issuer-profiles/{issuer_a}/api-keys", headers=h, json={"name": "a"})
        ).json()["id"]
        # The key belongs to issuer A: addressing it under issuer B is a 404.
        assert (
            await c.post(f"/issuer-profiles/{issuer_b}/api-keys/{kid}/rotate", headers=h)
        ).status_code == 404
        assert (
            await c.delete(f"/issuer-profiles/{issuer_b}/api-keys/{kid}", headers=h)
        ).status_code == 404


async def test_t35_expiry_warning_scan_idempotent() -> None:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="EX")
    org, user = r.org_id, r.user_id
    async with tenant_session(str(org), str(user)) as s:
        prof = await inv.create_issuer_profile(
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
        await notif.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            enabled=True,
            target="ops@example.test",
        )
        near = await svc.mint(
            s, org_id=org, actor_id=user, issuer_profile_id=prof.id, name="near", ttl_days=20
        )
        # A far-future key must NOT warn.
        await svc.mint(
            s, org_id=org, actor_id=user, issuer_profile_id=prof.id, name="far", ttl_days=200
        )
        kid = near.key.id
    # Scan twice: the within-window key warns once (idempotent), the far one never.
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        await svc.scan_issuer_key_expiry(s, org_id=org)
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        await svc.scan_issuer_key_expiry(s, org_id=org)
    async with tenant_session(str(org), str(user)) as s:
        rows = (
            (await s.execute(select(Notification).where(Notification.kind == "issuer_key_expiry")))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # exactly one, despite two scans and a second (far) key
    assert rows[0].dedupe_key == f"issuer_key_expiry:{kid}:30:email"


async def test_t37_allowlist_mint_update_and_public_api_enforcement() -> None:
    # ASGITransport presents client ip 127.0.0.1 to the app, so an allowlist
    # containing 127.0.0.1/32 admits the request and a disjoint one denies it
    # with the COLLAPSED 401 (no allowlist oracle).
    async with _client() as c:
        h, issuer = await _setup(c)
        m = await c.post(
            f"/issuer-profiles/{issuer}/api-keys",
            headers=h,
            json={
                "name": "restricted",
                "permissions": ["invoice:read"],
                "ip_allowlist": ["127.0.0.1"],
            },
        )
        assert m.status_code == 200, m.text
        key = m.json()
        assert key["ip_allowlist"] == ["127.0.0.1/32"]
        raw = key["raw"]

        ok = await c.get("/api/v1/invoices", headers=_bearer(raw))
        assert ok.status_code == 200, ok.text

        # Tighten to a disjoint network WITHOUT re-minting: same secret, 401.
        u = await c.put(
            f"/issuer-profiles/{issuer}/api-keys/{key['id']}/allowlist",
            headers=h,
            json={"ip_allowlist": ["10.0.0.0/8"]},
        )
        assert u.status_code == 200, u.text
        assert u.json()["ip_allowlist"] == ["10.0.0.0/8"]
        denied = await c.get("/api/v1/invoices", headers=_bearer(raw))
        assert denied.status_code == 401
        assert denied.json()["code"] == "auth.token_invalid"  # collapsed

        # Lift the restriction: the same secret works again.
        u2 = await c.put(
            f"/issuer-profiles/{issuer}/api-keys/{key['id']}/allowlist",
            headers=h,
            json={"ip_allowlist": None},
        )
        assert u2.status_code == 200
        again = await c.get("/api/v1/invoices", headers=_bearer(raw))
        assert again.status_code == 200


async def test_t37c_invalid_allowlist_rejected() -> None:
    async with _client() as c:
        h, issuer = await _setup(c)
        bad = await c.post(
            f"/issuer-profiles/{issuer}/api-keys",
            headers=h,
            json={"name": "bad", "ip_allowlist": ["not-a-cidr"]},
        )
        assert bad.status_code == 422
        assert bad.json()["code"] == "issuer_api_key.ip_allowlist_invalid"


async def test_t37d_forwarded_for_spoof_is_defeated(monkeypatch) -> None:
    # End-to-end through the app: with the trusted-proxy chain configured, the
    # allowlist matches the RIGHTMOST non-proxy hop (what nginx appends), so a
    # left-prepended allowlisted IP cannot bypass it; with NO trusted proxies
    # a forwarded request fails closed.
    from mycelium_core.config import get_settings

    async with _client() as c:
        h, issuer = await _setup(c)
        m = await c.post(
            f"/issuer-profiles/{issuer}/api-keys",
            headers=h,
            json={"name": "r", "permissions": ["invoice:read"], "ip_allowlist": ["203.0.113.0/24"]},
        )
        assert m.status_code == 200, m.text
        raw = m.json()["raw"]

        monkeypatch.setenv("MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES", '["10.0.0.0/8"]')
        get_settings.cache_clear()
        try:
            # Real client (rightmost non-proxy) inside the allowlist -> 200.
            ok = await c.get(
                "/api/v1/invoices",
                headers={**_bearer(raw), "X-Forwarded-For": "203.0.113.7, 10.0.0.5"},
            )
            assert ok.status_code == 200, ok.text
            # Spoof: allowlisted IP prepended, but the real appended hop is
            # off-net -> that hop wins -> collapsed 401.
            spoof = await c.get(
                "/api/v1/invoices",
                headers={**_bearer(raw), "X-Forwarded-For": "203.0.113.7, 198.51.100.9"},
            )
            assert spoof.status_code == 401
            assert spoof.json()["code"] == "auth.token_invalid"
        finally:
            monkeypatch.delenv("MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES", raising=False)
            get_settings.cache_clear()

        # No trusted proxies configured + a forwarded request -> fail closed.
        denied = await c.get(
            "/api/v1/invoices",
            headers={**_bearer(raw), "X-Forwarded-For": "203.0.113.7"},
        )
        assert denied.status_code == 401
        assert denied.json()["code"] == "auth.token_invalid"


async def test_purge_revoked_key_via_rest() -> None:
    async with _client() as c:
        h, issuer = await _setup(c)
        m = await c.post(
            f"/issuer-profiles/{issuer}/api-keys",
            headers=h,
            json={"name": "to-purge", "permissions": ["invoice:read"]},
        )
        assert m.status_code == 200, m.text
        kid = m.json()["id"]

        # hard=true on an ACTIVE key -> 409 not_revoked (revoke first).
        r = await c.request(
            "DELETE", f"/issuer-profiles/{issuer}/api-keys/{kid}?hard=true", headers=h
        )
        assert r.status_code == 409
        assert r.json()["code"] == "issuer_api_key.not_revoked"

        # Soft delete (revoke) -> still in the list, revoked.
        assert (
            await c.request("DELETE", f"/issuer-profiles/{issuer}/api-keys/{kid}", headers=h)
        ).status_code == 204
        lst = (await c.get(f"/issuer-profiles/{issuer}/api-keys", headers=h)).json()
        row = next(k for k in lst if k["id"] == kid)
        assert row["revoked_at"] is not None

        # hard=true -> purged, gone from the list.
        assert (
            await c.request(
                "DELETE", f"/issuer-profiles/{issuer}/api-keys/{kid}?hard=true", headers=h
            )
        ).status_code == 204
        lst2 = (await c.get(f"/issuer-profiles/{issuer}/api-keys", headers=h)).json()
        assert all(k["id"] != kid for k in lst2)
