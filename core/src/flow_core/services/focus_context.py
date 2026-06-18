"""PPR-seeded focus context: the reading set around a seed (WS-B2, ADR-0034).

``compute_personalized_pagerank`` already ranks the subgraph around a seed
note, but until now only the mindmap SPA consumed it -- an agent asking a
question could not pull "the relevant subgraph and nothing else". This
service exposes that as a resolvable reading set: top notes by induced PPR
mass from the seed, each carrying a title + snippet so the caller can decide
what to read without N follow-up lookups.

When a ``query`` is given the PPR neighbourhood is RE-RANKED by late RRF
fusion with the hybrid retrieval ranking (graph proximity AND content
relevance), so a question about the seed surfaces the parts of its
neighbourhood that actually answer it. Vendor-neutral: no LLM, pure graph +
retrieval. Read-only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.membership import Role
from flow_core.models.memory_blob import MemoryBlob
from flow_core.models.note import Note
from flow_core.models.note_part_index_pointer import NotePartIndexPointer
from flow_core.services import graph as graph_svc
from flow_core.services import memory as memory_svc
from flow_core.services.rbac import require_role

# RRF constant for the late fusion of the PPR rank and the retrieval rank.
# Matches the retrieval pipeline's k so the two fused rankings are on the
# same scale.
_FUSION_K = 60
# How many PPR neighbours to keep as the re-ranking pool before the query
# fusion narrows to ``budget``. A multiple of budget so content relevance
# can pull a slightly-less-central but on-topic note into the final set.
_POOL_FACTOR = 3
_SNIPPET_CHARS = 280


@dataclass(frozen=True)
class FocusNode:
    note_id: uuid.UUID
    title: str | None
    snippet: str | None
    ppr_mass: float
    # Fused PPR+RRF score when a query was given; the PPR mass otherwise.
    score: float
    # "humus" when the note is archived/decomposed material (ADR-0034), so
    # the caller can render the leaf marker, else None.
    provenance: str | None


async def focus_context(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    seed_id: uuid.UUID,
    budget: int = 24,
    query: str | None = None,
    project_id: uuid.UUID | None = None,
) -> list[FocusNode]:
    """The PPR-seeded reading set around ``seed_id``.

    Returns up to ``budget`` notes ordered by induced PPR mass (the seed's
    "neighbourhood of attention"), each resolved to title + snippet. With a
    ``query`` the neighbourhood is re-ranked by late RRF fusion with the
    hybrid retrieval ranking. RLS-scoped; member role required.
    """
    await require_role(session, org_id, actor_id, Role.member)
    ranks = await graph_svc.compute_personalized_pagerank(
        session, org_id=org_id, seed_ids=[seed_id]
    )
    # The seed's neighbourhood by induced mass (drop the seed itself and
    # zero-mass nodes the walk never reached).
    ppr_ordered = sorted(
        ((nid, m) for nid, m in ranks.items() if nid != seed_id and m > 0.0),
        key=lambda kv: (-kv[1], str(kv[0])),
    )
    if not ppr_ordered:
        return []
    pool = ppr_ordered[: max(budget * _POOL_FACTOR, budget)]
    ppr_rank = {nid: i for i, (nid, _) in enumerate(pool)}
    mass = {nid: m for nid, m in pool}

    scored: list[tuple[uuid.UUID, float]]
    if query:
        rrf_rank = await _retrieval_ranks(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=project_id,
            query=query,
            seed_id=seed_id,
            limit=len(pool),
        )
        # Late RRF fusion: every pool node keeps its PPR term; nodes that
        # also surface for the query get the retrieval term too.
        scored = sorted(
            (
                (
                    nid,
                    1.0 / (_FUSION_K + r)
                    + (1.0 / (_FUSION_K + rrf_rank[nid]) if nid in rrf_rank else 0.0),
                )
                for nid, r in ppr_rank.items()
            ),
            key=lambda kv: (-kv[1], str(kv[0])),
        )
    else:
        scored = [(nid, mass[nid]) for nid, _ in pool]

    final = scored[:budget]
    note_ids = [nid for nid, _ in final]
    titles, humus = await _titles_and_humus(session, note_ids)
    snippets = await _snippets(session, note_ids)
    return [
        FocusNode(
            note_id=nid,
            title=titles.get(nid),
            snippet=snippets.get(nid),
            ppr_mass=mass[nid],
            score=score,
            provenance="humus" if humus.get(nid) else None,
        )
        for nid, score in final
    ]


async def _retrieval_ranks(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: uuid.UUID | None,
    query: str,
    seed_id: uuid.UUID,
    limit: int,
) -> dict[uuid.UUID, int]:
    """Map the hybrid retrieval ranking for ``query`` back to note ids (via
    the note-part index pointer), keeping the best rank per note. Notes are
    first-class retrieval hits (their parts are indexed as blobs), so this is
    a note-space ranking despite retrieve() working over blobs."""
    hits = await memory_svc.retrieve(
        session,
        org_id=org_id,
        actor_id=actor_id,
        project_id=project_id,
        query=query,
        operation_id=f"focus:{seed_id}",
        limit=limit,
    )
    if not hits:
        return {}
    blob_ids = [h.blob.id for h in hits]
    rows = (
        await session.execute(
            select(NotePartIndexPointer.blob_id, NotePartIndexPointer.note_id).where(
                NotePartIndexPointer.blob_id.in_(blob_ids)
            )
        )
    ).all()
    blob_to_note = {bid: nid for bid, nid in rows}
    rrf_rank: dict[uuid.UUID, int] = {}
    for rank, h in enumerate(hits):
        nid = blob_to_note.get(h.blob.id)
        if nid is not None and nid not in rrf_rank:
            rrf_rank[nid] = rank
    return rrf_rank


async def _titles_and_humus(
    session: AsyncSession, note_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, str | None], dict[uuid.UUID, bool]]:
    if not note_ids:
        return {}, {}
    rows = (
        await session.execute(
            select(Note.id, Note.title, Note.humus_flag).where(Note.id.in_(note_ids))
        )
    ).all()
    titles = {nid: title for nid, title, _ in rows}
    humus = {nid: bool(flag) for nid, _, flag in rows}
    return titles, humus


async def _snippets(
    session: AsyncSession, note_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    """One head-of-text snippet per note from its indexed part blobs (the
    same text the search uses: title || body). Deterministic pick (lowest
    part id) when a note has several parts."""
    if not note_ids:
        return {}
    rows = (
        await session.execute(
            select(
                NotePartIndexPointer.note_id,
                NotePartIndexPointer.part_id,
                MemoryBlob.text,
            )
            .join(MemoryBlob, MemoryBlob.id == NotePartIndexPointer.blob_id)
            .where(NotePartIndexPointer.note_id.in_(note_ids))
        )
    ).all()
    best: dict[uuid.UUID, tuple[uuid.UUID, str | None]] = {}
    for nid, pid, text in rows:
        cur = best.get(nid)
        if cur is None or str(pid) < str(cur[0]):
            best[nid] = (pid, text)
    out: dict[uuid.UUID, str | None] = {}
    for nid, (_, text) in best.items():
        if text:
            snippet = " ".join(text.split())
            out[nid] = snippet[:_SNIPPET_CHARS]
    return out


__all__ = ["FocusNode", "focus_context"]
