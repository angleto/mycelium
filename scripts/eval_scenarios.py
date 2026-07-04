"""WS-EVAL T3 scenario runs over the REAL MCP tool layer (task 0cea068d,
protocol nota 0cb0dda0 §4 + §6).

Generates a T1 workspace, ingests it into a THROWAWAY org (this script
creates orgs, members, notes and erases some of them: NEVER point it at
prod), builds the T2 adversarial queries, then drives the scenarios as
three distinct actors (two agents + one human reviewer) through
``mycelium_mcp.gateway.execute_tool``:

    docker run -d --rm -e POSTGRES_USER=mycelium -e POSTGRES_PASSWORD=mycelium \
        -e POSTGRES_DB=mycelium -p 5446:5432 pgvector/pgvector:pg16
    # bootstrap_roles.sql + alembic upgrade head + db_harden, then:
    MYCELIUM_DATABASE_URL_SYNC=... MYCELIUM_DATABASE_URL=... \
        uv run python scripts/eval_scenarios.py --scale 1000 \
        --out ~/data/WORK/mycelium-bench/scenarios/run1

Outputs ``scenario_records.jsonl`` (the eval_report dialect: feed it to
``scripts/eval_report.py`` together with the static-query records),
``scenario_steps.jsonl`` (the full actor/tool/latency trail) and a manifest
with SHA256 hashes. Latency figures are labelled with the local hardware —
they are indicative, not the protocol's latency measurement (§5 has its own
procedure)."""

from __future__ import annotations

import argparse
import asyncio
import platform
import uuid
from pathlib import Path

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services.auth import signup
from mycelium_core.services.eval_queries import build_queries
from mycelium_core.services.eval_workspace import generate_workspace, ingest_workspace
from mycelium_core.services.memberships import add_member
from mycelium_mcp.eval_scenarios import (
    BlobMap,
    ScenarioActor,
    ScenarioRunner,
    mint_actor_tokens,
    redteam_perimeter,
    run_auth_spoof_probe,
    run_concurrency_latency,
    run_dense_visibility_probe,
    run_erasure,
    run_freshness_interactive,
    run_humus_cycle,
    run_kg_and_walk,
    run_multi_agent,
    run_perimeter,
    run_personal_to_shared,
    run_review_gate_cycles,
    run_static_queries,
    run_write_race_tournament,
    write_scenario_artifacts,
)


async def _member_actor(
    org_id: uuid.UUID, owner_id: uuid.UUID, name: str, kind: str, role: str = "owner"
) -> ScenarioActor:
    # role="owner" is deliberate: §6(A5) drives calls through real agent
    # tokens, which are owner-bound in mycelium's token model, so a distinct
    # token-bearing identity must be an owner.
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"wseval-{name}-{uuid.uuid4().hex[:8]}@example.test",
            password=uuid.uuid4().hex,
            org_name=f"WSEVAL-{name}",
        )
    async with admin_session() as s:
        from sqlalchemy import select

        from mycelium_core.models.user import User

        email = (await s.execute(select(User).where(User.id == r.user_id))).scalar_one().email
    async with tenant_session(str(org_id), str(owner_id)) as s:
        await add_member(s, org_id=org_id, actor_id=owner_id, email=email, role=role)
    return ScenarioActor(name=name, kind=kind, user_id=r.user_id, org_id=org_id)


def _parse() -> tuple[argparse.Namespace, Path]:
    ap = argparse.ArgumentParser(description="WS-EVAL T3 MCP scenario runner.")
    ap.add_argument("--scale", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--query-seed", type=int, default=1042)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--out", required=True)
    ap.add_argument("--redteam-attempts", type=int, default=20)
    ap.add_argument("--skip-concurrency", action="store_true")
    ap.add_argument("--concurrency-per-client", type=int, default=100)
    ap.add_argument("--concurrency-replicas", type=int, default=5)
    ap.add_argument("--races", type=int, default=60, help="A4 write-race tournament size")
    ap.add_argument("--gate-cycles", type=int, default=60, help="A6 review-gate leak cycles")
    args = ap.parse_args()
    return args, Path(args.out).expanduser()


