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
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core import __version__
from flow_core.db import admin_session, tenant_session
from flow_core.embedder import embedder_available
from flow_core.errors import AuthError, DomainError, ForbiddenError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.agent_run import AgentRun
from flow_core.models.billing import CostBasis, RateCard, UsageRecord
from flow_core.models.budget import Budget, BudgetPeriod
from flow_core.models.client_profile import ClientProfile
from flow_core.models.dependency import DependencyType, TaskDependency
from flow_core.models.dispatch_request import DispatchRequest
from flow_core.models.email import EmailAccount, EmailMessage, EmailProvider
from flow_core.models.executor import Executor, ExecutorKind
from flow_core.models.invoice import Invoice
from flow_core.models.memory_blob import MemoryBlob
from flow_core.models.note import Note, NoteKind, NoteTurn
from flow_core.models.notification import NotificationChannelKind, RecurrenceFreq
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.schedule import Schedule
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import (
    ConstraintKind,
    Necessity,
    ScheduleMode,
    SchedulePolicy,
    Task,
)
from flow_core.models.task_handoff import TaskHandoff
from flow_core.models.time_entry import TimeEntry
from flow_core.models.user import User
from flow_core.models.workflow import WorkflowDefinition, WorkflowState, WorkflowTransition
from flow_core.security import decode_token_async
from flow_core.services import advisory as advisory_svc
from flow_core.services import agent_runtime as agent_runtime_svc
from flow_core.services import attachments as attachments_svc
from flow_core.services import billing as billing_svc
from flow_core.services import budgets as budgets_svc
from flow_core.services import calendar as calendars
from flow_core.services import coordination as coordination_svc
from flow_core.services import dependencies, scheduler, tasks, taxonomy
from flow_core.services import dispatch_loop as dispatch_loop_svc
from flow_core.services import email as email_svc
from flow_core.services import executors as executors_svc
from flow_core.services import invoice as invoice_svc
from flow_core.services import memory as memory_svc
from flow_core.services import note_links as note_links_svc
from flow_core.services import notes as notes_svc
from flow_core.services import notifications as notif_svc
from flow_core.services import time_tracking as time_svc
from flow_core.services import workflow as workflow_svc
from flow_core.services.rbac import get_role
from flow_core.services.taxonomy import ClientInput
from flow_core.services.time_tracking import ReportGroup
from flow_core.services.workflow import StateEdit, StateSpec

mcp: FastMCP = FastMCP("flow")


# Per-request principal published by the HTTP transport's bearer
# middleware (``server_http.py``). When set, ``_tenant`` reads
# ``(user_id, org_id, token_id)`` from here and skips both the JWT
# decode and the claims/positional org resolution — the bearer was
# already validated at the HTTP boundary via
# ``authenticate_agent_token`` (migration 0059) so the principal is
# trustworthy. The third element is the ``agent_tokens.id`` of the
# token used (always populated under HTTP; the bearer must be a
# ``flow_at_…`` agent token). ``None`` for the stdio transport, which
# keeps using the legacy positional-args flow with a plain JWT.
_PRINCIPAL: ContextVar[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None] | None] = ContextVar(
    "_PRINCIPAL", default=None
)


@asynccontextmanager
async def _tenant(
    token: str = "", org_id: str = ""
) -> AsyncIterator[tuple[AsyncSession, uuid.UUID, uuid.UUID]]:
    """Open a tenant session for the duration of the call.

    Two code paths:
    - HTTP transport: the bearer middleware in ``server_http`` populated
      ``_PRINCIPAL`` with the authenticated ``(user_id, org_id,
      token_id)``; the agent-token nature lets us tag the audit log
      as ``actor_kind='mcp_token'`` with ``actor_subject_id`` = token id.
    - stdio transport (legacy): the caller passes a session JWT (a
      human's bearer used outside the SPA) plus ``org_id`` as
      positional args; tagged ``actor_kind='human_api'``.
    """
    principal = _PRINCIPAL.get()
    if principal is not None:
        user_id, org, token_id = principal
        actor_kind = "mcp_token"
        actor_subject = str(token_id) if token_id is not None else None
    else:
        claims = await decode_token_async(token)
        sub = claims.get("sub")
        if not isinstance(sub, str):
            raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
        user_id = uuid.UUID(sub)
        token_org = claims.get("org_id")
        if isinstance(token_org, str) and token_org:
            org = uuid.UUID(token_org)
        else:
            org = uuid.UUID(org_id)
        actor_kind = "human_api"
        actor_subject = None
    async with tenant_session(
        str(org),
        str(user_id),
        actor_kind=actor_kind,
        actor_subject_id=actor_subject,
    ) as session:
        await get_role(session, org, user_id)  # raises if not a member
        yield session, org, user_id


async def _resolve_agent_context(
    session: AsyncSession, org: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Return ``(ai_assistant_identity_id, agent_token_id)`` for the
    current MCP call, or ``(None, None)`` for stdio / human-bearer
    requests. The identity is resolved through
    ``agent_tokens.assistant_id -> ai_assistants -> identities`` and
    is therefore ``None`` for bare tokens (assistant_id IS NULL,
    pre-migration 0059). The token id is returned in both cases so
    AI authorship survives a bare token too — ``agent_tokens.name``
    becomes the display label in the serializer (migration 0093)."""
    principal = _PRINCIPAL.get()
    if principal is None:
        return None, None
    _user_id, _org, token_id = principal
    if token_id is None:
        return None, None
    from sqlalchemy import select as _sel

    from flow_core.models.agent_token import AgentToken
    from flow_core.models.ai_assistant import AiAssistant
    from flow_core.models.identity import Identity

    row = await session.execute(
        _sel(Identity.id)
        .join(AiAssistant, AiAssistant.id == Identity.ai_assistant_id)
        .join(AgentToken, AgentToken.assistant_id == AiAssistant.id)
        .where(
            AgentToken.id == token_id,
            Identity.org_id == org,
        )
    )
    identity_id = row.scalar_one_or_none()
    return identity_id, token_id


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


def _tag_brief(t: Tag) -> dict[str, Any]:
    """Tag chip as carried on a task/note (mirrors the REST TagBrief):
    id/kind/name/color, no version. Lets MCP callers see and reason
    about a task's tags without a follow-up list_tags round-trip."""
    return {
        "id": str(t.id),
        "kind": t.kind.value,
        "name": t.name,
        "color": t.color,
    }


def _task(t: Task, tags: list[Tag] | None = None) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "title": t.title,
        "state_id": str(t.state_id),
        "priority": t.priority,
        "version": t.version,
        "tags": [_tag_brief(g) for g in (tags or [])],
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
) -> dict[str, Any]:
    """Create a project under a client. Rate/billable are client-level
    (set on the client); the project carries name/budget/description."""
    async with _tenant(token, org_id) as (s, org, user):
        tag = await taxonomy.create_project(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            client_tag_id=(uuid.UUID(client_tag_id) if client_tag_id else None),
        )
        return _tag(tag)


