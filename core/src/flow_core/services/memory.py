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

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.config import get_settings
from flow_core.embedder import Embedder, EmbedResult, get_embedder, get_embedder_v2
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.billing import CostBasis
from flow_core.models.membership import Role
from flow_core.models.memory_blob import BlobSource, MemoryBlob, MemoryBlobTag
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task_tag import TaskTag
from flow_core.services import audit, billing, taxonomy
from flow_core.services.chunker import Chunker
from flow_core.services.rbac import require_role

_RRF_K = 60
_OVERSAMPLE = 50
# Sentinel model id recorded on a blob written while the embedder is
# unavailable (missing optional extra / load failure): the row is kept
# valid and FTS-searchable, just without a semantic vector. The SPA
# uses this to show "keyword-only" provenance (see /memory/status).
_NO_EMBED_MODEL = "none"


@dataclass(frozen=True)
class Hit:
    blob: MemoryBlob
    rrf: float


async def _safe_embed(emb: Embedder, text: str) -> EmbedResult | None:
    """Embed defensively. The local model depends on an optional extra
    (``sentence-transformers``); if it is missing or fails to load,
    ``embed`` raises (ImportError/RuntimeError/...). Memory must still
    work in keyword-only mode, so any failure (or an empty vector) is
    swallowed here and the caller degrades to FTS-only. Never raises
    because the embedder is unavailable."""
    try:
        result = await emb.embed(text)
    except Exception:
        # Optional dependency / model load is best-effort: any failure
        # degrades to keyword-only, never propagates to the caller.
        return None
    if not result.vector:
        return None
    return result


async def _resolve_channel_tag_id(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    channel_tag_id: uuid.UUID | None,
    channel_key: str | None = None,
) -> uuid.UUID | None:
    """Resolve the optional memory channel for a write/search.

    Two ways to address a channel, both RLS-scoped to the tenant:

    - ``channel_tag_id``: an explicit tag id. It must be a tag visible
      in this tenant (RLS scopes ``tags`` to the org) AND of kind
      ``memory_channel`` (NotFound when absent, TAG_KIND_MISMATCH when
      the wrong kind -- behaviour preserved for existing callers).
    - ``channel_key``: the DETERMINISTIC stable slug an integration
      writes into. Resolved to the tenant's *enabled* ``memory_channel``
      tag with that ``system_key`` (CHANNEL_NOT_FOUND if absent or
      disabled, or it belongs to another org -- RLS makes it invisible).

    If BOTH are given they must resolve to the SAME tag, else a domain
    error (an integration must not be ambiguous about its target).
    Manual writes stay channel-OPTIONAL: both None -> None (no forced
    default)."""
    by_key: uuid.UUID | None = None
    if channel_key is not None:
        key_tag = await taxonomy.resolve_channel_by_key(
            session, org_id=org_id, channel_key=channel_key
        )
        by_key = key_tag.id
    if channel_tag_id is None:
        return by_key
    tag = (await session.execute(select(Tag).where(Tag.id == channel_tag_id))).scalar_one_or_none()
    if tag is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    if tag.kind is not TagKind.memory_channel:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    if by_key is not None and by_key != tag.id:
        # An explicit id and a key that point at different channels: the
        # caller is ambiguous about its target, refuse rather than guess
        # (docs/adr/0021: confirm, never guess).
        raise DomainError(MessageCode.DOMAIN_ERROR)
    return tag.id


def _project_pred(project_id: uuid.UUID | None):  # type: ignore[no-untyped-def]
    if project_id is None:
        return MemoryBlob.project_id.is_(None)
    return MemoryBlob.project_id == project_id


