"""Canonical retrieval stages. Other stages (CrossEncoderReranker,
HyDE, DedupeBySource, Snippet) land here as separate modules when
their owning task drops them in."""

from __future__ import annotations

from mycelium_core.services.retrieval.stages.access import AccessCounterStage
from mycelium_core.services.retrieval.stages.dedupe import DedupeBySourceStage
from mycelium_core.services.retrieval.stages.fusion import RRFFusionStage
from mycelium_core.services.retrieval.stages.humus import (
    HumusCapStage,
    HumusStage,
    proposed_note_blob_exclusion,
)
from mycelium_core.services.retrieval.stages.lexical import LexicalFTSStage
from mycelium_core.services.retrieval.stages.order_limit import (
    GraderMinStage,
    LimitStage,
    OrderingStage,
    RelativeFloorStage,
)
from mycelium_core.services.retrieval.stages.rerank import (
    CrossEncoderRerankerStage,
    RerankGate,
)
from mycelium_core.services.retrieval.stages.semantic import SemanticDenseStage

__all__ = [
    "AccessCounterStage",
    "CrossEncoderRerankerStage",
    "DedupeBySourceStage",
    "GraderMinStage",
    "HumusCapStage",
    "HumusStage",
    "LexicalFTSStage",
    "LimitStage",
    "OrderingStage",
    "RRFFusionStage",
    "RelativeFloorStage",
    "RerankGate",
    "SemanticDenseStage",
    "proposed_note_blob_exclusion",
]
