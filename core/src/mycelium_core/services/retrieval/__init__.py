"""Retrieval pipeline: composable stages over ``memory_blobs``.

Entry points re-exported here:

- ``RetrievalPipeline``: the executor.
- ``RetrievalContext`` / ``Candidate`` / ``Stage``: the data types.
- ``stages.*``: the canonical stages (lexical, semantic, RRF, ordering,
  limit, grader-min, access-counter).

``memory.retrieve`` is a thin wrapper that builds the default pipeline
and runs it; callers wanting a different recipe (e.g. add a reranker,
swap RRF for weighted-sum) compose their own pipeline directly.
"""

from __future__ import annotations

from mycelium_core.services.retrieval.pipeline import RetrievalPipeline
from mycelium_core.services.retrieval.types import (
    Candidate,
    RetrievalContext,
    Stage,
    merge_candidates,
)

__all__ = [
    "Candidate",
    "RetrievalContext",
    "RetrievalPipeline",
    "Stage",
    "merge_candidates",
]