@mcp.tool()
async def list_tags(token: str, org_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    """List tags, optionally filtered by kind."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await taxonomy.list_tags(s, org_id=org, kind=TagKind(kind) if kind else None)
        return [_tag(t) for t in rows]


def _client(t: Tag, p: ClientProfile) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "status": t.status,
        "version": t.version,
        "ragione_sociale": p.ragione_sociale,
        "id_paese": p.id_paese,
        "id_codice": p.id_codice,
        "codice_fiscale": p.codice_fiscale,
        "indirizzo": p.indirizzo,
        "cap": p.cap,
        "comune": p.comune,
        "provincia": p.provincia,
        "nazione": p.nazione,
        "codice_destinatario": p.codice_destinatario,
        "pec": p.pec,
        "description": p.description,
        "default_billable": p.default_billable,
        "tariffa": str(p.tariffa) if p.tariffa is not None else None,
        "valuta": p.valuta,
    }


def _project(t: Tag, p: ProjectProfile) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "status": t.status,
        "version": t.version,
        "client_tag_id": str(p.client_tag_id) if p.client_tag_id else None,
        "budget": str(p.budget) if p.budget is not None else None,
        "color": t.color,
        "description": p.description,
        "workflow_id": str(p.workflow_id) if p.workflow_id else None,
    }


@mcp.tool()
async def list_clients(token: str, org_id: str) -> list[dict[str, Any]]:
    """List clients with their invoicing profile."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await taxonomy.list_clients(s, org_id=org)
        return [_client(t, p) for t, p in rows]


@mcp.tool()
async def list_projects(token: str, org_id: str) -> list[dict[str, Any]]:
    """List projects with their profile (client link, budget, color)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await taxonomy.list_projects(s, org_id=org)
        return [_project(t, p) for t, p in rows]


@mcp.tool()
async def get_tag(token: str, org_id: str, tag_id: str) -> dict[str, Any]:
    """Read one tag (generic/client/project)."""
    async with _tenant(token, org_id) as (s, org, _user):
        t = await taxonomy.get_tag(s, org_id=org, tag_id=uuid.UUID(tag_id))
        return _tag(t)


@mcp.tool()
async def update_tag(
    token: str,
    org_id: str,
    tag_id: str,
    expected_version: int,
    name: str | None = None,
    color: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Rename / recolor / set status of a tag (status: active|archived)."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await taxonomy.update_tag(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(tag_id),
            expected_version=expected_version,
            name=name,
            color=color,
            status=status,
        )
        return {"tag_id": tag_id, "version": version}


@mcp.tool()
async def update_client(
    token: str,
    org_id: str,
    tag_id: str,
    name: str | None = None,
    ragione_sociale: str | None = None,
    id_paese: str | None = None,
    id_codice: str | None = None,
    codice_fiscale: str | None = None,
    indirizzo: str | None = None,
    cap: str | None = None,
    comune: str | None = None,
    provincia: str | None = None,
    nazione: str | None = None,
    codice_destinatario: str | None = None,
    pec: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Edit a client's name and invoicing card. Only the given fields
    are changed."""
    # Widened to match the taxonomy.update_client signature (dict is
    # invariant: dict[str, str | None] is not a subtype of dict[str,
    # object]). The MCP tool still only exposes string fields, so the
    # widened type is over-permissive in this caller — that's fine.
    fields: dict[str, object] = {}
    for key, val in (
        ("ragione_sociale", ragione_sociale),
        ("id_paese", id_paese),
        ("id_codice", id_codice),
        ("codice_fiscale", codice_fiscale),
        ("indirizzo", indirizzo),
        ("cap", cap),
        ("comune", comune),
        ("provincia", provincia),
        ("nazione", nazione),
        ("codice_destinatario", codice_destinatario),
        ("pec", pec),
        ("description", description),
    ):
        if val is not None:
            fields[key] = val
    async with _tenant(token, org_id) as (s, org, user):
        await taxonomy.update_client(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(tag_id),
            name=name,
            fields=fields,
        )
        return {"tag_id": tag_id, "updated": True}


@mcp.tool()
async def update_project(
    token: str,
    org_id: str,
    tag_id: str,
    name: str | None = None,
    client_tag_id: str | None = None,
    budget: float | None = None,
    color: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Edit a project. Pass ``client_tag_id`` to reassign the project
    to another client. Only the given fields are changed."""
    fields: dict[str, object] = {}
    if client_tag_id is not None:
        fields["client_tag_id"] = uuid.UUID(client_tag_id)
    if budget is not None:
        fields["budget"] = Decimal(str(budget))
    if color is not None:
        fields["color"] = color
    if description is not None:
        fields["description"] = description
    async with _tenant(token, org_id) as (s, org, user):
        await taxonomy.update_project(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(tag_id),
            name=name,
            fields=fields,
        )
        return {"tag_id": tag_id, "updated": True}


@mcp.tool()
async def set_tag_scope(
    token: str, org_id: str, tag_id: str, target_ids: list[str]
) -> dict[str, Any]:
    """Replace a tag's scope with the given project/client tag ids
    (empty list = global / visible everywhere). Admin."""
    async with _tenant(token, org_id) as (s, org, user):
        await taxonomy.set_tag_scope(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(tag_id),
            target_ids=[uuid.UUID(t) for t in target_ids],
        )
        return {"tag_id": tag_id, "targets": len(target_ids)}


@mcp.tool()
async def create_task(
    token: str,
    org_id: str,
    title: str,
    description: str | None = None,
    priority: int = 3,
    importance: int | None = None,
    urgency: int | None = None,
    tag_ids: list[str] | None = None,
    estimate_effort_h: float | None = None,
    required_capabilities: list[str] | None = None,
    monetary_cost: float | None = None,
    location: str | None = None,
    necessity: str | None = None,
    budget_id: str | None = None,
    assignee_ids: list[str] | None = None,
    assignee_id: str | None = None,
    start_at: str | None = None,
    duration_minutes: int | None = None,
    recurrence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a task, optionally tagged. Supports personal-domain
    attributes (cost/location/necessity/budget) for the advisory layer.
    ``required_capabilities`` (docs/adr/0025 P2) are the capabilities the
    task needs from its executor (empty = any enabled agent). Pass
    ``start_at`` + ``duration_minutes`` to create an appointment-task
    (migration 0094 + ADR-0008 addendum): the task becomes a calendar
    block subject to no-overlap on ``assignee_id`` (and any explicit
    participants added via ``add_task_participant``)."""
    async with _tenant(token, org_id) as (s, org, user):
        # When the MCP call is authenticated with an agent token
        # (HTTP transport), record the ai_assistant identity (if the
        # token is bound to one) AND the token id itself, so /tasks
        # can render an AI badge whether or not the token has been
        # upgraded to the ai_assistants flow (migrations 0059 / 0091
        # / 0093 in concert).
        creator_identity_id, creator_token_id = await _resolve_agent_context(s, org)
        task = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title=title,
            description=description,
            priority=priority,
            importance=importance,
            urgency=urgency,
            estimate_effort_h=(
                Decimal(str(estimate_effort_h)) if estimate_effort_h is not None else None
            ),
            required_capabilities=list(required_capabilities or []),
            monetary_cost=(Decimal(str(monetary_cost)) if monetary_cost is not None else None),
            location=location,
            necessity=Necessity(necessity) if necessity is not None else Necessity.should,
            budget_id=uuid.UUID(budget_id) if budget_id else None,
            tag_ids=[uuid.UUID(t) for t in (tag_ids or [])],
            assignee_ids=[uuid.UUID(u) for u in (assignee_ids or [])],
            assignee_id=uuid.UUID(assignee_id) if assignee_id else None,
            created_by_identity_id=creator_identity_id,
            created_by_token_id=creator_token_id,
            start_at=dt.datetime.fromisoformat(start_at) if start_at else None,
            duration_minutes=duration_minutes,
            recurrence=recurrence,
        )
        return _task(task)


@mcp.tool()
async def list_tasks(
    token: str,
    org_id: str,
    state_id: str | None = None,
    assignee_kind: str | None = None,
    assignee_handles: list[str] | None = None,
    owner_handles: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List tasks, optionally filtered by workflow state id."""
    # docs/adr/0028 Punto 4: identity-axis filters. ``assignee_kind``
    # accepts ``user`` or ``ai_assistant``; ``assignee_handles`` /
    # ``owner_handles`` are multi-select on the respective handles.
    from flow_core.models.identity import IdentityKind

    kind: IdentityKind | None = IdentityKind(assignee_kind) if assignee_kind else None
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await tasks.list_tasks(
            s,
            org_id=org,
            state_id=uuid.UUID(state_id) if state_id else None,
            assignee_kind=kind,
            assignee_handles=assignee_handles,
            owner_handles=owner_handles,
        )
        tagmap = await tasks.tags_by_task(s, task_ids=[t.id for t in rows])
        return [_task(t, tagmap.get(t.id, [])) for t in rows]


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


def _task_full(t: Task, tags: list[Tag] | None = None) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "title": t.title,
        "description": t.description,
        "state_id": str(t.state_id),
        "priority": t.priority,
        "importance": t.importance,
        "urgency": t.urgency,
        "start_date": t.start_date.isoformat() if t.start_date else None,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "billable": t.billable,
        "parent_task_id": (str(t.parent_task_id) if t.parent_task_id else None),
        "estimate_effort_h": (
            str(t.estimate_effort_h) if t.estimate_effort_h is not None else None
        ),
        "required_capabilities": list(t.required_capabilities or []),
        "monetary_cost": (str(t.monetary_cost) if t.monetary_cost is not None else None),
        "location": t.location,
        "necessity": t.necessity.value,
        "budget_id": str(t.budget_id) if t.budget_id else None,
        "is_archived": t.is_archived,
        "offered": t.offered,
        "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
        "version": t.version,
        "tags": [_tag_brief(g) for g in (tags or [])],
    }


@mcp.tool()
async def get_task(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Read one task with its full attribute set (for editing)."""
    async with _tenant(token, org_id) as (s, org, _user):
        t = await tasks.get_task(s, org_id=org, task_id=uuid.UUID(task_id))
        tagmap = await tasks.tags_by_task(s, task_ids=[t.id])
        return _task_full(t, tagmap.get(t.id, []))


@mcp.tool()
async def update_task(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    title: str | None = None,
    description: str | None = None,
    priority: int | None = None,
    importance: int | None = None,
    urgency: int | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    billable: bool | None = None,
    estimate_effort_h: float | None = None,
    required_capabilities: list[str] | None = None,
    parent_task_id: str | None = None,
    monetary_cost: float | None = None,
    location: str | None = None,
    necessity: str | None = None,
    budget_id: str | None = None,
) -> dict[str, Any]:
    """Edit task fields (only the given ones). Priority is re-derived
    when both importance and urgency are present (Eisenhower).
    ``required_capabilities`` is the P2 executor capability requirement
    (docs/adr/0025); pass [] to clear it."""
    values: dict[str, Any] = {}
    if title is not None:
        values["title"] = title
    if description is not None:
        values["description"] = description
    if priority is not None:
        values["priority"] = priority
    if importance is not None:
        values["importance"] = importance
    if urgency is not None:
        values["urgency"] = urgency
    if start_date is not None:
        values["start_date"] = dt.date.fromisoformat(start_date)
    if due_date is not None:
        values["due_date"] = dt.date.fromisoformat(due_date)
    if billable is not None:
        values["billable"] = billable
    if estimate_effort_h is not None:
        values["estimate_effort_h"] = Decimal(str(estimate_effort_h))
    if required_capabilities is not None:
        values["required_capabilities"] = list(required_capabilities)
    if parent_task_id is not None:
        values["parent_task_id"] = uuid.UUID(parent_task_id)
    if monetary_cost is not None:
        values["monetary_cost"] = Decimal(str(monetary_cost))
    if location is not None:
        values["location"] = location
    if necessity is not None:
        values["necessity"] = Necessity(necessity)
    if budget_id is not None:
        values["budget_id"] = uuid.UUID(budget_id)
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            values=values,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def archive_task(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    archived: bool = True,
) -> dict[str, Any]:
    """Archive (or unarchive with ``archived=False``) a task."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.archive_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            archived=archived,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def delete_task(
    token: str, org_id: str, task_id: str, expected_version: int
) -> dict[str, Any]:
    """Soft-delete a task (recoverable via restore_task)."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.soft_delete_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def restore_task(
    token: str, org_id: str, task_id: str, expected_version: int
) -> dict[str, Any]:
    """Restore a soft-deleted task."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.restore_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def add_task_tag(token: str, org_id: str, task_id: str, tag_id: str) -> dict[str, Any]:
    """Attach a tag to a task (idempotent). Use a project tag to move
    the task into a project, or a generic/client tag to label it."""
    async with _tenant(token, org_id) as (s, org, user):
        await tasks.attach_tag(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            tag_id=uuid.UUID(tag_id),
        )
        return {"task_id": task_id, "tag_id": tag_id}


@mcp.tool()
async def remove_task_tag(token: str, org_id: str, task_id: str, tag_id: str) -> dict[str, Any]:
    """Detach a tag from a task."""
    async with _tenant(token, org_id) as (s, org, user):
        await tasks.detach_tag(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            tag_id=uuid.UUID(tag_id),
        )
        return {"task_id": task_id, "tag_id": tag_id, "removed": True}


@mcp.tool()
async def move_task_to_project(
    token: str, org_id: str, task_id: str, project_tag_id: str
) -> dict[str, Any]:
    """Reassign a task to another project: detach its current project
    tag(s) and attach the new one. Composed from tag operations; the
    task's client follows from the project."""
    async with _tenant(token, org_id) as (s, org, user):
        new_project = uuid.UUID(project_tag_id)
        tagmap = await tasks.tags_by_task(s, task_ids=[uuid.UUID(task_id)])
        for tag in tagmap.get(uuid.UUID(task_id), []):
            if tag.kind is TagKind.project and tag.id != new_project:
                await tasks.detach_tag(
                    s,
                    org_id=org,
                    actor_id=user,
                    task_id=uuid.UUID(task_id),
                    tag_id=tag.id,
                )
        await tasks.attach_tag(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            tag_id=new_project,
        )
        return {"task_id": task_id, "project_tag_id": project_tag_id}


@mcp.tool()
async def assign_task(token: str, org_id: str, task_id: str, user_id: str) -> dict[str, Any]:
    """Assign a user to a task (idempotent)."""
    async with _tenant(token, org_id) as (s, org, user):
        await tasks.assign(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            user_id=uuid.UUID(user_id),
        )
        return {"task_id": task_id, "user_id": user_id}


@mcp.tool()
async def unassign_task(token: str, org_id: str, task_id: str, user_id: str) -> dict[str, Any]:
    """Unassign a user from a task."""
    async with _tenant(token, org_id) as (s, org, user):
        await tasks.unassign(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            user_id=uuid.UUID(user_id),
        )
        return {"task_id": task_id, "user_id": user_id, "removed": True}


@mcp.tool()
async def list_comments(token: str, org_id: str, task_id: str) -> list[dict[str, Any]]:
    """List a task's comments, oldest first."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await tasks.list_comments(s, org_id=org, task_id=uuid.UUID(task_id))
        return [
            {
                "id": str(c.id),
                "task_id": str(c.task_id),
                "user_id": str(c.user_id) if c.user_id else None,
                "body": c.body,
                "created_at": c.created_at.isoformat(),
            }
            for c in rows
        ]


# --- task dependencies: remove + list (FR-3) ---


def _dependency(d: TaskDependency) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "predecessor_id": str(d.predecessor_id),
        "successor_id": str(d.successor_id),
        "type": d.type.value,
        "lag_working_minutes": d.lag_working_minutes,
        "version": d.version,
    }


@mcp.tool()
async def list_dependencies(
    token: str, org_id: str, task_id: str | None = None
) -> list[dict[str, Any]]:
    """List task dependencies, optionally only those touching a task."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await dependencies.list_dependencies(
            s,
            org_id=org,
            task_id=uuid.UUID(task_id) if task_id else None,
        )
        return [_dependency(d) for d in rows]


@mcp.tool()
async def remove_dependency(token: str, org_id: str, dependency_id: str) -> dict[str, Any]:
    """Remove a task dependency edge."""
    async with _tenant(token, org_id) as (s, org, user):
        await dependencies.remove_dependency(
            s,
            org_id=org,
            actor_id=user,
            dependency_id=uuid.UUID(dependency_id),
        )
        return {"dependency_id": dependency_id, "removed": True}


# --- F3: calendars + deterministic schedule (FR-4) ---
# Events were unified into tasks in migration 0094; their MCP surface
# (the four ``*_event`` tools and the ``_event`` helper) is gone.
# Appointments are tasks with ``start_at`` + ``duration_minutes``.


