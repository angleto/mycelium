"""F6 MCP co-equality (DB-backed): memory tools reuse the same service
layer as REST (docs/adr/0001), with the embedder factory overridden by
a deterministic in-memory one (ADR-0012 seam)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.db import admin_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    grant_credits,
    memory_delete_blob,
    memory_erase,
    memory_search,
    memory_write,
    upsert_rate_card,
)


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def test_mcp_memory(_embedder: None) -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP6",
        )
    token, org = r.token, str(r.org_id)

    await grant_credits(token=token, org_id=org, amount=100.0)
    await upsert_rate_card(
        token=token,
        org_id=org,
        model_id=FakeEmbedder.model_id,
        provider="local",
        credits_per_input=0.001,
    )
    b = await memory_write(
        token=token,
        org_id=org,
        text="alpha bravo charlie",
        operation_id="w1",
        source_kind="email",
        source_id="m1",
    )
    assert b["tier"] == "hot"

    hits = await memory_search(token=token, org_id=org, query="bravo", operation_id="q1")
    assert b["id"] in {h["blob"]["id"] for h in hits}

    erased = await memory_erase(token=token, org_id=org, source_kind="email", source_id="m1")
    assert erased["deleted"] == 1


async def test_mcp_memory_delete_blob(_embedder: None) -> None:
    """MCP parity for DELETE /memory/blobs/{id}: a written blob is
    hard-deleted via the tool and no longer retrievable."""
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP6D",
        )
    token, org = r.token, str(r.org_id)
    await grant_credits(token=token, org_id=org, amount=100.0)
    await upsert_rate_card(
        token=token,
        org_id=org,
        model_id=FakeEmbedder.model_id,
        provider="local",
        credits_per_input=0.001,
    )
    proj = str(uuid.uuid4())
    b = await memory_write(
        token=token, org_id=org, text="delta echo foxtrot", operation_id="wd", project_id=proj
    )
    bid = b["id"]
    hits = await memory_search(
        token=token, org_id=org, query="echo", operation_id="qd", project_id=proj
    )
    assert bid in {h["blob"]["id"] for h in hits}

    res = await memory_delete_blob(token=token, org_id=org, blob_id=bid)
    assert res == {"blob_id": bid, "deleted": True}

    # Gone: a fresh search no longer returns it.
    after = await memory_search(
        token=token, org_id=org, query="echo", operation_id="qd2", project_id=proj
    )
    assert bid not in {h["blob"]["id"] for h in after}
