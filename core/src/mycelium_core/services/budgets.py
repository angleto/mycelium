"""Budget envelope service (docs/adr/0014, FR-14).

CRUD plus deterministic consumption: how much of an envelope the
attached tasks' ``monetary_cost`` consumes, and the residual. RBAC at
member level (a personal budget lives in a possibly mono-person org);
optimistic concurrency; audit; i18n.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.concurrency import optimistic_update
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.budget import Budget, BudgetPeriod
from mycelium_core.models.membership import Role
from mycelium_core.models.task import Task
from mycelium_core.services import audit
from mycelium_core.services.rbac import require_role

_UPDATABLE = frozenset(
    {
        "name",
        "category",
        "period_kind",
        "period_start",
        "period_end",
        "amount",
        "currency",
    }
)


@dataclass(frozen=True)
class Consumption:
    budget_id: uuid.UUID
    amount: Decimal
    currency: str
    consumed: Decimal
    residual: Decimal
    task_count: int


async def get_budget(session: AsyncSession, *, org_id: uuid.UUID, budget_id: uuid.UUID) -> Budget:
    b = (await session.execute(select(Budget).where(Budget.id == budget_id))).scalar_one_or_none()
    if b is None:
        raise NotFoundError(MessageCode.BUDGET_NOT_FOUND)
    return b


async def create_budget(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    period_kind: BudgetPeriod,
    period_start: dt.date,
    period_end: dt.date,
    amount: Decimal,
    currency: str = "EUR",
    category: str | None = None,
) -> Budget:
    await require_role(session, org_id, actor_id, Role.member)
    if period_end < period_start or amount < 0:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    b = Budget(
        org_id=org_id,
        name=name,
        category=category,
        period_kind=period_kind,
        period_start=period_start,
        period_end=period_end,
        amount=amount,
        currency=currency,
    )
    session.add(b)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="budget",
        entity_id=b.id,
        action="create",
    )
    return b


async def list_budgets(session: AsyncSession, *, org_id: uuid.UUID) -> list[Budget]:
    return list(
        (await session.execute(select(Budget).order_by(Budget.period_start.desc(), Budget.name)))
        .scalars()
        .all()
    )


async def update_budget(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    budget_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    if not values or set(values) - _UPDATABLE:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    await get_budget(session, org_id=org_id, budget_id=budget_id)
    new_version = await optimistic_update(
        session,
        Budget,
        pk=budget_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="budget",
        entity_id=budget_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def delete_budget(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    budget_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    b = await get_budget(session, org_id=org_id, budget_id=budget_id)
    await session.delete(b)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="budget",
        entity_id=budget_id,
        action="delete",
    )


async def consumption(
    session: AsyncSession, *, org_id: uuid.UUID, budget_id: uuid.UUID
) -> Consumption:
    """Deterministic: sum monetary_cost of active (not deleted, not
    archived) tasks attached to the budget; residual = amount - consumed."""
    b = await get_budget(session, org_id=org_id, budget_id=budget_id)
    total, count = (
        await session.execute(
            select(
                func.coalesce(func.sum(Task.monetary_cost), 0),
                func.count(Task.id),
            ).where(
                Task.budget_id == budget_id,
                Task.deleted_at.is_(None),
                Task.is_archived.is_(False),
                Task.monetary_cost.is_not(None),
            )
        )
    ).one()
    consumed = Decimal(total)
    return Consumption(
        budget_id=budget_id,
        amount=b.amount,
        currency=b.currency,
        consumed=consumed,
        residual=b.amount - consumed,
        task_count=int(count),
    )
