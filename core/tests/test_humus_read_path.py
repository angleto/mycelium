"""Humus read-path (ADR-0034, task 06fbf2a7): the archived material the
decomposition pipeline flagged (``notes.humus_flag``) re-enters retrieval.

Two surfaces, two DB-bound tests (the pure stage logic -- per-branch k,
the 30% cap, provenance propagation -- is covered fast in
test_retrieval_pipeline.py):

- focused walk (``memory.retrieve``): humus is a parallel source, hits
  carry provenance "humus", and the hard cap keeps humus <= 30% of slots;
- free wander (``graph.biased_random_walk``): the walk biases toward
  high-centrality humus (PageRank * humus_flag) yet keeps it <= 50%.

Deterministic FakeEmbedder seam; notes index at ``tenant_session``
teardown (same convention as test_note_search).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from flow_core.db import admin_session, tenant_session
from flow_core.embedder import set_embedder_override
from flow_core.models.note import Note, NoteKind
from flow_core.services import graph, memory
from flow_core.services import note_links as nl
from flow_core.services import notes as nt
from flow_core.services.auth import signup


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="HUMUS")
    return r.org_id, r.user_id


async def _flag_humus(s, note_ids: list[uuid.UUID]) -> None:
    """Set ``humus_flag`` the way the decomposition pipeline does (ORM flip
    on the loaded note), so the RLS UPDATE path is exercised exactly."""
    for nid in note_ids:
        note = (await s.execute(select(Note).where(Note.id == nid))).scalar_one()
        note.humus_flag = True


async def test_humus_focused_walk_provenance_and_cap(_embedder: None) -> None:
    """A query that matches one live note and four humus notes returns the
    humus hits marked provenance='humus', hard-capped at floor(10*0.3)=3
    slots, with the live note present and unmarked."""
    org, user = await _org()
    query = "quarterly revenue forecast synthesis"
    async with tenant_session(str(org), str(user)) as s:
        live = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            text="quarterly revenue forecast synthesis live working note",
        )
        humus = [
            await nt.create_note(
                s,
                org_id=org,
                actor_id=user,
                kind=NoteKind.text,
                text=f"quarterly revenue forecast synthesis distilled atom {i}",
            )
            for i in range(4)
        ]
        live_id = live.id
        humus_ids = [n.id for n in humus]
    # Flag the four humus notes (the write half the read path consumes).
    async with tenant_session(str(org), str(user)) as s:
        await _flag_humus(s, humus_ids)
    # Retrieve: humus is a parallel, boosted, but capped source.
    async with tenant_session(str(org), str(user)) as s:
        hits = await memory.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=query,
            operation_id=f"op-{uuid.uuid4().hex}",
            limit=10,
        )
    humus_hits = [h for h in hits if h.provenance == "humus"]
    # Hard cap: four humus matched, at most floor(10*0.3)=3 surface.
    assert len(humus_hits) == 3
    # Provenance is only ever the humus marker or None.
    assert {h.provenance for h in hits} <= {None, "humus"}
    # The live note survived the cap and is NOT marked humus.
    assert any(h.provenance is None for h in hits)
    assert live_id is not None  # (id captured for clarity; identity via marker)


async def test_free_wander_humus_bias_and_cap(_embedder: None) -> None:
    """A hub with two humus + two live leaves: the free wander biases its
    first step toward humus (PageRank * humus_flag) and keeps humus <= 50%
    of every walk."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        hub = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text="hub")
        leaves = [
            await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text=f"leaf {i}")
            for i in range(4)
        ]
        hub_id = hub.id
        leaf_ids = [n.id for n in leaves]
        for lid in leaf_ids:
            await nl.link_notes(
                s,
                org_id=org,
                actor_id=user,
                parent_note_id=hub_id,
                child_note_id=lid,
                kind="related",
            )
    humus_leaves = set(leaf_ids[:2])
    async with tenant_session(str(org), str(user)) as s:
        await _flag_humus(s, list(humus_leaves))

    async with tenant_session(str(org), str(user)) as s:
        humus_first = 0
        live_first = 0
        humus_seen_total = 0
        for seed_rng in range(30):
            path = await graph.biased_random_walk(
                s, org_id=org, seed_id=hub_id, budget=8, seed_rng=seed_rng
            )
            assert path[0] == hub_id
            if len(path) > 1:
                if path[1] in humus_leaves:
                    humus_first += 1
                elif path[1] in set(leaf_ids):
                    live_first += 1
            humus_in_walk = [n for n in path if n in humus_leaves]
            humus_seen_total += len(humus_in_walk)
            # Hard cap: humus stays a minority of the walk (<= 50%).
            assert len(humus_in_walk) <= (len(path) + 1) // 2
    # Bias direction: humus leaves (boosted) are chosen first more often
    # than live leaves of equal degree/centrality.
    assert humus_first > live_first
    # The bias actually surfaces humus across the runs.
    assert humus_seen_total > 0
