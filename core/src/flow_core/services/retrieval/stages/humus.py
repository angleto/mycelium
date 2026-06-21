"""Humus retrieval source + hard cap (ADR-0034, task 06fbf2a7).

``humus_flag`` is set at write time by the decomposition pipeline
(ADR-0039) on notes that have decomposed into atoms; until now nothing
on the read path used it. ADR-0034 closes the loop for the *focused
walk* (the seeded RAG retrieval in ``memory.retrieve``): humus is a
PARALLEL retrieval source, late-fused into the final list via RRF with a
small fixed boost, then hard-capped at a fraction of the slots so even a
strong-but-old atom cannot crowd out live notes.

Two stages:

- :class:`HumusStage` runs the SAME lexical+semantic retrieval as the
  main pipeline but restricted to the humus subset (blobs whose source
  note carries ``humus_flag``), fuses it into one ranked ``humus`` branch
  and stamps each candidate ``provenance='humus'``. Reusing the canonical
  stages (rather than re-issuing the dual-tier vector SQL) keeps the
  humus list ranked exactly like live results.
- :class:`HumusCapStage` enforces the hard cap (ADR-0034: 30% of the
  focused-walk slots). It runs AFTER ordering so the kept humus are the
  most relevant; the freed slots fall through to live candidates ranked
  just below.

The boost is a fixed RRF weight + a small per-branch ``k`` (wired in
``memory.retrieve``), not learned, so humus quality cannot game the loop.
Anti-monoculture (ADR-0033 / ``RelativeFloorStage``) still operates on
the final, unified list.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from sqlalchemy import ColumnElement, String, cast, select

from flow_core.models.memory_blob import BlobSource, MemoryBlob
from flow_core.models.note import Note
from flow_core.models.note_part import NotePart
from flow_core.services.retrieval.stages.fusion import RRFFusionStage
from flow_core.services.retrieval.stages.lexical import LexicalFTSStage
from flow_core.services.retrieval.stages.order_limit import OrderingStage
from flow_core.services.retrieval.stages.semantic import SemanticDenseStage
from flow_core.services.retrieval.types import (
    Candidate,
    RetrievalContext,
    Stage,
    merge_candidates,
)

# The marker carried on a humus-sourced candidate (and surfaced to the
# SPA as the leaf icon). Single source of truth so the cap stage and the
# Hit mapping agree.
HUMUS_PROVENANCE = "humus"


def humus_blob_predicate(org_id: uuid.UUID) -> ColumnElement[bool]:
    """SQL predicate selecting blobs whose source note carries
    ``humus_flag``. A note blob is ``BlobSource(source_kind='note_part',
    source_id=str(part.id))`` (services.note_search) and ``humus_flag``
    lives on ``notes``, so the join is blob -> note_part -> note. The
    ``note_part`` filter bounds the join to note blobs; ``NotePart.id`` is
    cast to text (always safe) to meet the text ``source_id`` column."""
    return MemoryBlob.id.in_(
        select(BlobSource.blob_id)
        .join(NotePart, cast(NotePart.id, String) == BlobSource.source_id)
        .join(Note, Note.id == NotePart.note_id)
        .where(
            BlobSource.source_kind == "note_part",
            BlobSource.org_id == org_id,
            Note.humus_flag.is_(True),
            # ADR-0043 D2: an autonomously-generated humus note awaiting
            # human review (``review_state='proposed'``) is withheld from the
            # walk until approved. NULL/'approved' both pass (IS DISTINCT FROM).
            Note.review_state.is_distinct_from("proposed"),
        )
    )


def proposed_note_blob_exclusion(org_id: uuid.UUID) -> ColumnElement[bool]:
    """SQL predicate EXCLUDING blobs whose source note is an autonomously-
    generated proposal awaiting human review (``review_state='proposed'``,
    ADR-0043 D2). ANDed into ``memory.retrieve``'s base ``tag_clauses`` so the
    lexical, dense AND humus stages all withhold a proposed note in one place.
    Unconditional: when no proposed note exists the subquery is empty and the
    ``NOT IN`` is a no-op, so behaviour is byte-identical."""
    return MemoryBlob.id.notin_(
        select(BlobSource.blob_id)
        .join(NotePart, cast(NotePart.id, String) == BlobSource.source_id)
        .join(Note, Note.id == NotePart.note_id)
        .where(
            BlobSource.source_kind == "note_part",
            BlobSource.org_id == org_id,
            Note.review_state == "proposed",
        )
    )


@dataclass
class HumusStage(Stage):
    name: str = "humus"
    oversample: int = 50
    min_similarity: float = 0.0

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        # Restrict the canonical lexical+semantic retrieval to the humus
        # subset by AND-ing the humus predicate into the per-call tag
        # clauses (the lexical/semantic stages already fold ``tag_clauses``
        # into their WHERE). A cloned, non-mutated context preserves the
        # immutability contract of RetrievalContext.
        sub_ctx = replace(
            ctx,
            tag_clauses=(*ctx.tag_clauses, humus_blob_predicate(ctx.org_id)),
        )
        sub_stages: list[Stage] = [
            LexicalFTSStage(oversample=self.oversample),
            SemanticDenseStage(oversample=self.oversample, min_similarity=self.min_similarity),
            RRFFusionStage(),
            OrderingStage(),
        ]
        ranked: list[Candidate] = []
        for st in sub_stages:
            ranked = await st.run(query, sub_ctx, ranked)
        # Re-key the fused humus order as a single ``humus`` branch (1-based
        # rank) and stamp provenance; the outer RRF then fuses + boosts it.
        humus = [
            Candidate(
                blob_id=c.blob_id,
                scores_by_stage={self.name: float(i + 1)},
                provenance=HUMUS_PROVENANCE,
            )
            for i, c in enumerate(ranked[: self.oversample])
        ]
        return merge_candidates(candidates, humus)


@dataclass
class HumusCapStage(Stage):
    """Hard cap on humus-sourced slots (ADR-0034). Keeps at most
    ``floor(limit * ratio)`` humus candidates (the most relevant, since
    this runs after ordering); the rest are dropped so live candidates
    ranked just below take those slots. A pure list filter -- no I/O."""

    name: str = "humus_cap"
    ratio: float = 0.3
    limit: int = 10

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        budget = int(self.limit * self.ratio)
        kept = 0
        out: list[Candidate] = []
        for c in candidates:
            if c.provenance == HUMUS_PROVENANCE:
                if kept >= budget:
                    continue
                kept += 1
            out.append(c)
        return out
