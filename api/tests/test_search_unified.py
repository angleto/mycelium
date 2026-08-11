"""Unified /search endpoint + task-search index plumbing.

End-to-end checks: creating a task makes it findable via /search
(kind=task), editing it refreshes the blob, deleting the task drops
the blob, and soft-delete / archived tasks are hidden unless the
caller opts in. The fake embedder seam keeps semantic recall
deterministic without the optional model.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.embedder import set_embedder_override


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


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


async def _grant_and_rate(c: AsyncClient, h: dict[str, str]) -> None:
    """The task-search resync uses the same embedder + metering path as
    /memory/blobs; grant a balance and a rate card so the metered embed
    in the resync doesn't trip on a missing card. The keyword-only fallback
    is exercised separately by the existing memory tests."""
    await c.post("/billing/grant", headers=h, json={"amount": "100"})
    await c.post(
        "/billing/rate-cards",
        headers=h,
        json={
            "model_id": FakeEmbedder.model_id,
            "provider": "local",
            "credits_per_input": "0.001",
        },
    )


async def test_create_task_makes_it_searchable(_fake_embedder: None) -> None:
    """A fresh task is indexed at commit time: /search finds it by a
    title token immediately, no async backfill needed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Quarterly budget review for alpha"},
            )
        ).json()
        tid = task["id"]

        r = await c.post(
            "/search",
            headers=h,
            json={"q": "quarterly budget", "kinds": ["task"], "limit": 10},
        )
        assert r.status_code == 200, r.text
        hits = r.json()
        assert any(hit["kind"] == "task" and hit["task_id"] == tid for hit in hits), (
            f"expected the new task in hits, got {hits}"
        )


async def test_checklist_text_is_indexed(_fake_embedder: None) -> None:
    """A checklist item word makes the parent task findable even when
    the title doesn't contain it: the resync renders ``title +
    description + checklist`` as one blob."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Shopping list"},
            )
        ).json()
        tid = task["id"]
        await c.post(
            f"/tasks/{tid}/checklist",
            headers=h,
            json={"text": "pomodori cipolle aglio"},
        )

        r = await c.post(
            "/search",
            headers=h,
            json={"q": "pomodori", "kinds": ["task"], "limit": 10},
        )
        assert r.status_code == 200, r.text
        hits = r.json()
        assert any(hit["task_id"] == tid for hit in hits), (
            f"checklist word should surface the parent task: {hits}"
        )


async def test_delete_task_drops_the_blob(_fake_embedder: None) -> None:
    """Deleting the task removes its pointer and the underlying blob;
    a subsequent search no longer returns it."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Ephemeral migration plan"},
            )
        ).json()
        tid = task["id"]

        # Visible first.
        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "ephemeral migration", "kinds": ["task"]},
            )
        ).json()
        assert any(h_["task_id"] == tid for h_ in hits)

        # Soft-delete via POST /tasks/{id}/delete (the REST DELETE verb
        # is reserved for sub-resources; the task delete is a state
        # transition that sets ``deleted_at``, not a row removal). The
        # listener-driven resync detects the soft-delete and cleans the
        # pointer + blob.
        await c.post(
            f"/tasks/{tid}/delete",
            headers=h,
            json={"expected_version": task["version"]},
        )

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={
                    "q": "ephemeral migration",
                    "kinds": ["task"],
                    "include_deleted": False,
                },
            )
        ).json()
        assert not any(h_["task_id"] == tid for h_ in hits), (
            "soft-deleted task should be hidden by default"
        )

        # include_deleted=true brings it back.
        hits = (
            await c.post(
                "/search",
                headers=h,
                json={
                    "q": "ephemeral migration",
                    "kinds": ["task"],
                    "include_deleted": True,
                },
            )
        ).json()
        assert any(h_["task_id"] == tid for h_ in hits), (
            f"include_deleted=true should expose the soft-deleted task: {hits}"
        )


