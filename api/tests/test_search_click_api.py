"""POST /search/click (ADR-0035 recall_at_k, task 89508ca9).

The endpoint is fire-and-forget telemetry: 204 on success, 422 on a
shape violation (pydantic), 400 on an incoherent event (rank below the
shown count). The captured row unblocks the ``recall_at_k`` sensor,
asserted end-to-end through GET /garden/health.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, name: str = "A") -> dict[str, str]:
    r = await c.post(
        "/auth/signup",
        json={
            "email": _email(),
            "password": "pw-strong-123",
            "workspace_name": name,
        },
    )
    a = r.json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }


def _click_body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "q": "potatura del melo",
        "hit_kind": "note",
        "hit_id": str(uuid.uuid4()),
        "rank": 1,
        "result_count": 8,
    }
    body.update(over)
    return body


async def test_click_is_captured_and_unblocks_recall_sensor() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.post("/search/click", headers=h, json=_click_body())
        assert r.status_code == 204

        # A probe click must not contribute to the sensor.
        r = await c.post("/search/click", headers=h, json=_click_body(rank=2, is_probe=True))
        assert r.status_code == 204

        health = (await c.get("/garden/health", headers=h)).json()
        recall = health["metrics"]["recall_at_k"]
        assert recall["value"] == 1.0
        assert recall["reason"] is None


async def test_click_validation() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # Shape violations -> 422 from pydantic.
        assert (
            await c.post("/search/click", headers=h, json=_click_body(rank=0))
        ).status_code == 422
        assert (await c.post("/search/click", headers=h, json=_click_body(q=""))).status_code == 422
        # Coherence violation (clicked rank beyond the shown count) -> 400.
        assert (
            await c.post("/search/click", headers=h, json=_click_body(rank=9, result_count=3))
        ).status_code == 400
        # Unknown hit kind -> 400 (domain validation, not pydantic).
        assert (
            await c.post("/search/click", headers=h, json=_click_body(hit_kind="banana"))
        ).status_code == 400


async def test_click_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/search/click", json=_click_body())
        assert r.status_code in (401, 403)
