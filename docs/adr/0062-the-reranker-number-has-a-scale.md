# ADR-0062: The reranker's number has a scale, and the pipeline stops guessing it

Status: Accepted (2026-09-13)
Relates to: ADR-0061 (the embedder seam has a side), ADR-0012 (embedder
abstraction). Task f0d24fdb.

## Context

`Reranker.rerank()` returned "a score per document, higher = better". Order was
the only thing the contract promised, and for a while order was the only thing
anybody used: the stage sorts by it.

Two consumers then started reading the MAGNITUDE, and each one guessed a
different scale.

`GraderMinStage.min_rerank_prob` squashed the score through a sigmoid, assuming
it was a raw logit. sentence-transformers applies the checkpoint's own
activation inside `predict`, and for every reranker this repository has
measured that activation is a Sigmoid, so the squash was a second one. Measured
on `bge-reranker-v2-m3`: a relevant document and a certainly-irrelevant one
leave the provider 0.5017 and 0.0000161 apart and reached the comparison 0.6229
and 0.5000 apart. The whole usable band of the abstain floor was [0.5, 0.731].
Nothing failed, because a sigmoid is monotone and the order was untouched; and
every test of the floor passed, because each one built its own fake reranker
that returned logits, so both halves of a broken contract agreed.

`RelativeFloorStage` dropped candidates below `0.4 * top`, a ratio written for
RRF sums where strong and weak differ by little. In the cross-encoder's domain
they differ by everything, so on LoCoMo the incumbent served 181 tokens per
query against 482 with no reranker and scored recall@10 0.713, BELOW the
no-reranker arm. The reranker was being blamed for a cut that stage made.

And the scale is not even a property of cross-encoders, which is what makes a
convention insufficient: measured the same day, `bge-reranker-v2-m3`,
`bge-reranker-base` and `gte-multilingual-reranker-base` declare Sigmoid and
return [0,1], while `cross-encoder/ms-marco-MiniLM-L6-v2` declares Identity and
returns raw logits (8.18 and -11.42 on the same pair). A provider that trusts
the checkpoint hands out two different scales under one contract, and the day
someone changes `MYCELIUM_RERANKER_MODEL` the floor silently stops meaning what
it meant.

## Decision

**1. The seam carries a relevance score in [0, 1], and the provider is what
makes that true.** `LocalReranker` asks sentence-transformers for raw logits
explicitly and applies one sigmoid itself, so the range holds whatever the
checkpoint declares. `RerankResult` states the range, states that it is ordered
and not calibrated, and states why: the one consumer that reads the magnitude
is a floor, and a floor has to mean the same thing across providers.

**2. `RelativeFloorStage` steps aside when the candidates carry a rerank
score.** An absolute relevance signal is present, and a ratio of the top
measures nothing in that domain. The absolute cut there belongs to
`GraderMinStage.min_rerank_score`. The condition is the presence of the score
and not the feature flag, because with the reranker enabled the gate still
declines short queries and thin candidate sets and the provider can fail open,
and in all of those the scores are still RRF sums the floor must still cut.

**3. Executing a third party's code requires pinning the commit it comes
from.** `trust_remote_code=True` without `code_revision` is refused by the
constructor. Several strong multilingual rerankers define their own
architecture in a SECOND repository their config points at
(`gte-multilingual-reranker-base` -> `Alibaba-NLP/new-impl`), and unpinned that
decision has no object: what runs is whatever that repository serves at load
time. Pinned, the thing being trusted is one sha somebody read. The production
factory passes neither, so a deployment cannot execute remote code at all; only
the benchmark can, and only pinned.

## Consequences

The per-org key is `retrieval_grader_min_rerank_score` and its value is
compared as it arrives. Renaming was free: no surface ever wrote the old key
(the workspace settings endpoint exposes only `retrieval_grader_min_rrf`), so
no stored value can exist, and one that did would have been calibrated against
the broken scale.

Every test double for a reranker now returns a [0,1] score. Three of them
returned logits; the one in `test_eval_offline` went red the moment the
contract was fixed, which is the whole argument for TST-01 in one line.

The measured numbers for both candidates are unchanged, because both
checkpoints already applied the sigmoid the provider now applies itself, and
the sweep of the floor is now a measurement of the model rather than of the
stage.

## What this does not decide

Whether the reranker is turned on in production. That is a latency and memory
question on a 4-core node, and the numbers to decide it can only be taken on
the pod.

Whether the honest-abstain gate gets built on this signal. Swept on both
models after the fix, and no floor reaches significance on a paired count of
"answered correctly or honestly silent" (best point: +5 of 301 at p=0.18). The
mechanism stays, off by default; see `docs/eval/reranker-quality-2026-09.md`.

## Alternatives rejected

**Make the seam carry raw logits and keep the squash in the grader.** Ordering
is identical either way, so the choice is decided by the consumer that reads
the magnitude, and that one is a floor specified in [0,1]. It also points the
wrong way for a provider that is not a local checkpoint: a hosted reranker
returns a normalized relevance score rather than a logit, and a logit-shaped
seam would make it invert one, which is infinite at both ends. (Which hosted
rerankers this repository would actually use is not settled -- Scaleway's
`/v1/rerank`, checked on 2026-09-12, is a bi-encoder and not a candidate at
all.)

**Leave the range to convention and document it.** The measurement above is the
refutation: one of the four checkpoints tried breaks the convention, and it
breaks it silently.

**Recommend pinning instead of requiring it.** The unpinned form is exactly as
easy to write and silently unbounded, and the benchmark that first wanted
remote code runs on the same laptop as everything else.
