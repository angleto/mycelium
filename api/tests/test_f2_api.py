"""F2 API end-to-end (DB-backed): custom workflow + project override,
dependencies, cycle rejection, graph + blocked overlay, isolation."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f2_api_flow() -> None:
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

        w = (
            await c.post(
                "/workflows",
                headers=h,
                json={
                    "name": "Simple",
                    "states": [
                        {"name": "open", "ord": 1, "is_initial": True},
                        {"name": "closed", "ord": 2, "is_terminal": True},
                    ],
                    "transitions": [{"from_state": "open", "to_state": "closed"}],
                },
            )
        ).json()
        pr = (await c.post("/projects", headers=h, json={"name": "P"})).json()
        ow = await c.patch(
            f"/projects/{pr['id']}/workflow",
            headers=h,
            json={"expected_version": 1, "workflow_id": w["id"]},
        )
        assert ow.status_code == 200 and ow.json()["version"] == 2

        t1 = (await c.post("/tasks", headers=h, json={"title": "T1", "tag_ids": [pr["id"]]})).json()
        t2 = (await c.post("/tasks", headers=h, json={"title": "T2", "tag_ids": [pr["id"]]})).json()
        st = {
            s["name"]: s["id"] for s in (await c.get(f"/tasks/{t1['id']}/states", headers=h)).json()
        }
        assert set(st) == {"open", "closed"}

        dep = await c.post(
            "/dependencies",
            headers=h,
            json={
                "predecessor_id": t1["id"],
                "successor_id": t2["id"],
                "type": "FS",
            },
        )
        assert dep.status_code == 200 and dep.json()["type"] == "FS"

        cyc = await c.post(
            "/dependencies",
            headers=h,
            json={
                "predecessor_id": t2["id"],
                "successor_id": t1["id"],
                "type": "FS",
            },
        )
        assert cyc.status_code == 400
        assert cyc.json()["code"] == "dependency.cycle"

        deps = (await c.get(f"/dependencies?task_id={t1['id']}", headers=h)).json()
        assert len(deps) == 1

        g = (await c.get("/graph", headers=h)).json()
        nb = {n["id"]: n["blocked"] for n in g["nodes"]}
        assert nb[t2["id"]] is True and nb[t1["id"]] is False
        assert len(g["edges"]) == 1 and g["edges"][0]["type"] == "FS"

        moved = await c.post(
            f"/tasks/{t1['id']}/state",
            headers=h,
            json={"expected_version": t1["version"], "state_id": st["closed"]},
        )
        assert moved.status_code == 200
        g2 = (await c.get("/graph", headers=h)).json()
        assert {n["id"]: n["blocked"] for n in g2["nodes"]}[t2["id"]] is False

        cross = await c.get(
            "/graph",
            headers={"Authorization": f"Bearer {a['token']}", "X-Org-Id": b["org_id"]},
        )
        assert cross.status_code == 403
