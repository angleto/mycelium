"""F6 hierarchical memory (DB-backed), ADR-0005/0007/0016 + FR-8.

Deterministic fake embedder (ADR-0012 seam). Covers: metered write
(gated by credits), hybrid RRF retrieval (exact rare token + semantic),
determinism, HARD (org, project) isolation, GDPR erasure cascade, tier
recompute keeping cold queryable.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.services import billing
from mycelium_core.services import memory as mem
from mycelium_core.services.auth import signup
from mycelium_worker import garden

_FAKE = FakeEmbedder()


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org(name: str = "MEM") -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name=name)
    return r.org_id, r.user_id


async def _seed_billing(s, org: uuid.UUID, user: uuid.UUID) -> None:
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))
    await billing.upsert_rate_card(
        s,
        org_id=org,
        actor_id=user,
        model_id=FakeEmbedder.model_id,
        provider="local",
        values={"credits_per_input": Decimal("0.001")},
    )


async def test_write_free_without_rate_card_metered_with() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        # No rate card: the bundled self-hosted embedder is FREE, so
        # the write succeeds out of the box and nothing is debited
        # (memory must work without any billing setup).
        before0 = await billing.balance(s, org_id=org)
        await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="hello world",
            operation_id="w0",
            embedder=_FAKE,
        )
        assert await billing.balance(s, org_id=org) == before0  # free

        # With a rate card configured, the embedding IS metered.
        await _seed_billing(s, org, user)
        before = await billing.balance(s, org_id=org)
        await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="hello world",
            operation_id="w1",
            embedder=_FAKE,
        )
        after = await billing.balance(s, org_id=org)
    assert after < before  # embedding debited when a rate card exists


async def test_hybrid_retrieval_and_determinism() -> None:
    org, user = await _org()
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="the quarterly budget review meeting",
            operation_id="a",
            embedder=_FAKE,
        )
        rare = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="xyzzy plugh frobnicate",
            operation_id="b",
            embedder=_FAKE,
        )
        # Rare exact token: the lexical branch must surface it.
        hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="xyzzy",
            operation_id="q1",
            embedder=_FAKE,
        )
        assert rare.id in {h.blob.id for h in hits}
        # Deterministic: same input -> identical ordered ids + scores.
        h2 = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="budget review",
            operation_id="q2",
            embedder=_FAKE,
        )
        h3 = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="budget review",
            operation_id="q2",
            embedder=_FAKE,
        )
    assert [(x.blob.id, round(x.rrf, 9)) for x in h2] == [(x.blob.id, round(x.rrf, 9)) for x in h3]


async def test_hard_project_and_org_isolation() -> None:
    org, user = await _org("ISO-A")
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=p1,
            text_body="secret p1 content alpha",
            operation_id="i1",
            embedder=_FAKE,
        )
        # Same query, other project: must return nothing (hard boundary).
        cross_proj = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=p2,
            query="secret alpha",
            operation_id="i2",
            embedder=_FAKE,
        )
        assert cross_proj == []
    other_org, other_user = await _org("ISO-B")
    async with tenant_session(str(other_org), str(other_user)) as s:
        await _seed_billing(s, other_org, other_user)
        cross_org = await mem.retrieve(
            s,
            org_id=other_org,
            actor_id=other_user,
            project_id=p1,
            query="secret alpha",
            operation_id="i3",
            embedder=_FAKE,
        )
    assert cross_org == []


async def test_gdpr_erase_cascades() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        b = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="from an email",
            operation_id="e1",
            sources=[("email", "msg-1")],
            embedder=_FAKE,
        )
        deleted = await mem.gdpr_erase(
            s, org_id=org, actor_id=user, source_kind="email", source_id="msg-1"
        )
        assert deleted == 1
        with pytest.raises(DomainError):
            await mem.get_blob(s, org_id=org, blob_id=b.id)


async def _perturbed_blob(org: uuid.UUID, user: uuid.UUID) -> tuple[uuid.UUID, str, str]:
    """Write a fresh blob, recompute its tier under default thresholds (the
    value the garden sweep will reproduce -- deterministic since a fresh blob
    has access_count=0, so the time-dependent decay term is 0), then perturb
    the stored tier to a DIFFERENT one. Returns (blob_id, expected, perturbed)."""
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        b = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="a stored memory whose tier the sweep should maintain",
            operation_id="seed",
            embedder=_FAKE,
        )
        await mem.recompute_tier(s, org_id=org)
        expected = b.tier
        perturbed = "hot" if expected != "hot" else "cold"
        b.tier = perturbed
        await s.flush()
    return b.id, expected, perturbed


async def test_garden_sweep_recomputes_tier_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """WS-D4: with ``garden_tier_recompute_enabled`` on, the autonomous garden
    sweep applies the access-decay tier recompute per workspace (ADR-0016:
    demote-not-delete), restoring a stale tier WITHOUT an on-demand call.
    ``_all_workspaces`` is pinned to the test org to keep the sweep isolated."""
    org, user = await _org()
    blob_id, expected, perturbed = await _perturbed_blob(org, user)
    assert perturbed != expected

    async def _only_this_org() -> list[uuid.UUID]:
        return [org]

    monkeypatch.setattr(garden, "_all_workspaces", _only_this_org)
    monkeypatch.setattr(get_settings(), "garden_tier_recompute_enabled", True)
    await garden.run_once()

    async with tenant_session(str(org), str(user)) as s:
        refreshed = await mem.get_blob(s, org_id=org, blob_id=blob_id)
        assert refreshed.tier == expected  # the sweep re-ran recompute_tier


async def test_garden_sweep_skips_tier_recompute_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recompute step is opt-in: with the flag off, the garden sweep leaves
    the (perturbed) tier untouched."""
    org, user = await _org()
    blob_id, _expected, perturbed = await _perturbed_blob(org, user)

    async def _only_this_org() -> list[uuid.UUID]:
        return [org]

    monkeypatch.setattr(garden, "_all_workspaces", _only_this_org)
    monkeypatch.setattr(get_settings(), "garden_tier_recompute_enabled", False)
    await garden.run_once()

    async with tenant_session(str(org), str(user)) as s:
        refreshed = await mem.get_blob(s, org_id=org, blob_id=blob_id)
        assert refreshed.tier == perturbed  # untouched: the gated step did not run


