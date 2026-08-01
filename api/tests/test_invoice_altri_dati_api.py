"""AltriDatiGestionali over HTTP (FatturaPA 2.2.1.16).

The line bodies carry an optional ``altri_dati`` list (absent = nothing
emitted, the normal case) and there is a dedicated REPLACE endpoint for
editing the set on its own. Draft-only, like every other line edit.

The database is shared, so every assertion is scoped to the invoice this
test created.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_altri_dati_http_surface() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "ADG"},
            )
        ).json()
        # Issuer-profile writes need the effective role admin (the header
        # is clamped to the membership).
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        await c.post(
            "/issuer-profiles",
            headers=h,
            json={
                "label": "Principale",
                "legal_name": "Acme Srl",
                "vat_number": "01234567890",
                "address": "Via Roma 1",
                "postal_code": "00100",
                "city": "Roma",
            },
        )
        client = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "Client SpA",
                    "legal_name": "Client SpA",
                    "country_code": "IT",
                    "vat_number": "09876543210",
                    "sdi_code": "ABCDEFG",
                    "address": "Via Milano 2",
                    "postal_code": "20100",
                    "city": "Milano",
                },
            )
        ).json()
        inv = (
            await c.post("/invoices", headers=h, json={"client_tag_id": client["id"], "year": 2026})
        ).json()

        # Omitted -> empty. Nothing is emitted for the ordinary line.
        plain = await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={"description": "consulenza", "unit_price": "100.00", "quantity": "1"},
        )
        assert plain.status_code == 200
        assert plain.json()["altri_dati"] == []

        # A full block (all four elements) on create.
        created = await c.post(
            f"/invoices/{inv['id']}/lines",
            headers=h,
            json={
                "description": "documento commerciale",
                "unit_price": "50.00",
                "quantity": "1",
                "altri_dati": [
                    {
                        "tipo_dato": "N.DOC.COMM",
                        "riferimento_testo": "DC-2026-0001",
                        "riferimento_numero": "17.25",
                        "riferimento_data": "2026-03-01",
                    }
                ],
            },
        )
        assert created.status_code == 200
        ln = created.json()
        (blk,) = ln["altri_dati"]
        assert blk["tipo_dato"] == "N.DOC.COMM"
        assert blk["riferimento_testo"] == "DC-2026-0001"
        assert Decimal(blk["riferimento_numero"]) == Decimal("17.25")
        assert blk["riferimento_data"] == "2026-03-01"

        # The line listing carries the blocks per line (one query, but
        # what matters here is the payload shape).
        listed = (await c.get(f"/invoices/{inv['id']}/lines", headers=h)).json()
        by_id = {x["id"]: x for x in listed}
        assert by_id[plain.json()["id"]]["altri_dati"] == []
        assert len(by_id[ln["id"]]["altri_dati"]) == 1

        # The structured preview (what the SPA renders as the courtesy
        # document) carries them too: filled in means visible.
        prev = (await c.get(f"/invoices/{inv['id']}/preview", headers=h)).json()
        prev_by_desc = {x["description"]: x for x in prev["lines"]}
        assert prev_by_desc["consulenza"]["altri_dati"] == []
        assert [b["tipo_dato"] for b in prev_by_desc["documento commerciale"]["altri_dati"]] == [
            "N.DOC.COMM"
        ]

        # PUT of the line WITHOUT altri_dati leaves the blocks alone: a
        # price fix must not silently drop them.
        kept = await c.put(
            f"/invoices/{inv['id']}/lines/{ln['id']}",
            headers=h,
            json={"description": "documento commerciale", "unit_price": "60.00", "quantity": "1"},
        )
        assert kept.status_code == 200
        assert len(kept.json()["altri_dati"]) == 1

        # PUT of the line WITH altri_dati: the path the SPA takes when a
        # user edits an existing line and adds blocks there. The other
        # PUT case above only proved that OMITTING the field preserves
        # them, which says nothing about SETTING them this way.
        added = await c.put(
            f"/invoices/{inv['id']}/lines/{plain.json()['id']}",
            headers=h,
            json={
                "description": "consulenza",
                "unit_price": "10.00",
                "quantity": "1",
                "altri_dati": [{"tipo_dato": "INTENTO", "riferimento_testo": "PROT-1/000001"}],
            },
        )
        assert added.status_code == 200, added.text
        assert [b["tipo_dato"] for b in added.json()["altri_dati"]] == ["INTENTO"]
        # ...and it survives a re-read, which is what the user checks.
        again = await c.get(f"/invoices/{inv['id']}/lines", headers=h)
        fresh = {r["id"]: r for r in again.json()}[plain.json()["id"]]
        assert [b["tipo_dato"] for b in fresh["altri_dati"]] == ["INTENTO"]

        # The dedicated REPLACE endpoint: the caller sends the full,
        # ordered set. NB3 leaves the three reference fields empty.
        rep = await c.put(
            f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati",
            headers=h,
            json=[
                {"tipo_dato": "INTENTO", "riferimento_testo": "08060120341234567-000001"},
                {"tipo_dato": "NB3"},
            ],
        )
        assert rep.status_code == 200
        assert [b["tipo_dato"] for b in rep.json()] == ["INTENTO", "NB3"]
        assert rep.json()[1]["riferimento_testo"] is None
        got = await c.get(f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati", headers=h)
        assert got.status_code == 200
        assert [b["tipo_dato"] for b in got.json()] == ["INTENTO", "NB3"]

        # Rejections are coded 400s (the XSD facets), not 500s.
        bad_tipo = await c.put(
            f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati",
            headers=h,
            json=[{"tipo_dato": "INTENTÒ"}],  # not Basic-Latin
        )
        assert bad_tipo.status_code == 400
        assert bad_tipo.json()["code"] == "invoice.altri_dati_invalid"
        bad_numero = await c.put(
            f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati",
            headers=h,
            json=[{"tipo_dato": "X", "riferimento_numero": "1.123456789"}],
        )
        assert bad_numero.status_code == 400
        # Pydantic guards the coarse bounds before the service is reached.
        too_long = await c.put(
            f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati",
            headers=h,
            json=[{"tipo_dato": "ABCDEFGHIJK"}],
        )
        assert too_long.status_code == 422
        # A refused write changed nothing.
        assert [
            b["tipo_dato"]
            for b in (
                await c.get(f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati", headers=h)
            ).json()
        ] == ["INTENTO", "NB3"]

        # A line id that is not on this invoice is a 404, not an empty list.
        assert (
            await c.get(f"/invoices/{inv['id']}/lines/{uuid.uuid4()}/altri-dati", headers=h)
        ).status_code == 404

        # [] clears the set.
        assert (
            await c.put(f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati", headers=h, json=[])
        ).json() == []

        # Immutable after emission (ADR-0009): the edit is refused, the
        # read still works.
        keep = await c.put(
            f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati",
            headers=h,
            json=[{"tipo_dato": "NB3"}],
        )
        assert keep.status_code == 200
        tx = await c.post(f"/invoices/{inv['id']}/transmit", headers=h, json={})
        assert tx.status_code == 200 and tx.json()["state"] == "transmitted"
        late = await c.put(
            f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati",
            headers=h,
            json=[{"tipo_dato": "INTENTO"}],
        )
        assert late.status_code == 409
        assert late.json()["code"] == "invoice.not_draft"
        still = await c.get(f"/invoices/{inv['id']}/lines/{ln['id']}/altri-dati", headers=h)
        assert [b["tipo_dato"] for b in still.json()] == ["NB3"]
