"""Workflows router. Thin adapter over the service layer
(docs/adr/0001, 0004, FR-6)."""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.filenames import slugify_filename
from mycelium_api.schemas import (
    ProjectWorkflowIn,
    StateOut,
    TransitionOut,
    VersionOut,
    WorkflowCreateIn,
    WorkflowDocIn,
    WorkflowOut,
    WorkflowPatchIn,
)
from mycelium_core.models.membership import Role
from mycelium_core.services import workflow as wf
from mycelium_core.services import workflow_io as wf_io
from mycelium_core.services.rbac import ensure_role
from mycelium_core.services.workflow import StateEdit, StateSpec

router = APIRouter(tags=["workflows"])


@router.post("/workflows", response_model=WorkflowOut)
async def create_workflow(
    body: WorkflowCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> WorkflowOut:
    ensure_role(ctx.role, Role.owner)
    w = await wf.create_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        name=body.name,
        description=body.description,
        states=[
            StateSpec(
                name=s.name,
                ord=s.ord,
                is_initial=s.is_initial,
                is_terminal=s.is_terminal,
                is_hidden=s.is_hidden,
                description=s.description,
            )
            for s in body.states
        ],
        transitions=[(t.from_state, t.to_state) for t in body.transitions],
    )
    return WorkflowOut(
        id=w.id,
        name=w.name,
        description=w.description,
        is_default=w.is_default,
        version=w.version,
    )


def _document(body: WorkflowDocIn) -> wf_io.WorkflowDoc:
    """Body -> validated document. Every rule lives in the service, so
    the SPA and ``mycelium workflow import`` get the same verdict and
    the same message (docs/adr/0052)."""
    return wf_io.normalize(
        kind=body.kind,
        version=body.version,
        name=body.name,
        description=body.description,
        states=[
            wf_io.DocState(
                name=s.name,
                is_initial=s.is_initial,
                is_terminal=s.is_terminal,
                is_hidden=s.is_hidden,
                description=s.description,
            )
            for s in body.states
        ],
        transitions=[
            wf_io.DocTransition(from_state=t.from_state, to_state=t.to_state)
            for t in body.transitions
        ],
    )


@router.post("/workflows/import", response_model=WorkflowOut)
async def import_workflow(
    body: WorkflowDocIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    name: Annotated[str | None, Query(max_length=120)] = None,
) -> WorkflowOut:
    """Create a workflow from an interchange document.

    ``name`` overrides the one in the file: ``workflow_defs`` is unique
    on ``(org_id, name)``, so it is what lets the same file be imported
    twice into one workspace.
    """
    ensure_role(ctx.role, Role.owner)
    w = await wf_io.import_as_new_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        doc=_document(body),
        name=name,
    )
    return WorkflowOut(
        id=w.id,
        name=w.name,
        description=w.description,
        is_default=w.is_default,
        version=w.version,
    )


@router.post("/workflows/{workflow_id}/import", status_code=204)
async def import_into_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowDocIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    """Replace an existing workflow's configuration with the document.

    States are matched by NAME so the ones that survive keep their ids
    and their tasks; a state the document drops is deleted, which the
    service still refuses while any task sits in it.
    """
    ensure_role(ctx.role, Role.owner)
    await wf_io.import_into_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        workflow_id=workflow_id,
        doc=_document(body),
    )


@router.get("/workflows/{workflow_id}/export")
async def export_workflow(
    workflow_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> Response:
    """The workflow as a downloadable interchange document."""
    doc = await wf_io.export_workflow(ctx.session, workflow_id=workflow_id)
    payload = json.dumps(wf_io.to_json(doc), indent=2, ensure_ascii=False) + "\n"
    filename = f"workflow-{slugify_filename(doc.name)}.json"
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.patch("/workflows/{workflow_id}", status_code=204)
async def update_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    ensure_role(ctx.role, Role.owner)
    await wf.update_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        workflow_id=workflow_id,
        name=body.name,
        description=body.description,
        states=[
            StateEdit(
                id=s.id,
                name=s.name,
                ord=s.ord,
                is_initial=s.is_initial,
                is_terminal=s.is_terminal,
                is_hidden=s.is_hidden,
                description=s.description,
            )
            for s in body.states
        ],
        transitions=[(t.from_state, t.to_state) for t in body.transitions],
    )


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    ensure_role(ctx.role, Role.owner)
    await wf.delete_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        workflow_id=workflow_id,
    )


@router.post("/workflows/{workflow_id}/default", status_code=204)
async def set_default_workflow(
    workflow_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    ensure_role(ctx.role, Role.owner)
    await wf.set_default_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        workflow_id=workflow_id,
    )


@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[WorkflowOut]:
    rows = await wf.list_workflows(ctx.session, ctx.org_id)
    return [
        WorkflowOut(
            id=w.id,
            name=w.name,
            description=w.description,
            is_default=w.is_default,
            version=w.version,
        )
        for w in rows
    ]


@router.get("/workflows/{workflow_id}/states", response_model=list[StateOut])
async def workflow_states(
    workflow_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[StateOut]:
    states = await wf.get_states(ctx.session, workflow_id)
    return [
        StateOut(
            id=s.id,
            name=s.name,
            ord=s.ord,
            is_initial=s.is_initial,
            is_terminal=s.is_terminal,
            is_hidden=s.is_hidden,
            description=s.description,
        )
        for s in states
    ]


@router.get(
    "/workflows/{workflow_id}/transitions",
    response_model=list[TransitionOut],
)
async def workflow_transitions(
    workflow_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[TransitionOut]:
    edges = await wf.list_transitions(ctx.session, workflow_id)
    return [TransitionOut(from_state_id=e.from_state_id, to_state_id=e.to_state_id) for e in edges]


@router.patch("/projects/{project_tag_id}/workflow", response_model=VersionOut)
async def set_project_workflow(
    project_tag_id: uuid.UUID,
    body: ProjectWorkflowIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    ensure_role(ctx.role, Role.owner)
    version = await wf.set_project_workflow(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_tag_id=project_tag_id,
        workflow_id=body.workflow_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=project_tag_id, version=version)
