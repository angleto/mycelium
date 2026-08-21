"""A note in the TRASH is not a graph node (task f8402e7f, audit note
`bdc62d7a` §2.4).

ADR-0043 D1 defines an effective note as ``review_state IS DISTINCT FROM
'proposed' AND deleted_at IS NULL``, but the note/graph surfaces used to
filter only the first half: a trashed note still entered the centrality
node set (so it moved the PageRank of LIVE notes), still surfaced in the
bounded neighbourhood an agent gets as its working set, and could still
be offered as a link target. This pins the whole predicate on the surfaces
that had half of it, plus the restore path -- the perimeter is derived at
query time, so a restore brings the note back everywhere with nothing
re-indexed (same property `test_blob_lifecycle` pins for the blob side).

`test_graph_local` holds the twin for the bounded walk; `test_graph_proposed_
exclusion` holds the twin for the other half of the predicate.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.services import graph, graph_local, link_prediction
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup
from mycelium_core.services.note_effective import (
    effective_note_clause,
    ineffective_note_ids,
    note_is_effective,
)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="NOTEFF")
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


async def _version(s: object, note_id: uuid.UUID) -> int:
    return int(
        (await s.execute(select(Note.version).where(Note.id == note_id))).scalar_one()  # type: ignore[attr-defined]
    )


async def _trash(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> None:
    async with tenant_session(str(org), str(user)) as s:
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            expected_version=await _version(s, note_id),
        )


async def _restore(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> None:
    async with tenant_session(str(org), str(user)) as s:
        await nt.restore_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            expected_version=await _version(s, note_id),
        )


def _rounded(ranks: dict[uuid.UUID, float]) -> dict[uuid.UUID, float]:
    return {nid: round(v, 9) for nid, v in ranks.items()}


async def test_trashed_note_leaves_the_node_set_and_the_live_ranking() -> None:
    """(a) A trashed note is neither a node nor an edge endpoint, and the
    centrality of the notes that stayed is exactly what it was BEFORE the
    trashed note ever existed -- it does not merely disappear from the
    output, it stops influencing it."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        c = await _note(s, org, user, "c")
        for x, y in ((a, b), (b, c)):
            await _related(s, org, x, y)
        # The ranking of the live weave, before the fourth note exists.
        baseline = _rounded(await graph.compute_pagerank(s, org_id=org))
        assert set(baseline) == {a, b, c}

        # d hangs off a and DOES move the live ranking while it is effective.
        d = await _note(s, org, user, "d")
        await _related(s, org, a, d)
        with_d = _rounded(await graph.compute_pagerank(s, org_id=org))
        assert d in with_d
        assert {k: v for k, v in with_d.items() if k != d} != baseline

    await _trash(org, user, d)

    async with tenant_session(str(org), str(user)) as s:
        assert d not in await graph._node_ids(s, org_id=org, include_tasks=False)
        edges = await graph.compute_note_edge_weights(s, org_id=org)
        # Betweenness derives its node set from the EDGES, so an edge that
        # survived would resurrect d as a phantom node.
        assert d not in {e.src for e in edges} | {e.dst for e in edges}
        assert d not in await graph.compute_betweenness(s, org_id=org)
        assert d not in (await graph.compute_leiden_clusters(s, org_id=org)).clusters
        assert d not in await graph.compute_recency(s, org_id=org)
        assert _rounded(await graph.compute_pagerank(s, org_id=org)) == baseline


