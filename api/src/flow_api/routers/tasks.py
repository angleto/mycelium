"""Tasks router. Thin adapter over the service layer (docs/adr/0001)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    AssigneeIn,
    CommentCreateIn,
    CommentOut,
    ExpectedVersionIn,
    TagRefIn,
    TaskCreateIn,
    TaskOut,
    TaskPatchIn,
    TaskStatusIn,
    VersionOut,
)
from flow_core.models.comment import Comment
from flow_core.models.task import Task, TaskStatus
from flow_core.services import tasks as svc

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _out(t: Task) -> TaskOut:
    return TaskOut(
        id=t.id,
        title=t.title,
        description=t.description,
        status=t.status,
        priority=t.priority,
        start_date=t.start_date,
        due_date=t.due_date,
        parent_task_id=t.parent_task_id,
        executor_kind=t.executor_kind,
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
        start_date=body.start_date,
        due_date=body.due_date,
        parent_task_id=body.parent_task_id,
        executor_kind=body.executor_kind,
        executor_user_id=body.executor_user_id,
        estimate_effort_h=body.estimate_effort_h,
        tag_ids=body.tag_ids,
        assignee_ids=body.assignee_ids,
    )
    return _out(task)


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    task_status: TaskStatus | None = None,
    tag_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    parent_task_id: uuid.UUID | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
) -> list[TaskOut]:
    rows = await svc.list_tasks(
        ctx.session,
        org_id=ctx.org_id,
        status=task_status,
        tag_id=tag_id,
        assignee_id=assignee_id,
        parent_task_id=parent_task_id,
        include_archived=include_archived,
        include_deleted=include_deleted,
    )
    return [_out(t) for t in rows]


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]) -> TaskOut:
    return _out(await svc.get_task(ctx.session, org_id=ctx.org_id, task_id=task_id))


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


@router.post("/{task_id}/status", response_model=VersionOut)
async def set_status(
    task_id: uuid.UUID,
    body: TaskStatusIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> VersionOut:
    version = await svc.set_status(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
        status=body.status,
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