def _schedule(s: Schedule) -> dict[str, Any]:
    return {
        "task_id": str(s.task_id),
        "es": s.es.isoformat() if s.es else None,
        "ef": s.ef.isoformat() if s.ef else None,
        "ls": s.ls.isoformat() if s.ls else None,
        "lf": s.lf.isoformat() if s.lf else None,
        "slack_minutes": s.slack_minutes,
        "on_logical_critical_path": s.on_logical_critical_path,
        "on_critical_chain": s.on_critical_chain,
        "projected_cost": str(s.projected_cost),
        "scheduled_start": (s.scheduled_start.isoformat() if s.scheduled_start else None),
        "scheduled_end": (s.scheduled_end.isoformat() if s.scheduled_end else None),
        "assigned_executor_id": (str(s.assigned_executor_id) if s.assigned_executor_id else None),
        "unassignable": s.unassignable,
        "unassignable_reason": s.unassignable_reason,
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
async def list_holidays(token: str, org_id: str, calendar_id: str) -> list[dict[str, Any]]:
    """List a calendar's holidays (ascending)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await calendars.list_holidays(s, org_id=org, calendar_id=uuid.UUID(calendar_id))
        return [{"calendar_id": calendar_id, "day": d.isoformat()} for d in rows]


@mcp.tool()
async def remove_holiday(token: str, org_id: str, calendar_id: str, day: str) -> dict[str, Any]:
    """Remove a holiday (ISO date) from a calendar."""
    async with _tenant(token, org_id) as (s, org, user):
        await calendars.remove_holiday(
            s,
            org_id=org,
            actor_id=user,
            calendar_id=uuid.UUID(calendar_id),
            day=dt.date.fromisoformat(day),
        )
        return {"calendar_id": calendar_id, "day": day, "removed": True}


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


# Migration 0097 dropped the standalone events table. Appointments are
# tasks with ``start_at`` + ``duration_minutes`` -- AI agents create
# them through ``create_task`` and the participants endpoint /
# add_participant tool. The four legacy ``*_event`` MCP tools were
# removed in this commit.


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
    policy: str | None = None,
) -> dict[str, Any]:
    """Deterministically recompute the schedule for a scope under a
    resource-leveling ``policy`` (fastest|cheapest|balanced|throughput,
    default balanced). Returns the row count plus the projected makespan
    and projected credit cost so policies are comparable, and the count
    of llm tasks with no admissible executor (P2 dispatch gaps;
    ADR-0025)."""
    async with _tenant(token, org_id) as (s, org, user):
        summary = await scheduler.recompute(
            s,
            org_id=org,
            actor_id=user,
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
            as_of=dt.datetime.fromisoformat(as_of) if as_of else None,
            policy=(SchedulePolicy(policy) if policy else SchedulePolicy.balanced),
        )
        return {
            "count": summary.count,
            "makespan_minutes": summary.makespan_minutes,
            "projected_credit_cost": str(summary.projected_credit_cost),
            "policy": summary.policy.value,
            "unassignable_count": summary.unassignable_count,
        }


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


# --- Executor registry (docs/adr/0025, P2) ---
# Co-equal to the REST surface. Reads are member-level; mutations are
# owner-gated inside the service (the RBAC choke point + effective-role
# sudo), mirroring the rate-card / issuer-profile tools.


def _executor(e: Executor) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "kind": e.kind.value,
        "name": e.name,
        "user_id": str(e.user_id) if e.user_id else None,
        "context_switch_cost_minutes": e.context_switch_cost_minutes,
        "provider": e.provider,
        "model_id": e.model_id,
        "max_parallel": e.max_parallel,
        "credit_budget": (str(e.credit_budget) if e.credit_budget is not None else None),
        "credit_rate_per_hour": str(e.credit_rate_per_hour),
        "enabled": e.enabled,
        "capability_tags": list(e.capability_tags or []),
        "version": e.version,
    }


@mcp.tool()
async def executors_list(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the workspace executors (humans + llm agents). Member-level
    (the schedule plan must show its assignments)."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_executor(e) for e in await executors_svc.list_executors(s, org_id=org)]


@mcp.tool()
async def executor_create(
    token: str,
    org_id: str,
    name: str,
    kind: str = "llm_agent",
    user_id: str | None = None,
    context_switch_cost_minutes: int = 0,
    provider: str | None = None,
    model_id: str | None = None,
    max_parallel: int = 4,
    credit_budget: float | None = None,
    credit_rate_per_hour: float = 0.0,
    enabled: bool = True,
    capability_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Owner: create an executor (docs/adr/0025 P2). An ``llm_agent``
    needs a name + max_parallel>=1 + credit_rate_per_hour>=0; a ``human``
    must be bound to a workspace member (user_id). Owner-gated in the
    service (effective-role sudo enforced)."""
    async with _tenant(token, org_id) as (s, org, user):
        row = await executors_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind(kind),
            name=name,
            user_id=uuid.UUID(user_id) if user_id else None,
            context_switch_cost_minutes=context_switch_cost_minutes,
            provider=provider,
            model_id=model_id,
            max_parallel=max_parallel,
            credit_budget=(Decimal(str(credit_budget)) if credit_budget is not None else None),
            credit_rate_per_hour=Decimal(str(credit_rate_per_hour)),
            enabled=enabled,
            capability_tags=list(capability_tags or []),
        )
        return _executor(row)


