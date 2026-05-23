"""Symmetric "related task" links: pure navigation aid.

The relation is bidirectional by definition. We store one canonical row
per unordered pair, with the lower UUID in ``task_a_id``, so the unique
index dedupes natively (no need for application-level "either direction"
checks). All callers should hand in the two ids in any order; the
service canonicalises before insert.

No cycle rules (unlike ``dependencies``): A-B and B-A are the same edge.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.task_relation import TaskRelation
from flow_core.services import audit
from flow_core.services.rbac import require_role
from flow_core.services.tasks import get_task


def _canonical_pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (lower, higher) so the storage invariant ``a < b`` holds."""
    return (a, b) if a < b else (b, a)


async def add_relation(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    other_id: uuid.UUID,
) -> TaskRelation:
    await require_role(session, org_id, actor_id, Role.member)
    if task_id == other_id:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    # Both tasks must exist in the tenant (and not be soft-deleted).
    await get_task(session, org_id=org_id, task_id=task_id)
    await get_task(session, org_id=org_id, task_id=other_id)
    a, b = _canonical_pair(task_id, other_id)
    rel = TaskRelation(org_id=org_id, task_a_id=a, task_b_id=b)
    try:
        async with session.begin_nested():
            session.add(rel)
            await session.flush()
    except IntegrityError as exc:
        raise DomainError(MessageCode.DOMAIN_ERROR) from exc
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_relation",
        entity_id=rel.id,
        action="create",
    )
    return rel


async def remove_relation(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    relation_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    rel = (
        await session.execute(select(TaskRelation).where(TaskRelation.id == relation_id))
    ).scalar_one_or_none()
    if rel is None:
        raise NotFoundError(MessageCode.DOMAIN_ERROR)
    await session.delete(rel)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_relation",
        entity_id=relation_id,
        action="delete",
    )


async def list_relations(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
) -> list[TaskRelation]:
    stmt = select(TaskRelation)
    if task_id is not None:
        stmt = stmt.where(or_(TaskRelation.task_a_id == task_id, TaskRelation.task_b_id == task_id))
    return list((await session.execute(stmt)).scalars().all())
