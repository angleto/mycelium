"""MCP server: thin adapter over flow_core, co-equal to the REST API
(docs/adr/0001). Same service layer, RBAC and (org) isolation.

Each tool authenticates with a JWT and an org id, opens a tenant
session (RLS GUCs set) and verifies membership, exactly like the REST
``tenant_ctx`` dependency.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core import __version__
from flow_core.db import tenant_session
from flow_core.errors import AuthError
from flow_core.i18n import MessageCode
from flow_core.models.dependency import DependencyType
from flow_core.models.event import Event
from flow_core.models.schedule import Schedule
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import ConstraintKind, ScheduleMode, Task
from flow_core.models.time_entry import TimeEntry
from flow_core.security import decode_token
from flow_core.services import calendar as calendars
from flow_core.services import dependencies, scheduler, tasks, taxonomy
from flow_core.services import events as events_svc
from flow_core.services import time_tracking as time_svc
from flow_core.services.rbac import get_role
from flow_core.services.taxonomy import ClientInput
from flow_core.services.time_tracking import ReportGroup

mcp: FastMCP = FastMCP("flow")


@asynccontextmanager
async def _tenant(
    token: str, org_id: str
) -> AsyncIterator[tuple[AsyncSession, uuid.UUID, uuid.UUID]]:
    claims = decode_token(token)
    sub = claims.get("sub")
    if not isinstance(sub, str):
        raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
    user_id = uuid.UUID(sub)
    org = uuid.UUID(org_id)
    async with tenant_session(str(org), str(user_id)) as session:
        await get_role(session, org, user_id)  # raises if not a member
        yield session, org, user_id


@mcp.tool()
def ping() -> str:
    """Liveness probe; returns the flow-core version."""
    return f"flow-core {__version__}"


def _tag(t: Tag) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "kind": t.kind.value,
        "name": t.name,
        "version": t.version,
    }


def _task(t: Task) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "title": t.title,
        "state_id": str(t.state_id),
        "priority": t.priority,
        "version": t.version,
    }


@mcp.tool()
async def create_tag(
    token: str, org_id: str, kind: str, name: str, color: str | None = None
) -> dict[str, Any]:
    """Create a tag (kind: generic|client|project)."""
    async with _tenant(token, org_id) as (s, org, user):
        tag = await taxonomy.create_tag(
            s,
            org_id=org,
            actor_id=user,
            kind=TagKind(kind),
            name=name,
            color=color,
        )
        return _tag(tag)


@mcp.tool()
async def create_client(
    token: str,
    org_id: str,
    name: str,
    ragione_sociale: str,
    id_paese: str | None = None,
    id_codice: str | None = None,
) -> dict[str, Any]:
    """Create a client tag with its typed profile."""
    async with _tenant(token, org_id) as (s, org, user):
        tag = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            profile=ClientInput(
                ragione_sociale=ragione_sociale,
                id_paese=id_paese,
                id_codice=id_codice,
            ),
        )
        return _tag(tag)


@mcp.tool()
async def create_project(
    token: str,
    org_id: str,
    name: str,
    client_tag_id: str | None = None,
    tariffa: float | None = None,
    valuta: str = "EUR",
) -> dict[str, Any]:
    """Create a project tag with billing profile."""
    async with _tenant(token, org_id) as (s, org, user):
        tag = await taxonomy.create_project(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            client_tag_id=(uuid.UUID(client_tag_id) if client_tag_id else None),
            tariffa=Decimal(str(tariffa)) if tariffa is not None else None,
            valuta=valuta,
        )
        return _tag(tag)


@mcp.tool()
async def list_tags(token: str, org_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    """List tags, optionally filtered by kind."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await taxonomy.list_tags(s, org_id=org, kind=TagKind(kind) if kind else None)
        return [_tag(t) for t in rows]


