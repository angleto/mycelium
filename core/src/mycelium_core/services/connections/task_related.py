"""Related tasks: a symmetric link with no scheduling meaning."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.models.task import Task
from mycelium_core.models.task_relation import TaskRelation
from mycelium_core.services import task_relations as relations_svc
from mycelium_core.services.connections import (
    Connection,
    Direction,
    Item,
    ItemKind,
    Relation,
)

T = ItemKind.task


class _TaskRelated:
    name = "taskrel"
    creates = frozenset({(Relation.related, T, T)})

    async def list(
        self, session: AsyncSession, *, org_id: uuid.UUID, item: Item
    ) -> list[Connection]:
        if item.kind is not T:
            return []
        a, b = aliased(Task), aliased(Task)
        rows = (
            await session.execute(
                select(TaskRelation, a.title, b.title)
                .join(a, a.id == TaskRelation.task_a_id)
                .join(b, b.id == TaskRelation.task_b_id)
                .where(
                    TaskRelation.org_id == org_id,
                    or_(TaskRelation.task_a_id == item.id, TaskRelation.task_b_id == item.id),
                    a.deleted_at.is_(None),
                    b.deleted_at.is_(None),
                )
            )
        ).all()
        return [
            Connection(
                handle=f"{self.name}:{r.id}",
                relation=Relation.related,
                direction=Direction.none,
                other_kind=T,
                other_id=r.task_b_id if r.task_a_id == item.id else r.task_a_id,
                other_title=b_title if r.task_a_id == item.id else a_title,
                removable=True,
                created_at=r.created_at,
            )
            for r, a_title, b_title in rows
        ]

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
        await relations_svc.add_relation(
            session, org_id=org_id, actor_id=actor_id, task_id=source.id, other_id=target.id
        )

    async def unlink(
        self, session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, key: str
    ) -> None:
        await relations_svc.remove_relation(
            session, org_id=org_id, actor_id=actor_id, relation_id=uuid.UUID(key)
        )


ADAPTER = _TaskRelated()
