"""API surface for the ADR-0037 learning follow-up (task ea2156df):
POST /garden/learning/rollback + GET /garden/learning/telemetry.

Wiring / auth / serialization + RLS scoping; the deep rollback maths are
covered at the service level in core/tests/test_garden_learning_rollback.py.
"""

from __future__ import annotations

import datetime
import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "L"},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _make_note(c: AsyncClient, h: dict[str, str]) -> str:
    r = await c.post("/notes", headers=h, json={"kind": "text", "title": "n", "text": "n"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _reject_tag(c: AsyncClient, h: dict[str, str], node_id: str, tag_id: str) -> None:
    r = await c.post(
        "/garden/apply",
        headers=h,
        json={
            "node_id": node_id,
            "suggestion_type": "tag",
            "suggestion_value": {"tag_id": tag_id},
            "action": "reject",
        },
    )
    assert r.status_code == 200, r.text


async def test_telemetry_reports_reject_hotspots() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        node = await _make_note(c, h)
        tag_x, tag_y = str(uuid.uuid4()), str(uuid.uuid4())
        await _reject_tag(c, h, node, tag_x)
        await _reject_tag(c, h, node, tag_x)
        await _reject_tag(c, h, node, tag_y)

        body = (await c.get("/garden/learning/telemetry", headers=h)).json()
        hot = {r["feature_key"]: r["declines"] for r in body["reject_hotspots"]}
        assert hot[f"tag:{tag_x}"] == 2
        assert hot[f"tag:{tag_y}"] == 1
        # No baseline snapshot yet -> drift is empty, not an error.
        assert body["drift"] == []


async def test_rollback_endpoint_returns_a_diff_and_is_self_scoped() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        node = await _make_note(c, h)
        await _reject_tag(c, h, node, str(uuid.uuid4()))
        to = datetime.datetime.now(datetime.UTC).isoformat()
        r = await c.post("/garden/learning/rollback", headers=h, json={"to": to})
        assert r.status_code == 200, r.text
        body = r.json()
        # No snapshot in the API path -> degrades to a truncated replay to
        # 'now', which reproduces the current state: a well-formed no-op diff.
        assert body["rolled_back_to"] is not None
        assert body["snapshot_at"] is None
        assert "summary" in body and isinstance(body["features_changed"], int)


async def test_telemetry_is_cross_tenant_isolated() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h_a = await _signup(c)
        h_b = await _signup(c)
        node_a = await _make_note(c, h_a)
        leaked = str(uuid.uuid4())
        await _reject_tag(c, h_a, node_a, leaked)
        # Workspace B sees none of A's reject history.
        body_b = (await c.get("/garden/learning/telemetry", headers=h_b)).json()
        assert body_b["reject_hotspots"] == []
