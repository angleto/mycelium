"""Events router: appointments with no-ubiquity (a participant cannot
have two overlapping appointments). Thin adapter over the service layer
(docs/adr/0001, 0008, FR-4)."""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import EventCreateIn, EventOut, EventRescheduleIn, VersionOut
from flow_core.models.event import Event
from flow_core.services import events as svc

router = APIRouter(tags=["events"])


def _out(e: Event) -> EventOut:
    return EventOut(
        id=e.id,
        title=e.title,
        start_at=e.start_at,
        end_at=e.end_at,
        location=e.location,
        project_tag_id=e.project_tag_id,
        client_tag_id=e.client_tag_id,
        version=e.version,
    )


@router.post("/events", response_model=EventOut)
async def create_event(
    body: EventCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> EventOut:
    e = await svc.create_event(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        title=body.title,
        start_at=body.start_at,
        end_at=body.end_at,
        participant_ids=body.participant_ids,
        project_tag_id=body.project_tag_id,
        client_tag_id=body.client_tag_id,
        location=body.location,
    )
    return _out(e)


@router.get("/events", response_model=list[EventOut])
async def list_events(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    user_id: uuid.UUID | None = None,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
) -> list[EventOut]:
    rows = await svc.list_events(
        ctx.session,
        org_id=ctx.org_id,
        user_id=user_id,
        start_from=start_from,
        start_to=start_to,
    )
    return [_out(e) for e in rows]


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(
    event_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> EventOut:
    return _out(await svc.get_event(ctx.session, org_id=ctx.org_id, event_id=event_id))


@router.patch("/events/{event_id}", response_model=VersionOut)
async def reschedule_event(
    event_id: uuid.UUID,
    body: EventRescheduleIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> VersionOut:
    version = await svc.reschedule_event(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        event_id=event_id,
        start_at=body.start_at,
        end_at=body.end_at,
        expected_version=body.expected_version,
    )
    return VersionOut(id=event_id, version=version)


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.delete_event(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        event_id=event_id,
    )
