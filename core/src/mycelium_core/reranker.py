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
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mycelium_core.config import get_settings

logger = logging.getLogger(__name__)

# Weight of the newest batch in the running cost estimate (see
# ``LocalReranker._observe``). 0.3 lets a real change of regime dominate after
# about three batches while one slow batch moves the estimate by under a third.
_COST_SMOOTHING = 0.3


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
    def estimate_seconds(self, query: str, pairs: Sequence[str]) -> float | None:
        """How long scoring ``pairs`` would take if submitted NOW, including
        any work this provider has already queued, or ``None`` when the
        provider cannot score at all (feature off, model not loaded, not yet
        measured). The stage spends a time budget with it instead of a fixed
        count of pairs, because the cost of one pair is not a constant: on the
        production node (2026-09-14, task 03cdf674) the same model took 1.67 s
        a pair in a batch of 10 and 2.42 s a pair in a batch of 50, and the
        laptop was 2.2x faster than either. A count that was right for one of
        those is wrong for the others.

        ``None`` is a real answer and the stage reports it as such; returning
        a guess instead would let a request load a model inline."""
        ...

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        """``pairs`` is the document texts; the query is broadcast.
        Returns one score per document in the same order as ``pairs``,
        higher = better and IN [0, 1] (see ``RerankResult``). A provider
        whose model emits raw logits converts them; it does not hand the
        logit out and hope the consumer knows. Raises
        ``RerankerUnavailable`` when it cannot score, never loads inline."""
        ...


class RerankerUnavailable(RuntimeError):
    """The provider cannot score right now: the feature is off, the optional
    extra is missing, or the model has not been loaded off the request path.
    Distinct from a scoring failure, which is a provider that should have
    worked and did not; a caller is told which of the two happened."""


def cost_units(query: str, pairs: Sequence[str]) -> int:
    """The work a cross-encoder batch costs, in the unit the estimate is kept
    in: characters, padded to the longest pair of the batch, because the batch
    is padded to its longest sequence before the forward pass. Characters
    rather than tokens so that estimating never needs the tokenizer, which is
    loaded with the model. The proportionality is an approximation and the
    estimate absorbs its error, since it is re-measured on every batch."""
    if not pairs:
        return 0
    return len(pairs) * (len(query) + max(len(p) for p in pairs))


class NoopReranker:
    """Stands in when the feature is gated off. It cannot score: its estimate
    is ``None`` and ``rerank`` raises, so the stage reports the request as
    unavailable instead of passing the RRF order through as if it had been
    reranked, which is what it did until 2026-10-22."""

    model_id = "noop"

    def estimate_seconds(self, query: str, pairs: Sequence[str]) -> float | None:
        return None

    async def rerank(self, query: str, pairs: Sequence[str]) -> RerankResult:
        raise RerankerUnavailable("the reranker is not enabled in this deployment")


class LocalReranker:
    """sentence-transformers CrossEncoder, loaded once per process by
    ``prewarm`` and never by a request. Lazily imported so the optional extra
    is not required for the Noop path. The load and every forward pass run on
    a single thread of its own, so the asyncio loop stays responsive and the
    model is never run concurrently with itself."""

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
        # One thread of its own, not the loop's shared default pool: a forward
        # pass holds a core for seconds on the production node, and in the
        # shared pool it would queue behind (and in front of) every other
        # blocking call the process makes. One worker also serialises the
        # model, which is what the cost estimate below assumes: queued work
        # is waited for, not run alongside.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reranker")
        # Measured seconds per cost unit (see ``cost_units``), None until the
        # first measurement. Seeded by ``prewarm``, then moved by every batch.
        self._seconds_per_unit: float | None = None
        # Units submitted to the executor and not yet finished, so that an
        # estimate made while another request is scoring includes the wait.
        self._pending_units = 0

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

    async def prewarm(self) -> None:
        """Load the model and take the first cost measurement, OFF the request
        path. This is the only way the model gets loaded: a request never
        loads it, because on the production node the load took 114.6 s and
        added ~2.7 GiB to a backend capped at 6 GiB (task 03cdf674), which is
        a cost no single search may impose on the process. Until this has
        finished the provider reports itself unavailable. Safe to call more
        than once."""
        async with self._load_lock:
            if self._model is None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._executor, self._load_sync)
        if self._seconds_per_unit is None:
            # One measured batch seeds the estimate, so that the first real
            # request is admitted or refused on a number rather than a guess.
            # Two pairs of roughly a note's length; every later batch moves
            # the estimate towards what the real traffic costs.
            sample = "the memory system and its abstention threshold " * 25
            query = "which threshold does the reranker use"
            await self.rerank(query, [sample, sample[: len(sample) // 2]])

    def estimate_seconds(self, query: str, pairs: Sequence[str]) -> float | None:
        if self._model is None or self._seconds_per_unit is None:
            return None
        return self._seconds_per_unit * (self._pending_units + cost_units(query, pairs))

    async def rerank(  # pragma: no cover - network/model
        self, query: str, pairs: Sequence[str]
    ) -> RerankResult:
        if not pairs:
            return RerankResult(scores=[], model_id=self._model_name)
        model = self._model
        if model is None:
            raise RerankerUnavailable(f"{self._model_name} is not loaded; it loads at startup only")

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

        units = cost_units(query, pairs)
        self._pending_units += units
        try:
            loop = asyncio.get_running_loop()

            def _timed() -> tuple[list[float], float]:
                # Timed inside the worker, so the measurement is the forward
                # pass and not the time this batch spent queued behind another.
                started = time.monotonic()
                out = _run()
                return out, time.monotonic() - started

            scores, elapsed = await loop.run_in_executor(self._executor, _timed)
        finally:
            self._pending_units -= units
        self._observe(units, elapsed)
        return RerankResult(scores=scores, model_id=self._model_name)

    def _observe(self, units: int, elapsed: float) -> None:
        """Move the estimate towards the batch just measured. An exponential
        average, so the estimate follows a change of hardware, model or
        document length within a few batches without being thrown by one slow
        outlier; the weight of the newest batch is a smoothing choice, not a
        property of any data."""
        if units <= 0:
            return
        observed = elapsed / units
        if self._seconds_per_unit is None:
            self._seconds_per_unit = observed
        else:
            self._seconds_per_unit += _COST_SMOOTHING * (observed - self._seconds_per_unit)


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
