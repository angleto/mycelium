"""Fase P (task 561c6aca): the ``protected`` facet over REST.

Round-trip: POST /notes/{id}/protect flips ``NoteOut.protected`` (and
``unprotect`` releases it); a non-atom is refused by
POST /garden/review/restore-source (400, domain error)."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_protect_roundtrip_and_restore_source_guard() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "P"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}

        n = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "prosa", "text": "corpo della prosa finita"},
            )
        ).json()
        assert n["protected"] is False

        r = await c.post(
            f"/notes/{n['id']}/protect",
            headers=h,
            json={"expected_version": n["version"]},
        )
        assert r.status_code == 200, r.text
        got = (await c.get(f"/notes/{n['id']}", headers=h)).json()
        assert got["protected"] is True

        r2 = await c.post(
            f"/notes/{n['id']}/unprotect",
            headers=h,
            json={"expected_version": got["version"]},
        )
        assert r2.status_code == 200, r2.text
        got2 = (await c.get(f"/notes/{n['id']}", headers=h)).json()
        assert got2["protected"] is False

        # A plain note is not a humus atom: restore-source must refuse it.
        r3 = await c.post(
            "/garden/review/restore-source",
            headers=h,
            json={"note_id": n["id"]},
        )
        assert r3.status_code == 400, r3.text
