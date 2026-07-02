"""Humus retrieval A/B against a REAL corpus (task 4836a6cc / note 9a2adb4a §4).

The reproducible counterpart to the deterministic CI harness check
(core/tests/test_eval_humus_ab.py): runs the SAME `eval_offline.run_humus_ab`
matrix (Configs A_on / B_branch_off / C_atoms_excluded x ks x {raw,
consolidation}) over an already-populated org, from two gold JSONL files.

PRE-FLIGHT (adversarial review 2026-07-02 -- all four bit the first version):
  1. PROJECT PERIMETER. Retrieval follows memory._project_pred: project_id
     None means blobs with NO project (IS NULL), NOT "no filter". A
     project-scoped corpus MUST pass --project or every case silently misses.
     The script prints the perimeter and aborts if it contains zero blobs.
  2. SIDE EFFECTS. This is NOT read-only: every retrieve bumps access_count
     (feeds ranking decay) and meters query embeddings. Pass explicit
     --org/--actor, or add --accept-side-effects to allow auto-resolution.
  3. BILLING. embed_query meters on basis=local (free unless an embedding
     rate card exists) and embed_query_hosted meters unconditionally when a
     hosted embedder is configured. On a real org check billing_balance and
     rate cards first, or run against a dump org.
  4. FLOOR. Pre-register the org's retrieval_semantic_min_similarity from the
     measured cosine gap (scripts/diag_retrieval.py) BEFORE looking at
     outcomes, and report a fairness sweep across nearby floors: the fairness
     verdict flips with the floor.

In-pod invocation (args DO reach argparse with `python -`; the gold files
must be copied into the pod first):

    kubectl -n mycelium-production cp raw.jsonl <pod>:/tmp/raw.jsonl
    kubectl -n mycelium-production cp con.jsonl <pod>:/tmp/con.jsonl
    kubectl -n mycelium-production exec -i deploy/mycelium-backend -- \
        python - --raw /tmp/raw.jsonl --consolidation /tmp/con.jsonl \
        --org <uuid> --actor <uuid> [--project <uuid>] \
        < scripts/eval_humus_ab.py

or locally against a DB seeded with a real dump (MYCELIUM_DATABASE_URL* env).

Gold file formats (JSONL, one object per line):
  raw.jsonl:           {"query": "...", "expected_blob_ids": ["<uuid>", ...]}
  consolidation.jsonl: {"query": "...", "atom_blob_ids": ["<uuid>", ...],
                        "source_blob_ids": ["<uuid>", ...]}
Consolidation queries should be sourced INDEPENDENTLY of the atom text (e.g.
from real past search queries), not authored by whoever wrote the atoms.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import func, select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services import eval_offline


async def _resolve_org_actor(
    org_arg: str | None, actor_arg: str | None, project: str | None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Use the passed org/actor if given; else pick an owner membership whose
    org actually has blobs (optionally within ``project``), mirroring
    scripts/diag_retrieval.py."""
    if org_arg and actor_arg:
        return uuid.UUID(org_arg), uuid.UUID(actor_arg)
    async with admin_session() as s:
        owners = (
            await s.execute(
                select(Membership.org_id, Membership.user_id).where(Membership.role == Role.owner)
            )
        ).all()
    for cand_org, cand_user in owners:
        async with tenant_session(str(cand_org), str(cand_user), project_id=project) as s:
            stmt = select(MemoryBlob.id).limit(1)
            if project is not None:
                stmt = stmt.where(MemoryBlob.project_id == project)
            if (await s.execute(stmt)).first() is not None:
                return cand_org, cand_user
    raise SystemExit("no owner org with blobs found; pass --org and --actor explicitly")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Humus retrieval A/B over a real corpus.")
    ap.add_argument("--raw", required=True, help="raw-recall gold JSONL")
    ap.add_argument("--consolidation", required=True, help="consolidation gold JSONL")
    ap.add_argument("--org", default=None, help="org uuid (else auto-resolve an owner)")
    ap.add_argument("--actor", default=None, help="member uuid (else auto-resolve)")
    ap.add_argument(
        "--project",
        default=None,
        help="project uuid: the RETRIEVAL PERIMETER (omitted = only blobs "
        "with NO project are scored, per memory._project_pred)",
    )
    ap.add_argument("--ks", default="3,5,10", help="comma-separated k values")
    ap.add_argument(
        "--accept-side-effects",
        action="store_true",
        help="required to run against an AUTO-RESOLVED org (the run bumps "
        "access counters and meters query embeddings on that org)",
    )
    args = ap.parse_args()

    explicit_target = bool(args.org and args.actor)
    if not explicit_target and not args.accept_side_effects:
        raise SystemExit(
            "refusing to auto-resolve a live org without --accept-side-effects "
            "(the run mutates access counters and meters embeddings); "
            "pass --org/--actor explicitly or add the flag"
        )

    ks = tuple(int(x) for x in args.ks.split(",") if x.strip())
    raw_cases = eval_offline.load_cases(args.raw)
    consolidation_cases = eval_offline.load_consolidation_cases(args.consolidation)
    org_id, actor_id = await _resolve_org_actor(args.org, args.actor, args.project)
    project_id = uuid.UUID(args.project) if args.project else None

    print(f"org_id={org_id}  actor_id={actor_id}  ks={ks}")
    print(f"raw_cases={len(raw_cases)}  consolidation_cases={len(consolidation_cases)}")
    perimeter = f"project_id == {project_id}" if project_id else "project_id IS NULL"
    print(f"retrieval perimeter: {perimeter}")
    async with tenant_session(str(org_id), str(actor_id), project_id=args.project) as s:
        n_in_perimeter = (
            await s.execute(
                select(func.count())
                .select_from(MemoryBlob)
                .where(
                    MemoryBlob.org_id == org_id,
                    MemoryBlob.project_id == project_id
                    if project_id is not None
                    else MemoryBlob.project_id.is_(None),
                )
            )
        ).scalar_one()
        print(f"blobs in perimeter: {n_in_perimeter}")
        if not n_in_perimeter:
            raise SystemExit(
                "the retrieval perimeter contains ZERO blobs -- every case would "
                "miss artificially. Pass the correct --project (or none for "
                "project-less blobs)."
            )
        report = await eval_offline.run_humus_ab(
            s,
            org_id=org_id,
            actor_id=actor_id,
            raw_cases=raw_cases,
            consolidation_cases=consolidation_cases,
            ks=ks,
            project_id=project_id,
        )
    print(report.render())


if __name__ == "__main__":
    asyncio.run(main())
