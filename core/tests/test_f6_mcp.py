"""F6 MCP co-equality (DB-backed): memory tools reuse the same service
layer as REST (docs/adr/0001), with the embedder factory overridden by
a deterministic in-memory one (ADR-0012 seam)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder

from flow_core.db import admin_session
from flow_core.embedder import set_embedder_override
from flow_core.services.auth import signup
from flow_mcp.server import (
    grant_credits,
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
