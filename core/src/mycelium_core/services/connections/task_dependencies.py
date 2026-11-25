"""Task dependencies (ADR-0004): finish-to-start and the other three types,
with a lag in working minutes."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.dependency import DependencyType, TaskDependency
from mycelium_core.models.task import Task
from mycelium_core.services import dependencies as dependencies_svc
from mycelium_core.services.connections import (
    Connection,
    Direction,
    Item,
    ItemKind,
    Relation,
)

T = ItemKind.task


class _TaskDependencies:
    name = "dep"
    creates = frozenset({(Relation.depends_on, T, T)})

    async def list(
        self, session: AsyncSession, *, org_id: uuid.UUID, item: Item
    ) -> list[Connection]:
        if item.kind is not T:
            return []
        pred, succ = aliased(Task), aliased(Task)
        rows = (
            await session.execute(
                select(TaskDependency, pred.title, succ.title)
                .join(pred, pred.id == TaskDependency.predecessor_id)
                .join(succ, succ.id == TaskDependency.successor_id)
                .where(
                    TaskDependency.org_id == org_id,
                    or_(
                        TaskDependency.predecessor_id == item.id,
                        TaskDependency.successor_id == item.id,
                    ),
                    pred.deleted_at.is_(None),
                    succ.deleted_at.is_(None),
                )
            )
        ).all()
        out: list[Connection] = []
        for d, p_title, s_title in rows:
            # The successor waits for the predecessor: seen from the
            # successor, it depends on the other.
            is_successor = d.successor_id == item.id
            out.append(
                Connection(
                    handle=f"{self.name}:{d.id}",
                    relation=Relation.depends_on,
                    direction=Direction.out if is_successor else Direction.in_,
                    other_kind=T,
                    other_id=d.predecessor_id if is_successor else d.successor_id,
                    other_title=p_title if is_successor else s_title,
                    removable=True,
                    created_at=d.created_at,
                    detail={"type": str(d.type.value), "lag_minutes": d.lag_working_minutes},
                )
            )
        return out

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
        try:
            dep_type = DependencyType(options.get("dependency_type") or "FS")
            lag = int(options.get("lag_minutes") or 0)
        except (ValueError, TypeError):
            raise DomainError(MessageCode.CONNECTION_INVALID) from None
        # "source depends on target": the target comes first.
        await dependencies_svc.add_dependency(
            session,
            org_id=org_id,
            actor_id=actor_id,
            predecessor_id=target.id,
            successor_id=source.id,
            type=dep_type,
            lag_working_minutes=lag,
        )

    async def unlink(
        self, session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, key: str
    ) -> None:
        await dependencies_svc.remove_dependency(
            session, org_id=org_id, actor_id=actor_id, dependency_id=uuid.UUID(key)
        )


ADAPTER = _TaskDependencies()
