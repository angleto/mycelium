"""Per-workspace budget + kill-switch for the autonomous metabolism (WS-F5).

The garden sweep and the embedding backfill run on their own, as
``actor_kind='system'``, and meter their spend like any user action. Left
unbounded they would burn credits with no human in the loop. This service
is the brake: a per-workspace kill-switch and a daily spend cap that pause
the *autonomous* jobs alone -- user actions never pass through here, so
they are never throttled (ec88362f G19).

Two knobs live in ``Organization.settings`` (same bag as the retrieval
floors), tuned live from the SPA:

- ``autonomous_jobs_enabled`` (default true): the kill-switch.
- ``autonomous_daily_credit_cap`` (default 0 = unlimited): pause once
  today's system spend reaches it.

``status`` is read at the top of each per-workspace tick; a paused
workspace is skipped (and the skip is logged + observable as the
``autonomous_spend_today`` garden-health sensor).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.organization import Organization
from mycelium_core.services import billing

ENABLED_KEY = "autonomous_jobs_enabled"
DAILY_CAP_KEY = "autonomous_daily_credit_cap"


@dataclass(frozen=True)
class AutonomousBudgetStatus:
    paused: bool
    # None when running; "kill_switch" or "budget_exceeded" when paused.
    reason: str | None
    spent_today: Decimal
    # The configured daily cap, or None when uncapped.
    cap: Decimal | None
    enabled: bool


def _start_of_utc_day(now: datetime.datetime) -> datetime.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _settings(session: AsyncSession, org_id: uuid.UUID) -> tuple[bool, Decimal | None]:
    raw = (
        await session.execute(select(Organization.settings).where(Organization.id == org_id))
    ).scalar_one_or_none()
    bag = raw if isinstance(raw, dict) else {}
    enabled = bool(bag.get(ENABLED_KEY, True))
    cap: Decimal | None = None
    try:
        c = Decimal(str(bag.get(DAILY_CAP_KEY, 0)))
        cap = c if c > 0 else None
    except (TypeError, ValueError, ArithmeticError):
        cap = None
    return enabled, cap


async def status(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> AutonomousBudgetStatus:
    """Whether the autonomous jobs may run for this workspace right now.

    Paused when the kill-switch is off, or when today's system spend has
    reached the daily cap. The spend query is skipped when there is no cap
    (the common case), so an unconfigured workspace pays nothing.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    enabled, cap = await _settings(session, org_id)
    if not enabled:
        return AutonomousBudgetStatus(
            paused=True, reason="kill_switch", spent_today=Decimal(0), cap=cap, enabled=False
        )
    if cap is None:
        return AutonomousBudgetStatus(
            paused=False, reason=None, spent_today=Decimal(0), cap=None, enabled=True
        )
    spent = await billing.system_spend_since(session, org_id=org_id, since=_start_of_utc_day(now))
    if spent >= cap:
        return AutonomousBudgetStatus(
            paused=True, reason="budget_exceeded", spent_today=spent, cap=cap, enabled=True
        )
    return AutonomousBudgetStatus(
        paused=False, reason=None, spent_today=spent, cap=cap, enabled=True
    )


__all__ = ["DAILY_CAP_KEY", "ENABLED_KEY", "AutonomousBudgetStatus", "status"]
