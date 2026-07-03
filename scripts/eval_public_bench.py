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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import eval_public_bench as bench
from mycelium_core.services.auth import signup


def _load_instances(dataset: str, path: Path) -> list[bench.BenchInstance]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON list of {dataset} entries")
    parse = (
        bench.parse_longmemeval_instance if dataset == "longmemeval" else bench.parse_locomo_sample
    )
    return [parse(obj) for obj in data]


async def main() -> None:
    ap = argparse.ArgumentParser(description="LongMemEval/LOCOMO retrieval bench.")
    ap.add_argument("--dataset", required=True, choices=["longmemeval", "locomo"])
    ap.add_argument("--path", required=True, help="dataset JSON file (operator-provided)")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit-instances", type=int, default=None)
    ap.add_argument(
        "--limit-questions", type=int, default=None, help="per-instance question cap (LOCOMO)"
    )
    args = ap.parse_args()

    instances = _load_instances(args.dataset, Path(args.path))
    if args.limit_instances is not None:
        instances = instances[: args.limit_instances]
    print(f"{args.dataset}: {len(instances)} instance(s) from {args.path}")

    scores: list[bench.InstanceScore] = []
    embedder_models: set[str] = set()
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
            )
            embedder_models.update(await bench.corpus_embedder_models(s, org_id=org))
        scores.append(score)
        n_q = len(score.results)
        print(
            f"  [{i + 1}/{len(instances)}] {instance.instance_id}: "
            f"{len(instance.units)} units, {n_q} questions scored"
        )

    report = bench.aggregate(args.dataset, args.k, scores, sorted(embedder_models))
    print()
    print(report.render())


if __name__ == "__main__":
    asyncio.run(main())
