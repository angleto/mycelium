"""GET /garden/candidates (task 4995a32f): the distillation-candidate read
surface. Smoke + shape + gating (no auth -> 401)."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_garden_candidates_smoke_and_shape() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}

        r = await c.get("/garden/candidates", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) == {"nodes", "edges"}
        assert isinstance(body["nodes"], list)
        assert isinstance(body["edges"], list)

        # kind filter is accepted and validated
        r2 = await c.get("/garden/candidates", headers=h, params={"kind": "link_add"})
        assert r2.status_code == 200, r2.text
        r3 = await c.get("/garden/candidates", headers=h, params={"kind": "bogus"})
        assert r3.status_code == 422  # Literal enum rejects unknown kinds


async def test_garden_candidates_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/garden/candidates")
        assert r.status_code == 401