async def test_tier_recompute_keeps_cold_queryable() -> None:
    org, user = await _org()
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        b = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="rarely accessed but important note",
            operation_id="t1",
            embedder=_FAKE,
        )
        counts = await mem.recompute_tier(s, org_id=org, hot_threshold=999.0, warm_threshold=998.0)
        refreshed = await mem.get_blob(s, org_id=org, blob_id=b.id)
        assert refreshed.tier == "cold"  # demoted, not evicted
        hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="important note",
            operation_id="t2",
            embedder=_FAKE,
        )
    assert counts["cold"] >= 1
    assert b.id in {h.blob.id for h in hits}  # cold stays queryable


async def test_keyword_query_drops_semantic_only_noise() -> None:
    """Lexical-priority fusion + the relative floor make a keyword query
    return the real (lexical) hit and drop pure-semantic noise.

    Under the bag-of-words FakeEmbedder a doc sharing no token with the
    query is orthogonal (cosine 0), so it enters ONLY via the semantic
    branch. Weighted RRF sinks it well below the lexical hit and the
    relative floor then cuts it -- the exact 'marzia returns unrelated
    essays' failure, reproduced and fixed."""
    org, user = await _org("KWNOISE")
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        lexical = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="alpha alpha alpha",
            operation_id="a",
            embedder=_FAKE,
        )
        semantic_only = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="zzzbravo zzzbravo zzzbravo",
            operation_id="b",
            embedder=_FAKE,
        )
        hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="alpha",
            operation_id="q0",
            embedder=_FAKE,
        )
        ids = {h.blob.id for h in hits}
        assert lexical.id in ids
        assert semantic_only.id not in ids
        # The surviving hit is the lexical match, ranked top.
        assert hits[0].blob.id == lexical.id


async def test_conceptual_query_keeps_semantic_results() -> None:
    """The relative floor must NOT cut a genuinely-semantic query: with
    no lexical hit the score profile is flat (all semantic, similar
    ranks), so every result stays -- recall is preserved."""
    org, user = await _org("CONCEPT")
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        a = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="quarterly revenue forecast model",
            operation_id="a",
            embedder=_FAKE,
        )
        b = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="annual budget projection spreadsheet",
            operation_id="b",
            embedder=_FAKE,
        )
        # Query shares no exact token with either doc -> both enter only
        # via the semantic branch (flat profile); neither is cut.
        hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="financial planning numbers",
            operation_id="q",
            embedder=_FAKE,
        )
        ids = {h.blob.id for h in hits}
        assert a.id in ids
        assert b.id in ids


async def test_stem_collision_yields_to_exact_match() -> None:
    """The Italian stemmer conflates a short proper noun with a common
    word (search 'marzia' stem-matches an essay dated 'marzo'). Splitting
    lexical into exact vs stem and weighting exact far higher means the
    exact 'Marzia' hit dominates and the stem-only 'marzo' blob is cut by
    the relative floor -- the real 'marzia returns unrelated content' bug."""
    org, user = await _org("STEM")
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        exact = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="promemoria per Marzia",
            operation_id="a",
            embedder=_FAKE,
        )
        collision = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="consegna del progetto entro marzo 2025",
            operation_id="b",
            embedder=_FAKE,
        )
        hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="marzia",
            operation_id="q",
            embedder=_FAKE,
        )
        ids = {h.blob.id for h in hits}
        assert exact.id in ids
        assert collision.id not in ids
        assert hits[0].blob.id == exact.id


