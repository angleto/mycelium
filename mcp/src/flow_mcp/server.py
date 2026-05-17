"""MCP server: thin adapter over flow_core, co-equal to the REST API
(docs/adr/0001). Same service layer, RBAC and (org) isolation.

Each tool authenticates with a JWT and an org id, opens a tenant
session (RLS GUCs set) and verifies membership, exactly like the REST
``tenant_ctx`` dependency.
"""

from __future__ import annotations

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
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import Task
from flow_core.security import decode_token
from flow_core.services import dependencies, tasks, taxonomy
from flow_core.services.rbac import get_role
from flow_core.services.taxonomy import ClientInput

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
