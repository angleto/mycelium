"""F7 invoice recycle-bin (soft-delete) + archive: the visibility bands
(active / archived / trashed) and the trash/restore/archive/unarchive
actions, plus that a transmitted document is never purged while a trashed
draft can be permanently deleted."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _ids(c: AsyncClient, h: dict[str, str], view: str | None = None) -> list[str]:
    params = {} if view is None else {"view": view}
    rows = (await c.get("/invoices", headers=h, params=params)).json()
    return [x["id"] for x in rows]


async def test_invoice_trash_restore_archive_flow() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
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
        iid = (await c.post("/invoices", headers=h, json={"client_tag_id": client["id"]})).json()[
            "id"
        ]

        # Fresh draft is in the active band only.
        assert iid in await _ids(c, h)
        assert iid not in await _ids(c, h, "archived")
        assert iid not in await _ids(c, h, "trashed")

        # Trash -> leaves active, enters the bin; a direct GET 404s.
        tr = await c.post(f"/invoices/{iid}/trash", headers=h)
        assert tr.status_code == 200 and tr.json()["deleted_at"] is not None
        assert iid not in await _ids(c, h)
        assert iid in await _ids(c, h, "trashed")
        assert (await c.get(f"/invoices/{iid}", headers=h)).status_code == 404

        # Restore -> back to active.
        rs = await c.post(f"/invoices/{iid}/restore", headers=h)
        assert rs.status_code == 200 and rs.json()["deleted_at"] is None
        assert iid in await _ids(c, h)

        # Archive -> leaves active, enters the archive band.
        ar = await c.post(f"/invoices/{iid}/archive", headers=h)
        assert ar.status_code == 200 and ar.json()["is_archived"] is True
        assert iid not in await _ids(c, h)
        assert iid in await _ids(c, h, "archived")

        # Unarchive -> back to active.
        un = await c.post(f"/invoices/{iid}/unarchive", headers=h)
        assert un.status_code == 200 and un.json()["is_archived"] is False
        assert iid in await _ids(c, h)

        # Trash, then permanently delete the draft -> gone from every band.
        await c.post(f"/invoices/{iid}/trash", headers=h)
        assert (await c.delete(f"/invoices/{iid}", headers=h)).status_code == 204
        assert iid not in await _ids(c, h, "trashed")
        assert iid not in await _ids(c, h)


async def test_transmitted_invoice_can_be_trashed_but_not_purged() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
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
        iid = (await c.post("/invoices", headers=h, json={"client_tag_id": client["id"]})).json()[
            "id"
        ]
        await c.post(
            f"/invoices/{iid}/lines",
            headers=h,
            json={"description": "Consulenza", "unit_price": "100.00", "quantity": "1"},
        )
        tx = await c.post(f"/invoices/{iid}/transmit", headers=h, json={})
        assert tx.status_code == 200 and tx.json()["state"] == "transmitted"

        # A transmitted invoice can still be trashed (just hidden)...
        assert (await c.post(f"/invoices/{iid}/trash", headers=h)).status_code == 200
        assert iid in await _ids(c, h, "trashed")
        # ...but never hard-deleted (fiscal record): the draft-only guard 409s.
        assert (await c.delete(f"/invoices/{iid}", headers=h)).status_code == 409
        # It survives in the bin and restores fine.
        assert (await c.post(f"/invoices/{iid}/restore", headers=h)).status_code == 200
        assert iid in await _ids(c, h)
