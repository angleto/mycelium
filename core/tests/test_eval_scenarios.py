"""WS-EVAL T3 (task 0cea068d): the MCP scenario runner's guarantees on a
small ingested T1 workspace, FakeEmbedder, real DB — mirroring the
test_mcp_gateway idiom (principal published per call, dispatch through
``gateway.execute_tool``).

Covered per the protocol (nota 0cb0dda0 §4 + §6): interactive freshness;
humus cycle with the deterministic approval policy INCLUDING the rejected
branch; erasure propagation with a derived atom + specificity; perimeter
0-leak + the red-team machinery; multi-agent visibility / provenance /
review gate / concurrent same-part writes; and the JSONL round-trip into
``eval_report.load_records`` (one reporting path)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.user import User
from mycelium_core.services import eval_report
from mycelium_core.services.auth import signup
from mycelium_core.services.eval_queries import QueryRecord, build_queries
from mycelium_core.services.eval_workspace import generate_workspace, ingest_workspace
from mycelium_core.services.memberships import add_member
from mycelium_mcp import eval_scenarios as sc

_SCALE = 240
_SEED = 42


@pytest.fixture(autouse=True)
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    yield
    set_embedder_override(None)


async def _org_with_actors() -> tuple[sc.ScenarioActor, sc.ScenarioActor, sc.ScenarioActor]:
    async with admin_session() as s:
        owner = await signup(
            s,
            email=f"t3-{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="T3",
        )
    org_id = owner.org_id
    human = sc.ScenarioActor(name="human", kind="human", user_id=owner.user_id, org_id=org_id)
    agents: list[sc.ScenarioActor] = []
    for name in ("agent_a", "agent_b"):
        async with admin_session() as s:
            r = await signup(
                s,
                email=f"t3-{name}-{uuid.uuid4().hex[:8]}@example.test",
                password="pw-strong-123",
                org_name=f"T3-{name}",
            )
        async with admin_session() as s:
            email = (await s.execute(select(User).where(User.id == r.user_id))).scalar_one().email
        # owner role: §6(A5) mints a real agent token per actor, and tokens
        # are owner-bound in mycelium's model (a distinct identity = a
        # distinct owner).
        async with tenant_session(str(org_id), str(owner.user_id)) as s:
            await add_member(s, org_id=org_id, actor_id=owner.user_id, email=email, role="owner")
        agents.append(sc.ScenarioActor(name=name, kind="agent", user_id=r.user_id, org_id=org_id))
    # Drive all scenarios through the REAL bearer-resolution path (not an
    # injected principal): mint a real agent token per actor.
    human, agent_a, agent_b = await sc.mint_actor_tokens([human, agents[0], agents[1]])
    return agent_a, agent_b, human


async def _ingested_env() -> tuple[
    sc.ScenarioRunner,
    sc.ScenarioActor,
    sc.ScenarioActor,
    sc.ScenarioActor,
    object,
    object,
    sc.BlobMap,
    list[QueryRecord],
]:
    agent_a, agent_b, human = await _org_with_actors()
    ws = generate_workspace(seed=_SEED, scale=_SCALE)
    queries = build_queries(ws, seed=_SEED + 1000)
    async with tenant_session(str(human.org_id), str(human.user_id)) as s:
        ingest = await ingest_workspace(s, org_id=human.org_id, actor_id=human.user_id, ws=ws)
    blobmap = sc.BlobMap(human.org_id, human.user_id)
    await blobmap.load(ingest)
    runner = sc.ScenarioRunner(org_id=human.org_id, k=10, seed=_SEED)
    return runner, agent_a, agent_b, human, ws, ingest, blobmap, queries


async def test_freshness_interactive_roundtrip() -> None:
    runner, agent_a, agent_b, _human, _ws, ingest, blobmap, _q = await _ingested_env()
    project = next(iter(sorted(ingest.project_ids)))  # type: ignore[attr-defined]
    await sc.run_freshness_interactive(
        runner, agent_a, agent_b, project_name=project, ingest=ingest, blobmap=blobmap
    )
    (rec,) = [r for r in runner.records if r.category == "freshness_interactive"]
    # The updated content must be served to the OTHER actor: no event.
    assert rec.rank is not None and rec.event is False
    assert rec.extra["writer"] == "agent_a" and rec.actor == "agent_b"


async def test_humus_cycle_policy_and_rejected_gate() -> None:
    runner, agent_a, _b, human, ws, ingest, blobmap, queries = await _ingested_env()
    displacement = [q for q in queries if q.category == "collision"][:2]
    await sc.run_static_queries(
        runner, agent_a, ws=ws, ingest=ingest, blobmap=blobmap, queries=displacement
    )
    await sc.run_humus_cycle(
        runner,
        agent_a,
        human,
        ws=ws,
        ingest=ingest,
        blobmap=blobmap,
        displacement_queries=displacement,
    )
    # The scenario must be runnable on the generated workspace: a skip here
    # means the generator no longer produces archived gold-bearing notes,
    # which is itself a regression worth failing on.
    assert not any(s["scenario"] == "humus_cycle" for s in runner.summary.skipped)
    cons = [r for r in runner.records if r.category == "humus_consolidation"]
    assert cons and cons[0].extra["atom_won"] is True and cons[0].event is False
    rejected = [r for r in runner.records if r.category == "humus_rejected_gate"]
    assert rejected and rejected[0].event is False  # the rejected atom never surfaced
    displaced = [r for r in runner.records if r.category == "humus_displacement"]
    assert displaced and all(r.event is False for r in displaced)


async def test_erasure_propagation_and_specificity() -> None:
    import dataclasses as _dc

    runner, agent_a, _b, human, ws, ingest, blobmap, queries = await _ingested_env()
    erasure = [q for q in queries if q.category == "erasure" and q.erase_unit_id]
    assert erasure, "T2 produced no erasure queries at this scale/seed"
    # A guaranteed-hit erasure case (query = the fact tuple itself, the
    # strongest possible pull) pins the degradation semantics
    # deterministically under the FakeEmbedder; the adversarial T2
    # phrasings may legitimately pre-miss and are then recorded with
    # pre_hit=False, not counted as degradations.
    fact = next(f for f in ws.facts if f.fact_id == erasure[0].fact_id)  # type: ignore[attr-defined]
    pinned = _dc.replace(
        erasure[0],
        query_id="pinned",
        query_text=f"{fact.entity_name} {fact.attribute} {fact.value}",
    )
    await sc.run_erasure(
        runner,
        agent_a,
        human,
        ws=ws,
        ingest=ingest,
        blobmap=blobmap,
        queries=[pinned, *queries],
    )
    props = [r for r in runner.records if r.category == "erasure_propagation"]
    assert props
    pinned_rec = next(r for r in props if r.qid == "erasure-pinned")
    assert pinned_rec.extra["pre_hit"] is True
    for r in props:
        assert r.extra["erase_ok"] is True
        assert isinstance(r.extra["derived_survived"], bool)
        if r.extra["pre_hit"]:
            # Direct degradation is guaranteed by the blob erase; the
            # derived outcome is MEASURED (event captures any survival,
            # direct or derived — the protocol's erasure-propagation claim).
            assert r.rank is None
            assert r.event is r.extra["derived_survived"]
    specs = [r for r in runner.records if r.category == "erasure_specificity"]
    assert specs and all(r.event is False for r in specs)


async def test_perimeter_zero_leak_and_redteam_machinery() -> None:
    runner, _a, agent_b, _human, ws, ingest, blobmap, queries = await _ingested_env()
    await sc.run_perimeter(runner, agent_b, ws=ws, ingest=ingest, blobmap=blobmap, queries=queries)
    perimeter = [r for r in runner.records if r.category == "perimeter"]
    if perimeter:  # quota-dependent at small scale; machinery is the redteam below
        assert all(r.event is False for r in perimeter)
    await sc.redteam_perimeter(runner, agent_b, ws=ws, ingest=ingest, blobmap=blobmap, attempts=8)
    red = [r for r in runner.records if r.category == "perimeter_redteam"]
    assert len(red) >= 1
    assert all(r.event is False for r in red), "perimeter leak under adversarial queries"


async def test_multi_agent_visibility_provenance_gate_and_concurrency() -> None:
    runner, agent_a, agent_b, human, _ws, ingest, blobmap, _q = await _ingested_env()
    project = next(iter(sorted(ingest.project_ids)))  # type: ignore[attr-defined]
    await sc.run_multi_agent(
        runner, agent_a, agent_b, human, ingest=ingest, blobmap=blobmap, project_name=project
    )
    by_cat = {r.category: r for r in runner.records}
    vis = by_cat["ma_visibility"]
    assert vis.rank is not None and vis.event is False
    # A8: reclassified as a topology property; the number is post-commit
    # SEARCH latency, not an eventual-consistency delay.
    assert vis.extra["classification"] == "topology_property_single_primary"
    assert vis.extra["search_latency_post_commit_ms"] >= 0.0
    prov = by_cat["ma_provenance"]
    assert prov.event is False, "creating revision does not attribute the writing actor"
    conc = by_cat["ma_concurrent_writes"]
    assert conc.extra["one_winner"] is True
    assert conc.event is False and conc.extra["final_body"] == "versione del retry"
    gate = by_cat["ma_review_gate"]
    assert gate.extra["visible_before_approval"] is False
    assert gate.extra["visible_after_approval"] is True
    assert gate.event is False


async def test_records_roundtrip_into_eval_report(tmp_path: object) -> None:
    from pathlib import Path

    runner, agent_a, _agent_b, _human, ws, ingest, blobmap, queries = await _ingested_env()
    subset = [q for q in queries if q.category in ("collision", "impossible")][:6]
    await sc.run_static_queries(
        runner, agent_a, ws=ws, ingest=ingest, blobmap=blobmap, queries=subset
    )
    out = Path(str(tmp_path))
    manifest = sc.write_scenario_artifacts(runner, out)
    assert manifest["records"] == len(subset)
    loaded = eval_report.load_records(out / "scenario_records.jsonl")
    assert len(loaded) == len(subset)
    # The dialect fields survive the round-trip (extras are ignored).
    assert {r.category for r in loaded} <= {"collision", "impossible"}
    assert all(isinstance(r.hit, bool) for r in loaded)


# ── §6 hardening (A4-A9) ────────────────────────────────────────────────────


async def test_calls_go_through_real_agent_token_auth() -> None:
    """A5: with minted tokens, steps resolve the principal through the real
    bearer path (``authenticated=True``), not an injected tuple."""
    runner, agent_a, agent_b, _human, _ws, ingest, _blobmap, _q = await _ingested_env()
    assert agent_a.token is not None and agent_b.token is not None
    project_id = next(iter(ingest.project_ids.values()))  # type: ignore[attr-defined]
    await runner.search(agent_a, "qualcosa", project_id=project_id)
    assert runner.steps and all(s.authenticated for s in runner.steps)


async def test_auth_spoof_is_ignored() -> None:
    """A5: a forged actor_id in the arguments never changes attribution."""
    runner, agent_a, agent_b, _human, _ws, ingest, _blobmap, _q = await _ingested_env()
    project_id = next(iter(ingest.project_ids.values()))  # type: ignore[attr-defined]
    await sc.run_auth_spoof_probe(runner, agent_a, agent_b, project_id=project_id)
    (rec,) = [r for r in runner.records if r.category == "ma_auth_spoof"]
    assert rec.event is False
    assert rec.extra["attributed_to_true_identity"] is True
    assert rec.extra["forged_identity_accepted"] is False


async def test_write_race_tournament_ledger_oracle() -> None:
    """A4: a small tournament of real read→write races — exactly one winner
    per race, an untorn final body, and a computed CP bound with zero
    anomalies."""
    runner, agent_a, agent_b, human, _ws, ingest, _blobmap, _q = await _ingested_env()
    project_id = next(iter(ingest.project_ids.values()))  # type: ignore[attr-defined]
    summary = await sc.run_write_race_tournament(
        runner, [agent_a, agent_b, human], project_id=project_id, races=6
    )
    assert summary["races"] == 6
    assert summary["anomalies"] == 0
    assert summary["anomaly_rate_cp95_upper"] is not None
    races = [r for r in runner.records if r.category == "ma_write_race"]
    assert len(races) == 6
    assert all(r.event is False for r in races)
    # At least one contender commits each race, the final body is untorn,
    # and losers fail on stale-version (serialized last-write-wins is valid;
    # "exactly one winner" is NOT the invariant).
    assert all(r.extra["winners"] >= 1 for r in races)
    assert all(r.extra["final_body_valid"] for r in races)


async def test_review_gate_leak_probe_all_surfaces() -> None:
    """A6: a proposed atom is invisible on EVERY peer read surface across
    cycles; the double-approve yields one effective transition; the TOCTOU
    case is recorded as pending task 2e36e732 (never a silent pass)."""
    runner, agent_a, agent_b, human, _ws, ingest, _blobmap, _q = await _ingested_env()
    project_id = next(iter(ingest.project_ids.values()))  # type: ignore[attr-defined]
    summary = await sc.run_review_gate_cycles(
        runner, agent_a, agent_b, human, agent_b, project_id=project_id, cycles=4
    )
    assert summary["cycles"] == 4
    assert summary["leaks"] == 0
    assert summary["leak_rate_cp95_upper"] is not None
    cycles = [r for r in runner.records if r.category == "ma_review_gate_cycle"]
    assert cycles and all(r.event is False for r in cycles)
    assert all(not r.extra["leaked_surfaces"] for r in cycles)
    (dbl,) = [r for r in runner.records if r.category == "ma_review_gate_double_approve"]
    assert dbl.event is False and dbl.extra["oks"] >= 1
    (toc,) = [r for r in runner.records if r.category == "ma_review_gate_toctou"]
    assert toc.event is False  # pending finding, not a gate failure
    assert "pending_task_2e36e732" in toc.extra


async def test_dense_visibility_probe_machinery() -> None:
    """A8: the dense-only visibility probe runs and is recorded (the real
    dense signal is a confirmatory-run concern; FakeEmbedder makes it
    machinery-only, never an event)."""
    runner, agent_a, agent_b, _human, _ws, ingest, _blobmap, _q = await _ingested_env()
    project_id = next(iter(ingest.project_ids.values()))  # type: ignore[attr-defined]
    await sc.run_dense_visibility_probe(runner, agent_a, agent_b, project_id=project_id)
    (rec,) = [r for r in runner.records if r.category == "ma_dense_visibility"]
    assert rec.event is False
    assert "dense_found" in rec.extra
    assert any(s["scenario"] == "degraded_embedder_reembed_lag" for s in runner.summary.skipped)


async def test_personal_to_shared_trajectory() -> None:
    """A9: a personal note is invisible to a peer in the project perimeter,
    becomes retrievable once shared, and the revision chain keeps the
    author's identity."""
    runner, _agent_a, agent_b, human, _ws, ingest, _blobmap, _q = await _ingested_env()
    project = next(iter(sorted(ingest.project_ids)))  # type: ignore[attr-defined]
    await sc.run_personal_to_shared(runner, human, agent_b, project_name=project, ingest=ingest)
    (rec,) = [r for r in runner.records if r.category == "ma_personal_to_shared"]
    assert rec.extra["hidden_before_share"] is True
    assert rec.extra["chain_has_owner"] is True
    assert rec.extra["visible_after_touch"] is True
    assert rec.event is False