@mcp.tool()
async def executor_update(
    token: str,
    org_id: str,
    executor_id: str,
    expected_version: int,
    name: str | None = None,
    context_switch_cost_minutes: int | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    max_parallel: int | None = None,
    credit_budget: float | None = None,
    credit_rate_per_hour: float | None = None,
    enabled: bool | None = None,
    capability_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Owner: patch an executor (optimistic concurrency). ``kind`` and
    ``user_id`` are immutable identity. Owner-gated in the service."""
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if context_switch_cost_minutes is not None:
        values["context_switch_cost_minutes"] = context_switch_cost_minutes
    if provider is not None:
        values["provider"] = provider
    if model_id is not None:
        values["model_id"] = model_id
    if max_parallel is not None:
        values["max_parallel"] = max_parallel
    if credit_budget is not None:
        values["credit_budget"] = Decimal(str(credit_budget))
    if credit_rate_per_hour is not None:
        values["credit_rate_per_hour"] = Decimal(str(credit_rate_per_hour))
    if enabled is not None:
        values["enabled"] = enabled
    if capability_tags is not None:
        values["capability_tags"] = list(capability_tags)
    async with _tenant(token, org_id) as (s, org, user):
        version = await executors_svc.update_executor(
            s,
            org_id=org,
            actor_id=user,
            executor_id=uuid.UUID(executor_id),
            expected_version=expected_version,
            values=values,
        )
        return {"executor_id": executor_id, "version": version}


@mcp.tool()
async def executor_delete(token: str, org_id: str, executor_id: str) -> dict[str, Any]:
    """Owner: delete an executor. Always allowed (including the seeded
    default agent): the scheduler marks affected llm tasks unassignable
    rather than silently rerouting. Owner-gated in the service."""
    async with _tenant(token, org_id) as (s, org, user):
        await executors_svc.delete_executor(
            s,
            org_id=org,
            actor_id=user,
            executor_id=uuid.UUID(executor_id),
        )
        return {"deleted": True}


# --- Agent execution runtime (docs/adr/0025, P3) ---
# Co-equal to the REST surface. Reads are member-level; start/cancel
# are owner-gated INSIDE the service (the RBAC choke point +
# effective-role sudo), because running an agent spends credits -- same
# gate model as the billing-grant tools. The governance (hard tool
# allowlist, step/budget caps, cooperative kill switch) is in
# flow_core.services.agent_runtime; this is a thin wrapper.


def _agent_run(r: AgentRun) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "task_id": str(r.task_id),
        "executor_id": str(r.executor_id) if r.executor_id else None,
        "status": r.status.value,
        "steps": r.steps,
        "credits_spent": str(r.credits_spent),
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "error": r.error,
        "artifact_note_id": (str(r.artifact_note_id) if r.artifact_note_id else None),
        "cancel_requested": r.cancel_requested,
        "blocked_reason": r.blocked_reason,
        "version": r.version,
    }


@mcp.tool()
async def agent_run_start(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Owner: run the agent on an already-dispatched ``llm_agent`` task
    end-to-end (spawn -> work -> artifact -> complete) and return the
    TERMINAL run (succeeded|failed|cancelled|blocked). On-demand, not an
    autonomous loop. Bounded (step/budget caps), killable, every tool
    call confined to the actor's effective RBAC. Owner-gated in the
    service (running an agent spends credits; effective-role sudo
    enforced)."""
    async with _tenant(token, org_id) as (s, org, user):
        run = await agent_runtime_svc.start_run(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
        )
        return _agent_run(run)


@mcp.tool()
async def agent_run_get(token: str, org_id: str, run_id: str) -> dict[str, Any]:
    """Read one agent run (member-level, RLS-scoped)."""
    async with _tenant(token, org_id) as (s, org, _user):
        run = await agent_runtime_svc.get_run(s, org_id=org, run_id=uuid.UUID(run_id))
        return _agent_run(run)


@mcp.tool()
async def agent_runs_list(
    token: str, org_id: str, task_id: str | None = None
) -> list[dict[str, Any]]:
    """List agent runs (member-level), newest first, optionally filtered
    to one task."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await agent_runtime_svc.list_runs(
            s,
            org_id=org,
            task_id=uuid.UUID(task_id) if task_id else None,
        )
        return [_agent_run(r) for r in rows]


@mcp.tool()
async def agent_run_cancel(token: str, org_id: str, run_id: str) -> dict[str, Any]:
    """Owner: request cancellation (cooperative kill switch the loop
    observes). Idempotent; a terminal run is an error. Owner-gated in
    the service."""
    async with _tenant(token, org_id) as (s, org, user):
        run = await agent_runtime_svc.cancel_run(
            s,
            org_id=org,
            actor_id=user,
            run_id=uuid.UUID(run_id),
        )
        return _agent_run(run)


# --- P4: coordination handoffs + contract-net (docs/adr/0025) ---


def _handoff(h: TaskHandoff) -> dict[str, Any]:
    return {
        "id": str(h.id),
        "predecessor_task_id": str(h.predecessor_task_id),
        "successor_task_id": str(h.successor_task_id),
        "from_executor_id": (str(h.from_executor_id) if h.from_executor_id else None),
        "to_executor_id": str(h.to_executor_id) if h.to_executor_id else None,
        "message": h.message,
        "artifact_note_id": (str(h.artifact_note_id) if h.artifact_note_id else None),
        "status": h.status.value,
        "delivered_at": h.delivered_at.isoformat() if h.delivered_at else None,
        "consumed_at": h.consumed_at.isoformat() if h.consumed_at else None,
        "version": h.version,
    }


@mcp.tool()
async def task_handoffs_list(token: str, org_id: str, task_id: str) -> list[dict[str, Any]]:
    """List the coordination handoffs touching a task (incoming +
    outgoing). The on-completion creation is automatic (no create
    tool). Member-level, RLS-scoped (a foreign task yields none)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await coordination_svc.list_handoffs(s, org_id=org, task_id=uuid.UUID(task_id))
        return [_handoff(h) for h in rows]


@mcp.tool()
async def task_offer(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Owner: announce a task to eligible members (contract-net call-
    for-proposals); marks it offered + notifies. Owner-gated in the
    service (effective-role sudo enforced). The llm_agent 'award' is
    the P2 admission dispatch, not this tool."""
    async with _tenant(token, org_id) as (s, org, user):
        task = await coordination_svc.offer_task(
            s, org_id=org, actor_id=user, task_id=uuid.UUID(task_id)
        )
        tagmap = await tasks.tags_by_task(s, task_ids=[task.id])
        return _task_full(task, tagmap.get(task.id, []))


@mcp.tool()
async def task_claim(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Member: claim an offered task (contract-net award) -> the caller
    becomes an assignee, ``offered`` is cleared, the offerer is
    notified. Errors if not offered / already claimed."""
    async with _tenant(token, org_id) as (s, org, user):
        task = await coordination_svc.claim_task(
            s, org_id=org, actor_id=user, task_id=uuid.UUID(task_id)
        )
        tagmap = await tasks.tags_by_task(s, task_ids=[task.id])
        return _task_full(task, tagmap.get(task.id, []))


@mcp.tool()
async def task_decline(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Member: decline an offered task (lightweight: notify the offerer
    + audit, no assignment, ``offered`` left set for others). Errors if
    not offered."""
    async with _tenant(token, org_id) as (s, org, user):
        task = await coordination_svc.decline_task(
            s, org_id=org, actor_id=user, task_id=uuid.UUID(task_id)
        )
        tagmap = await tasks.tags_by_task(s, task_ids=[task.id])
        return _task_full(task, tagmap.get(task.id, []))


# --- P5: closed-loop dispatch + approval gates (docs/adr/0025) ---
# Co-equal to the REST surface. The queue read is member-level; approve
# / deny / tick are owner-gated inside the service (a tick can spend
# credits via the P3 metered path -> effective-role sudo enforced),
# mirroring the agent-run-start / billing-grant tools.


def _dispatch_request(r: DispatchRequest) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "task_id": str(r.task_id),
        "executor_id": str(r.executor_id) if r.executor_id else None,
        "status": r.status.value,
        "projected_credit_cost": str(r.projected_credit_cost),
        "agent_run_id": str(r.agent_run_id) if r.agent_run_id else None,
        "requested_at": r.requested_at.isoformat() if r.requested_at else None,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decided_by": str(r.decided_by) if r.decided_by else None,
        "reason": r.reason,
        "version": r.version,
    }


@mcp.tool()
async def dispatch_requests_list(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the closed-loop dispatch queue (member-level, RLS-scoped),
    newest first. Each row is an approval gate for an admitted
    ``llm_agent`` task: status, the assigned executor, the projected
    credit cost, and the started ``agent_run_id`` once dispatched."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await dispatch_loop_svc.list_requests(s, org_id=org)
        return [_dispatch_request(r) for r in rows]


@mcp.tool()
async def dispatch_approve(
    token: str, org_id: str, request_id: str, expected_version: int
) -> dict[str, Any]:
    """Owner: approve a pending dispatch request, then immediately
    attempt the dispatch inline (approve-then-inline-dispatch: the run
    starts via the P3 metered path in this call). Owner-gated in the
    service (a dispatch spends credits; effective-role sudo enforced).
    Optimistic concurrency on ``expected_version``."""
    async with _tenant(token, org_id) as (s, org, user):
        req = await dispatch_loop_svc.approve_request(
            s,
            org_id=org,
            actor_id=user,
            request_id=uuid.UUID(request_id),
            expected_version=expected_version,
        )
        return _dispatch_request(req)


@mcp.tool()
async def dispatch_deny(
    token: str,
    org_id: str,
    request_id: str,
    expected_version: int,
    reason: str | None = None,
) -> dict[str, Any]:
    """Owner: deny an active dispatch request (never starts a run), with
    an optional short reason. Owner-gated in the service; optimistic
    concurrency on ``expected_version``."""
    async with _tenant(token, org_id) as (s, org, user):
        req = await dispatch_loop_svc.deny_request(
            s,
            org_id=org,
            actor_id=user,
            request_id=uuid.UUID(request_id),
            expected_version=expected_version,
            reason=reason,
        )
        return _dispatch_request(req)


@mcp.tool()
async def dispatch_tick(token: str, org_id: str, policy: str | None = None) -> dict[str, Any]:
    """Owner: run one closed-loop tick now (recompute -> admit -> gate
    -> dispatch via P3). The worker calls the same service on a timer.
    Owner-gated in the service (a tick can spend credits). ``policy`` is
    the resource-leveling policy for the recompute
    (fastest|cheapest|balanced|throughput, default balanced)."""
    async with _tenant(token, org_id) as (s, org, user):
        res = await dispatch_loop_svc.tick(
            s,
            org_id=org,
            actor_id=user,
            policy=(SchedulePolicy(policy) if policy else SchedulePolicy.balanced),
        )
        return {
            "policy": res.policy.value,
            "enabled": res.enabled,
            "created": res.created,
            "approved": res.approved,
            "dispatched": res.dispatched,
            "skipped": res.skipped,
            "failed": res.failed,
            "projected_makespan_minutes": res.projected_makespan_minutes,
            "projected_credit_cost": str(res.projected_credit_cost),
        }


# --- F4: time tracking (FR-5) ---


_EMPTY_TASK_CTX = time_svc.TaskContext()


def _time_entry(
    e: TimeEntry,
    ctx: time_svc.TaskContext = _EMPTY_TASK_CTX,
    note_title: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "task_id": str(e.task_id),
        "user_id": str(e.user_id),
        "started_at": e.started_at.isoformat(),
        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
        "duration_seconds": e.duration_seconds,
        "source": e.source.value,
        "executor_kind": e.executor_kind.value,
        "billable": e.billable,
        "parallel": e.parallel,
        "rate_snapshot": (str(e.rate_snapshot) if e.rate_snapshot is not None else None),
        "currency": e.currency,
        "memo": e.memo,
        "note_id": str(e.note_id) if e.note_id else None,
        "note_title": note_title,
        "version": e.version,
        "task_title": ctx.task_title,
        "client_tag_id": (str(ctx.client_tag_id) if ctx.client_tag_id else None),
        "client_name": ctx.client_name,
        "project_tag_id": (str(ctx.project_tag_id) if ctx.project_tag_id else None),
        "project_name": ctx.project_name,
        "client_timezone": ctx.client_timezone,
    }


async def _time_entry_one(s: Any, e: TimeEntry) -> dict[str, Any]:
    titles = await time_svc.resolve_note_titles(s, [e.note_id])
    note_title = titles.get(e.note_id) if e.note_id is not None else None
    return _time_entry(e, await time_svc.context_for_entry(s, e), note_title)


async def _time_entries_many(s: Any, rows: list[TimeEntry]) -> list[dict[str, Any]]:
    ctxs = await time_svc.resolve_task_contexts(s, [e.task_id for e in rows])
    titles = await time_svc.resolve_note_titles(s, [e.note_id for e in rows])
    return [
        _time_entry(
            e,
            ctxs.get(e.task_id, _EMPTY_TASK_CTX),
            titles.get(e.note_id) if e.note_id is not None else None,
        )
        for e in rows
    ]


@mcp.tool()
async def start_timer(
    token: str,
    org_id: str,
    task_id: str | None = None,
    billable: bool | None = None,
    memo: str | None = None,
    note_id: str | None = None,
    parallel: bool = False,
) -> dict[str, Any]:
    """Start the live timer for a task. Serial (default) replaces the
    single running timer; ``parallel=True`` runs alongside others
    (e.g. concurrent LLM tasks). The same task is never
    double-tracked. Proposal A: pass ``note_id`` to log time in a work
    note (it must be linked to a task); the billing task is derived
    from it, so ``task_id`` may be omitted (or must agree). ``memo`` is
    the free-text note on the entry (not the Note entity)."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.start_timer(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id) if task_id else None,
            billable=billable,
            memo=memo,
            note_id=uuid.UUID(note_id) if note_id else None,
            parallel=parallel,
        )
        return await _time_entry_one(s, e)


@mcp.tool()
async def stop_timer(
    token: str,
    org_id: str,
    task_id: str | None = None,
    memo: str | None = None,
) -> dict[str, Any]:
    """Stop a running timer: the one for ``task_id`` if given, else the
    serial timer. Computes the duration. ``memo`` overwrites the
    entry's free-text note when given."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.stop_timer(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id) if task_id else None,
            memo=memo,
        )
        return await _time_entry_one(s, e)


@mcp.tool()
async def add_time_entry(
    token: str,
    org_id: str,
    task_id: str | None = None,
    started_at: str = "",
    ended_at: str | None = None,
    duration_seconds: int | None = None,
    billable: bool | None = None,
    memo: str | None = None,
    note_id: str | None = None,
) -> dict[str, Any]:
    """Add a manual time entry (provide ended_at or duration_seconds).
    Proposal A: a ``note_id`` derives the billing task (the note must
    be linked to a task; ``task_id`` may be omitted or must agree).
    ``memo`` is the entry's free-text note (not the Note entity)."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id) if task_id else None,
            started_at=dt.datetime.fromisoformat(started_at),
            ended_at=dt.datetime.fromisoformat(ended_at) if ended_at else None,
            duration_seconds=duration_seconds,
            billable=billable,
            memo=memo,
            note_id=uuid.UUID(note_id) if note_id else None,
        )
        return await _time_entry_one(s, e)


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
        return await _time_entries_many(s, rows)


@mcp.tool()
async def get_time_entry(token: str, org_id: str, entry_id: str) -> dict[str, Any]:
    """Read one time entry."""
    async with _tenant(token, org_id) as (s, org, _user):
        e = await time_svc.get_entry(s, org_id=org, entry_id=uuid.UUID(entry_id))
        return await _time_entry_one(s, e)


@mcp.tool()
async def list_running_timers(token: str, org_id: str, user_id: str) -> list[dict[str, Any]]:
    """All live timers for a user (the serial one plus any parallel)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await time_svc.running_entries(s, org_id=org, user_id=uuid.UUID(user_id))
        return await _time_entries_many(s, rows)


@mcp.tool()
async def update_time_entry(
    token: str,
    org_id: str,
    entry_id: str,
    expected_version: int,
    memo: str | None = None,
    billable: bool | None = None,
    task_id: str | None = None,
    note_id: str | None = None,
    clear_note_id: bool = False,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> dict[str, Any]:
    """Correct a time entry. Beyond memo/billable you can reassign it
    to another task (``task_id``, transitively changing project/client)
    and fix the recorded interval (``started_at``/``ended_at``,
    ISO-8601); ``duration_seconds`` is recomputed server-side. Omit
    ``ended_at`` to leave it unchanged; pass it explicitly to set or
    clear the stop time. Proposal A work-note link: pass ``note_id`` to
    set it (the note must be linked to the entry's task, else a domain
    error), or ``clear_note_id=True`` to unlink; omitting both
    preserves the stored value."""
    values: dict[str, Any] = {}
    if memo is not None:
        values["memo"] = memo
    if billable is not None:
        values["billable"] = billable
    if task_id is not None:
        values["task_id"] = uuid.UUID(task_id)
    if clear_note_id:
        values["note_id"] = None
    elif note_id is not None:
        values["note_id"] = uuid.UUID(note_id)
    if started_at is not None:
        values["started_at"] = dt.datetime.fromisoformat(started_at)
    if ended_at is not None:
        values["ended_at"] = dt.datetime.fromisoformat(ended_at)
    async with _tenant(token, org_id) as (s, org, user):
        version = await time_svc.update_entry(
            s,
            org_id=org,
            actor_id=user,
            entry_id=uuid.UUID(entry_id),
            expected_version=expected_version,
            values=values,
        )
        return {"entry_id": entry_id, "version": version}


@mcp.tool()
async def delete_time_entry(token: str, org_id: str, entry_id: str) -> dict[str, Any]:
    """Delete a time entry."""
    async with _tenant(token, org_id) as (s, org, user):
        await time_svc.delete_entry(
            s,
            org_id=org,
            actor_id=user,
            entry_id=uuid.UUID(entry_id),
        )
        return {"entry_id": entry_id, "deleted": True}


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


@mcp.tool()
async def time_report_by_task(
    token: str,
    org_id: str,
    start_from: str | None = None,
    start_to: str | None = None,
) -> list[dict[str, Any]]:
    """Per-task time aggregate for the caller: total/billable seconds
    and entry count per task, each row carrying the resolved
    project/client and the client's IANA timezone. Ordered by total
    time desc. Optional ISO-8601 ``start_from``/``start_to`` window."""
    async with _tenant(token, org_id) as (s, org, user):
        rows = await time_svc.task_report(
            s,
            org_id=org,
            actor_id=user,
            start_from=dt.datetime.fromisoformat(start_from) if start_from else None,
            start_to=dt.datetime.fromisoformat(start_to) if start_to else None,
        )
        return [
            {
                "task_id": str(r.task_id),
                "task_title": r.task_title,
                "client_tag_id": (str(r.client_tag_id) if r.client_tag_id else None),
                "client_name": r.client_name,
                "project_tag_id": (str(r.project_tag_id) if r.project_tag_id else None),
                "project_name": r.project_name,
                "client_timezone": r.client_timezone,
                "total_seconds": r.total_seconds,
                "billable_seconds": r.billable_seconds,
                "entry_count": r.entry_count,
            }
            for r in rows
        ]


# --- F4b: budgets + deterministic advisory (FR-13/FR-14) ---


def _budget(b: Budget) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "name": b.name,
        "category": b.category,
        "period_kind": b.period_kind.value,
        "period_start": b.period_start.isoformat(),
        "period_end": b.period_end.isoformat(),
        "amount": str(b.amount),
        "currency": b.currency,
        "version": b.version,
    }


@mcp.tool()
async def create_budget(
    token: str,
    org_id: str,
    name: str,
    period_kind: str,
    period_start: str,
    period_end: str,
    amount: float,
    currency: str = "EUR",
    category: str | None = None,
) -> dict[str, Any]:
    """Create a budget envelope (period_kind: month|quarter|year|custom)."""
    async with _tenant(token, org_id) as (s, org, user):
        b = await budgets_svc.create_budget(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            category=category,
            period_kind=BudgetPeriod(period_kind),
            period_start=dt.date.fromisoformat(period_start),
            period_end=dt.date.fromisoformat(period_end),
            amount=Decimal(str(amount)),
            currency=currency,
        )
        return _budget(b)


@mcp.tool()
async def list_budgets(token: str, org_id: str) -> list[dict[str, Any]]:
    """List budget envelopes."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_budget(b) for b in await budgets_svc.list_budgets(s, org_id=org)]


@mcp.tool()
async def budget_consumption(token: str, org_id: str, budget_id: str) -> dict[str, Any]:
    """Deterministic consumption vs residual for a budget."""
    async with _tenant(token, org_id) as (s, org, _user):
        c = await budgets_svc.consumption(s, org_id=org, budget_id=uuid.UUID(budget_id))
        return {
            "budget_id": str(c.budget_id),
            "amount": str(c.amount),
            "currency": c.currency,
            "consumed": str(c.consumed),
            "residual": str(c.residual),
            "task_count": c.task_count,
        }


@mcp.tool()
async def update_budget(
    token: str,
    org_id: str,
    budget_id: str,
    expected_version: int,
    name: str | None = None,
    category: str | None = None,
    period_kind: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    amount: float | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    """Edit a budget envelope (only the given fields)."""
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if category is not None:
        values["category"] = category
    if period_kind is not None:
        values["period_kind"] = BudgetPeriod(period_kind)
    if period_start is not None:
        values["period_start"] = dt.date.fromisoformat(period_start)
    if period_end is not None:
        values["period_end"] = dt.date.fromisoformat(period_end)
    if amount is not None:
        values["amount"] = Decimal(str(amount))
    if currency is not None:
        values["currency"] = currency
    async with _tenant(token, org_id) as (s, org, user):
        version = await budgets_svc.update_budget(
            s,
            org_id=org,
            actor_id=user,
            budget_id=uuid.UUID(budget_id),
            expected_version=expected_version,
            values=values,
        )
        return {"budget_id": budget_id, "version": version}


@mcp.tool()
async def delete_budget(token: str, org_id: str, budget_id: str) -> dict[str, Any]:
    """Delete a budget envelope."""
    async with _tenant(token, org_id) as (s, org, user):
        await budgets_svc.delete_budget(
            s,
            org_id=org,
            actor_id=user,
            budget_id=uuid.UUID(budget_id),
        )
        return {"budget_id": budget_id, "deleted": True}


@mcp.tool()
async def what_can_i_do_now(
    token: str,
    org_id: str,
    window_start: str,
    duration_minutes: int,
    location: str | None = None,
    context_tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic: feasible tasks for a free window, ranked."""
    async with _tenant(token, org_id) as (s, org, user):
        rows = await advisory_svc.what_can_i_do_now(
            s,
            org_id=org,
            actor_id=user,
            window_start=dt.datetime.fromisoformat(window_start),
            duration_minutes=duration_minutes,
            location=location,
            context_tags=context_tags,
        )
        return [
            {
                "task_id": str(r.task_id),
                "title": r.title,
                "necessity": r.necessity.value,
                "priority": r.priority,
                "due_date": r.due_date.isoformat() if r.due_date else None,
                "remaining_minutes": r.remaining_minutes,
            }
            for r in rows
        ]


@mcp.tool()
async def errands(
    token: str,
    org_id: str,
    location: str | None = None,
    context: str | None = None,
) -> list[dict[str, Any]]:
    """Deterministic: tasks relevant to a place/context across the org."""
    async with _tenant(token, org_id) as (s, org, user):
        rows = await advisory_svc.errands(
            s,
            org_id=org,
            actor_id=user,
            location=location,
            context=context,
        )
        return [
            {
                "task_id": str(r.task_id),
                "title": r.title,
                "location": r.location,
                "necessity": r.necessity.value,
                "priority": r.priority,
            }
            for r in rows
        ]


@mcp.tool()
async def prioritize_within_budget(token: str, org_id: str, budget_id: str) -> dict[str, Any]:
    """Deterministic priority/value-density selection within a budget."""
    async with _tenant(token, org_id) as (s, org, user):
        plan = await advisory_svc.prioritize_within_budget(
            s,
            org_id=org,
            actor_id=user,
            budget_id=uuid.UUID(budget_id),
        )
        return {
            "budget_id": str(plan.budget_id),
            "amount": str(plan.amount),
            "currency": plan.currency,
            "allocated": str(plan.allocated),
            "residual": str(plan.residual),
            "selected": [
                {
                    "task_id": str(p.task_id),
                    "title": p.title,
                    "cost": str(p.cost),
                    "necessity": p.necessity.value,
                    "priority": p.priority,
                    "value": p.value,
                }
                for p in plan.selected
            ],
            "excluded": plan.excluded,
        }


# --- F5: email (FR-7) ---


def _email_account(a: EmailAccount) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "provider": a.provider.value,
        "email_address": a.email_address,
        "status": a.status.value,
        "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
        "last_error": a.last_error,
        "version": a.version,
    }


def _email_message(m: EmailMessage) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "account_id": str(m.account_id),
        "from_addr": m.from_addr,
        "subject": m.subject,
        "snippet": m.snippet,
        "received_at": m.received_at.isoformat(),
        "linked_task_id": (str(m.linked_task_id) if m.linked_task_id else None),
        "version": m.version,
    }


@mcp.tool()
async def create_email_account(
    token: str,
    org_id: str,
    provider: str,
    email_address: str,
    secret: str,
    imap_host: str | None = None,
    imap_port: int | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
) -> dict[str, Any]:
    """Register an email account. The secret is stored encrypted and
    never returned."""
    async with _tenant(token, org_id) as (s, org, user):
        a = await email_svc.create_account(
            s,
            org_id=org,
            actor_id=user,
            provider=EmailProvider(provider),
            email_address=email_address,
            secret=secret,
            imap_host=imap_host,
            imap_port=imap_port,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
        )
        return _email_account(a)


@mcp.tool()
async def list_email_accounts(token: str, org_id: str) -> list[dict[str, Any]]:
    """List email accounts (no secrets)."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_email_account(a) for a in await email_svc.list_accounts(s, org_id=org)]


@mcp.tool()
async def sync_email_account(
    token: str, org_id: str, account_id: str, limit: int = 50
) -> dict[str, Any]:
    """Idempotently sync one account (known messages are skipped)."""
    async with _tenant(token, org_id) as (s, org, user):
        r = await email_svc.sync_account(
            s,
            org_id=org,
            actor_id=user,
            account_id=uuid.UUID(account_id),
            limit=limit,
        )
        return {
            "account_id": str(r.account_id),
            "fetched": r.fetched,
            "created": r.created,
            "ok": r.ok,
            "error": r.error,
        }


@mcp.tool()
async def list_email_messages(
    token: str, org_id: str, account_id: str | None = None
) -> list[dict[str, Any]]:
    """List ingested messages, optionally filtered by account."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await email_svc.list_messages(
            s,
            org_id=org,
            account_id=uuid.UUID(account_id) if account_id else None,
        )
        return [_email_message(m) for m in rows]


@mcp.tool()
async def email_to_task(
    token: str,
    org_id: str,
    message_id: str,
    project_tag_id: str | None = None,
) -> dict[str, Any]:
    """Create a task from a message, with a source link."""
    async with _tenant(token, org_id) as (s, org, user):
        task_id = await email_svc.email_to_task(
            s,
            org_id=org,
            actor_id=user,
            message_id=uuid.UUID(message_id),
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
        )
        return {"task_id": str(task_id)}


@mcp.tool()
async def send_email(
    token: str,
    org_id: str,
    account_id: str,
    to_addrs: list[str],
    subject: str,
    body_text: str,
) -> dict[str, Any]:
    """Send a message from an account."""
    async with _tenant(token, org_id) as (s, org, user):
        sent = await email_svc.send_message(
            s,
            org_id=org,
            actor_id=user,
            account_id=uuid.UUID(account_id),
            to_addrs=to_addrs,
            subject=subject,
            body_text=body_text,
        )
        return {"sent_id": sent}


@mcp.tool()
async def reply_email(token: str, org_id: str, message_id: str, body_text: str) -> dict[str, Any]:
    """Reply in-thread to an ingested message."""
    async with _tenant(token, org_id) as (s, org, user):
        sent = await email_svc.reply_to_message(
            s,
            org_id=org,
            actor_id=user,
            message_id=uuid.UUID(message_id),
            body_text=body_text,
        )
        return {"sent_id": sent}


# --- F5b: billing / metering (FR-15) ---


def _usage(r: UsageRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "operation_id": r.operation_id,
        "model_id": r.model_id,
        "op": r.op,
        "basis": r.basis.value,
        "units_in": str(r.units_in),
        "units_out": str(r.units_out),
        "credits": str(r.credits),
    }


def _rate_card(c: RateCard) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "model_id": c.model_id,
        "provider": c.provider,
        "unit": c.unit.value,
        "credits_per_input": str(c.credits_per_input),
        "credits_per_output": str(c.credits_per_output),
        "is_active": c.is_active,
        "version": c.version,
    }


@mcp.tool()
async def billing_balance(token: str, org_id: str) -> dict[str, Any]:
    """Current credit balance for the org."""
    async with _tenant(token, org_id) as (s, org, _user):
        return {"balance": str(await billing_svc.balance(s, org_id=org))}


@mcp.tool()
async def grant_credits(
    token: str, org_id: str, amount: float, reason: str | None = None
) -> dict[str, Any]:
    """Admin: top up credits (manual grant; v1 has no payment gateway)."""
    async with _tenant(token, org_id) as (s, org, user):
        new_balance = await billing_svc.grant_credits(
            s,
            org_id=org,
            actor_id=user,
            amount=Decimal(str(amount)),
            reason=reason,
        )
        return {"balance": str(new_balance)}


@mcp.tool()
async def meter_usage(
    token: str,
    org_id: str,
    operation_id: str,
    op: str,
    model_id: str | None = None,
    units_in: float = 0.0,
    units_out: float = 0.0,
    basis: str = "local",
) -> dict[str, Any]:
    """Idempotent metered debit (re-running the same operation_id does
    not charge twice)."""
    async with _tenant(token, org_id) as (s, org, user):
        record = await billing_svc.meter(
            s,
            org_id=org,
            actor_id=user,
            operation_id=operation_id,
            op=op,
            model_id=model_id,
            units_in=Decimal(str(units_in)),
            units_out=Decimal(str(units_out)),
            basis=CostBasis(basis),
        )
        return _usage(record)


@mcp.tool()
async def upsert_rate_card(
    token: str,
    org_id: str,
    model_id: str,
    provider: str,
    credits_per_input: float = 0.0,
    credits_per_output: float = 0.0,
) -> dict[str, Any]:
    """Admin: create or update a model rate card."""
    async with _tenant(token, org_id) as (s, org, user):
        card = await billing_svc.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id=model_id,
            provider=provider,
            values={
                "credits_per_input": Decimal(str(credits_per_input)),
                "credits_per_output": Decimal(str(credits_per_output)),
            },
        )
        return _rate_card(card)


@mcp.tool()
async def list_rate_cards(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the org rate cards."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_rate_card(c) for c in await billing_svc.list_rate_cards(s, org_id=org)]


@mcp.tool()
async def list_usage(token: str, org_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """List recent metered usage records."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_usage(r) for r in await billing_svc.list_usage(s, org_id=org, limit=limit)]


# --- F6: hierarchical memory (FR-8) ---


def _blob(b: MemoryBlob, tags: list[Tag] | None = None) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "project_id": str(b.project_id) if b.project_id else None,
        "namespace": b.namespace,
        "tier": b.tier,
        "text": b.text,
        "model_id": b.model_id,
        "cluster_id": str(b.cluster_id) if b.cluster_id else None,
        "tags": [
            {"id": str(g.id), "kind": g.kind.value, "name": g.name, "color": g.color}
            for g in (tags or [])
        ],
    }


@mcp.tool()
async def memory_write(
    token: str,
    org_id: str,
    text: str,
    operation_id: str,
    project_id: str | None = None,
    namespace: str = "note",
    source_kind: str | None = None,
    source_id: str | None = None,
    tag_ids: list[str] | None = None,
    channel_tag_id: str | None = None,
    channel_key: str | None = None,
) -> dict[str, Any]:
    """Write a memory blob. The embedding is metered *when produced*;
    if the embedding model is unavailable the blob is stored
    keyword-only (still FTS-searchable, never an error). Optional
    provenance for GDPR erasure. Tags = explicit ``tag_ids`` plus an
    optional memory channel plus those inherited from tagged sources.
    The channel may be addressed by ``channel_tag_id`` (a
    ``memory_channel`` tag id) or, deterministically, by ``channel_key``
    (its stable slug, what integrations use); if both are given they
    must resolve to the same channel. The (org, project) boundary is
    hard."""
    async with _tenant(token, org_id) as (s, org, user):
        sources = (
            [(source_kind, source_id)] if source_kind is not None and source_id is not None else []
        )
        blob = await memory_svc.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=uuid.UUID(project_id) if project_id else None,
            text_body=text,
            operation_id=operation_id,
            namespace=namespace,
            sources=sources,
            tag_ids=[uuid.UUID(t) for t in (tag_ids or [])],
            channel_tag_id=uuid.UUID(channel_tag_id) if channel_tag_id else None,
            channel_key=channel_key,
        )
        tagmap = await memory_svc.tags_by_blob(s, blob_ids=[blob.id])
        return _blob(blob, tagmap.get(blob.id))


@mcp.tool()
async def memory_search(
    token: str,
    org_id: str,
    query: str,
    operation_id: str,
    project_id: str | None = None,
    limit: int = 10,
    tag_ids: list[str] | None = None,
    channel_tag_id: str | None = None,
    channel_key: str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid RRF retrieval within the (org, project) boundary
    (retrieval-as-tool, ADR-0016). Degrades to keyword-only when the
    embedding model is unavailable. Optional ``tag_ids`` /
    ``channel_tag_id`` / ``channel_key`` narrow to blobs carrying every
    given tag (and the channel), a facet that never crosses the
    boundary. The channel may be given by id or, deterministically, by
    its stable ``channel_key`` slug; if both are given they must
    resolve to the same channel. Deterministic order."""
    async with _tenant(token, org_id) as (s, org, user):
        hits = await memory_svc.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=uuid.UUID(project_id) if project_id else None,
            query=query,
            operation_id=operation_id,
            limit=limit,
            tag_ids=[uuid.UUID(t) for t in (tag_ids or [])],
            channel_tag_id=uuid.UUID(channel_tag_id) if channel_tag_id else None,
            channel_key=channel_key,
        )
        tagmap = await memory_svc.tags_by_blob(s, blob_ids=[h.blob.id for h in hits])
        return [{"blob": _blob(h.blob, tagmap.get(h.blob.id)), "rrf": h.rrf} for h in hits]


@mcp.tool()
async def memory_erase(token: str, org_id: str, source_kind: str, source_id: str) -> dict[str, Any]:
    """GDPR erasure by provenance; cascades to embedding/sources."""
    async with _tenant(token, org_id) as (s, org, user):
        deleted = await memory_svc.gdpr_erase(
            s,
            org_id=org,
            actor_id=user,
            source_kind=source_kind,
            source_id=source_id,
        )
        return {"deleted": deleted}


@mcp.tool()
async def memory_consolidate(
    token: str,
    org_id: str,
    blob_ids: list[str],
    operation_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Merge same-(org, project) blobs into one concept, provenance
    preserved. Never crosses org/project."""
    async with _tenant(token, org_id) as (s, org, user):
        blob = await memory_svc.consolidate(
            s,
            org_id=org,
            actor_id=user,
            project_id=uuid.UUID(project_id) if project_id else None,
            blob_ids=[uuid.UUID(b) for b in blob_ids],
            operation_id=operation_id,
        )
        tagmap = await memory_svc.tags_by_blob(s, blob_ids=[blob.id])
        return _blob(blob, tagmap.get(blob.id))


@mcp.tool()
async def memory_delete_blob(token: str, org_id: str, blob_id: str) -> dict[str, Any]:
    """Delete a single memory entry (hard delete; cascades to its
    tags/sources/vector). Member-level, RLS-scoped: a foreign/unknown
    blob id is memory.not_found. Distinct from ``memory_erase`` (which
    removes a provenance link and only deletes a blob left orphaned)."""
    async with _tenant(token, org_id) as (s, org, user):
        await memory_svc.delete_blob(
            s,
            org_id=org,
            actor_id=user,
            blob_id=uuid.UUID(blob_id),
        )
        return {"blob_id": blob_id, "deleted": True}


@mcp.tool()
async def memory_status(token: str, org_id: str) -> dict[str, Any]:
    """Whether semantic (vector) retrieval is available, or memory is
    running keyword-only because the optional embedding model is not
    installed. Member-level."""
    async with _tenant(token, org_id) as (_s, _org, _user):
        return {"semantic": embedder_available()}


# --- Memory channels (controlled, seeded vocabulary; FR-8) ---------
#
# Listing is member-level (the agent needs it to pick a channel);
# create/rename/enable-disable is PLATFORM-ADMIN only. The REST surface
# gates this with the sudo rule "capability (is_admin) AND active
# X-Admin-Mode elevation". MCP is a tool protocol with no per-call
# elevation header, so the equivalent gate here is the capability
# itself (``users.is_admin``); a non-admin caller is rejected exactly
# like the REST 403. (Mirrors the REST gating note in
# api/src/flow_api/routers/memory_channels.py.)
_CANONICAL_CHANNEL_KEYS = frozenset(k for k, _ in taxonomy.CANONICAL_MEMORY_CHANNELS)


def _channel(t: Tag) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "system_key": t.system_key,
        "enabled": t.status == "active",
        "seeded": t.system_key in _CANONICAL_CHANNEL_KEYS,
        "description": taxonomy.channel_description(t.system_key),
        "version": t.version,
    }


async def _require_platform_admin(user_id: uuid.UUID) -> None:
    """Platform-admin capability gate for channel management. ``users``
    is global (not RLS-scoped), so it is read via an admin session,
    exactly like the REST admin surface. Raises the same code the REST
    layer maps to 403 for a non-admin."""
    async with admin_session() as s:
        u = (await s.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if u is None or not u.is_admin:
        raise ForbiddenError(MessageCode.CHANNEL_ADMIN_ONLY)


@mcp.tool()
async def memory_channels_list(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the tenant's configured memory channels (seeds the
    canonical four on first call). Any authenticated member may list --
    the agent needs it to pick a channel by ``system_key``. RLS-scoped."""
    async with _tenant(token, org_id) as (s, org, _user):
        channels = await taxonomy.list_memory_channels(s, org_id=org)
        return [_channel(t) for t in channels]


@mcp.tool()
async def memory_channel_create(
    token: str,
    org_id: str,
    name: str,
    system_key: str | None = None,
) -> dict[str, Any]:
    """Create a custom memory channel. PLATFORM-ADMIN only (see the
    module note: REST also requires an active X-Admin-Mode elevation;
    MCP gates on the is_admin capability)."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_platform_admin(user)
        tag = await taxonomy.create_memory_channel(
            s, org_id=org, actor_id=user, name=name, system_key=system_key
        )
        return _channel(tag)


@mcp.tool()
async def memory_channel_update(
    token: str,
    org_id: str,
    channel_id: str,
    name: str | None = None,
    enabled: bool | None = None,
    system_key: str | None = None,
) -> dict[str, Any]:
    """Rename and/or enable/disable a memory channel. PLATFORM-ADMIN
    only. A seeded channel may be renamed and disabled but its
    ``system_key`` is immutable (channel.key_immutable)."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_platform_admin(user)
        tag = await taxonomy.update_memory_channel(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(channel_id),
            name=name,
            enabled=enabled,
            system_key=system_key,
        )
        return _channel(tag)


@mcp.tool()
async def memory_channel_delete(token: str, org_id: str, channel_id: str) -> dict[str, Any]:
    """Delete a custom memory channel. PLATFORM-ADMIN only. A seeded
    channel is not deletable -- disable it instead
    (channel.seeded_undeletable)."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_platform_admin(user)
        await taxonomy.delete_memory_channel(
            s, org_id=org, actor_id=user, tag_id=uuid.UUID(channel_id)
        )
        return {"deleted": channel_id}


# --- F6b: notes / conversation / canonical intent (FR-16) ---


def _note(
    n: Note,
    tags: list[Tag] | None = None,
    primary_task_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    # docs/adr/0029 P3: ``task_id`` comes from the typed link table.
    return {
        "id": str(n.id),
        "project_id": str(n.project_id) if n.project_id else None,
        "task_id": str(primary_task_id) if primary_task_id else None,
        "kind": n.kind.value,
        "status": n.status.value,
        "title": n.title,
        "transcript": n.transcript,
        "version": n.version,
        "tags": [_tag_brief(g) for g in (tags or [])],
    }


def _turn(t: NoteTurn) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "role": t.role.value,
        "content": t.content,
        "ord": t.ord,
    }


@mcp.tool()
async def create_note(
    token: str,
    org_id: str,
    kind: str,
    text: str | None = None,
    title: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Capture a note (voice|text|conversation). Unmetered."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind(kind),
            project_id=uuid.UUID(project_id) if project_id else None,
            title=title,
            text=text,
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid)


@mcp.tool()
async def list_notes(
    token: str,
    org_id: str,
    project_id: str | None = None,
    tag_id: str | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """List notes (newest first); for the @note picker. Optional
    project/tag focus and archive/trash views."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await notes_svc.list_notes(
            s,
            org_id=org,
            project_id=uuid.UUID(project_id) if project_id else None,
            tag_id=uuid.UUID(tag_id) if tag_id else None,
            include_archived=include_archived,
            include_deleted=include_deleted,
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id for n in rows])
        pid_map = await note_links_svc.primary_task_ids_for_notes(
            s, org_id=org, note_ids=[n.id for n in rows]
        )
        return [_note(n, tagmap.get(n.id, []), primary_task_id=pid_map.get(n.id)) for n in rows]


@mcp.tool()
async def get_note(token: str, org_id: str, note_id: str) -> dict[str, Any]:
    """Read one note."""
    async with _tenant(token, org_id) as (s, org, _user):
        note = await notes_svc.get_note(s, org_id=org, note_id=uuid.UUID(note_id))
        tagmap = await notes_svc.tags_by_note(s, note_ids=[note.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=note.id)
        return _note(note, tagmap.get(note.id, []), primary_task_id=pid)


@mcp.tool()
async def get_or_create_task_note(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Open a task's "work note" (creating it on first call). Idempotent:
    repeated calls return the same note. Time spent on the note is billed
    to the task via the task-scoped timer."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.get_or_create_work_note(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid)


@mcp.tool()
async def create_task_note(
    token: str,
    org_id: str,
    task_id: str,
    title: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """TASK-side of the bidirectional Proposal A link: create a *fresh*
    work note pre-linked to the task (NOT idempotent, unlike
    get_or_create_task_note). Title defaults to the task title. Time
    logged in the note rolls up to the task."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.create_note_for_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            title=title,
            text=text,
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid)


@mcp.tool()
async def update_note(
    token: str,
    org_id: str,
    note_id: str,
    expected_version: int,
    title: str | None = None,
    text: str | None = None,
    task_id: str | None = None,
    clear_task_id: bool = False,
) -> dict[str, Any]:
    """Edit a note's title/body. A blank title is re-derived from the
    first line of the body. Bidirectional Proposal A link: pass
    ``task_id`` to link the note to a task (validated in-org, else
    TASK_NOT_FOUND), or ``clear_task_id=True`` to unlink; omitting both
    leaves the existing link untouched."""
    async with _tenant(token, org_id) as (s, org, user):
        extra: dict[str, Any] = {}
        if clear_task_id:
            extra["task_id"] = None
        elif task_id is not None:
            extra["task_id"] = uuid.UUID(task_id)
        version = await notes_svc.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
            title=title,
            text=text,
            **extra,
        )
        return {"note_id": note_id, "version": version}


@mcp.tool()
async def archive_note(
    token: str,
    org_id: str,
    note_id: str,
    expected_version: int,
    archived: bool = True,
) -> dict[str, Any]:
    """Archive (or unarchive with ``archived=False``) a note."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await notes_svc.archive_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
            archived=archived,
        )
        return {"note_id": note_id, "version": version}


@mcp.tool()
async def delete_note(
    token: str, org_id: str, note_id: str, expected_version: int
) -> dict[str, Any]:
    """Soft-delete a note (recoverable via restore_note)."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await notes_svc.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
        )
        return {"note_id": note_id, "version": version}


