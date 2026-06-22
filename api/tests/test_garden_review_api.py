"""Review-inbox endpoints (ADR-0043, task e87daff4):
GET /garden/review/pending + POST /garden/review/approve|reject.

Thin-adapter tests: the gate's behaviour is unit-tested in
``core/tests/test_garden_review.py``; here we pin the HTTP contract. A
``proposed`` node is only ever born from the AUTONOMOUS path, so the fixture
generates one through the real decomposition service (``autonomous=True``
under the gate) on the signed-up workspace, then drives the endpoints over
HTTP: the inbox lists it WITH its producing model, approve makes it
retrievable, reject 404s it, and a non-proposed note is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_ai import FakeLLM
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.ai_providers import set_llm_override
from flow_core.config import get_settings
from flow_core.db import tenant_session
from flow_core.embedder import set_embedder_override
from flow_core.models.note import NoteKind
from flow_core.services import decomposition as decomp
from flow_core.services import notes as nt


@pytest.fixture
def _wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    set_llm_override(FakeLLM)
    set_embedder_override(FakeEmbedder)
    monkeypatch.setattr(get_settings(), "garden_review_gate_enabled", True)
    try:
        yield
    finally:
        set_llm_override(None)
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> tuple[dict[str, str], str, str]:
    """Returns ``(headers, workspace_id, user_id)`` for the new workspace."""
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "REV"},
        )
    ).json()
    headers = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }
    return headers, a["workspace_id"], a["user_id"]


async def _seed_proposed(org: str, user: str) -> uuid.UUID:
    """Generate a ``proposed`` pattern humus note the way the autonomous sweep
    would, directly on the workspace the API test signed up."""
    async with tenant_session(org, user) as s:
        ids = []
        for i in range(3):
            n = await nt.create_note(
                s,
                org_id=uuid.UUID(org),
                actor_id=uuid.UUID(user),
                kind=NoteKind.text,
                title=f"src{i}",
                text=f"compost forecast synthesis source {i}",
            )
            n.is_archived = True
            ids.append(n.id)
        await s.flush()
        res = await decomp.extract_cluster_pattern(
            s, org_id=uuid.UUID(org), actor_id=uuid.UUID(user), source_note_ids=ids, autonomous=True
        )
    return res.note_id


async def test_pending_lists_with_model_then_approve_makes_effective(_wire: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))

        # The proposed note is hidden from the normal read path...
        assert (await c.get(f"/notes/{nid}", headers=h)).status_code == 404
        # ...but present in the review inbox, with its producing model.
        pending = (await c.get("/garden/review/pending", headers=h)).json()
        row = next(p for p in pending if p["note_id"] == nid)
        assert row["origin_model_id"] == "fake-llm"
        assert row["humus_kind"] == "pattern"
        assert row["preview"]

        # Approve -> effective.
        r = await c.post("/garden/review/approve", headers=h, json={"note_id": nid})
        assert r.status_code == 200, r.text
        assert r.json()["review_state"] == "approved"
        assert r.json()["rejected"] is False
        assert (await c.get(f"/notes/{nid}", headers=h)).status_code == 200
        after = (await c.get("/garden/review/pending", headers=h)).json()
        assert nid not in {p["note_id"] for p in after}


async def test_reject_soft_deletes_the_proposal(_wire: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))

        r = await c.post(
            "/garden/review/reject", headers=h, json={"note_id": nid, "reason": "weak"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["rejected"] is True
        # Gone from the inbox and from the read path (soft-deleted).
        assert nid not in {
            p["note_id"] for p in (await c.get("/garden/review/pending", headers=h)).json()
        }
        assert (await c.get(f"/notes/{nid}", headers=h)).status_code == 404


async def test_review_of_a_plain_note_is_404(_wire: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _org, _user = await _signup(c)
        n = (await c.post("/notes", headers=h, json={"kind": "text", "text": "ordinary"})).json()
        r = await c.post("/garden/review/approve", headers=h, json={"note_id": n["id"]})
        assert r.status_code == 404


async def test_accept_ratio_endpoint_reflects_an_approval(_wire: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        # No reviews yet -> empty per-model breakdown.
        assert (await c.get("/garden/review/accept-ratio", headers=h)).json() == []
        nid = str(await _seed_proposed(org, user))
        await c.post("/garden/review/approve", headers=h, json={"note_id": nid})
        rows = (await c.get("/garden/review/accept-ratio", headers=h)).json()
        row = next(r for r in rows if r["model_id"] == "fake-llm")
        assert row["approved"] == 1
        assert row["rejected"] == 0
        assert row["ratio"] == 1.0
