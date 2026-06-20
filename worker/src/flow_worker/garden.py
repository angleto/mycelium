"""Garden seasonal-rule worker (docs/adr/0029 P1).

Periodic sweep that applies the automatic maturity transitions on
notes:

- ``seed`` -> ``growing`` when touched within ``seed_to_growing_days``
- ``growing`` / ``mature`` -> ``dormant`` when untouched for
  ``growing_to_dormant_days``
- ``dormant`` -> ``growing`` when touched again

Manual transitions (`services/note_links.set_maturity`) always win
over the worker. ``mature`` is never set automatically (the user
decides).

Each tick runs per workspace as the workspace owner, mirroring the
dispatch worker pattern (own session, own RLS, own audit). Failures
on a single workspace are isolated; the loop keeps going.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.models.membership import Membership, Role
from flow_core.models.organization import Organization
from flow_core.services import (
    autonomous_budget,
    coactivity,
    garden_classify,
    garden_health,
    garden_learning,
    graph_snapshot,
    memory,
    note_links,
)

_log = logging.getLogger("flow.worker.garden")


async def _all_workspaces() -> list[uuid.UUID]:
    async with admin_session() as s:
        rows = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
        return [o.id for o in sorted(rows, key=lambda o: str(o.id))]


async def _owner_of(org_id: uuid.UUID) -> uuid.UUID | None:
    async with admin_session() as s:
        rows = (
            (
                await s.execute(
                    select(Membership)
                    .where(Membership.org_id == org_id, Membership.role == Role.owner)
                    .order_by(Membership.created_at, Membership.user_id)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        return None
    return rows[0].user_id


async def run_once() -> int:
    """One sweep over all workspaces. Returns the total number of
    notes whose maturity changed in this tick (across all
    workspaces)."""
    total = 0
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("garden sweep: failed to list workspaces")
        return 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            # WS-F5: the per-workspace kill-switch / daily budget cap. A
            # paused workspace skips the metabolic work but still refreshes
            # the health snapshot, so the paused state stays observable in
            # the sensors dashboard.
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as bs:
                bstatus = await autonomous_budget.status(bs, org_id=org_id)
            if bstatus.paused:
                _log.info(
                    "garden sweep paused org=%s reason=%s spent=%s cap=%s",
                    org_id,
                    bstatus.reason,
                    bstatus.spent_today,
                    bstatus.cap,
                )
                try:
                    async with tenant_session(str(org_id), str(owner), actor_kind="system") as hs:
                        await garden_health.persist_snapshot(hs, org_id=org_id)
                except Exception:
                    _log.exception("garden health snapshot failed for org=%s", org_id)
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                counters = await note_links.tick_maturity_transitions(
                    s, org_id=org_id, actor_id=owner
                )
                # Value-axis auto-promotion growing -> mature (ADR-0032).
                # Reversible (label-only, audited + feedback event); the
                # global flag disables it outright.
                auto_matured = 0
                if get_settings().garden_auto_mature_enabled:
                    auto_matured = await garden_classify.auto_promote_mature(
                        s, org_id=org_id, actor_id=owner
                    )
            # Garden-health daily snapshot (ADR-0035), in its OWN session
            # so a sensor-query failure can never roll back the maturity
            # transitions committed above. Idempotent per (org, day).
            try:
                async with tenant_session(str(org_id), str(owner), actor_kind="system") as hs:
                    await garden_health.persist_snapshot(hs, org_id=org_id)
            except Exception:
                _log.exception("garden health snapshot failed for org=%s", org_id)
            # Co-activity edge materialisation (task f0a15247, ADR-0031
            # w_coact). Aggregates the activity log into pairwise session
            # counts in note_coactivity, the third soft-OR source of
            # compute_note_edge_weights. MUST run before the graph snapshot
            # below so the materialised centrality/betweenness/Leiden see
            # the fresh co-activity edges (the snapshot signature folds in
            # the co-activity fingerprint). Own session/try for the same
            # failure-isolation reason as the snapshots; sub-flagged so a
            # deployment can opt out independently.
            if get_settings().garden_coactivity_enabled:
                try:
                    async with tenant_session(str(org_id), str(owner), actor_kind="system") as cas:
                        n_pairs = await coactivity.refresh_coactivity(cas, org_id=org_id)
                        if n_pairs:
                            _log.info("coactivity refresh org=%s pairs=%d", org_id, n_pairs)
                except Exception:
                    _log.exception("coactivity refresh failed for org=%s", org_id)
            # Graph-analytics materialisation (task d8664631): PageRank +
            # Leiden + betweenness into garden_graph_snapshot. Signature-
            # gated, so an unchanged graph costs three COUNT queries; only
            # a real change pays the O(V·E) betweenness. Own session for
            # the same failure-isolation reason as the health snapshot.
            try:
                async with tenant_session(str(org_id), str(owner), actor_kind="system") as gs:
                    await graph_snapshot.refresh_graph_snapshot(gs, org_id=org_id)
            except Exception:
                _log.exception("graph snapshot refresh failed for org=%s", org_id)
            # Memory tier recompute (task 09007016 / WS-D4, ADR-0016: tier =
            # latency, not retention). Re-tiers blobs on access-decay, demoting
            # cold ones (never deletes; they stay queryable). Own session + try
            # for the same failure-isolation reason as the snapshots above, and
            # sub-flagged so a deployment opts in independently of the maturity
            # sweep.
            if get_settings().garden_tier_recompute_enabled:
                try:
                    async with tenant_session(str(org_id), str(owner), actor_kind="system") as ts:
                        await memory.recompute_tier(ts, org_id=org_id)
                except Exception:
                    _log.exception("memory tier recompute failed for org=%s", org_id)
            # Autonomous classify-on-ingest (task b8c60940 / WS-D2, ADR-0032
            # P4). Stamps not-yet-seen notes with the structural community the
            # graph snapshot just computed + an auto_classified_at marker, so
            # new nodes are classified proactively instead of waiting for a
            # human to open the panel. Read-only (no tag/link/maturity
            # auto-apply). Own session/try; sub-flagged. Runs AFTER the graph
            # snapshot refresh above so it reads the fresh clusters.
            if get_settings().garden_autoclassify_enabled:
                try:
                    async with tenant_session(str(org_id), str(owner), actor_kind="system") as cs:
                        await garden_classify.autoclassify_unprocessed(cs, org_id=org_id)
                except Exception:
                    _log.exception("autoclassify pass failed for org=%s", org_id)
            # Online-learning prior metabolism (task 49d24048, ADR-0037): decay
            # stale per-user classification priors + prune the neutral ones, so
            # old preferences fade. Own session/try (failure-isolated like the
            # snapshots above); sub-flagged so a deployment can opt out.
            if get_settings().garden_learning_decay_enabled:
                try:
                    async with tenant_session(str(org_id), str(owner), actor_kind="system") as ls:
                        decayed, pruned = await garden_learning.decay_priors(ls, org_id=org_id)
                        if decayed or pruned:
                            _log.info(
                                "garden learning decay org=%s decayed=%d pruned=%d",
                                org_id,
                                decayed,
                                pruned,
                            )
                except Exception:
                    _log.exception("learning prior decay failed for org=%s", org_id)
            # Daily prior snapshot (task ea2156df, ADR-0037 "Snapshots and
            # rollback"): checkpoint each user's priors so rollback is
            # decay-aware point-in-time and drift has a baseline. Runs AFTER
            # decay so the checkpoint is the actual post-decay live state;
            # daily-idempotent (skips users checkpointed in the last ~20h).
            # Own session/try, sub-flagged like the decay above.
            if get_settings().garden_learning_snapshot_enabled:
                try:
                    async with tenant_session(str(org_id), str(owner), actor_kind="system") as ss:
                        n_snap = await garden_learning.snapshot_priors(ss, org_id=org_id)
                        if n_snap:
                            _log.info("garden learning snapshot org=%s users=%d", org_id, n_snap)
                except Exception:
                    _log.exception("learning prior snapshot failed for org=%s", org_id)
            n = sum(counters.values()) + auto_matured
            if n > 0:
                _log.info(
                    "garden tick org=%s seed_to_growing=%d to_dormant=%d "
                    "dormant_to_growing=%d auto_matured=%d",
                    org_id,
                    counters["seed_to_growing"],
                    counters["to_dormant"],
                    counters["dormant_to_growing"],
                    auto_matured,
                )
                total += n
        except Exception:
            _log.exception("garden tick failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    """Periodic loop: ``run_once`` on a modest interval. Reuses the
    ``dispatch_loop_interval_seconds`` setting as a sensible default;
    can diverge in the future via a dedicated setting."""
    interval = max(60, get_settings().dispatch_loop_interval_seconds * 4)
    _log.info("garden loop worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
