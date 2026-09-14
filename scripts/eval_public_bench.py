"""Public memory benchmarks (LongMemEval / LOCOMO) over the REAL pipeline
(task cc4653bd). Ingest a dataset file into THROWAWAY orgs (one per
instance -- each LongMemEval entry carries its own haystack; each LOCOMO
sample is one conversation) and score retrieval with the same
``eval_offline.run_eval`` path as the CI gate.

Run against a disposable database (the script creates orgs and writes
blobs; never point it at prod):

    docker run -d --rm -e POSTGRES_USER=mycelium -e POSTGRES_PASSWORD=mycelium \
        -e POSTGRES_DB=mycelium -p 5436:5432 pgvector/pgvector:pg16
    # bootstrap_roles.sql + alembic upgrade head + db_harden, then:
    MYCELIUM_DATABASE_URL_SYNC=... MYCELIUM_DATABASE_URL=... \
        uv run python scripts/eval_public_bench.py \
        --dataset longmemeval --path ~/data/WORK/mycelium-bench/datasets/longmemeval_oracle.json \
        --limit-instances 20

Datasets are operator-provided (never committed; ~100MB for the full
variants): LongMemEval from huggingface ``xiaowu0162/longmemeval-cleaned``
(``longmemeval_oracle.json`` is the small evidence-only variant), LOCOMO from
github ``snap-research/locomo`` (``data/locomo10.json``).

HONESTY: the report prints the corpus ``model_id`` set. ``['none']`` means no
embedder was importable and every number is KEYWORD-ONLY retrieval; install
the worker's bge-m3 extra (sentence-transformers) for dense numbers. Scores
are retrieval recall@k / MRR + abstention correctness -- not judged QA.

EMBEDDER ROUND (``--embedders spec.json``): run the whole bench once per
candidate embedder and print one paired comparison against the incumbent.
Each candidate gets its own throwaway orgs, so no corpus ever mixes two
vector spaces. Queries and documents are embedded differently when the
checkpoint says so (``EmbedSide``, ADR-0061), which is how production
embeds too. See ``eval_embedder_round`` for the spec format.

    MYCELIUM_DATABASE_URL_SYNC=... MYCELIUM_DATABASE_URL=... \
        uv run python scripts/eval_public_bench.py \
        --dataset locomo --path ~/.../locomo10.json --limit-instances 2 \
        --embedders docs/eval/embedder-round-2026-09.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import Embedder, EmbedSide, set_embedder_override
from mycelium_core.reranker import LocalReranker, set_reranker_override
from mycelium_core.services import eval_embedder_round as round_
from mycelium_core.services import eval_public_bench as bench
from mycelium_core.services.auth import signup
from mycelium_core.services.eval_baselines import SystemRun, paired_table


def _load_instances(dataset: str, path: Path) -> list[bench.BenchInstance]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON list of {dataset} entries")
    parse = (
        bench.parse_longmemeval_instance if dataset == "longmemeval" else bench.parse_locomo_sample
    )
    return [parse(obj) for obj in data]


async def _run_pass(
    instances: list[bench.BenchInstance],
    args: argparse.Namespace,
    *,
    tag: str,
    embedder: Embedder | None = None,
) -> tuple[bench.BenchReport, tuple[bench.InstanceScore, ...]]:
    """One full pass over the dataset: a throwaway org per instance, ingest,
    then score.

    ONE embedder covers both phases. It used to be two, installed around
    ingest and around scoring, because an instruction-tuned model prefixes
    the query side only and the ``Embedder`` seam had no notion of side. The
    seam has one now (``EmbedSide``), so the asymmetry lives where production
    can also use it, and this script no longer has to be read to know which
    phase is running. ``None`` runs the configured embedder, which is the
    plain bench.
    """
    scores: list[bench.InstanceScore] = []
    embedder_models: set[str] = set()
    # Built once, outside the loop: it is constant across instances, and a
    # closure created per iteration would capture the loop's binding rather
    # than its value (ruff B023) -- harmless while the override is consumed
    # immediately, a real bug the first time one is deferred.
    if embedder is not None:
        set_embedder_override(lambda: embedder)
    for i, instance in enumerate(instances):
        async with admin_session() as s:
            r = await signup(
                s,
                email=f"bench-{uuid.uuid4().hex[:10]}@example.test",
                password=uuid.uuid4().hex,  # throwaway org, never logged into
                org_name=f"BENCH-{args.dataset}-{i}",
            )
        org, user = r.org_id, r.user_id
        async with tenant_session(str(org), str(user)) as s:
            await bench.ingest_instance(s, org_id=org, actor_id=user, instance=instance)
        async with tenant_session(str(org), str(user)) as s:
            score = await bench.score_instance(
                s,
                org_id=org,
                actor_id=user,
                instance=instance,
                k=args.k,
                limit_questions=args.limit_questions,
                grader_min_rrf=args.grader_floor,
                grader_min_rerank_score=args.grader_rerank_floor,
                graph=(None if args.graph is None else args.graph == "on"),
            )
            embedder_models.update(await bench.corpus_embedder_models(s, org_id=org))
        scores.append(score)
        print(
            f"  {tag}[{i + 1}/{len(instances)}] {instance.instance_id}: "
            f"{len(instance.units)} units, {len(score.results)} questions scored"
        )

    report = bench.aggregate(
        args.dataset,
        args.k,
        scores,
        sorted(embedder_models),
        grader_min_rrf=args.grader_floor,
        grader_min_rerank_score=args.grader_rerank_floor,
        graph=(None if args.graph is None else args.graph == "on"),
    )
    return report, tuple(scores)


async def _run_round(
    instances: list[bench.BenchInstance],
    args: argparse.Namespace,
    spec: round_.RoundSpec,
) -> None:
    """Every candidate over the same dataset, then one paired comparison."""
    print(f"embedder round: {len(spec.candidates)} candidate(s), baseline={spec.baseline}")
    outcomes: list[round_.CandidateOutcome] = []
    try:
        for candidate in spec.candidates:
            print(f"\n--- {candidate.name} ({candidate.model}) ---")
            emb = round_.build_embedder(candidate)
            report, scores = await _run_pass(
                instances, args, tag=f"{candidate.name} ", embedder=emb
            )
            outcomes.append(
                round_.CandidateOutcome(
                    candidate=candidate,
                    report=report,
                    scores=scores,
                    native_dim=emb.native_dim,
                    query_prompt=emb.declared_prompt(EmbedSide.query),
                )
            )
    finally:
        # Unconditional: a candidate that raises mid-round must not leave a
        # process-global override installed for whatever runs next.
        set_embedder_override(None)
    print()
    print(round_.render_round(outcomes, baseline=spec.baseline))


def _sweep(paths: list[str], floors: list[float]) -> str:
    """What the honest-abstain floor would have done, from a finished run.

    EXACT rather than estimated. ``GraderMinStage`` either drops the entire
    result (top rerank score below the floor) or leaves it exactly as it is,
    so a question's outcome at floor F is decided by two numbers already in
    the dump: the score of the top hit and the rank it had without a floor.
    Re-scoring the corpus once per candidate floor would produce the same
    table for 11 minutes a row (gte) or an hour (the incumbent).

    A question whose ``top_rerank`` is null was never reranked -- the gate
    declined, or the feature was off -- and no floor can touch it. That is
    the same condition the stage checks, and printing the count keeps a sweep
    over a no-reranker dump from reading as "the floor does nothing".
    """
    lines = [
        f"{'system':<26}{'floor':>7}{'n':>6}{'recall':>8}{'MRR':>8}{'abstain ok':>12}{'served':>8}"
    ]
    for raw in paths:
        obj = json.loads(Path(raw).read_text(encoding="utf-8"))
        recs = obj["records"]
        scored = [r for r in recs if not r["impossible"]]
        impossible = [r for r in recs if r["impossible"]]
        graded = sum(1 for r in recs if r.get("top_rerank") is not None)
        for floor in [None, *floors]:

            def cut(r: dict[str, object], f: float | None = floor) -> bool:
                top = r.get("top_rerank")
                return f is not None and isinstance(top, float) and top < f

            hits = [r for r in scored if not cut(r) and r["rank"] is not None]
            rr = sum(1.0 / int(r["rank"]) for r in hits)
            n = len(scored)
            # An abstention question's rank is None BY CONSTRUCTION (nothing
            # is expected), so it says nothing here: what counts is whether
            # the pipeline abstained, or the floor now makes it.
            ok = sum(1 for r in impossible if cut(r) or r["abstained"])
            served = [0 if cut(r) else int(r["served_tokens"]) for r in recs]
            lines.append(
                f"{obj['system']:<26}{'off' if floor is None else format(floor, 'g'):>7}"
                f"{n:>6}{len(hits) / n if n else 0.0:>8.3f}{rr / n if n else 0.0:>8.3f}"
                f"{ok / len(impossible) if impossible else 0.0:>12.3f}"
                f"{round(sum(served) / len(served)) if served else 0:>8d}"
            )
        lines.append(
            f"  ({graded}/{len(recs)} questions carry a rerank score; "
            f"a floor cannot reach the rest)"
        )
    return "\n".join(lines)


def _dump(path: Path, run: SystemRun) -> None:
    path.write_text(
        json.dumps({"system": run.system, "records": run.records}, indent=1),
        encoding="utf-8",
    )
    print(f"per-question results -> {path} ({len(run.records)} records)")


def _compare(paths: list[str]) -> str:
    """Paired table over passes dumped by --dump-results.

    Comparing two runs by their printed recall is comparing two numbers that
    were each computed over the same 230 questions: the questions are paired,
    and a paired test over the discordant ones is both stronger and the only
    one entitled to a p-value here."""
    runs: list[SystemRun] = []
    for raw in paths:
        obj = json.loads(Path(raw).read_text(encoding="utf-8"))
        runs.append(
            SystemRun(system=obj["system"], proxy=False, records=obj["records"], skipped_non_note=0)
        )
    seen = {r.system for r in runs}
    if len(seen) != len(runs):
        raise SystemExit(f"--compare: two runs share a label ({sorted(seen)}); use --label")
    return paired_table(runs, base_system=runs[0].system)


async def _install_candidate_reranker(
    model: str, *, revision: str | None, code_revision: str | None
) -> None:
    """Point the reranker seam at a candidate model for this process.

    Same shape as the embedder round's ``set_embedder_override``: the
    measurement swaps the provider, not the pipeline, so what is compared is
    one model against another through the stage production would run.

    It loads and scores a probe pair BEFORE the bench starts, and lets the
    failure through. The stage swallows a reranker exception by design (it
    degrades to the RRF order so enabling the feature cannot break search),
    which means a candidate that fails to load produces a full, plausible
    report identical to the no-reranker baseline. That report would be read
    as "this model changes nothing" when it means "this model never ran".
    """
    provider = LocalReranker(
        model,
        trust_remote_code=bool(code_revision),
        revision=revision,
        code_revision=code_revision,
    )
    probe = await provider.rerank(
        "quale soglia usa il reranker?",
        ["la soglia di abstain va letta sul punteggio del reranker", "ricetta della carbonara"],
    )
    set_reranker_override(lambda: provider)
    print(f"reranker override: {model} (trust_remote_code, eval only) probe={probe.scores}")


async def main() -> None:
    ap = argparse.ArgumentParser(description="LongMemEval/LOCOMO retrieval bench.")
    ap.add_argument("--dataset", required=True, choices=["longmemeval", "locomo"])
    ap.add_argument("--path", required=True, help="dataset JSON file (operator-provided)")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit-instances", type=int, default=None)
    ap.add_argument(
        "--limit-questions", type=int, default=None, help="per-instance question cap (LOCOMO)"
    )
    ap.add_argument(
        "--embedders",
        default=None,
        metavar="SPEC.json",
        help="run the embedder round: the whole bench once per candidate in the "
        "spec file, then one paired comparison against the incumbent "
        "(see mycelium_core.services.eval_embedder_round)",
    )
    ap.add_argument(
        "--grader-floor",
        type=float,
        default=None,
        help="per-call retrieval_grader_min_rrf override (floor sweep, task f0d24fdb); "
        "RRF-fused domain, clamp at 0.05",
    )
    ap.add_argument(
        "--grader-rerank-floor",
        type=float,
        default=None,
        help="per-call retrieval_grader_min_rerank_score override (honest-abstain "
        "gate, task f0d24fdb): a floor in the reranker's own [0,1] score; only "
        "bites with --rerank / reranker enabled. To SWEEP it use --sweep-floors "
        "on a dump instead, which is exact and costs one pass rather than one "
        "per value.",
    )
    ap.add_argument(
        "--sweep-floors",
        nargs="+",
        default=None,
        metavar="RUN.json",
        help="do not run the bench: for each --dump-results file, print what "
        "the honest-abstain floor would have produced at every value of "
        "--sweep-at. EXACT, not a model: the floor either empties a result or "
        "leaves it untouched, so each question's outcome is a function of the "
        "recorded top rerank score and its unfloored rank. Needs a dump from a "
        "run with the reranker ON (task f0d24fdb).",
    )
    ap.add_argument(
        "--sweep-at",
        default="0.01,0.05,0.1,0.2,0.3,0.5,0.7,0.9",
        metavar="A,B,C",
        help="floors to evaluate with --sweep-floors (the unfloored arm is "
        "always printed first as the baseline).",
    )
    ap.add_argument(
        "--graph",
        choices=["on", "off"],
        default=None,
        help="Fase 4 graph-proximity source A/B (task 561c6aca): 'on'/'off' "
        "overrides the workspace default, which is dark. Run twice, on against "
        "off, for the publishable recall@k delta.",
    )
    ap.add_argument(
        "--reranker-model",
        default=None,
        metavar="HF_ID",
        help="measure a CANDIDATE cross-encoder instead of the configured one "
        "(task f0d24fdb), through the reranker override seam. Remote code is "
        "executed only when --reranker-code-revision pins the commit it comes "
        "from, which is why this lives in the bench and not in the factory: "
        "the candidates in this size class ship their own architecture, and "
        "running their code is a supply-chain decision that has to be taken "
        "deliberately, not inherited from a benchmark. "
        "Needs the stage ON (MYCELIUM_RERANKER_ENABLED=true); "
        "MYCELIUM_RERANKER_TOP_K decides how many pairs it scores, which is "
        "what it costs.",
    )
    ap.add_argument(
        "--reranker-code-revision",
        default=None,
        metavar="SHA",
        help="commit of the repository the candidate's REMOTE CODE comes from "
        "(gte: a sha of Alibaba-NLP/new-impl). Required by LocalReranker "
        "whenever remote code is executed, here too: a measurement that runs "
        "unpinned code is measuring something nobody can name later.",
    )
    ap.add_argument(
        "--reranker-revision",
        default=None,
        metavar="SHA",
        help="commit of the candidate's CHECKPOINT repository (optional; "
        "weights moving under a fixed id is a reproducibility problem).",
    )
    ap.add_argument(
        "--dump-results",
        default=None,
        metavar="OUT.json",
        help="write this pass's PER-QUESTION results (qid, category, instance, "
        "rank, served tokens) so two passes can be compared as a paired "
        "design later. An aggregate recall alone cannot say whether a delta "
        "is the model or the resampling.",
    )
    ap.add_argument(
        "--label",
        default=None,
        help="name this pass carries in --dump-results and in the compared "
        "table (default: the reranker model, else 'baseline')",
    )
    ap.add_argument(
        "--compare",
        nargs="+",
        default=None,
        metavar="RUN.json",
        help="do not run the bench: load these --dump-results files and print "
        "the paired comparison (exact McNemar on discordant hits, "
        "cluster-bootstrap CI on the MRR delta, clusters = bench instance). "
        "The FIRST file is the baseline every other is compared against.",
    )
    args = ap.parse_args()

    if args.compare:
        print(_compare(args.compare))
        return

    if args.sweep_floors:
        print(_sweep(args.sweep_floors, [float(x) for x in args.sweep_at.split(",")]))
        return

    instances = _load_instances(args.dataset, Path(args.path))
    if args.limit_instances is not None:
        instances = instances[: args.limit_instances]
    print(f"{args.dataset}: {len(instances)} instance(s) from {args.path}")

    if args.reranker_model:
        await _install_candidate_reranker(
            args.reranker_model,
            revision=args.reranker_revision,
            code_revision=args.reranker_code_revision,
        )
    try:
        if args.embedders:
            await _run_round(instances, args, round_.load_round_spec(args.embedders))
            return

        report, scores = await _run_pass(instances, args, tag="")
        if args.dump_results:
            label = args.label or args.reranker_model or "baseline"
            _dump(Path(args.dump_results), bench.system_run(scores, system=label))
        print()
        print(report.render())
    finally:
        set_reranker_override(None)


if __name__ == "__main__":
    asyncio.run(main())
