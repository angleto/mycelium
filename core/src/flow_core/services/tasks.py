"""Task service: CRUD, tags/assignees, comments. RBAC, optimistic
concurrency, i18n, audit. Workflow rules arrive in F2.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.comment import Comment
from flow_core.models.membership import Role
from flow_core.models.tag import Tag
from flow_core.models.task import ExecKind, Task, TaskStatus
from flow_core.models.task_assignee import TaskAssignee
from flow_core.models.task_tag import TaskTag
from flow_core.services import audit
from flow_core.services.rbac import require_role

_UPDATABLE = frozenset(
    {
        "title",
        "description",
        "priority",
        "start_date",
        "due_date",
        "estimate_effort_h",
        "executor_kind",
        "executor_user_id",
        "parent_task_id",
    }
)


async def _require_tag(session: AsyncSession, tag_id: uuid.UUID) -> None:
    found = (await session.execute(select(Tag.id).where(Tag.id == tag_id))).scalar_one_or_none()
    if found is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)


async def get_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    include_deleted: bool = False,
) -> Task:
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None or (task.deleted_at is not None and not include_deleted):
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    return task


async def create_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    title: str,
    description: str | None = None,
    priority: int = 3,
    start_date: dt.date | None = None,
    due_date: dt.date | None = None,
    parent_task_id: uuid.UUID | None = None,
    executor_kind: ExecKind = ExecKind.human,
    executor_user_id: uuid.UUID | None = None,
    estimate_effort_h: Decimal | None = None,
    tag_ids: Sequence[uuid.UUID] = (),
    assignee_ids: Sequence[uuid.UUID] = (),
) -> Task:
    await require_role(session, org_id, actor_id, Role.member)
    if parent_task_id is not None:
        await get_task(session, org_id=org_id, task_id=parent_task_id)
    task = Task(
        org_id=org_id,
        title=title,
        description=description,
        priority=priority,
        start_date=start_date,
        due_date=due_date,
        parent_task_id=parent_task_id,
        executor_kind=executor_kind,
        executor_user_id=executor_user_id,
        estimate_effort_h=estimate_effort_h,
        created_by=actor_id,
    )
    session.add(task)
    await session.flush()
    for tag_id in tag_ids:
        await _require_tag(session, tag_id)
        session.add(TaskTag(org_id=org_id, task_id=task.id, tag_id=tag_id))
    for user_id in assignee_ids:
        session.add(TaskAssignee(org_id=org_id, task_id=task.id, user_id=user_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task.id,
        action="create",
    )
    return task


async def list_tasks(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    status: TaskStatus | None = None,
    tag_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    parent_task_id: uuid.UUID | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
) -> list[Task]:
    stmt = select(Task)
    if not include_deleted:
        stmt = stmt.where(Task.deleted_at.is_(None))
    if not include_archived:
        stmt = stmt.where(Task.is_archived.is_(False))
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if parent_task_id is not None:
        stmt = stmt.where(Task.parent_task_id == parent_task_id)
    if tag_id is not None:
        stmt = stmt.join(TaskTag, TaskTag.task_id == Task.id).where(TaskTag.tag_id == tag_id)
    if assignee_id is not None:
        stmt = stmt.join(TaskAssignee, TaskAssignee.task_id == Task.id).where(
            TaskAssignee.user_id == assignee_id
        )
    stmt = stmt.order_by(Task.created_at.desc())
    return list((await session.execute(stmt)).scalars().unique().all())


async def update_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    unknown = set(values) - _UPDATABLE
    if unknown:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def _set(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
    action: str,
) -> int:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id, include_deleted=True)
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action=action,
    )
    return new_version


async def set_status(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    status: TaskStatus,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"status": status},
        action="set_status",
    )


async def archive_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    archived: bool = True,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"is_archived": archived},
        action="archive",
    )


async def soft_delete_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"deleted_at": dt.datetime.now(tz=dt.UTC)},
        action="soft_delete",
    )


async def restore_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"deleted_at": None},
        action="restore",
    )


async def attach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    await _require_tag(session, tag_id)
    try:
        async with session.begin_nested():
            session.add(TaskTag(org_id=org_id, task_id=task_id, tag_id=tag_id))
            await session.flush()
    except IntegrityError:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="attach_tag",
    )


async def detach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(TaskTag).where(TaskTag.task_id == task_id, TaskTag.tag_id == tag_id)
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="detach_tag",
    )


async def assign(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    try:
        async with session.begin_nested():
            session.add(TaskAssignee(org_id=org_id, task_id=task_id, user_id=user_id))
            await session.flush()
    except IntegrityError:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="assign",
    )


async def unassign(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(TaskAssignee).where(
            TaskAssignee.task_id == task_id,
            TaskAssignee.user_id == user_id,
        )
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="unassign",
    )


async def add_comment(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    body: str,
) -> Comment:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    comment = Comment(org_id=org_id, task_id=task_id, user_id=actor_id, body=body)
    session.add(comment)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="comment",
        entity_id=comment.id,
        action="create",
    )
    return comment


async def list_comments(
    session: AsyncSession, *, org_id: uuid.UUID, task_id: uuid.UUID
) -> list[Comment]:
    stmt = select(Comment).where(Comment.task_id == task_id).order_by(Comment.created_at)
    return list((await session.execute(stmt)).scalars().all())
