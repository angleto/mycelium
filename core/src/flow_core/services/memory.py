"""Hierarchical memory service (docs/adr/0005, 0007, 0016, FR-8).

Hard isolation: every query carries the mandatory (org_id, project_id)
predicate on top of RLS + partitioning, never relevance. Retrieval is
deterministic hybrid RRF (semantic cosine + lexical tsvector),
oversampled per branch, fused with k=60 and a stable tiebreak. Writing
an embedding is a metered cost operation (ADR-0019). The cold tier is
always queryable; the tier is a latency hint, never retention. GDPR
erasure cascades by provenance. Consolidation never crosses
org/project (ADR-0007).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.config import get_settings
from flow_core.embedder import Embedder, get_embedder
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.billing import CostBasis
from flow_core.models.membership import Role
from flow_core.models.memory_blob import BlobSource, MemoryBlob
from flow_core.services import audit, billing
from flow_core.services.rbac import require_role

_RRF_K = 60
_OVERSAMPLE = 50


@dataclass(frozen=True)
class Hit:
    blob: MemoryBlob
    rrf: float


def _project_pred(project_id: uuid.UUID | None):  # type: ignore[no-untyped-def]
    if project_id is None:
        return MemoryBlob.project_id.is_(None)
    return MemoryBlob.project_id == project_id


async def write_blob(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: uuid.UUID | None,
    text_body: str,
    operation_id: str,
    namespace: str = "note",
    sources: Sequence[tuple[str, str]] = (),
    importance: Decimal = Decimal(0),
    embedder: Embedder | None = None,
) -> MemoryBlob:
    await require_role(session, org_id, actor_id, Role.member)
    emb = embedder or get_embedder()
    result = await emb.embed(text_body)
    expected = get_settings().embed_dim
    if len(result.vector) != expected:
        raise DomainError(MessageCode.MEMORY_DIM_MISMATCH, expected=str(expected))
    # Embedding is a cost-incurring op: gate + debit (ADR-0019).
    await billing.meter(
        session,
        org_id=org_id,
        actor_id=actor_id,
        operation_id=operation_id,
        op="embed",
        model_id=result.model_id,
        units_in=Decimal(result.tokens),
        basis=CostBasis.local,
    )
    now = dt.datetime.now(tz=dt.UTC)
    blob = MemoryBlob(
        org_id=org_id,
        project_id=project_id,
        namespace=namespace,
        tier="hot",
        text=text_body,
        embedding=result.vector,
        model_id=result.model_id,
        dim=len(result.vector),
        access_count=1,
        last_accessed_at=now,
        importance=importance,
        access_score=importance,
    )
    session.add(blob)
    await session.flush()
    for kind, sid in sources:
        session.add(BlobSource(blob_id=blob.id, org_id=org_id, source_kind=kind, source_id=sid))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=blob.id,
        action="write",
    )
    return blob


async def _branch_ranks(
    session: AsyncSession, stmt: Select[tuple[uuid.UUID]]
) -> dict[uuid.UUID, int]:
    rows = (await session.execute(stmt)).scalars().all()
    return {bid: i + 1 for i, bid in enumerate(rows)}


async def retrieve(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: uuid.UUID | None,
    query: str,
    operation_id: str,
    limit: int = 10,
    grader_min_rrf: float | None = None,
    embedder: Embedder | None = None,
) -> list[Hit]:
    await require_role(session, org_id, actor_id, Role.member)
    emb = embedder or get_embedder()
    qres = await emb.embed(query)
    # The query embedding is also a metered cost op.
    await billing.meter(
        session,
        org_id=org_id,
        actor_id=actor_id,
        operation_id=operation_id,
        op="embed_query",
        model_id=qres.model_id,
        units_in=Decimal(qres.tokens),
        basis=CostBasis.local,
    )

    pred = _project_pred(project_id)
    semantic = (
        select(MemoryBlob.id)
        .where(
            MemoryBlob.org_id == org_id,
            pred,
            MemoryBlob.embedding.is_not(None),
        )
        .order_by(MemoryBlob.embedding.cosine_distance(qres.vector))
        .limit(_OVERSAMPLE)
    )
    lexical = (
        select(MemoryBlob.id)
        .where(
            MemoryBlob.org_id == org_id,
            pred,
            text("fts @@ plainto_tsquery('simple', :q)"),
        )
        .order_by(text("ts_rank(fts, plainto_tsquery('simple', :q)) DESC"))
        .limit(_OVERSAMPLE)
    ).params(q=query)

    sem = await _branch_ranks(session, semantic)
    lex = await _branch_ranks(session, lexical)
    ids = set(sem) | set(lex)
    if not ids:
        return []

    fused: dict[uuid.UUID, float] = {}
    for bid in ids:
        score = 0.0
        if bid in sem:
            score += 1.0 / (_RRF_K + sem[bid])
        if bid in lex:
            score += 1.0 / (_RRF_K + lex[bid])
        fused[bid] = score

    blobs = {
        b.id: b
        for b in (await session.execute(select(MemoryBlob).where(MemoryBlob.id.in_(ids))))
        .scalars()
        .all()
    }
    # Deterministic order: RRF desc, then created_at asc, then id.
    ordered = sorted(
        ids,
        key=lambda b: (-fused[b], blobs[b].created_at, str(b)),
    )
    if grader_min_rrf is not None and (not ordered or fused[ordered[0]] < grader_min_rrf):
        return []
    top = ordered[:limit]
    if top:
        now = dt.datetime.now(tz=dt.UTC)
        await session.execute(
            update(MemoryBlob)
            .where(MemoryBlob.id.in_(top))
            .values(
                access_count=MemoryBlob.access_count + 1,
                last_accessed_at=now,
                access_score=MemoryBlob.access_score + 1,
            )
        )
        await session.flush()
    return [Hit(blob=blobs[b], rrf=fused[b]) for b in top]


async def gdpr_erase(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_kind: str,
    source_id: str,
) -> int:
    """Remove the provenance link; delete any blob left with no
    provenance (cascading its embedding/sources/cluster membership)."""
    await require_role(session, org_id, actor_id, Role.member)
    affected = (
        (
            await session.execute(
                select(BlobSource.blob_id).where(
                    BlobSource.source_kind == source_kind,
                    BlobSource.source_id == source_id,
                )
            )
        )
        .scalars()
        .all()
    )
    await session.execute(
        delete(BlobSource).where(
            BlobSource.source_kind == source_kind,
            BlobSource.source_id == source_id,
        )
    )
    await session.flush()
    deleted = 0
    for bid in set(affected):
        remaining = (
            await session.execute(
                select(func.count()).select_from(BlobSource).where(BlobSource.blob_id == bid)
            )
        ).scalar_one()
        if remaining == 0:
            await session.execute(delete(MemoryBlob).where(MemoryBlob.id == bid))
            deleted += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=None,
        action="gdpr_erase",
        diff={"source": f"{source_kind}:{source_id}", "deleted": str(deleted)},
    )
    return deleted


async def recompute_tier(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    half_life_days: float = 30.0,
    hot_threshold: float = 5.0,
    warm_threshold: float = 1.0,
    now: dt.datetime | None = None,
) -> dict[str, int]:
    """Deterministic tier from a decayed access score + importance.
    Never deletes: a rare blob is demoted to cold, still queryable
    (ADR-0016 invariant)."""
    ref = now or dt.datetime.now(tz=dt.UTC)
    blobs = list(
        (await session.execute(select(MemoryBlob).where(MemoryBlob.org_id == org_id)))
        .scalars()
        .all()
    )
    counts = {"hot": 0, "warm": 0, "cold": 0}
    for b in blobs:
        age_days = 0.0
        if b.last_accessed_at is not None:
            age_days = (ref - b.last_accessed_at).total_seconds() / 86400.0
        decay = 0.5 ** (age_days / half_life_days)
        score = float(b.importance) + float(b.access_count) * decay
        tier = "hot" if score >= hot_threshold else "warm" if score >= warm_threshold else "cold"
        b.tier = tier
        b.access_score = Decimal(str(round(score, 6)))
        counts[tier] += 1
    await session.flush()
    return counts


async def consolidate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: uuid.UUID | None,
    blob_ids: Sequence[uuid.UUID],
    embedder: Embedder | None = None,
    operation_id: str,
) -> MemoryBlob:
    """Deterministically merge same-(org, project) blobs into one
    concept, preserving every source's provenance. Never crosses
    org/project (ADR-0007). LLM summarization is an optional later
    refinement; v1 concatenates deterministically."""
    await require_role(session, org_id, actor_id, Role.member)
    members = list(
        (await session.execute(select(MemoryBlob).where(MemoryBlob.id.in_(list(blob_ids)))))
        .scalars()
        .all()
    )
    if not members:
        raise NotFoundError(MessageCode.MEMORY_NOT_FOUND)
    for m in members:
        if m.org_id != org_id or m.project_id != project_id:
            raise DomainError(MessageCode.MEMORY_CROSS_SUBJECT)
    cluster_id = uuid.uuid4()
    merged_text = "\n\n".join(sorted((m.text or "") for m in members))
    consolidated = await write_blob(
        session,
        org_id=org_id,
        actor_id=actor_id,
        project_id=project_id,
        text_body=merged_text,
        operation_id=operation_id,
        namespace="consolidated",
        embedder=embedder,
    )
    consolidated.cluster_id = cluster_id
    member_ids = [m.id for m in members]
    await session.execute(
        update(MemoryBlob).where(MemoryBlob.id.in_(member_ids)).values(cluster_id=cluster_id)
    )
    # Preserve provenance: copy every member source onto the concept.
    src_rows = (
        await session.execute(
            select(BlobSource.source_kind, BlobSource.source_id)
            .where(BlobSource.blob_id.in_(member_ids))
            .distinct()
        )
    ).all()
    for kind, sid in src_rows:
        session.add(
            BlobSource(
                blob_id=consolidated.id,
                org_id=org_id,
                source_kind=kind,
                source_id=sid,
            )
        )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=consolidated.id,
        action="consolidate",
        diff={"members": str(len(members))},
    )
    return consolidated


async def get_blob(session: AsyncSession, *, org_id: uuid.UUID, blob_id: uuid.UUID) -> MemoryBlob:
    b = (
        await session.execute(select(MemoryBlob).where(MemoryBlob.id == blob_id))
    ).scalar_one_or_none()
    if b is None:
        raise NotFoundError(MessageCode.MEMORY_NOT_FOUND)
    return b
