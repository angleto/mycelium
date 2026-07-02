"""Public Invoice API (/api/v1) end-to-end (phase 3 of task 19b7e874).

Auth + scoping + idempotency of the per-issuer-key surface: permission gating
(T08), issuer + org hard-scoping / IDOR (T09), the confused-deputy inline-client
gate (T11), fiscal idempotency (T15) + body-mismatch (T16), the two 401 shapes
(T29), the not-draft conflict (T30), and credit-note issuer scoping (T38).

Setup goes through the service layer (signup + issuer profile + client + mint);
the /api/v1 calls go over HTTP with the raw key, since key management is REST-
only and lands in phase 4.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from mycelium_api import rate_limit
from mycelium_api.app import create_app
from mycelium_api.main import app
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import QuotaExceededError
from mycelium_core.models.invoice import Invoice
from mycelium_core.services import invoice as inv
from mycelium_core.services import issuer_api_keys as svc
from mycelium_core.services.auth import signup
from mycelium_core.services.issuer_api_keys import (
    PERM_CLIENT_WRITE,
    PERM_COMPOSE,
    PERM_CREDIT_NOTE,
    PERM_READ,
    PERM_SEND,
)
from mycelium_core.services.taxonomy import ClientInput, create_client

_LINE = {"description": "consulting", "unit_price": "100.00"}


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _h(raw: str, idem: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {raw}"}
    if idem is not None:
        h["Idempotency-Key"] = idem
    return h


async def _base() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A workspace with one issuer profile + one client. Returns (org, user,
    issuer_profile_id, client_tag_id)."""
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="IK")
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        prof = await inv.create_issuer_profile(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            label="A",
            legal_name="Acme Srl",
            vat_number="01234567890",
            address="Via Roma 1",
            postal_code="00100",
            city="Roma",
            is_default=True,
        )
        tag = await create_client(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            name="Client SpA",
            profile=ClientInput(
                legal_name="Client SpA",
                country_code="IT",
                vat_number="09876543210",
                sdi_code="ABCDEFG",
                address="Via Milano 2",
                postal_code="20100",
                city="Milano",
            ),
        )
    return r.org_id, r.user_id, prof.id, tag.id


async def _issuer(org: uuid.UUID, user: uuid.UUID, label: str) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        prof = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label=label,
            legal_name=f"{label} Srl",
            vat_number="01234567890",
            address="Via B 2",
            postal_code="00100",
            city="Roma",
        )
    return prof.id


async def _key(org: uuid.UUID, user: uuid.UUID, issuer: uuid.UUID, perms: list[str]) -> str:
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.mint(
            s, org_id=org, actor_id=user, issuer_profile_id=issuer, name="k", permissions=perms
        )
        return res.raw


def _compose_body(client_tag_id: uuid.UUID, *, transmit: bool = False) -> dict:
    return {"client_tag_id": str(client_tag_id), "lines": [_LINE], "transmit": transmit}


async def test_t08_per_key_permission() -> None:
    org, user, issuer, client = await _base()
    full = await _key(org, user, issuer, [PERM_COMPOSE, PERM_SEND, PERM_CREDIT_NOTE, PERM_READ])
    cn_only = await _key(org, user, issuer, [PERM_CREDIT_NOTE])
    async with _client() as c:
        r = await c.post(
            "/api/v1/invoices", headers=_h(full, "c1"), json=_compose_body(client, transmit=True)
        )
        assert r.status_code == 200, r.text
        parent = r.json()["id"]
        # credit_note perm creates+transmits its TD04...
        cn = await c.post(
            "/api/v1/invoices/credit-note",
            headers=_h(cn_only, "cn1"),
            json={"parent_invoice_id": parent},
        )
        assert cn.status_code == 200 and cn.json()["document_type"] == "TD04"
        # ...but does NOT grant the generic transmit (no invoice:send).
        t = await c.post(f"/api/v1/invoices/{parent}/transmit", headers=_h(cn_only, "t1"))
        assert t.status_code == 403


async def test_t09_issuer_and_org_hard_scoping_idor() -> None:
    org, user, issuer_a, client = await _base()
    issuer_b = await _issuer(org, user, "B")
    key_a = await _key(org, user, issuer_a, [PERM_COMPOSE, PERM_READ])
    key_b = await _key(org, user, issuer_b, [PERM_COMPOSE, PERM_READ])
    async with _client() as c:
        rb = await c.post("/api/v1/invoices", headers=_h(key_b, "b1"), json=_compose_body(client))
        assert rb.status_code == 200
        inv_b = rb.json()["id"]
        # Key for issuer A cannot see an invoice under issuer B (same org) -> 404.
        g = await c.get(f"/api/v1/invoices/{inv_b}", headers=_h(key_a))
        assert g.status_code == 404
        # ...and it is absent from A's issuer-scoped list.
        lst = await c.get("/api/v1/invoices", headers=_h(key_a))
        assert inv_b not in {x["id"] for x in lst.json()}
    # Cross-org: another workspace's invoice is a 404 to key_a (RLS).
    org2, user2, issuer2, client2 = await _base()
    key2 = await _key(org2, user2, issuer2, [PERM_COMPOSE, PERM_READ])
    async with _client() as c:
        r2 = await c.post("/api/v1/invoices", headers=_h(key2, "o2"), json=_compose_body(client2))
        inv2 = r2.json()["id"]
        g = await c.get(f"/api/v1/invoices/{inv2}", headers=_h(key_a))
        assert g.status_code == 404


