"""API surface of ``purge_client``/``purge_project``: the DELETE
endpoints on ``/clients/{tag_id}`` and ``/projects/{tag_id}``.

Covers the precondition (must be archived) and the recursion into
projects when purging a client. The fine-grained subgraph + invoice
cascade is covered at the service layer (``core/tests/test_taxonomy_purge``).
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_delete_project_endpoint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        s = (
            await c.post(
                "/auth/signup",
                json={
                    "email": _email(),
                    "password": "pw-strong-123",
                    "workspace_name": "W",
                },
            )
        ).json()
        h = {
            "Authorization": f"Bearer {s['token']}",
            "X-Workspace-Id": s["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        cl = (
            await c.post(
                "/clients",
                headers=h,
                json={"name": "Cli", "ragione_sociale": "Cli SRL"},
            )
        ).json()
        pr = (
            await c.post(
                "/projects",
                headers=h,
                json={"name": "ToPurge", "client_tag_id": cl["id"]},
            )
        ).json()
        # Active project: delete must refuse.
        bad = await c.delete(f"/projects/{pr['id']}", headers=h)
        assert bad.status_code == 400
        assert bad.json()["code"] == "tag.not_archived"

        # Archive then purge.
        ok = await c.patch(
            f"/tags/{pr['id']}",
            headers=h,
            json={"expected_version": pr["version"], "status": "archived"},
        )
        assert ok.status_code == 200
        gone = await c.delete(f"/projects/{pr['id']}", headers=h)
        assert gone.status_code == 204
        # Listing no longer surfaces it.
        rows = (await c.get("/projects", headers=h)).json()
        assert all(r["id"] != pr["id"] for r in rows)


async def test_delete_client_recurses_projects() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        s = (
            await c.post(
                "/auth/signup",
                json={
                    "email": _email(),
                    "password": "pw-strong-123",
                    "workspace_name": "W2",
                },
            )
        ).json()
        h = {
            "Authorization": f"Bearer {s['token']}",
            "X-Workspace-Id": s["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        cl = (
            await c.post(
                "/clients",
                headers=h,
                json={"name": "DoomedCo", "ragione_sociale": "DoomedCo SRL"},
            )
        ).json()
        p1 = (
            await c.post(
                "/projects",
                headers=h,
                json={"name": "P1", "client_tag_id": cl["id"]},
            )
        ).json()
        # Archive the client and delete it; child project is purged too
        # (purge_client recurses).
        await c.patch(
            f"/tags/{cl['id']}",
            headers=h,
            json={"expected_version": cl["version"], "status": "archived"},
        )
        gone = await c.delete(f"/clients/{cl['id']}", headers=h)
        assert gone.status_code == 204
        clients = (await c.get("/clients", headers=h)).json()
        assert all(r["id"] != cl["id"] for r in clients)
        projects = (await c.get("/projects", headers=h)).json()
        assert all(r["id"] != p1["id"] for r in projects)
