"""Centrality Phase 2 (task d8664631): betweenness, recency, snapshot.

Brandes betweenness on known topologies (path, star), the separate
recency axis, and the signature-gated materialisation the worker tick
drives — all against the real DB.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.services import graph as graph_svc
from mycelium_core.services import graph_snapshot as snap_svc
from mycelium_core.services import note_links
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.auth import signup


async def _org_user() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="GS",
        )
    return a.org_id, a.user_id


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body {title}",
    )


async def _link(s: object, org: uuid.UUID, user: uuid.UUID, a: Note, b: Note) -> None:
    await note_links.link_notes(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        parent_note_id=a.id,
        child_note_id=b.id,
        kind="related",
    )


async def test_betweenness_path_graph_middle_is_the_bridge() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        c = await _note(s, org, user, "c")
        await _link(s, org, user, a, b)
        await _link(s, org, user, b, c)
        bc = await graph_svc.compute_betweenness(s, org_id=org)
    # A - B - C: every (A, C) shortest path crosses B.
    assert bc[b.id] == pytest.approx(1.0)
    assert bc[a.id] == 0.0 and bc[c.id] == 0.0


async def test_betweenness_star_hub_maximal_leaves_zero() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        hub = await _note(s, org, user, "hub")
        leaves = [await _note(s, org, user, f"leaf{i}") for i in range(3)]
        for leaf in leaves:
            await _link(s, org, user, hub, leaf)
        bc = await graph_svc.compute_betweenness(s, org_id=org)
    assert bc[hub.id] == pytest.approx(1.0)
    for leaf in leaves:
        assert bc[leaf.id] == 0.0


async def test_recency_decays_with_age() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        n = await _note(s, org, user, "fresh")
        now = datetime.datetime.now(datetime.UTC)
        rec_now = await graph_svc.compute_recency(s, org_id=org, now=now)
        later = now + datetime.timedelta(days=2 * graph_svc.RECENCY_TAU_DAYS)
        rec_later = await graph_svc.compute_recency(s, org_id=org, now=later)
    assert rec_now[n.id] == pytest.approx(1.0, abs=0.01)
    # Two taus later the boost has decayed to e^-2 ~ 0.135.
    assert rec_later[n.id] == pytest.approx(0.1353, abs=0.01)


async def test_refresh_is_signature_gated_and_idempotent() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        await _link(s, org, user, a, b)
        # First refresh computes and stores.
        assert await snap_svc.refresh_graph_snapshot(s, org_id=org) is True
        snap = await snap_svc.get_graph_snapshot(s, org_id=org)
        assert snap is not None
        assert str(a.id) in snap.centrality and str(b.id) in snap.centrality
        first_computed_at = snap.computed_at
        # Unchanged graph -> the signature gate skips the recompute.
        assert await snap_svc.refresh_graph_snapshot(s, org_id=org) is False
        # A graph change flips the signature and the refresh runs again.
        c = await _note(s, org, user, "c")
        await _link(s, org, user, b, c)
        assert await snap_svc.refresh_graph_snapshot(s, org_id=org) is True
        snap2 = await snap_svc.get_graph_snapshot(s, org_id=org)
        assert snap2 is not None
        assert str(c.id) in snap2.centrality
        # Path a-b-c: b is now the stored bridge.
        assert snap2.betweenness[str(b.id)] == pytest.approx(1.0)
        assert snap2.computed_at >= first_computed_at


async def test_force_refresh_bypasses_signature() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        assert await snap_svc.refresh_graph_snapshot(s, org_id=org) is True
        assert await snap_svc.refresh_graph_snapshot(s, org_id=org) is False
        assert await snap_svc.refresh_graph_snapshot(s, org_id=org, force=True) is True
