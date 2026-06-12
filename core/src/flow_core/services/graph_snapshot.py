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

from flow_core.models.garden_graph_snapshot import GardenGraphSnapshot
from flow_core.models.note import Note
from flow_core.models.note_link import NoteNoteLink
from flow_core.models.note_tag import NoteTag
from flow_core.services import graph as graph_svc

_log = logging.getLogger("flow.graph_snapshot")


async def graph_signature(session: AsyncSession, *, org_id: uuid.UUID) -> str:
    """Cheap fingerprint of everything the analytics derive from: the
    note set, the typed link set and the note↔tag assignments. Count +
    latest-link-timestamp catches every add/remove (a delete changes
    the count, an add changes both). Note-body edits don't change the
    graph and correctly don't change the signature."""
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
    ts = max_link_ts.isoformat() if max_link_ts is not None else "-"
    return f"n{notes}:l{links}:t{note_tags}:{ts}"


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
    recomputation actually ran. Idempotent: same graph -> same row."""
    sig = await graph_signature(session, org_id=org_id)
    existing = await get_graph_snapshot(session, org_id=org_id)
    if not force and existing is not None and existing.signature == sig:
        return False
    centrality = await graph_svc.compute_pagerank(session, org_id=org_id)
    betweenness = await graph_svc.compute_betweenness(session, org_id=org_id)
    cluster_res = await graph_svc.compute_leiden_clusters(session, org_id=org_id)
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
