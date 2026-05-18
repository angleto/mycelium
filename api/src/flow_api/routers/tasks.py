"""Tasks router. Thin adapter over the service layer (docs/adr/0001)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    AssigneeIn,
    CommentCreateIn,
    CommentOut,
    ExpectedVersionIn,
    StateOut,
    TagBrief,
    TagRefIn,
    TaskCreateIn,
    TaskOut,
    TaskPatchIn,
    TaskStateIn,
    VersionOut,
)
from flow_core.models.comment import Comment
from flow_core.models.tag import Tag
from flow_core.models.task import Task
from flow_core.models.workflow import WorkflowState
from flow_core.services import tasks as svc
from flow_core.services import workflow as wf

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _out(t: Task, state_name: str, tags: list[Tag] | None = None) -> TaskOut:
    return TaskOut(
        tags=[
            TagBrief(id=g.id, kind=g.kind, name=g.name, color=g.color)
            for g in (tags or [])
        ],
        id=t.id,
        title=t.title,
        description=t.description,
        state_id=t.state_id,
        state=state_name,
        priority=t.priority,
        importance=t.importance,
        urgency=t.urgency,
        start_date=t.start_date,
        due_date=t.due_date,
        parent_task_id=t.parent_task_id,
        executor_kind=t.executor_kind,
        monetary_cost=t.monetary_cost,
        location=t.location,
        necessity=t.necessity,
        budget_id=t.budget_id,
        is_archived=t.is_archived,
        version=t.version,
    )


def _comment_out(c: Comment) -> CommentOut:
    return CommentOut(
        id=c.id,
        task_id=c.task_id,
        user_id=c.user_id,
        body=c.body,
        version=c.version,
    )


async def _state_names(ctx: TenantCtx, state_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not state_ids:
        return {}
    rows = (
        await ctx.session.execute(
            select(WorkflowState.id, WorkflowState.name).where(WorkflowState.id.in_(state_ids))
        )
    ).all()
    return {sid: name for sid, name in rows}


@router.post("", response_model=TaskOut)
async def create_task(
    body: TaskCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]
) -> TaskOut:
    task = await svc.create_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        title=body.title,
        description=body.description,
        priority=body.priority,
        importance=body.importance,
        urgency=body.urgency,
        start_date=body.start_date,
        due_date=body.due_date,
        parent_task_id=body.parent_task_id,
        executor_kind=body.executor_kind,
        executor_user_id=body.executor_user_id,
        estimate_effort_h=body.estimate_effort_h,
        monetary_cost=body.monetary_cost,
        location=body.location,
        necessity=body.necessity,
        budget_id=body.budget_id,
        tag_ids=body.tag_ids,
        assignee_ids=body.assignee_ids,
    )
    names = await _state_names(ctx, {task.state_id})
    tagmap = await svc.tags_by_task(ctx.session, task_ids=[task.id])
    return _out(task, names.get(task.state_id, ""), tagmap.get(task.id, []))


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    state_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    parent_task_id: uuid.UUID | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
) -> list[TaskOut]:
    rows = await svc.list_tasks(
        ctx.session,
        org_id=ctx.org_id,
        state_id=state_id,
        tag_id=tag_id,
        assignee_id=assignee_id,
        parent_task_id=parent_task_id,
        include_archived=include_archived,
        include_deleted=include_deleted,
    )
    names = await _state_names(ctx, {t.state_id for t in rows})
    tagmap = await svc.tags_by_task(ctx.session, task_ids=[t.id for t in rows])
    return [_out(t, names.get(t.state_id, ""), tagmap.get(t.id, [])) for t in rows]


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]) -> TaskOut:
    task = await svc.get_task(ctx.session, org_id=ctx.org_id, task_id=task_id)
    names = await _state_names(ctx, {task.state_id})
    tagmap = await svc.tags_by_task(ctx.session, task_ids=[task.id])
    return _out(task, names.get(task.state_id, ""), tagmap.get(task.id, []))


@router.get("/{task_id}/states", response_model=list[StateOut])
async def task_states(
    task_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]
) -> list[StateOut]:
    await svc.get_task(ctx.session, org_id=ctx.org_id, task_id=task_id)
    workflow = await wf.effective_workflow_for_task(ctx.session, ctx.org_id, task_id)
    states = await wf.get_states(ctx.session, workflow.id)
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


@router.patch("/{task_id}", response_model=VersionOut)
async def patch_task(
    task_id: uuid.UUID,
    body: TaskPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> VersionOut:
    values: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    version = await svc.update_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
        values=values,
    )
    return VersionOut(id=task_id, version=version)


@router.post("/{task_id}/state", response_model=VersionOut)
async def set_state(
    task_id: uuid.UUID,
    body: TaskStateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> VersionOut:
    version = await svc.set_state(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
        state_id=body.state_id,
    )
    return VersionOut(id=task_id, version=version)


@router.post("/{task_id}/delete", response_model=VersionOut)
async def soft_delete(
    task_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> VersionOut:
    version = await svc.soft_delete_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=task_id, version=version)


@router.post("/{task_id}/restore", response_model=VersionOut)
async def restore(
    task_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> VersionOut:
    version = await svc.restore_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=task_id, version=version)


@router.post("/{task_id}/tags", status_code=status.HTTP_204_NO_CONTENT)
async def attach_tag(
    task_id: uuid.UUID,
    body: TagRefIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.attach_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        tag_id=body.tag_id,
    )


@router.delete("/{task_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_tag(
    task_id: uuid.UUID,
    tag_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.detach_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        tag_id=tag_id,
    )


@router.post("/{task_id}/assignees", status_code=status.HTTP_204_NO_CONTENT)
async def assign(
    task_id: uuid.UUID,
    body: AssigneeIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.assign(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        user_id=body.user_id,
    )


@router.delete(
    "/{task_id}/assignees/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unassign(
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.unassign(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        user_id=user_id,
    )


@router.post("/{task_id}/comments", response_model=CommentOut)
async def add_comment(
    task_id: uuid.UUID,
    body: CommentCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> CommentOut:
    c = await svc.add_comment(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        body=body.body,
    )
    return _comment_out(c)


@router.get("/{task_id}/comments", response_model=list[CommentOut])
async def list_comments(
    task_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]
) -> list[CommentOut]:
    rows = await svc.list_comments(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return [_comment_out(c) for c in rows]
