"""The structure of the work in the unified weave (ADR-0077 decision 4).

A subtask and its parent, and a task and the one it waits for, are edges of
the weave of notes and tasks: the graph view, task centrality and the
related-task suggestions are computed on it. What is asserted:

- with the unified weave off, parent and dependency rows change nothing
  (the notes-only path stays byte-identical);
- with it on, a parent pair is one edge at the parent weight and a
  dependency pair is one edge at the dependency weight however many types
  join it; an edge to a soft-deleted task is not emitted;
- the snapshot signature moves when the structure changes with every count
  unchanged (a re-parent), and does not move when nothing changed;
- a related-task suggestion never proposes the source's parent, subtask or
  dependency, which are already connected, while the structure still
  carries the walk to the task beyond them.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.dependency import DependencyType, TaskDependency
from mycelium_core.models.tag import TagKind
from mycelium_core.models.task import Task
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.services import graph as graph_svc
from mycelium_core.services import graph_snapshot as snap_svc
from mycelium_core.services import link_prediction as linkpred_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup


async def _workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="WSG",
        )
    return a.org_id, a.user_id


async def _task(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Task:
    return await tasks_svc.create_task(s, org_id=org, actor_id=user, title=title)  # type: ignore[arg-type]


async def _parent(s: object, child: uuid.UUID, parent: uuid.UUID | None) -> None:
    await s.execute(update(Task).where(Task.id == child).values(parent_task_id=parent))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


async def _depend(
    s: object, org: uuid.UUID, pred: uuid.UUID, succ: uuid.UUID, kind: DependencyType
) -> None:
    s.add(TaskDependency(org_id=org, predecessor_id=pred, successor_id=succ, type=kind))  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]


def _weights(edges: list[graph_svc.EdgeWeight]) -> dict[frozenset[uuid.UUID], float]:
    return {frozenset((e.src, e.dst)): e.weight for e in edges}


async def test_off_path_ignores_the_structure_of_the_work() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await _task(s, org, user, "a")
        b = await _task(s, org, user, "b")
        c = await _task(s, org, user, "c")
        before = (
            await graph_svc.compute_note_edge_weights(s, org_id=org, include_tasks=False),
            await snap_svc.graph_signature(s, org_id=org, include_tasks=False),
        )
        await _parent(s, b.id, a.id)
        await _depend(s, org, a.id, c.id, DependencyType.FS)
        after = (
            await graph_svc.compute_note_edge_weights(s, org_id=org, include_tasks=False),
            await snap_svc.graph_signature(s, org_id=org, include_tasks=False),
        )
    assert after == before


async def test_parent_and_dependency_are_one_edge_each_at_their_weight() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        parent = await _task(s, org, user, "parent")
        child = await _task(s, org, user, "child")
        pred = await _task(s, org, user, "pred")
        gone = await _task(s, org, user, "gone")
        await _parent(s, child.id, parent.id)
        # Two types on one pair: when, not how close; one edge.
        await _depend(s, org, pred.id, child.id, DependencyType.FS)
        await _depend(s, org, pred.id, child.id, DependencyType.SS)
        # An edge to a task in the bin is not an edge.
        await _depend(s, org, gone.id, parent.id, DependencyType.FS)
        await s.execute(
            update(Task)
            .where(Task.id == gone.id)
            .values(deleted_at=datetime.datetime.now(datetime.UTC))
        )
        await s.flush()
        w = _weights(await graph_svc.compute_note_edge_weights(s, org_id=org, include_tasks=True))
    assert w[frozenset((child.id, parent.id))] == graph_svc._TASK_PARENT_WEIGHT
    assert w[frozenset((pred.id, child.id))] == graph_svc._TASK_DEPENDENCY_WEIGHT
    assert not any(gone.id in pair for pair in w)


async def test_the_signature_follows_the_structure_not_only_its_counts() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await _task(s, org, user, "a")
        b = await _task(s, org, user, "b")
        c = await _task(s, org, user, "c")
        await _parent(s, c.id, a.id)
        await _depend(s, org, a.id, b.id, DependencyType.FS)
        first = await snap_svc.graph_signature(s, org_id=org, include_tasks=True)
        assert await snap_svc.graph_signature(s, org_id=org, include_tasks=True) == first
        # Same number of parented tasks, different parent.
        await _parent(s, c.id, b.id)
        reparented = await snap_svc.graph_signature(s, org_id=org, include_tasks=True)
        assert reparented != first
        # Same number of dependencies, a different pair.
        await s.execute(
            update(TaskDependency)
            .where(TaskDependency.predecessor_id == a.id)
            .values(predecessor_id=c.id)
        )
        await s.flush()
        assert await snap_svc.graph_signature(s, org_id=org, include_tasks=True) != reparented


async def test_suggestions_skip_what_the_structure_already_connects() -> None:
    """Source S has a parent P, a subtask K and waits for W, and all of them
    share S's tag, so each would be a candidate by tags alone. Beyond W sits
    X, tied to S only through W: the walk must still reach it."""
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        src = await _task(s, org, user, "S")
        par = await _task(s, org, user, "P")
        kid = await _task(s, org, user, "K")
        wait = await _task(s, org, user, "W")
        beyond = await _task(s, org, user, "X")
        tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="shared"
        )
        for t in (src, par, kid, wait):
            s.add(TaskTag(org_id=org, task_id=t.id, tag_id=tag.id))
        await s.flush()
        await _parent(s, src.id, par.id)
        await _parent(s, kid.id, src.id)
        await _depend(s, org, wait.id, src.id, DependencyType.FS)
        await _depend(s, org, beyond.id, wait.id, DependencyType.FS)
        got = {
            r.task_id
            for r in await linkpred_svc.suggest_links_for_task(s, org_id=org, task_id=src.id)
        }
    assert got.isdisjoint({par.id, kid.id, wait.id})
    assert beyond.id in got