async def main(args: argparse.Namespace, out_dir: Path) -> None:
    ws = generate_workspace(seed=args.seed, scale=args.scale)
    queries = build_queries(ws, seed=args.query_seed)
    print(f"workspace: {len(ws.units)} unità, {len(ws.facts)} fatti; query: {len(queries)}")

    async with admin_session() as s:
        owner = await signup(
            s,
            email=f"wseval-human-{uuid.uuid4().hex[:8]}@example.test",
            password=uuid.uuid4().hex,
            org_name="WSEVAL-T3",
        )
    org_id = owner.org_id
    human = ScenarioActor(name="human", kind="human", user_id=owner.user_id, org_id=org_id)
    agent_a = await _member_actor(org_id, owner.user_id, "agent_a", "agent")
    agent_b = await _member_actor(org_id, owner.user_id, "agent_b", "agent")
    # §6(A5): drive calls through REAL agent tokens (owner-bound), not an
    # injected principal.
    human, agent_a, agent_b = await mint_actor_tokens([human, agent_a, agent_b])

    async with tenant_session(str(org_id), str(owner.user_id)) as s:
        ingest = await ingest_workspace(s, org_id=org_id, actor_id=owner.user_id, ws=ws)
    blobmap = BlobMap(org_id, owner.user_id)
    await blobmap.load(ingest)
    print(f"ingested: {len(ingest.units)} unità, {len(ingest.project_ids)} progetti")

    runner = ScenarioRunner(
        org_id=org_id,
        k=args.k,
        seed=args.seed,
        hardware_label=f"{platform.platform()} / {platform.machine()}",
    )
    await run_static_queries(
        runner, agent_a, ws=ws, ingest=ingest, blobmap=blobmap, queries=queries
    )
    project_name = next(iter(sorted(ingest.project_ids)))
    await run_freshness_interactive(
        runner, agent_a, agent_b, project_name=project_name, ingest=ingest, blobmap=blobmap
    )
    displacement = [q for q in queries if q.category == "collision"][:2]
    await run_humus_cycle(
        runner,
        agent_a,
        human,
        ws=ws,
        ingest=ingest,
        blobmap=blobmap,
        displacement_queries=displacement,
    )
    await run_erasure(
        runner, agent_a, human, ws=ws, ingest=ingest, blobmap=blobmap, queries=queries
    )
    await run_perimeter(runner, agent_b, ws=ws, ingest=ingest, blobmap=blobmap, queries=queries)
    await redteam_perimeter(
        runner, agent_b, ws=ws, ingest=ingest, blobmap=blobmap, attempts=args.redteam_attempts
    )
    await run_kg_and_walk(runner, agent_a, ws=ws, ingest=ingest)
    await run_multi_agent(
        runner,
        agent_a,
        agent_b,
        human,
        ingest=ingest,
        blobmap=blobmap,
        project_name=project_name,
    )
    project_id = ingest.project_ids.get(project_name)
    # §6 hardened scenarios.
    await run_auth_spoof_probe(runner, agent_a, agent_b, project_id=project_id)
    await run_dense_visibility_probe(runner, agent_a, agent_b, project_id=project_id)
    await run_personal_to_shared(runner, human, agent_b, project_name=project_name, ingest=ingest)
    tourney = await run_write_race_tournament(
        runner, [agent_a, agent_b, human], project_id=project_id, races=args.races
    )
    gate = await run_review_gate_cycles(
        runner, agent_a, agent_b, human, agent_b, project_id=project_id, cycles=args.gate_cycles
    )
    print(f"write-race tournament: {tourney}")
    print(f"review-gate cycles: {gate}")
    if not args.skip_concurrency:
        pool = [q.query_text for q in queries[:10]]
        await run_concurrency_latency(
            runner,
            [agent_a, agent_b, human],
            queries=pool,
            project_id=project_id,
            per_client=args.concurrency_per_client,
            replicas=args.concurrency_replicas,
        )

    manifest = write_scenario_artifacts(runner, out_dir)
    print(f"records={manifest['records']} steps={manifest['steps']} events={manifest['events']}")
    print(f"authenticated_steps={manifest['authenticated_steps']}")
    for cat, b in manifest["zero_event_bounds"].items():
        print(f"  bound {cat}: {b['events']}/{b['n']} events, cp95_upper={b['cp95_upper']}")
    for sk in manifest["skipped"]:
        print(f"  skipped: {sk['scenario']}: {sk['reason']}")
    for row in manifest["concurrency_latency"]:
        print(
            f"  concurrency {row['concurrent_workers']}: p50={row['p50_ms']}ms "
            f"p95={row['p95_ms']}ms CI95={row['p95_ci95_ms']} ({row['hardware']})"
        )


if __name__ == "__main__":
    _args, _out = _parse()
    asyncio.run(main(_args, _out))
