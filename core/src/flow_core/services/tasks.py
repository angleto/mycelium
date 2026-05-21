"""Task service: CRUD, tags/assignees, comments, workflow state
transitions. RBAC, optimistic concurrency, i18n, audit (docs/adr/0004).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.comment import Comment
from flow_core.models.membership import Role
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import ExecKind, Necessity, Task
from flow_core.models.task_assignee import TaskAssignee
from flow_core.models.task_tag import TaskTag
from flow_core.models.workflow import WorkflowState
from flow_core.services import audit, taxonomy
from flow_core.services import workflow as wf
from flow_core.services.rbac import require_role


def derive_priority(importance: int, urgency: int) -> int:
    """Eisenhower: importance and urgency are 1..5 where 1 is the most
    pressing (1 = Critical / Now, 5 = Trivial / Whenever). priority =
    importance*urgency, so it runs 1 (most prioritary: Critical+Now)
    .. 25 (least: Trivial+Whenever). 1 is highest and the default task
    / scheduler ordering is ascending (ADR-0004), so the smallest
    number is always done first. importance/urgency are persisted so
    the matrix round-trips."""
    return max(1, min(25, importance * urgency))


_UPDATABLE = frozenset(
    {
        "title",
        "description",
        "priority",
        "importance",
        "urgency",
        "start_date",
        "due_date",
        "estimate_effort_h",
        "executor_kind",
        "executor_user_id",
        "assignee_handle",
        "required_capabilities",
        "parent_task_id",
        "monetary_cost",
        "location",
        "necessity",
        "budget_id",
        "billable",
    }
)


async def _require_tag(session: AsyncSession, tag_id: uuid.UUID) -> None:
    found = (await session.execute(select(Tag.id).where(Tag.id == tag_id))).scalar_one_or_none()
    if found is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)


async def get_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    include_deleted: bool = False,
) -> Task:
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None or (task.deleted_at is not None and not include_deleted):
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    return task


async def create_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    title: str,
    description: str | None = None,
    priority: int = 3,
    importance: int | None = None,
    urgency: int | None = None,
    start_date: dt.date | None = None,
    due_date: dt.date | None = None,
    billable: bool | None = None,
    parent_task_id: uuid.UUID | None = None,
    executor_kind: ExecKind = ExecKind.human,
    executor_user_id: uuid.UUID | None = None,
    assignee_handle: str | None = None,
    estimate_effort_h: Decimal | None = None,
    required_capabilities: Sequence[str] | None = None,
    monetary_cost: Decimal | None = None,
    location: str | None = None,
    necessity: Necessity = Necessity.should,
    budget_id: uuid.UUID | None = None,
    tag_ids: Sequence[uuid.UUID] = (),
    assignee_ids: Sequence[uuid.UUID] = (),
) -> Task:
    await require_role(session, org_id, actor_id, Role.member)
    if parent_task_id is not None:
        await get_task(session, org_id=org_id, task_id=parent_task_id)
    eff_tag_ids = list(tag_ids)
    project_tag_id: uuid.UUID | None = None
    if eff_tag_ids:
        project_tag_id = (
            await session.execute(
                select(Tag.id).where(Tag.id.in_(eff_tag_ids), Tag.kind == TagKind.project).limit(1)
            )
        ).scalar_one_or_none()
    if project_tag_id is None:
        # No orphan tasks: every task gets a project (hence, via the
        # project, a client). Falls back to the default "General"
        # project under the default "Personal" client.
        project_tag_id = await taxonomy.ensure_default_project(
            session, org_id=org_id, actor_id=actor_id
        )
        eff_tag_ids.append(project_tag_id)
    # Auto-tag the project's client. A project always belongs to a
    # client (project_profile.client_tag_id is set at creation,
    # ensure_default_project also wires it to Personal). Carrying the
    # client tag explicitly on the task lets every per-client filter /
    # report / focus stay a flat ``WHERE tag_id = <client>`` instead of
    # joining task_tags -> project_profile. Idempotent: skip if already
    # present in tag_ids.
    client_tag_id = (
        await session.execute(
            select(ProjectProfile.client_tag_id).where(ProjectProfile.tag_id == project_tag_id)
        )
    ).scalar_one_or_none()
    if client_tag_id is not None and client_tag_id not in eff_tag_ids:
        eff_tag_ids.append(client_tag_id)
    workflow = await wf.resolve_effective_workflow(session, org_id, project_tag_id)
    initial = await wf.get_initial_state(session, workflow.id)
    if importance is not None and urgency is not None:
        priority = derive_priority(importance, urgency)
    # #21 Stage B: when the SPA sends ``assignee_handle`` it wins over
    # the legacy executor_kind / executor_user_id pair. Resolve the
    # handle against users + AI assistants and mirror the result into
    # the legacy columns so the scheduler / dispatch keep working
    # unchanged. Stage C drops the mirror columns entirely.
    if assignee_handle:
        from flow_core.models.ai_assistant import AiAssistant
        from flow_core.models.user import User

        user_row = (
            await session.execute(select(User.id).where(User.handle == assignee_handle))
        ).scalar_one_or_none()
        if user_row is not None:
            executor_kind = ExecKind.human
            executor_user_id = user_row
        else:
            assistant_row = (
                await session.execute(
                    select(AiAssistant.id).where(
                        AiAssistant.handle == assignee_handle,
                        AiAssistant.org_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if assistant_row is not None:
                executor_kind = ExecKind.llm_agent
                executor_user_id = None
            else:
                raise DomainError(MessageCode.DOMAIN_ERROR)
    task = Task(
        org_id=org_id,
        title=title,
        description=description,
        priority=priority,
        importance=importance,
        urgency=urgency,
        start_date=start_date,
        due_date=due_date,
        billable=billable,
        state_id=initial.id,
        parent_task_id=parent_task_id,
        executor_kind=executor_kind,
        executor_user_id=executor_user_id,
        assignee_handle=assignee_handle or None,
        estimate_effort_h=estimate_effort_h,
        required_capabilities=list(required_capabilities or []),
        monetary_cost=monetary_cost,
        location=location,
        necessity=necessity,
        budget_id=budget_id,
        created_by=actor_id,
    )
    session.add(task)
    await session.flush()
    for tag_id in eff_tag_ids:
        await _require_tag(session, tag_id)
        session.add(TaskTag(org_id=org_id, task_id=task.id, tag_id=tag_id))
    for user_id in assignee_ids:
        session.add(TaskAssignee(org_id=org_id, task_id=task.id, user_id=user_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task.id,
        action="create",
    )
    return task


async def list_tasks(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    state_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    parent_task_id: uuid.UUID | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
) -> list[Task]:
    stmt = select(Task)
    if not include_deleted:
        stmt = stmt.where(Task.deleted_at.is_(None))
    if not include_archived:
        stmt = stmt.where(Task.is_archived.is_(False))
    if state_id is not None:
        stmt = stmt.where(Task.state_id == state_id)
    if parent_task_id is not None:
        stmt = stmt.where(Task.parent_task_id == parent_task_id)
    if tag_id is not None:
        stmt = stmt.join(TaskTag, TaskTag.task_id == Task.id).where(TaskTag.tag_id == tag_id)
    if assignee_id is not None:
        stmt = stmt.join(TaskAssignee, TaskAssignee.task_id == Task.id).where(
            TaskAssignee.user_id == assignee_id
        )
    # Default order: most prioritary first (priority asc, 1 = top),
    # newest as tiebreak. The number is always "smaller = sooner".
    stmt = stmt.order_by(Task.priority.asc(), Task.created_at.desc())
    return list((await session.execute(stmt)).scalars().unique().all())


async def tags_by_task(
    session: AsyncSession, *, task_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Tag]]:
    """Batched task -> tags (for showing tag chips in the task list
    without an N+1)."""
    out: dict[uuid.UUID, list[Tag]] = {}
    if not task_ids:
        return out
    rows = await session.execute(
        select(TaskTag.task_id, Tag)
        .join(Tag, Tag.id == TaskTag.tag_id)
        .where(TaskTag.task_id.in_(task_ids))
    )
    for tid, tag in rows.all():
        out.setdefault(tid, []).append(tag)
    return out


async def update_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    unknown = set(values) - _UPDATABLE
    if unknown:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    current = await get_task(session, org_id=org_id, task_id=task_id)
    if "importance" in values or "urgency" in values:
        imp = values.get("importance", current.importance)
        urg = values.get("urgency", current.urgency)
        if imp is not None and urg is not None:
            values["priority"] = derive_priority(imp, urg)
    # #21 Stage B: assignee_handle is the authoritative assignee from
    # v1.2.27 onward. To keep the legacy scheduler / dispatch happy we
    # ALSO update the executor_kind / executor_user_id mirror columns
    # synchronously here: the resolver looks the handle up in users
    # vs ai_assistants and writes the appropriate exec_kind. Stage C
    # will drop the mirror columns entirely and rewire the scheduler.
    if "assignee_handle" in values:
        handle = values["assignee_handle"]
        if handle:
            from flow_core.models.ai_assistant import AiAssistant
            from flow_core.models.user import User

            user_row = (
                await session.execute(select(User.id).where(User.handle == handle))
            ).scalar_one_or_none()
            if user_row is not None:
                values["executor_kind"] = ExecKind.human
                values["executor_user_id"] = user_row
            else:
                assistant_row = (
                    await session.execute(
                        select(AiAssistant.id).where(
                            AiAssistant.handle == handle,
                            AiAssistant.org_id == org_id,
                        )
                    )
                ).scalar_one_or_none()
                if assistant_row is not None:
                    values["executor_kind"] = ExecKind.llm_agent
                    values["executor_user_id"] = None
                else:
                    raise DomainError(MessageCode.DOMAIN_ERROR)
        else:
            # Explicit clear: drop the human binding too.
            values["executor_user_id"] = None
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


_SCHEDULE_FIELDS = frozenset(
    {
        "schedule_mode",
        "constraint_kind",
        "constraint_date",
        "remaining_effort_h",
        "actual_start",
        "is_milestone",
    }
)


async def set_schedule_fields(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    """Write-back of scheduler pins/constraints (FR-4, docs/adr/0004).
    The next recompute reads these; manual/constraint survive it."""
    if not values:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    unknown = set(values) - _SCHEDULE_FIELDS
    if unknown:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="set_schedule",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def set_state(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    state_id: uuid.UUID,
) -> int:
    await require_role(session, org_id, actor_id, Role.member)
    task = await get_task(session, org_id=org_id, task_id=task_id)
    workflow = await wf.effective_workflow_for_task(session, org_id, task_id)
    if not await wf.state_in_workflow(session, workflow.id, state_id):
        raise DomainError(MessageCode.TRANSITION_NOT_ALLOWED)
    old_state_id = task.state_id
    if old_state_id != state_id:
        await wf.assert_transition(session, workflow.id, old_state_id, state_id)
    # Terminal detection reuses the SAME notion the scheduler uses
    # (``WorkflowState.is_terminal``), never a hardcoded state name.
    term_rows: dict[uuid.UUID, bool] = {
        sid: is_term
        for sid, is_term in (
            await session.execute(
                select(WorkflowState.id, WorkflowState.is_terminal).where(
                    WorkflowState.id.in_({old_state_id, state_id})
                )
            )
        ).all()
    }
    was_terminal = bool(term_rows.get(old_state_id, False))
    now_terminal = bool(term_rows.get(state_id, False))
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=expected_version,
        values={"state_id": state_id},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="set_state",
    )
    # Coordination handoff fan-out (docs/adr/0025, P4): fire ONLY when
    # the transition crosses INTO a terminal state from a non-terminal
    # one (re-entering the same terminal state is a no-op -- idempotent
    # by construction). ADDITIVE + NON-FATAL: a coordination failure is
    # swallowed inside the hook; the state transition above is the
    # source of truth and is never rolled back by it. Imported lazily
    # to avoid a tasks<->notifications<->coordination import cycle.
    if now_terminal and not was_terminal:
        from flow_core.services import coordination as _coord

        await session.refresh(task)
        await _coord.on_task_completed(session, org_id=org_id, actor_id=actor_id, task=task)
    return new_version


async def _set(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
    action: str,
) -> int:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id, include_deleted=True)
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action=action,
    )
    return new_version


async def archive_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    archived: bool = True,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"is_archived": archived},
        action="archive",
    )


async def soft_delete_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"deleted_at": dt.datetime.now(tz=dt.UTC)},
        action="soft_delete",
    )


async def restore_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"deleted_at": None},
        action="restore",
    )


async def attach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    await _require_tag(session, tag_id)
    # If the added tag is a project, also attach its client tag — same
    # hierarchy invariant as create_task. Bulk-attach the pair so the
    # caller can't observe a half-state.
    extra: list[uuid.UUID] = []
    tag_row = (await session.execute(select(Tag.kind).where(Tag.id == tag_id))).scalar_one_or_none()
    if tag_row is TagKind.project:
        client_tag_id = (
            await session.execute(
                select(ProjectProfile.client_tag_id).where(ProjectProfile.tag_id == tag_id)
            )
        ).scalar_one_or_none()
        if client_tag_id is not None:
            extra.append(client_tag_id)
    for tid in (tag_id, *extra):
        try:
            async with session.begin_nested():
                session.add(TaskTag(org_id=org_id, task_id=task_id, tag_id=tid))
                await session.flush()
        except IntegrityError:
            continue
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="attach_tag",
    )


async def detach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(TaskTag).where(TaskTag.task_id == task_id, TaskTag.tag_id == tag_id)
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="detach_tag",
    )


async def assign(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    try:
        async with session.begin_nested():
            session.add(TaskAssignee(org_id=org_id, task_id=task_id, user_id=user_id))
            await session.flush()
    except IntegrityError:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="assign",
    )


async def unassign(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(TaskAssignee).where(
            TaskAssignee.task_id == task_id,
            TaskAssignee.user_id == user_id,
        )
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="unassign",
    )


async def add_comment(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    body: str,
) -> Comment:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    comment = Comment(org_id=org_id, task_id=task_id, user_id=actor_id, body=body)
    session.add(comment)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="comment",
        entity_id=comment.id,
        action="create",
    )
    return comment


async def list_comments(
    session: AsyncSession, *, org_id: uuid.UUID, task_id: uuid.UUID
) -> list[Comment]:
    stmt = select(Comment).where(Comment.task_id == task_id).order_by(Comment.created_at)
    return list((await session.execute(stmt)).scalars().all())
