"""Executor seeding/resolution (docs/adr/0025, P1).

Lazy + idempotent bootstrap, mirroring the ``ensure_default_*`` shape
(e.g. ``ensure_default_memory_channels``): the resource-aware scheduler
calls these to guarantee every workspace has the executor rows it needs
with **zero manual configuration**. Defaults make a fresh workspace a
no-op vs the pre-P1 scheduler (human switch cost 0; one default LLM
agent at rate 0, K=4).

System actions (no role gate): the scheduler already gated the actor
(``require_role(member)``) before recompute, and seeding must be safe
to call from any order, repeatedly, without colliding (idempotency
guarded by an existence query + a nested savepoint that swallows the
unique race, exactly like the memory-channel seed).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.executor import Executor, ExecutorKind
from flow_core.models.membership import Membership, Role
from flow_core.services import audit
from flow_core.services.rbac import require_role

# The single default LLM-agent pool for a workspace (P1: one pool;
# capability routing across multiple agents is P2). Stable name = the
# idempotency key for the lazy seed.
DEFAULT_AGENT_NAME = "Assistant"
_DEFAULT_AGENT_MAX_PARALLEL = 4
_DEFAULT_AGENT_RATE = Decimal(0)


async def ensure_default_agent(session: AsyncSession, *, org_id: uuid.UUID) -> Executor:
    """Ensure the workspace's default ``llm_agent`` executor exists.

    Idempotent: guarded by an existence query on
    ``(org_id, kind=llm_agent, name=DEFAULT_AGENT_NAME)`` then a nested
    savepoint that swallows a concurrent-seed unique race. Returns the
    (existing or freshly created) row.
    """
    existing = (
        await session.execute(
            select(Executor).where(
                Executor.kind == ExecutorKind.llm_agent,
                Executor.name == DEFAULT_AGENT_NAME,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    agent = Executor(
        org_id=org_id,
        kind=ExecutorKind.llm_agent,
        name=DEFAULT_AGENT_NAME,
        max_parallel=_DEFAULT_AGENT_MAX_PARALLEL,
        credit_rate_per_hour=_DEFAULT_AGENT_RATE,
        enabled=True,
    )
    try:
        async with session.begin_nested():
            session.add(agent)
            await session.flush()
    except IntegrityError:
        return (
            await session.execute(
                select(Executor).where(
                    Executor.kind == ExecutorKind.llm_agent,
                    Executor.name == DEFAULT_AGENT_NAME,
                )
            )
        ).scalar_one()
    return agent


async def ensure_human_executor(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> Executor:
    """Ensure a per-user ``human`` executor row exists (idempotent).

    The human's working calendar is still resolved by ``user_id`` via
    ``build_work_calendar``; this row only carries the per-executor
    ``context_switch_cost_minutes`` (default 0 -> no penalty -> the
    pre-P1 behaviour). Guarded by an existence query on
    ``(org_id, kind=human, user_id)`` + a nested-savepoint race guard.
    """
    existing = (
        await session.execute(
            select(Executor).where(
                Executor.kind == ExecutorKind.human,
                Executor.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = Executor(
        org_id=org_id,
        kind=ExecutorKind.human,
        name=str(user_id),
        user_id=user_id,
        context_switch_cost_minutes=0,
        enabled=True,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return (
            await session.execute(
                select(Executor).where(
                    Executor.kind == ExecutorKind.human,
                    Executor.user_id == user_id,
                )
            )
        ).scalar_one()
    return row


async def ensure_workspace_executors(session: AsyncSession, *, org_id: uuid.UUID) -> None:
    """Seed, for the workspace, a human executor per member user and
    the one default LLM-agent pool. Idempotent + order-independent;
    safe to call on every recompute (a fresh workspace becomes a no-op
    vs pre-P1)."""
    await ensure_default_agent(session, org_id=org_id)
    member_user_ids = (
        (await session.execute(select(Membership.user_id).order_by(Membership.user_id)))
        .scalars()
        .all()
    )
    for uid in member_user_ids:
        await ensure_human_executor(session, org_id=org_id, user_id=uid)


async def list_executors(session: AsyncSession, *, org_id: uuid.UUID) -> list[Executor]:
    """All executors for the workspace (RLS-scoped), deterministically
    ordered (kind, name, id)."""
    rows = (
        (
            await session.execute(
                select(Executor).order_by(Executor.kind, Executor.name, Executor.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# --- Registry CRUD (docs/adr/0025, P2) ---
#
# Reads are member-level (the schedule plan must show its assignments).
# Mutations are owner-gated, mirroring the rate-card / issuer-profile
# precedent (workspace fiscal/config CRUD): the effective-role GUC
# (``rbac.require_role``) enforces the sudo-effective role, so an owner
# who de-escalated cannot mutate the registry. Every mutation is audited.

# Patchable executor columns (capability_tags + the llm/human knobs that
# make sense to edit post-seed). ``kind``/``user_id`` are immutable
# identity; ``name`` is editable.
_EXECUTOR_PATCHABLE = frozenset(
    {
        "name",
        "context_switch_cost_minutes",
        "provider",
        "model_id",
        "max_parallel",
        "credit_budget",
        "credit_rate_per_hour",
        "enabled",
        "capability_tags",
    }
)


async def get_executor(
    session: AsyncSession, *, org_id: uuid.UUID, executor_id: uuid.UUID
) -> Executor:
    row = (
        await session.execute(select(Executor).where(Executor.id == executor_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.EXECUTOR_NOT_FOUND)
    return row


def _validate_common(values: dict[str, Any]) -> None:
    """Shared field validation for create + patch (only the keys
    present are checked). A stable EXECUTOR_INVALID code with a short
    English ``detail`` (the offending field) -- no hardcoded prose."""
    if "name" in values:
        name = values["name"]
        if not isinstance(name, str) or not name.strip():
            raise DomainError(MessageCode.EXECUTOR_INVALID, detail="name")
    if "max_parallel" in values and int(values["max_parallel"]) < 1:
        raise DomainError(MessageCode.EXECUTOR_INVALID, detail="max_parallel")
    if (
        "credit_rate_per_hour" in values
        and values["credit_rate_per_hour"] is not None
        and Decimal(values["credit_rate_per_hour"]) < 0
    ):
        raise DomainError(MessageCode.EXECUTOR_INVALID, detail="credit_rate_per_hour")
    if (
        "credit_budget" in values
        and values["credit_budget"] is not None
        and Decimal(values["credit_budget"]) < 0
    ):
        raise DomainError(MessageCode.EXECUTOR_INVALID, detail="credit_budget")
    if "context_switch_cost_minutes" in values and int(values["context_switch_cost_minutes"]) < 0:
        raise DomainError(MessageCode.EXECUTOR_INVALID, detail="context_switch_cost_minutes")
    if "capability_tags" in values:
        tags = values["capability_tags"]
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t for t in tags):
            raise DomainError(MessageCode.EXECUTOR_INVALID, detail="capability_tags")


async def create_executor(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: ExecutorKind,
    name: str,
    user_id: uuid.UUID | None = None,
    context_switch_cost_minutes: int = 0,
    provider: str | None = None,
    model_id: str | None = None,
    max_parallel: int = 4,
    credit_budget: Decimal | None = None,
    credit_rate_per_hour: Decimal = Decimal(0),
    enabled: bool = True,
    capability_tags: list[str] | None = None,
) -> Executor:
    """Create an executor (owner-gated). An ``llm_agent`` needs a name +
    ``max_parallel`` >= 1 + ``credit_rate_per_hour`` >= 0
    (provider/model optional). A ``human`` executor is normally
    auto-seeded; a manual ``human`` create is allowed ONLY if bound to a
    workspace member (``user_id`` must be a member) -- never an unbound
    human resource."""
    await require_role(session, org_id, actor_id, Role.owner)
    tags = list(capability_tags or [])
    _validate_common(
        {
            "name": name,
            "max_parallel": max_parallel,
            "credit_rate_per_hour": credit_rate_per_hour,
            "credit_budget": credit_budget,
            "context_switch_cost_minutes": context_switch_cost_minutes,
            "capability_tags": tags,
        }
    )
    if kind is ExecutorKind.human:
        if user_id is None:
            raise DomainError(MessageCode.EXECUTOR_INVALID, detail="user_id")
        is_member = (
            await session.execute(select(Membership.user_id).where(Membership.user_id == user_id))
        ).scalar_one_or_none()
        if is_member is None:
            raise DomainError(MessageCode.EXECUTOR_INVALID, detail="user_id")
    row = Executor(
        org_id=org_id,
        kind=kind,
        name=name,
        user_id=user_id if kind is ExecutorKind.human else None,
        context_switch_cost_minutes=context_switch_cost_minutes,
        provider=provider,
        model_id=model_id,
        max_parallel=max_parallel,
        credit_budget=credit_budget,
        credit_rate_per_hour=credit_rate_per_hour,
        enabled=enabled,
        capability_tags=tags,
    )
    session.add(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="executor",
        entity_id=row.id,
        action="create",
    )
    return row


async def update_executor(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    executor_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    """Patch an executor (owner-gated, optimistic concurrency). Only
    ``_EXECUTOR_PATCHABLE`` keys; ``kind``/``user_id`` are immutable
    identity. Returns the new version."""
    await require_role(session, org_id, actor_id, Role.owner)
    unknown = set(values) - _EXECUTOR_PATCHABLE
    if unknown:
        raise DomainError(MessageCode.EXECUTOR_INVALID, detail=", ".join(sorted(unknown)))
    await get_executor(session, org_id=org_id, executor_id=executor_id)
    _validate_common(values)
    new_version = await optimistic_update(
        session,
        Executor,
        pk=executor_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="executor",
        entity_id=executor_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def delete_executor(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    executor_id: uuid.UUID,
) -> None:
    """Delete an executor (owner-gated). Deletion is always allowed --
    including the seeded default agent: the scheduler handles an empty /
    incapable agent set by marking affected llm tasks ``unassignable``
    (admission), it never silently reroutes. ``schedule.assigned_executor_id``
    is FK ON DELETE SET NULL so prior schedule rows stay readable until
    the next recompute."""
    await require_role(session, org_id, actor_id, Role.owner)
    row = await get_executor(session, org_id=org_id, executor_id=executor_id)
    await session.delete(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="executor",
        entity_id=executor_id,
        action="delete",
    )
