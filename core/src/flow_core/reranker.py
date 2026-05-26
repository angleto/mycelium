"""Cross-encoder reranker provider abstraction (task `27579d6a`).

The reranker is a second-stage scorer that takes (query, doc) PAIRS
and scores each as a single forward pass through a cross-encoder
model. Quality is materially higher than the bi-encoder embeddings
the dense branch uses (the cross-encoder sees query+doc joined and
can attend across them); cost is O(top-K) per query instead of O(N).

Mirrors the shape of ``flow_core.embedder``: Protocol + neutral DTO
+ injectable factory + cheap availability probe + override seam for
tests. A NoopReranker stands in when the feature is gated off; the
LocalReranker depends on sentence-transformers (already a Flow
optional extra) for ``CrossEncoder``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from flow_core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankResult:
    """Scores aligned to the input ``pairs`` order, plus the
    ``model_id`` actually used (so callers can record/meter)."""

    scores: list[float]
    model_id: str


@runtime_checkable
class Reranker(Protocol):
    async def rerank(
        self, query: str, pairs: Sequence[str]
    ) -> RerankResult:
        """``pairs`` is the document texts; the query is broadcast.
        Returns a score per document, higher = better, in the same
        order as ``pairs``."""
        ...


class NoopReranker:
    """Identity reranker: scores all docs equal so the OrderingStage
    falls back to the upstream RRF order. Used when the feature is
    disabled (the gate keeps the production default off)."""

    model_id = "noop"

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        return RerankResult(scores=[0.0] * len(pairs), model_id=self.model_id)


class LocalReranker:
    """sentence-transformers CrossEncoder loaded once per process.
    Lazily imported so the optional extra is not required for the
    Noop path. The load and the predict both run in a worker thread
    so the asyncio loop stays responsive even on cold start."""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3") -> None:
        self._model_name = model_name
        self._model: object | None = None
        self._load_lock = asyncio.Lock()

    @property
    def model_id(self) -> str:
        return self._model_name

    def _load_sync(self) -> object:
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:  # pragma: no cover - optional extra
                raise RuntimeError(
                    "LocalReranker requires the 'sentence-transformers' extra"
                ) from exc
            self._model = CrossEncoder(self._model_name)
        return self._model

    async def _model_ready(self) -> object:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                return await asyncio.to_thread(self._load_sync)
            return self._model

    async def prewarm(self) -> None:
        """Force the in-memory load (off the request path). Safe to
        call multiple times; subsequent calls are no-ops."""
        await self._model_ready()

    async def rerank(  # pragma: no cover - network/model
        self, query: str, pairs: Sequence[str]
    ) -> RerankResult:
        if not pairs:
            return RerankResult(scores=[], model_id=self._model_name)
        model = await self._model_ready()

        def _run() -> list[float]:
            return [
                float(s) for s in model.predict(  # type: ignore[attr-defined]
                    [(query, p) for p in pairs]
                )
            ]

        scores = await asyncio.to_thread(_run)
        return RerankResult(scores=scores, model_id=self._model_name)


_FactoryFn = Callable[[], Reranker]
_override: _FactoryFn | None = None
_singleton: Reranker | None = None


def set_reranker_override(fn: _FactoryFn | None) -> None:
    """Test seam: replace the model-backed reranker with a deterministic
    in-memory one. Production leaves this None."""
    global _override
    _override = fn


def get_reranker() -> Reranker:
    if _override is not None:
        return _override()
    global _singleton
    if _singleton is None:
        settings = get_settings()
        if not settings.reranker_enabled:
            _singleton = NoopReranker()
        else:
            _singleton = LocalReranker(settings.reranker_model)
    return _singleton


def reranker_available() -> bool:
    """Cheap probe for status reporting: can a usable reranker be
    produced *without* loading the model? An override (CI/tests) is
    always considered available; otherwise the cross-encoder needs the
    optional ``sentence-transformers`` extra. Never raises."""
    if _override is not None:
        return True
    if not get_settings().reranker_enabled:
        return False
    try:
        return importlib.util.find_spec("sentence_transformers") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False
