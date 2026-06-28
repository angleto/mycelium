"""4f3c2207 — retrieval observability: retrieve/search expose a meta envelope
so a caller can tell 'nothing relevant' from 'recall silently degraded to
keyword-only'. The three caller-invisible collapses (un-embeddable query,
per-org floor rejecting every neighbour, keyword-only blobs) are now visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import embedder_available, set_embedder_override
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.models.organization import Organization
from mycelium_core.services import billing
from mycelium_core.services import memory as memory_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.server import memory_search, memory_write, search


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _seed() -> tuple[str, str, uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="meta",
        )
    assert r.token is not None
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        await billing.grant_credits(s, org_id=r.org_id, actor_id=r.user_id, amount=Decimal(100))
        await billing.upsert_rate_card(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            model_id=FakeEmbedder.model_id,
            provider="local",
            values={"credits_per_input": Decimal("0.001")},
        )
    return r.token, str(r.org_id), r.org_id, r.user_id


async def test_meta_query_embedded_and_dense_contributed(_fake_embedder: None) -> None:
    token, org, org_id, user_id = await _seed()
    proj = uuid.uuid4()
    await memory_write(
        token=token, org_id=org, text="alpha bravo charlie", operation_id="w1", project_id=str(proj)
    )
    async with tenant_session(org, str(user_id)) as s:
        hits, meta = await memory_svc.retrieve_with_meta(
            s, org_id=org_id, actor_id=user_id, project_id=proj, query="bravo", operation_id="q1"
        )
    assert hits
    assert meta.query_embedded is True
    assert meta.dense_branch_contributed is True
    assert meta.dense_rejected_by_floor == 0


async def test_meta_floor_rejection_is_visible(_fake_embedder: None) -> None:
    token, org, org_id, user_id = await _seed()
    proj = uuid.uuid4()
    await memory_write(
        token=token, org_id=org, text="delta echo foxtrot", operation_id="w2", project_id=str(proj)
    )
    # Push the per-org floor above any achievable cosine: the dense branch
    # now rejects every neighbour (the exact prod mis-calibration). RLS hides
    # the row from a no-tenant session, so set it inside the tenant context.
    async with tenant_session(org, str(user_id)) as s:
        o = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        o.settings = {**(o.settings or {}), memory_svc.SEMANTIC_MIN_SIM_KEY: 0.99}
    async with tenant_session(org, str(user_id)) as s:
        hits, meta = await memory_svc.retrieve_with_meta(
            s, org_id=org_id, actor_id=user_id, project_id=proj, query="echo", operation_id="q2"
        )
    # Keyword recall is intact; only the dense branch was silently killed --
    # and the meta now exposes exactly that.
    assert hits
    assert meta.query_embedded is True
    assert meta.dense_rejected_by_floor > 0
    assert meta.dense_branch_contributed is False


async def test_meta_keyword_only_hits_and_model_id(_fake_embedder: None) -> None:
    token, org, org_id, user_id = await _seed()
    proj = uuid.uuid4()
    b = await memory_write(
        token=token, org_id=org, text="golf hotel india", operation_id="w3", project_id=str(proj)
    )
    # Force the blob keyword-only: no dense vector, sentinel model_id. RLS
    # scopes the row to the tenant, so mutate it inside the tenant context.
    async with tenant_session(org, str(user_id)) as s:
        blob = (
            await s.execute(select(MemoryBlob).where(MemoryBlob.id == uuid.UUID(b["id"])))
        ).scalar_one()
        blob.model_id = "none"
        blob.embedding = None
    async with tenant_session(org, str(user_id)) as s:
        hits, meta = await memory_svc.retrieve_with_meta(
            s, org_id=org_id, actor_id=user_id, project_id=proj, query="hotel", operation_id="q3"
        )
    assert any(h.blob.id == uuid.UUID(b["id"]) for h in hits)
    assert meta.keyword_only_hits > 0


async def test_mcp_tools_return_the_meta_envelope(_fake_embedder: None) -> None:
    token, org, _oid, _uid = await _seed()
    proj = str(uuid.uuid4())
    await memory_write(
        token=token, org_id=org, text="juliet kilo lima", operation_id="w4", project_id=proj
    )
    _META_KEYS = {
        "query_embedded",
        "dense_branch_contributed",
        "dense_rejected_by_floor",
        "keyword_only_hits",
    }
    ms = await memory_search(
        token=token, org_id=org, query="kilo", operation_id="q4", project_id=proj
    )
    assert set(ms) == {"hits", "meta"}
    assert set(ms["meta"]) == _META_KEYS

    us = await search(token=token, org_id=org, q="kilo", project_id=proj)
    assert set(us) == {"hits", "meta"}
    assert set(us["meta"]) == _META_KEYS
    # Per-hit model_id is re-exposed on the unified search rows.
    assert all("model_id" in h for h in us["hits"])


async def test_meta_query_not_embedded_without_embedder() -> None:
    # No embedder override: in CI (sentence-transformers absent) the query
    # cannot embed and the meta says so. Skip if a real embedder is present.
    if embedder_available():
        pytest.skip("a real embedder is available; the disabled-embedder path isn't exercised")
    token, org, org_id, user_id = await _seed()
    proj = uuid.uuid4()
    await memory_write(
        token=token, org_id=org, text="mike november oscar", operation_id="w5", project_id=str(proj)
    )
    async with tenant_session(org, str(user_id)) as s:
        _hits, meta = await memory_svc.retrieve_with_meta(
            s, org_id=org_id, actor_id=user_id, project_id=proj, query="november", operation_id="q5"
        )
    assert meta.query_embedded is False
    assert meta.dense_branch_contributed is False