async def _visible_tag_ids(session: AsyncSession, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Subset of ``ids`` that are tags visible in the current tenant
    (RLS scopes ``tags`` to the org): guards explicit tags so a caller
    cannot link a blob to another workspace's tag."""
    if not ids:
        return set()
    rows = await session.execute(select(Tag.id).where(Tag.id.in_(list(ids))))
    return set(rows.scalars().all())


async def _inherited_tag_ids(
    session: AsyncSession, sources: Sequence[tuple[str, str]]
) -> set[uuid.UUID]:
    """Tags inherited from the provenance: a blob derived from tagged
    sources keeps their tags. Only kinds that actually carry tags in
    the schema contribute (today: ``task`` via ``task_tags``)."""
    task_ids: list[uuid.UUID] = []
    for kind, sid in sources:
        if kind != "task":
            continue
        try:
            task_ids.append(uuid.UUID(sid))
        except ValueError:
            continue
    if not task_ids:
        return set()
    rows = await session.execute(select(TaskTag.tag_id).where(TaskTag.task_id.in_(task_ids)))
    return set(rows.scalars().all())


async def _attach_blob_tags(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    blob_id: uuid.UUID,
    tag_ids: set[uuid.UUID],
) -> None:
    """Link a freshly written blob to a deduplicated tag set."""
    for tid in tag_ids:
        session.add(MemoryBlobTag(blob_id=blob_id, org_id=org_id, tag_id=tid))
    if tag_ids:
        await session.flush()


async def tags_by_blob(
    session: AsyncSession, *, blob_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Tag]]:
    """Batched blob -> tags (chips in search results, no N+1)."""
    out: dict[uuid.UUID, list[Tag]] = {}
    if not blob_ids:
        return out
    rows = await session.execute(
        select(MemoryBlobTag.blob_id, Tag)
        .join(Tag, Tag.id == MemoryBlobTag.tag_id)
        .where(MemoryBlobTag.blob_id.in_(list(blob_ids)))
    )
    for bid, tag in rows.all():
        out.setdefault(bid, []).append(tag)
    return out


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
    tag_ids: Sequence[uuid.UUID] = (),
    channel_tag_id: uuid.UUID | None = None,
    channel_key: str | None = None,
    embedder: Embedder | None = None,
    chunker: Chunker | None = None,
) -> MemoryBlob:
    """Write a memory blob (or N blobs if the text is chunked).

    Long ``namespace='note'`` text is split into paragraph-sized
    chunks (task `bbc21aa1`); each chunk gets its own blob + own
    BlobSource(chunk_index=i). The function returns the FIRST chunk's
    blob -- callers that need every chunk can read them back via
    ``blob_sources.source_id``. Short text and other namespaces stay
    single-vector (WholeChunker), so the legacy contract is preserved.

    ``chunker`` is the explicit override: pass a Chunker instance to
    force a strategy regardless of the namespace/length heuristic.
    """
    from flow_core.services.chunker import pick_chunker

    await require_role(session, org_id, actor_id, Role.member)
    channel_id = await _resolve_channel_tag_id(
        session, org_id=org_id, channel_tag_id=channel_tag_id, channel_key=channel_key
    )
    emb = embedder or get_embedder()
    expected = get_settings().embed_dim
    selected = chunker or pick_chunker(namespace=namespace, text=text_body)
    pieces = selected.chunks(text_body)
    # Cache tag computation outside the loop: explicit/channel/inherited
    # don't change per chunk so we compute them once.
    explicit = await _visible_tag_ids(session, tag_ids)
    inherited = await _inherited_tag_ids(session, sources)
    channel = {channel_id} if channel_id is not None else set()
    all_tags = explicit | inherited | channel

    # v2 embedder optional: when configured, every new write populates
    # both columns so the search surface can dual-read immediately
    # without waiting for the worker to backfill. Cost is 2x embed
    # per write, accepted for the duration of the migration window.
    emb_v2 = get_embedder_v2()
    settings_for_v2 = get_settings()
    expected_v2 = settings_for_v2.embed_dim_v2

    first_blob: MemoryBlob | None = None
    for piece in pieces:
        result = await _safe_embed(emb, piece.text)
        if result is not None and len(result.vector) != expected:
            raise DomainError(MessageCode.MEMORY_DIM_MISMATCH, expected=str(expected))
        if result is not None:
            await billing.meter_if_billable(
                session,
                org_id=org_id,
                actor_id=actor_id,
                operation_id=operation_id,
                op="embed",
                model_id=result.model_id,
                units_in=Decimal(result.tokens),
                basis=CostBasis.local,
            )
        # v2 path: populate the v2 columns when the migration target
        # model is configured. Dim mismatch on v2 is a misconfiguration
        # of ``embed_dim_v2`` (the column type is fixed at migration
        # time), surface it; the v1 write above already succeeded so
        # the row is still searchable in keyword + v1 semantic.
        result_v2 = await _safe_embed(emb_v2, piece.text) if emb_v2 is not None else None
        if result_v2 is not None and len(result_v2.vector) != expected_v2:
            raise DomainError(MessageCode.MEMORY_DIM_MISMATCH, expected=str(expected_v2))
        if result_v2 is not None:
            await billing.meter_if_billable(
                session,
                org_id=org_id,
                actor_id=actor_id,
                operation_id=operation_id,
                op="embed_v2",
                model_id=result_v2.model_id,
                units_in=Decimal(result_v2.tokens),
                basis=CostBasis.local,
            )
        now = dt.datetime.now(tz=dt.UTC)
        blob = MemoryBlob(
            org_id=org_id,
            project_id=project_id,
            namespace=namespace,
            tier="hot",
            text=piece.text,
            embedding=result.vector if result is not None else None,
            model_id=result.model_id if result is not None else _NO_EMBED_MODEL,
            dim=len(result.vector) if result is not None else expected,
            embedding_v2=result_v2.vector if result_v2 is not None else None,
            model_id_v2=result_v2.model_id if result_v2 is not None else None,
            dim_v2=len(result_v2.vector) if result_v2 is not None else None,
            access_count=1,
            last_accessed_at=now,
            importance=importance,
            access_score=importance,
        )
        session.add(blob)
        await session.flush()
        for kind, sid in sources:
            session.add(
                BlobSource(
                    blob_id=blob.id,
                    org_id=org_id,
                    source_kind=kind,
                    source_id=sid,
                    chunk_index=piece.index,
                )
            )
        await session.flush()
        await _attach_blob_tags(
            session, org_id=org_id, blob_id=blob.id, tag_ids=all_tags
        )
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="memory_blob",
            entity_id=blob.id,
            action="write",
            diff={"chunk_index": str(piece.index)} if len(pieces) > 1 else None,
        )
        if first_blob is None:
            first_blob = blob
    if first_blob is None:
        # pick_chunker guarantees a non-empty list (WholeChunker
        # returns [Chunk(text=input, 0)] even for empty input), so
        # this branch is unreachable; raise rather than return a
        # spurious value so a future refactor can't silently corrupt
        # the contract.
        raise RuntimeError("chunker produced no chunks")
    return first_blob


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
    tag_ids: Sequence[uuid.UUID] | None = None,
    channel_tag_id: uuid.UUID | None = None,
    channel_key: str | None = None,
    embedder: Embedder | None = None,
    rerank: bool = False,
) -> list[Hit]:
    await require_role(session, org_id, actor_id, Role.member)
    channel_id = await _resolve_channel_tag_id(
        session, org_id=org_id, channel_tag_id=channel_tag_id, channel_key=channel_key
    )
    emb = embedder or get_embedder()
    qres = await _safe_embed(emb, query)
    if qres is not None:
        # The query embedding is also a metered cost op. Skipped when
        # the embedder is unavailable: there is no semantic branch and
        # thus no embedding cost (keyword-only retrieval).
        await billing.meter_if_billable(
            session,
            org_id=org_id,
            actor_id=actor_id,
            operation_id=operation_id,
            op="embed_query",
            model_id=qres.model_id,
            units_in=Decimal(qres.tokens),
            basis=CostBasis.local,
        )
    # Optional v2 query embed for the dual-read path: when a v2 model
    # is configured we embed the query with both so SemanticDenseStage
    # can hit both branches in one pass. Metered separately so the bill
    # tracks the migration cost explicitly.
    emb_v2_inst = get_embedder_v2()
    qres_v2 = await _safe_embed(emb_v2_inst, query) if emb_v2_inst is not None else None
    if qres_v2 is not None:
        await billing.meter_if_billable(
            session,
            org_id=org_id,
            actor_id=actor_id,
            operation_id=operation_id,
            op="embed_query_v2",
            model_id=qres_v2.model_id,
            units_in=Decimal(qres_v2.tokens),
            basis=CostBasis.local,
        )

    # Build the per-call context (predicates pre-computed once) and run
    # the canonical retrieval pipeline. The pipeline is the extension
    # point: rerankers / HyDE / chunking-dedupe land as additional
    # stages without touching this function.
    from flow_core.config import get_settings as _get_settings
    from flow_core.services.retrieval import RetrievalContext, RetrievalPipeline
    from flow_core.services.retrieval.stages import (
        AccessCounterStage,
        CrossEncoderRerankerStage,
        DedupeBySourceStage,
        GraderMinStage,
        LexicalFTSStage,
        LimitStage,
        OrderingStage,
        RerankGate,
        RRFFusionStage,
        SemanticDenseStage,
    )

    pred = _project_pred(project_id)
    # Tags are a facet *inside* the (org, project) boundary, never a
    # way past it: ANDed into both branches, never replacing the hard
    # predicate. A blob must carry every requested tag (faceted AND).
    # The optional memory channel is just one more required tag (it does
    # not get a special SQL path; it folds into the AND set).
    wanted = set(tag_ids or ())
    if channel_id is not None:
        wanted.add(channel_id)
    tag_clauses: tuple[ColumnElement[bool], ...] = ()
    if wanted:
        tagged = (
            select(MemoryBlobTag.blob_id)
            .where(
                MemoryBlobTag.org_id == org_id,
                MemoryBlobTag.tag_id.in_(wanted),
            )
            .group_by(MemoryBlobTag.blob_id)
            .having(func.count(func.distinct(MemoryBlobTag.tag_id)) == len(wanted))
        )
        tag_clauses = (MemoryBlob.id.in_(tagged),)

    ctx = RetrievalContext(
        session=session,
        org_id=org_id,
        actor_id=actor_id,
        project_id=project_id,
        operation_id=operation_id,
        embedder=emb,
        project_pred=pred,
        tag_clauses=tag_clauses,
        query_embedding=qres,
        extras={"query_embedding_v2": qres_v2} if qres_v2 is not None else {},
    )
    # Reranker stage is added between RRF and the final ordering only
    # when the caller asked for it (``rerank=True``) OR the workspace
    # has it enabled globally. The stage itself further gates on
    # query length and candidate count (see RerankGate); a gated-off
    # stage is a no-op so the pipeline cost is bounded by RRF.
    settings = _get_settings()
    use_rerank = rerank or settings.reranker_enabled
    from flow_core.services.retrieval.types import Stage as _Stage

    stages: list[_Stage] = [
        LexicalFTSStage(oversample=_OVERSAMPLE),
        SemanticDenseStage(oversample=_OVERSAMPLE),
        RRFFusionStage(k=_RRF_K),
    ]
    if use_rerank:
        stages.append(
            CrossEncoderRerankerStage(
                gate=RerankGate(
                    min_query_tokens=settings.reranker_min_query_tokens,
                    min_candidates=settings.reranker_min_candidates,
                ),
            )
        )
    stages.extend(
        [
            OrderingStage(),
            # DedupeBySourceStage runs after ordering so the candidate
            # kept per source is the highest-scored chunk (the order is
            # already RRF/rerank-DESC by this point). It also runs
            # BEFORE Limit so the truncation is on unique sources, not
            # on chunks (otherwise top-10 could collapse to 3 sources
            # with 7 chunks of the same parent).
            DedupeBySourceStage(),
            GraderMinStage(min_score=grader_min_rrf),
            LimitStage(k=limit),
            AccessCounterStage(),
        ]
    )
    pipeline = RetrievalPipeline(stages=stages)
    top = await pipeline.run(query, ctx)
    if not top:
        return []
    # Load full blobs for the result (the pipeline carries only ids +
    # rank; the caller still expects ``Hit(blob=MemoryBlob, rrf=score)``).
    blobs = {
        b.id: b
        for b in (
            await session.execute(
                select(MemoryBlob).where(MemoryBlob.id.in_([c.blob_id for c in top]))
            )
        )
        .scalars()
        .all()
    }
    return [Hit(blob=blobs[c.blob_id], rrf=c.score) for c in top if c.blob_id in blobs]


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
    # The concept inherits the union of its members' tags (same spirit
    # as the provenance union above; deterministic, never cross-subject
    # since all members share (org, project)).
    member_tags = set(
        (
            await session.execute(
                select(MemoryBlobTag.tag_id).where(MemoryBlobTag.blob_id.in_(member_ids)).distinct()
            )
        )
        .scalars()
        .all()
    )
    await _attach_blob_tags(session, org_id=org_id, blob_id=consolidated.id, tag_ids=member_tags)
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


