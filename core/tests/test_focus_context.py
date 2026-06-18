"""PPR-seeded focus context (WS-B2).

The note-part search index flushes at ``tenant_session`` teardown, so a
test seeds the graph + content in one session and asserts in a fresh one
(the pattern test_note_search uses). Deterministic FakeEmbedder seam.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from _fake_embedder import FakeEmbedder

from flow_core.db import admin_session, tenant_session
from flow_core.embedder import set_embedder_override
from flow_core.models.note import NoteKind
from flow_core.services import billing, note_links
from flow_core.services import focus_context as fc
from flow_core.services import notes as nt
from flow_core.services.auth import signup


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="FC",
        )
    return r.org_id, r.user_id


async def _seed_billing(s: object, org: uuid.UUID, user: uuid.UUID) -> None:
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))  # type: ignore[arg-type]
    await billing.upsert_rate_card(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        model_id=FakeEmbedder.model_id,
        provider="local",
        values={"credits_per_input": Decimal("0.001")},
    )


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str, text: str) -> uuid.UUID:
    n = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=text,
    )
    return n.id


async def _link(s: object, org: uuid.UUID, user: uuid.UUID, a: uuid.UUID, b: uuid.UUID) -> None:
    await note_links.link_notes(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        parent_note_id=a,
        child_note_id=b,
        kind="related",
    )


async def test_focus_context_orders_by_ppr_with_content(_embedder: None) -> None:
    """The reading set is the seed's neighbourhood by induced PPR mass (seed
    excluded), and a directly-linked note outranks a two-hop one. Title +
    snippet are resolved so the caller can read without follow-ups."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        seed = await _note(s, org, user, "seed", "the seed note body about gardens")
        near = await _note(s, org, user, "alpha", "alpha body about gardens")
        far = await _note(s, org, user, "bravo", "bravo body about gardens")
        await _link(s, org, user, seed, near)  # seed <-> near (1 hop)
        await _link(s, org, user, near, far)  # near <-> far (2 hops from seed)
        seed_id, near_id, far_id = seed, near, far
    async with tenant_session(str(org), str(user)) as s:
        nodes = await fc.focus_context(s, org_id=org, actor_id=user, seed_id=seed_id, budget=10)
    ids = [n.note_id for n in nodes]
    assert seed_id not in ids  # the caller already has the seed
    assert near_id in ids and far_id in ids
    assert ids.index(near_id) < ids.index(far_id)  # 1 hop > 2 hops by mass
    by_id = {n.note_id: n for n in nodes}
    assert by_id[near_id].title == "alpha"
    assert by_id[near_id].snippet  # head of the indexed part text
    assert by_id[near_id].ppr_mass > 0.0
    assert by_id[near_id].provenance is None


async def test_focus_context_empty_for_isolated_seed(_embedder: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        seed = await _note(s, org, user, "lonely", "no links here")
        seed_id = seed
    async with tenant_session(str(org), str(user)) as s:
        nodes = await fc.focus_context(s, org_id=org, actor_id=user, seed_id=seed_id, budget=10)
    assert nodes == []


async def test_focus_context_query_fusion_reranks_toward_relevant(_embedder: None) -> None:
    """Without a query the more-central note ranks first; WITH a query that
    matches the less-central note, late RRF fusion lifts the on-topic note
    above the more-central but off-topic one."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        seed = await _note(s, org, user, "seed", "seed about gardens")
        central = await _note(s, org, user, "central", "central note about gardens and soil")
        topic = await _note(s, org, user, "topic", "zephyrium zephyrium zephyrium distinctive term")
        x = await _note(s, org, user, "x", "x filler about gardens")
        y = await _note(s, org, user, "y", "y filler about gardens")
        # seed touches both candidates; ``central`` has extra neighbours so
        # its induced PPR mass is higher than ``topic``'s.
        await _link(s, org, user, seed, central)
        await _link(s, org, user, seed, topic)
        await _link(s, org, user, central, x)
        await _link(s, org, user, central, y)
        seed_id, central_id, topic_id = seed, central, topic
    # No query: central (more mass) ranks above topic.
    async with tenant_session(str(org), str(user)) as s:
        plain = await fc.focus_context(s, org_id=org, actor_id=user, seed_id=seed_id, budget=10)
    plain_ids = [n.note_id for n in plain]
    assert plain_ids.index(central_id) < plain_ids.index(topic_id)
    # Query matching ``topic``: fusion lifts it above ``central``.
    async with tenant_session(str(org), str(user)) as s:
        fused = await fc.focus_context(
            s, org_id=org, actor_id=user, seed_id=seed_id, budget=10, query="zephyrium"
        )
    fused_ids = [n.note_id for n in fused]
    assert fused_ids.index(topic_id) < fused_ids.index(central_id)
