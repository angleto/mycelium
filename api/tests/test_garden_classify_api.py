"""GET /garden/classify/{node_id} + POST /garden/apply (ADR-0032).

Thin-adapter tests: the heuristics are unit-tested in
``core/tests/test_garden_classify.py`` / ``test_garden_apply.py``; here we
pin the HTTP contract — response shape, the ``kinds`` filter, 404 on a
non-note id, accept/reject round-trip, and the Pydantic guard that an
``auto`` action (worker-only) is refused at the boundary.
"""

from __future__ import annotations

import datetime
import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.db import tenant_session
from mycelium_core.services import garden_classify as gc


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, ws: str = "GC") -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": ws},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _make_note(c: AsyncClient, h: dict[str, str], title: str) -> str:
    r = await c.post("/notes", headers=h, json={"kind": "text", "title": title, "text": title})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _tag(c: AsyncClient, h: dict[str, str], name: str) -> str:
    r = await c.post("/tags", headers=h, json={"kind": "generic", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_classify_returns_well_formed_shape() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _make_note(c, h, "n")
        r = await c.get(f"/garden/classify/{nid}", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["node_id"] == nid
    assert body["node_kind"] == "note"
    assert body["tags"] == [] and body["links"] == []
    assert body["maturity"] is None  # default seed -> not a candidate
    assert body["model_version"] == "garden-classify-v1"
    assert isinstance(body["signals_used"], list)


async def test_classify_kinds_filter_limits_signals() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _make_note(c, h, "n")
        r = await c.get(f"/garden/classify/{nid}", headers=h, params={"kinds": "tags"})
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["maturity"] is None
    assert body["cluster"] is None


async def test_classify_unknown_node_is_404() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get(f"/garden/classify/{uuid.uuid4()}", headers=h)
        assert r.status_code == 404, r.text


async def test_apply_accept_tag_round_trip() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _make_note(c, h, "n")
        tid = await _tag(c, h, "topic")
        r = await c.post(
            "/garden/apply",
            headers=h,
            json={
                "node_id": nid,
                "suggestion_type": "tag",
                "suggestion_value": {"tag_id": tid},
                "action": "accept",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] is True
        assert body["action"] == "accept"
        uuid.UUID(body["feedback_id"])  # well-formed id
        # The tag is now on the note (verified through the public read path).
        note = (await c.get(f"/notes/{nid}", headers=h)).json()
    assert any(t["id"] == tid for t in note.get("tags", []))


async def test_apply_reject_does_not_apply() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _make_note(c, h, "n")
        tid = await _tag(c, h, "nope")
        r = await c.post(
            "/garden/apply",
            headers=h,
            json={
                "node_id": nid,
                "suggestion_type": "tag",
                "suggestion_value": {"tag_id": tid},
                "action": "reject",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["applied"] is False
        note = (await c.get(f"/notes/{nid}", headers=h)).json()
    assert not any(t["id"] == tid for t in note.get("tags", []))


async def test_classify_source_is_live_without_cache() -> None:
    """ADR-0042 D6: with no persisted cache the read recomputes live and
    labels the response ``source = live``."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _make_note(c, h, "n")
        body = (await c.get(f"/garden/classify/{nid}", headers=h)).json()
    assert body["source"] == "live"


async def test_classify_serves_precomputed_then_refresh_forces_live() -> None:
    """ADR-0042 D6: a fresh persisted cache is served as ``source =
    precomputed``; ``refresh=true`` bypasses it and recomputes live."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        signup = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "GC"},
            )
        ).json()
        h = {"Authorization": f"Bearer {signup['token']}", "X-Workspace-Id": signup["workspace_id"]}
        org = uuid.UUID(signup["workspace_id"])
        user = uuid.UUID(signup["user_id"])
        nid = await _make_note(c, h, "n")
        tid = await _tag(c, h, "topic")
        # Seed the cache with a hand-built result so the precomputed path is
        # deterministic (an empty note would cache zero rows -> read None).
        seeded = gc.ClassifyResult(
            node_id=uuid.UUID(nid),
            node_kind="note",
            tags=[gc.TagSuggestion(tag_id=uuid.UUID(tid), confidence=0.9, rationale="seed")],
            links=[],
            maturity=None,
            cluster=None,
            signals_used=["seed"],
            model_version=gc.MODEL_VERSION,
            generated_at=datetime.datetime.now(datetime.UTC),
        )
        async with tenant_session(str(org), str(user)) as s:
            await gc.persist_classification(
                s, org_id=org, node_kind="note", node_id=uuid.UUID(nid), result=seeded
            )
        precomputed = (await c.get(f"/garden/classify/{nid}", headers=h)).json()
        live = (
            await c.get(f"/garden/classify/{nid}", headers=h, params={"refresh": "true"})
        ).json()
    assert precomputed["source"] == "precomputed"
    assert [t["tag_id"] for t in precomputed["tags"]] == [tid]  # served from the cache
    assert live["source"] == "live"


async def test_apply_rejects_auto_action_at_boundary() -> None:
    # 'auto' is worker-only; the API Literal must refuse it (422), so a
    # client cannot forge a system promotion event.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _make_note(c, h, "n")
        r = await c.post(
            "/garden/apply",
            headers=h,
            json={
                "node_id": nid,
                "suggestion_type": "maturity",
                "suggestion_value": {"value": "mature"},
                "action": "auto",
            },
        )
    assert r.status_code == 422, r.text
