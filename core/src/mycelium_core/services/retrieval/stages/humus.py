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

from sqlalchemy import ColumnElement, String, and_, cast, or_, select

from mycelium_core.models.memory_blob import BlobSource, MemoryBlob
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart
from mycelium_core.models.task import Task
from mycelium_core.models.task_index_pointer import TaskIndexPointer
from mycelium_core.services.note_effective import effective_note_clause
from mycelium_core.services.retrieval.stages.fusion import RRFFusionStage
from mycelium_core.services.retrieval.stages.lexical import LexicalFTSStage
from mycelium_core.services.retrieval.stages.order_limit import OrderingStage
from mycelium_core.services.retrieval.stages.semantic import SemanticDenseStage
from mycelium_core.services.retrieval.types import (
    Candidate,
    RetrievalContext,
    Stage,
    merge_candidates,
)

# The marker carried on a humus-sourced candidate (and surfaced to the
# SPA as the leaf icon). Single source of truth so the cap stage and the
# Hit mapping agree.
HUMUS_PROVENANCE = "humus"


def humus_blob_predicate(
    org_id: uuid.UUID, kinds: frozenset[str] | None = None
) -> ColumnElement[bool]:
    """SQL predicate selecting blobs whose source note carries
    ``humus_flag``. A note blob is ``BlobSource(source_kind='note_part',
    source_id=str(part.id))`` (services.note_search) and ``humus_flag``
    lives on ``notes``, so the join is blob -> note_part -> note. The
    ``note_part`` filter bounds the join to note blobs; ``NotePart.id`` is
    cast to text (always safe) to meet the text ``source_id`` column.

    ``kinds`` optionally restricts to specific ``humus_kind`` values
    (``distillation`` | ``pattern`` | ``season``); None/empty = every humus
    atom (historical behaviour). NOTE the limit of this lever: it narrows the
    humus BRANCH (which atoms get the boost + the ``provenance='humus'``
    marker), it does NOT remove the other kinds from retrieval -- every atom
    stays reachable through the base lexical/dense branches. Per-kind
    atom-PRESENCE A/B is done at the case level (ConsolidationCase sources),
    not with this knob."""
    conds = [
        BlobSource.source_kind == "note_part",
        BlobSource.org_id == org_id,
        Note.humus_flag.is_(True),
        # Effective source notes only: a humus atom awaiting human review
        # (ADR-0043 D2) or sitting in the bin (task c5da112c) is out of the
        # walk. The base ``ineffective_source_blob_exclusion`` already
        # withholds it from every branch; this keeps the humus predicate
        # correct standalone (it is also used outside the pipeline, e.g. the
        # free wander set). Read from the shared note predicate so this copy
        # cannot drift the way the ~10 hand-written ones did (task f8402e7f).
        effective_note_clause(),
    ]
    if kinds:
        conds.append(Note.humus_kind.in_(tuple(kinds)))
    return MemoryBlob.id.in_(
        select(BlobSource.blob_id)
        .join(NotePart, cast(NotePart.id, String) == BlobSource.source_id)
        .join(Note, Note.id == NotePart.note_id)
        .where(*conds)
    )


def humus_note_blob_exclusion(org_id: uuid.UUID) -> ColumnElement[bool]:
    """SQL predicate EXCLUDING every humus-atom blob (source note carries
    ``humus_flag``) from a retrieval branch. ANDed into the base
    ``tag_clauses`` when ``memory.retrieve(..., exclude_humus_from_base=True)``
    so the lexical AND dense branches behave as if the atoms were never
    written -- the 'atoms fully absent' arm (Config C) of the humus empirical
    A/B (task 4836a6cc), used to check that a consolidation query is genuinely
    answered by NO raw blob. Unconditional NOT IN: an empty subquery is a
    no-op, so it never fires unless the caller opts in.

    No ``review_state`` filter here, deliberately: ``proposed`` notes are
    already withheld from every branch by ``proposed_note_blob_exclusion``
    (always ANDed into the base tag_clauses in ``memory.retrieve``), so this
    exclusion only needs to remove the remaining, effective atoms."""
    return MemoryBlob.id.notin_(
        select(BlobSource.blob_id)
        .join(NotePart, cast(NotePart.id, String) == BlobSource.source_id)
        .join(Note, Note.id == NotePart.note_id)
        .where(
            BlobSource.source_kind == "note_part",
            BlobSource.org_id == org_id,
            Note.humus_flag.is_(True),
        )
    )


