"""Subtasks: a task's parent (``tasks.parent_task_id``)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.task import Task
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.connections import (
    Connection,
    Direction,
    Item,
    ItemKind,
    Relation,
)

T = ItemKind.task


class _TaskParent:
    name = "parent"
    creates = frozenset({(Relation.subtask_of, T, T)})

    async def list(
        self, session: AsyncSession, *, org_id: uuid.UUID, item: Item
    ) -> list[Connection]:
        if item.kind is not T:
            return []
        task = await session.get(Task, item.id)
        if task is None or task.org_id != org_id:
            return []
        out: list[Connection] = []
        if task.parent_task_id is not None:
            parent = await session.get(Task, task.parent_task_id)
            if parent is not None and parent.deleted_at is None:
                out.append(
                    Connection(
                        handle=f"{self.name}:{task.id}",
                        relation=Relation.subtask_of,
                        direction=Direction.out,
                        other_kind=T,
                        other_id=parent.id,
                        other_title=parent.title,
                        removable=True,
                    )
                )
        children = (
            await session.execute(
                select(Task.id, Task.title).where(
                    Task.org_id == org_id,
                    Task.parent_task_id == item.id,
                    Task.deleted_at.is_(None),
                )
            )
        ).all()
        out.extend(
            Connection(
                handle=f"{self.name}:{cid}",
                relation=Relation.subtask_of,
                direction=Direction.in_,
                other_kind=T,
                other_id=cid,
                other_title=title,
                removable=True,
            )
            for cid, title in children
        )
        return out

    async def _set_parent(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        child_id: uuid.UUID,
        parent_id: uuid.UUID | None,
    ) -> None:
        child = await session.get(Task, child_id)
        if child is None or child.org_id != org_id or child.deleted_at is not None:
            raise NotFoundError(MessageCode.TASK_NOT_FOUND)
        await tasks_svc.update_task(
            session,
            org_id=org_id,
            actor_id=actor_id,
            task_id=child_id,
            expected_version=child.version,
            values={"parent_task_id": parent_id},
        )

    async def link(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        source: Item,
        relation: Relation,
        target: Item,
        options: dict[str, Any],
    ) -> None:
        await self._set_parent(
            session, org_id=org_id, actor_id=actor_id, child_id=source.id, parent_id=target.id
        )

    async def unlink(
        self, session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, key: str
    ) -> None:
        await self._set_parent(
            session, org_id=org_id, actor_id=actor_id, child_id=uuid.UUID(key), parent_id=None
        )


ADAPTER = _TaskParent()