@mcp.tool()
async def restore_note(
    token: str, org_id: str, note_id: str, expected_version: int
) -> dict[str, Any]:
    """Restore a soft-deleted note."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await notes_svc.restore_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
        )
        return {"note_id": note_id, "version": version}


@mcp.tool()
async def add_note_tag(token: str, org_id: str, note_id: str, tag_id: str) -> dict[str, Any]:
    """Attach a tag to a note (idempotent). A client tag sets the
    note's client; a project tag organizes it under a project."""
    async with _tenant(token, org_id) as (s, org, user):
        await notes_svc.attach_tag(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            tag_id=uuid.UUID(tag_id),
        )
        return {"note_id": note_id, "tag_id": tag_id}


@mcp.tool()
async def remove_note_tag(token: str, org_id: str, note_id: str, tag_id: str) -> dict[str, Any]:
    """Detach a tag from a note."""
    async with _tenant(token, org_id) as (s, org, user):
        await notes_svc.detach_tag(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            tag_id=uuid.UUID(tag_id),
        )
        return {"note_id": note_id, "tag_id": tag_id, "removed": True}


@mcp.tool()
async def list_turns(token: str, org_id: str, note_id: str) -> list[dict[str, Any]]:
    """List the turns of a conversation note, in order."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await notes_svc.list_turns(s, org_id=org, note_id=uuid.UUID(note_id))
        return [_turn(t) for t in rows]


@mcp.tool()
async def start_conversation_session(
    token: str,
    org_id: str,
    title: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Start a new conversation session (a conversation Note)."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.conversation,
            project_id=uuid.UUID(project_id) if project_id else None,
            title=title,
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid)


@mcp.tool()
async def append_message(
    token: str, org_id: str, note_id: str, content: str, operation_id: str
) -> dict[str, Any]:
    """Append a user message; returns the metered LLM reply turn."""
    async with _tenant(token, org_id) as (s, org, user):
        reply = await notes_svc.append_message(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            content=content,
            operation_id=operation_id,
        )
        return _turn(reply)


@mcp.tool()
async def transcribe_note(
    token: str, org_id: str, note_id: str, operation_id: str
) -> dict[str, Any]:
    """Run STT on a voice note (metered per audio-minute)."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.transcribe(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            operation_id=operation_id,
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid)


