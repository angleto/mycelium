"""Regression + behaviour guard for the unified task graph (ADR-0042 D1,
task b8c60940 step 4).

The whole point of the ``garden_unified_task_graph_enabled`` flag is a
**byte-identical** notes-only path when it is off, because the graph
analytics (edge weights, PageRank, PPR, betweenness, Leiden, and the
snapshot signature) drive the mindmap, ``/garden/clusters``, centrality
sensors and the closed WS-* graph work. The blast radius is high, so the
first test here builds a workspace that ALSO has tasks, task relations,
note↔task links and task tags, and asserts that with tasks invisible
(``include_tasks=False``) every output equals the one computed before the
task rows existed. If any task signal leaked into the notes-only path,
these equalities break.

The ON-path tests then assert tasks genuinely join the weave, and that the
flag (via ``get_settings``) is what flips the default.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import select, update

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.models.note import Note, NoteKind
from flow_core.models.note_link import NoteTaskLink
from flow_core.models.note_tag import NoteTag
from flow_core.models.tag import TagKind
from flow_core.models.task import Task
from flow_core.models.task_relation import TaskRelation
from flow_core.models.task_tag import TaskTag
from flow_core.services import graph as graph_svc
from flow_core.services import graph_snapshot as snap_svc
from flow_core.services import link_prediction as linkpred_svc
from flow_core.services import note_links, taxonomy
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup


async def _workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="UTG",
        )
    return a.org_id, a.user_id


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body of {title}",
    )


async def _task(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Task:
    return await tasks_svc.create_task(s, org_id=org, actor_id=user, title=title)  # type: ignore[arg-type]


async def _tag(s: object, org: uuid.UUID, user: uuid.UUID, name: str) -> uuid.UUID:
    tag = await taxonomy.create_tag(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=TagKind.generic,
        name=name,
    )
    return tag.id


async def _tag_note(s: object, org: uuid.UUID, note_id: uuid.UUID, tag_id: uuid.UUID) -> None:
    s.add(NoteTag(org_id=org, note_id=note_id, tag_id=tag_id))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _tag_task(s: object, org: uuid.UUID, task_id: uuid.UUID, tag_id: uuid.UUID) -> None:
    s.add(TaskTag(org_id=org, task_id=task_id, tag_id=tag_id))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _link_notes(
    s: object, org: uuid.UUID, user: uuid.UUID, a: uuid.UUID, b: uuid.UUID
) -> None:
    await note_links.link_notes(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        parent_note_id=a,
        child_note_id=b,
        kind="related",
    )


async def _link_note_task(
    s: object, org: uuid.UUID, note_id: uuid.UUID, task_id: uuid.UUID, kind: str
) -> None:
    s.add(NoteTaskLink(org_id=org, note_id=note_id, task_id=task_id, kind=kind))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _relate_tasks(s: object, org: uuid.UUID, a: uuid.UUID, b: uuid.UUID) -> None:
    lo, hi = (a, b) if a < b else (b, a)
    s.add(TaskRelation(org_id=org, task_a_id=lo, task_b_id=hi))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _all_outputs(s: object, org: uuid.UUID, *, include_tasks: bool) -> dict[str, object]:
    """Every flag-sensitive graph output in one bundle, for equality
    comparison. PPR is seeded at the lowest note id so the seed exists in
    both the notes-only and unified runs."""
    note_ids = sorted(
        (
            await s.execute(select(Note.id).where(Note.org_id == org))  # type: ignore[attr-defined]
        )
        .scalars()
        .all(),
        key=str,
    )
    seed = note_ids[0]
    return {
        "edges": await graph_svc.compute_note_edge_weights(
            s,  # type: ignore[arg-type]
            org_id=org,
            include_tasks=include_tasks,
        ),
        "pagerank": await graph_svc.compute_pagerank(
            s,  # type: ignore[arg-type]
            org_id=org,
            include_tasks=include_tasks,
        ),
        "ppr": await graph_svc.compute_personalized_pagerank(
            s,  # type: ignore[arg-type]
            org_id=org,
            seed_ids=[seed],
            include_tasks=include_tasks,
        ),
        "betweenness": await graph_svc.compute_betweenness(
            s,  # type: ignore[arg-type]
            org_id=org,
            include_tasks=include_tasks,
        ),
        "leiden": await graph_svc.compute_leiden_clusters(
            s,  # type: ignore[arg-type]
            org_id=org,
            include_tasks=include_tasks,
        ),
        "signature": await snap_svc.graph_signature(
            s,  # type: ignore[arg-type]
            org_id=org,
            include_tasks=include_tasks,
        ),
    }


async def test_off_path_byte_identical_with_tasks_present() -> None:
    """The core regression guard: task rows present, but with the flag off
    every analytic equals what it was before any task existed. Proves no
    task signal leaks into the notes-only weave."""
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        # Notes-only base: two linked + co-tagged notes plus a third.
        g = await _tag(s, org, user, "shared")
        n1 = await _note(s, org, user, "n1")
        n2 = await _note(s, org, user, "n2")
        n3 = await _note(s, org, user, "n3")
        await _tag_note(s, org, n1.id, g)
        await _tag_note(s, org, n2.id, g)
        await _link_notes(s, org, user, n1.id, n3.id)

        before = await _all_outputs(s, org, include_tasks=False)

        # Now add task rows that WOULD perturb every source if they leaked:
        # a task sharing the generic tag (AA edge), a note↔task link, a
        # task↔task relation.
        t1 = await _task(s, org, user, "t1")
        t2 = await _task(s, org, user, "t2")
        await _tag_task(s, org, t1.id, g)
        await _link_note_task(s, org, n1.id, t1.id, "subject")
        await _relate_tasks(s, org, t1.id, t2.id)

        after = await _all_outputs(s, org, include_tasks=False)

    assert after["edges"] == before["edges"]
    assert after["pagerank"] == before["pagerank"]
    assert after["ppr"] == before["ppr"]
    assert after["betweenness"] == before["betweenness"]
    assert after["leiden"] == before["leiden"]
    assert after["signature"] == before["signature"]
    # And the OFF signature stays in the notes-only format (no task dims).
    assert ":T" not in str(after["signature"])


async def test_on_path_includes_tasks_in_every_surface() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        g = await _tag(s, org, user, "shared")
        n1 = await _note(s, org, user, "n1")
        await _tag_note(s, org, n1.id, g)
        t1 = await _task(s, org, user, "t1")
        t2 = await _task(s, org, user, "t2")
        await _tag_task(s, org, t1.id, g)  # n1<->t1 AA edge
        await _link_note_task(s, org, n1.id, t1.id, "subject")  # n1<->t1 typed edge
        await _relate_tasks(s, org, t1.id, t2.id)  # t1<->t2 related edge

        off = await _all_outputs(s, org, include_tasks=False)
        on = await _all_outputs(s, org, include_tasks=True)

    pr_on: dict[uuid.UUID, float] = on["pagerank"]  # type: ignore[assignment]
    pr_off: dict[uuid.UUID, float] = off["pagerank"]  # type: ignore[assignment]
    assert t1.id in pr_on and t2.id in pr_on
    assert t1.id not in pr_off and t2.id not in pr_off

    edge_pairs = {(e.src, e.dst) for e in on["edges"]}  # type: ignore[attr-defined]
    lo, hi = (n1.id, t1.id) if str(n1.id) <= str(t1.id) else (t1.id, n1.id)
    assert (lo, hi) in edge_pairs  # n1<->t1 surfaced
    tlo, thi = (t1.id, t2.id) if str(t1.id) <= str(t2.id) else (t2.id, t1.id)
    assert (tlo, thi) in edge_pairs  # t1<->t2 related surfaced

    ppr_on: dict[uuid.UUID, float] = on["ppr"]  # type: ignore[assignment]
    assert ppr_on.get(t1.id, 0.0) > 0.0  # reachable from n1 via the typed link

    assert on["signature"] != off["signature"]
    assert ":T" in str(on["signature"])

    # Leiden only runs with the optional clustering extra; assert task
    # membership only when it is available.
    leiden_on = on["leiden"]
    if leiden_on.modularity is not None:  # type: ignore[attr-defined]
        assert t1.id in leiden_on.clusters  # type: ignore[attr-defined]


async def test_on_path_excludes_soft_deleted_tasks() -> None:
    """A soft-deleted task keeps its relation / note-link / tag rows, but it
    is not a graph node: those rows must not emit phantom edges or nodes."""
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        n1 = await _note(s, org, user, "n1")
        t1 = await _task(s, org, user, "t1")
        t2 = await _task(s, org, user, "t2")
        await _link_note_task(s, org, n1.id, t1.id, "subject")
        await _relate_tasks(s, org, t1.id, t2.id)
        await s.execute(
            update(Task)
            .where(Task.id == t1.id)
            .values(deleted_at=datetime.datetime.now(datetime.UTC))
        )
        await s.flush()
        edges = await graph_svc.compute_note_edge_weights(s, org_id=org, include_tasks=True)
        bc = await graph_svc.compute_betweenness(s, org_id=org, include_tasks=True)
        pr = await graph_svc.compute_pagerank(s, org_id=org, include_tasks=True)
    node_ids = {e.src for e in edges} | {e.dst for e in edges}
    assert t1.id not in node_ids  # lingering relation/link rows emit no edge
    assert t1.id not in bc  # not a betweenness node
    assert t1.id not in pr  # not a centrality node
    assert t2.id in pr  # a live task is still a node, even if now isolated
    assert t2.id not in node_ids  # its only edge was to the deleted task


async def test_suggest_links_for_task_ignores_soft_deleted_tasks() -> None:
    """The task link-prediction scores of live candidates must be unchanged by
    a soft-deleted task that shares a tag (would inflate the Adamic-Adar
    rarity denominator) and is related to a candidate (would inflate its
    degree damping)."""
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await _task(s, org, user, "a")
        b = await _task(s, org, user, "b")
        g = await _tag(s, org, user, "shared")
        await _tag_task(s, org, a.id, g)
        await _tag_task(s, org, b.id, g)  # a<->b candidate via shared tag
        before = await linkpred_svc.suggest_links_for_task(s, org_id=org, task_id=a.id)
        # A soft-deleted task that shares the tag and is related to b.
        d = await _task(s, org, user, "d")
        await _tag_task(s, org, d.id, g)
        await _relate_tasks(s, org, b.id, d.id)
        await s.execute(
            update(Task)
            .where(Task.id == d.id)
            .values(deleted_at=datetime.datetime.now(datetime.UTC))
        )
        await s.flush()
        after = await linkpred_svc.suggest_links_for_task(s, org_id=org, task_id=a.id)
    before_scores = {(r.task_id, round(r.score, 9)) for r in before}
    after_scores = {(r.task_id, round(r.score, 9)) for r in after}
    assert before_scores == after_scores
    assert d.id not in {r.task_id for r in after}  # never a candidate either


async def test_flag_drives_the_unified_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """The primitives are pure (notes-only by default); it is the unified
    SURFACE that reads the fleet flag and opts in. ``refresh_graph_snapshot``
    is one such surface: with the flag on, the stored snapshot spans tasks
    and the signature carries the task dimensions."""
    monkeypatch.setattr(get_settings(), "garden_unified_task_graph_enabled", True)
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        g = await _tag(s, org, user, "shared")
        n1 = await _note(s, org, user, "n1")
        await _tag_note(s, org, n1.id, g)
        t1 = await _task(s, org, user, "t1")
        await _tag_task(s, org, t1.id, g)
        await _link_note_task(s, org, n1.id, t1.id, "subject")
        assert await snap_svc.refresh_graph_snapshot(s, org_id=org) is True
        snap = await snap_svc.get_graph_snapshot(s, org_id=org)
    assert snap is not None
    assert str(t1.id) in snap.centrality  # the task is a stored graph node
    assert ":T" in snap.signature  # signature folded in the task dimensions
