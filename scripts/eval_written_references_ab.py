"""ADR-0073 decision 8, retrieval: written references as graph evidence, A/B
on the WS-EVAL synthetic workspace.

The workspace is the T1 generator's, with a pre-registered fraction of its
``related`` links between notes written as code chips in the citing body
instead of typed (``eval_workspace.write_links_as_references``: fraction
and seed are module constants, fixed before any run). Both arms run the full
pipeline with the graph stage on, over the same T2 queries on the same
ingested workspace; the second also reads written references as edges.

The setting ``graph_written_references_enabled`` may be turned on only if
neither recall@k nor MRR is lower beyond its cluster-bootstrap interval
(``eval_baselines.written_reference_verdict``). The run is recorded with
its revision: the manifest carries the git sha passed in ``--revision``.

Runs against a DISPOSABLE database (creates an org and ingests the
workspace; never point it at prod):

    make test-db-up TEST_DB_PORT=<free port>
    MYCELIUM_DATABASE_URL_SYNC=... MYCELIUM_DATABASE_URL=... \\
        uv run python scripts/eval_written_references_ab.py --scale 1000 \\
        --revision "$(git rev-parse HEAD)" --out <dir>
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import uuid
from collections import Counter
from pathlib import Path

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import get_embedder
from mycelium_core.services.auth import signup
from mycelium_core.services.eval_baselines import (
    paired_table,
    run_written_reference_ab,
    write_runs,
    written_reference_verdict,
)
from mycelium_core.services.eval_queries import build_queries
from mycelium_core.services.eval_workspace import (
    WRITTEN_REFERENCE_FRACTION,
    WRITTEN_REFERENCE_SEED,
    generate_workspace,
    ingest_workspace,
    write_links_as_references,
)


def _parse() -> tuple[argparse.Namespace, Path]:
    ap = argparse.ArgumentParser(description="ADR-0073 written-reference retrieval A/B.")
    ap.add_argument("--scale", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--query-seed", type=int, default=1042)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--revision", required=True, help="git sha the run measures")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    return args, Path(args.out).expanduser()


async def main(args: argparse.Namespace, out_dir: Path) -> None:
    ws = write_links_as_references(generate_workspace(seed=args.seed, scale=args.scale))
    queries = build_queries(ws, seed=args.query_seed)
    n_written = sum(len(u.written_refs) for u in ws.units)
    print(
        f"workspace: {len(ws.units)} units, {len(ws.facts)} facts, "
        f"{n_written} written references; queries: {len(queries)}"
    )

    async with admin_session() as s:
        owner = await signup(
            s,
            email=f"adr0073-ab-{uuid.uuid4().hex[:8]}@example.test",
            password=uuid.uuid4().hex,
            org_name="ADR0073-AB",
        )
    async with tenant_session(str(owner.org_id), str(owner.user_id)) as s:
        ingest = await ingest_workspace(s, org_id=owner.org_id, actor_id=owner.user_id, ws=ws)

    async with tenant_session(str(owner.org_id), str(owner.user_id)) as s:
        runs = await run_written_reference_ab(
            s,
            org_id=owner.org_id,
            actor_id=owner.user_id,
            ws=ws,
            ingest=ingest,
            records=queries,
            embedder=get_embedder(),
            k=args.k,
        )
    manifest = write_runs(runs, out_dir)
    verdict = written_reference_verdict(runs, seed=args.seed)
    graph = {
        run.system: Counter(
            "contributed" if r["graph_contributed"] else ("ran" if r["graph_ran"] else "skipped")
            for r in run.records
        )
        for run in runs
    }
    # The stratum the written references can act on: queries with a gold
    # unit at either end of one. Outside it the arms can differ only through
    # longer walks, so a pooled interval that straddles zero mostly says how
    # few queries were exposed; the count is printed beside the verdict.
    ends = {u.unit_id for u in ws.units if u.written_refs} | {
        t for u in ws.units for t in u.written_refs
    }
    exposed = {q.query_id for q in queries if ends & set(q.gold_unit_ids)}
    exposed_runs = [
        dataclasses.replace(run, records=[r for r in run.records if r["qid"] in exposed])
        for run in runs
    ]
    exposed_verdict = (
        written_reference_verdict(exposed_runs, seed=args.seed)
        if all(any(not r["impossible"] for r in run.records) for run in exposed_runs)
        else None
    )
    record = {
        "revision": args.revision,
        "scale": args.scale,
        "seed": args.seed,
        "query_seed": args.query_seed,
        "k": args.k,
        "written_reference_fraction": WRITTEN_REFERENCE_FRACTION,
        "written_reference_seed": WRITTEN_REFERENCE_SEED,
        "written_references": n_written,
        "graph_stage": {name: dict(c) for name, c in graph.items()},
        "verdict": {
            "n": verdict.n,
            "d_recall": verdict.d_recall,
            "recall_ci": list(verdict.recall_ci),
            "d_mrr": verdict.d_mrr,
            "mrr_ci": list(verdict.mrr_ci),
            "mcnemar_p": verdict.mcnemar_p,
            "passes": verdict.passes,
        },
        "exposed_queries": len(exposed),
        "exposed_verdict": None
        if exposed_verdict is None
        else {
            "n": exposed_verdict.n,
            "d_recall": exposed_verdict.d_recall,
            "recall_ci": list(exposed_verdict.recall_ci),
            "d_mrr": exposed_verdict.d_mrr,
            "mrr_ci": list(exposed_verdict.mrr_ci),
        },
        "files": manifest["systems"],
    }
    (out_dir / "written_references_ab.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print()
    print(paired_table(runs, base_system="mycelium_graph", seed=args.seed))
    print()
    print(f"graph stage per arm: {record['graph_stage']}")
    print(
        f"verdict: Δrecall@{args.k} {verdict.d_recall:+.3f} "
        f"[{verdict.recall_ci[0]:+.3f},{verdict.recall_ci[1]:+.3f}], "
        f"ΔMRR {verdict.d_mrr:+.3f} [{verdict.mrr_ci[0]:+.3f},{verdict.mrr_ci[1]:+.3f}] "
        f"-> {'passes' if verdict.passes else 'does not pass'}"
    )
    if exposed_verdict is not None:
        ev = exposed_verdict
        print(
            f"exposed stratum ({ev.n} queries with a gold unit at an end of a written "
            f"reference): Δrecall {ev.d_recall:+.3f} "
            f"[{ev.recall_ci[0]:+.3f},{ev.recall_ci[1]:+.3f}], "
            f"ΔMRR {ev.d_mrr:+.3f} [{ev.mrr_ci[0]:+.3f},{ev.mrr_ci[1]:+.3f}]"
        )


if __name__ == "__main__":
    _args, _out = _parse()
    asyncio.run(main(_args, _out))
