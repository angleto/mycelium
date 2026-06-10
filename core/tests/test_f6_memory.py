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

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.services import billing
from flow_core.services import memory as mem
from flow_core.services.auth import signup

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
