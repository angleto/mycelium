"""Distillation-candidate surfacing (task 4995a32f).

The decomposition pipeline (``services.decomposition``) is entirely
on-demand: nothing scans the corpus to *propose* work. This module is the
missing "suggest, don't automate" half. It computes, read-only and with no
LLM call, WHAT could be distilled -- so a GUI badge and an MCP caller can
ask "are there distillations to do?" and then act with a strong model
(the caller's own via ``distilled_text``, or the org's hosted provider)
under the existing review + version safety net.

Distillation is graph MAINTENANCE, not only summarisation (Angelo,
2026-07-02): the memory is a searchable graph, so candidates come in two
families.

- NODE candidates -- compact inert nodes into denser ones:
    * ``distill``  : one inert note -> one atom.
    * ``pattern``  : a Leiden community of >=2 archived notes -> one
                     cross-note pattern atom.
    * ``season``   : a quarter's archived notes -> one seasonal atom.
- EDGE candidates -- curate the link structure:
    * ``link_add``   : a note pair with strong tag/co-activity evidence
                       but NO manual link (Adamic-Adar link prediction,
                       reusing ``graph.compute_note_edge_weights``).
    * ``link_prune`` : an existing ``related`` link whose connective
                       basis has decayed (no shared generic tags, no
                       co-activity). Conservative: never touches
                       ``hypha_of``/``supersedes``/``contradicts`` (they
                       carry provenance / decisions).

Everything runs in a tenant session (RLS scopes to the org). ``project_id``
narrows to one project (the note<->project-tag junction); ``None`` is
org-wide (NOT ``IS NULL`` -- notes have no project column). Idempotency is
respected: a candidate already realised (a distilled note, an existing
pattern/season signature, an existing link) is never proposed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.models.note import Note, NoteMaturity
from mycelium_core.models.note_coactivity import NoteCoactivity
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.services import graph as graph_svc
from mycelium_core.services import graph_snapshot as snap_svc
from mycelium_core.services import note_inert
from mycelium_core.services.decomposition import _MAX_PATTERN_SOURCES, _existing_humus
from mycelium_core.services.graph import _note_generic_tags, _pair_key

# Bound the corpus scan: we rank and slice to ``limit`` anyway, so an org
# with thousands of inert notes never materialises them all.
_SCAN_CAP = 2000

CandidateKind = str  # "all" | "distill" | "pattern" | "season" | "link_add" | "link_prune"


@dataclass(frozen=True)
class _NodeCandidate:
    kind: str  # "distill" | "pattern" | "season"
    note_ids: list[uuid.UUID]
    title: str
    reason: str
    score: float
    preview: str


@dataclass(frozen=True)
class _EdgeCandidate:
    op: str  # "add" | "prune"
    src_note_id: uuid.UUID
    dst_note_id: uuid.UUID
    link_kind: str
    src_title: str
    dst_title: str
    reason: str
    score: float


def _pattern_signature(note_ids: list[uuid.UUID]) -> str:
    """The SAME scheme ``decomposition.extract_cluster_pattern`` uses: the
    sources sorted by ``str(id)``, joined with commas, sha256, first 32
    hex chars. Matching it exactly is what lets us exclude a cluster whose
    pattern already exists."""
    ordered = sorted(str(n) for n in note_ids)
    return hashlib.sha256(",".join(ordered).encode("utf-8")).hexdigest()[:32]


async def _project_note_ids(
    session: AsyncSession, *, org_id: uuid.UUID, project_id: uuid.UUID
) -> set[uuid.UUID]:
    """Note ids carrying the project-kind tag (the note<->project junction,
    mirroring ``notes`` list filtering)."""
    rows = (
        await session.execute(
            select(NoteTag.note_id).where(NoteTag.org_id == org_id, NoteTag.tag_id == project_id)
        )
    ).all()
    return {r[0] for r in rows}


async def list_distillation_candidates(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    kind: CandidateKind = "all",
    limit: int = 50,
) -> dict[str, list[dict[str, object]]]:
    """Compute distillation candidates (nodes + edges). Pure read, no LLM.

    ``kind`` selects a family (default ``"all"``). ``limit`` caps EACH
    output list. Returns ``{"nodes": [...], "edges": [...]}`` with the
    fields the GUI/MCP contract pins.
    """
    want_distill = kind in ("all", "distill")
    want_pattern = kind in ("all", "pattern")
    want_season = kind in ("all", "season")
    want_link_add = kind in ("all", "link_add")
    want_link_prune = kind in ("all", "link_prune")

    project_ids: set[uuid.UUID] | None = None
    if project_id is not None:
        project_ids = await _project_note_ids(session, org_id=org_id, project_id=project_id)
        if not project_ids:
            return {"nodes": [], "edges": []}

    nodes: list[_NodeCandidate] = []
    edges: list[_EdgeCandidate] = []

    # A single snapshot read powers centrality ranking and pattern clusters.
    snapshot = await snap_svc.get_graph_snapshot(session, org_id=org_id)
    centrality: dict[str, float] = {}
    clusters: dict[str, int] = {}
    if snapshot is not None:
        centrality = {str(k): float(v) for k, v in (snapshot.centrality or {}).items()}
        clusters = {str(k): int(v) for k, v in (snapshot.clusters or {}).items()}

    now = dt.datetime.now(dt.UTC)

    # ---- inert notes (distill eligibility) -------------------------------
    inert_notes: list[Note] = []
    if want_distill or want_pattern:
        quiet_cutoff = now - dt.timedelta(days=note_inert.QUIET_WINDOW_DAYS)
        child = aliased(Note)
        already_distilled = (
            select(NoteNoteLink.id)
            .join(child, child.id == NoteNoteLink.child_note_id)
            .where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.parent_note_id == Note.id,
                NoteNoteLink.kind == "hypha_of",
                child.humus_kind == "distillation",
            )
            .exists()
        )
        stmt = (
            select(Note)
            .where(
                Note.org_id == org_id,
                Note.deleted_at.is_(None),
                Note.humus_flag.is_(False),
                or_(
                    Note.is_archived.is_(True),
                    Note.maturity == NoteMaturity.dormant.value,
                ),
                Note.updated_at < quiet_cutoff,
                ~note_inert.open_work_exists(Note.id),
                ~already_distilled,
            )
            .limit(_SCAN_CAP)
        )
        for n in (await session.execute(stmt)).scalars().all():
            if project_ids is not None and n.id not in project_ids:
                continue
            inert_notes.append(n)

    # ---- NODE / distill --------------------------------------------------
    if want_distill:

        def _rank(n: Note) -> tuple[float, float]:
            # centrality desc, then older-updated first (more stale -> riper)
            c = centrality.get(str(n.id), 0.0)
            age = -(n.updated_at.timestamp() if n.updated_at else 0.0)
            return (c, age)

        for n in sorted(inert_notes, key=_rank, reverse=True)[:limit]:
            nodes.append(
                _NodeCandidate(
                    kind="distill",
                    note_ids=[n.id],
                    title=(n.title or "senza titolo"),
                    reason="nota inerte non ancora distillata",
                    score=round(centrality.get(str(n.id), 0.0), 6),
                    preview=(n.title or "senza titolo")[:200],
                )
            )

    # ---- NODE / pattern (Leiden community of >=2 ARCHIVED notes) ---------
    # ``extract_cluster_pattern`` accepts ONLY ``is_archived`` sources and
    # signs the sorted-then-truncated (<=_MAX_PATTERN_SOURCES) id list. Mirror
    # BOTH here: build the candidate from the archived subset (a dormant-only
    # member would be dropped at extraction, shrinking the set below 2 or
    # shifting the signature), sort+truncate identically -- otherwise a
    # pattern that was already extracted is re-proposed forever.
    if want_pattern and clusters:
        archived_ids = {n.id for n in inert_notes if n.is_archived}
        title_by_id = {n.id: (n.title or "senza titolo") for n in inert_notes}
        by_community: dict[int, list[uuid.UUID]] = defaultdict(list)
        for nid_str, community in clusters.items():
            try:
                nid = uuid.UUID(nid_str)
            except ValueError:
                continue
            if nid in archived_ids:
                by_community[community].append(nid)
        pattern_rows: list[_NodeCandidate] = []
        for members in by_community.values():
            ordered = sorted(members, key=str)[:_MAX_PATTERN_SOURCES]
            if len(ordered) < 2:
                continue
            signature = _pattern_signature(ordered)
            if await _existing_humus(session, org_id=org_id, kind="pattern", signature=signature):
                continue
            titles = ", ".join(title_by_id.get(m, "?") for m in ordered[:4])
            pattern_rows.append(
                _NodeCandidate(
                    kind="pattern",
                    note_ids=ordered,
                    title=f"Pattern · {len(ordered)} note",
                    reason=(
                        f"cluster di {len(ordered)} note archiviate affini, "
                        "pattern non ancora estratto"
                    ),
                    score=float(len(ordered)),
                    preview=titles[:200],
                )
            )
        pattern_rows.sort(key=lambda c: c.score, reverse=True)
        nodes.extend(pattern_rows[:limit])

    # ---- NODE / season (quarter with >=1 archived note) ------------------
    if want_season:
        arch_stmt = select(Note.id, Note.created_at).where(
            Note.org_id == org_id,
            Note.deleted_at.is_(None),
            Note.is_archived.is_(True),
            Note.humus_flag.is_(False),
        )
        by_window: dict[tuple[int, int], list[uuid.UUID]] = defaultdict(list)
        for nid, created_at in (await session.execute(arch_stmt)).all():
            if project_ids is not None and nid not in project_ids:
                continue
            if created_at is None:
                continue
            q = (created_at.month - 1) // 3 + 1
            by_window[(created_at.year, q)].append(nid)
        season_rows: list[_NodeCandidate] = []
        for (year, quarter), ids in by_window.items():
            signature = f"{year}Q{quarter}"
            if await _existing_humus(session, org_id=org_id, kind="season", signature=signature):
                continue
            season_rows.append(
                _NodeCandidate(
                    kind="season",
                    note_ids=ids[:limit],
                    title=f"Season · {year} Q{quarter}",
                    reason=(
                        f"{len(ids)} note archiviate in {year}Q{quarter}, "
                        "stagione non ancora sintetizzata"
                    ),
                    score=float(len(ids)),
                    preview=f"{len(ids)} note archiviate nel trimestre {year}Q{quarter}",
                )
            )
        # newest quarter first
        season_rows.sort(key=lambda c: c.title, reverse=True)
        nodes.extend(season_rows[:limit])

    # ---- EDGE candidates (need the weave + existing links) ---------------
    if want_link_add or want_link_prune:
        linked_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
        related_pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
        link_rows = (
            await session.execute(
                select(
                    NoteNoteLink.parent_note_id,
                    NoteNoteLink.child_note_id,
                    NoteNoteLink.kind,
                ).where(NoteNoteLink.org_id == org_id)
            )
        ).all()
        for parent_id, child_id, link_kind in link_rows:
            linked_pairs.add(_pair_key(parent_id, child_id))
            if link_kind == "related":
                related_pairs.append(_pair_key(parent_id, child_id))

        # ---- EDGE / link_add (Adamic-Adar/co-activity, no manual link) ---
        if want_link_add:
            weave = await graph_svc.compute_note_edge_weights(session, org_id=org_id)
            add_rows: list[_EdgeCandidate] = []
            for e in weave:
                pk = _pair_key(e.src, e.dst)
                if pk in linked_pairs:
                    continue
                if project_ids is not None and (
                    pk[0] not in project_ids or pk[1] not in project_ids
                ):
                    continue
                add_rows.append(
                    _EdgeCandidate(
                        op="add",
                        src_note_id=pk[0],
                        dst_note_id=pk[1],
                        link_kind="related",
                        src_title="",
                        dst_title="",
                        reason="forte affinità (tag/co-attività) senza alcun link",
                        score=round(float(e.weight), 6),
                    )
                )
            # weave is already weight-desc; keep that order
            edges.extend(add_rows[:limit])

        # ---- EDGE / link_prune (related link, basis decayed) -------------
        if want_link_prune and related_pairs:
            note_tags = await _note_generic_tags(session, org_id=org_id)
            coact_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
            for a, b, cnt in (
                await session.execute(
                    select(
                        NoteCoactivity.note_a_id,
                        NoteCoactivity.note_b_id,
                        NoteCoactivity.session_count,
                    ).where(NoteCoactivity.org_id == org_id)
                )
            ).all():
                if cnt and cnt > 0:
                    coact_pairs.add(_pair_key(a, b))
            prune_rows: list[_EdgeCandidate] = []
            for pk in related_pairs:
                if project_ids is not None and (
                    pk[0] not in project_ids or pk[1] not in project_ids
                ):
                    continue
                shared = note_tags.get(pk[0], set()) & note_tags.get(pk[1], set())
                if shared or pk in coact_pairs:
                    continue
                prune_rows.append(
                    _EdgeCandidate(
                        op="prune",
                        src_note_id=pk[0],
                        dst_note_id=pk[1],
                        link_kind="related",
                        src_title="",
                        dst_title="",
                        reason="link 'related' senza più tag condivisi né co-attività",
                        score=1.0,
                    )
                )
            edges.extend(prune_rows[:limit])

    # ---- fill titles for every note referenced (one batched query) -------
    needed: set[uuid.UUID] = set()
    for ec in edges:
        needed.add(ec.src_note_id)
        needed.add(ec.dst_note_id)
    title_map: dict[uuid.UUID, str] = {}
    if needed:
        for nid, title in (
            await session.execute(select(Note.id, Note.title).where(Note.id.in_(needed)))
        ).all():
            title_map[nid] = title or "senza titolo"

    node_payload = [
        {
            "kind": c.kind,
            "note_ids": [str(x) for x in c.note_ids],
            "title": c.title,
            "reason": c.reason,
            "score": c.score,
            "preview": c.preview,
        }
        for c in nodes
    ]
    edge_payload = [
        {
            "op": e.op,
            "src_note_id": str(e.src_note_id),
            "dst_note_id": str(e.dst_note_id),
            "link_kind": e.link_kind,
            "src_title": title_map.get(e.src_note_id, ""),
            "dst_title": title_map.get(e.dst_note_id, ""),
            "reason": e.reason,
            "score": e.score,
        }
        for e in edges
    ]
    return {"nodes": node_payload, "edges": edge_payload}