async def test_t11_confused_deputy_inline_client_needs_client_write() -> None:
    org, user, issuer, client = await _base()
    compose_only = await _key(org, user, issuer, [PERM_COMPOSE, PERM_READ])  # no client_write
    inline = {
        "client": {
            "legal_name": "Brand New Co",
            "country_code": "IT",
            "vat_number": "12345678903",
            "address": "Via X 1",
            "postal_code": "00100",
            "city": "Roma",
        },
        "lines": [_LINE],
    }
    async with _client() as c:
        # Inline recipient without invoice:client_write -> 403 (cannot create a client).
        r = await c.post("/api/v1/invoices", headers=_h(compose_only, "i1"), json=inline)
        assert r.status_code == 403
        # Referencing an existing client_tag_id -> allowed.
        ok = await c.post(
            "/api/v1/invoices", headers=_h(compose_only, "i2"), json=_compose_body(client)
        )
        assert ok.status_code == 200
    # With client_write, the inline path resolves-or-creates.
    full = await _key(org, user, issuer, [PERM_COMPOSE, PERM_CLIENT_WRITE, PERM_READ])
    async with _client() as c:
        r = await c.post("/api/v1/invoices", headers=_h(full, "i3"), json=inline)
        assert r.status_code == 200, r.text


async def test_t15_fiscal_idempotency_concurrent() -> None:
    org, user, issuer, client = await _base()
    key = await _key(org, user, issuer, [PERM_COMPOSE, PERM_READ])
    body = _compose_body(client)
    async with _client() as c:
        r1, r2 = await asyncio.gather(
            c.post("/api/v1/invoices", headers=_h(key, "same"), json=body),
            c.post("/api/v1/invoices", headers=_h(key, "same"), json=body),
        )
        assert r1.status_code == 200 and r2.status_code == 200
        # Exactly one draft: both responses carry the same id...
        assert r1.json()["id"] == r2.json()["id"]
        # ...and only one invoice exists for the client.
        lst = await c.get(
            "/api/v1/invoices", headers=_h(key), params={"client_tag_id": str(client)}
        )
        assert len(lst.json()) == 1


async def test_t16_idempotency_body_mismatch() -> None:
    org, user, issuer, client = await _base()
    key = await _key(org, user, issuer, [PERM_COMPOSE, PERM_READ])
    async with _client() as c:
        a = await c.post(
            "/api/v1/invoices",
            headers=_h(key, "k16"),
            json={"client_tag_id": str(client), "lines": [_LINE]},
        )
        assert a.status_code == 200
        b = await c.post(
            "/api/v1/invoices",
            headers=_h(key, "k16"),
            json={
                "client_tag_id": str(client),
                "lines": [{"description": "different", "unit_price": "5"}],
            },
        )
        assert b.status_code == 422


async def test_t29_auth_error_shapes() -> None:
    async with _client() as c:
        missing = await c.get("/api/v1/invoices")
        assert missing.status_code == 401 and missing.json()["code"] == "auth.missing_bearer"
        malformed = await c.get("/api/v1/invoices", headers={"Authorization": "Basic zzz"})
        assert malformed.status_code == 401 and malformed.json()["code"] == "auth.missing_bearer"
        bad = await c.get(
            "/api/v1/invoices", headers={"Authorization": f"Bearer mycelium_ik_{'x' * 43}"}
        )
        # Credential-invalid is collapsed (not a key-existence oracle).
        assert bad.status_code == 401 and bad.json()["code"] == "auth.token_invalid"


async def test_t30_double_transmit_conflict() -> None:
    org, user, issuer, client = await _base()
    key = await _key(org, user, issuer, [PERM_COMPOSE, PERM_SEND, PERM_READ])
    async with _client() as c:
        r = await c.post("/api/v1/invoices", headers=_h(key, "c30"), json=_compose_body(client))
        inv_id = r.json()["id"]
        t1 = await c.post(f"/api/v1/invoices/{inv_id}/transmit", headers=_h(key, "t30a"))
        assert t1.status_code == 200 and t1.json()["state"] == "transmitted"
        # A second transmit (different idempotency key) hits the immutable state -> 409.
        t2 = await c.post(f"/api/v1/invoices/{inv_id}/transmit", headers=_h(key, "t30b"))
        assert t2.status_code == 409