async def test_archived_task_hidden_by_default(_fake_embedder: None) -> None:
    """An archived task is filtered out unless include_archived=true."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Bestiary of obscure compiler bugs"},
            )
        ).json()
        tid = task["id"]
        # Archive via the dedicated POST /tasks/{id}/archive endpoint
        # (is_archived is not exposed through generic PATCH; it's a
        # state transition with its own audit action).
        await c.post(
            f"/tasks/{tid}/archive",
            headers=h,
            json={"expected_version": task["version"]},
        )

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "bestiary", "kinds": ["task"]},
            )
        ).json()
        assert not any(h_["task_id"] == tid for h_ in hits)

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "bestiary", "kinds": ["task"], "include_archived": True},
            )
        ).json()
        assert any(h_["task_id"] == tid for h_ in hits)


async def test_note_is_searchable_as_kind_note(_fake_embedder: None) -> None:
    """A text note created via /notes is indexed per-part at commit time
    and surfaces in /search as a titled ``kind='note'`` hit that carries
    ``note_id`` + ``part_id`` (resolved via ``note_part_index_pointer``),
    not as an opaque ``kind='blob'`` row."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        note = (
            await c.post(
                "/notes",
                headers=h,
                json={
                    "kind": "text",
                    "title": "Roadmap notes",
                    "text": "the mycelium decomposition differentiator zphlogiston",
                },
            )
        ).json()
        nid = note["id"]

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "zphlogiston", "limit": 10},
            )
        ).json()
        note_hits = [x for x in hits if x["kind"] == "note"]
        assert any(x["note_id"] == nid for x in note_hits), (
            f"expected the note as kind='note', got {hits}"
        )
        hit = next(x for x in note_hits if x["note_id"] == nid)
        assert hit["title"] == "Roadmap notes"
        assert hit["part_id"]
        assert hit["task_id"] is None


async def test_note_blob_is_deduped_from_blob_kind(_fake_embedder: None) -> None:
    """When both 'note' and 'blob' are requested, the note part blob is
    emitted once as the titled ``kind='note'`` row, not also as the
    opaque ``kind='blob'`` row (the catch-all blob branch spans the note
    channel, so without dedup the same blob would appear twice)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        note = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "Dedup check", "text": "qwxvz unique token"},
            )
        ).json()
        nid = note["id"]

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "qwxvz", "kinds": ["note", "blob"], "limit": 10},
            )
        ).json()
        # The note's underlying blob id appears exactly once, as kind=note.
        note_rows = [x for x in hits if x["kind"] == "note" and x["note_id"] == nid]
        assert len(note_rows) == 1, f"note should surface once: {hits}"
        note_blob_id = note_rows[0]["blob_id"]
        assert not any(x["kind"] == "blob" and x["blob_id"] == note_blob_id for x in hits), (
            f"note blob must not also appear as kind='blob': {hits}"
        )


async def test_soft_deleted_note_hidden_unless_include_deleted(_fake_embedder: None) -> None:
    """A soft-deleted note is filtered out of ``kind='note'`` hits by
    default (the part blob lingers but the note row is hidden), and
    re-appears with include_deleted=true -- mirroring the task branch."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        note = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "Doomed", "text": "kqxjj soon gone"},
            )
        ).json()
        nid = note["id"]

        # Visible first.
        hits = (await c.post("/search", headers=h, json={"q": "kqxjj", "kinds": ["note"]})).json()
        assert any(x["note_id"] == nid for x in hits)

        await c.post(
            f"/notes/{nid}/delete",
            headers=h,
            json={"expected_version": note["version"]},
        )

        hits = (await c.post("/search", headers=h, json={"q": "kqxjj", "kinds": ["note"]})).json()
        assert not any(x.get("note_id") == nid for x in hits), (
            "soft-deleted note should be hidden by default"
        )

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "kqxjj", "kinds": ["note"], "include_deleted": True},
            )
        ).json()
        assert any(x.get("note_id") == nid for x in hits), (
            f"include_deleted=true should expose the soft-deleted note: {hits}"
        )


