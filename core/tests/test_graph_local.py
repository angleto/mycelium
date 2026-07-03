"""Fase 1 of the search-informed graph (task 561c6aca): the local adjacency
provider and the bounded best-first traversal.

The load-bearing pin is PARITY: for every non-proposed note X,
``graph_local.local_edges(X)`` must return exactly the weights the full
builder ``graph.compute_note_edge_weights`` materialises for the pairs
touching X -- same links, same Adamic-Adar tag overlap, same co-activity,
same soft-OR, byte-equal floats. The bounded walk then gets budget /
threshold / determinism / visibility guarantees of its own.
"""

from __future__ import annotations

import itertools
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.errors import NotFoundError  # noqa: E402
from mycelium_core.models.memory_blob import MemoryBlob  # noqa: E402
from mycelium_core.models.note import Note, NoteKind  # noqa: E402
from mycelium_core.models.note_coactivity import NoteCoactivity  # noqa: E402
from mycelium_core.models.note_link import NoteNoteLink  # noqa: E402
from mycelium_core.models.note_part import NotePart  # noqa: E402
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer  # noqa: E402
from mycelium_core.models.note_tag import NoteTag  # noqa: E402
from mycelium_core.models.tag import TagKind  # noqa: E402
from mycelium_core.services import focus_context, graph, graph_local, taxonomy  # noqa: E402
from mycelium_core.services import notes as nt  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402
from mycelium_core.services.graph import _pair_key  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org(name: str = "GRLOC") -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name=name)
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


async def _link(s: object, org: uuid.UUID, a: uuid.UUID, b: uuid.UUID, kind: str) -> None:
    s.add(NoteNoteLink(org_id=org, parent_note_id=a, child_note_id=b, kind=kind))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _tag(s: object, org: uuid.UUID, user: uuid.UUID, name: str) -> uuid.UUID:
    tag = await taxonomy.create_tag(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=TagKind.generic,
        name=name,
    )
    return tag.id


async def _attach(s: object, org: uuid.UUID, note_id: uuid.UUID, tag_id: uuid.UUID) -> None:
    s.add(NoteTag(org_id=org, note_id=note_id, tag_id=tag_id))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _coact(s: object, org: uuid.UUID, a: uuid.UUID, b: uuid.UUID, count: int) -> None:
    ka, kb = _pair_key(a, b)
    s.add(NoteCoactivity(org_id=org, note_a_id=ka, note_b_id=kb, session_count=count))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def test_local_edges_parity_with_full_builder() -> None:
    """For every note X the per-node provider returns exactly the full
    builder's weights for the pairs touching X (all three sources active)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "A")
        b = await _note(s, org, user, "B")
        c = await _note(s, org, user, "C")
        d = await _note(s, org, user, "D")
        e = await _note(s, org, user, "E")
        f = await _note(s, org, user, "F")
        await _link(s, org, a, b, "related")
        await _link(s, org, a, c, "hypha_of")
        await _link(s, org, c, d, "supersedes")
        t_rare = await _tag(s, org, user, "rare-tag")
        t_mid = await _tag(s, org, user, "mid-tag")
        for nid in (a, b):
            await _attach(s, org, nid, t_rare)
        for nid in (a, d, e):
            await _attach(s, org, nid, t_mid)
        await _coact(s, org, a, f, 3)
        await _coact(s, org, b, c, 2)

        full = await graph.compute_note_edge_weights(s, org_id=org)
        by_pair = {(str(ew.src), str(ew.dst)): ew.weight for ew in full}
        for x in (a, b, c, d, e, f):
            local = await graph_local.local_edges(s, org_id=org, note_id=x)
            expected = {}
            for (src, dst), w in by_pair.items():
                if str(x) == src:
                    expected[dst] = w
                elif str(x) == dst:
                    expected[src] = w
            got = {str(nb): w for nb, w in local.items()}
            assert got == expected, f"parity broken at note {x}"


async def test_local_edges_drop_proposed_neighbour() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "A")
        b = await _note(s, org, user, "B")
        await _link(s, org, a, b, "related")
        nb = (await s.execute(select(Note).where(Note.id == b))).scalar_one()
        nb.review_state = "proposed"
        await s.flush()
        assert await graph_local.local_edges(s, org_id=org, note_id=a) == {}


async def test_bounded_budget_determinism_and_order() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        chain = [await _note(s, org, user, f"N{i}") for i in range(5)]
        for x, y in itertools.pairwise(chain):
            await _link(s, org, x, y, "related")
        r1 = await graph_local.bounded_neighborhood(
            s, org_id=org, actor_id=user, seed_note_id=chain[0], node_budget=2, tau=0.001
        )
        r2 = await graph_local.bounded_neighborhood(
            s, org_id=org, actor_id=user, seed_note_id=chain[0], node_budget=2, tau=0.001
        )
        assert [n.note_id for n in r1.nodes] == [n.note_id for n in r2.nodes]
        assert len(r1.nodes) == 2
        assert r1.stopped_by == "node_budget"
        # Best-first: weights non-increasing, hops recorded, tree edges match.
        assert r1.nodes[0].weight >= r1.nodes[1].weight
        assert [n.hop for n in r1.nodes] == [1, 2]
        assert [(e.src, e.dst) for e in r1.edges] == [
            (chain[0], chain[1]),
            (chain[1], chain[2]),
        ]


async def test_bounded_tau_prunes_weak_paths() -> None:
    """related edge (0.45) x gamma 0.85 = 0.3825 at hop 1, ~0.146 at hop 2:
    tau 0.2 keeps the first hop and prunes the second, regardless of the
    node budget."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "A")
        b = await _note(s, org, user, "B")
        c = await _note(s, org, user, "C")
        await _link(s, org, a, b, "related")
        await _link(s, org, b, c, "related")
        r = await graph_local.bounded_neighborhood(
            s, org_id=org, actor_id=user, seed_note_id=a, node_budget=10, tau=0.2
        )
        assert [n.note_id for n in r.nodes] == [b]
        assert r.stopped_by == "exhausted"


