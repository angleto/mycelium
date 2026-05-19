"""Workflows router. Thin adapter over the service layer
(docs/adr/0001, 0004, FR-6)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    ProjectWorkflowIn,
    StateOut,
    TransitionOut,
    VersionOut,
    WorkflowCreateIn,
    WorkflowOut,
    WorkflowPatchIn,
)
from flow_core.models.membership import Role
from flow_core.services import workflow as wf
from flow_core.services.rbac import ensure_role
from flow_core.services.workflow import StateEdit, StateSpec

router = APIRouter(tags=["workflows"])


@router.post("/workflows", response_model=WorkflowOut)
async def create_workflow(
    body: WorkflowCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]
) -> WorkflowOut:
    ensure_role(ctx.role, Role.admin)
    w = await wf.create_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        name=body.name,
        states=[
            StateSpec(
                name=s.name,
                ord=s.ord,
                is_initial=s.is_initial,
                is_terminal=s.is_terminal,
            )
            for s in body.states
        ],
        transitions=[(t.from_state, t.to_state) for t in body.transitions],
    )
    return WorkflowOut(id=w.id, name=w.name, is_default=w.is_default, version=w.version)


@router.patch("/workflows/{workflow_id}", status_code=204)
async def update_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    ensure_role(ctx.role, Role.admin)
    await wf.update_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        workflow_id=workflow_id,
        name=body.name,
        states=[
            StateEdit(
                id=s.id,
                name=s.name,
                ord=s.ord,
                is_initial=s.is_initial,
                is_terminal=s.is_terminal,
            )
            for s in body.states
        ],
        transitions=[(t.from_state, t.to_state) for t in body.transitions],
    )


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    ensure_role(ctx.role, Role.admin)
    await wf.delete_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        workflow_id=workflow_id,
    )


@router.post("/workflows/{workflow_id}/default", status_code=204)
async def set_default_workflow(
    workflow_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    ensure_role(ctx.role, Role.admin)
    await wf.set_default_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        workflow_id=workflow_id,
    )


@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[WorkflowOut]:
    rows = await wf.list_workflows(ctx.session, ctx.org_id)
    return [
        WorkflowOut(
            id=w.id,
            name=w.name,
            is_default=w.is_default,
            version=w.version,
        )
        for w in rows
    ]


@router.get("/workflows/{workflow_id}/states", response_model=list[StateOut])
async def workflow_states(
    workflow_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[StateOut]:
    states = await wf.get_states(ctx.session, workflow_id)
    return [
        StateOut(
            id=s.id,
            name=s.name,
            ord=s.ord,
            is_initial=s.is_initial,
            is_terminal=s.is_terminal,
        )
        for s in states
    ]


@router.get(
    "/workflows/{workflow_id}/transitions",
    response_model=list[TransitionOut],
)
async def workflow_transitions(
    workflow_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[TransitionOut]:
    edges = await wf.list_transitions(ctx.session, workflow_id)
    return [TransitionOut(from_state_id=e.from_state_id, to_state_id=e.to_state_id) for e in edges]


@router.patch("/projects/{project_tag_id}/workflow", response_model=VersionOut)
async def set_project_workflow(
    project_tag_id: uuid.UUID,
    body: ProjectWorkflowIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> VersionOut:
    ensure_role(ctx.role, Role.admin)
    version = await wf.set_project_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_tag_id=project_tag_id,
        workflow_id=body.workflow_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=project_tag_id, version=version)
