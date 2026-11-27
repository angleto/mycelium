"""What a session should do at a prompt, by a decision rule that is only
recorded, never acted on (docs/adr/0081).

At a prompt a session can continue, compact its history, restart clean, or
hand a piece of work to a subagent. The rule compares them on three terms:

- **anchoring**: what the history is worth on work it fits, against what it
  costs on work it does not fit. Only a clean restart changes this term. A
  summary that keeps what the history says about the work keeps the same
  decision on every history, and so the same loss (proposition T4a of the
  memory essay). A summary that drops something loses at least as much on
  the work the history fits (corollary T4c);
- **dilution**: what length alone costs a reader that has limits;
- **cost**: what the remaining calls pay to read the context again, against
  what a compaction pays to build the shorter one.

The two quality terms are in `Parameters` with the measurement each comes
from. On the one model measured, both are indistinguishable from zero on a
synthetic bench, and neither is measured on real sessions. Until it is, the
recommendation is made on cost alone. Every record then says that, so a
summary of the log can never present a cost answer as a quality answer.

Standard library only: this runs on the hook's fast path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Action(StrEnum):
    CONTINUE = "continue"
    COMPACT = "compact"
    RESTART_CLEAN = "restart_clean"
    DELEGATE = "delegate"


class Basis(StrEnum):
    """What a recommendation was decided on."""

    #: The quality terms are measured for this model, and the rule weighed them.
    QUALITY_AND_COST = "quality_and_cost"
    #: The quality terms are not distinguishable from zero for this model
    #: where they were measured. The rule decides on cost.
    COST_QUALITY_NULL = "cost_quality_null"
    #: The quality terms were never measured for this model. The rule decides
    #: on cost, and says the quality side is unknown.
    COST_QUALITY_UNMEASURED = "cost_quality_unmeasured"


@dataclass(frozen=True, slots=True)
class Prices:
    """USD per million tokens. From
    https://platform.claude.com/docs/en/about-claude/pricing, read 2026-11-27."""

    input: float
    output: float
    cache_read: float
    #: Claude Code writes its cache with the one-hour lifetime: every
    #: ``cache_creation`` in the transcripts of 2026-11-27 is
    #: ``ephemeral_1h_input_tokens``. That write is priced at twice the input.
    cache_write: float


@dataclass(frozen=True, slots=True)
class Quality:
    """The anchoring and dilution terms for one model, as measured.

    ``null`` says that no measured cell separated from the ceiling. It does
    not say the effect is zero. ``anchoring_fail_upper`` and
    ``dilution_fail_upper`` are the 95% upper bounds of the failure rate on
    the bench (Beta posterior, uniform prior). The summary reports them so
    nobody reads a null as an absence."""

    null: bool
    anchoring_fail_upper: float
    dilution_fail_upper: float
    source: str


@dataclass(frozen=True, slots=True)
class Parameters:
    """One versioned table of every number the rule reads. Changing a value
    changes ``version``, and every record carries the version it was made
    with, so a log mixing two tables can be split."""

    version: str
    #: Median context of a session's first call: system prompt, tools,
    #: instructions. 50.7k over 199 sessions (memoria_v7 DESIGN section 1).
    fresh_context: int
    #: Median context of the first call after a compaction: 68,420 over 11
    #: compactions in this machine's transcripts, 2026-11-27.
    compacted_context: int
    #: What a compaction writes as its summary: the compacted context less
    #: the fresh one. An estimate: the compaction call's own output is not in
    #: the transcript.
    summary_output: int
    #: Median of the model calls a session still makes, which stays between
    #: 99 and 144 whether counted from the 50th, 100th or 200th call or from
    #: the first call at 100k, 200k or 300k of context (60 sessions on this
    #: machine, 9,763 calls, 2026-11-27). So it is used as a constant. Past 400
    #: calls only 5 sessions remain, and the median there (262) is not used.
    remaining_calls: int
    prices: dict[str, Prices]
    quality: dict[str, Quality]


