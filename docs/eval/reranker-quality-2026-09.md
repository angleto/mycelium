# Reranker quality: what a cross-encoder buys, and which one (2026-09-13)

Three systems through the production pipeline on LoCoMo (2 conversations,
k=10, 230 paired scored questions plus 71 abstention questions): no reranker,
`Alibaba-NLP/gte-multilingual-reranker-base` (the candidate, 306M) and
`BAAI/bge-reranker-v2-m3` (the incumbent, 568M). Task f0d24fdb.

The latency measurement that produced the candidate, on a 4-thread CPU: the
incumbent takes 7.8s at 10 pairs and 44s at 50, gte 1.4s and 8.8s. That made
gte cheap. It did not make it good, and quality is the measurement that was
missing.

Every timing here is from a development machine, not from the pod. It shares
the node's core COUNT (4) and not its cores, so these are lower bounds on what
production would pay, and they are labelled as such wherever they appear.

## How it ran

Same dataset, same ingest, same embedder (bge-m3), a fresh throwaway database
per arm. The reranker arms run with `MYCELIUM_RERANKER_ENABLED=true` and
`MYCELIUM_RERANKER_TOP_K=16`, the candidate injected through the reranker
override seam by `scripts/eval_public_bench.py --reranker-model`, which loads
it with `trust_remote_code` so the production factory never has to.

`top_k=16` and not 50 on purpose: it is the setting that could plausibly ship
in-request for gte (~2.2s per query on the development machine, so more on the
node). A quality number measured at a cost nobody would pay is not a quality
number anyone can use.

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
cut there is `GraderMinStage`'s `min_rerank_score`, calibrated in that domain
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
incumbent's favour, against 5.5x the CPU time. On the development machine, at
top_k=16, that is ~2.2s per query for gte and ~12s for the incumbent: in-request
the choice is gte or nothing, and the incumbent is an offline or GPU answer.

Neither number is the pod's. The node is `scw-bvphoenix-production-clus-
mycelium-...`, 4 CPUs and 3800m allocatable, and the backend carries no CPU
limit, so it competes for those four cores with the worker and already embeds
every query with bge-m3. Memory is the other unmeasured half: the backend
limit is 6Gi with bge-m3 already resident, and the local RSS reading is not a
valid proxy (safetensors memory-maps the weights, so the process counts only
the pages it touches while a cgroup counts them differently). The fp32 upper
bounds are 2.27Gi for the incumbent and 1.22Gi for gte. Both numbers can only
be taken on the pod.

**Adopting gte is blocked on a supply-chain decision, not on quality.** It
defines its own architecture and only loads with `trust_remote_code`, which
executes 1563 lines of Python served by `Alibaba-NLP/new-impl` at load time.
Those lines were read for this measurement: pure model definition, no
filesystem, no network, no subprocess. That is an audit of one revision
(`40ced75c3017eb27626c9d4ea981bde21a2662f4`), not of whatever that repository
serves next month.

That is now bounded rather than open: both repositories can be pinned (the
checkpoint through `revision`, the code through `code_revision`, verified by
loading the model that way on 2026-09-13), and `LocalReranker` REFUSES
`trust_remote_code` without the code pin. The production factory passes
neither, so a deployment cannot execute remote code at all. Adopting gte means
writing the two shas down and saying who read them; the incumbent needs none of
this, which is a real part of its price difference.

## Coda, 2026-09-14: the two numbers taken on the pod, and what they close

The section above ends on "both numbers can only be taken on the pod". They
were, with a throwaway Job on the `mycelium` pool (node
`scw-bvphoenix-production-clus-mycelium--67b247`, arm64, 4 CPU / 3800m
allocatable), the backend image of the tag in service, the model cache in its
own `emptyDir`, and memory read from the container's cgroup rather than from
the process. Task `03cdf674`; the probe is `scripts/perf/reranker_pod_probe.py`
and the manifest lives in the deploy repository.

**The small candidate does not run there at all.**
`gte-multilingual-reranker-base` loads (24.6s, 1.48 GiB of cgroup) and then
fails every forward pass, including on a single short pair:

    modeling.py, line 392, in forward
        rope_cos = rope_cos[position_ids].unsqueeze(2)
    IndexError: index 2464942325760 is out of bounds for dimension 0 with size 304

The index differs each run (2464942325760, then 4276378337280), so it is
uninitialised memory read as an index inside the remote code, not an input out
of range. The same id and the same pinned sha work on the development machine.
This is the one thing no laptop measurement could have said, and it removes the
model the latency argument above was pointing at.

**The incumbent runs, and costs about 2.2x its laptop figure.**

| | pod | laptop | ratio |
|---|---|---|---|
| load | 114.6s | - | - |
| top_k=10 | **16.74s** | 7.8s | 2.15x |
| top_k=16 | **27.18s** | 12s | 2.27x |
| top_k=50 (the code default) | **120.9s** | 44s | 2.75x |

Memory, from the cgroup: 668 MiB before, **3390 MiB after the load**, peak
**3743 MiB**, so the model adds ~2.7 GiB to the container. That sits inside the
interval this document predicted (2.27 GiB of fp32 weights plus activations and
allocator arena) and confirms the local +0.42 GB reading was an artefact of
counting RSS on memory-mapped weights.

**What that decides.** The criterion was gte under ~1s at top_k=10 with room
under 6Gi. Not only is it unmet, the model it rested on does not work on the
node. The global switch stays off with a number beside it, and the sentence
"2-5s is within the noise of an agent turn" is falsified: on the node it is
16.7s at top_k=10 and 121s at the shipped default of 50, so a caller that flips
`rerank=true` without also lowering `reranker_top_k` waits two minutes. A
cross-encoder on this node, on CPU, is not an interactive-path component. What
remains open, and unmeasured, is a hosted reranker, a much smaller int8 model,
or reranking off the response path -- none of which this measurement touched.

**A supply-chain detail found in the logs.** With `code_revision` pinned,
`modeling.py` came from the pinned sha but transformers still reported
downloading a new version of `configuration.py` from the head of the same
`new-impl` repository. The pin `LocalReranker` demands therefore covers the
model definition and not everything that executes.

## The honest-abstain floor: swept on both models, and it buys nothing

`abstention_correct` is 0.000 in every arm above: 71 questions whose right
answer is silence, and the pipeline serves ten hits to all of them. Since
2026-07 the plan for that has been the reranker's own score, on the argument
that it grades relevance directly where the RRF sum only grades position.

Now swept. Each question's outcome at a floor F is decided by two numbers
already recorded per question, the top hit's rerank score and its unfloored
rank, because the floor either empties a result or leaves it untouched: the
tables below are exact, not a model, and come from one pass per model rather
than one pass per row (`--sweep-floors`).

```
system   floor     n  recall     MRR  abstain ok  served
gte        off   230   0.791   0.633       0.000     477
gte        0.4   230   0.791   0.633       0.028     472
gte       0.45   230   0.774   0.625       0.056     457
gte        0.5   230   0.704   0.574       0.254     407
gte       0.55   230   0.570   0.467       0.507     314
gte        0.6   230   0.396   0.331       0.704     206

