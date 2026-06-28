"""Dependency service (docs/adr/0004, FR-3): typed edges, cycle
detection before insert, DAG graph query, derived blocked overlay.
CPM scheduling consumes these in F3.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.dependency import DependencyType, TaskDependency
from mycelium_core.models.membership import Role
from mycelium_core.models.task import Task
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import audit
from mycelium_core.services.rbac import require_role
from mycelium_core.services.tasks import get_task


async def _adjacency(
    session: AsyncSession,
) -> dict[uuid.UUID, set[uuid.UUID]]:
    rows = (
        await session.execute(
            select(
                TaskDependency.predecessor_id,
                TaskDependency.successor_id,
            )
        )
    ).all()
    adj: dict[uuid.UUID, set[uuid.UUID]] = {}
    for pred, succ in rows:
        adj.setdefault(pred, set()).add(succ)
    return adj


def _reachable(
    adj: dict[uuid.UUID, set[uuid.UUID]],
    start: uuid.UUID,
    target: uuid.UUID,
) -> bool:
    seen: set[uuid.UUID] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj.get(node, ()))
    return False


async def add_dependency(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    predecessor_id: uuid.UUID,
    successor_id: uuid.UUID,
    type: DependencyType,
    lag_working_minutes: int = 0,
) -> TaskDependency:
    await require_role(session, org_id, actor_id, Role.member)
    if predecessor_id == successor_id:
        raise DomainError(MessageCode.DEPENDENCY_CYCLE)
    await get_task(session, org_id=org_id, task_id=predecessor_id)
    await get_task(session, org_id=org_id, task_id=successor_id)
    adj = await _adjacency(session)
    if _reachable(adj, successor_id, predecessor_id):
        raise DomainError(MessageCode.DEPENDENCY_CYCLE)
    dep = TaskDependency(
        org_id=org_id,
        predecessor_id=predecessor_id,
        successor_id=successor_id,
        type=type,
        lag_working_minutes=lag_working_minutes,
    )
    try:
        async with session.begin_nested():
            session.add(dep)
            await session.flush()
    except IntegrityError as exc:
        raise DomainError(MessageCode.DOMAIN_ERROR) from exc
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="dependency",
        entity_id=dep.id,
        action="create",
    )
    return dep


async def remove_dependency(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    dependency_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    dep = (
        await session.execute(select(TaskDependency).where(TaskDependency.id == dependency_id))
    ).scalar_one_or_none()
    if dep is None:
        raise NotFoundError(MessageCode.DOMAIN_ERROR)
    await session.delete(dep)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="dependency",
        entity_id=dependency_id,
        action="delete",
    )


async def list_dependencies(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    limit: int | None = None,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> list[TaskDependency]:
    """Dependency edges, newest first (created_at desc, id asc -- a total
    order). With ``task_id`` only those touching the task (naturally small);
    without it the whole RLS-scoped graph, so ``limit`` + the ``after`` keyset
    cursor page that org-wide branch instead of streaming every edge."""
    stmt = select(TaskDependency).order_by(TaskDependency.created_at.desc(), TaskDependency.id)
    if task_id is not None:
        stmt = stmt.where(
            (TaskDependency.predecessor_id == task_id) | (TaskDependency.successor_id == task_id)
        )
    if after is not None:
        ac, ai = after
        stmt = stmt.where(
            or_(
                TaskDependency.created_at < ac,
                and_(TaskDependency.created_at == ac, TaskDependency.id > ai),
            )
        )
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def _blocked_ids(session: AsyncSession, node_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Derived overlay (FR-3): a task is blocked if it has an incoming
    dependency whose predecessor is not in a terminal workflow state.
    Non-persistent; refined by the scheduler (F3)."""
    if not node_ids:
        return set()
    pred = aliased(Task)
    rows = (
        (
            await session.execute(
                select(TaskDependency.successor_id)
                .join(pred, pred.id == TaskDependency.predecessor_id)
                .join(WorkflowState, WorkflowState.id == pred.state_id)
                .where(
                    TaskDependency.successor_id.in_(node_ids),
                    WorkflowState.is_terminal.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def blocked_task_ids(
    session: AsyncSession, *, org_id: uuid.UUID, node_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Public view of the derived blocked overlay (consumed by the
    advisory layer, F4b): tasks with a non-terminal predecessor."""
    return await _blocked_ids(session, node_ids)


async def graph(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_tag_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    stmt = select(Task).where(Task.deleted_at.is_(None))
    if project_tag_id is not None:
        stmt = stmt.join(TaskTag, TaskTag.task_id == Task.id).where(
            TaskTag.tag_id == project_tag_id
        )
    tasks = list((await session.execute(stmt)).scalars().unique().all())
    node_ids = {t.id for t in tasks}
    state_ids = {t.state_id for t in tasks}
    state_names: dict[uuid.UUID, str] = {}
    if state_ids:
        for sid, name in (
            await session.execute(
                select(WorkflowState.id, WorkflowState.name).where(WorkflowState.id.in_(state_ids))
            )
        ).all():
            state_names[sid] = name
    blocked = await _blocked_ids(session, node_ids)
    deps = await list_dependencies(session, org_id=org_id)
    edges = [
        {
            "predecessor": str(d.predecessor_id),
            "successor": str(d.successor_id),
            "type": d.type.value,
            "lag_working_minutes": d.lag_working_minutes,
        }
        for d in deps
        if d.predecessor_id in node_ids and d.successor_id in node_ids
    ]
    nodes = [
        {
            "id": str(t.id),
            "title": t.title,
            "state": state_names.get(t.state_id, ""),
            "blocked": t.id in blocked,
        }
        for t in tasks
    ]
    return {"nodes": nodes, "edges": edges}