@mcp.tool()
async def run_command(token: str, org_id: str, text: str) -> dict[str, Any]:
    """Deterministic canonical NL command (offline, unmetered)."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.run_command(s, org_id=org, actor_id=user, text=text)
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid)


@mcp.tool()
async def synthesize_speech(
    token: str, org_id: str, text: str, operation_id: str
) -> dict[str, Any]:
    """TTS voice-out (metered per character)."""
    async with _tenant(token, org_id) as (s, org, user):
        return await notes_svc.synthesize(
            s,
            org_id=org,
            actor_id=user,
            text=text,
            operation_id=operation_id,
        )


# --- Attachments on notes / tasks (DB-BYTEA) ---
#
# Binary UPLOAD is intentionally NOT exposed over MCP: tools exchange
# JSON, and base64-blob round-trips do not fit the protocol (and would
# bypass the multipart size guard). Upload stays REST-only
# (POST /notes|tasks/{id}/attachments). MCP gets read/curation parity
# (list + delete), mirroring the other list_*/delete_* tools.


@mcp.tool()
async def list_attachments(
    token: str,
    org_id: str,
    note_id: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    """List a note's OR a task's attachments (metadata only; the binary
    is never returned). Pass exactly one of note_id / task_id."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await attachments_svc.list_attachments(
            s,
            org_id=org,
            note_id=uuid.UUID(note_id) if note_id else None,
            task_id=uuid.UUID(task_id) if task_id else None,
        )
        return [
            {
                "id": str(r.id),
                "note_id": str(r.note_id) if r.note_id else None,
                "task_id": str(r.task_id) if r.task_id else None,
                "filename": r.filename,
                "mime_type": r.mime_type,
                "size_bytes": r.size_bytes,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@mcp.tool()
async def delete_attachment(token: str, org_id: str, attachment_id: str) -> dict[str, Any]:
    """Hard-delete an attachment (the stored blob goes with the row).
    Member-level, org-scoped (RLS)."""
    async with _tenant(token, org_id) as (s, org, user):
        await attachments_svc.delete_attachment(
            s,
            org_id=org,
            actor_id=user,
            attachment_id=uuid.UUID(attachment_id),
        )
        return {"attachment_id": attachment_id, "deleted": True}


# --- F7: electronic invoicing (FR-9) ---


def _invoice(i: Invoice) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "kind": i.kind.value,
        "document_type": i.document_type.value,
        "series": i.series,
        "year": i.year,
        "number": i.number,
        "state": i.state.value,
        "total": str(i.total),
        "identificativo_sdi": i.identificativo_sdi,
        "sdi_status": i.sdi_status.value,
        "conservation_status": i.conservation_status.value,
        "version": i.version,
    }


@mcp.tool()
async def set_issuer_profile(
    token: str,
    org_id: str,
    denominazione: str,
    label: str = "Principale",
    piva: str | None = None,
    codice_fiscale: str | None = None,
    indirizzo: str = "",
    cap: str = "",
    comune: str = "",
) -> dict[str, Any]:
    """Create-or-update the default issuer profile, the invoice
    "intestazione" (admin). Idempotent on the org default: updates it if
    one exists, else creates it (and flags it default)."""
    async with _tenant(token, org_id) as (s, org, user):
        current = await invoice_svc.get_default_issuer_profile(s, org_id=org)
        if current is None:
            p = await invoice_svc.create_issuer_profile(
                s,
                org_id=org,
                actor_id=user,
                label=label,
                denominazione=denominazione,
                piva=piva,
                codice_fiscale=codice_fiscale,
                indirizzo=indirizzo,
                cap=cap,
                comune=comune,
                is_default=True,
            )
        else:
            p = await invoice_svc.update_issuer_profile(
                s,
                org_id=org,
                actor_id=user,
                profile_id=current.id,
                values={
                    "label": label,
                    "denominazione": denominazione,
                    "piva": piva,
                    "codice_fiscale": codice_fiscale,
                    "indirizzo": indirizzo,
                    "cap": cap,
                    "comune": comune,
                },
            )
        return {
            "id": str(p.id),
            "label": p.label,
            "denominazione": p.denominazione,
            "is_default": p.is_default,
            "version": p.version,
        }


@mcp.tool()
async def create_invoice(
    token: str, org_id: str, client_tag_id: str, series: str = "A"
) -> dict[str, Any]:
    """Create a draft invoice."""
    async with _tenant(token, org_id) as (s, org, user):
        inv = await invoice_svc.create_draft(
            s,
            org_id=org,
            actor_id=user,
            client_tag_id=uuid.UUID(client_tag_id),
            series=series,
        )
        return _invoice(inv)


@mcp.tool()
async def add_invoice_line(
    token: str,
    org_id: str,
    invoice_id: str,
    description: str,
    unit_price: float,
    quantity: float = 1.0,
    vat_rate: float = 22.0,
) -> dict[str, Any]:
    """Add a line to a draft invoice."""
    async with _tenant(token, org_id) as (s, org, user):
        ln = await invoice_svc.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=uuid.UUID(invoice_id),
            description=description,
            unit_price=Decimal(str(unit_price)),
            quantity=Decimal(str(quantity)),
            vat_rate=Decimal(str(vat_rate)),
        )
        return {"id": str(ln.id), "line_no": ln.line_no}


