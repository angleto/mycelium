"""Billing router: wallet balance, admin grant, metering, rate cards.
Thin adapter over the service layer (docs/adr/0001, 0019, FR-15).
Admin-gated writes are enforced in the service (RBAC choke point)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    BalanceOut,
    ByokFactorIn,
    GrantIn,
    LedgerOut,
    MeterIn,
    RateCardOut,
    RateCardUpsertIn,
    StorageRateIn,
    UsageOut,
)
from flow_core.models.billing import CreditLedger, RateCard, UsageRecord
from flow_core.services import billing as svc

router = APIRouter(prefix="/billing", tags=["billing"])


def _usage_out(r: UsageRecord) -> UsageOut:
    return UsageOut(
        id=r.id,
        operation_id=r.operation_id,
        model_id=r.model_id,
        op=r.op,
        basis=r.basis,
        units_in=r.units_in,
        units_out=r.units_out,
        credits=r.credits,
    )


def _rate_out(c: RateCard) -> RateCardOut:
    return RateCardOut(
        id=c.id,
        model_id=c.model_id,
        provider=c.provider,
        unit=c.unit,
        credits_per_input=c.credits_per_input,
        credits_per_output=c.credits_per_output,
        markup=c.markup,
        is_active=c.is_active,
        tier=c.tier,
        version=c.version,
    )


@router.get("/balance", response_model=BalanceOut)
async def get_balance(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> BalanceOut:
    return BalanceOut(balance=await svc.balance(ctx.session, org_id=ctx.org_id))


@router.post("/grant", response_model=BalanceOut)
async def grant(
    body: GrantIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> BalanceOut:
    new_balance = await svc.grant_credits(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        amount=body.amount,
        reason=body.reason,
    )
    return BalanceOut(balance=new_balance)


@router.post("/meter", response_model=UsageOut)
async def meter(
    body: MeterIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> UsageOut:
    record = await svc.meter(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        operation_id=body.operation_id,
        op=body.op,
        model_id=body.model_id,
        units_in=body.units_in,
        units_out=body.units_out,
        basis=body.basis,
    )
    return _usage_out(record)


@router.get("/ledger", response_model=list[LedgerOut])
async def ledger(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    limit: int = 100,
    offset: int = 0,
) -> list[LedgerOut]:
    rows = await svc.list_ledger(ctx.session, org_id=ctx.org_id, limit=limit, offset=offset)

    def _out(e: CreditLedger) -> LedgerOut:
        return LedgerOut(
            id=e.id,
            kind=e.kind.value,
            amount=e.amount,
            operation_id=e.operation_id,
            reason=e.reason,
            balance_after=e.balance_after,
        )

    return [_out(e) for e in rows]


@router.get("/usage", response_model=list[UsageOut])
async def usage(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    limit: int = 100,
) -> list[UsageOut]:
    rows = await svc.list_usage(ctx.session, org_id=ctx.org_id, limit=limit)
    return [_usage_out(r) for r in rows]


@router.get("/rate-cards", response_model=list[RateCardOut])
async def list_rate_cards(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[RateCardOut]:
    return [_rate_out(c) for c in await svc.list_rate_cards(ctx.session, org_id=ctx.org_id)]


@router.post("/rate-cards", response_model=RateCardOut)
async def upsert_rate_card(
    body: RateCardUpsertIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> RateCardOut:
    values = body.model_dump(exclude={"model_id", "provider"})
    card = await svc.upsert_rate_card(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        model_id=body.model_id,
        provider=body.provider,
        values=values,
    )
    return _rate_out(card)


@router.put("/storage-rate", response_model=BalanceOut)
async def set_storage_rate(
    body: StorageRateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> BalanceOut:
    row = await svc.set_storage_rate(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        kind=body.kind,
        credits_per_gb_month=body.credits_per_gb_month,
    )
    return BalanceOut(balance=row.credits_per_gb_month)


@router.put("/byok-factor", response_model=BalanceOut)
async def set_byok_factor(
    body: ByokFactorIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> BalanceOut:
    cfg = await svc.set_byok_factor(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        factor=body.factor,
    )
    return BalanceOut(balance=cfg.byok_fee_factor)