m3         off   230   0.817   0.651       0.000     523
m3        0.05   230   0.809   0.646       0.099     506
m3         0.1   230   0.783   0.627       0.183     486
m3         0.2   230   0.757   0.603       0.254     459
m3         0.3   230   0.713   0.568       0.324     426
m3         0.5   230   0.639   0.512       0.423     375
```

The incumbent's curve is gentler, and around 0.05-0.1 it looks like a free
lunch: 18% of the impossible questions answered with silence for three points
of recall. It is not one. Count "answered correctly OR honestly silent" over
all 301 questions and the trade is paired, so it can be tested rather than
admired:

```
--- gte: baseline 182/301 right
  floor 0.4   184/301  lost   0  gained   2  McNemar p=0.5000
  floor 0.45  182/301  lost   4  gained   4  McNemar p=1.0000
  floor 0.5   180/301  lost  20  gained  18  McNemar p=0.8714
--- m3: baseline 188/301 right
  floor 0.05  193/301  lost   2  gained   7  McNemar p=0.1797
  floor 0.1   193/301  lost   8  gained  13  McNemar p=0.3833
  floor 0.2   192/301  lost  14  gained  18  McNemar p=0.5966
  floor 0.3   187/301  lost  24  gained  23  McNemar p=1.0000
```

The best point either model reaches is +5 questions out of 301 at p=0.18. Every
other one is a wash or a loss.

The reason is in the distributions, and it is not a threshold that needs
finding:

```
            answerable (230)            impossible (71)
gte   med 0.591 [0.532, 0.644]    med 0.547 [0.498, 0.604]   AUC 0.639
m3    med 0.828 [0.494, 0.939]    med 0.633 [0.199, 0.879]   AUC 0.598
```

AUC 0.639 and 0.598, against 0.5 for a coin, and on both models the impossible
questions' MAXIMUM is higher than the answerable ones'. The bigger model, which
is the better retriever, is the WORSE discriminator here. That is what a
cross-encoder is: it scores "is this document relevant to this query", and for
almost every question some document looks plausible. "Is there an answer in
here at all" is a different question and this signal does not answer it.

So the July hypothesis is not supported: the honest-abstain gate does not get
built on the reranker score. The mechanism stays in the pipeline, correct and
off by default, because the mechanism is right and the signal is what failed;
the per-org key stays unreachable from every surface rather than shipping a
knob with no good value.

What this does not settle: the same sweep on a different signal (a calibrated
answerability head, or the MARGIN between the top and the tail rather than the
top alone, which these dumps could answer without a new run), and LoCoMo's own
notion of an unanswerable question, which is an adversarial paraphrase rather
than an absent subject.

## One last limit on all of it

One dataset, two conversations, English. LoCoMo is the public bench already
wired; the workload is mixed Italian and English, and nothing here measures the
Italian half. Both reranker arms were run twice and reproduced to three
decimals, so the numbers are stable; stability is not generality.
