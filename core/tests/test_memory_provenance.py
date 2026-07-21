"""First-class provenance on memory_blobs (enabler A, migration 0085).

Before this, a blob's author was only the ``agent/<handle>`` tag-lane
convention, which cannot satisfy the unconditional-provenance axiom once more
than one agent writes to the shared store. Now every blob carries ``created_by``
(the authoring identity, a user OR an ai_assistant) and ``origin_model_id`` (the
LLM that produced it), and recall can filter by author while shared reads stay
open.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select
from tests_helpers import seed_ai_assistant_identity

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services import agent_tokens as at_svc
from mycelium_core.services import billing, identities
from mycelium_core.services import memory as memory_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.server import _PRINCIPAL, memory_search, memory_write


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _seed() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="PROV",
        )
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
    return r.org_id, r.user_id


async def _blob(org: uuid.UUID, user: uuid.UUID, blob_id: uuid.UUID) -> MemoryBlob:
    async with tenant_session(str(org), str(user)) as s:
        return (await s.execute(select(MemoryBlob).where(MemoryBlob.id == blob_id))).scalar_one()


async def test_write_records_explicit_provenance(_fake_embedder: None) -> None:
    org, user = await _seed()
    async with tenant_session(str(org), str(user)) as s:
        author = await identities.ensure_for_user(s, org_id=org, user_id=user)
        written = await memory_svc.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="explicit provenance payload",
            operation_id="w-explicit",
            created_by_identity_id=author.id,
            origin_model_id="test-model-x",
        )
    row = await _blob(org, user, written.id)
    assert row.created_by == author.id
    assert row.origin_model_id == "test-model-x"


async def test_write_defaults_author_to_actor_user_identity(_fake_embedder: None) -> None:
    """Unconditional provenance: even a plain human write (no explicit identity,
    no model) is stamped with the actor's user identity, not left NULL."""
    org, user = await _seed()
    async with tenant_session(str(org), str(user)) as s:
        author = await identities.ensure_for_user(s, org_id=org, user_id=user)
        written = await memory_svc.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="defaulted provenance payload",
            operation_id="w-default",
        )
    row = await _blob(org, user, written.id)
    assert row.created_by == author.id
    assert row.origin_model_id is None


async def test_recall_filters_by_author(_fake_embedder: None) -> None:
    org, user = await _seed()
    async with tenant_session(str(org), str(user)) as s:
        a = await seed_ai_assistant_identity(s, org_id=org, user_id=user, label="agent-a")
        b = await seed_ai_assistant_identity(s, org_id=org, user_id=user, label="agent-b")
        blob_a = await memory_svc.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="shared coordination probe kilo",
            operation_id="w-a",
            created_by_identity_id=a.id,
        )
        await memory_svc.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="shared coordination probe kilo",
            operation_id="w-b",
            created_by_identity_id=b.id,
        )

    async with tenant_session(str(org), str(user)) as s:
        only_a = await memory_svc.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query="coordination probe kilo",
            operation_id="q-a",
            created_by=a.id,
        )
    ids = {h.blob.id for h in only_a}
    assert ids == {blob_a.id}  # author filter isolates A

    async with tenant_session(str(org), str(user)) as s:
        shared = await memory_svc.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query="coordination probe kilo",
            operation_id="q-shared",
        )
    assert len({h.blob.id for h in shared}) >= 2  # unfiltered sees both authors


async def test_mcp_memory_write_stamps_the_agent_identity(_fake_embedder: None) -> None:
    """Over an agent token the blob's author is the ai_assistant identity, and
    the value is surfaced in the tool output + usable as a recall filter."""
    org, user = await _seed()
    async with tenant_session(str(org), str(user)) as s:
        ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user, label="claude")
        minted = await at_svc.mint(
            s, org_id=org, actor_id=user, name="obs", assistant_id=ident.ai_assistant_id
        )
    token_id = minted.token.id

    # Mimic the HTTP bearer middleware: publish the principal so the tool
    # resolves the ai_assistant identity behind the token.
    reset = _PRINCIPAL.set((user, org, token_id))
    try:
        out = await memory_write(
            token="",
            org_id="",
            text="agent authored payload zeta",
            operation_id="mcp-w",
            origin_model_id="claude-test",
        )
        assert out["created_by"] == str(ident.id)
        assert out["origin_model_id"] == "claude-test"

        found = await memory_search(
            token="",
            org_id="",
            query="agent authored payload zeta",
            operation_id="mcp-q",
            created_by=str(ident.id),
        )
        assert any(h["blob"]["id"] == out["id"] for h in found["hits"])

        # A different author filter must not surface it.
        other = await memory_search(
            token="",
            org_id="",
            query="agent authored payload zeta",
            operation_id="mcp-q2",
            created_by=str(uuid.uuid4()),
        )
        assert not any(h["blob"]["id"] == out["id"] for h in other["hits"])
    finally:
        _PRINCIPAL.reset(reset)
