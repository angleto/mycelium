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
        gused = (
            await c.post("/tags", headers=h, json={"kind": "generic", "name": "used-A"})
        ).json()
        gunused = (
            await c.post("/tags", headers=h, json={"kind": "generic", "name": "unused"})
        ).json()
        # A task that belongs to client A's project, tagged with the
        # 'used-A' generic: that generic is now "used within the focus".
        tk = (await c.post("/tasks", headers=h, json={"title": "TA"})).json()
        await c.post(f"/tasks/{tk['id']}/tags", headers=h, json={"tag_id": pa["id"]})
        await c.post(f"/tasks/{tk['id']}/tags", headers=h, json={"tag_id": gused["id"]})

        scoped = (await c.get("/tags", headers=h, params={"for_client": ca["id"]})).json()
        ids = {t["id"] for t in scoped}

        assert ca["id"] in ids  # the focused client
        assert pa["id"] in ids  # its project
        assert gused["id"] in ids  # generic USED within the focus
        assert cb["id"] not in ids  # other client — was the bug
        assert pb["id"] not in ids  # other client's project — was the bug
        # The "Filter by tags" leak (reported 4x): a generic tag not
        # used anywhere in the focus must NOT appear under it.
        assert gunused["id"] not in ids

        # No focus → everything is visible (unchanged behaviour).
        allids = {t["id"] for t in (await c.get("/tags", headers=h)).json()}
        assert {
            ca["id"],
            cb["id"],
            pa["id"],
            pb["id"],
            gused["id"],
            gunused["id"],
        } <= allids

        # Manager surface (manage=true): under the same client focus a
        # GLOBAL generic (no scope rows) must reappear, since the manager
        # is where its "Restrict to..." is added — the reported bug. The
        # cross-client structural leak must STAY closed even here.
        managed = (
            await c.get(
                "/tags",
                headers=h,
                params={"for_client": ca["id"], "manage": "true"},
            )
        ).json()
        mids = {t["id"] for t in managed}
        assert gunused["id"] in mids  # global generic — the fix
        assert gused["id"] in mids  # still used within the focus
        assert ca["id"] in mids  # the focused client
        assert pa["id"] in mids  # its project
        assert cb["id"] not in mids  # other client — leak stays closed
        assert pb["id"] not in mids  # other client's project — stays closed
