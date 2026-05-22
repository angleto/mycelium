"""SdI transmission mandate service (docs/adr/0011, FR-9).

Grant/revoke the authorization a VAT subject (issuer profile) gives Flow to
transmit its invoices through the accredited SdICoop channel. Owner/admin
action, audited. ``invoice.transmit`` consults ``get_active_mandate`` before
sending via the intermediary channel; manual export needs no mandate (the
tenant submits its own document).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.invoice import IssuerProfile
from flow_core.models.membership import Role
from flow_core.models.sdi_mandate import SdiMandate, SdiMandateStatus
from flow_core.services import audit
from flow_core.services.rbac import require_role


async def _require_issuer(
    session: AsyncSession, *, org_id: uuid.UUID, issuer_profile_id: uuid.UUID
) -> None:
    """Validate the issuer profile exists in this org (RLS-scoped). Kept
    local to avoid importing services.invoice (which imports this module)."""
    p = (
        await session.execute(select(IssuerProfile).where(IssuerProfile.id == issuer_profile_id))
    ).scalar_one_or_none()
    if p is None:
        raise NotFoundError(MessageCode.FISCAL_PROFILE_REQUIRED, detail="profile")


async def get_active_mandate(
    session: AsyncSession, *, org_id: uuid.UUID, issuer_profile_id: uuid.UUID
) -> SdiMandate | None:
    return (
        await session.execute(
            select(SdiMandate).where(
                SdiMandate.issuer_profile_id == issuer_profile_id,
                SdiMandate.status == SdiMandateStatus.active,
            )
        )
    ).scalar_one_or_none()


async def list_mandates(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    issuer_profile_id: uuid.UUID | None = None,
) -> list[SdiMandate]:
    stmt = select(SdiMandate).order_by(SdiMandate.granted_at.desc())
    if issuer_profile_id is not None:
        stmt = stmt.where(SdiMandate.issuer_profile_id == issuer_profile_id)
    return list((await session.execute(stmt)).scalars().all())


async def grant_mandate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
    reference: str | None = None,
) -> SdiMandate:
    """Authorize Flow to transmit this VAT subject's invoices. Idempotent:
    returns the existing active mandate if one is already in force."""
    await require_role(session, org_id, actor_id, Role.admin)
    await _require_issuer(session, org_id=org_id, issuer_profile_id=issuer_profile_id)
    existing = await get_active_mandate(session, org_id=org_id, issuer_profile_id=issuer_profile_id)
    if existing is not None:
        return existing
    mandate = SdiMandate(org_id=org_id, issuer_profile_id=issuer_profile_id, reference=reference)
    session.add(mandate)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="sdi_mandate",
        entity_id=mandate.id,
        action="grant",
    )
    return mandate


async def revoke_mandate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
) -> SdiMandate:
    await require_role(session, org_id, actor_id, Role.admin)
    mandate = await get_active_mandate(session, org_id=org_id, issuer_profile_id=issuer_profile_id)
    if mandate is None:
        raise NotFoundError(MessageCode.MANDATE_NOT_FOUND)
    mandate.status = SdiMandateStatus.revoked
    mandate.revoked_at = dt.datetime.now(tz=dt.UTC)
    mandate.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="sdi_mandate",
        entity_id=mandate.id,
        action="revoke",
    )
    return mandate
