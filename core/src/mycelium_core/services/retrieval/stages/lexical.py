"""Lexical FTS stage: dual-dictionary tsvector on ``memory_blobs.fts``
(``simple``, exact terms) and ``fts_lang`` (stemmed in the row's OWN
language, migrations 0007 + 0066).

Emitted as TWO separate signals so fusion can weight them apart:

- ``lexical_exact``: blobs that match the query terms VERBATIM (the
  ``simple`` dictionary does no stemming). The strongest signal.
- ``lexical_stem``: blobs that match only after stemming with the row's
  ``fts_language`` dictionary (task b1baaf52 -- an English row stems with
  ``english``, a French row with ``french``, not everything with Italian).
  Useful for morphology ('gatti' -> 'gatto') but it also conflates a short
  proper noun with a common word -- e.g. 'marzia' and 'marzo' share the
  Italian stem, so an essay dated 'marzo 2025' spuriously matches a search
  for the name Marzia. Weighted DOWN in fusion (near the semantic tier) so
  a stem-only hit never masquerades as an exact hit; the relative floor
  then drops it when a genuine exact match set a high top score.

Each signal's per-blob value is its 1-based rank in that dictionary's
ts_rank-sorted oversample list."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text

from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services.retrieval.types import (
    Candidate,
    RetrievalContext,
    Stage,
    merge_candidates,
)


@dataclass
class LexicalFTSStage(Stage):
    name: str = "lexical"
    oversample: int = 50
    # Emit ONLY the verbatim signal. Set for an entity-code lookup, where
    # stemming has nothing to contribute and can only introduce a
    # near-match: an id is either present in the text or it is not.
    exact_only: bool = False

    async def _ranked(
        self, ctx: RetrievalContext, query: str, *, match: str, rank: str, key: str
    ) -> list[Candidate]:
        stmt = (
            select(MemoryBlob.id)
            .where(
                MemoryBlob.org_id == ctx.org_id,
                ctx.project_pred,
                text(match),
                *ctx.tag_clauses,
            )
            # id as a final tiebreak so tied ts_rank rows get a STABLE rank
            # across calls -- otherwise the RRF rank (and thus the fused
            # score) shuffles between identical queries, making the order
            # non-deterministic and offset pagination unreliable.
            .order_by(text(f"{rank} DESC"), MemoryBlob.id)
            .limit(self.oversample)
        ).params(q=query)
        rows = (await ctx.session.execute(stmt)).scalars().all()
        return [
            Candidate(blob_id=bid, scores_by_stage={key: float(i + 1)})
            for i, bid in enumerate(rows)
        ]

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        exact = await self._ranked(
            ctx,
            query,
            match="fts @@ plainto_tsquery('simple', :q)",
            rank="ts_rank(fts, plainto_tsquery('simple', :q))",
            key="lexical_exact",
        )
        if self.exact_only:
            return merge_candidates(candidates, exact)
        # Stem in the ROW's own language (task b1baaf52): fts_lang is now a
        # per-row tsvector (migration 0066), so the matching tsquery must use
        # the same config. fts_language is a closed domain (a valid config or
        # 'simple'), so the cast is always well-defined; 'simple'-tagged rows
        # simply don't stem.
        stem = await self._ranked(
            ctx,
            query,
            match="fts_lang @@ plainto_tsquery(fts_language::regconfig, :q)",
            rank="ts_rank(fts_lang, plainto_tsquery(fts_language::regconfig, :q))",
            key="lexical_stem",
        )
        return merge_candidates(candidates, exact + stem)
