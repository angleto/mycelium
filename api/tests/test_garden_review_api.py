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


async def _assistant(c: AsyncClient, h: dict[str, str]) -> dict[str, str]:
    """An assistant credential with the scopes a garden worker holds."""
    created = (
        await c.post(
            "/ai-assistants",
            headers={**h, "X-Workspace-Role": "owner"},
            json={
                "label": "Checker",
                "handle": f"c{uuid.uuid4().hex[:8]}",
                "scope": ["notes:read", "notes:write"],
            },
        )
    ).json()
    assert "raw_secret" in created, created
    return {
        "Authorization": f"Bearer {created['raw_secret']}",
        "X-Workspace-Id": h["X-Workspace-Id"],
    }


async def test_an_assistant_checks_and_revises_and_the_person_decides(_wire: None) -> None:
    """ADR-0076 over HTTP, with an assistant credential carrying a scope list.

    The assistant reads the proposal whole, attaches a check, cannot approve,
    and writes a revision. The person then sees the check (stale on the
    original, which the revision declined), the original beside the
    revision, and approves the revision. The original's model is not charged
    a rejection nobody made, and asking again for the same pattern finds the
    revision."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))
        a = await _assistant(c, h)

        read = await c.get(f"/garden/review/nodes/{nid}", headers=a)
        assert read.status_code == 200, read.text
        original = read.json()
        assert original["review_state"] == "proposed" and original["body"]

        finding = {
            "claim": "the sources share a forecast",
            "status": "unsupported",
            "evidence": "",
            "comment": "no source mentions a forecast",
        }
        mismatched = await c.post(
            "/garden/review/check",
            headers=a,
            json={
                "note_id": nid,
                "expected_version": original["version"],
                "verdict": "supported",
                "summary": "fine",
                "findings": [finding],
            },
        )
        assert mismatched.status_code in (400, 422), mismatched.text
        check = await c.post(
            "/garden/review/check",
            headers=a,
            json={
                "note_id": nid,
                "expected_version": original["version"],
                "verdict": "issues",
                "summary": "one claim is not in the sources",
                "findings": [finding],
                "checker_model_id": "claude-test",
            },
        )
        assert check.status_code == 200, check.text
        assert check.json()["checker_identity_id"] is not None

        refused = await c.post(
            "/garden/review/approve",
            headers=a,
            json={"note_id": nid, "expected_version": original["version"]},
        )
        assert refused.status_code == 403, refused.text

        rev = await c.post(
            "/garden/review/revise",
            headers=a,
            json={
                "note_id": nid,
                "expected_version": original["version"],
                "text": "A pattern grounded in the three sources, and nothing else.",
                "origin_model_id": "claude-test",
            },
        )
        assert rev.status_code == 200, rev.text
        rid = rev.json()["revision_note_id"]
        assert rev.json()["revises_note_id"] == nid and rev.json()["review_state"] == "proposed"

        revision = (await c.get(f"/garden/review/nodes/{rid}", headers=h)).json()
        assert revision["origin_model_id"] == "external:claude-test"
        assert revision["revises"]["note_id"] == nid
        assert revision["revises"]["body"] == original["body"]
        assert sorted(s["note_id"] for s in revision["sources"]) == sorted(
            s["note_id"] for s in original["sources"]
        )

        after = (await c.get(f"/garden/review/nodes/{nid}", headers=h)).json()
        assert after["review_state"] == "declined"
        assert [x["note_id"] for x in after["revised_by"]] == [rid]
        assert [(k["verdict"], k["stale"]) for k in after["checks"]] == [("issues", True)]

        async with tenant_session(org, user) as s:
            again = await decomp.extract_cluster_pattern(
                s,
                org_id=uuid.UUID(org),
                actor_id=uuid.UUID(user),
                source_note_ids=[uuid.UUID(x["note_id"]) for x in original["sources"]],
            )
        assert (str(again.note_id), again.created) == (rid, False)

        ok = await c.post(
            "/garden/review/approve",
            headers=h,
            json={"note_id": rid, "expected_version": revision["version"]},
        )
        assert ok.status_code == 200 and ok.json()["review_state"] == "approved", ok.text
        # One decision a person made, on the revision; the decline the
        # revision caused is not charged to the original's model.
        ratio = (await c.get("/garden/review/accept-ratio", headers=h)).json()
        assert [(m["model_id"], m["approved"], m["rejected"]) for m in ratio] == [
            ("external:claude-test", 1, 0)
        ], ratio


async def test_an_agent_reads_a_long_source_within_budget_and_pages_the_rest(
    _wire: None,
) -> None:
    """On production one 93,218-character source made a reading 96,184
    characters, which a client would not take as one result. An agent's
    reading now carries the source's opening, says it was cut and how long
    the whole is, and the rest is read by windows; the person's reading
    (``full_text``) is whole. A note that is not one of the proposal's
    sources is not readable through this door."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        long_body = "".join(f"riga {i:05d} del diario di gara.\n" for i in range(3000))
        async with tenant_session(org, user) as s:
            src = await nt.create_note(
                s,
                org_id=uuid.UUID(org),
                actor_id=uuid.UUID(user),
                kind=NoteKind.text,
                title="diario lungo",
                text=long_body,
            )
            other = await nt.create_note(
                s,
                org_id=uuid.UUID(org),
                actor_id=uuid.UUID(user),
                kind=NoteKind.text,
                title="altro",
                text="unrelated",
            )
            res = await decomp.distill_note(
                s,
                org_id=uuid.UUID(org),
                actor_id=uuid.UUID(user),
                note_id=src.id,
                distilled_text="Una sintesi breve.",
                origin_model_id="small",
            )
            src_id, other_id, nid = str(src.id), str(other.id), str(res.distilled_note_id)
        total = len(long_body)
        assert total > 90_000

        a = await _assistant(c, h)
        agent = await c.get(f"/garden/review/nodes/{nid}", headers=a)
        assert agent.status_code == 200, agent.text
        assert len(agent.text) < 30_000, len(agent.text)
        source = agent.json()["sources"][0]
        assert source["body_truncated"] is True and source["body_chars"] == total
        assert long_body.startswith(source["body"])

        person = (
            await c.get(f"/garden/review/nodes/{nid}", headers=h, params={"full_text": True})
        ).json()["sources"][0]
        assert person["body_truncated"] is False and len(person["body"]) == total

        pieces, offset = [], 0
        while offset is not None:
            window = await c.get(
                f"/garden/review/nodes/{nid}/sources/{src_id}",
                headers=a,
                params={"offset": offset},
            )
            assert window.status_code == 200, window.text
            pieces.append(window.json()["text"])
            offset = window.json()["next_offset"]
        assert "".join(pieces) == person["body"]
        assert len(pieces) == -(-total // 20_000)

        stranger = await c.get(f"/garden/review/nodes/{nid}/sources/{other_id}", headers=a)
        assert stranger.status_code == 404


async def test_a_pasted_proposal_code_is_found_and_opens_its_reading(_wire: None) -> None:
    """A person pasting the code of a proposal must find it: before, the
    lookup applied the review gate and answered nothing, so the codes the
    review pages showed led nowhere. Resolution finds it and routes it to
    the reading view; a list of things to link to still does not offer it."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))
        prefix = nid.replace("-", "")[:8]

        picker = (await c.get(f"/lookup/{prefix}", headers=h)).json()
        assert picker["matches"] == []

        found = (
            await c.get(f"/lookup/{prefix}", headers=h, params={"include_gated": True})
        ).json()["matches"]
        assert [(m["id"], m["review_state"], m["route_url"]) for m in found] == [
            (nid, "proposed", f"/garden/review/{nid}")
        ]


async def test_the_person_hands_a_kind_over_and_the_agent_cannot_widen_it(_wire: None) -> None:
    """ADR-0076 phase B over HTTP. Nothing is handed over by default; the
    owner hands one kind over in the workspace settings; the assistant
    cannot write that setting itself; it declines within the grant and is
    refused outside it; the person sees the decision and reverses it."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid = str(await _seed_proposed(org, user))
        a = await _assistant(c, h)
        assert (await c.get("/garden/delegation/policy", headers=a)).json() == {"granted": []}

        v = (await c.get(f"/garden/review/nodes/{nid}", headers=a)).json()["version"]
        refused = await c.post(
            "/garden/delegation/decide",
            headers=a,
            json={
                "target_kind": "synthesis",
                "target_id": nid,
                "verdict": "decline",
                "expected_version": v,
            },
        )
        assert refused.status_code in (400, 409, 422), refused.text
        assert refused.json()["code"] == "garden.delegation_refused"

        owner = {**h, "X-Workspace-Role": "owner"}
        ws = (await c.get("/workspaces/me", headers=owner)).json()
        body = {
            "expected_version": ws["version"],
            "estimate_presets": ws["settings"]["estimate_presets"],
            "garden_delegation": ["synthesis_decline"],
        }
        widen = await c.patch("/workspaces/me/settings", headers=a, json=body)
        assert widen.status_code in (401, 403), widen.text
        ok = await c.patch("/workspaces/me/settings", headers=owner, json=body)
        assert ok.status_code == 200, ok.text
        assert (await c.get("/garden/delegation/policy", headers=a)).json() == {
            "granted": ["synthesis_decline"]
        }

        dec = await c.post(
            "/garden/delegation/decide",
            headers=a,
            json={
                "target_kind": "synthesis",
                "target_id": nid,
                "verdict": "decline",
                "expected_version": v,
                "reason": "a claim is not in the sources",
            },
        )
        assert dec.status_code == 200, dec.text
        assert dec.json()["assistant_identity_id"] is not None
        approve = await c.post(
            "/garden/delegation/decide",
            headers=a,
            json={
                "target_kind": "synthesis",
                "target_id": nid,
                "verdict": "approve",
                "expected_version": v + 1,
            },
        )
        assert approve.json()["code"] == "garden.delegation_refused", approve.text

        listed = (await c.get("/garden/delegation/decisions", headers=h)).json()
        assert [(d["target_id"], d["verdict"], d["reversed_at"]) for d in listed] == [
            (nid, "decline", None)
        ]
        agent_revert = await c.post(
            "/garden/delegation/revert",
            headers=a,
            json={"decision_id": dec.json()["decision_id"]},
        )
        assert agent_revert.status_code == 403, agent_revert.text
        back = await c.post(
            "/garden/delegation/revert",
            headers=h,
            json={"decision_id": dec.json()["decision_id"]},
        )
        assert back.status_code == 200 and back.json()["reversed_at"], back.text
        state = (await c.get(f"/garden/review/nodes/{nid}", headers=h)).json()["review_state"]
        assert state == "proposed"
        stats = (await c.get("/garden/delegation/stats", headers=h)).json()
        assert stats == [{"kind": "synthesis_decline", "decided": 1, "reversed": 1}]


async def test_every_kind_of_proposal_in_one_list_with_the_same_verbs(_wire: None) -> None:
    """ADR-0075 decision 1 over HTTP: a synthesis, a link proposal and a
    fact, listed together as pending, decided with the same verb, listed as
    decided, and reopened back to pending. A declined fact says it cannot be
    reopened, and reopening it is refused."""
    from mycelium_core.services import kg
    from mycelium_core.services import link_proposals as lp

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        sid = str(await _seed_proposed(org, user))
        async with tenant_session(org, user) as s:
            a = await nt.create_note(
                s, org_id=uuid.UUID(org), actor_id=uuid.UUID(user), kind=NoteKind.text, text="a"
            )
            b = await nt.create_note(
                s, org_id=uuid.UUID(org), actor_id=uuid.UUID(user), kind=NoteKind.text, text="b"
            )
            prop = await lp.submit(
                s, org_id=uuid.UUID(org), proposer="test", source_note_id=a.id, target_note_id=b.id
            )
            x = await kg.ensure_entity(s, org_id=uuid.UUID(org), name="X", entity_type="person")
            y = await kg.ensure_entity(
                s, org_id=uuid.UUID(org), name="Y", entity_type="organization"
            )
            edge = await kg.add_fact(
                s,
                org_id=uuid.UUID(org),
                subject_id=x.id,
                predicate="member_of",
                object_id=y.id,
                review_state="proposed",
            )
            lid, fid = str(prop.proposal_id), str(edge.id)

        pending = (await c.get("/garden/proposals", headers=h)).json()["items"]
        by = {(p["kind"], p["id"]): p for p in pending}
        assert set(by) == {("synthesis", sid), ("link", lid), ("fact", fid)}
        assert by[("link", lid)]["link_kinds"][0] == "related"
        assert by[("fact", fid)]["fact_predicate"] == "member_of"

        for kind, pid, extra in (
            ("synthesis", sid, {"expected_version": by[("synthesis", sid)]["version"]}),
            ("link", lid, {"link_kind": "related"}),
            ("fact", fid, {"expected_version": by[("fact", fid)]["version"]}),
        ):
            r = await c.post(
                "/garden/proposals/decide",
                headers=h,
                json={"kind": kind, "id": pid, "verdict": "accept", **extra},
            )
            assert r.status_code == 200, (kind, r.text)
        assert (await c.get("/garden/proposals", headers=h)).json()["items"] == []
        decided = (
            await c.get("/garden/proposals", headers=h, params={"listing": "decided"})
        ).json()["items"]
        assert {(p["kind"], p["state"], p["reopenable"]) for p in decided} == {
            ("synthesis", "accepted", True),
            ("link", "accepted", True),
            ("fact", "accepted", True),
        }
        links = (await c.get(f"/notes/{a.id}/links", headers=h)).json()
        assert any(str(b.id) in str(x) for x in links["outgoing"] + links["incoming"])

        for kind, pid in (("synthesis", sid), ("link", lid), ("fact", fid)):
            r = await c.post("/garden/proposals/reopen", headers=h, json={"kind": kind, "id": pid})
            assert r.status_code == 200, (kind, r.text)
        again = (await c.get("/garden/proposals", headers=h)).json()["items"]
        assert {(p["kind"], p["id"]) for p in again} == {
            ("synthesis", sid),
            ("link", lid),
            ("fact", fid),
        }
        links = (await c.get(f"/notes/{a.id}/links", headers=h)).json()
        assert not any(str(b.id) in str(x) for x in links["outgoing"] + links["incoming"])

        fv = next(p for p in again if p["kind"] == "fact")["version"]
        await c.post(
            "/garden/proposals/decide",
            headers=h,
            json={"kind": "fact", "id": fid, "verdict": "decline", "expected_version": fv},
        )
        gone = next(
            p
            for p in (
                await c.get("/garden/proposals", headers=h, params={"listing": "decided"})
            ).json()["items"]
            if p["id"] == fid
        )
        assert (gone["state"], gone["reopenable"]) == ("declined", False)
        refused = await c.post(
            "/garden/proposals/reopen", headers=h, json={"kind": "fact", "id": fid}
        )
        assert refused.status_code == 404

        a2 = await _assistant(c, h)
        assert (
            await c.post(
                "/garden/proposals/decide",
                headers=a2,
                json={"kind": "link", "id": lid, "verdict": "decline"},
            )
        ).status_code == 403


async def test_a_note_tells_what_happened_to_it(_wire: None) -> None:
    """ADR-0075 decision 5: a person's maturity move and the archive show in
    the note's history, newest first, with who did them; a body edit shows
    as an edit and does not put the text in the history."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _org, _user = await _signup(c)
        n = (await c.post("/notes", headers=h, json={"kind": "text", "text": "secret body"})).json()
        r = await c.post(
            f"/notes/{n['id']}/maturity",
            headers=h,
            json={"maturity": "growing", "expected_version": n["version"]},
        )
        assert r.status_code == 200, r.text
        hist = (await c.get(f"/garden/notes/{n['id']}/history", headers=h)).json()["items"]
        actions = [e["action"] for e in hist]
        assert "set_maturity" in actions, actions
        move = next(e for e in hist if e["action"] == "set_maturity")
        assert move["changes"]["maturity"]["new"] == "growing"
        assert move["actor_kind"].startswith("human_"), move["actor_kind"]
        assert "secret body" not in str(hist)
