"""Embedding model migration backfill (task `1d081395`).

Sweeps blobs with ``embedding_v2 IS NULL AND text IS NOT NULL`` and
populates the v2 columns by re-embedding with the configured v2 model.
Race protection: the UPDATE WHERE clause still requires
``embedding_v2 IS NULL`` so two concurrent workers (or a worker + a
new write) don't double-spend the embed cost.

This is a service-layer helper; the periodic loop wrapper lives in
``worker/embedding_migration.py`` (same shape as the existing
task-search backfill).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.config import get_settings
from flow_core.embedder import get_embedder_v2
from flow_core.models.memory_blob import MemoryBlob

logger = logging.getLogger(__name__)


async def run_embedding_migration(session: AsyncSession, *, batch_size: int = 50) -> int:
    """Backfill ``embedding_v2`` for blobs that don't have it yet.
    Returns the count of blobs migrated in this batch.

    The selector picks WHERE ``embedding_v2 IS NULL AND text IS NOT
    NULL`` and limits to ``batch_size`` to keep one tick short. Per
    row: embed via the v2 model, UPDATE the three v2 columns. The
    UPDATE keeps the IS NULL guard so a concurrent worker or a fresh
    write to the same row is honored (no double-spend).

    Returns 0 when the v2 model isn't configured (the worker should
    skip silently in that case; check before calling)."""
    emb_v2 = get_embedder_v2()
    if emb_v2 is None:
        return 0
    settings = get_settings()
    expected_dim = settings.embed_dim_v2

    rows = (
        await session.execute(
            select(MemoryBlob.id, MemoryBlob.org_id, MemoryBlob.text)
            .where(
                MemoryBlob.embedding_v2.is_(None),
                MemoryBlob.text.is_not(None),
            )
            .limit(batch_size)
        )
    ).all()
    if not rows:
        return 0

    migrated = 0
    for blob_id, org_id, text_body in rows:
        if not text_body:
            continue
        try:
            result = await emb_v2.embed(text_body)
        except Exception as exc:
            logger.debug("embedding migration failed for blob_id=%s: %s", blob_id, exc)
            continue
        if not result.vector or len(result.vector) != expected_dim:
            logger.warning(
                "embedding migration dim mismatch for blob_id=%s (got %d, expected %d)",
                blob_id,
                len(result.vector) if result.vector else 0,
                expected_dim,
            )
            continue
        update_result: CursorResult[tuple[uuid.UUID, ...]] = await session.execute(  # type: ignore[assignment]
            update(MemoryBlob)
            .where(
                MemoryBlob.id == blob_id,
                MemoryBlob.org_id == org_id,
                MemoryBlob.embedding_v2.is_(None),
            )
            .values(
                embedding_v2=result.vector,
                model_id_v2=result.model_id,
                dim_v2=len(result.vector),
            )
        )
        if update_result.rowcount > 0:
            migrated += 1
    return migrated


async def migration_status(session: AsyncSession) -> dict[str, int]:
    """How far along the migration is for the current tenant.
    Returns ``{total, migrated, pending}`` over visible blobs."""
    from sqlalchemy import func

    total_q = select(func.count()).select_from(MemoryBlob).where(MemoryBlob.text.is_not(None))
    migrated_q = (
        select(func.count()).select_from(MemoryBlob).where(MemoryBlob.embedding_v2.is_not(None))
    )
    total = (await session.execute(total_q)).scalar_one()
    migrated = (await session.execute(migrated_q)).scalar_one()
    return {
        "total": int(total),
        "migrated": int(migrated),
        "pending": int(total) - int(migrated),
    }
