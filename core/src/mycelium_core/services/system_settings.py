"""Global system settings: the runtime SdI environment switch (ADR-0011).

The active SdI environment ('test' | 'production') lives in the single-row
``system_settings`` table, not in an env var, so an admin flips it from
Settings without a redeploy. The two endpoint URLs are still config (env);
this only chooses which one the live RiceviFile send uses. Defaults to 'test'
(safe) when the row is absent.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.system_settings import SystemSettings

SDI_ENVIRONMENTS = ("test", "production")


async def _get_or_create(session: AsyncSession) -> SystemSettings:
    row = (await session.execute(select(SystemSettings))).scalar_one_or_none()
    if row is None:
        # The migration seeds the singleton; this only covers a DB that
        # predates it. Pinned-TRUE PK keeps it a singleton.
        row = SystemSettings(id=True, sdi_environment="test")
        session.add(row)
        await session.flush()
    return row


async def get_sdi_environment(session: AsyncSession) -> str:
    """The active SdI environment ('test' | 'production'); 'test' by default."""
    return (await _get_or_create(session)).sdi_environment


async def set_sdi_environment(session: AsyncSession, environment: str) -> SystemSettings:
    """Flip the active SdI environment. Rejects anything but test/production."""
    if environment not in SDI_ENVIRONMENTS:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"sdi_environment '{environment}'")
    row = await _get_or_create(session)
    row.sdi_environment = environment
    await session.flush()
    return row


def endpoint_for(environment: str) -> str:
    """The configured RiceviFile URL for an environment. Falls back to the
    legacy single ``sdi_endpoint_url`` when the env-specific one is unset."""
    s = get_settings()
    if environment == "production":
        return s.sdi_endpoint_url_prod or s.sdi_endpoint_url
    return s.sdi_endpoint_url_test or s.sdi_endpoint_url


async def resolve_sdi_endpoint(session: AsyncSession) -> str:
    """The endpoint URL the next live send should target, per the DB switch."""
    return endpoint_for(await get_sdi_environment(session))
