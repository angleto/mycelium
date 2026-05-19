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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.executor import Executor, ExecutorKind
from flow_core.models.membership import Membership

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
