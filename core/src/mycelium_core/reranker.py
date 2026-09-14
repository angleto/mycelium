"""Cross-encoder reranker provider abstraction (task `27579d6a`).

The reranker is a second-stage scorer that takes (query, doc) PAIRS
and scores each as a single forward pass through a cross-encoder
model. Quality is materially higher than the bi-encoder embeddings
the dense branch uses (the cross-encoder sees query+doc joined and
can attend across them); cost is O(top-K) per query instead of O(N).

Mirrors the shape of ``mycelium_core.embedder``: Protocol + neutral DTO
+ injectable factory + cheap availability probe + override seam for
tests. A NoopReranker stands in when the feature is gated off; the
LocalReranker depends on sentence-transformers (already a Mycelium
optional extra) for ``CrossEncoder``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mycelium_core.config import get_settings

logger = logging.getLogger(__name__)


def _sigmoid(x: float) -> float:
    """Numerically stable logistic sigmoid (overflow-safe both directions).

    Lives here, in the provider, because this is where a model's raw logit
    becomes the [0,1] score the seam promises. It used to live in the grader,
    which is what made it a SECOND squash on the checkpoints that had already
    applied one."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class RerankResult:
    """Scores aligned to the input ``pairs`` order, plus the
    ``model_id`` actually used (so callers can record/meter).

    ``scores`` are RELEVANCE SCORES IN [0, 1], monotone in relevance. The
    range is part of the contract and not an accident of a provider: a floor
    that abstains on a weak top hit (``GraderMinStage.min_rerank_score``) is
    the one consumer that reads the MAGNITUDE rather than the order, and it
    has to mean the same thing whichever provider produced it.

    Leaving the range undeclared cost a measurement twice over. The grader
    assumed a raw logit and squashed what it got: with `bge-reranker-v2-m3`,
    whose checkpoint declares a Sigmoid and therefore already returns
    probabilities, a relevant document and a certainly-irrelevant one left the
    provider 0.5017 and 0.0000161 apart and reached the comparison 0.6229 and
    0.5000 apart, pressing the whole usable band of the abstain floor into
    [0.5, 0.731]. Ordering never noticed, because a sigmoid is monotone.

    And the range is NOT a property of cross-encoders, which is the reason
    this is enforced here rather than documented as a habit: measured on
    2026-09-13, `bge-reranker-v2-m3` and `bge-reranker-base` and
    `gte-multilingual-reranker-base` declare Sigmoid and return [0,1], while
    `cross-encoder/ms-marco-MiniLM-L6-v2` declares Identity and returns raw
    logits (8.18 and -11.42 on the same pair). A provider that trusted the
    checkpoint would hand out two different scales under one contract, and
    the day someone swapped the model the floor would silently stop meaning
    anything.

    It is NOT a calibrated probability. A cross-encoder's squashed logit is
    ordered, not calibrated, so the number answers "more relevant than that
    one" and not "70% likely to be right": a floor on it is set by sweeping,
    never by picking a confidence.
    """

    scores: list[float]
    model_id: str


@runtime_checkable
class Reranker(Protocol):
    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        """``pairs`` is the document texts; the query is broadcast.
        Returns one score per document in the same order as ``pairs``,
        higher = better and IN [0, 1] (see ``RerankResult``). A provider
        whose model emits raw logits converts them; it does not hand the
        logit out and hope the consumer knows."""
        ...


class NoopReranker:
    """Identity reranker used when the feature is gated off. It scores all
    docs 0.0, but the ``CrossEncoderRerankerStage`` detects it and passes
    candidates through UNCHANGED -- applying a flat 0.0 would let the
    downstream ``OrderingStage`` re-sort on the created_at tiebreak and
    destroy the upstream RRF order. The production default keeps it off."""

    model_id = "noop"

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        return RerankResult(scores=[0.0] * len(pairs), model_id=self.model_id)


