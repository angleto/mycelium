"""Link prediction over the workspace note graph (task c7d0bb4c).

Phase 1: a server-side suggester that, given one note, returns the
top-K *candidate* notes the user might want to link to. The output
feeds the SPA's "suggested links" chips (and an MCP tool of the
same name): the user confirms or ignores; we **never** auto-create
the link.

Signal mix (Phase 1, no learned model yet):

- **Adamic-Adar** over shared generic tags (rare tags weigh more).
- **PPR co-visit**: personalised PageRank seeded at the source —
  candidates with more induced mass score higher (their semantic
  + structural neighbourhood overlaps the source's).
- **Degree damping**: a candidate with very high degree contributes
  less novelty (the system already nudges everyone toward popular
  notes; we want fresh signal).
- **Already-linked penalty**: pairs that already share a manual
  link in either direction are excluded.

Score = ``softor(aa, ppr_norm) * (1 - degree_norm * 0.3)``. The
soft-OR keeps the score in [0, 1]; the degree damping is a small
multiplicative penalty so a hub is never the *only* suggestion.

ADR-0033 anti-monoculture safeguards (diversity bonus, exploration
slot) live on top of this service; this Phase 1 implementation
returns the raw ranked list. Phase 2 will plug Node2Vec embedding +
GNN link prediction in place of the heuristic mix.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.note import Note
from flow_core.models.note_link import NoteNoteLink
from flow_core.models.note_tag import NoteTag
from flow_core.models.tag import Tag, TagKind
from flow_core.services import graph as graph_svc


@dataclass(frozen=True)
class LinkSuggestion:
    note_id: uuid.UUID
    score: float
    signals: dict[str, float]
    rationale: str


async def suggest_links_for_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    k: int = 5,
) -> list[LinkSuggestion]:
    """Return the top ``k`` candidate notes to link from ``note_id``.

    Excludes the source itself, every note already linked (either
    direction) and any note in another tenant (the underlying
    queries are RLS-scoped). Empty list when the workspace has
    fewer than 2 notes or the source has no signal.
    """
    note_rows = (
        await session.execute(
            select(Note.id).where(Note.org_id == org_id)
        )
    ).all()
    all_ids: set[uuid.UUID] = {r[0] for r in note_rows}
    if note_id not in all_ids or len(all_ids) < 2:
        return []
    # Already-linked exclusion set (undirected).
    linked_rows = (
        await session.execute(
            select(NoteNoteLink.parent_note_id, NoteNoteLink.child_note_id).where(
                NoteNoteLink.org_id == org_id
            )
        )
    ).all()
    excluded: set[uuid.UUID] = {note_id}
    for p, c in linked_rows:
        if p == note_id:
            excluded.add(c)
        elif c == note_id:
            excluded.add(p)
    # Generic-tag corpus + the source's tag set.
    tag_rows = (
        await session.execute(
            select(NoteTag.note_id, NoteTag.tag_id, Tag.kind)
            .join(Tag, Tag.id == NoteTag.tag_id)
            .where(NoteTag.org_id == org_id)
        )
    ).all()
    note_tags: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    tag_deg: dict[uuid.UUID, int] = defaultdict(int)
    for nid, tid, kind in tag_rows:
        if kind is TagKind.generic:
            note_tags[nid].add(tid)
            tag_deg[tid] += 1
    src_tags = note_tags.get(note_id, set())
    # PPR seeded at the source for the structural signal.
    ppr = await graph_svc.compute_personalized_pagerank(
        session, org_id=org_id, seed_ids=[note_id]
    )
    ppr_max = max(ppr.values()) if ppr else 1.0
    ppr_max = ppr_max if ppr_max > 0 else 1.0
    # Degree per node (manual links only) for the damping factor.
    degree: dict[uuid.UUID, int] = defaultdict(int)
    for p, c in linked_rows:
        if p != c:
            degree[p] += 1
            degree[c] += 1
    deg_max = max(degree.values()) if degree else 1
    deg_max = deg_max if deg_max > 0 else 1
    out: list[LinkSuggestion] = []
    for cand in all_ids:
        if cand in excluded:
            continue
        aa = graph_svc.adamic_adar_pair(src_tags, note_tags.get(cand, set()), tag_deg)
        ppr_norm = ppr.get(cand, 0.0) / ppr_max
        deg_norm = degree.get(cand, 0) / deg_max
        struct = graph_svc.softor([aa, ppr_norm])
        score = struct * (1.0 - 0.3 * deg_norm)
        if score <= 0:
            continue
        rationale_bits: list[str] = []
        if aa > 0:
            rationale_bits.append(f"shared rare tags ({aa:.2f})")
        if ppr_norm > 0:
            rationale_bits.append(f"reachable via PPR ({ppr_norm:.2f})")
        rationale = "; ".join(rationale_bits) or "weak structural signal"
        out.append(
            LinkSuggestion(
                note_id=cand,
                score=score,
                signals={
                    "adamic_adar": aa,
                    "ppr_norm": ppr_norm,
                    "degree_norm": deg_norm,
                },
                rationale=rationale,
            )
        )
    # Stable order: score desc, then id asc (deterministic on ties).
    out.sort(key=lambda s: (-s.score, str(s.note_id)))
    return out[: max(0, k)]


def _safe_log(x: float) -> float:
    """Avoid ``log(0)``; documented for future Node2Vec embedding
    weight; unused by the Phase 1 score above."""
    return math.log(max(1e-9, x))


__all__ = ["LinkSuggestion", "suggest_links_for_note"]
