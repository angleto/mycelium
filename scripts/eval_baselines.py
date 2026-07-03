"""WS-EVAL T6 (task af679753): external baselines + paired ablations on the
same synthetic workspace and query set (protocol nota 0cb0dda0 §1.5).

Runs against a DISPOSABLE database (creates an org and ingests the
workspace; never point it at prod):

    docker run -d --rm -e POSTGRES_USER=mycelium -e POSTGRES_PASSWORD=mycelium \
        -e POSTGRES_DB=mycelium -p 5447:5432 pgvector/pgvector:pg16
    # bootstrap_roles.sql + alembic upgrade head + db_harden, then:
    MYCELIUM_DATABASE_URL_SYNC=... MYCELIUM_DATABASE_URL=... \
        uv run python scripts/eval_baselines.py --scale 1000 --out ~/…/t6

Systems: mycelium (full), mycelium_humus_off, optional mycelium_rerank
(--rerank; needs the sentence-transformers extra), bm25 (proxy ablation
lexical-only), dense_only (proxy ablation), naive_rag. Output: one JSONL
per system in the eval_report dialect + manifest with SHA256 + the paired
comparison table (McNemar on hits, cluster-bootstrap CI on ΔMRR).
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import get_embedder
from mycelium_core.services.auth import signup
from mycelium_core.services.eval_baselines import paired_table, run_baselines, write_runs
from mycelium_core.services.eval_queries import build_queries
from mycelium_core.services.eval_workspace import generate_workspace, ingest_workspace


def _parse() -> tuple[argparse.Namespace, Path]:
    ap = argparse.ArgumentParser(description="WS-EVAL T6 baselines + ablations.")
    ap.add_argument("--scale", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--query-seed", type=int, default=1042)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rerank", action="store_true", help="also run mycelium_rerank")
    args = ap.parse_args()
    return args, Path(args.out).expanduser()


async def main(args: argparse.Namespace, out_dir: Path) -> None:
    ws = generate_workspace(seed=args.seed, scale=args.scale)
    queries = build_queries(ws, seed=args.query_seed)
    print(f"workspace: {len(ws.units)} unità, {len(ws.facts)} fatti; query: {len(queries)}")

    async with admin_session() as s:
        owner = await signup(
            s,
            email=f"wseval-t6-{uuid.uuid4().hex[:8]}@example.test",
            password=uuid.uuid4().hex,
            org_name="WSEVAL-T6",
        )
    async with tenant_session(str(owner.org_id), str(owner.user_id)) as s:
        ingest = await ingest_workspace(s, org_id=owner.org_id, actor_id=owner.user_id, ws=ws)
    print(f"ingested: {len(ingest.units)} unità, {len(ingest.project_ids)} progetti")

    embedder = get_embedder()
    async with tenant_session(str(owner.org_id), str(owner.user_id)) as s:
        runs = await run_baselines(
            s,
            org_id=owner.org_id,
            actor_id=owner.user_id,
            ws=ws,
            ingest=ingest,
            records=queries,
            embedder=embedder,
            k=args.k,
            rerank=args.rerank,
        )
    manifest = write_runs(runs, out_dir)
    print(f"records in {out_dir} ({len(manifest['systems'])} sistemi)")
    print()
    print(paired_table(runs, seed=args.seed))


if __name__ == "__main__":
    _args, _out = _parse()
    asyncio.run(main(_args, _out))
