"""GET /garden/audit (ADR-0036 event-bus audit panel).

Thin-adapter test: the bus semantics are unit-tested in
``core/tests/test_event_bus.py``; here we pin the HTTP contract -- empty
on a fresh workspace, the window/limit bounds, and that a real apply over
the API lands a propose -> commit chain on the stream with the fields the
SPA renders.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "GA"},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def test_audit_empty_and_bounds() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get("/garden/audit", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == []  # nothing has happened yet
        assert (await c.get("/garden/audit?days=0", headers=h)).status_code == 422
        assert (await c.get("/garden/audit?limit=0", headers=h)).status_code == 422


async def test_audit_shows_apply_chain() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note = (await c.post("/notes", headers=h, json={"kind": "text", "title": "n"})).json()
        ap = await c.post(
            "/garden/apply",
            headers=h,
            json={
                "node_id": note["id"],
                "suggestion_type": "maturity",
                "suggestion_value": {"value": "mature"},
                "action": "accept",
            },
        )
        assert ap.status_code == 200, ap.text
        events = (await c.get("/garden/audit", headers=h)).json()

    for_node = [e for e in events if e["node_id"] == note["id"]]
    kinds = {e["kind"] for e in for_node}
    assert {"propose", "commit"} <= kinds
    commit = next(e for e in for_node if e["kind"] == "commit")
    assert commit["actor_kind"] == "human"
    assert commit["applied_state"] == "committed"
    assert commit["payload"]["action"] == "accept"
    assert commit["parent_event_id"] is not None
