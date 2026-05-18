"""Workflow service (docs/adr/0004, FR-6): default per Org, optional
per-project override, state machine. Additive in F2.3; the task
state-machine cutover is F2.4. RBAC, optimistic, i18n, audit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import ConflictError, DomainError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task_tag import TaskTag
from flow_core.models.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
)
from flow_core.services import audit
from flow_core.services.rbac import require_role


@dataclass(frozen=True, slots=True)
class StateSpec:
    name: str
    ord: int = 0
    is_initial: bool = False
    is_terminal: bool = False


async def get_default_workflow(session: AsyncSession, org_id: uuid.UUID) -> WorkflowDefinition:
    wf = (
        await session.execute(
            select(WorkflowDefinition).where(WorkflowDefinition.is_default.is_(True))
        )
    ).scalar_one_or_none()
    if wf is None:
        raise DomainError(MessageCode.WORKFLOW_NOT_FOUND)
    return wf


async def resolve_effective_workflow(
    session: AsyncSession,
    org_id: uuid.UUID,
    project_tag_id: uuid.UUID | None = None,
) -> WorkflowDefinition:
    if project_tag_id is not None:
        wf_id = (
            await session.execute(
                select(ProjectProfile.workflow_id).where(ProjectProfile.tag_id == project_tag_id)
            )
        ).scalar_one_or_none()
        if wf_id is not None:
            wf = (
                await session.execute(
                    select(WorkflowDefinition).where(WorkflowDefinition.id == wf_id)
                )
            ).scalar_one_or_none()
            if wf is not None:
                return wf
    return await get_default_workflow(session, org_id)


async def list_workflows(session: AsyncSession, org_id: uuid.UUID) -> list[WorkflowDefinition]:
    return list(
        (await session.execute(select(WorkflowDefinition).order_by(WorkflowDefinition.name)))
        .scalars()
        .all()
    )


async def get_states(session: AsyncSession, workflow_id: uuid.UUID) -> list[WorkflowState]:
    return list(
        (
            await session.execute(
                select(WorkflowState)
                .where(WorkflowState.workflow_id == workflow_id)
                .order_by(WorkflowState.ord)
            )
        )
        .scalars()
        .all()
    )


async def list_transitions(
    session: AsyncSession, workflow_id: uuid.UUID
) -> list[WorkflowTransition]:
    """The allowed (from -> to) edges of a workflow. Lets the UI offer
    only legal next states instead of probing the backend per click."""
    return list(
        (
            await session.execute(
                select(WorkflowTransition).where(
                    WorkflowTransition.workflow_id == workflow_id
                )
            )
        )
        .scalars()
        .all()
    )


async def get_initial_state(session: AsyncSession, workflow_id: uuid.UUID) -> WorkflowState:
    st = (
        await session.execute(
            select(WorkflowState).where(
                WorkflowState.workflow_id == workflow_id,
                WorkflowState.is_initial.is_(True),
            )
        )
    ).scalar_one_or_none()
    if st is None:
        raise DomainError(MessageCode.WORKFLOW_INVALID)
    return st


async def can_transition(
    session: AsyncSession,
    workflow_id: uuid.UUID,
    from_state_id: uuid.UUID,
    to_state_id: uuid.UUID,
) -> bool:
    row = (
        await session.execute(
            select(WorkflowTransition.id).where(
                WorkflowTransition.workflow_id == workflow_id,
                WorkflowTransition.from_state_id == from_state_id,
                WorkflowTransition.to_state_id == to_state_id,
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def assert_transition(
    session: AsyncSession,
    workflow_id: uuid.UUID,
    from_state_id: uuid.UUID,
    to_state_id: uuid.UUID,
) -> None:
    if not await can_transition(session, workflow_id, from_state_id, to_state_id):
        raise DomainError(MessageCode.TRANSITION_NOT_ALLOWED)


async def create_workflow(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    states: list[StateSpec],
    transitions: list[tuple[str, str]],
) -> WorkflowDefinition:
    await require_role(session, org_id, actor_id, Role.admin)
    if sum(1 for s in states if s.is_initial) != 1:
        raise DomainError(MessageCode.WORKFLOW_INVALID)
    wf = WorkflowDefinition(org_id=org_id, name=name, is_default=False)
    try:
        async with session.begin_nested():
            session.add(wf)
            await session.flush()
    except IntegrityError as exc:
        raise DomainError(MessageCode.DOMAIN_ERROR) from exc
    by_name: dict[str, uuid.UUID] = {}
    for spec in states:
        st = WorkflowState(
            org_id=org_id,
            workflow_id=wf.id,
            name=spec.name,
            ord=spec.ord,
            is_initial=spec.is_initial,
            is_terminal=spec.is_terminal,
        )
        session.add(st)
        await session.flush()
        by_name[spec.name] = st.id
    for src, dst in transitions:
        if src not in by_name or dst not in by_name:
            raise DomainError(MessageCode.WORKFLOW_INVALID)
        session.add(
            WorkflowTransition(
                org_id=org_id,
                workflow_id=wf.id,
                from_state_id=by_name[src],
                to_state_id=by_name[dst],
            )
        )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="workflow",
        entity_id=wf.id,
        action="create",
    )
    return wf


async def set_project_workflow(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_tag_id: uuid.UUID,
    workflow_id: uuid.UUID | None,
    expected_version: int,
) -> int:
    await require_role(session, org_id, actor_id, Role.admin)
    if workflow_id is not None:
        exists = (
            await session.execute(
                select(WorkflowDefinition.id).where(WorkflowDefinition.id == workflow_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise DomainError(MessageCode.WORKFLOW_NOT_FOUND)
    result = await session.execute(
        update(ProjectProfile)
        .where(
            ProjectProfile.tag_id == project_tag_id,
            ProjectProfile.version == expected_version,
        )
        .values(
            workflow_id=workflow_id,
            version=ProjectProfile.version + 1,
        )
        .returning(ProjectProfile.version)
    )
    row = result.first()
    if row is None:
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="project",
        entity_id=project_tag_id,
        action="set_workflow",
    )
    return int(row[0])


async def effective_workflow_for_task(
    session: AsyncSession, org_id: uuid.UUID, task_id: uuid.UUID
) -> WorkflowDefinition:
    """A task's effective workflow = the project override of its
    project-kind tag (if any), else the Org default (docs/adr/0004)."""
    project_tag_id = (
        await session.execute(
            select(Tag.id)
            .join(TaskTag, TaskTag.tag_id == Tag.id)
            .where(
                TaskTag.task_id == task_id,
                Tag.kind == TagKind.project,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return await resolve_effective_workflow(session, org_id, project_tag_id)


async def state_in_workflow(
    session: AsyncSession, workflow_id: uuid.UUID, state_id: uuid.UUID
) -> bool:
    row = (
        await session.execute(
            select(WorkflowState.id).where(
                WorkflowState.id == state_id,
                WorkflowState.workflow_id == workflow_id,
            )
        )
    ).scalar_one_or_none()
    return row is not None
