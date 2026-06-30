"""Fleet-wide default rate cards (task 62676443, mechanism B).

The per-org ``rate_cards`` row is an *override*; ``default_rate_card`` is
the *default salvo override*. These cover the behavioural contract:

- ``our_key`` with no per-org card now BILLS from the fleet default
  (before this it was free), idempotent per ``operation_id``;
- a model with NO default stays free (we did not make everything paid);
- a per-org override wins over the default;
- a fresh org with no card inherits the same fleet default;
- BYOK is unaffected (byok factor, not the card);
- ``local`` with no default is still free.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.billing import CostBasis, DefaultRateCard
from mycelium_core.services import billing
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="BILL")
    return r.org_id, r.user_id


def _model() -> str:
    """A globally-unique model id so the unique(model_id) default row
    never collides with the migration seed or another test."""
    return f"test-default-{uuid.uuid4().hex[:12]}"


async def _seed_default(
    model_id: str,
    *,
    cost_in: str = "1",
    cost_out: str = "2",
    markup: str = "1",
    provider: str = "scaleway",
) -> None:
    """Insert a fleet default (no org, no RLS) via a system session."""
    async with admin_session() as s:
        s.add(
            DefaultRateCard(
                model_id=model_id,
                provider=provider,
                provider_cost_per_input=Decimal(cost_in),
                provider_cost_per_output=Decimal(cost_out),
                markup=Decimal(markup),
            )
        )
        await s.flush()


async def test_our_key_bills_via_fleet_default_idempotent() -> None:
    org, user = await _org()
    model = _model()
    await _seed_default(model, cost_in="1", cost_out="2", markup="1")
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(100))
        r1 = await billing.meter_if_billable(
            s,
            org_id=org,
            actor_id=user,
            operation_id="ok-1",
            op="llm",
            model_id=model,
            units_in=Decimal(3),
            units_out=Decimal(4),
            basis=CostBasis.our_key,
        )
        assert r1 is not None
        bal1 = await billing.balance(s, org_id=org)
        # Re-run same op: idempotent, no second charge.
        r2 = await billing.meter_if_billable(
            s,
            org_id=org,
            actor_id=user,
            operation_id="ok-1",
            op="llm",
            model_id=model,
            units_in=Decimal(3),
            units_out=Decimal(4),
            basis=CostBasis.our_key,
        )
        bal2 = await billing.balance(s, org_id=org)
    assert r1.credits == Decimal("11.0000")  # (3*1 + 4*2) * 1
    assert r2 is not None and r1.id == r2.id
    assert bal1 == Decimal("89.0000") and bal2 == bal1


async def test_our_key_without_any_card_stays_free() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(10))
        rec = await billing.meter_if_billable(
            s,
            org_id=org,
            actor_id=user,
            operation_id="free-1",
            op="llm",
            model_id=_model(),  # no default seeded for this id
            units_in=Decimal(1000),
            units_out=Decimal(1000),
            basis=CostBasis.our_key,
        )
        assert rec is None  # no default, no override -> free
        assert await billing.balance(s, org_id=org) == Decimal(10)


async def test_per_org_override_beats_default() -> None:
    org, user = await _org()
    model = _model()
    await _seed_default(model, cost_in="1", cost_out="1", markup="1")
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(100))
        # Per-org override: more expensive than the default.
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id=model,
            provider="scaleway",
            values={"provider_cost_per_input": Decimal(5), "markup": Decimal(1)},
        )
        rec = await billing.meter_if_billable(
            s,
            org_id=org,
            actor_id=user,
            operation_id="ovr-1",
            op="llm",
            model_id=model,
            units_in=Decimal(2),
            units_out=Decimal(0),
            basis=CostBasis.our_key,
        )
    assert rec is not None
    assert rec.credits == Decimal("10.0000")  # 2*5 (override), not 2*1 (default)


async def test_two_orgs_inherit_same_fleet_default() -> None:
    a_org, a_user = await _org()
    b_org, b_user = await _org()
    model = _model()
    await _seed_default(model, cost_in="1", cost_out="2", markup="1")
    async with tenant_session(str(a_org), str(a_user)) as s:
        await billing.grant_credits(s, org_id=a_org, actor_id=a_user, amount=Decimal(100))
        ra = await billing.meter_if_billable(
            s,
            org_id=a_org,
            actor_id=a_user,
            operation_id="a-1",
            op="llm",
            model_id=model,
            units_in=Decimal(3),
            units_out=Decimal(4),
            basis=CostBasis.our_key,
        )
    async with tenant_session(str(b_org), str(b_user)) as s:
        await billing.grant_credits(s, org_id=b_org, actor_id=b_user, amount=Decimal(100))
        rb = await billing.meter_if_billable(
            s,
            org_id=b_org,
            actor_id=b_user,
            operation_id="b-1",
            op="llm",
            model_id=model,
            units_in=Decimal(3),
            units_out=Decimal(4),
            basis=CostBasis.our_key,
        )
    assert ra is not None and rb is not None
    assert ra.credits == Decimal("11.0000") == rb.credits  # same fleet default


async def test_byok_unaffected_by_default() -> None:
    org, user = await _org()
    model = _model()
    await _seed_default(model, cost_in="999", cost_out="999", markup="1")
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(10))
        rec = await billing.meter_if_billable(
            s,
            org_id=org,
            actor_id=user,
            operation_id="byok-d-1",
            op="llm",
            model_id=model,
            units_in=Decimal(1000),
            units_out=Decimal(500),
            basis=CostBasis.byok,
        )
    assert rec is not None
    assert rec.credits == Decimal("0.1500")  # 0.0001 * 1500, default ignored


async def test_local_without_default_is_free() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(10))
        rec = await billing.meter_if_billable(
            s,
            org_id=org,
            actor_id=user,
            operation_id="loc-1",
            op="embed",
            model_id=_model(),  # bundled local model: no default
            units_in=Decimal(5000),
            basis=CostBasis.local,
        )
        assert rec is None
        assert await billing.balance(s, org_id=org) == Decimal(10)
