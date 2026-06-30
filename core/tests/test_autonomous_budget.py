"""Per-workspace autonomous budget + kill-switch (WS-F5)."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from sqlalchemy import update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.billing import CostBasis, UsageRecord
from mycelium_core.models.organization import Organization
from mycelium_core.services import autonomous_budget as ab
from mycelium_core.services import billing
from mycelium_core.services.auth import signup


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="AB",
        )
    return r.org_id, r.user_id


def _usage(org: uuid.UUID, *, actor_kind: str, credits: str) -> UsageRecord:
    return UsageRecord(
        org_id=org,
        operation_id=uuid.uuid4().hex,
        model_id="m",
        op="embed",
        basis=CostBasis.local,
        credits=Decimal(credits),
        actor_kind=actor_kind,
    )


async def test_meter_records_actor_kind() -> None:
    """meter() stamps the UsageRecord with the session's actor kind, so the
    autonomous (system) spend is distinguishable from user spend."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id="fake",
            provider="local",
            values={"credits_per_input": Decimal("0.001")},
        )
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        rec = await billing.meter(
            s,
            org_id=org,
            actor_id=user,
            operation_id="sys1",
            op="embed",
            model_id="fake",
            units_in=Decimal(100),
            basis=CostBasis.local,
        )
    assert rec.actor_kind == "system"
    async with tenant_session(str(org), str(user)) as s:  # default human_direct
        rec2 = await billing.meter(
            s,
            org_id=org,
            actor_id=user,
            operation_id="hum1",
            op="embed",
            model_id="fake",
            units_in=Decimal(100),
            basis=CostBasis.local,
        )
    assert rec2.actor_kind == "human_direct"


async def test_status_kill_switch_cap_and_user_spend_excluded() -> None:
    """status() pauses on the kill-switch or a breached daily cap, counts
    only system spend (user spend never throttles the autonomous jobs), and
    runs free (no spend query) when uncapped."""
    org, user = await _org()
    now = datetime.datetime.now(datetime.UTC)
    async with tenant_session(str(org), str(user)) as s:
        # Default: enabled, no cap -> never paused.
        st = await ab.status(s, org_id=org, now=now)
        assert st.paused is False
        assert st.cap is None
        # Today: 5 credits of system spend; 100 of user spend (must not count).
        s.add(_usage(org, actor_kind="system", credits="5"))
        s.add(_usage(org, actor_kind="human_direct", credits="100"))
        await s.flush()
        # Cap above the system spend -> running; user spend excluded.
        await s.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={ab.DAILY_CAP_KEY: 10})
        )
        st = await ab.status(s, org_id=org, now=now)
        assert st.paused is False
        assert st.spent_today == Decimal(5)
        assert st.cap == Decimal(10)
        # Cap at/below the system spend -> paused (budget_exceeded).
        await s.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={ab.DAILY_CAP_KEY: 3})
        )
        st = await ab.status(s, org_id=org, now=now)
        assert st.paused is True
        assert st.reason == "budget_exceeded"
        # Kill-switch off -> paused regardless of cap.
        await s.execute(
            update(Organization)
            .where(Organization.id == org)
            .values(settings={ab.ENABLED_KEY: False, ab.DAILY_CAP_KEY: 3})
        )
        st = await ab.status(s, org_id=org, now=now)
        assert st.paused is True
        assert st.reason == "kill_switch"
        assert st.enabled is False
