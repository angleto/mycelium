"""Regression: under a client focus, the tag catalog must not leak
other clients' client/project tags.

Client and project tags are intrinsically owned and (almost) never
carry TagScope rows, so the scope filter that only knew about TagScope
left every client's client-tag and every project tag visible under any
focus (reported 3x). list_tags now constrains client/project-kind tags
by ownership: only the focused client and its projects; generic tags
keep the global-or-scoped rule.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_client_focus_hides_other_clients_tags() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        # Privileged taxonomy writes need the effective owner role.
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        ca = (
            await c.post(
                "/clients", headers=h, json={"name": "ClientA", "ragione_sociale": "A srl"}
            )
        ).json()
        cb = (
            await c.post(
                "/clients", headers=h, json={"name": "ClientB", "ragione_sociale": "B srl"}
            )
        ).json()
        pa = (
            await c.post(
                "/projects",
                headers=h,
                json={"name": "ProjA", "client_tag_id": ca["id"]},
            )
        ).json()
        pb = (
            await c.post(
                "/projects",
                headers=h,
                json={"name": "ProjB", "client_tag_id": cb["id"]},
            )
        ).json()
        gen = (await c.post("/tags", headers=h, json={"kind": "generic", "name": "urgent"})).json()

        scoped = (await c.get("/tags", headers=h, params={"for_client": ca["id"]})).json()
        ids = {t["id"] for t in scoped}

        assert ca["id"] in ids  # the focused client
        assert pa["id"] in ids  # its project
        assert gen["id"] in ids  # generic (global) tags stay
        assert cb["id"] not in ids  # other client — was the bug
        assert pb["id"] not in ids  # other client's project — was the bug

        # No focus → everything is visible (unchanged behaviour).
        allt = (await c.get("/tags", headers=h)).json()
        allids = {t["id"] for t in allt}
        assert {ca["id"], cb["id"], pa["id"], pb["id"], gen["id"]} <= allids
