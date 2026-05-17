"""F1 API end-to-end (DB-backed): taxonomy + tasks REST, optimistic
concurrency, i18n error codes, tenant isolation."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f1_api_flow() -> None:
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

        cl = (
            await c.post(
                "/clients",
                headers=h,
                json={"name": "Acme", "ragione_sociale": "Acme SRL"},
            )
        ).json()
        pr = (
            await c.post(
                "/projects",
                headers=h,
                json={"name": "Site", "client_tag_id": cl["id"]},
            )
        ).json()
        task = (
            await c.post("/tasks", headers=h, json={"title": "Do X", "tag_ids": [pr["id"]]})
        ).json()
        assert task["version"] == 1

        listed = (await c.get(f"/tasks?tag_id={pr['id']}", headers=h)).json()
        assert [t["id"] for t in listed] == [task["id"]]

        ok = await c.patch(
            f"/tasks/{task['id']}",
            headers=h,
            json={"expected_version": 1, "title": "Do X2"},
        )
        assert ok.status_code == 200 and ok.json()["version"] == 2

        stale = await c.patch(
            f"/tasks/{task['id']}",
            headers=h,
            json={"expected_version": 1, "title": "stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "concurrency.stale_version"

        states = {
            s["name"]: s["id"]
            for s in (await c.get(f"/tasks/{task['id']}/states", headers=h)).json()
        }
        s3 = await c.post(
            f"/tasks/{task['id']}/state",
            headers=h,
            json={"expected_version": 2, "state_id": states["in_progress"]},
        )
        assert s3.json()["version"] == 3
        s4 = await c.post(
            f"/tasks/{task['id']}/state",
            headers=h,
            json={"expected_version": 3, "state_id": states["done"]},
        )
        assert s4.json()["version"] == 4

        cm = (await c.post(f"/tasks/{task['id']}/comments", headers=h, json={"body": "n"})).json()
        assert (await c.get(f"/tasks/{task['id']}/comments", headers=h)).json()[0]["id"] == cm["id"]

        await c.post(
            f"/tasks/{task['id']}/delete",
            headers=h,
            json={"expected_version": 4},
        )
        assert (await c.get("/tasks", headers=h)).json() == []

        cross = await c.get(
            "/tasks",
            headers={"Authorization": f"Bearer {a['token']}", "X-Org-Id": b["org_id"]},
        )
        assert cross.status_code == 403
        assert cross.json()["code"] == "rbac.no_membership"

        noauth = await c.get("/tasks", headers={"X-Org-Id": a["org_id"]})
        assert noauth.status_code == 401
