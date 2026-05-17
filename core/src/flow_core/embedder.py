"""Embedder abstraction (docs/adr/0012, 0005, FR-8).

Protocol + neutral DTO + injectable factory, the same seam as the LLM
provider and the email connector: the production embedder runs a local
multilingual model; CI injects a deterministic in-memory embedder.
``model_id`` and the produced ``dim`` are recorded per blob so a future
re-embedding to a different model is a new column, not an in-place
change (ADR-0005).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from flow_core.config import get_settings


@dataclass(frozen=True)
class EmbedResult:
    vector: list[float]
    model_id: str
    tokens: int  # billable units for metering (ADR-0019)


@runtime_checkable
class Embedder(Protocol):
    async def embed(self, text: str) -> EmbedResult: ...


class LocalEmbedder:
    """Reference local model (sentence-transformers, CPU/ARM). Lazily
    imported so the heavy dependency is optional and never loaded in
    CI (tests inject a fake). Not exercised by the test suite."""

    def __init__(self, model_name: str = "intfloat/multilingual-e5-small") -> None:
        self._model_name = model_name
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - optional extra
                raise RuntimeError(
                    "LocalEmbedder requires the 'sentence-transformers' extra"
                ) from exc
            self._model = SentenceTransformer(self._model_name)
        return self._model

    async def embed(self, text: str) -> EmbedResult:  # pragma: no cover - network/model
        import asyncio

        model = self._load()

        def _run() -> list[float]:
            return list(model.encode(text, normalize_embeddings=True))  # type: ignore[attr-defined]

        vec = await asyncio.to_thread(_run)
        return EmbedResult(
            vector=[float(x) for x in vec],
            model_id=self._model_name,
            tokens=max(1, len(text.split())),
        )


_FactoryFn = Callable[[], Embedder]
_override: _FactoryFn | None = None


def set_embedder_override(fn: _FactoryFn | None) -> None:
    """Test seam: replace the model-backed embedder with an in-memory
    one. Production leaves this None. Never set in production code."""
    global _override
    _override = fn


def get_embedder() -> Embedder:
    if _override is not None:
        return _override()
    return LocalEmbedder()


def embed_dim() -> int:
    return get_settings().embed_dim
