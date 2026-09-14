"""Graph-proximity retrieval source (Fase 4 of the search-informed graph,
task 561c6aca). DARK by default -- this is the publishable A/B lever.

Unlike the humus branch (a PARALLEL pre-fusion source), a graph seed does
not exist before the first pass: the graph is walked FROM the notes the
first pass already found. So this stage runs AFTER ``RRFFusionStage``:

1. seeds = the top-``seeds`` fused candidates (already ranked by ``score``);
2. for each seed blob resolve its note, run the bounded neighbourhood walk
   (``graph_local.bounded_neighborhood``, Fase 1 -- size-independent), and
   collect the reached notes ranked by path weight;
3. map each reached note to its indexed blob(s) and fold them in as an
   extra ``graph`` RRF branch: ``weight/(k+graph_rank)`` added to the fused
   score, exactly like any other branch. A note already in the pool gets
   the boost (graph proximity reinforces it); a note not otherwise found is
   injected with ``provenance='graph'`` so the A/B report -- and the SPA --
   can attribute the delta.

Byte-identical when off: the stage is only mounted when ``use_graph`` (like
``use_humus`` / ``use_rerank``); when the gate skips or the walk reaches
nothing new, the candidate list and scores are untouched. It records
``ctx.extras['graph_diag'] = {'ran', 'contributed'}`` for RetrievalMeta.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.services import graph_local
from mycelium_core.services.retrieval.types import Candidate, RetrievalContext, Stage

GRAPH_PROVENANCE = "graph"


@dataclass
class GraphGate:
    """Mirror of ``RerankGate``: the graph walk is only worth its per-seed
    traversals when the query has real structure and there is a pool to
    seed from."""

    min_query_tokens: int = 3
    min_candidates: int = 5

    def should_run(self, query: str, candidates: list[Candidate]) -> bool:
        if len(query.split()) < self.min_query_tokens:
            return False
        if len(candidates) < self.min_candidates:
            return False
        return True


@dataclass
class GraphProximityStage(Stage):
    name: str = "graph"
    k: int = 60
    weight: float = 0.2
    seeds: int = 3
    node_budget: int = 16
    gate: GraphGate | None = None

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        ctx.extras.setdefault("graph_diag", {"ran": False, "contributed": 0})
        gate = self.gate or GraphGate()
        if not gate.should_run(query, candidates):
            return candidates

        # Seeds = the highest-scored fused candidates. RRFFusionStage has
        # already set ``score``; take the pool in its current order (fusion
        # leaves it input-order, so sort explicitly to be robust to a
        # future reordering upstream).
        ordered = sorted(candidates, key=lambda c: c.score, reverse=True)
        seed_blob_ids = [c.blob_id for c in ordered[: self.seeds]]
        seed_notes = await self._notes_for_blobs(ctx, seed_blob_ids)
        if not seed_notes:
            return candidates
        ctx.extras["graph_diag"]["ran"] = True

        # Walk each seed's bounded neighbourhood, keeping the best (lowest)
        # rank a note earns across seeds. A note is ranked by path weight
        # DESC within a single walk; across walks we keep its strongest
        # appearance.
        best_rank: dict[uuid.UUID, int] = {}
        for note_id in seed_notes:
            nb = await graph_local.bounded_neighborhood(
                ctx.session,
                org_id=ctx.org_id,
                actor_id=ctx.actor_id,
                seed_note_id=note_id,
                node_budget=self.node_budget,
            )
            # ``nodes`` excludes the seed and is emitted best-first, so the
            # list index IS the within-walk rank (1-based).
            for rank, node in enumerate(nb.nodes, start=1):
                prev = best_rank.get(node.note_id)
                if prev is None or rank < prev:
                    best_rank[node.note_id] = rank
        if not best_rank:
            return candidates

        # Resolve reached notes -> their indexed blobs. One note may carry
        # several part blobs; add them all and let DedupeBySourceStage
        # collapse to one per source (as the base branches already rely on).
        blob_to_rank = await self._blobs_for_notes(ctx, best_rank)
        if not blob_to_rank:
            return candidates

        pool = {c.blob_id: c for c in candidates}
        contributed = 0
        for blob_id, graph_rank in blob_to_rank.items():
            term = self.weight / (self.k + graph_rank)
            existing = pool.get(blob_id)
            if existing is not None:
                # Already found by a live branch: graph proximity reinforces
                # it. Fold the term into the aggregate and record the branch
                # rank for diagnostics; do NOT stamp provenance (it was a
                # live hit first).
                existing.scores_by_stage[self.name] = graph_rank
                existing.score += term
            else:
                candidates.append(
                    Candidate(
                        blob_id=blob_id,
                        score=term,
                        scores_by_stage={self.name: graph_rank},
                        provenance=GRAPH_PROVENANCE,
                    )
                )
                contributed += 1
        ctx.extras["graph_diag"]["contributed"] = contributed
        return candidates

    @staticmethod
    async def _notes_for_blobs(ctx: RetrievalContext, blob_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """Distinct source notes of the seed blobs, org-scoped, order
        preserved (so the strongest seed walks first)."""
        if not blob_ids:
            return []
        rows = (
            await ctx.session.execute(
                select(NotePartIndexPointer.blob_id, NotePartIndexPointer.note_id).where(
                    NotePartIndexPointer.org_id == ctx.org_id,
                    NotePartIndexPointer.blob_id.in_(blob_ids),
                )
            )
        ).all()
        by_blob = {bid: nid for bid, nid in rows}
        seen: set[uuid.UUID] = set()
        out: list[uuid.UUID] = []
        for bid in blob_ids:
            nid = by_blob.get(bid)
            if nid is not None and nid not in seen:
                seen.add(nid)
                out.append(nid)
        return out

    @staticmethod
    async def _blobs_for_notes(
        ctx: RetrievalContext, note_rank: dict[uuid.UUID, int]
    ) -> dict[uuid.UUID, int]:
        """Every indexed blob of the reached notes, carrying its note's
        graph rank. Org-scoped."""
        rows = (
            await ctx.session.execute(
                select(NotePartIndexPointer.blob_id, NotePartIndexPointer.note_id).where(
                    NotePartIndexPointer.org_id == ctx.org_id,
                    NotePartIndexPointer.note_id.in_(list(note_rank)),
                )
            )
        ).all()
        return {bid: note_rank[nid] for bid, nid in rows if nid in note_rank}
