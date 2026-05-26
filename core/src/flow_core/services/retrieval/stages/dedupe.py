"""Dedupe-by-source stage (task `bbc21aa1`).

When a long document is indexed as N chunks (paragraph-split via
``services.chunker.ParagraphChunker``), all N chunks share the same
``(source_kind, source_id)``. The retrieve branches may surface
multiple chunks of the same parent in the top-K, polluting the user
visible result with near-duplicates of the same document.

This stage hydrates the missing ``(source_kind, source_id)`` for any
candidate that doesn't already carry them, then collapses the list:
keep one candidate per source (max-score), preserve the original
order. The kept candidate carries the winning chunk_index so the
caller can navigate to the right paragraph (the SPA uses this to
scroll to the matching section of the note).

Cost: at most one batched SELECT per call. When chunking isn't in
use (every blob is whole-doc, chunk_index=0 only), every blob has a
unique source_id anyway so the stage is a no-op pass-through.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from flow_core.models.memory_blob import BlobSource
from flow_core.services.retrieval.types import Candidate, RetrievalContext, Stage


@dataclass
class DedupeBySourceStage(Stage):
    name: str = "dedupe_by_source"

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if not candidates:
            return candidates
        # Hydrate source provenance for any candidate that doesn't
        # carry it yet. One batched SELECT regardless of N. Use the
        # natural (blob_id) lookup; one blob may have multiple
        # sources (provenance is N:1) but for the dedupe key we take
        # the first source recorded -- the retrieve has no way to
        # know which provenance the user meant, and the chunking
        # contract guarantees one source per chunk-blob anyway.
        missing = [c.blob_id for c in candidates if c.source_id is None]
        if missing:
            rows = (
                await ctx.session.execute(
                    select(
                        BlobSource.blob_id,
                        BlobSource.source_kind,
                        BlobSource.source_id,
                        BlobSource.chunk_index,
                    ).where(BlobSource.blob_id.in_(missing))
                )
            ).all()
            by_blob: dict[uuid.UUID, tuple[str, str, int]] = {}
            for bid, kind, sid, idx in rows:
                # First wins (rows arrive in insert order which is
                # arbitrary; the dedupe collapse below is stable
                # against this since we keep the highest-score
                # candidate per source).
                by_blob.setdefault(bid, (kind, sid, idx))
            for c in candidates:
                if c.source_id is None and c.blob_id in by_blob:
                    kind, sid, idx = by_blob[c.blob_id]
                    c.source_kind = kind
                    c.source_id = sid
                    c.chunk_index = idx

        # Collapse: scan in current order, keep the FIRST occurrence
        # of each (source_kind, source_id) since the order so far is
        # already by descending score (RRF / rerank put the best
        # candidate first). The skipped chunks of the same source are
        # dropped silently -- their snippet/index already lost the
        # tie to a stronger sibling.
        seen: set[tuple[str | None, str | None]] = set()
        out: list[Candidate] = []
        for c in candidates:
            key = (c.source_kind, c.source_id)
            if key == (None, None):
                # No provenance recorded: keep verbatim (legacy blobs
                # without BlobSource entries -- shouldn't happen on
                # current data but defensive).
                out.append(c)
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
        return out
