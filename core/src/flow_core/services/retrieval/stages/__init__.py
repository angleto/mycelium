"""Canonical retrieval stages. Other stages (CrossEncoderReranker,
HyDE, DedupeBySource, Snippet) land here as separate modules when
their owning task drops them in."""

from __future__ import annotations

from flow_core.services.retrieval.stages.access import AccessCounterStage
from flow_core.services.retrieval.stages.fusion import RRFFusionStage
from flow_core.services.retrieval.stages.lexical import LexicalFTSStage
from flow_core.services.retrieval.stages.order_limit import (
    GraderMinStage,
    LimitStage,
    OrderingStage,
)
from flow_core.services.retrieval.stages.rerank import (
    CrossEncoderRerankerStage,
    RerankGate,
)
from flow_core.services.retrieval.stages.semantic import SemanticDenseStage

__all__ = [
    "AccessCounterStage",
    "CrossEncoderRerankerStage",
    "GraderMinStage",
    "LexicalFTSStage",
    "LimitStage",
    "OrderingStage",
    "RRFFusionStage",
    "RerankGate",
    "SemanticDenseStage",
]