PARAMETERS = Parameters(
    version="2026-11-27.1",
    fresh_context=50_700,
    compacted_context=68_420,
    summary_output=17_720,
    remaining_calls=124,
    prices={
        "claude-opus-5-5": Prices(input=4.0, output=20.0, cache_read=0.20, cache_write=8.0),
        "claude-sonnet-5-5": Prices(input=2.0, output=10.0, cache_read=0.10, cache_write=4.0),
        "claude-fable-5-2": Prices(input=8.0, output=40.0, cache_read=0.20, cache_write=16.0),
    },
    quality={
        # memoria_v7 hardened pilot (thinktank, experiments/memoria_v7/pilot/SHAKEDOWN.md):
        # Df `cont` 35/36 (anchoring), `far` at about 375k 3/3 per arm (dilution).
        "claude-opus-5-5": Quality(
            null=True,
            anchoring_fail_upper=0.122,
            dilution_fail_upper=0.35,
            source="memoria_v7 hardened pilot, synthetic bench, 2026-11-27",
        ),
    },
)


@dataclass(frozen=True, slots=True)
class Observation:
    """What the hook can see at a prompt. Numbers only: no prompt text, no
    file content."""

    model: str | None
    #: Input tokens of the last model call: the context the next call reads.
    context: int
    #: A tool call or a turn failed since the previous prompt.
    failed_since_last_prompt: bool


@dataclass(frozen=True, slots=True)
class Recommendation:
    action: Action
    basis: Basis
    #: Cost of continuing less cost of the recommended alternative, in USD,
    #: over the remaining calls. Positive means the alternative is cheaper.
    margin_usd: float
    #: The alternative the rule weighed against continuing, and its margin,
    #: kept even when the recommendation is to continue, so the summary can
    #: show how close the call was.
    compact_margin_usd: float
    #: memoria_v4: after a failure, continue the same session with
    #: the news and the doubt. Measured on Haiku and Sonnet 5.5 only. Recorded
    #: so the summary can tell those prompts apart, never used to decide.
    after_failure: bool
    parameters: str


def _compact_margin(obs: Observation, p: Parameters, price: Prices) -> float:
    """USD saved over the remaining calls by compacting now rather than
    continuing.

    Both paths add the same new tokens from here on, so only the base the
    calls read differs, and the increments cancel. Continuing reads
    ``context`` at the cache price on every remaining call. Compacting pays
    for one call that reads the context and writes the summary, then one
    cache write of the compacted context, then reads that at the cache price.
    """
    per_m = 1_000_000
    keep = p.remaining_calls * obs.context * price.cache_read / per_m
    compaction = (
        obs.context * price.cache_read / per_m
        + p.summary_output * price.output / per_m
        + p.compacted_context * price.cache_write / per_m
    )
    after = p.remaining_calls * p.compacted_context * price.cache_read / per_m
    return keep - (compaction + after)


def recommend(obs: Observation, p: Parameters = PARAMETERS) -> Recommendation | None:
    """The rule's recommendation, or None for a model with no price in the
    table: a recommendation in a currency the rule cannot price would be a
    number nobody could check.

    A clean restart is never recommended on cost. Its cost is close to a
    compaction's, and it drops what the history is worth. The theory gives a
    restart only one advantage over a compaction, the anchoring term, and
    that term is null or unmeasured for every model in the table. Delegation
    is never recommended: whether the work splits into independent pieces is
    not something the hook can see. The hook records it when it happens."""
    if obs.model is None or obs.model not in p.prices:
        return None
    price = p.prices[obs.model]
    quality = p.quality.get(obs.model)
    if quality is None:
        basis = Basis.COST_QUALITY_UNMEASURED
    elif quality.null:
        basis = Basis.COST_QUALITY_NULL
    else:
        # A model whose quality terms separated would need them in the
        # comparison, and the table has no such model. Refuse rather than
        # quietly decide on cost.
        raise ValueError(
            f"quality terms for {obs.model} are measured and not null; "
            "the rule has no weighing for them yet"
        )
    margin = _compact_margin(obs, p, price)
    action = Action.COMPACT if margin > 0 else Action.CONTINUE
    return Recommendation(
        action=action,
        basis=basis,
        margin_usd=round(margin if action is Action.COMPACT else 0.0, 4),
        compact_margin_usd=round(margin, 4),
        after_failure=obs.failed_since_last_prompt,
        parameters=p.version,
    )
