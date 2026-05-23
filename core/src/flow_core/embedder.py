"""Embedder abstraction (docs/adr/0012, 0005, FR-8).

Protocol + neutral DTO + injectable factory, the same seam as the LLM
provider and the email connector: the production embedder runs a local
multilingual model; CI injects a deterministic in-memory embedder.
``model_id`` and the produced ``dim`` are recorded per blob so a future
re-embedding to a different model is a new column, not an in-place
change (ADR-0005).

Process-singleton: ``get_embedder()`` returns the SAME ``LocalEmbedder``
instance for the life of the process (when no override is set). The
underlying SentenceTransformer is multi-hundred-MB in resident memory;
returning a fresh instance per call (the pre-fix shape) made every
caller pay the in-memory load again and inflated the working set toward
the pod memory limit. The override seam used by tests bypasses this
cache, so CI behavior is unchanged.

Async-safe load: both ``embed`` and ``embed_batch`` move the
SentenceTransformer construction *and* the encode into a worker thread,
so a cold first call never blocks the asyncio event loop (which would
otherwise stall every concurrent request, including liveness probes
that share the loop). ``prewarm()`` is exposed so a server can warm the
model at startup off the request path.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

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
    CI (tests inject a fake). The model is loaded once per instance and
    the instance itself is cached at module scope by ``get_embedder``;
    both the load and the encode are dispatched to a worker thread."""

    def __init__(self, model_name: str = "intfloat/multilingual-e5-small") -> None:
        self._model_name = model_name
        self._model: object | None = None
        self._load_lock = asyncio.Lock()

    def _load_sync(self) -> object:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - optional extra
                raise RuntimeError(
                    "LocalEmbedder requires the 'sentence-transformers' extra"
                ) from exc
            self._model = SentenceTransformer(self._model_name)
        return self._model

    async def _model_ready(self) -> object:
        # Fast path: already loaded, no lock contention.
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                return await asyncio.to_thread(self._load_sync)
            return self._model

    async def prewarm(self) -> None:
        """Force the in-memory load now (off the request path). Safe to
        call multiple times; subsequent calls are no-ops."""
        await self._model_ready()

    async def embed(self, text: str) -> EmbedResult:  # pragma: no cover - network/model
        model = await self._model_ready()

        def _run() -> list[float]:
            return list(model.encode(text, normalize_embeddings=True))  # type: ignore[attr-defined]

        vec = await asyncio.to_thread(_run)
        return EmbedResult(
            vector=[float(x) for x in vec],
            model_id=self._model_name,
            tokens=max(1, len(text.split())),
        )

    async def embed_batch(self, texts: list[str]) -> list[EmbedResult]:  # pragma: no cover - model
        """Encode many texts in a single forward pass. SentenceTransformer
        batches internally, so this is ~order-of-magnitude faster than
        N calls to ``embed`` (the per-call Python overhead and tokenizer
        warmup dominate at small N)."""
        if not texts:
            return []
        model = await self._model_ready()

        def _run() -> list[list[float]]:
            arr = model.encode(texts, normalize_embeddings=True, batch_size=32)  # type: ignore[attr-defined]
            return [list(row) for row in arr]

        vecs = await asyncio.to_thread(_run)
        return [
            EmbedResult(
                vector=[float(x) for x in v],
                model_id=self._model_name,
                tokens=max(1, len(t.split())),
            )
            for v, t in zip(vecs, texts, strict=True)
        ]


_FactoryFn = Callable[[], Embedder]
_override: _FactoryFn | None = None
_singleton: LocalEmbedder | None = None


def set_embedder_override(fn: _FactoryFn | None) -> None:
    """Test seam: replace the model-backed embedder with an in-memory
    one. Production leaves this None. Never set in production code."""
    global _override
    _override = fn


def get_embedder() -> Embedder:
    if _override is not None:
        return _override()
    global _singleton
    if _singleton is None:
        _singleton = LocalEmbedder()
    return _singleton


def embedder_available() -> bool:
    """Cheap probe for status reporting: can a usable embedder be
    produced *without* loading the model? An injected override (CI, or
    a future hosted provider) is always considered available; otherwise
    the local model needs the optional ``sentence-transformers`` extra,
    so we only check that it is importable (no model download/load).
    Never raises."""
    if _override is not None:
        return True
    try:
        return importlib.util.find_spec("sentence_transformers") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


async def embed_batch(emb: Embedder, texts: list[str]) -> list[EmbedResult]:
    """Use the embedder's batched API when available, fall back to a
    sequential loop otherwise. Lets callers (e.g. gateway index build)
    benefit from real batching without forcing every Embedder to
    implement it on the Protocol."""
    method = getattr(emb, "embed_batch", None)
    if method is not None:
        coro = cast(Callable[[list[str]], Awaitable[list[EmbedResult]]], method)
        return await coro(texts)
    return [await emb.embed(t) for t in texts]


def embed_dim() -> int:
    return get_settings().embed_dim
