"""Order + limit + grader-min stages.

``OrderingStage`` sorts by score DESC with a tie-break on
``created_at`` ASC and then ``str(blob_id)``: deterministic so a
re-execution under identical conditions returns identical order. The
tie-break needs ``created_at`` populated; the stage loads the missing
fields in a single SELECT (so callers don't see N+1).

``GraderMinStage`` early-exits to an empty list when the top score
falls below a configured floor (used by graders that want "no answer"
over "weak answer").

``LimitStage`` truncates to top-K.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services.retrieval.types import Candidate, RetrievalContext, Stage


@dataclass
class OrderingStage(Stage):
    name: str = "order"

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if not candidates:
            return candidates
        # Ensure created_at is loaded for the tie-break.
        missing = [c.blob_id for c in candidates if c.created_at is None]
        if missing:
            rows = (
                await ctx.session.execute(
                    select(MemoryBlob.id, MemoryBlob.created_at).where(MemoryBlob.id.in_(missing))
                )
            ).all()
            by_id = {bid: ts for bid, ts in rows}
            for c in candidates:
                if c.created_at is None:
                    c.created_at = by_id.get(c.blob_id)
        candidates.sort(
            key=lambda c: (
                -c.score,
                c.created_at or _MIN_TS,
                str(c.blob_id),
            )
        )
        return candidates


@dataclass
class GraderMinStage(Stage):
    """Honest abstain: drop the whole result when the top hit is too weak,
    so the caller gets "not in memory" over a confidently-wrong first hit.

    Two floors, checked with a precedence rule (task f0d24fdb / N3):

    * ``min_rerank_score`` -- the QUALITY floor on the cross-encoder's own
      relevance score, which the reranker seam delivers in [0,1] (see
      ``RerankResult``). This is the honest-abstain signal: it scores
      query-doc relevance directly, where the RRF sum only scores position.
      It is only meaningful when the reranker actually ran (it writes
      ``scores_by_stage["rerank"]``).

      It is compared AS IT ARRIVES. It used to be squashed through a sigmoid
      here, on the assumption that the seam carried a raw logit; the seam
      carries a [0,1] score, so the squash was a second one, and it pressed
      the whole usable band of this floor into [0.5, 0.731] -- a relevant
      document and a certainly-irrelevant one, 0.5017 and 0.0000161 from the
      provider, arrived here 0.6229 and 0.5000. Ordering never noticed,
      because a sigmoid is monotone, and every test of this floor used a
      fixture that invented its own scale. Measured 2026-09-13.

      What the floor is WORTH, swept on LoCoMo the same day over both
      candidate models (230 answerable questions, 71 whose right answer is
      silence): nothing that survives a test. Counting "answered correctly or
      honestly silent" over all 301, the best floor either model reaches is
      +5 questions at McNemar p=0.18, and every other one is a wash or a
      loss. The top score tells "there is an answer here" from "there is not"
      with AUC 0.598-0.639 against 0.5 for a coin. Left in, correct and off by
      default, because the mechanism is right and the SIGNAL is what failed;
      read docs/eval/reranker-quality-2026-09.md before setting it to
      anything.
    * ``min_score`` -- the coarse RRF floor on the FUSED score (WS-B1),
      rank-based and measured near-useless for abstention (note 3276b266 §5),
      kept for continuity.

    PRECEDENCE: when the reranker ran AND a rerank floor is set, the rerank
    floor is the SOLE authority and the RRF floor is skipped -- after
    reranking ``candidates[0]`` is the top by rerank score, not by RRF, so a
    high-quality hit that reranked up from a low RRF rank would be spuriously
    cut by the RRF floor. With no rerank signal (or no rerank floor) the RRF
    floor applies exactly as before, so a caller that leaves ``min_rerank_score``
    None is byte-identical to the historical behaviour."""

    name: str = "grader_min"
    min_score: float | None = None
    min_rerank_score: float | None = None

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if not candidates:
            return candidates
        top = candidates[0]
        reranked = "rerank" in top.scores_by_stage
        # Quality gate takes precedence when the reranker ran (see class doc):
        # grade on the logit of the item the cross-encoder ranked #1.
        if self.min_rerank_score is not None and reranked:
            if top.scores_by_stage["rerank"] < self.min_rerank_score:
                ctx.extras["grader_abstained"] = True
                ctx.extras["grader_abstain_reason"] = "grader_min_rerank_score"
                return []
            return candidates
        if self.min_score is None:
            return candidates
        # Coarse RRF floor on the FUSED score, not the current ``score``: the
        # optional reranker overwrites ``score`` but preserves the fused score
        # under "rrf", so the threshold stays calibrated. Reached only when
        # there is no rerank quality signal to defer to.
        fused = top.scores_by_stage.get("rrf", top.score)
        if fused < self.min_score:
            # Record the abstain so RetrievalMeta can tell a deliberate
            # "no answer above the floor" from a genuinely empty index
            # (the empty result is otherwise byte-identical). WS-B1.
            ctx.extras["grader_abstained"] = True
            ctx.extras["grader_abstain_reason"] = "grader_min_rrf"
            return []
        return candidates


@dataclass
class RelativeFloorStage(Stage):
    """Drop candidates whose fused score falls far below the top hit OF
    THEIR OWN KIND OF MATCH.

    A keyword/name query produces a wide score gap: the lexical hits sit
    near the top while pure-semantic noise (weighted down in fusion)
    trails far behind -- those are cut. A conceptual query produces a
    FLAT score profile (all semantic, similar ranks), so nothing is more
    than ``ratio`` below the top and the cut is a no-op: recall for
    genuinely-semantic queries is preserved. ``ratio`` 0 disables it.

    The correction, 2026-09-13: whether one top or two is decided by the
    SHAPE OF THE QUERY, which is the thing the paragraph above is actually
    about and which the code used to infer from the score gap instead of
    reading.

    The proxy fails on a MIXED query: one document quotes the question's
    words and takes ``lexical_exact``, the real answer matches conceptually
    and has only a semantic stage. Their scores differ because a hit
    accumulates one RRF term per stage that ranked it and ``lexical_exact``
    is weighted 1.0 against 0.2 -- a gap set by WHICH STAGES FIRED, not by
    how good the answer is -- and the real answer lands on the far side of a
    gap it did not earn. On the frozen gold set (nota e5de06b0) the note
    holding the questions quotes each one verbatim, takes lexical rank 1 on
    all twenty, and the floor cut every conceptual candidate behind it:
    recall@5 0/20 measured on 2026-09-12, against a 3/20 baseline.

    So: for a KEYWORD query (few tokens, a name or a term) one top, exactly
    as before -- a semantic-only hit there is the "unrelated essays"
    failure and is cut. For a CONCEPTUAL query the two classes are floored
    against their own tops, because there the semantic-only hits are what
    the question is asking for and no lexical hit's score says anything
    about their quality.

    The threshold is deliberately the same shape as ``RerankGate``'s
    ``min_query_tokens``: both answer "is this a lookup or a question",
    and they should not drift apart. What neither can do is tell a good
    semantic hit from a bad one inside a conceptual query -- that needs an
    ABSOLUTE signal, the cross-encoder logit of task f0d24fdb.

    And once that signal is there, this stage steps aside. When the
    reranker has scored the candidates, ``score`` is no longer an RRF sum
    but the cross-encoder's own number, and ``ratio`` measures nothing in
    that domain: on ``bge-reranker-v2-m3`` a relevant document routinely
    scores 1e-5 against a top of 0.5, so a ratio of 0.4 deletes it.
    Measured on LoCoMo (2026-09-13, 230 paired questions, top_k=16): with
    the floor applied to rerank scores the incumbent served 181 tokens per
    query against 482 with no reranker, and recall@10 fell to 0.713 from
    0.735 -- the reranker was being blamed for a cut this stage made. The
    absolute cut in the rerank domain belongs to ``GraderMinStage``'s
    ``min_rerank_score``, which is calibrated in that domain and off by
    default.

    The condition is the fact, not the configuration: a candidate carrying
    a ``rerank`` component is one the cross-encoder actually scored. With
    the feature enabled the reranker still declines on short queries and on
    thin candidate sets (``RerankGate``), and can fail open -- in all of
    those the scores are still RRF sums and this floor still applies,
    which is what a flag-shaped test would have got wrong.

    Runs after OrderingStage (candidates already score-DESC)."""

    name: str = "relative_floor"
    ratio: float = 0.0
    #: At or below this many tokens the query is treated as a keyword/name
    #: lookup and the floor spans both match kinds.
    keyword_max_tokens: int = 3
    #: Name of the stage whose presence in ``scores_by_stage`` means the
    #: scores are absolute relevance and not an RRF sum. Matches
    #: ``CrossEncoderRerankerStage.name``; it is a field so the two can be
    #: wired together in a test without importing across stage modules.
    rerank_stage: str = "rerank"

    @staticmethod
    def _matched_lexically(candidate: Candidate) -> bool:
        """Whether any lexical stage ranked this candidate. The stage names
        are the branch names ``lexical_exact`` / ``lexical_stem``; anything
        else (semantic, semantic_hosted, humus) is a similarity signal."""
        return any(name.startswith("lexical") for name in candidate.scores_by_stage)

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        if self.ratio <= 0.0 or not candidates:
            return candidates
        if any(self.rerank_stage in c.scores_by_stage for c in candidates):
            return candidates
        conceptual = len(query.split()) > self.keyword_max_tokens
        tops: dict[bool, float] = {True: 0.0, False: 0.0}
        for c in candidates:
            key = self._matched_lexically(c) if conceptual else False
            tops[key] = max(tops[key], c.score)
        if max(tops.values()) <= 0.0:
            return candidates

        def keep(c: Candidate) -> bool:
            top = tops[self._matched_lexically(c) if conceptual else False]
            return top <= 0.0 or c.score >= self.ratio * top

        return [c for c in candidates if keep(c)]


@dataclass
class LimitStage(Stage):
    name: str = "limit"
    k: int = 10

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        return candidates[: self.k]


# Module-level sentinel for sort key when created_at is missing
# (shouldn't happen post-OrderingStage but defensive).
import datetime as _dt  # noqa: E402

_MIN_TS = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