async def delete_blob(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    blob_id: uuid.UUID,
) -> None:
    """Hard-delete one memory entry and everything that hangs off it.

    The blob is loaded org-scoped (RLS already constrains ``select`` to
    the tenant; a foreign / unknown id is MEMORY_NOT_FOUND -- same guard
    as ``get_blob``, so cross-org isolation is preserved). Deleting the
    ``memory_blobs`` row cascades by FK ON DELETE CASCADE to its
    ``blob_sources`` and ``memory_blob_tags`` (same composite-FK cascade
    ``gdpr_erase`` relies on); the embedding vector and the generated
    ``fts`` column live on the blob row itself and go with it. This is
    the user deleting a single entry directly (distinct from
    ``gdpr_erase``, which removes a provenance link and only deletes a
    blob left with no provenance)."""
    await require_role(session, org_id, actor_id, Role.member)
    blob = await get_blob(session, org_id=org_id, blob_id=blob_id)
    await session.execute(
        delete(MemoryBlob).where(MemoryBlob.id == blob.id, MemoryBlob.org_id == org_id)
    )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=blob_id,
        action="delete",
    )


async def attach_blob_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    blob_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    """Curate memory by hand / by the AI: add an explicit tag to an
    existing blob. Idempotent (re-adding is a no-op)."""
    await require_role(session, org_id, actor_id, Role.member)
    await get_blob(session, org_id=org_id, blob_id=blob_id)
    if not await _visible_tag_ids(session, [tag_id]):
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    try:
        async with session.begin_nested():
            session.add(MemoryBlobTag(blob_id=blob_id, org_id=org_id, tag_id=tag_id))
            await session.flush()
    except IntegrityError:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=blob_id,
        action="attach_tag",
    )


async def detach_blob_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    blob_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(MemoryBlobTag).where(
            MemoryBlobTag.blob_id == blob_id, MemoryBlobTag.tag_id == tag_id
        )
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=blob_id,
        action="detach_tag",
    )
