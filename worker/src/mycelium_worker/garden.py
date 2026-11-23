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
import datetime
import logging
import uuid

from sqlalchemy import select

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.organization import Organization
from mycelium_core.services import (
    autonomous_budget,
    coactivity,
    edge_usage,
    garden_classify,
    garden_health,
    garden_learning,
    graph_snapshot,
    kg,
    memory,
    note_links,
)

_log = logging.getLogger("mycelium.worker.garden")


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


async def _kg_extraction_pass(org_id: uuid.UUID, owner: uuid.UUID) -> None:
    """One budgeted pass of autonomous KG extraction for one workspace.

    Every note is read and recorded in its OWN transaction: the model is paid
    when it answers, so a later failure in the pass must not roll back the
    record of a read already paid for, which the next pass would then pay
    for again. Running out of credits ends the pass; the notes not reached
    stay pending for the next one."""
    settings = get_settings()
    settled_before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=settings.kg_extract_settle_minutes
    )
    async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
        pending = await kg.pending_reads(
            s,
            org_id=org_id,
            settled_before=settled_before,
            max_attempts=settings.kg_extract_max_attempts,
            token_budget=settings.kg_extract_tokens_per_pass,
            # Enough rows to fill one budget with small notes; the plan, not
            # this bound, decides what is read.
            scan=500,
        )
    plan = kg.plan_pass(pending, token_budget=settings.kg_extract_tokens_per_pass)
    for big in plan.too_large:
        async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
            await kg.record_too_large(s, org_id=org_id, pending=big)
    outcomes: dict[str, int] = {}
    for item in plan.read:
        try:
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                outcome = await kg.read_autonomously(s, org_id=org_id, actor_id=owner, pending=item)
        except DomainError as exc:
            if exc.code == MessageCode.INSUFFICIENT_CREDITS:
                _log.info("kg extraction pass stopped org=%s: out of credits", org_id)
                break
            raise
        if outcome is not None:  # None: another pass holds or did this read
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    if outcomes or plan.too_large:
        _log.info(
            "kg extraction pass org=%s pending=%d outcomes=%s too_large=%d",
            org_id,
            len(pending),
            outcomes,
            len(plan.too_large),
        )


async def _run_link_proposers(org_id: uuid.UUID, owner: uuid.UUID) -> None:
    from mycelium_core.services import link_proposals, link_proposers

    settings = get_settings()
    runs = []
    if settings.link_proposer_link_add_enabled:
        runs.append(link_proposers.run_link_add)
    if settings.link_proposer_unlinked_mention_enabled:
        runs.append(link_proposers.run_unlinked_mention)
    for run in runs:
        try:
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                report = await run(s, org_id=org_id)
            if report.submitted or report.skipped:
                _log.info(
                    "link proposer %s org=%s submitted=%d skipped=%s",
                    report.proposer,
                    org_id,
                    report.submitted,
                    report.skipped,
                )
        except Exception:
            _log.exception("link proposer failed for org=%s", org_id)
    try:
        async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
            expired = await link_proposals.expire(s, org_id=org_id)
        if expired:
            _log.info("link proposals expired org=%s n=%d", org_id, expired)
    except Exception:
        _log.exception("link proposal expiry failed for org=%s", org_id)


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
            # Link proposals (ADR-0073 decision 7): the built-in proposers
            # submit what a person will decide, and undecided proposals past
            # their time expire as such. Own session and try, like the
            # snapshots: a proposer failing must not roll back anything above.
            await _run_link_proposers(org_id, owner)
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
            # Search-informed edge-usage materialisation (Fase 2, task
            # 561c6aca): folds the windowed retrieval traces into
            # note_edge_usage, the FOURTH soft-OR source. Same ordering
            # constraint as co-activity (before the graph snapshot, whose
            # signature folds in the usage fingerprint) and the same
            # failure-isolation / sub-flag posture.
            if get_settings().garden_edge_usage_enabled:
                try:
                    async with tenant_session(str(org_id), str(owner), actor_kind="system") as eus:
                        n_pairs = await edge_usage.refresh_edge_usage(eus, org_id=org_id)
                        if n_pairs:
                            _log.info("edge-usage refresh org=%s pairs=%d", org_id, n_pairs)
                except Exception:
                    _log.exception("edge-usage refresh failed for org=%s", org_id)
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
            # On-create classification drain (task b8c60940 / WS-D2, ADR-0042
            # D5): classify the nodes enqueued at creation + cache the
            # suggestions (precomputed_suggestions). Own session/try; sub-
            # flagged so a deployment opts in independently. Runs AFTER the
            # graph snapshot refresh above so classify reads fresh clusters.
            if get_settings().garden_autoclassify_on_creation_enabled:
                try:
                    async with tenant_session(str(org_id), str(owner), actor_kind="system") as js:
                        await garden_classify.process_classification_jobs(js, org_id=org_id)
                except Exception:
                    _log.exception("classification job drain failed for org=%s", org_id)
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
            # Autonomous knowledge-graph extraction (ADR-0071). After every
            # other step, because it is the one that spends on a model: a
            # pass that runs out of credits must not cost the maturity sweep
            # or the snapshots above. Sub-flagged, off by default.
            if get_settings().kg_autonomous_extract_enabled:
                try:
                    await _kg_extraction_pass(org_id, owner)
                except Exception:
                    _log.exception("kg extraction pass failed for org=%s", org_id)
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
