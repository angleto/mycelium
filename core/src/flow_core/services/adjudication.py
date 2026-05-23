"""Adjudication service entry-points (docs/adr/0027).

Thin orchestration layer over the framework: open the row, instantiate
or look up the strategy, run it through a ``DBStepStore``, persist the
outcome, return the entity. Streaming (SSE) is intentionally not part
of M1: the framework already writes steps as a side effect, so a
follow-up endpoint can consume them via polling or LISTEN/NOTIFY.

The service runs under the caller's tenant session (RLS + role
checks); it must never escalate authority.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.adjudication import (
    AdjudicationContext,
    AdjudicationStrategy,
    DBStepStore,
)
from flow_core.adjudication.policy import PolicyRouter
from flow_core.adjudication.registry import get_registry
from flow_core.adjudication.strategies import register_builtins
from flow_core.errors import NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.adjudication import (
    Adjudication,
    AdjudicationStatus,
    AdjudicationStep,
    AdjudicationStepKind,
)

# Idempotent at import-time: any service consumer triggers built-in
# strategy registration. Out-of-tree strategies still need an explicit
# ``get_registry().load_entry_points()`` call from the app bootstrap.
register_builtins()


async def start_adjudication(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    question_text: str,
    context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    strategy_id: str | None = None,
    task_id: uuid.UUID | None = None,
    n_agents_available: int | None = None,
    router: PolicyRouter | None = None,
) -> Adjudication:
    """Create + run an adjudication end-to-end.

    Selection precedence: ``strategy_id`` override > ``router`` rules
    > applicability auto-rank (see ``PolicyRouter.select``). The
    adjudication row is committed before the strategy starts so the
    timeline is visible even if the strategy aborts mid-run.
    """
    adj_id = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    row = Adjudication(
        id=adj_id,
        org_id=org_id,
        task_id=task_id,
        question_text=question_text,
        context_json=dict(context or {}),
        strategy_id="(pending)",
        strategy_config_json=dict(config or {}),
        status=AdjudicationStatus.running,
        started_at=now,
        created_by=actor_id,
    )
    session.add(row)
    await session.flush()

    ctx = AdjudicationContext(
        org_id=org_id,
        actor_id=actor_id,
        adjudication_id=adj_id,
        question_text=question_text,
        context=dict(context or {}),
        config=dict(config or {}),
        task_id=task_id,
        n_agents_available=n_agents_available,
    )

    # Resolve the strategy.
    if strategy_id is not None:
        strategy: AdjudicationStrategy = get_registry().get(strategy_id)
        effective_config = ctx.config
    elif router is not None:
        strategy, effective_config = router.select(ctx)
    else:
        ranked = get_registry().rank(ctx)
        if not ranked:
            raise LookupError("no adjudication strategy is applicable to the supplied context")
        strategy = ranked[0][1]
        effective_config = ctx.config

    row.strategy_id = strategy.id
    row.strategy_config_json = dict(effective_config)
    if effective_config is not ctx.config:
        ctx = AdjudicationContext(
            org_id=ctx.org_id,
            actor_id=ctx.actor_id,
            adjudication_id=ctx.adjudication_id,
            question_text=ctx.question_text,
            context=ctx.context,
            config=dict(effective_config),
            task_id=ctx.task_id,
            n_agents_available=ctx.n_agents_available,
        )

    store = DBStepStore(session, adjudication_id=adj_id, org_id=org_id)

    wall_start = time.monotonic()
    try:
        outcome = await strategy.run(ctx, store)
    except Exception as e:
        wall_ms = int((time.monotonic() - wall_start) * 1000)
        row.status = AdjudicationStatus.aborted
        row.outcome_json = {"error": repr(e)}
        row.confidence = None
        row.cost_wall_ms = wall_ms
        row.ended_at = dt.datetime.now(dt.UTC)
        await session.flush()
        raise
    wall_ms = int((time.monotonic() - wall_start) * 1000)

    if outcome.aborted_reason is not None:
        row.status = AdjudicationStatus.aborted
    elif outcome.escalated:
        row.status = AdjudicationStatus.escalated
    else:
        row.status = AdjudicationStatus.resolved

    row.outcome_json = {
        "decision": outcome.decision,
        "confidence": outcome.confidence,
        "residual_dissent": [
            {
                "agent_id": d.agent_id,
                "position": d.position,
                "rationale": d.rationale,
            }
            for d in outcome.residual_dissent
        ],
        "escalated": outcome.escalated,
        "aborted_reason": outcome.aborted_reason,
        "meta": outcome.meta,
    }
    row.confidence = Decimal(str(round(outcome.confidence, 3)))
    row.cost_wall_ms = wall_ms
    row.ended_at = dt.datetime.now(dt.UTC)
    await session.flush()
    return row


async def get_adjudication(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    adjudication_id: uuid.UUID,
) -> Adjudication:
    stmt = select(Adjudication).where(
        Adjudication.id == adjudication_id,
        Adjudication.org_id == org_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.ADJUDICATION_NOT_FOUND)
    return row


async def list_adjudication_steps(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    adjudication_id: uuid.UUID,
) -> list[AdjudicationStep]:
    # RLS filters on org_id; the explicit predicate is defence in
    # depth and lets the planner skip the policy when running as the
    # owner role.
    stmt = (
        select(AdjudicationStep)
        .where(
            AdjudicationStep.adjudication_id == adjudication_id,
            AdjudicationStep.org_id == org_id,
        )
        .order_by(AdjudicationStep.step_no)
    )
    return list((await session.execute(stmt)).scalars().all())


async def resolve_escalation(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    adjudication_id: uuid.UUID,
    decision: dict[str, Any],
    rationale: str | None = None,
) -> Adjudication:
    """Close an ``escalated`` adjudication with the human's decision.

    Writes an ``intervention`` step (audit), sets the outcome's
    ``decision`` field and flips status to ``resolved``. Idempotent
    only by the caller's intent: re-calling on a resolved
    adjudication raises ``ValueError`` so a stale UI cannot quietly
    overwrite a finalised decision.
    """
    adj = await get_adjudication(session, org_id=org_id, adjudication_id=adjudication_id)
    if adj.status != AdjudicationStatus.escalated:
        raise ValueError(
            f"adjudication {adjudication_id} is not in 'escalated' status (current: {adj.status})"
        )

    # Append the intervention step on the same timeline. RLS scopes
    # the insert; the step is append-only.
    last_step = (
        await session.execute(
            select(AdjudicationStep.step_no)
            .where(AdjudicationStep.adjudication_id == adjudication_id)
            .order_by(AdjudicationStep.step_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    next_step_no = (last_step or 0) + 1

    session.add(
        AdjudicationStep(
            org_id=org_id,
            adjudication_id=adjudication_id,
            step_no=next_step_no,
            kind=AdjudicationStepKind.intervention,
            payload_json={
                "actor_id": str(actor_id),
                "decision": decision,
                "rationale": rationale,
            },
            agent_id=f"user:{actor_id}",
            created_at=dt.datetime.now(dt.UTC),
        )
    )

    outcome = dict(adj.outcome_json or {})
    outcome["decision"] = decision
    outcome["escalated"] = False
    outcome.setdefault("meta", {})
    outcome["meta"]["resolved_by"] = str(actor_id)
    if rationale is not None:
        outcome["meta"]["resolve_rationale"] = rationale
    adj.outcome_json = outcome
    adj.status = AdjudicationStatus.resolved
    adj.confidence = Decimal("1.000")
    adj.ended_at = dt.datetime.now(dt.UTC)
    await session.flush()
    return adj
