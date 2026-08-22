"""Participants service: add / remove / list additional identities
pinned to an appointment-task (migration 0095, ADR-0008 addendum).

The window is denormalised onto each participant row so the GiST
EXCLUDE constraint enforces per-identity no-ubiquity without a join.
A DB trigger (``sync_task_participants_window``) keeps the columns
aligned with the parent task and removes participants if the task
loses its appointment status. This module is the CRUD adapter; it
re-throws the EXCLUDE IntegrityError as :class:`ConflictError` with
``MessageCode.EVENT_OVERLAP`` so the API surfaces a 409 with the
same code as the assignee path.

The assignee is **not** modelled here: the no-overlap for the primary
owner of the appointment lives on the EXCLUDE attached to ``tasks``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.identity import Identity
from mycelium_core.models.membership import Role
from mycelium_core.models.task import Task
from mycelium_core.models.task_participant import TaskParticipant
from mycelium_core.services import identities as identities_svc
from mycelium_core.services.rbac import require_role


async def _require_appointment_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
) -> Task:
    """Fetch the task and require that it carries an appointment window
    (``start_at`` + ``duration_minutes`` both set). Plain tasks /
    reminders cannot have participants -- there is no time slot to
    occupy. Raises DomainError when the precondition fails."""
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None or task.deleted_at is not None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    if task.start_at is None or task.duration_minutes is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    return task


async def add_participant(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    identity_id: uuid.UUID | None = None,
    handle: str | None = None,
) -> TaskParticipant:
    """Pin an identity to the appointment-task's window. Accepts either
    ``identity_id`` (uuid) or ``handle`` (looked up in the workspace's
    org). Idempotent on the same ``(task_id, identity_id)`` pair
    (returns the existing row). Raises ConflictError(EVENT_OVERLAP)
    when the identity already holds another appointment overlapping
    the task's window."""
    await require_role(session, org_id, actor_id, Role.member)
    task = await _require_appointment_task(session, org_id=org_id, task_id=task_id)
    if identity_id is None and handle:
        identity = await identities_svc.lookup_by_handle(session, org_id=org_id, handle=handle)
        if identity is None:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        identity_id = identity.id
    if identity_id is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    # Identity must belong to this org (FK alone is org-agnostic) AND its
    # principal must still be bindable -- active, and for a user still a
    # member. The handle branch above goes through the resolver, and
    # leaving the explicit-id branch ungated would be the same "one door
    # filtered, the other five open" mistake.
    ident = await identities_svc.get_identity(session, org_id=org_id, identity_id=identity_id)
    await identities_svc.require_bindable_identity(session, org_id=org_id, ident=ident)

    existing = (
        await session.execute(
            select(TaskParticipant).where(
                TaskParticipant.task_id == task_id,
                TaskParticipant.identity_id == identity_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = TaskParticipant(
        org_id=org_id,
        task_id=task_id,
        identity_id=identity_id,
        start_at=task.start_at,
        duration_minutes=task.duration_minutes,
    )
    session.add(row)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        if "no_overlap_task_participants" in str(exc.orig):
            raise ConflictError(MessageCode.EVENT_OVERLAP) from exc
        raise
    return row


async def remove_participant(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    identity_id: uuid.UUID,
) -> None:
    """Unpin ``identity_id`` from the task. No-op if not a participant."""
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(TaskParticipant).where(
            TaskParticipant.task_id == task_id,
            TaskParticipant.identity_id == identity_id,
        )
    )
    await session.flush()


async def list_participants(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
) -> list[tuple[TaskParticipant, Identity]]:
    """List participants of a task, joined with the identity row so the
    caller can surface ``(handle, kind)`` without a second round-trip."""
    rows = (
        await session.execute(
            select(TaskParticipant, Identity)
            .join(Identity, Identity.id == TaskParticipant.identity_id)
            .where(TaskParticipant.task_id == task_id)
            .order_by(Identity.handle)
        )
    ).all()
    return [(p, i) for p, i in rows]


async def participants_by_task(
    session: AsyncSession,
    *,
    task_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[tuple[TaskParticipant, Identity]]]:
    """Batched participants lookup (avoids the N+1 from listing tasks
    with their participant chips). Empty input returns an empty dict."""
    out: dict[uuid.UUID, list[tuple[TaskParticipant, Identity]]] = {}
    if not task_ids:
        return out
    rows = (
        await session.execute(
            select(TaskParticipant, Identity)
            .join(Identity, Identity.id == TaskParticipant.identity_id)
            .where(TaskParticipant.task_id.in_(task_ids))
            .order_by(Identity.handle)
        )
    ).all()
    for p, i in rows:
        out.setdefault(p.task_id, []).append((p, i))
    return out
