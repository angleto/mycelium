"""``garden_classify`` proposal engine (ADR-0032) tests.

Covers the v1 read-only signals composed over the shipped substrate:

- shape + ``kinds`` filter (only requested signals are computed),
- tag co-occurrence suggestion (rarity-discounted),
- link suggestion excludes already-linked, surfaces shared-tag candidates,
- maturity value-axis: auto-promote tier (central AND curated) vs the
  proposal tier (curated below threshold) vs no-suggestion,
- cluster signal present or gracefully degraded without the extra.

Determinism note: maturity confidence is ``min(pr_pct, deg_term)`` and
``pr_pct`` is a *rank* percentile, so a node that is the unique PageRank
maximum scores ``pr_pct == 1.0`` regardless of the raw PageRank value —
the tiers are pinned by the manual-link degree alone.
"""

from __future__ import annotations

import uuid

from flow_core.db import admin_session, tenant_session
from flow_core.models.note import Note, NoteKind
from flow_core.models.note_tag import NoteTag
from flow_core.models.tag import TagKind
from flow_core.services import garden_classify as gc
from flow_core.services import note_links, taxonomy
from flow_core.services import notes as notes_svc
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _make_workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="GC")
    return a.org_id, a.user_id


async def _make_note(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body of {title}",
    )


async def _generic_tag(s: object, org: uuid.UUID, user: uuid.UUID, name: str) -> uuid.UUID:
    tag = await taxonomy.create_tag(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=TagKind.generic,
        name=name,
    )
    return tag.id


async def _attach(s: object, org: uuid.UUID, note_id: uuid.UUID, tag_id: uuid.UUID) -> None:
    s.add(NoteTag(org_id=org, note_id=note_id, tag_id=tag_id))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _link(
    s: object, org: uuid.UUID, user: uuid.UUID, parent: uuid.UUID, child: uuid.UUID
) -> None:
    await note_links.link_notes(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        parent_note_id=parent,
        child_note_id=child,
        kind="related",
    )


async def _warm_corpus(s: object, org: uuid.UUID) -> None:
    """Insert ``COLD_START_NODES`` isolated filler notes so the corpus clears
    the cold-start threshold and confidence damping is 1.0. The ranking tests
    use this to isolate the signal logic under test from WS-D5's sparse-graph
    damping (the fillers are untagged + unlinked, so they perturb no signal:
    a personalised PPR cannot reach them, co-occurrence support is unchanged,
    and the PageRank max is unmoved -- only the node count grows)."""
    for i in range(gc.COLD_START_NODES):
        s.add(Note(org_id=org, kind=NoteKind.text, title=f"filler-{i}"))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Shape + kinds filter
# ---------------------------------------------------------------------------


async def test_classify_returns_well_formed_result_for_isolated_note() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user, "lonely")
        res = await gc.classify_node(s, org_id=org, node_id=n.id)
    assert res.node_id == n.id
    assert res.node_kind == "note"
    assert res.tags == []
    assert res.links == []
    assert res.maturity is None  # default seed, not a growing candidate
    assert res.model_version == gc.MODEL_VERSION
    assert isinstance(res.signals_used, list)


async def test_kinds_filter_only_computes_requested_signals() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user, "x")
        await note_links.set_maturity(
            s, org_id=org, actor_id=user, note_id=n.id, maturity="growing"
        )
        res = await gc.classify_node(s, org_id=org, node_id=n.id, kinds=frozenset({"tags"}))
    # Only tags were requested; maturity/cluster must not be computed.
    assert res.maturity is None
    assert res.cluster is None
    assert "pagerank_pct" not in res.signals_used


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


async def test_tag_suggestion_picks_cooccurring_tag() -> None:
    # A has {X}; B and C have {X, Y}. Y co-occurs with A's tag on two
    # related notes, so it is a strong candidate for A.
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "a")
        b = await _make_note(s, org, user, "b")
        c = await _make_note(s, org, user, "c")
        x = await _generic_tag(s, org, user, "x-tag")
        y = await _generic_tag(s, org, user, "y-tag")
        for nid in (a.id, b.id, c.id):
            await _attach(s, org, nid, x)
        await _attach(s, org, b.id, y)
        await _attach(s, org, c.id, y)
        await _warm_corpus(s, org)  # past the cold-start threshold: damping=1.0
        res = await gc.classify_node(s, org_id=org, node_id=a.id, kinds=frozenset({"tags"}))
    suggested = {t.tag_id for t in res.tags}
    assert y in suggested
    assert x not in suggested  # already on A
    assert "tag_cooccur_adamic_adar" in res.signals_used


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


async def test_link_suggestion_excludes_linked_and_surfaces_shared_tag_candidate() -> None:
    # A is already linked to B (must be excluded). A and D share two rare
    # generic tags and are NOT linked, so D should surface above the floor.
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "a")
        b = await _make_note(s, org, user, "b")
        d = await _make_note(s, org, user, "d")
        t1 = await _generic_tag(s, org, user, "t1")
        t2 = await _generic_tag(s, org, user, "t2")
        for tid in (t1, t2):
            await _attach(s, org, a.id, tid)
            await _attach(s, org, d.id, tid)
        await _link(s, org, user, a.id, b.id)
        await _warm_corpus(s, org)  # past the cold-start threshold: damping=1.0
        res = await gc.classify_node(s, org_id=org, node_id=a.id, kinds=frozenset({"links"}))
    targets = {lc.target_id for lc in res.links}
    assert b.id not in targets  # already linked
    assert a.id not in targets  # never self
    assert d.id in targets
    assert all(lc.confidence >= gc.LINK_FLOOR for lc in res.links)
    assert all(lc.link_kind == "related" for lc in res.links)  # v1 default