def proposed_note_blob_exclusion(org_id: uuid.UUID) -> ColumnElement[bool]:
    """SQL predicate EXCLUDING blobs whose source note is an autonomously-
    generated proposal awaiting human review (``review_state='proposed'``,
    ADR-0043 D2). ANDed into ``memory.retrieve``'s base ``tag_clauses`` so the
    lexical, dense AND humus stages all withhold a proposed note in one place.
    Unconditional: when no proposed note exists the subquery is empty and the
    ``NOT IN`` is a no-op, so behaviour is byte-identical.

    Subsumed by :func:`ineffective_source_blob_exclusion` on the retrieval
    path (which also withholds soft-deleted sources); kept for callers and
    tests that need the proposed-only predicate in isolation."""
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


def ineffective_source_blob_exclusion(
    org_id: uuid.UUID, *, include_deleted: bool = False
) -> ColumnElement[bool]:
    """The blob-side effective-source predicate: EXCLUDE every blob whose
    source row is not currently effective (task c5da112c).

    This is the single place where "which source states keep their blobs
    retrievable" is defined for the memory surfaces:

    - a note that is an unreviewed autonomous proposal
      (``review_state='proposed'``, ADR-0043 D2) or SOFT-DELETED
      (``deleted_at`` set) withholds every blob carrying ``note_part``
      provenance from it -- the same join shape the proposed-only
      exclusion always used, so pointer-less legacy part blobs are
      covered too, and a consolidated blob is (conservatively) withheld
      while ANY of its member parts belongs to an ineffective note;
    - a soft-deleted task withholds its INDEX blob only, resolved via
      ``task_index_pointer`` (the ``task_search`` loader deliberately
      keeps the blob in place on soft-delete and defers visibility to
      search time -- this is that search-time filter for the blob
      surfaces). The pointer leg is deliberate: ``('task', id)`` in
      ``blob_sources`` is ALSO how independent agent memories cite the
      task they came from, and those must NOT vanish with the task.

    The perimeter is DERIVED from the source row at query time, so there
    is no duplicated state to maintain on delete/restore and no
    divergence window: restoring a note or task makes its blobs
    retrievable again with no re-index. Hard deletion is the
    complementary path: the provenance/pointer rows die with the row, so
    these joins no longer see the blob -- which is why every hard-delete
    path must erase the index blobs itself (``memory.
    erase_blobs_for_sources`` / the retention sweep) instead of relying
    on this predicate. Unconditional NOT IN: with no ineffective source
    the subqueries are empty and the clause is a no-op (all three
    subquery columns are NOT NULL, so the anti-join cannot poison).

    Deliberately NOT hidden: blobs whose only tie to a trashed row is
    whole-entity citation provenance (``('note', id)`` / ``('task',
    id)``) -- the kind namespace is shared with legacy index rows, but
    an independent memory citing a source must survive that source's
    trip to the trash.

    ``include_deleted=True`` is the unified /search opt-in ("show me the
    bin too"): it drops ONLY the soft-delete legs. The ADR-0043
    ``proposed`` withholding is not an option and always stands."""
    ineffective_note: ColumnElement[bool] = Note.review_state == "proposed"
    if not include_deleted:
        ineffective_note = or_(ineffective_note, Note.deleted_at.is_not(None))
    ineffective_note_blobs = (
        select(BlobSource.blob_id)
        .join(NotePart, cast(NotePart.id, String) == BlobSource.source_id)
        .join(Note, Note.id == NotePart.note_id)
        .where(
            BlobSource.source_kind == "note_part",
            BlobSource.org_id == org_id,
            ineffective_note,
        )
    )
    if include_deleted:
        return MemoryBlob.id.notin_(ineffective_note_blobs)
    deleted_task_blobs = (
        select(TaskIndexPointer.blob_id)
        .join(Task, Task.id == TaskIndexPointer.task_id)
        .where(
            TaskIndexPointer.org_id == org_id,
            Task.deleted_at.is_not(None),
        )
    )
    return and_(
        MemoryBlob.id.notin_(ineffective_note_blobs),
        MemoryBlob.id.notin_(deleted_task_blobs),
    )


@dataclass
class HumusStage(Stage):
    name: str = "humus"
    oversample: int = 50
    min_similarity: float = 0.0
    # Optional restriction to specific ``humus_kind`` values; None = all
    # (historical). Branch attribution only -- see humus_blob_predicate:
    # atoms of the other kinds remain retrievable via the base branches.
    kinds: frozenset[str] | None = None

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
            tag_clauses=(*ctx.tag_clauses, humus_blob_predicate(ctx.org_id, self.kinds)),
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
