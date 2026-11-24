"""Review-inbox endpoints (ADR-0043, task e87daff4):
GET /garden/review/pending + POST /garden/review/approve|reject.

Thin-adapter tests: the gate's behaviour is unit-tested in
``core/tests/test_garden_review.py``; here we pin the HTTP contract. Every
synthesis is born ``proposed``, so the fixture generates one through the
real decomposition service on the signed-up workspace, then drives the
endpoints over
HTTP: the inbox lists it WITH its producing model, approve makes it
retrievable, a decline keeps it out of reach of every ordinary door and of
the trash purge, and a non-proposed note is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_ai import FakeLLM
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.ai_providers import set_llm_override
from mycelium_core.db import tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.note import NoteKind
from mycelium_core.services import decomposition as decomp
from mycelium_core.services import notes as nt


@pytest.fixture
def _wire() -> Iterator[None]:
    set_llm_override(FakeLLM)
    set_embedder_override(FakeEmbedder)
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
    """Generate a ``proposed`` pattern humus note directly on the workspace
    the API test signed up."""
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
            s, org_id=uuid.UUID(org), actor_id=uuid.UUID(user), source_note_ids=ids
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


async def test_reject_declines_the_proposal(_wire: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))

        r = await c.post(
            "/garden/review/reject", headers=h, json={"note_id": nid, "reason": "weak"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["rejected"] is True
        # Gone from the inbox and from the read path (declined, ADR-0075).
        assert nid not in {
            p["note_id"] for p in (await c.get("/garden/review/pending", headers=h)).json()
        }
        assert (await c.get(f"/notes/{nid}", headers=h)).status_code == 404


async def test_a_proposal_is_read_whole_and_a_decline_survives_the_trash(_wire: None) -> None:
    """The lifecycle a person walks (ADR-0075), over HTTP.

    Read: the whole proposal and the whole text of its sources, which no
    other door serves -- ``/notes/{id}`` stays 404 in every gated state.
    Decline: the proposal leaves the inbox and lands among the declined,
    and emptying the workspace trash does not touch it, which is the defect
    that lost rejected proposals while they were soft-deletes. Reopen: it is
    pending again. Approve from declined: it becomes an ordinary note. The
    old door, restoring from the trash, answers 404 on a proposal."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))
        assert (await c.get("/garden/review/rejected", headers=h)).json() == []

        reading = await c.get(f"/garden/review/nodes/{nid}", headers=h)
        assert reading.status_code == 200, reading.text
        r = reading.json()
        assert r["review_state"] == "proposed" and r["body"]
        # The pattern was drawn from three archived sources; all three are
        # served whole, which is what the side-by-side reading needs.
        assert sorted(x["title"] for x in r["sources"]) == ["src0", "src1", "src2"]
        assert all(x["body"].startswith("compost forecast") for x in r["sources"])
        assert (await c.get(f"/notes/{nid}", headers=h)).status_code == 404

        dec = await c.post(
            "/garden/review/reject",
            headers=h,
            json={"note_id": nid, "reason": "weak", "expected_version": r["version"]},
        )
        assert dec.status_code == 200, dec.text
        assert dec.json()["review_state"] == "declined" and dec.json()["rejected"] is True
        assert nid not in {
            p["note_id"] for p in (await c.get("/garden/review/pending", headers=h)).json()
        }
        assert (await c.get(f"/notes/{nid}", headers=h)).status_code == 404
        trashed = (await c.get("/notes", headers=h, params={"include_deleted": True})).json()[
            "items"
        ]
        assert nid not in {n["id"] for n in trashed}

        # Emptying the bin is an owner's act, and an owner acts as a member
        # until the SPA steps up with this header.
        emptied = await c.post(
            "/workspaces/me/trash/empty", headers={**h, "X-Workspace-Role": "owner"}
        )
        assert emptied.status_code == 200, emptied.text
        declined = (await c.get("/garden/review/rejected", headers=h)).json()
        assert [d["note_id"] for d in declined] == [nid]
        assert declined[0]["rejected_at"] and declined[0]["preview"]
        again = (await c.get(f"/garden/review/nodes/{nid}", headers=h)).json()
        assert again["review_state"] == "declined" and again["body"] == r["body"]
        assert again["decided_at"] is not None

        assert (
            await c.post(
                f"/notes/{nid}/restore",
                headers=h,
                json={"expected_version": declined[0]["version"]},
            )
        ).status_code == 404

        reopened = await c.post(
            "/garden/review/reopen",
            headers=h,
            json={"note_id": nid, "expected_version": declined[0]["version"]},
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["review_state"] == "proposed"
        assert (await c.get("/garden/review/rejected", headers=h)).json() == []
        assert nid in {
            p["note_id"] for p in (await c.get("/garden/review/pending", headers=h)).json()
        }

        dec2 = await c.post(
            "/garden/review/reject",
            headers=h,
            json={"note_id": nid, "expected_version": reopened.json()["version"]},
        )
        ok = await c.post(
            "/garden/review/approve",
            headers=h,
            json={"note_id": nid, "expected_version": dec2.json()["version"]},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["review_state"] == "approved"
        assert (await c.get(f"/notes/{nid}", headers=h)).status_code == 200


async def test_removing_a_declined_proposal_is_the_one_irreversible_act(_wire: None) -> None:
    """A pending proposal cannot be removed unread; a declined one can, and
    then it is gone from every review surface."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))
        v = (await c.get(f"/garden/review/nodes/{nid}", headers=h)).json()["version"]
        pending_removal = await c.post(
            "/garden/review/remove", headers=h, json={"note_id": nid, "expected_version": v}
        )
        assert pending_removal.status_code == 404

        dec = await c.post(
            "/garden/review/reject", headers=h, json={"note_id": nid, "expected_version": v}
        )
        v2 = dec.json()["version"]
        stale = await c.post(
            "/garden/review/remove", headers=h, json={"note_id": nid, "expected_version": v}
        )
        assert stale.status_code == 409
        gone = await c.post(
            "/garden/review/remove", headers=h, json={"note_id": nid, "expected_version": v2}
        )
        assert gone.status_code == 200, gone.text
        assert gone.json() == {"note_id": nid, "removed": True}
        assert (await c.get(f"/garden/review/nodes/{nid}", headers=h)).status_code == 404
        assert (await c.get("/garden/review/rejected", headers=h)).json() == []


async def test_a_source_lists_what_was_generated_from_it(_wire: None) -> None:
    """The source note says what happened to it: the proposal generated
    from it, and its state at each step. The source must be a note the
    caller can open; the proposal itself is not."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))
        src = (await c.get(f"/garden/review/nodes/{nid}", headers=h)).json()["sources"][0]
        listed = (await c.get(f"/garden/review/by-source/{src['note_id']}", headers=h)).json()
        assert [(x["note_id"], x["review_state"]) for x in listed] == [(nid, "proposed")]
        v = (await c.get(f"/garden/review/nodes/{nid}", headers=h)).json()["version"]
        await c.post(
            "/garden/review/reject", headers=h, json={"note_id": nid, "expected_version": v}
        )
        listed = (await c.get(f"/garden/review/by-source/{src['note_id']}", headers=h)).json()
        assert [(x["note_id"], x["review_state"]) for x in listed] == [(nid, "declined")]
        assert (await c.get(f"/garden/review/by-source/{nid}", headers=h)).status_code == 404


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


async def test_approve_with_expected_version_guard(_wire: None) -> None:
    """The pending row serves the version pin; echoing it back guards the
    approve (TOCTOU, task 2e36e732): a stale pin is a 409
    ``concurrency.stale_version``, the fresh pin succeeds and the response
    carries the post-action version."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))

        pending = (await c.get("/garden/review/pending", headers=h)).json()
        row = next(p for p in pending if p["note_id"] == nid)
        seen = row["version"]
        assert isinstance(seen, int)

        stale = await c.post(
            "/garden/review/approve",
            headers=h,
            json={"note_id": nid, "expected_version": seen + 1},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "concurrency.stale_version"

        ok = await c.post(
            "/garden/review/approve",
            headers=h,
            json={"note_id": nid, "expected_version": seen},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["review_state"] == "approved"
        assert ok.json()["version"] == seen + 1