class LocalReranker:
    """sentence-transformers CrossEncoder loaded once per process.
    Lazily imported so the optional extra is not required for the
    Noop path. The load and the predict both run in a worker thread
    so the asyncio loop stays responsive even on cold start."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        *,
        trust_remote_code: bool = False,
        revision: str | None = None,
        code_revision: str | None = None,
    ) -> None:
        """``trust_remote_code`` lets sentence-transformers EXECUTE python
        that ships with the checkpoint, which several strong multilingual
        rerankers need (``gte-multilingual-reranker-base`` defines its own
        architecture that way, in a SECOND repository its config points at).
        It turns a model id into arbitrary code from a third party, which is
        a supply-chain decision and not a model choice.

        So it does not travel alone: with ``trust_remote_code`` on,
        ``code_revision`` is REQUIRED, and it pins the commit of the repo the
        code comes from. Without it the decision has no object -- what runs is
        whatever that repository serves at load time, which may be audited
        today and something else next month. With it, the thing being trusted
        is one sha somebody read (for gte on 2026-09-13:
        ``40ced75c3017eb27626c9d4ea981bde21a2662f4`` of
        ``Alibaba-NLP/new-impl``, 1563 lines of pure model definition, no
        filesystem, no network, no subprocess). Required rather than
        recommended because the unpinned form is exactly as easy to write and
        silently unbounded, and the benchmark that first wanted it runs on the
        same laptop as everything else.

        ``revision`` pins the CHECKPOINT repo, which is a different repository
        from the code one and moves independently; it is optional because
        weights that change under a fixed id are a reproducibility problem
        rather than an execution one.

        TWO THINGS THE 2026-09-14 RUN ON THE PRODUCTION NODE FOUND, and both
        limit what the paragraphs above can promise (task 03cdf674):

        1. The pin does not cover everything. With ``code_revision`` set,
           ``modeling.py`` did come from the pinned sha, but transformers
           still logged "A new version of the following files was downloaded"
           for ``configuration.py`` of the same ``new-impl`` repository. So
           one file of the trusted code came from a moving reference anyway.
           What would close it is pinning through the transformers cache
           rather than through this constructor; until then, "pinned" here
           means the model definition and not the whole of what executes.
        2. ``gte-multilingual-reranker-base`` LOADS on that node (arm64,
           torch CPU) and then fails every forward pass with an out-of-range
           index into its rope table -- a different, absurd index each run,
           i.e. uninitialised memory read as an index, inside that remote
           code. The same id and sha run on the laptop. Executing a third
           party's python is therefore not only a supply-chain decision, it
           is a portability one, and neither is visible from where the model
           is chosen."""
        if trust_remote_code and not code_revision:
            raise ValueError(
                "LocalReranker: trust_remote_code needs code_revision -- "
                "executing a third party's python from a moving reference is "
                "not a decision anybody can make"
            )
        self._model_name = model_name
        self._trust_remote_code = trust_remote_code
        self._revision = revision
        self._code_revision = code_revision
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
            # code_revision goes to BOTH: the config resolves the remote
            # architecture from its auto_map, and the model class is fetched
            # again when AutoModelForSequenceClassification instantiates it.
            # Pinning only one leaves the other free to move.
            pin = {"code_revision": self._code_revision} if self._code_revision else {}
            self._model = CrossEncoder(
                self._model_name,
                trust_remote_code=self._trust_remote_code,
                revision=self._revision,
                config_kwargs=pin or None,
                model_kwargs=pin or None,
            )
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
            # Raw logits asked for explicitly, squashed HERE. The checkpoint's
            # own activation is not a contract: some declare Sigmoid and some
            # Identity (see RerankResult), so trusting it makes the seam's
            # range depend on which model id a deployment happens to carry.
            # One conversion, in the provider, is what lets the abstain floor
            # be a number and not a number-per-model.
            import torch  # local: the model is already loaded by this point

            raw = model.predict(  # type: ignore[attr-defined]
                [(query, p) for p in pairs],
                activation_fn=torch.nn.Identity(),
            )
            return [_sigmoid(float(s)) for s in raw]

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