@mcp.tool()
async def transmit_invoice(token: str, org_id: str, invoice_id: str) -> dict[str, Any]:
    """Validate, allocate the progressive number and transmit (channel
    injected; manual export by default)."""
    async with _tenant(token, org_id) as (s, org, user):
        inv = await invoice_svc.transmit(
            s, org_id=org, actor_id=user, invoice_id=uuid.UUID(invoice_id)
        )
        return _invoice(inv)


@mcp.tool()
async def invoice_credit_note(
    token: str, org_id: str, parent_invoice_id: str, causale: str | None = None
) -> dict[str, Any]:
    """Create a TD04 credit note linked to a transmitted invoice."""
    async with _tenant(token, org_id) as (s, org, user):
        inv = await invoice_svc.create_credit_note(
            s,
            org_id=org,
            actor_id=user,
            parent_invoice_id=uuid.UUID(parent_invoice_id),
            causale=causale,
        )
        return _invoice(inv)


@mcp.tool()
async def ingest_sdi_receipt(
    token: str, org_id: str, identificativo_sdi: str, outcome: str
) -> dict[str, Any]:
    """Correlate an SdI receipt (RC/MC/NS/AT) by IdentificativoSdI."""
    async with _tenant(token, org_id) as (s, org, user):
        inv = await invoice_svc.ingest_receipt(
            s,
            org_id=org,
            actor_id=user,
            identificativo_sdi=identificativo_sdi,
            outcome=outcome,
        )
        return _invoice(inv)


# --- F8: notifications / recurrence / reminders (FR-12) ---


@mcp.tool()
async def set_notification_pref(
    token: str,
    org_id: str,
    user_id: str,
    channel: str,
    target: str,
    enabled: bool = True,
) -> dict[str, Any]:
    """Set a user's per-channel notification preference."""
    async with _tenant(token, org_id) as (s, org, user):
        p = await notif_svc.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=uuid.UUID(user_id),
            channel=NotificationChannelKind(channel),
            enabled=enabled,
            target=target,
        )
        return {"channel": p.channel.value, "enabled": p.enabled}


@mcp.tool()
async def dispatch_notifications(token: str, org_id: str) -> dict[str, Any]:
    """Send pending notifications (per-item fault isolation)."""
    async with _tenant(token, org_id) as (s, org, user):
        r = await notif_svc.dispatch_pending(s, org_id=org, actor_id=user)
        return {"sent": r.sent, "failed": r.failed}


@mcp.tool()
async def create_recurrence(
    token: str,
    org_id: str,
    task_id: str,
    freq: str,
    next_run: str,
    interval: int = 1,
) -> dict[str, Any]:
    """Make a task recurring (mutually exclusive with dependencies)."""
    async with _tenant(token, org_id) as (s, org, user):
        rec = await notif_svc.create_recurrence(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            freq=RecurrenceFreq(freq),
            next_run=dt.datetime.fromisoformat(next_run),
            interval=interval,
        )
        return {"task_id": str(rec.task_id), "freq": rec.freq.value}


