"""Credit metering core (docs/adr/0019, FR-15).

The single choke point for cost enforcement (like RBAC). Debits are
idempotent by ``operation_id``; the balance changes under an atomic
check-and-debit (``SELECT ... FOR UPDATE``) so concurrent metered
operations cannot overdraw. Ledger and usage rows are append-only
(DB trigger). Admin grants credits and edits rate cards; a payment
gateway is out of v1.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.billing import (
    BillingConfig,
    CostBasis,
    CreditLedger,
    LedgerEntryKind,
    RateCard,
    StorageKind,
    StorageRate,
    UsageRecord,
    Wallet,
)
from flow_core.models.membership import Role
from flow_core.services import audit
from flow_core.services.rbac import require_role

_RATE_UPDATABLE = frozenset(
    {
        "provider",
        "unit",
        "credits_per_input",
        "credits_per_output",
        "provider_cost_per_input",
        "provider_cost_per_output",
        "markup",
        "is_active",
        "tier",
    }
)


async def get_wallet(session: AsyncSession, org_id: uuid.UUID) -> Wallet:
    """Lazily create the per-org wallet (balance 0)."""
    w = (await session.execute(select(Wallet).where(Wallet.org_id == org_id))).scalar_one_or_none()
    if w is not None:
        return w
    try:
        async with session.begin_nested():
            w = Wallet(org_id=org_id, balance=Decimal(0))
            session.add(w)
            await session.flush()
        await session.refresh(w)  # adopt the Numeric(18,4) scale
        return w
    except IntegrityError:
        return (await session.execute(select(Wallet).where(Wallet.org_id == org_id))).scalar_one()


async def get_billing_config(session: AsyncSession, org_id: uuid.UUID) -> BillingConfig:
    c = (
        await session.execute(select(BillingConfig).where(BillingConfig.org_id == org_id))
    ).scalar_one_or_none()
    if c is not None:
        return c
    try:
        async with session.begin_nested():
            c = BillingConfig(org_id=org_id)
            session.add(c)
            await session.flush()
        await session.refresh(c)  # adopt the Numeric(18,8) scale
        return c
    except IntegrityError:
        return (
            await session.execute(select(BillingConfig).where(BillingConfig.org_id == org_id))
        ).scalar_one()


async def balance(session: AsyncSession, *, org_id: uuid.UUID) -> Decimal:
    return (await get_wallet(session, org_id)).balance


async def grant_credits(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    amount: Decimal,
    reason: str | None = None,
) -> Decimal:
    """Admin tops up credits (v1 manual grant). Atomic + audited."""
    if amount <= 0:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.admin)
    await get_wallet(session, org_id)
    locked = (
        await session.execute(select(Wallet).where(Wallet.org_id == org_id).with_for_update())
    ).scalar_one()
    new_balance = locked.balance + amount
    # Pessimistic FOR UPDATE lock (ADR-0019): mutate the locked row
    # directly. The wallet PK is org_id (no surrogate id), so the
    # id-based optimistic_update does not apply here.
    locked.balance = new_balance
    locked.version += 1
    session.add(
        CreditLedger(
            org_id=org_id,
            kind=LedgerEntryKind.grant,
            amount=amount,
            operation_id=None,
            reason=reason,
            balance_after=new_balance,
            created_by=actor_id,
        )
    )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="wallet",
        entity_id=None,
        action="grant",
        diff={"amount": str(amount)},
    )
    return new_balance


async def ensure_credits(session: AsyncSession, *, org_id: uuid.UUID, needed: Decimal) -> None:
    """Cheap pre-check (the authoritative guard is the atomic debit in
    :func:`meter`)."""
    bal = await balance(session, org_id=org_id)
    if bal < needed:
        raise DomainError(
            MessageCode.INSUFFICIENT_CREDITS,
            needed=str(needed),
            balance=str(bal),
        )


async def _active_rate_card(session: AsyncSession, model_id: str | None) -> RateCard | None:
    if model_id is None:
        return None
    return (
        await session.execute(
            select(RateCard).where(RateCard.model_id == model_id, RateCard.is_active.is_(True))
        )
    ).scalar_one_or_none()


def _compute_credits(
    *,
    basis: CostBasis,
    units_in: Decimal,
    units_out: Decimal,
    rate: RateCard | None,
    byok_factor: Decimal,
) -> Decimal:
    if basis is CostBasis.byok:
        return (byok_factor * (units_in + units_out)).quantize(Decimal("0.0001"))
    if rate is None:
        raise DomainError(MessageCode.RATE_CARD_NOT_FOUND, model_id="?")
    if basis is CostBasis.our_key and rate.provider_cost_per_input is not None:
        raw = units_in * rate.provider_cost_per_input + units_out * (
            rate.provider_cost_per_output or Decimal(0)
        )
        return (raw * rate.markup).quantize(Decimal("0.0001"))
    raw = units_in * rate.credits_per_input + units_out * rate.credits_per_output
    return raw.quantize(Decimal("0.0001"))


async def meter(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    operation_id: str,
    op: str,
    model_id: str | None = None,
    units_in: Decimal = Decimal(0),
    units_out: Decimal = Decimal(0),
    basis: CostBasis = CostBasis.local,
) -> UsageRecord:
    """Idempotent metered debit. Re-running the same ``operation_id``
    returns the prior record without charging again. The wallet row is
    locked FOR UPDATE so concurrent meters cannot overdraw."""
    existing = (
        await session.execute(
            select(UsageRecord).where(
                UsageRecord.operation_id == operation_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    rate = await _active_rate_card(session, model_id)
    if basis is not CostBasis.byok and rate is None:
        raise DomainError(MessageCode.RATE_CARD_NOT_FOUND, model_id=model_id or "?")
    cfg = await get_billing_config(session, org_id)
    credits = _compute_credits(
        basis=basis,
        units_in=units_in,
        units_out=units_out,
        rate=rate,
        byok_factor=cfg.byok_fee_factor,
    )

    await get_wallet(session, org_id)
    locked = (
        await session.execute(select(Wallet).where(Wallet.org_id == org_id).with_for_update())
    ).scalar_one()
    if locked.balance < credits:
        raise DomainError(
            MessageCode.INSUFFICIENT_CREDITS,
            needed=str(credits),
            balance=str(locked.balance),
        )
    new_balance = locked.balance - credits
    try:
        async with session.begin_nested():
            # Pessimistic FOR UPDATE lock holds; mutate the locked row
            # (wallet PK is org_id, no surrogate id).
            locked.balance = new_balance
            locked.version += 1
            record = UsageRecord(
                org_id=org_id,
                operation_id=operation_id,
                model_id=model_id,
                op=op,
                basis=basis,
                units_in=units_in,
                units_out=units_out,
                credits=credits,
                created_by=actor_id,
            )
            session.add(record)
            session.add(
                CreditLedger(
                    org_id=org_id,
                    kind=LedgerEntryKind.debit,
                    amount=credits,
                    operation_id=operation_id,
                    reason=op,
                    balance_after=new_balance,
                    created_by=actor_id,
                )
            )
            await session.flush()
    except IntegrityError:
        # Concurrent duplicate of the same operation_id: already metered.
        return (
            await session.execute(
                select(UsageRecord).where(UsageRecord.operation_id == operation_id)
            )
        ).scalar_one()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="usage_record",
        entity_id=record.id,
        action="meter",
        diff={"op": op, "credits": str(credits)},
    )
    return record


# --- Admin: rate cards, storage rates, billing config ---


async def upsert_rate_card(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    model_id: str,
    provider: str,
    values: dict[str, Any] | None = None,
) -> RateCard:
    await require_role(session, org_id, actor_id, Role.admin)
    extra = values or {}
    if set(extra) - _RATE_UPDATABLE:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    existing = (
        await session.execute(select(RateCard).where(RateCard.model_id == model_id))
    ).scalar_one_or_none()
    if existing is None:
        card = RateCard(org_id=org_id, model_id=model_id, provider=provider, **extra)
        session.add(card)
        await session.flush()
        action = "rate_card_create"
        result = card
    else:
        await optimistic_update(
            session,
            RateCard,
            pk=existing.id,
            expected_version=existing.version,
            values={"provider": provider, **extra},
        )
        action = "rate_card_update"
        result = (
            await session.execute(select(RateCard).where(RateCard.id == existing.id))
        ).scalar_one()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="rate_card",
        entity_id=result.id,
        action=action,
    )
    return result


async def list_rate_cards(session: AsyncSession, *, org_id: uuid.UUID) -> list[RateCard]:
    return list(
        (await session.execute(select(RateCard).order_by(RateCard.model_id))).scalars().all()
    )


async def set_storage_rate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: StorageKind,
    credits_per_gb_month: Decimal,
) -> StorageRate:
    await require_role(session, org_id, actor_id, Role.admin)
    existing = (
        await session.execute(select(StorageRate).where(StorageRate.kind == kind))
    ).scalar_one_or_none()
    if existing is None:
        row = StorageRate(org_id=org_id, kind=kind, credits_per_gb_month=credits_per_gb_month)
        session.add(row)
        await session.flush()
    else:
        existing.credits_per_gb_month = credits_per_gb_month
        existing.version += 1
        await session.flush()
        row = existing
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="storage_rate",
        entity_id=None,
        action="set",
        diff={"kind": kind.value, "rate": str(credits_per_gb_month)},
    )
    return row


async def set_byok_factor(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    factor: Decimal,
) -> BillingConfig:
    if factor < 0:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.admin)
    cfg = await get_billing_config(session, org_id)
    # billing_config PK is org_id (no surrogate id); mutate directly.
    cfg.byok_fee_factor = factor
    cfg.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="billing_config",
        entity_id=None,
        action="set_byok_factor",
        diff={"factor": str(factor)},
    )
    return cfg


async def list_ledger(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
) -> list[CreditLedger]:
    return list(
        (
            await session.execute(
                select(CreditLedger)
                .order_by(CreditLedger.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def list_usage(
    session: AsyncSession, *, org_id: uuid.UUID, limit: int = 100
) -> list[UsageRecord]:
    return list(
        (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
