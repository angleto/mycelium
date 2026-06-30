"""Embedding backfill (task 5276207e).

Re-embeds missing vectors for the current tenant, both tiers, so a dim
rebuild or a per-org hosted-embedder opt-in converges in the background
instead of a write-blocking big-bang:

- LOCAL ``embedding``: rows where it is NULL (e.g. after the 0028 dim
  rebuild, or a keyword-only task-search write). Uses the local embedder.
- HOSTED ``embedding_hosted``: rows where it is NULL, only when the org
  has a hosted embedder configured (``resolve_hosted_embedder``).

Race protection: every UPDATE keeps the ``IS NULL`` guard so a concurrent
worker or a fresh write to the same row is honored (no double work). The
periodic loop wrapper lives in ``worker/embedding_migration.py``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.embedder import Embedder, embed_batch, get_embedder
from mycelium_core.models.memory_blob import MemoryBlob

logger = logging.getLogger(__name__)


async def _backfill_tier(
    session: AsyncSession,
    *,
    embedder: Embedder,
    expected_dim: int,
    is_null: Any,
    set_values: Callable[..., Mapping[str, Any]],
    batch_size: int,
    tier: str,
) -> int:
    """Embed a batch of rows missing one tier's vector and UPDATE under
    the IS NULL guard. ``is_null`` is the missing-vector predicate and
    ``set_values(blob_id, org_id, result)`` builds the UPDATE values."""
    rows = (
        await session.execute(
            select(MemoryBlob.id, MemoryBlob.org_id, MemoryBlob.text)
            .where(is_null, MemoryBlob.text.is_not(None))
            .limit(batch_size)
        )
    ).all()
    # Skip empty/whitespace bodies (text IS NOT NULL still admits ""): they
    # embed to nothing and would just churn the same rows every sweep.
    candidates = [(bid, borg, txt) for bid, borg, txt in rows if txt and txt.strip()]
    if not candidates:
        return 0
    # Embed the whole batch in ONE forward pass instead of N sequential
    # ``embed`` calls: SentenceTransformer batches internally, ~an order of
    # magnitude faster (per-call tokenizer/Python overhead dominates at
    # small N). ``embed_batch`` falls back to a sequential loop for embedders
    # without a batch method (e.g. the CI fake / a future hosted provider).
    try:
        results = await embed_batch(embedder, [txt for _, _, txt in candidates])
    except Exception:
        # Fail LOUD, not silent: a systemic embedder failure (e.g. the model
        # extra missing from this process image) used to be swallowed at
        # ``debug`` per row, so the backfill no-opped invisibly and the dense
        # tier stayed empty with no signal. Surface it and skip this sweep.
        logger.warning(
            "%s backfill: embedder failed on %d eligible row(s) "
            "(model unavailable in this process?); skipping this sweep",
            tier,
            len(candidates),
            exc_info=True,
        )
        return 0
    done = 0
    for (blob_id, blob_org, _), result in zip(candidates, results, strict=False):
        if not result.vector or len(result.vector) != expected_dim:
            logger.warning(
                "%s backfill dim mismatch for blob_id=%s (got %d, expected %d)",
                tier,
                blob_id,
                len(result.vector) if result.vector else 0,
                expected_dim,
            )
            continue
        upd = await session.execute(
            update(MemoryBlob)
            .where(MemoryBlob.id == blob_id, MemoryBlob.org_id == blob_org, is_null)
            .values(**set_values(result))
        )
        if (upd.rowcount or 0) > 0:  # type: ignore[attr-defined]
            done += 1
    return done


async def run_embedding_backfill(
    session: AsyncSession, org_id: uuid.UUID, *, batch_size: int = 50
) -> int:
    """Backfill both tiers for the current tenant; returns rows touched."""
    settings = get_settings()
    done = await _backfill_tier(
        session,
        embedder=get_embedder(),
        expected_dim=settings.embed_dim,
        is_null=MemoryBlob.embedding.is_(None),
        set_values=lambda r: {
            "embedding": r.vector,
            "model_id": r.model_id,
            "dim": len(r.vector),
        },
        batch_size=batch_size,
        tier="local",
    )
    from mycelium_core.services.embedder_resolver import resolve_hosted_embedder

    hosted = await resolve_hosted_embedder(session, org_id)
    if hosted is not None:
        done += await _backfill_tier(
            session,
            embedder=hosted[0],
            expected_dim=settings.embed_dim_hosted,
            is_null=MemoryBlob.embedding_hosted.is_(None),
            set_values=lambda r: {
                "embedding_hosted": r.vector,
                "model_id_hosted": r.model_id,
                "dim_hosted": len(r.vector),
            },
            batch_size=batch_size,
            tier="hosted",
        )
    return done


async def migration_status(session: AsyncSession) -> dict[str, int]:
    """Backfill coverage for the current tenant. ``migrated`` is the
    always-on LOCAL tier; ``hosted`` is the optional hosted tier."""
    total = (
        await session.execute(
            select(func.count()).select_from(MemoryBlob).where(MemoryBlob.text.is_not(None))
        )
    ).scalar_one()
    local_done = (
        await session.execute(
            select(func.count()).select_from(MemoryBlob).where(MemoryBlob.embedding.is_not(None))
        )
    ).scalar_one()
    hosted_done = (
        await session.execute(
            select(func.count())
            .select_from(MemoryBlob)
            .where(MemoryBlob.embedding_hosted.is_not(None))
        )
    ).scalar_one()
    return {
        "total": int(total),
        "migrated": int(local_done),
        "pending": int(total) - int(local_done),
        "hosted": int(hosted_done),
    }