async def test_semantic_similarity_floor_drops_far_neighbours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-org semantic floor (``Organization.settings``) removes
    vector neighbours below the cosine threshold, so a query whose only
    semantic "matches" are far noise returns just the genuine (lexical)
    hits.

    Under the bag-of-words ``FakeEmbedder`` a doc that shares no token
    with the query is orthogonal (cosine 0), so it can enter ONLY via the
    semantic branch -- the perfect probe for the floor.

    Isolation: the separate relative-score floor (``RelativeFloorStage``,
    ratio ``_RELATIVE_FLOOR_RATIO``=0.4) would itself cut the
    weighted-down orthogonal neighbour even with the semantic floor OFF,
    masking the probe -- which is why commit cc55eda dropped this test
    instead of fixing it. Disable that one stage here so the per-org
    *semantic* floor is the only variable under test. This restores the
    end-to-end coverage of the configurable-floor read path
    (``semantic_min_similarity`` helper -> ``retrieve`` ->
    ``SemanticDenseStage._keep``) that no surviving test exercises.
    """
    from sqlalchemy import update

    from mycelium_core.models.organization import Organization

    # Control the confounding relative floor; the semantic floor is the
    # variable under test. Read at retrieve()-time from module globals.
    monkeypatch.setattr(mem, "_RELATIVE_FLOOR_RATIO", 0.0)

    org, user = await _org("FLOOR")
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        near = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="alpha alpha alpha",
            operation_id="a",
            embedder=_FAKE,
        )
        far = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="zzzbravo zzzbravo zzzbravo",
            operation_id="b",
            embedder=_FAKE,
        )
        # Floor OFF (default 0.0): the orthogonal neighbour is kept (it is
        # a semantic-only candidate), proving the next assertion relies on
        # the floor and nothing else.
        off = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="alpha",
            operation_id="q0",
            embedder=_FAKE,
        )
        ids_off = {h.blob.id for h in off}
        assert near.id in ids_off
        assert far.id in ids_off

        # Floor ON at 0.5: cosine(query "alpha", "zzzbravo...") == 0 < 0.5
        # -> dropped; the real match (cosine 1.0, also a lexical hit)
        # stays. The per-org setting is read live by mem.retrieve.
        await s.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={mem.SEMANTIC_MIN_SIM_KEY: 0.5})
        )
        on = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="alpha",
            operation_id="q1",
            embedder=_FAKE,
        )
        ids_on = {h.blob.id for h in on}
        assert near.id in ids_on
        assert far.id not in ids_on


async def test_grader_floor_abstains_when_top_below_per_org_floor() -> None:
    """WS-B1: the per-org grader/abstain floor (``Organization.settings``)
    makes retrieve return [] when even the best hit's fused RRF score sits
    below the floor -- "no answer" over "weak answer" -- and is read live by
    ``retrieve`` so /search + MCP search inherit it. The floor value is
    calibrated off the hit's own reported ``rrf`` so the test is robust to
    the absolute RRF magnitude."""
    from sqlalchemy import update

    from mycelium_core.models.organization import Organization

    org, user = await _org("GRADER")
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        blob = await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body="alpha alpha alpha",
            operation_id="w",
            embedder=_FAKE,
        )
        # Floor OFF (default): the genuine hit is returned; capture its
        # fused score to calibrate the floor.
        off = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="alpha",
            operation_id="q0",
            embedder=_FAKE,
        )
        assert blob.id in {h.blob.id for h in off}
        top = off[0].rrf
        assert top > 0.0

        # Floor ABOVE the top fused score -> abstain ([]).
        await s.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={mem.GRADER_MIN_RRF_KEY: min(1.0, top * 2.0)})
        )
        high = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="alpha",
            operation_id="q1",
            embedder=_FAKE,
        )
        assert high == []

        # Floor BELOW the top fused score -> the hit still comes back.
        await s.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={mem.GRADER_MIN_RRF_KEY: top * 0.5})
        )
        low = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="alpha",
            operation_id="q2",
            embedder=_FAKE,
        )
        assert blob.id in {h.blob.id for h in low}


async def test_grader_floor_helper_off_sentinels() -> None:
    """The helper returns None (no abstain) for absent / zero / malformed
    settings, so an unconfigured workspace keeps its historical behaviour."""
    org, user = await _org("GRADERH")
    async with tenant_session(str(org), str(user)) as s:
        assert await mem.grader_min_rrf_floor(s, org) is None  # absent
        from sqlalchemy import update

        from mycelium_core.models.organization import Organization

        await s.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={mem.GRADER_MIN_RRF_KEY: 0.0})
        )
        assert await mem.grader_min_rrf_floor(s, org) is None  # zero = off
        await s.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={mem.GRADER_MIN_RRF_KEY: 0.25})
        )
        assert await mem.grader_min_rrf_floor(s, org) == 0.25
