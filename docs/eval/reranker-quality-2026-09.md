# Reranker quality: what a cross-encoder buys, and which one (2026-09-13)

Three systems through the production pipeline on LoCoMo (2 conversations,
k=10, 230 paired scored questions plus 71 abstention questions): no reranker,
`Alibaba-NLP/gte-multilingual-reranker-base` (the candidate, 306M) and
`BAAI/bge-reranker-v2-m3` (the incumbent, 568M). Task f0d24fdb.

The latency measurement that produced the candidate: on the pod's CPU the
incumbent takes 7.8s at 10 pairs and 44s at 50, gte 1.4s and 8.8s. That made
gte cheap. It did not make it good, and this is the measurement that was
missing.

## How it ran

Same dataset, same ingest, same embedder (bge-m3), a fresh throwaway database
per arm. The reranker arms run with `MYCELIUM_RERANKER_ENABLED=true` and
`MYCELIUM_RERANKER_TOP_K=16`, the candidate injected through the reranker
override seam by `scripts/eval_public_bench.py --reranker-model`, which loads
it with `trust_remote_code` so the production factory never has to.

`top_k=16` and not 50 on purpose: it is the setting that could ship in-request
for gte (~2.2s per query on this CPU). A quality number measured at a cost
nobody would pay is not a quality number anyone can use.

Every arm dumps its per-question ranks (`--dump-results`), so the arms are
compared as the paired design they are: exact McNemar on the discordant hits,
cluster-bootstrap CI on the MRR delta with the conversation as the cluster,
since questions inside one conversation share a haystack.

## What it says

```
system                    n   recall  Δrecall  McNemar p     MRR    ΔMRR            Δ95%CI
none                    230    0.735   +0.000     1.0000   0.496  +0.000 [+0.000,+0.000]
gte                     230    0.791   +0.057     0.0044   0.633  +0.137 [+0.102,+0.156]
m3 (incumbent)          230    0.817   +0.083     0.0000   0.651  +0.155 [+0.133,+0.167]
```

Against gte as the baseline instead, the incumbent is ahead by +0.026 recall
(p=0.0703, not significant at 0.05) and +0.018 MRR ([+0.012,+0.031], which does
exclude zero). So: **a cross-encoder is worth it, and the bigger one is
slightly better.** gte does not beat the incumbent on quality; it costs a fifth
of it.

Per category, recall / MRR:

| category | n | none | gte | m3 |
|---|---|---|---|---|
| single-hop | 114 | 0.693 / 0.458 | 0.746 / 0.589 | 0.781 / 0.612 |
| multi-hop | 42 | 0.595 / 0.315 | 0.667 / 0.455 | 0.690 / 0.433 |
| temporal | 63 | 0.937 / 0.710 | 0.984 / 0.878 | 0.984 / 0.924 |
| open-domain | 11 | 0.545 / 0.356 | 0.636 / 0.360 | 0.727 / 0.327 |
| adversarial (abstain) | 71 | 0.000 | 0.000 | 0.000 |

The gain over no reranker is not resampling noise: both metrics move, they move
in every category, McNemar is far under 0.05 and the MRR intervals are far from
zero. Its shape is the one a cross-encoder is expected to have (+0.137 MRR
against +0.057 recall for gte): most of what it buys is ORDER, which is what
the embedder round could not buy at any model size. Recall moves too because
reranking the top 16 can promote a document RRF had left between rank 11 and
16.

Run-to-run: the no-reranker arm gave 0.735/0.495 and 0.735/0.496 on two runs,
gte 0.791/0.633 on two. The noise floor on MRR is around 0.001, two orders
below the deltas.

Tokens served per query: 482 with no reranker, 488 with gte, 529 with the
incumbent. A reranker reorders the ten hits, it does not shorten them. The
token saving July attributed to the reranker came from an abstain floor on the
reranker logit, which is not enabled in any of these arms.

## The defect this measurement found, and why the first table was wrong

The incumbent's first arm served **181 tokens per query against 482 with no
reranker**, and scored recall 0.713, BELOW the no-reranker arm. That number was
written down here before it was explained, and it was wrong about the model: a
reranker that reorders ten hits cannot shorten them, so something was deleting
hits.

`RelativeFloorStage` runs after the reranker and drops candidates below
`0.4 * top`. That ratio was designed for RRF sums, where the spread between a
strong and a weak candidate is narrow. The reranker overwrites `score` with its
own number, and there the spread is enormous: `bge-reranker-v2-m3` gives a
relevant document 1e-5 against a top of 0.5, so a 0.4 ratio deletes it.

The fix is in `RelativeFloorStage`: when any candidate carries a `rerank`
component the stage steps aside, because an absolute relevance signal is
present and a ratio of the top measures nothing in that domain. The absolute
cut there is `GraderMinStage`'s `min_rerank_prob`, calibrated in that domain
and off by default. The condition is the presence of the score, not the feature
flag: with the reranker enabled the gate still declines short queries and thin
candidate sets, and the provider can fail open, and in all of those the scores
are still RRF sums the floor must still cut.

What the fix is worth, measured: the incumbent goes from 0.713/0.627 to
**0.817/0.651**, recall +0.104, and its served tokens from 181 to 529. gte is
unchanged to three decimals (0.791/0.633 with and without), because its score
distribution never put a kept document below 0.4 of the top. So the floor was
costing the incumbent a tenth of its recall while leaving the candidate alone,
and reading the two arms without the fix said gte was the better retriever. It
is not. It is the cheaper one.

## What it does not settle

**Which one to adopt is a latency decision now, not a quality one.** The
quality gap is +0.026 recall (not significant) and +0.018 MRR in the
incumbent's favour, against 5.5x the CPU time. On this hardware, at top_k=16,
that is ~2.2s per query for gte and ~12s for the incumbent: in-request the
choice is gte or nothing, and the incumbent is an offline or GPU answer.
Neither of those sentences is a measurement of the pod, which is the next thing
to measure.

**Adopting gte is blocked on a supply-chain decision, not on quality.** It
defines its own architecture and only loads with `trust_remote_code`, which
executes 1563 lines of Python served by `Alibaba-NLP/new-impl` at load time.
Those lines were read for this measurement: pure model definition, no
filesystem, no network, no subprocess. That is an audit of one revision
(`40ced75c3017eb27626c9d4ea981bde21a2662f4`), not of whatever that repository
serves next month. `LocalReranker` keeps `trust_remote_code=False` and only the
bench passes True, on a throwaway database. Adopting gte means pinning both
revisions and writing that decision down, or it means not adopting gte. The
incumbent needs none of this.

**The abstain floor is untouched.** `abstention_correct` is 0.000 in every arm:
71 questions whose right answer is silence, and the pipeline serves ten hits to
all of them. The reranker logit is the honest floor for that
(`--grader-rerank-floor` exists and was not swept here), and it is the rest of
task f0d24fdb, not a footnote to this measurement.

**One dataset, two conversations, English.** LoCoMo is the public bench already
wired; the workload is mixed Italian and English, and nothing here measures the
Italian half.
