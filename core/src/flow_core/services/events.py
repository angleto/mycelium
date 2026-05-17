"""Events / appointments service (docs/adr/0008, FR-4).

No-ubiquity: a participant cannot have two overlapping appointments.
Enforced transactionally here; the scheduler treats events as fixed
exclusive reservations on a person's timeline.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.event import Event, EventParticipant
from flow_core.models.membership import Role
from flow_core.services import audit
from flow_core.services.rbac import require_role


async def _has_overlap(
    session: AsyncSession,
    participant_ids: Sequence[uuid.UUID],
    start_at: dt.datetime,
    end_at: dt.datetime,
    exclude_event_id: uuid.UUID | None = None,
) -> bool:
    if not participant_ids:
        return False
    stmt = (
        select(EventParticipant.user_id)
        .join(Event, Event.id == EventParticipant.event_id)
        .where(
            EventParticipant.user_id.in_(participant_ids),
            Event.start_at < end_at,
            Event.end_at > start_at,
        )
        .limit(1)
    )
    if exclude_event_id is not None:
        stmt = stmt.where(Event.id != exclude_event_id)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def get_event(session: AsyncSession, *, org_id: uuid.UUID, event_id: uuid.UUID) -> Event:
    ev = (await session.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise NotFoundError(MessageCode.EVENT_NOT_FOUND)
    return ev


async def create_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    title: str,
    start_at: dt.datetime,
    end_at: dt.datetime,
    participant_ids: Sequence[uuid.UUID],
    project_tag_id: uuid.UUID | None = None,
    client_tag_id: uuid.UUID | None = None,
    location: str | None = None,
) -> Event:
    await require_role(session, org_id, actor_id, Role.member)
    if end_at <= start_at:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if await _has_overlap(session, participant_ids, start_at, end_at):
        raise DomainError(MessageCode.EVENT_OVERLAP)
    ev = Event(
        org_id=org_id,
        project_tag_id=project_tag_id,
        client_tag_id=client_tag_id,
        title=title,
        start_at=start_at,
        end_at=end_at,
        location=location,
    )
    session.add(ev)
    await session.flush()
    for uid in participant_ids:
        session.add(EventParticipant(org_id=org_id, event_id=ev.id, user_id=uid))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="event",
        entity_id=ev.id,
        action="create",
    )
    return ev


async def list_events(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    start_from: dt.datetime | None = None,
    start_to: dt.datetime | None = None,
) -> list[Event]:
    stmt = select(Event)
    if user_id is not None:
        stmt = stmt.join(EventParticipant, EventParticipant.event_id == Event.id).where(
            EventParticipant.user_id == user_id
        )
    if start_from is not None:
        stmt = stmt.where(Event.start_at >= start_from)
    if start_to is not None:
        stmt = stmt.where(Event.start_at < start_to)
    stmt = stmt.order_by(Event.start_at)
    return list((await session.execute(stmt)).scalars().unique().all())


async def reschedule_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    event_id: uuid.UUID,
    start_at: dt.datetime,
    end_at: dt.datetime,
    expected_version: int,
) -> int:
    await require_role(session, org_id, actor_id, Role.member)
    if end_at <= start_at:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await get_event(session, org_id=org_id, event_id=event_id)
    participants = (
        (
            await session.execute(
                select(EventParticipant.user_id).where(EventParticipant.event_id == event_id)
            )
        )
        .scalars()
        .all()
    )
    if await _has_overlap(session, participants, start_at, end_at, exclude_event_id=event_id):
        raise DomainError(MessageCode.EVENT_OVERLAP)
    new_version = await optimistic_update(
        session,
        Event,
        pk=event_id,
        expected_version=expected_version,
        values={"start_at": start_at, "end_at": end_at},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="event",
        entity_id=event_id,
        action="reschedule",
    )
    return new_version


async def delete_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    event_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    ev = await get_event(session, org_id=org_id, event_id=event_id)
    await session.delete(ev)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="event",
        entity_id=event_id,
        action="delete",
    )
