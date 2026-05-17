"""F5b billing/metering (DB-backed), ADR-0019/FR-15 verification.

Append-only ledger, admin grant, idempotent metered debit, atomic
check-and-debit with no overdraft under concurrency, insufficient
credits, BYOK platform-fee basis, cross-org isolation.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, update

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.models.billing import CostBasis, CreditLedger
from flow_core.services import billing
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="BILL")
    return r.org_id, r.user_id


async def test_grant_and_append_only_ledger() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        bal = await billing.grant_credits(
            s, org_id=org, actor_id=user, amount=Decimal(100), reason="seed"
        )
        assert bal == Decimal(100)
        assert await billing.balance(s, org_id=org) == Decimal(100)
        entries = await billing.list_ledger(s, org_id=org)
        assert len(entries) == 1 and entries[0].kind.value == "grant"
        # Append-only: UPDATE/DELETE rejected by the DB trigger.
        with pytest.raises(Exception):  # noqa: B017 (DBAPIError from trigger)
            await s.execute(
                update(CreditLedger)
                .where(CreditLedger.id == entries[0].id)
                .values(amount=Decimal(1))
            )
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(Exception):  # noqa: B017
            await s.execute(delete(CreditLedger))


async def test_meter_is_idempotent() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(100))
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id="m1",
            provider="local",
            values={"credits_per_input": Decimal(1), "credits_per_output": Decimal(2)},
        )
        r1 = await billing.meter(
            s,
            org_id=org,
            actor_id=user,
            operation_id="op-1",
            op="llm",
            model_id="m1",
            units_in=Decimal(3),
            units_out=Decimal(4),
        )
        bal1 = await billing.balance(s, org_id=org)
        r2 = await billing.meter(
            s,
            org_id=org,
            actor_id=user,
            operation_id="op-1",
            op="llm",
            model_id="m1",
            units_in=Decimal(3),
            units_out=Decimal(4),
        )
        bal2 = await billing.balance(s, org_id=org)
    assert r1.credits == Decimal("11.0000")  # 3*1 + 4*2
    assert r1.id == r2.id  # same record, not charged twice
    assert bal1 == Decimal("89.0000") and bal2 == bal1


async def test_insufficient_credits_rejected() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(5))
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id="m1",
            provider="local",
            values={"credits_per_input": Decimal(10)},
        )
        with pytest.raises(DomainError):
            await billing.meter(
                s,
                org_id=org,
                actor_id=user,
                operation_id="op-x",
                op="llm",
                model_id="m1",
                units_in=Decimal(1),
            )
        assert await billing.balance(s, org_id=org) == Decimal(5)


async def test_byok_platform_fee_basis() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(10))
        # No rate card needed for BYOK; default factor 0.0001.
        r = await billing.meter(
            s,
            org_id=org,
            actor_id=user,
            operation_id="byok-1",
            op="llm",
            units_in=Decimal(1000),
            units_out=Decimal(500),
            basis=CostBasis.byok,
        )
        assert r.credits == Decimal("0.1500")  # 0.0001 * 1500


async def test_atomic_check_and_debit_no_overdraft() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(10))
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id="m1",
            provider="local",
            values={"credits_per_input": Decimal(10)},
        )

    async def one(op_id: str) -> bool:
        async with tenant_session(str(org), str(user)) as s:
            try:
                await billing.meter(
                    s,
                    org_id=org,
                    actor_id=user,
                    operation_id=op_id,
                    op="llm",
                    model_id="m1",
                    units_in=Decimal(1),  # costs 10; balance only covers one
                )
                return True
            except DomainError:
                return False

    results = await asyncio.gather(one("c-a"), one("c-b"))
    assert sorted(results) == [False, True]  # exactly one succeeded
    async with tenant_session(str(org), str(user)) as s:
        assert await billing.balance(s, org_id=org) == Decimal(0)


async def test_wallet_is_org_isolated() -> None:
    a_org, a_user = await _org()
    b_org, b_user = await _org()
    async with tenant_session(str(a_org), str(a_user)) as s:
        await billing.grant_credits(s, org_id=a_org, actor_id=a_user, amount=Decimal(50))
    async with tenant_session(str(b_org), str(b_user)) as s:
        assert await billing.balance(s, org_id=b_org) == Decimal(0)
        assert await billing.list_ledger(s, org_id=b_org) == []
