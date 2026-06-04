"""Two-tier embedding backfill + per-org hosted embedder (task 5276207e).

The local tier (bge-m3, ``embedding``) is always written; the hosted tier
(``embedding_hosted`` halfvec) is per-org. Tests inject in-memory fakes
via the override seams (no real model / network) and exercise the
DB-bound write + backfill round-trip + the fail-closed key probe.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from _fake_embedder import FakeEmbedder
from httpx import Response
from sqlalchemy import select

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.embedder import (
    EmbedResult,
    set_embedder_override,
    set_hosted_embedder_override,
)
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.memory_blob import MemoryBlob
from flow_core.services import embedder_resolver, memory
from flow_core.services import embedding_migration as svc
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


class FakeHostedEmbedder:
    """Deterministic 4000-dim unit vector (the hosted fleet dim)."""

    model_id = "fake-hosted"

    async def embed(self, text: str) -> EmbedResult:
        dim = get_settings().embed_dim_hosted
        vec = [0.0] * dim
        vec[len(text) % dim] = 1.0
        return EmbedResult(vector=vec, model_id=self.model_id, tokens=max(1, len(text.split())))


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    yield
    set_embedder_override(None)
    set_hosted_embedder_override(None)


async def test_local_write_then_hosted_backfill() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EMB")
    org, user = a.org_id, a.user_id
    settings = get_settings()

    # Local-only write (no hosted embedder yet): embedding populated at the
    # local dim, embedding_hosted NULL.
    async with tenant_session(str(org), str(user)) as s:
        blob = await memory.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="hello world embeddings",
            operation_id="op-write",
        )
        row = (
            await s.execute(
                select(MemoryBlob).where(MemoryBlob.id == blob.id, MemoryBlob.org_id == org)
            )
        ).scalar_one()
        assert row.embedding is not None and len(row.embedding) == settings.embed_dim
        assert row.embedding_hosted is None

    # Enable a hosted embedder (fake 4000d) and run the backfill: the
    # hosted tier is now populated, the local tier untouched.
    set_hosted_embedder_override(FakeHostedEmbedder)
    async with tenant_session(str(org), str(user)) as s:
        touched = await svc.run_embedding_backfill(s, org, batch_size=50)
        assert touched >= 1
        row = (
            await s.execute(
                select(MemoryBlob).where(MemoryBlob.id == blob.id, MemoryBlob.org_id == org)
            )
        ).scalar_one()
        assert row.embedding_hosted is not None
        # halfvec reads back as a pgvector HalfVector value object.
        assert len(row.embedding_hosted.to_list()) == settings.embed_dim_hosted
        assert row.model_id_hosted == "fake-hosted"

        status = await svc.migration_status(s)
        assert status["migrated"] >= 1 and status["hosted"] >= 1


@respx.mock
async def test_set_org_embedder_provider_probe_rejects_wrong_dim() -> None:
    # The candidate model returns a 1024-d vector -> below the hosted dim,
    # so the fail-closed probe rejects it (nothing persisted active).
    respx.post("https://api.scaleway.ai/v1/embeddings").mock(
        return_value=Response(
            200, json={"data": [{"embedding": [0.1] * 1024}], "usage": {"total_tokens": 1}}
        )
    )
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EMB")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError) as exc:
            await embedder_resolver.set_org_embedder_provider(
                s,
                org_id=org,
                actor_id=user,
                provider="scaleway",
                model="qwen3-embedding-8b",
                api_key="scw-bad",
            )
        assert exc.value.code is MessageCode.PROVIDER_KEY_INVALID
        assert await embedder_resolver.get_org_embedder_provider(s, org) is None


@respx.mock
async def test_set_org_embedder_provider_probe_accepts_correct_dim() -> None:
    dim = get_settings().embed_dim_hosted
    respx.post("https://api.scaleway.ai/v1/embeddings").mock(
        return_value=Response(
            200, json={"data": [{"embedding": [0.1] * dim}], "usage": {"total_tokens": 1}}
        )
    )
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EMB")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        row = await embedder_resolver.set_org_embedder_provider(
            s,
            org_id=org,
            actor_id=user,
            provider="scaleway",
            model="qwen3-embedding-8b",
            api_key="scw-good",
        )
        assert row.provider == "scaleway"
        assert row.api_key_ciphertext  # stored (encrypted)
        resolved = await embedder_resolver.resolve_hosted_embedder(s, org)
        assert resolved is not None


async def test_migration_status_returns_counts() -> None:
    """migration_status SELECTs total + local + hosted counters."""
    session = AsyncMock()
    total, local_done, hosted_done = MagicMock(), MagicMock(), MagicMock()
    total.scalar_one.return_value = 100
    local_done.scalar_one.return_value = 60
    hosted_done.scalar_one.return_value = 12
    session.execute = AsyncMock(side_effect=[total, local_done, hosted_done])
    out = await svc.migration_status(session)
    assert out == {"total": 100, "migrated": 60, "pending": 40, "hosted": 12}