async def _index_text(s: object, org: uuid.UUID, note_id: uuid.UUID, text: str) -> None:
    """Give a note an indexed part blob of a KNOWN size (create_note in this
    direct-service context does not index parts; the char budget reads the
    indexed text via NotePartIndexPointer, same source as focus_context)."""
    part = (
        (await s.execute(select(NotePart).where(NotePart.note_id == note_id)))  # type: ignore[attr-defined]
        .scalars()
        .first()
    )
    if part is None:
        part = NotePart(org_id=org, note_id=note_id, ord=0, body=text)
        s.add(part)  # type: ignore[attr-defined]
        await s.flush()  # type: ignore[attr-defined]
    blob = MemoryBlob(org_id=org, namespace="note", text=text)
    s.add(blob)  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]
    s.add(  # type: ignore[attr-defined]
        NotePartIndexPointer(
            part_id=part.id, note_id=note_id, org_id=org, blob_id=blob.id, content_hash="x"
        )
    )
    await s.flush()  # type: ignore[attr-defined]


async def test_bounded_char_budget_stops_walk() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "A")
        b = await _note(s, org, user, "B")
        await _link(s, org, a, b, "related")
        await _index_text(s, org, a, "x" * 100)
        await _index_text(s, org, b, "y" * 100)
        # Seed fits (100 <= 150) but the neighbour would overflow: the
        # overflowing node is NOT returned and the walk stops there.
        r = await graph_local.bounded_neighborhood(
            s, org_id=org, actor_id=user, seed_note_id=a, char_budget=150, tau=0.001
        )
        assert r.nodes == [] and r.stopped_by == "char_budget"
        # Both fit at 250: chars are accounted exactly, then the frontier
        # exhausts.
        r2 = await graph_local.bounded_neighborhood(
            s, org_id=org, actor_id=user, seed_note_id=a, char_budget=250, tau=0.001
        )
        assert [n.note_id for n in r2.nodes] == [b]
        assert r2.stopped_by == "exhausted"
        assert r2.chars == 200


async def test_bounded_seed_visibility() -> None:
    org, user = await _org()
    org2, user2 = await _org("GRLOC2")
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "A")
        with pytest.raises(NotFoundError):
            await graph_local.bounded_neighborhood(
                s, org_id=org, actor_id=user, seed_note_id=uuid.uuid4()
            )
        note = (await s.execute(select(Note).where(Note.id == a))).scalar_one()
        note.review_state = "proposed"
        await s.flush()
        with pytest.raises(NotFoundError):
            await graph_local.bounded_neighborhood(s, org_id=org, actor_id=user, seed_note_id=a)
    async with tenant_session(str(org2), str(user2)) as s:
        # RLS: another org's seed does not resolve.
        with pytest.raises(NotFoundError):
            await graph_local.bounded_neighborhood(s, org_id=org2, actor_id=user2, seed_note_id=a)


async def test_walk_context_bounded_mode() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "A")
        b = await _note(s, org, user, "B")
        await _link(s, org, a, b, "hypha_of")
        steps = await focus_context.walk_context(
            s, org_id=org, actor_id=user, seed_id=a, mode="bounded", budget=5
        )
        assert [w.note_id for w in steps] == [b]
        assert steps[0].step == 1  # hop distance
        assert steps[0].title == "B"
