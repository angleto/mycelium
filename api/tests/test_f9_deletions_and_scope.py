"""F9: archived-tag exclusion + focus (project/client) scoping of the
tag list, memory-entry deletion, and note-derived memory landing on the
seeded ``note`` channel.

Why this exists:
- An archived tag must vanish from EVERY selection/filter surface (the
  root fix is in ``taxonomy.list_tags``: status filter, default
  exclude). ``GET /tags`` threads ``include_archived`` so the Tag
  manager can still un-archive one.
- When the SPA focus is a CLIENT (resp. a PROJECT) the tag list must
  not offer globally-unscoped foreign tags nor tags of projects/clients
  outside the focus: ``for_client`` / ``for_project`` resolve the
  visible scope (global OR scoped to the focus / its related
  client-projects).
- ``DELETE /memory/blobs/{id}`` hard-deletes one entry (cascade),
  member-level, RLS-scoped.
- ``notes.transcribe`` files its transcript memory on the seeded
  ``note`` channel deterministically.

Owner-gated writes (clients/projects, set scope) need the effective
role ``owner`` (clamped to the membership; a fresh signup's caller is
the workspace owner), so those calls send ``X-Workspace-Role: owner``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_ai import FakeSTT
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.ai_providers import set_stt_override
from flow_core.embedder import set_embedder_override


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


@pytest.fixture
def _fake_stt_embedder() -> Iterator[None]:
    set_stt_override(FakeSTT)
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_stt_override(None)
        set_embedder_override(None)


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


def _owner(h: dict[str, str]) -> dict[str, str]:
    return {**h, "X-Workspace-Role": "owner"}


async def _grant_and_rate(c: AsyncClient, h: dict[str, str]) -> None:
    # Billing grant + rate-card upserts are admin-gated; the effective
    # role must be owner (clamped to the fresh signup's owner
    # membership), so send the X-Workspace-Role lever.
    oh = _owner(h)
    g = await c.post("/billing/grant", headers=oh, json={"amount": "100"})
    assert g.status_code == 200, g.text
    for model in (FakeEmbedder.model_id, "fake-stt"):
        r = await c.post(
            "/billing/rate-cards",
            headers=oh,
            json={"model_id": model, "provider": "local", "credits_per_input": "0.001"},
        )
        assert r.status_code == 200, r.text


# --- Item 1: archived tags vanish from every selection surface -------


async def test_archived_tag_excluded_by_default_and_everywhere() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)

        t = (await c.post("/tags", headers=h, json={"kind": "generic", "name": "facet"})).json()
        # Default list shows it (active).
        ids = {x["id"] for x in (await c.get("/tags", headers=h)).json()}
        assert t["id"] in ids

        # Archive it via PATCH /tags/{id} (status soft-state).
        pa = await c.patch(
            f"/tags/{t['id']}",
            headers=h,
            json={"expected_version": t["version"], "status": "archived"},
        )
        assert pa.status_code == 200, pa.text

        # GET /tags default: archived tag is GONE (root fix).
        default = (await c.get("/tags", headers=h)).json()
        assert t["id"] not in {x["id"] for x in default}
        # Filtering by its kind still excludes it.
        bykind = (await c.get("/tags?kind=generic", headers=h)).json()
        assert t["id"] not in {x["id"] for x in bykind}

        # include_archived=true (Tag manager) brings it back so it can
        # be un-archived.
        full = (await c.get("/tags?include_archived=true", headers=h)).json()
        assert t["id"] in {x["id"] for x in full}

        # Un-archive: it reappears in the default list.
        cur = next(x for x in full if x["id"] == t["id"])
        pr = await c.patch(
            f"/tags/{t['id']}",
            headers=h,
            json={"expected_version": cur["version"], "status": "active"},
        )
        assert pr.status_code == 200, pr.text
        assert t["id"] in {x["id"] for x in (await c.get("/tags", headers=h)).json()}


# --- Item 2: focus (client/project) scoping of the tag list ----------


async def test_for_client_and_for_project_scoping() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        oh = _owner(h)

        client_a = (
            await c.post("/clients", headers=oh, json={"name": "A", "ragione_sociale": "A srl"})
        ).json()
        client_b = (
            await c.post("/clients", headers=oh, json={"name": "B", "ragione_sociale": "B srl"})
        ).json()
        proj_a = (
            await c.post(
                "/projects",
                headers=oh,
                json={"name": "PA", "client_tag_id": client_a["id"]},
            )
        ).json()

        # A global tag (no scope) -> always visible.
        g = (await c.post("/tags", headers=h, json={"kind": "generic", "name": "glob"})).json()
        # A tag scoped to client A.
        sa = (await c.post("/tags", headers=h, json={"kind": "generic", "name": "scA"})).json()
        assert (
            await c.put(
                f"/tags/{sa['id']}/scope",
                headers=oh,
                json={"target_ids": [client_a["id"]]},
            )
        ).status_code == 204
        # A tag scoped to a PROJECT of client A.
        sp = (await c.post("/tags", headers=h, json={"kind": "generic", "name": "scPA"})).json()
        assert (
            await c.put(
                f"/tags/{sp['id']}/scope",
                headers=oh,
                json={"target_ids": [proj_a["id"]]},
            )
        ).status_code == 204

        # Focus = client B: only the GLOBAL tag (no A-scoped, no
        # A-project-scoped).
        for_b = (await c.get(f"/tags?for_client={client_b['id']}", headers=h)).json()
        keys_b = {x["id"] for x in for_b}
        assert g["id"] in keys_b
        assert sa["id"] not in keys_b
        assert sp["id"] not in keys_b

        # Focus = client A: global + the A-scoped tag + the tag scoped to
        # a PROJECT of A (client -> its projects resolution).
        for_a = (await c.get(f"/tags?for_client={client_a['id']}", headers=h)).json()
        keys_a = {x["id"] for x in for_a}
        assert g["id"] in keys_a
        assert sa["id"] in keys_a
        assert sp["id"] in keys_a

        # Focus = project A: global + the project-scoped tag + the tag
        # scoped to its client (project -> its client resolution, the
        # pre-existing for_project behaviour, unchanged).
        for_p = (await c.get(f"/tags?for_project={proj_a['id']}", headers=h)).json()
        keys_p = {x["id"] for x in for_p}
        assert g["id"] in keys_p
        assert sp["id"] in keys_p
        assert sa["id"] in keys_p  # sa is scoped to A's client

        # No focus: behaviour unchanged (every active tag, scoped or
        # not, is returned).
        none = {x["id"] for x in (await c.get("/tags", headers=h)).json()}
        assert {g["id"], sa["id"], sp["id"]} <= none


# --- Item 3: delete a memory entry -----------------------------------


async def test_delete_memory_blob_and_cross_org_isolation(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        ha = await _signup(c)
        hb = await _signup(c)
        await _grant_and_rate(c, ha)

        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=ha,
            json={"project_id": proj, "text": "ephemeral note", "operation_id": "w1"},
        )
        assert w.status_code == 200, w.text
        bid = w.json()["id"]
        assert (await c.get(f"/memory/blobs/{bid}", headers=ha)).status_code == 200

        # Cross-org: org B cannot delete org A's blob -> 404
        # (RLS-scoped, MEMORY_NOT_FOUND); still intact for A.
        foreign = await c.delete(f"/memory/blobs/{bid}", headers=hb)
        assert foreign.status_code == 404, foreign.text
        assert foreign.json()["code"] == "memory.not_found"
        assert (await c.get(f"/memory/blobs/{bid}", headers=ha)).status_code == 200

        # Owner deletes it -> 204, then GET -> 404.
        d = await c.delete(f"/memory/blobs/{bid}", headers=ha)
        assert d.status_code == 204, d.text
        gone = await c.get(f"/memory/blobs/{bid}", headers=ha)
        assert gone.status_code == 404, gone.text
        assert gone.json()["code"] == "memory.not_found"

        # Deleting an unknown id -> 404.
        unk = await c.delete(f"/memory/blobs/{uuid.uuid4()}", headers=ha)
        assert unk.status_code == 404, unk.text


# --- Item 4: note-derived memory lands on the seeded "note" channel --


async def test_note_transcript_memory_tagged_with_note_channel(
    _fake_stt_embedder: None,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)

        # A voice note with audio -> transcribe produces a memory blob.
        n = await c.post(
            "/notes",
            headers=h,
            json={
                "kind": "voice",
                "audio_ref": "s3://audio/a.webm",
                "audio_seconds": 30,
            },
        )
        assert n.status_code == 200, n.text
        note = n.json()
        proj = note["project_id"]

        tr = await c.post(
            f"/notes/{note['id']}/transcribe",
            headers=h,
            json={"operation_id": "tr1", "embed": True},
        )
        assert tr.status_code == 200, tr.text
        transcript = tr.json()["transcript"]
        assert transcript

        # The transcript-derived blob is searchable within the note's
        # project AND carries the seeded "note" memory channel tag.
        s = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": transcript,
                "operation_id": "tr1-q",
            },
        )
        assert s.status_code == 200, s.text
        hits = s.json()
        assert len(hits) >= 1
        blob = hits[0]["blob"]
        chan = [t for t in blob["tags"] if t["kind"] == "memory_channel"]
        assert len(chan) == 1
        assert chan[0]["name"] == "Note"

        # Restricting the search to channel_key="note" still finds it
        # (deterministic landing), proving the channel binding.
        s2 = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": transcript,
                "operation_id": "tr1-q2",
                "channel_key": "note",
            },
        )
        assert s2.status_code == 200, s2.text
        assert blob["id"] in {x["blob"]["id"] for x in s2.json()}