@mcp.tool()
async def create_task(
    token: str,
    org_id: str,
    title: str,
    description: str | None = None,
    priority: int = 3,
    tag_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create a task, optionally tagged."""
    async with _tenant(token, org_id) as (s, org, user):
        task = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title=title,
            description=description,
            priority=priority,
            tag_ids=[uuid.UUID(t) for t in (tag_ids or [])],
        )
        return _task(task)


@mcp.tool()
async def list_tasks(token: str, org_id: str, state_id: str | None = None) -> list[dict[str, Any]]:
    """List tasks, optionally filtered by workflow state id."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await tasks.list_tasks(
            s,
            org_id=org,
            state_id=uuid.UUID(state_id) if state_id else None,
        )
        return [_task(t) for t in rows]


@mcp.tool()
async def add_comment(token: str, org_id: str, task_id: str, body: str) -> dict[str, Any]:
    """Add a comment to a task."""
    async with _tenant(token, org_id) as (s, org, user):
        c = await tasks.add_comment(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            body=body,
        )
        return {"id": str(c.id), "task_id": str(c.task_id)}


@mcp.tool()
async def add_dependency(
    token: str,
    org_id: str,
    predecessor_id: str,
    successor_id: str,
    type: str,
    lag_working_minutes: int = 0,
) -> dict[str, Any]:
    """Add a typed task dependency (FS/SS/FF/SF). Cycles are rejected."""
    async with _tenant(token, org_id) as (s, org, user):
        d = await dependencies.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=uuid.UUID(predecessor_id),
            successor_id=uuid.UUID(successor_id),
            type=DependencyType(type),
            lag_working_minutes=lag_working_minutes,
        )
        return {"id": str(d.id), "type": d.type.value}


@mcp.tool()
async def graph(token: str, org_id: str, project_tag_id: str | None = None) -> dict[str, Any]:
    """Return the dependency DAG (nodes + edges) for a scope."""
    async with _tenant(token, org_id) as (s, org, _user):
        return await dependencies.graph(
            s,
            org_id=org,
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
        )