async def test_trashed_note_is_never_a_link_suggestion_target() -> None:
    """(c) A note nobody can open must not be proposed as a link target --
    the same argument ADR-0043 makes for an un-approved proposal."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        c = await _note(s, org, user, "c")
        d = await _note(s, org, user, "d")
        # d shares the weave with the others but is not linked to a, so it is
        # a legitimate candidate for a while it is effective.
        for x, y in ((a, b), (b, c), (c, d)):
            await _related(s, org, x, y)
        assert d in {
            s_.note_id
            for s_ in await link_prediction.suggest_links_for_note(s, org_id=org, note_id=a)
        }

    await _trash(org, user, d)

    async with tenant_session(str(org), str(user)) as s:
        cands = await link_prediction.suggest_links_for_note(s, org_id=org, note_id=a)
        assert d not in {s_.note_id for s_ in cands}
        # The source itself being trashed yields nothing rather than a
        # neighbourhood of a node the rest of the system denies.
        assert await link_prediction.suggest_links_for_note(s, org_id=org, note_id=d) == []


async def test_restore_returns_the_note_to_every_surface_without_reindex() -> None:
    """(d) The perimeter is DERIVED at query time: nothing is rewritten on
    delete and nothing is rebuilt on restore, so one UPDATE of ``deleted_at``
    puts the note back into centrality, the bounded walk and the link
    candidates at once."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")
        b = await _note(s, org, user, "b")
        c = await _note(s, org, user, "c")
        for x, y in ((a, b), (b, c)):
            await _related(s, org, x, y)
        before_pr = _rounded(await graph.compute_pagerank(s, org_id=org))
        before_hood = {
            n.note_id
            for n in (
                await graph_local.bounded_neighborhood(
                    s, org_id=org, actor_id=user, seed_note_id=a, tau=0.001
                )
            ).nodes
        }
        before_cands = {
            s_.note_id
            for s_ in await link_prediction.suggest_links_for_note(s, org_id=org, note_id=a)
        }
        assert c in before_hood and c in before_cands

    await _trash(org, user, c)
    async with tenant_session(str(org), str(user)) as s:
        assert c not in await graph.compute_pagerank(s, org_id=org)
        hood = await graph_local.bounded_neighborhood(
            s, org_id=org, actor_id=user, seed_note_id=a, tau=0.001
        )
        assert c not in {n.note_id for n in hood.nodes}
        assert c not in {
            s_.note_id
            for s_ in await link_prediction.suggest_links_for_note(s, org_id=org, note_id=a)
        }
        # The link rows survive the trip to the bin: nothing was destroyed,
        # so nothing has to be rebuilt.
        assert (
            await s.execute(
                select(NoteNoteLink.parent_note_id).where(
                    NoteNoteLink.org_id == org,
                    NoteNoteLink.child_note_id == c,
                )
            )
        ).scalars().all() == [b]

    await _restore(org, user, c)
    async with tenant_session(str(org), str(user)) as s:
        assert _rounded(await graph.compute_pagerank(s, org_id=org)) == before_pr
        hood = await graph_local.bounded_neighborhood(
            s, org_id=org, actor_id=user, seed_note_id=a, tau=0.001
        )
        assert {n.note_id for n in hood.nodes} == before_hood
        assert {
            s_.note_id
            for s_ in await link_prediction.suggest_links_for_note(s, org_id=org, note_id=a)
        } == before_cands


async def test_the_three_forms_agree_on_every_state() -> None:
    """The clause, its complement-as-ids and the row-level mirror are three
    renderings of ONE rule: pin them against each other over the whole
    (review_state x deleted_at) matrix, so a future edit cannot move one
    without the others."""
    org, user = await _org()
    states: list[str | None] = [None, "approved", "proposed"]
    ids: dict[tuple[str | None, bool], uuid.UUID] = {}
    deleted_at = dt.datetime.now(dt.UTC)
    async with tenant_session(str(org), str(user)) as s:
        for review_state in states:
            for trashed in (False, True):
                nid = await _note(s, org, user, f"{review_state}-{trashed}")
                note = (await s.execute(select(Note).where(Note.id == nid))).scalar_one()
                note.review_state = review_state
                if trashed:
                    note.deleted_at = deleted_at
                await s.flush()
                ids[(review_state, trashed)] = nid

        for include_deleted in (False, True):
            for include_proposed in (False, True):
                selected = {
                    r[0]
                    for r in (
                        await s.execute(
                            select(Note.id).where(
                                Note.org_id == org,
                                effective_note_clause(
                                    include_deleted=include_deleted,
                                    include_proposed=include_proposed,
                                ),
                            )
                        )
                    ).all()
                }
                for (review_state, trashed), nid in ids.items():
                    expected = note_is_effective(
                        review_state=review_state,
                        deleted_at=deleted_at if trashed else None,
                        include_deleted=include_deleted,
                        include_proposed=include_proposed,
                    )
                    assert (nid in selected) is expected, (
                        review_state,
                        trashed,
                        include_deleted,
                        include_proposed,
                    )

        # The id-set form is exactly the complement, whole-org and narrowed
        # to a candidate set alike (the builders rely on both).
        every = set(ids.values())
        effective = {
            r[0]
            for r in (
                await s.execute(select(Note.id).where(Note.org_id == org, effective_note_clause()))
            ).all()
        }
        assert await ineffective_note_ids(s, org_id=org) == every - effective
        assert await ineffective_note_ids(s, org_id=org, among=every) == every - effective
        assert await ineffective_note_ids(s, org_id=org, among=[]) == set()

        # ``include_proposed`` is not a listing option in either form: it is
        # the review-inbox bypass of ``get_note`` and the photographers'
        # opt-out (the revision logger and ``snapshot_note``), never a way
        # for a surface to show a proposal.
        assert note_is_effective(review_state="proposed", deleted_at=None, include_proposed=True)
        assert not note_is_effective(review_state="proposed", deleted_at=None)
