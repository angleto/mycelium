"""Offline graph-analytics materialisation (task d8664631, Phase 2 of
8c0a8f08).

The worker's garden tick calls :func:`refresh_graph_snapshot` per org:
a cheap input signature (note / link / note-tag counts + the latest
link timestamp) decides whether the stored analytics are still valid;
only a changed graph pays the recomputation (PageRank + Leiden +
betweenness — the last one is the whole reason this runs offline:
Brandes is O(V·E), too slow for the request path on a grown garden).

The live ``/garden/graph`` and ``/garden/clusters`` endpoints keep
computing the cheap analytics on demand so an edit is reflected
immediately; they read only ``betweenness`` from here. When
latency/volume eventually demand it, flipping them to serve the whole
snapshot (signature check -> stored row) is a small change on this
already-populated table.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.models.garden_graph_snapshot import GardenGraphSnapshot
from mycelium_core.models.note import Note
from mycelium_core.models.note_coactivity import NoteCoactivity
from mycelium_core.models.note_link import NoteNoteLink, NoteTaskLink
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.task import Task
from mycelium_core.models.task_relation import TaskRelation
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.services import graph as graph_svc

_log = logging.getLogger("mycelium.graph_snapshot")


async def graph_signature(
    session: AsyncSession, *, org_id: uuid.UUID, include_tasks: bool = False
) -> str:
    """Cheap fingerprint of everything the analytics derive from: the
    note set, the typed link set, the note↔tag assignments and the
    co-activity edges. Count + latest-link-timestamp catches every
    add/remove (a delete changes the count, an add changes both).
    Note-body edits don't change the graph and correctly don't change the
    signature.

    Co-activity feeds ``compute_note_edge_weights`` (a third soft-OR
    source), so it must be in the fingerprint or the materialised
    centrality/betweenness/Leiden would ignore fresh co-activity edges
    until some unrelated note/link/tag change happened to bump the
    signature. The fingerprint uses the row count + session-count sum +
    latest co-active timestamp — all content, none of them the
    per-recompute ``computed_at`` — so a no-op re-materialise of the same
    window leaves the signature (and thus the snapshot) untouched.

    When ``include_tasks`` (ADR-0042 D1) the snapshot's centrality /
    clusters / betweenness span notes + tasks, so the signature folds in
    the task node + task-edge + task-tag counts; otherwise a task change
    would not invalidate the stored unified analytics. With it off the
    suffix is omitted and the signature is byte-identical to the notes-only
    fingerprint (no spurious recompute on an existing snapshot). The caller
    (``refresh_graph_snapshot``) passes the fleet flag in.
    """
    notes = (
        await session.execute(select(func.count()).select_from(Note).where(Note.org_id == org_id))
    ).scalar_one()
    links, max_link_ts = (
        await session.execute(
            select(func.count(), func.max(NoteNoteLink.created_at)).where(
                NoteNoteLink.org_id == org_id
            )
        )
    ).one()
    note_tags = (
        await session.execute(
            select(func.count()).select_from(NoteTag).where(NoteTag.org_id == org_id)
        )
    ).scalar_one()
    coact_n, coact_sum, max_coact_ts = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(NoteCoactivity.session_count), 0),
                func.max(NoteCoactivity.last_coactive_at),
            ).where(NoteCoactivity.org_id == org_id)
        )
    ).one()
    ts = max_link_ts.isoformat() if max_link_ts is not None else "-"
    cts = max_coact_ts.isoformat() if max_coact_ts is not None else "-"
    sig = f"n{notes}:l{links}:t{note_tags}:{ts}:c{coact_n}/{coact_sum}/{cts}"
    if not include_tasks:
        return sig
    tasks_n = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.org_id == org_id, Task.deleted_at.is_(None))
        )
    ).scalar_one()
    rels = (
        await session.execute(
            select(func.count()).select_from(TaskRelation).where(TaskRelation.org_id == org_id)
        )
    ).scalar_one()
    ntl = (
        await session.execute(
            select(func.count()).select_from(NoteTaskLink).where(NoteTaskLink.org_id == org_id)
        )
    ).scalar_one()
    task_tags = (
        await session.execute(
            select(func.count()).select_from(TaskTag).where(TaskTag.org_id == org_id)
        )
    ).scalar_one()
    return f"{sig}:T{tasks_n}/R{rels}/NT{ntl}/TT{task_tags}"


async def get_graph_snapshot(
    session: AsyncSession, *, org_id: uuid.UUID
) -> GardenGraphSnapshot | None:
    return (
        await session.execute(
            select(GardenGraphSnapshot).where(GardenGraphSnapshot.org_id == org_id)
        )
    ).scalar_one_or_none()


async def refresh_graph_snapshot(
    session: AsyncSession, *, org_id: uuid.UUID, force: bool = False
) -> bool:
    """Recompute and upsert the org's analytics snapshot when the graph
    changed since the stored one (or ``force``). Returns True when a
    recomputation actually ran. Idempotent: same graph -> same row.

    This is one of the unified surfaces (ADR-0042 D1): it reads the fleet
    ``garden_unified_task_graph_enabled`` flag once and threads it into the
    signature and every analytic, so the stored centrality / clusters /
    betweenness span notes + tasks iff the workspace opted in. Flag off ->
    notes-only, byte-identical to before tasks were graph nodes."""
    include_tasks = get_settings().garden_unified_task_graph_enabled
    sig = await graph_signature(session, org_id=org_id, include_tasks=include_tasks)
    existing = await get_graph_snapshot(session, org_id=org_id)
    if not force and existing is not None and existing.signature == sig:
        return False
    centrality = await graph_svc.compute_pagerank(
        session, org_id=org_id, include_tasks=include_tasks
    )
    betweenness = await graph_svc.compute_betweenness(
        session, org_id=org_id, include_tasks=include_tasks
    )
    cluster_res = await graph_svc.compute_leiden_clusters(
        session, org_id=org_id, include_tasks=include_tasks
    )
    values = {
        "org_id": org_id,
        "signature": sig,
        "centrality": {str(k): v for k, v in centrality.items()},
        "betweenness": {str(k): v for k, v in betweenness.items()},
        "clusters": {str(k): v for k, v in cluster_res.clusters.items()},
        "modularity": cluster_res.modularity,
    }
    stmt = (
        pg_insert(GardenGraphSnapshot)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_garden_graph_snapshot_org",
            set_={
                "signature": sig,
                "centrality": values["centrality"],
                "betweenness": values["betweenness"],
                "clusters": values["clusters"],
                "modularity": values["modularity"],
                "computed_at": func.now(),
            },
        )
    )
    await session.execute(stmt)
    # The raw upsert bypasses the ORM identity map: expire any cached
    # instance so a same-session reader sees the fresh values.
    if existing is not None:
        session.expire(existing)
    _log.info(
        "graph snapshot refreshed org=%s notes=%d sig=%s",
        org_id,
        len(centrality),
        sig,
    )
    return True


__all__ = [
    "get_graph_snapshot",
    "graph_signature",
    "refresh_graph_snapshot",
]