@mcp.tool()
async def set_task_state(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    state_id: str,
) -> dict[str, Any]:
    """Transition a task to a workflow state (validated)."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            state_id=uuid.UUID(state_id),
        )
        return {"task_id": task_id, "version": version}


# --- F3: calendars, events, deterministic schedule (FR-4) ---


def _event(e: Event) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "title": e.title,
        "start_at": e.start_at.isoformat(),
        "end_at": e.end_at.isoformat(),
        "version": e.version,
    }


def _schedule(s: Schedule) -> dict[str, Any]:
    return {
        "task_id": str(s.task_id),
        "es": s.es.isoformat() if s.es else None,
        "ef": s.ef.isoformat() if s.ef else None,
        "ls": s.ls.isoformat() if s.ls else None,
        "lf": s.lf.isoformat() if s.lf else None,
        "slack_minutes": s.slack_minutes,
        "on_logical_critical_path": s.on_logical_critical_path,
        "scheduled_start": (s.scheduled_start.isoformat() if s.scheduled_start else None),
        "scheduled_end": (s.scheduled_end.isoformat() if s.scheduled_end else None),
        "input_fingerprint": s.input_fingerprint,
    }


@mcp.tool()
async def create_calendar(
    token: str,
    org_id: str,
    name: str,
    weekly_hours: dict[str, list[list[str]]],
    timezone: str = "Europe/Rome",
) -> dict[str, Any]:
    """Create a working calendar (weekday -> [start, end] HH:MM windows)."""
    async with _tenant(token, org_id) as (s, org, user):
        cal = await calendars.create_calendar(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            timezone=timezone,
            weekly_hours=weekly_hours,
        )
        return {"id": str(cal.id), "name": cal.name, "version": cal.version}


@mcp.tool()
async def list_calendars(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the org working calendars."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await calendars.list_calendars(s, org_id=org)
        return [
            {
                "id": str(c.id),
                "name": c.name,
                "is_default": c.is_default,
                "timezone": c.timezone,
            }
            for c in rows
        ]


@mcp.tool()
async def add_holiday(token: str, org_id: str, calendar_id: str, day: str) -> dict[str, Any]:
    """Add a holiday (ISO date) to a calendar; idempotent."""
    async with _tenant(token, org_id) as (s, org, user):
        await calendars.add_holiday(
            s,
            org_id=org,
            actor_id=user,
            calendar_id=uuid.UUID(calendar_id),
            day=dt.date.fromisoformat(day),
        )
        return {"calendar_id": calendar_id, "day": day}


@mcp.tool()
async def set_user_calendar(
    token: str,
    org_id: str,
    user_id: str,
    calendar_id: str,
    daily_capacity_h: float,
) -> dict[str, Any]:
    """Assign a calendar + daily capacity (hours) to a user."""
    async with _tenant(token, org_id) as (s, org, user):
        await calendars.set_user_calendar(
            s,
            org_id=org,
            actor_id=user,
            user_id=uuid.UUID(user_id),
            calendar_id=uuid.UUID(calendar_id),
            daily_capacity_h=Decimal(str(daily_capacity_h)),
        )
        return {"user_id": user_id, "calendar_id": calendar_id}


@mcp.tool()
async def create_event(
    token: str,
    org_id: str,
    title: str,
    start_at: str,
    end_at: str,
    participant_ids: list[str] | None = None,
    project_tag_id: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Create an appointment. Overlap for any participant is rejected
    (no-ubiquity)."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await events_svc.create_event(
            s,
            org_id=org,
            actor_id=user,
            title=title,
            start_at=dt.datetime.fromisoformat(start_at),
            end_at=dt.datetime.fromisoformat(end_at),
            participant_ids=[uuid.UUID(p) for p in (participant_ids or [])],
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
            location=location,
        )
        return _event(e)


@mcp.tool()
async def list_events(token: str, org_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
    """List appointments, optionally filtered by participant."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await events_svc.list_events(
            s,
            org_id=org,
            user_id=uuid.UUID(user_id) if user_id else None,
        )
        return [_event(e) for e in rows]


@mcp.tool()
async def reschedule_event(
    token: str,
    org_id: str,
    event_id: str,
    start_at: str,
    end_at: str,
    expected_version: int,
) -> dict[str, Any]:
    """Move an appointment; overlap for any participant is rejected."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await events_svc.reschedule_event(
            s,
            org_id=org,
            actor_id=user,
            event_id=uuid.UUID(event_id),
            start_at=dt.datetime.fromisoformat(start_at),
            end_at=dt.datetime.fromisoformat(end_at),
            expected_version=expected_version,
        )
        return {"event_id": event_id, "version": version}


@mcp.tool()
async def delete_event(token: str, org_id: str, event_id: str) -> dict[str, Any]:
    """Delete an appointment."""
    async with _tenant(token, org_id) as (s, org, user):
        await events_svc.delete_event(s, org_id=org, actor_id=user, event_id=uuid.UUID(event_id))
        return {"event_id": event_id, "deleted": True}


@mcp.tool()
async def set_task_schedule(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    schedule_mode: str | None = None,
    constraint_kind: str | None = None,
    constraint_date: str | None = None,
    remaining_effort_h: float | None = None,
    actual_start: str | None = None,
    is_milestone: bool | None = None,
) -> dict[str, Any]:
    """Write-back scheduler pins/constraints; survives recompute (FR-4)."""
    values: dict[str, Any] = {}
    if schedule_mode is not None:
        values["schedule_mode"] = ScheduleMode(schedule_mode)
    if constraint_kind is not None:
        values["constraint_kind"] = ConstraintKind(constraint_kind)
    if constraint_date is not None:
        values["constraint_date"] = dt.datetime.fromisoformat(constraint_date)
    if remaining_effort_h is not None:
        values["remaining_effort_h"] = Decimal(str(remaining_effort_h))
    if actual_start is not None:
        values["actual_start"] = dt.datetime.fromisoformat(actual_start)
    if is_milestone is not None:
        values["is_milestone"] = is_milestone
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.set_schedule_fields(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            values=values,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def recompute_schedule(
    token: str,
    org_id: str,
    project_tag_id: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Deterministically recompute the schedule for a scope."""
    async with _tenant(token, org_id) as (s, org, user):
        count = await scheduler.recompute(
            s,
            org_id=org,
            actor_id=user,
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
            as_of=dt.datetime.fromisoformat(as_of) if as_of else None,
        )
        return {"count": count}


