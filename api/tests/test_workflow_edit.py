"""Workflow edit / delete / set-default + the >=1 invariant."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_workflow_edit_default_delete() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        # Owner with full entitlement: workflow writes need the
        # effective role admin, which is X-Workspace-Role clamped to
        # the membership (absent header => member, least privilege).
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        wfs = (await c.get("/workflows", headers=h)).json()
        default = next(w for w in wfs if w["is_default"])

        # Default cannot be deleted (keeps the >=1 invariant).
        r = await c.delete(f"/workflows/{default['id']}", headers=h)
        assert r.status_code == 400

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

        st = (await c.get(f"/workflows/{w['id']}/states", headers=h)).json()
        open_id = next(s["id"] for s in st if s["name"] == "open")

        # Edit: rename, add a state, keep "open" by id, new transition.
        r = await c.patch(
            f"/workflows/{w['id']}",
            headers=h,
            json={
                "name": "Simple v2",
                "states": [
                    {"id": open_id, "name": "open", "ord": 0, "is_initial": True},
                    {"name": "wip", "ord": 1},
                    {"name": "closed", "ord": 2, "is_terminal": True},
                ],
                "transitions": [
                    {"from_state": "open", "to_state": "wip"},
                    {"from_state": "wip", "to_state": "closed"},
                ],
            },
        )
        assert r.status_code == 204
        names = {s["name"] for s in (await c.get(f"/workflows/{w['id']}/states", headers=h)).json()}
        assert names == {"open", "wip", "closed"}
        tr = (await c.get(f"/workflows/{w['id']}/transitions", headers=h)).json()
        assert len(tr) == 2
        assert (
            next(x for x in (await c.get("/workflows", headers=h)).json() if x["id"] == w["id"])[
                "name"
            ]
            == "Simple v2"
        )

        # Promote it to default; the old default is demoted.
        assert (await c.post(f"/workflows/{w['id']}/default", headers=h)).status_code == 204
        wfs = (await c.get("/workflows", headers=h)).json()
        assert [x["is_default"] for x in wfs].count(True) == 1
        assert next(x for x in wfs if x["id"] == w["id"])["is_default"] is True

        # The previous default is now deletable (not default, no tasks).
        r = await c.delete(f"/workflows/{default['id']}", headers=h)
        assert r.status_code == 204
