"""A proposed (autonomously-generated, un-approved) note must not influence
the graph STRUCTURE (task 035ce6de, ADR-0043 follow-up).

D2 already hides a ``review_state='proposed'`` note from every direct-
visibility surface (the walk, search, listings, lookup). This pins the
second-order leak: it must also stay out of PageRank / Leiden / betweenness /
recency and the link-prediction candidate set -- even though it carries
``hypha_of`` edges to its sources -- until a human approves it. After approval
it re-enters the weave exactly like any note.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from flow_core.db import admin_session, tenant_session  # noqa: E402
from flow_core.models.note import Note, NoteKind  # noqa: E402
from flow_core.models.note_link import NoteNoteLink  # noqa: E402
from flow_core.services import graph, link_prediction  # noqa: E402
from flow_core.services import notes as nt  # noqa: E402
from flow_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="GRAPHX")
    return r.org_id, r.user_id


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> uuid.UUID:
    n = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body of {title}",
    )
    return n.id


async def _related(s: object, org: uuid.UUID, a: uuid.UUID, b: uuid.UUID) -> None:
    s.add(NoteNoteLink(org_id=org, parent_note_id=a, child_note_id=b, kind="related"))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _make_proposed(s: object, nid: uuid.UUID) -> None:
    note = (await s.execute(select(Note).where(Note.id == nid))).scalar_one()  # type: ignore[attr-defined]
    note.review_state = "proposed"
    await s.flush()  # type: ignore[attr-defined]


async def test_proposed_note_absent_from_graph_then_present_after_approval() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        # A path a-b-c-d gives betweenness something to measure; p hangs off a.
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        c = await _note(s, org, user, "c")
        d = await _note(s, org, user, "d")
        p = await _note(s, org, user, "p")
        for x, y in ((a, b), (b, c), (c, d), (a, p)):
            await _related(s, org, x, y)
        await _make_proposed(s, p)

        # Centrality / clustering / recency / link candidates all exclude p.
        assert p not in await graph.compute_pagerank(s, org_id=org)
        assert p not in (await graph.compute_leiden_clusters(s, org_id=org)).clusters
        assert p not in await graph.compute_betweenness(s, org_id=org)
        assert p not in await graph.compute_recency(s, org_id=org)
        cands = await link_prediction.suggest_links_for_note(s, org_id=org, note_id=b)
        assert p not in {cand.note_id for cand in cands}
        # The real notes are still there (the filter is surgical).
        assert a in await graph.compute_pagerank(s, org_id=org)

    # Approve p -> it re-enters the weave like any note.
    async with tenant_session(str(org), str(user)) as s:
        note = (await s.execute(select(Note).where(Note.id == p))).scalar_one()
        note.review_state = "approved"
        await s.flush()
        assert p in await graph.compute_pagerank(s, org_id=org)
        assert p in await graph.compute_recency(s, org_id=org)