async def test_grader_floor_abstains_through_unified_search(_fake_embedder: None) -> None:
    """WS-B1 end-to-end: the per-org grader/abstain floor flows through the
    unified /search path (``search_unified`` -> ``memory.retrieve``), not only
    ``memory.retrieve`` in isolation. /search exposes no per-call knob, so it
    inherits the workspace floor: a ceiling floor makes /search abstain ("no
    answer" over "weak answer"), and a tiny positive floor lets the genuine
    hit back -- proving the floor is a real threshold applied on the unified
    surface that the SPA and the MCP agents share."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Quarterly budget review for alpha"},
            )
        ).json()
        tid = task["id"]
        body = {"q": "quarterly budget", "kinds": ["task"], "limit": 10}

        def _found(hits: list[dict]) -> bool:
            return any(x["kind"] == "task" and x["task_id"] == tid for x in hits)

        async def _set_floor(value: float) -> None:
            me = (await c.get("/workspaces/me", headers=h)).json()
            r = await c.patch(
                "/workspaces/me/settings",
                headers=h,
                json={
                    "expected_version": me["version"],
                    # estimate_presets is a required field on the settings PATCH;
                    # echo the current value so we only move the grader floor.
                    "estimate_presets": me["settings"]["estimate_presets"],
                    "retrieval_grader_min_rrf": value,
                },
            )
            assert r.status_code == 200, r.text

        # Floor off (default): the genuine hit comes back through /search.
        assert _found((await c.post("/search", headers=h, json=body)).json())

        # Floor at the ceiling (the fused-RRF domain max, >= any achievable
        # score; the [0,1] footgun was corrected to <=0.05) -> /search abstains.
        await _set_floor(0.05)
        assert (await c.post("/search", headers=h, json=body)).json() == []

        # A tiny positive floor (a real threshold, not on/off) -> hit returns.
        await _set_floor(0.001)
        assert _found((await c.post("/search", headers=h, json=body)).json())


# ------------------------------------------------------------ id lookups
#
# Searching an entity code is a LOOKUP, not a similarity question. It has
# exactly two right answers -- the entity the code names, and the places
# that cite it verbatim -- and both are exact. Left to the hybrid pipeline
# the code was embedded instead, and since 8 hex digits carry no meaning
# its nearest neighbours are arbitrary: an id query came back with a page
# of confident, unrelated results.


async def test_searching_an_id_returns_the_entity_it_names(_fake_embedder: None) -> None:
    """The task whose id starts with the code leads the results, and the
    note that cites the code verbatim follows.

    Neither half is reachable through similarity: a task does not contain
    its own id, and a hex token has no neighbours worth having."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        tid = (
            await c.post("/tasks", headers=h, json={"title": "Riallineare il set consolidato"})
        ).json()["id"]
        code = tid[:8]
        # A note that MENTIONS the code, the "where is this cited" half.
        citing = (
            await c.post(
                "/notes",
                headers=h,
                json={
                    "kind": "text",
                    "title": "Registro decisioni",
                    "text": f"Deciso oggi: chiudere {code} prima del rilascio.",
                },
            )
        ).json()["id"]
        # Decoys with no relation to the code and no shared vocabulary.
        for n in range(3):
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": f"Decoy {n}", "text": f"Materiale estraneo {n}."},
            )

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": code, "kinds": ["task", "note"], "limit": 10},
            )
        ).json()

        assert hits, "an id that names a live task must not come back empty"
        assert hits[0]["kind"] == "task" and hits[0]["task_id"] == tid, (
            f"the named entity must lead, got {hits[0]}"
        )
        assert any(hit["kind"] == "note" and hit["note_id"] == citing for hit in hits), (
            f"the note citing {code} verbatim must be found, got {hits}"
        )
        # And nothing else: every decoy would have arrived as a nearest
        # neighbour of the embedded code.
        returned = {(hit["kind"], hit.get("task_id") or hit.get("note_id")) for hit in hits}
        assert returned == {("task", tid), ("note", citing)}, (
            f"an id lookup must return only exact matches, got {hits}"
        )


async def test_an_id_that_matches_nothing_returns_nothing(_fake_embedder: None) -> None:
    """The honest answer to an unknown code is an empty list.

    ``5d44d8e5`` in the field was a REVISION id: never an entity, never
    written in prose. It came back with five results anyway."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        tid = (await c.post("/tasks", headers=h, json={"title": "Qualcosa"})).json()["id"]
        for n in range(3):
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": f"Nota {n}", "text": f"Contenuto {n}."},
            )

        unknown = "5d44d8e5"
        assert not tid.startswith(unknown), "fixture collision: pick another code"

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": unknown, "kinds": ["task", "note"], "limit": 10},
            )
        ).json()
        assert hits == [], f"an unknown id must return nothing, got {hits}"


async def test_a_branch_with_no_real_match_cannot_pad_the_page(_fake_embedder: None) -> None:
    """Cross-branch floor: an exact hit in one branch must not drag the
    other branch's semantic tail along.

    Each branch already drops its own tail, but relative to its OWN top --
    so a branch holding nothing has a flat profile, keeps everything, and
    used to hand it all to a merge with no floor at all."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        tid = (
            await c.post("/tasks", headers=h, json={"title": "Revisione zqxwvu trimestrale"})
        ).json()["id"]
        for n in range(3):
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": f"Altro {n}", "text": f"Argomento diverso {n}."},
            )

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "zqxwvu", "kinds": ["task", "note"], "limit": 10},
            )
        ).json()

        assert any(hit["kind"] == "task" and hit["task_id"] == tid for hit in hits)
        assert [hit for hit in hits if hit["kind"] == "note"] == [], (
            f"the note branch had no match for 'zqxwvu' and must contribute nothing, got {hits}"
        )