@mcp.tool()
async def get_schedule(token: str, org_id: str, task_id: str) -> dict[str, Any] | None:
    """Read one task's derived schedule row."""
    async with _tenant(token, org_id) as (s, org, _user):
        row = await scheduler.get_schedule(s, org_id=org, task_id=uuid.UUID(task_id))
        return _schedule(row) if row is not None else None


@mcp.tool()
async def list_schedule(
    token: str, org_id: str, project_tag_id: str | None = None
) -> list[dict[str, Any]]:
    """List derived schedule rows for a scope."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await scheduler.list_schedule(
            s,
            org_id=org,
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
        )
        return [_schedule(r) for r in rows]


# --- F4: time tracking (FR-5) ---


def _time_entry(e: TimeEntry) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "task_id": str(e.task_id),
        "user_id": str(e.user_id),
        "started_at": e.started_at.isoformat(),
        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
        "duration_seconds": e.duration_seconds,
        "source": e.source.value,
        "billable": e.billable,
        "rate_snapshot": (str(e.rate_snapshot) if e.rate_snapshot is not None else None),
        "currency": e.currency,
        "note": e.note,
        "version": e.version,
    }


@mcp.tool()
async def start_timer(
    token: str,
    org_id: str,
    task_id: str,
    billable: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    """Start the live timer for a task. One running timer per user is
    enforced; a second start is rejected."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.start_timer(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            billable=billable,
            note=note,
        )
        return _time_entry(e)


@mcp.tool()
async def stop_timer(token: str, org_id: str, note: str | None = None) -> dict[str, Any]:
    """Stop the running timer; computes the duration."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.stop_timer(s, org_id=org, actor_id=user, note=note)
        return _time_entry(e)


@mcp.tool()
async def add_time_entry(
    token: str,
    org_id: str,
    task_id: str,
    started_at: str,
    ended_at: str | None = None,
    duration_seconds: int | None = None,
    billable: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    """Add a manual time entry (provide ended_at or duration_seconds)."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            started_at=dt.datetime.fromisoformat(started_at),
            ended_at=dt.datetime.fromisoformat(ended_at) if ended_at else None,
            duration_seconds=duration_seconds,
            billable=billable,
            note=note,
        )
        return _time_entry(e)


@mcp.tool()
async def list_time_entries(
    token: str,
    org_id: str,
    task_id: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """List time entries, optionally filtered by task or user."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await time_svc.list_entries(
            s,
            org_id=org,
            task_id=uuid.UUID(task_id) if task_id else None,
            user_id=uuid.UUID(user_id) if user_id else None,
        )
        return [_time_entry(e) for e in rows]


@mcp.tool()
async def time_report(
    token: str,
    org_id: str,
    group_by: str = "project",
    billable: bool | None = None,
) -> list[dict[str, Any]]:
    """Aggregated time report grouped by project|client|generic|user|task."""
    async with _tenant(token, org_id) as (s, org, user):
        rows = await time_svc.report(
            s,
            org_id=org,
            actor_id=user,
            group_by=ReportGroup(group_by),
            billable=billable,
        )
        return [
            {
                "key": r.key,
                "label": r.label,
                "seconds": r.seconds,
                "billable_seconds": r.billable_seconds,
                "amount": str(r.amount),
                "currency": r.currency,
            }
            for r in rows
        ]
