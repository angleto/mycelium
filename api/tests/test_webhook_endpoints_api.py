"""Signed-webhook endpoint management REST (task 2c23e955, ADR-0047).

Owner-gated create / list / patch / rotate-secret / revoke / purge under
``/issuer-profiles/{id}/webhook-endpoints``, the issuer-nesting 404, URL
validation, and the revoke-then-purge lifecycle.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


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


async def test_webhook_endpoint_lifecycle() -> None:
    async with _client() as c:
        h, issuer = await _setup(c)
        base = f"/issuer-profiles/{issuer}/webhook-endpoints"

        # Create -> secret shown once.
        r = await c.post(
            base,
            headers=h,
            json={"name": "prod", "url": "https://1.1.1.1/hook", "event_types": []},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        eid = body["id"]
        assert body["secret"].startswith("whsec_")
        assert body["active"] is True

        # List -> no secret leaked.
        lst = (await c.get(base, headers=h)).json()
        assert len(lst) == 1 and "secret" not in lst[0]

        # Patch the subscription set.
        r = await c.patch(f"{base}/{eid}", headers=h, json={"event_types": ["invoice.delivered"]})
        assert r.status_code == 200 and r.json()["event_types"] == ["invoice.delivered"]

        # Rotate the secret -> new secret, different from the first.
        r = await c.post(f"{base}/{eid}/rotate-secret", headers=h)
        assert r.status_code == 200 and r.json()["secret"] != body["secret"]

        # Deliveries endpoint (empty, but reachable).
        assert (await c.get(f"{base}/{eid}/deliveries", headers=h)).status_code == 200

        # hard=true on an ACTIVE endpoint -> 409 not_revoked.
        r = await c.request("DELETE", f"{base}/{eid}?hard=true", headers=h)
        assert r.status_code == 409 and r.json()["code"] == "webhook_endpoint.not_revoked"

        # Revoke (soft) -> still listed, revoked.
        assert (await c.request("DELETE", f"{base}/{eid}", headers=h)).status_code == 204
        row = next(e for e in (await c.get(base, headers=h)).json() if e["id"] == eid)
        assert row["revoked_at"] is not None and row["active"] is False

        # Purge -> gone.
        assert (await c.request("DELETE", f"{base}/{eid}?hard=true", headers=h)).status_code == 204
        assert all(e["id"] != eid for e in (await c.get(base, headers=h)).json())


async def test_create_rejects_non_https_url() -> None:
    async with _client() as c:
        h, issuer = await _setup(c)
        r = await c.post(
            f"/issuer-profiles/{issuer}/webhook-endpoints",
            headers=h,
            json={"name": "x", "url": "http://1.1.1.1/hook"},
        )
        assert r.status_code == 422
        assert r.json()["code"] == "webhook_endpoint.url_invalid"


async def test_endpoint_under_wrong_issuer_is_404() -> None:
    async with _client() as c:
        h, issuer = await _setup(c)
        made = (
            await c.post(
                f"/issuer-profiles/{issuer}/webhook-endpoints",
                headers=h,
                json={"name": "x", "url": "https://1.1.1.1/hook"},
            )
        ).json()
        other = (
            await c.post(
                "/issuer-profiles",
                headers=h,
                json={
                    "label": "P2",
                    "legal_name": "Beta Srl",
                    "vat_number": "11111111119",
                    "address": "Via B 2",
                    "postal_code": "20100",
                    "city": "Milano",
                },
            )
        ).json()
        r = await c.patch(
            f"/issuer-profiles/{other['id']}/webhook-endpoints/{made['id']}",
            headers=h,
            json={"name": "z"},
        )
        assert r.status_code == 404