@mcp.tool()
async def spawn_due_recurrences(
    token: str, org_id: str, as_of: str | None = None
) -> dict[str, Any]:
    """Materialize due recurrences as independent task rows."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notif_svc.spawn_due(
            s,
            org_id=org,
            actor_id=user,
            now=dt.datetime.fromisoformat(as_of) if as_of else None,
        )
        return {"count": n}


@mcp.tool()
async def scan_reminders(token: str, org_id: str, within_days: int = 1) -> dict[str, Any]:
    """Enqueue idempotent due-date reminders for assignees."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notif_svc.scan_reminders(s, org_id=org, actor_id=user, within_days=within_days)
        return {"count": n}


# --- F6c: workflows (FR-6) ---


def _workflow(w: WorkflowDefinition) -> dict[str, Any]:
    return {
        "id": str(w.id),
        "name": w.name,
        "is_default": w.is_default,
        "version": w.version,
    }


def _state(st: WorkflowState) -> dict[str, Any]:
    return {
        "id": str(st.id),
        "name": st.name,
        "ord": st.ord,
        "is_initial": st.is_initial,
        "is_terminal": st.is_terminal,
    }


def _transition(tr: WorkflowTransition) -> dict[str, Any]:
    return {
        "from_state_id": str(tr.from_state_id),
        "to_state_id": str(tr.to_state_id),
    }


@mcp.tool()
async def list_workflows(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the org workflow definitions."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await workflow_svc.list_workflows(s, org)
        return [_workflow(w) for w in rows]


@mcp.tool()
async def workflow_states(token: str, org_id: str, workflow_id: str) -> list[dict[str, Any]]:
    """List a workflow's states (ordered)."""
    async with _tenant(token, org_id) as (s, _org, _user):
        rows = await workflow_svc.get_states(s, uuid.UUID(workflow_id))
        return [_state(st) for st in rows]


@mcp.tool()
async def workflow_transitions(token: str, org_id: str, workflow_id: str) -> list[dict[str, Any]]:
    """List a workflow's allowed (from -> to) transitions."""
    async with _tenant(token, org_id) as (s, _org, _user):
        rows = await workflow_svc.list_transitions(s, uuid.UUID(workflow_id))
        return [_transition(tr) for tr in rows]


@mcp.tool()
async def create_workflow(
    token: str,
    org_id: str,
    name: str,
    states: list[dict[str, Any]],
    transitions: list[list[str]],
) -> dict[str, Any]:
    """Create a workflow. ``states`` items: {name, ord?, is_initial?,
    is_terminal?} (exactly one initial). ``transitions``: [from, to]
    name pairs."""
    async with _tenant(token, org_id) as (s, org, user):
        w = await workflow_svc.create_workflow(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            states=[
                StateSpec(
                    name=st["name"],
                    ord=int(st.get("ord", 0)),
                    is_initial=bool(st.get("is_initial", False)),
                    is_terminal=bool(st.get("is_terminal", False)),
                )
                for st in states
            ],
            transitions=[(t[0], t[1]) for t in transitions],
        )
        return _workflow(w)


@mcp.tool()
async def update_workflow(
    token: str,
    org_id: str,
    workflow_id: str,
    name: str,
    states: list[dict[str, Any]],
    transitions: list[list[str]],
) -> dict[str, Any]:
    """Rename + reconcile a workflow's states (match by ``id``; new
    ones omit it; dropped only if unused) and replace transitions."""
    async with _tenant(token, org_id) as (s, org, user):
        await workflow_svc.update_workflow(
            s,
            org_id=org,
            actor_id=user,
            workflow_id=uuid.UUID(workflow_id),
            name=name,
            states=[
                StateEdit(
                    id=uuid.UUID(st["id"]) if st.get("id") else None,
                    name=st["name"],
                    ord=int(st.get("ord", 0)),
                    is_initial=bool(st.get("is_initial", False)),
                    is_terminal=bool(st.get("is_terminal", False)),
                )
                for st in states
            ],
            transitions=[(t[0], t[1]) for t in transitions],
        )
        return {"workflow_id": workflow_id, "updated": True}


@mcp.tool()
async def delete_workflow(token: str, org_id: str, workflow_id: str) -> dict[str, Any]:
    """Delete a workflow (refused for the default or if its states
    still hold tasks)."""
    async with _tenant(token, org_id) as (s, org, user):
        await workflow_svc.delete_workflow(
            s,
            org_id=org,
            actor_id=user,
            workflow_id=uuid.UUID(workflow_id),
        )
        return {"workflow_id": workflow_id, "deleted": True}


@mcp.tool()
async def set_default_workflow(token: str, org_id: str, workflow_id: str) -> dict[str, Any]:
    """Promote a workflow to the org default (keeps exactly one)."""
    async with _tenant(token, org_id) as (s, org, user):
        await workflow_svc.set_default_workflow(
            s,
            org_id=org,
            actor_id=user,
            workflow_id=uuid.UUID(workflow_id),
        )
        return {"workflow_id": workflow_id, "is_default": True}


@mcp.tool()
async def set_project_workflow(
    token: str,
    org_id: str,
    project_tag_id: str,
    expected_version: int,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Set (or clear, with ``workflow_id=None``) a project's workflow
    override. None falls back to the org default."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await workflow_svc.set_project_workflow(
            s,
            org_id=org,
            actor_id=user,
            project_tag_id=uuid.UUID(project_tag_id),
            workflow_id=uuid.UUID(workflow_id) if workflow_id else None,
            expected_version=expected_version,
        )
        return {"project_tag_id": project_tag_id, "version": version}


# ---------------------------------------------------------------------------
# Garden ecosystem (docs/adr/0029 P1): typed note links + named lifecycle
# operations + maturity setter. Wraps services/note_links.py.
# ---------------------------------------------------------------------------


@mcp.tool()
async def set_note_maturity(
    token: str,
    org_id: str,
    note_id: str,
    maturity: str,
) -> dict[str, Any]:
    """Manual override of a note's garden lifecycle (seed | growing |
    mature | dormant). Cannot run on a note already transplanted
    (``promoted_at`` set)."""
    async with _tenant(token, org_id) as (s, org, user):
        note = await note_links_svc.set_maturity(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            maturity=maturity,
        )
        return {"note_id": str(note.id), "maturity": note.maturity}


@mcp.tool()
async def link_notes(
    token: str,
    org_id: str,
    parent_note_id: str,
    child_note_id: str,
    kind: str,
) -> dict[str, Any]:
    """Link two notes with a typed relation: ``atom_of`` (atomic child
    of an index parent), ``references`` (citation backlink),
    ``replies_to`` (threaded elaboration), ``supersedes``."""
    async with _tenant(token, org_id) as (s, org, user):
        link = await note_links_svc.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=uuid.UUID(parent_note_id),
            child_note_id=uuid.UUID(child_note_id),
            kind=kind,
        )
        return {
            "link_id": str(link.id),
            "parent_note_id": str(link.parent_note_id),
            "child_note_id": str(link.child_note_id),
            "kind": link.kind,
        }


@mcp.tool()
async def unlink_notes(
    token: str,
    org_id: str,
    parent_note_id: str,
    child_note_id: str,
    kind: str,
) -> dict[str, Any]:
    """Remove a typed note-to-note link. Returns ``removed`` true/false
    (false when the link did not exist)."""
    async with _tenant(token, org_id) as (s, org, user):
        removed = await note_links_svc.unlink_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=uuid.UUID(parent_note_id),
            child_note_id=uuid.UUID(child_note_id),
            kind=kind,
        )
        return {"removed": removed}


@mcp.tool()
async def derive_task_from_note(
    token: str,
    org_id: str,
    note_id: str,
    title: str,
    description: str | None = None,
    estimate_effort_h: float | None = None,
) -> dict[str, Any]:
    """Create a task as a fruit of this note. The note stays alive
    (no transplant). A ``derived_from`` link is recorded."""
    async with _tenant(token, org_id) as (s, org, user):
        from decimal import Decimal

        eff = Decimal(str(estimate_effort_h)) if estimate_effort_h is not None else None
        task, link = await note_links_svc.derive_task_from_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            title=title,
            description=description,
            estimate_effort_h=eff,
        )
        return {
            "task_id": str(task.id),
            "link_id": str(link.id),
            "kind": link.kind,
        }


@mcp.tool()
async def promote_note_to_task(
    token: str,
    org_id: str,
    note_id: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Transplant the note to a task. The note becomes read-only
    (``promoted_at`` set); a ``promoted_from`` link records the
    provenance."""
    async with _tenant(token, org_id) as (s, org, user):
        task, link = await note_links_svc.promote_note_to_task(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            title=title,
        )
        return {
            "task_id": str(task.id),
            "link_id": str(link.id),
            "kind": link.kind,
        }


@mcp.tool()
async def start_task_on_note(
    token: str,
    org_id: str,
    task_id: str,
    note_id: str,
) -> dict[str, Any]:
    """Watering: this task is the work of growing the note. Records a
    ``subject`` link."""
    async with _tenant(token, org_id) as (s, org, user):
        link = await note_links_svc.start_task_on_note(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            note_id=uuid.UUID(note_id),
        )
        return {"link_id": str(link.id), "kind": link.kind}


@mcp.tool()
async def record_task_artifact(
    token: str,
    org_id: str,
    task_id: str,
    note_id: str,
) -> dict[str, Any]:
    """The task produced (or updated) this note. Records an
    ``artifact`` link (Proposal A semantics, surfaced explicitly)."""
    async with _tenant(token, org_id) as (s, org, user):
        link = await note_links_svc.record_task_artifact(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            note_id=uuid.UUID(note_id),
        )
        return {"link_id": str(link.id), "kind": link.kind}


# ---------------------------------------------------------------------------
# Identity-first task ownership / assignment (docs/adr/0028 D5 follow-up):
# explicit primitives that wrap ``update_task``. Useful for Telegram chat
# ("assegna @marco al task X") and the conversational assistant: a named
# operation reads better than a generic update.
# ---------------------------------------------------------------------------


@mcp.tool()
async def set_task_owner(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    owner_id: str | None = None,
    owner_handle: str | None = None,
) -> dict[str, Any]:
    """Reassign accountability for a task (docs/adr/0028 D2). Owner
    is always a real user. Pass either ``owner_id`` (uuid) or
    ``owner_handle`` (resolved against ``identities`` under the
    current org; the identity must be ``kind=user``)."""
    async with _tenant(token, org_id) as (s, org, user):
        if owner_id is None and owner_handle:
            from flow_core.models.identity import Identity, IdentityKind

            row = (
                await s.execute(
                    select(Identity).where(
                        Identity.org_id == org,
                        Identity.handle == owner_handle,
                        Identity.kind == IdentityKind.user,
                    )
                )
            ).scalar_one_or_none()
            if row is None or row.user_id is None:
                raise NotFoundError(MessageCode.USER_NOT_FOUND)
            resolved = row.user_id
        elif owner_id is not None:
            resolved = uuid.UUID(owner_id)
        else:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        version = await tasks.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            values={"owner_id": resolved},
        )
        return {"task_id": task_id, "owner_id": str(resolved), "version": version}


@mcp.tool()
async def set_task_assignee(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    assignee_id: str | None = None,
    assignee_handle: str | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    """Set or clear who should work on the task (docs/adr/0028 D2).
    Pass ``assignee_id`` (uuid into identities) or ``assignee_handle``
    (resolved under the current org). ``clear=True`` unassigns the
    task; the routing kind then falls back to ``task.executor_kind``
    (ADR-0028)."""
    async with _tenant(token, org_id) as (s, org, user):
        values: dict[str, Any] = {}
        if clear:
            values["assignee_id"] = None
        elif assignee_id is not None:
            values["assignee_id"] = uuid.UUID(assignee_id)
        elif assignee_handle:
            values["assignee_handle"] = assignee_handle
        else:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        version = await tasks.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            values=values,
        )
        return {"task_id": task_id, "version": version, "cleared": clear}