# ---------------------------------------------------------------------------
# Maturity (the value axis)
# ---------------------------------------------------------------------------


async def _hub(
    s: object, org: uuid.UUID, user: uuid.UUID, in_links: int, isolated: int = 0
) -> uuid.UUID:
    """A growing note that is the unique PageRank maximum (``in_links``
    leaves point at it) with ``in_links`` manual links touching it."""
    hub = await _make_note(s, org, user, "hub")
    for i in range(in_links):
        leaf = await _make_note(s, org, user, f"leaf{i}")
        await _link(s, org, user, leaf.id, hub.id)  # leaf -> hub: authority flows in
    for j in range(isolated):
        await _make_note(s, org, user, f"iso{j}")
    await note_links.set_maturity(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        note_id=hub.id,
        maturity="growing",
    )
    return hub.id


async def test_maturity_auto_promotes_central_and_curated_hub() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        hub_id = await _hub(s, org, user, in_links=5)  # deg=5 -> deg_term=1.0
        await _warm_corpus(s, org)  # past the cold-start threshold: damping=1.0
        res = await gc.classify_node(s, org_id=org, node_id=hub_id, kinds=frozenset({"maturity"}))
    assert res.maturity is not None
    assert res.maturity.value == "mature"
    assert res.maturity.confidence >= gc.MATURE_AUTO
    assert res.maturity.auto_apply is True
    assert {"pagerank_pct", "manual_degree"}.issubset(set(res.signals_used))


async def test_maturity_proposal_tier_when_central_but_undercurated() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        # deg=2 -> deg_term=0.667; still the PR max so pr_pct=1.0;
        # conf=min(1.0, 0.667)=0.667 in [SUGGEST, AUTO).
        hub_id = await _hub(s, org, user, in_links=2, isolated=2)
        await _warm_corpus(s, org)  # past the cold-start threshold: damping=1.0
        res = await gc.classify_node(s, org_id=org, node_id=hub_id, kinds=frozenset({"maturity"}))
    assert res.maturity is not None
    assert res.maturity.value == "mature"
    assert gc.MATURE_SUGGEST <= res.maturity.confidence < gc.MATURE_AUTO
    assert res.maturity.auto_apply is False


async def test_no_maturity_suggestion_for_seed_note() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user, "fresh")  # default seed
        res = await gc.classify_node(s, org_id=org, node_id=n.id, kinds=frozenset({"maturity"}))
    assert res.maturity is None


async def test_no_maturity_suggestion_for_growing_but_uncurated_note() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user, "growing-lonely")
        await note_links.set_maturity(
            s, org_id=org, actor_id=user, note_id=n.id, maturity="growing"
        )
        # No links -> deg_term=0 -> conf=0 -> below the suggest floor.
        res = await gc.classify_node(s, org_id=org, node_id=n.id, kinds=frozenset({"maturity"}))
    assert res.maturity is None


# ---------------------------------------------------------------------------
# Cluster (present or gracefully degraded)
# ---------------------------------------------------------------------------


async def test_cluster_signal_present_or_degraded_gracefully() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "a")
        b = await _make_note(s, org, user, "b")
        await _link(s, org, user, a.id, b.id)
        res = await gc.classify_node(s, org_id=org, node_id=a.id, kinds=frozenset({"cluster"}))
    if res.cluster is None:
        # The optional `clustering` extra is not installed: explicit, not silent.
        assert "leiden_extra_absent" in res.signals_used
    else:
        assert "leiden_cluster" in res.signals_used
        assert res.cluster.modularity is not None


# ---------------------------------------------------------------------------
# Cold-start damping (WS-D5)
# ---------------------------------------------------------------------------


def test_cold_start_damping_factor() -> None:
    """Linear ramp: 0 for an empty corpus, 0.5 at the half-point, full at the
    threshold and beyond."""
    assert gc._cold_start_damping(0) == 0.0
    assert gc._cold_start_damping(gc.COLD_START_NODES // 2) == 0.5
    assert gc._cold_start_damping(gc.COLD_START_NODES) == 1.0
    assert gc._cold_start_damping(gc.COLD_START_NODES + 50) == 1.0


async def test_cold_start_damps_confident_suggestion_on_sparse_corpus() -> None:
    """The exact tag-co-occurrence scenario that surfaces a candidate on a
    warm corpus produces NO suggestion on a 3-note corpus, because the
    confidence is damped under the floor -- and the result flags
    corpus_too_sparse so the absence is explained, not silent."""
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "a")
        b = await _make_note(s, org, user, "b")
        c = await _make_note(s, org, user, "c")
        x = await _generic_tag(s, org, user, "x-tag")
        y = await _generic_tag(s, org, user, "y-tag")
        for nid in (a.id, b.id, c.id):
            await _attach(s, org, nid, x)
        await _attach(s, org, b.id, y)
        await _attach(s, org, c.id, y)
        # No warm-up: 3 notes is deep in the cold-start regime.
        res = await gc.classify_node(s, org_id=org, node_id=a.id, kinds=frozenset({"tags"}))
    assert res.tags == []  # the co-occurring tag is damped under TAG_FLOOR
    assert "corpus_too_sparse" in res.signals_used
    assert res.raw["cold_start_damping"] < 1.0
    assert res.raw["node_count"] == 3.0
