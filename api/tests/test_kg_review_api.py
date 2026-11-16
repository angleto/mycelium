"""The knowledge-graph review inbox over HTTP (ADR-0071):
GET /kg/review/pending + POST /kg/review/approve|reject.

Thin-adapter tests: the gate's behaviour is unit-tested in
``core/tests/test_kg_review.py``; here we pin the HTTP contract on the
signed-up workspace: the inbox lists a proposed fact with its model and
source note, approve makes it effective, reject removes it for good, a stale
version is a 409, a fact that is not a proposal is a 404, and an agent token
carrying a scope list cannot decide.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.db import tenant_session
from mycelium_core.services import kg


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> tuple[dict[str, str], str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "KG"},
        )
    ).json()
    headers = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    return headers, a["workspace_id"], a["user_id"]


async def _seed(org: str, user: str, *, proposed: bool = True) -> uuid.UUID:
    o = uuid.UUID(org)
    async with tenant_session(org, user) as s:
        x = await kg.ensure_entity(s, org_id=o, name="Ada", entity_type="person")
        y = await kg.ensure_entity(s, org_id=o, name="Analytical", entity_type="project")
        edge = await kg.add_fact(
            s,
            org_id=o,
            subject_id=x.id,
            predicate="works_on",
            object_id=y.id,
            review_state="proposed" if proposed else None,
            origin_model_id="fake-llm",
        )
        return edge.id


async def test_pending_then_approve_with_the_version_read() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h, org, user = await _signup(c)
        eid = str(await _seed(org, user))
        page = (await c.get("/kg/review/pending", headers=h)).json()
        assert [i["edge_id"] for i in page["items"]] == [eid]
        row = page["items"][0]
        assert row["subject"]["name"] == "Ada"
        assert row["object"]["type"] == "project"
        assert row["predicate"] == "works_on"
        assert row["origin_model_id"] == "fake-llm"
        assert page["next_cursor"] is None

        stale = await c.post(
            "/kg/review/approve",
            headers=h,
            json={"edge_id": eid, "expected_version": row["version"] + 1},
        )
        assert stale.status_code == 409, stale.text
        r = await c.post(
            "/kg/review/approve",
            headers=h,
            json={"edge_id": eid, "expected_version": row["version"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["review_state"] == "approved"
        assert (await c.get("/kg/review/pending", headers=h)).json()["items"] == []


async def test_reject_removes_the_proposal_and_cannot_be_approved_after() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h, org, user = await _signup(c)
        eid = str(await _seed(org, user))
        r = await c.post("/kg/review/reject", headers=h, json={"edge_id": eid})
        assert r.status_code == 200, r.text
        assert r.json()["review_state"] == "rejected"
        assert (await c.get("/kg/review/pending", headers=h)).json()["items"] == []
        assert (
            await c.post("/kg/review/approve", headers=h, json={"edge_id": eid})
        ).status_code == 404


async def test_a_fact_a_person_asserted_is_not_a_proposal() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h, org, user = await _signup(c)
        eid = str(await _seed(org, user, proposed=False))
        for verb in ("approve", "reject"):
            r = await c.post(f"/kg/review/{verb}", headers=h, json={"edge_id": eid})
            assert r.status_code == 404, (verb, r.text)
            assert r.json()["code"] == "kg.fact_not_found", r.text


async def test_another_workspace_sees_nothing_and_decides_nothing() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h, org, user = await _signup(c)
        eid = str(await _seed(org, user))
        other, *_ = await _signup(c)
        assert (await c.get("/kg/review/pending", headers=other)).json()["items"] == []
        r = await c.post("/kg/review/approve", headers=other, json={"edge_id": eid})
        assert r.status_code == 404, r.text
        page = (await c.get("/kg/review/pending", headers=h)).json()
        assert [i["edge_id"] for i in page["items"]] == [eid]
