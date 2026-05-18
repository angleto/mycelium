"""F7 API end-to-end (DB-backed): fiscal profile, invoice draft +
lines, transmit (manual export -> out of AdE coverage), immutability,
XML download, TD04 credit note, cross-org isolation."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f7_api_flow() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "org_name": "A"},
            )
        ).json()
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "org_name": "B"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Org-Id": a["org_id"]}

        fp = await c.put(
            "/fiscal-profile",
            headers=h,
            json={
                "denominazione": "Acme Srl",
                "piva": "01234567890",
                "indirizzo": "Via Roma 1",
                "cap": "00100",
                "comune": "Roma",
            },
        )
        assert fp.status_code == 200 and fp.json()["denominazione"] == "Acme Srl"

        client = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "Client SpA",
                    "ragione_sociale": "Client SpA",
                    "id_paese": "IT",
                    "id_codice": "09876543210",
                    "codice_destinatario": "ABCDEFG",
                },
            )
        ).json()

        inv = (
            await c.post(
                "/invoices",
                headers=h,
                json={"client_tag_id": client["id"], "year": 2026},
            )
        ).json()
        assert inv["number"] is None  # not allocated until transmit
        await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "consulting", "unit_price": "100.00", "quantity": "2"},
        )
        tx = await c.post(f"/invoices/{inv['id']}/transmit", headers=h, json={})
        assert tx.status_code == 200
        body = tx.json()
        assert body["number"] == 1 and body["state"] == "transmitted"
        assert body["total"] == "244.00"  # 200 + 22%
        assert body["conservation_status"] == "out_of_coverage"

        xml = (await c.get(f"/invoices/{inv['id']}/xml", headers=h)).json()["xml"]
        assert "FatturaElettronica" in xml and "FPR12" in xml

        # Immutable after emission.
        late = await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "late", "unit_price": "1"},
        )
        assert late.status_code == 409

        cn = await c.post(
            "/invoices/credit-note",
            headers=h,
            json={"parent_invoice_id": inv["id"]},
        )
        assert cn.status_code == 200 and cn.json()["document_type"] == "TD04"

        paid = await c.post(f"/invoices/{inv['id']}/paid", headers=h)
        assert paid.status_code == 200 and paid.json()["payment_status"] == "paid"

        cross = await c.get(
            "/invoices",
            headers={"Authorization": f"Bearer {a['token']}", "X-Org-Id": b["org_id"]},
        )
        assert cross.status_code == 403