async def test_t38_credit_note_issuer_scoping() -> None:
    org, user, issuer_a, client = await _base()
    issuer_b = await _issuer(org, user, "B")
    key_a_full = await _key(
        org, user, issuer_a, [PERM_COMPOSE, PERM_SEND, PERM_CREDIT_NOTE, PERM_READ]
    )
    key_b_full = await _key(org, user, issuer_b, [PERM_COMPOSE, PERM_SEND, PERM_READ])
    key_a_cn = await _key(org, user, issuer_a, [PERM_CREDIT_NOTE])
    async with _client() as c:
        # A transmitted parent under issuer B.
        rb = await c.post(
            "/api/v1/invoices",
            headers=_h(key_b_full, "b38"),
            json=_compose_body(client, transmit=True),
        )
        parent_b = rb.json()["id"]
        # Issuer-A credit-note key cannot correct issuer B's invoice -> 404.
        cn_cross = await c.post(
            "/api/v1/invoices/credit-note",
            headers=_h(key_a_cn, "cn38"),
            json={"parent_invoice_id": parent_b},
        )
        assert cn_cross.status_code == 404
        # A credit_note-only key cannot compose a TD01 -> 403.
        comp = await c.post(
            "/api/v1/invoices", headers=_h(key_a_cn, "comp38"), json=_compose_body(client)
        )
        assert comp.status_code == 403
        # A parent under issuer A: the same credit-note key succeeds.
        ra = await c.post(
            "/api/v1/invoices",
            headers=_h(key_a_full, "a38"),
            json=_compose_body(client, transmit=True),
        )
        parent_a = ra.json()["id"]
        ok = await c.post(
            "/api/v1/invoices/credit-note",
            headers=_h(key_a_cn, "cn38b"),
            json={"parent_invoice_id": parent_a},
        )
        assert ok.status_code == 200 and ok.json()["document_type"] == "TD04"


# --- phase 3b: rate limit / events feed / CORS guard / fiscal-filename record --


async def _key_id(org: uuid.UUID, user: uuid.UUID, issuer: uuid.UUID, name: str) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.mint(
            s,
            org_id=org,
            actor_id=user,
            issuer_profile_id=issuer,
            name=name,
            permissions=[PERM_SEND],
        )
        return res.key.id


async def test_t14_rate_limit_per_key_isolation() -> None:
    org, user, issuer, _ = await _base()
    k1 = await _key_id(org, user, issuer, "k1")
    k2 = await _key_id(org, user, issuer, "k2")
    async with tenant_session(str(org), str(user)) as s:
        # limit 2 within the window: the 3rd call over-limits.
        await rate_limit.check(
            s, org_id=org, key_id=k1, endpoint_class="transmit", limit=2, window_seconds=60
        )
        await rate_limit.check(
            s, org_id=org, key_id=k1, endpoint_class="transmit", limit=2, window_seconds=60
        )
        with pytest.raises(QuotaExceededError):
            await rate_limit.check(
                s, org_id=org, key_id=k1, endpoint_class="transmit", limit=2, window_seconds=60
            )
    # A different key has its own bucket (per-key isolation).
    async with tenant_session(str(org), str(user)) as s:
        await rate_limit.check(
            s, org_id=org, key_id=k2, endpoint_class="transmit", limit=2, window_seconds=60
        )


async def test_t39_events_feed_reflects_state_changes() -> None:
    org, user, issuer, client = await _base()
    key = await _key(org, user, issuer, [PERM_COMPOSE, PERM_SEND, PERM_READ])
    async with _client() as c:
        r = await c.post("/api/v1/invoices", headers=_h(key, "e1"), json=_compose_body(client))
        inv_id = r.json()["id"]
        ev = (await c.get("/api/v1/events", headers=_h(key))).json()
        first = next(e for e in ev if e["invoice_id"] == inv_id)
        assert first["state"] == "draft"
        t = await c.post(f"/api/v1/invoices/{inv_id}/transmit", headers=_h(key, "e2"))
        assert t.status_code == 200
        # Since the draft's updated_at, the transmitted state change surfaces.
        ev2 = (
            await c.get("/api/v1/events", headers=_h(key), params={"since": first["updated_at"]})
        ).json()
        states = {e["invoice_id"]: e["state"] for e in ev2}
        assert states.get(inv_id) == "transmitted"


async def test_f8_transmit_records_fiscal_filename() -> None:
    org, user, issuer, client = await _base()
    key = await _key(org, user, issuer, [PERM_COMPOSE, PERM_SEND, PERM_READ])
    async with _client() as c:
        r = await c.post(
            "/api/v1/invoices", headers=_h(key, "f8"), json=_compose_body(client, transmit=True)
        )
        assert r.status_code == 200
        inv_id = uuid.UUID(r.json()["id"])
    async with tenant_session(str(org), str(user)) as s:
        row = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert row.progressivo_invio is not None
        assert row.nome_file is not None and row.nome_file.endswith(".xml")


def test_t25_cors_wildcard_refused_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYCELIUM_CORS_ORIGINS", "*")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            create_app()
    finally:
        monkeypatch.delenv("MYCELIUM_CORS_ORIGINS", raising=False)
        get_settings.cache_clear()
