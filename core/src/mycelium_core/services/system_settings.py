"""Global system settings: the runtime SdI environment switch (ADR-0011).

The active SdI environment ('test' | 'production') lives in the single-row
``system_settings`` table, not in an env var, so an admin flips it from
Settings without a redeploy. The two endpoint URLs are still config (env);
this only chooses which one the live RiceviFile send uses. Defaults to 'test'
(safe) when the row is absent.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.system_settings import SystemSettings

SDI_ENVIRONMENTS = ("test", "production")

#: An Italian fiscal identifier: 11 digits (a company, whose codice fiscale IS
#: its P.IVA) or the 16-character personal codice fiscale. FatturaPA 1.1.1.2
#: asks for the CODICE FISCALE of the trasmittente and says so plainly
#: ("per i soggetti stabiliti nel territorio dello Stato Italiano corrisponde
#: al Codice Fiscale"), and SdI verifies it in Anagrafe Tributaria AS a codice
#: fiscale: "se non esiste come codice fiscale, il file viene scartato con
#: codice errore 00300" (Allegato A, Specifiche tecniche 1.9.1, §2.1.1).
#: Shape only. It cannot tell whether an 11-digit value is a company's codice
#: fiscale (correct) or a physical person's P.IVA (scarto 00300), because that
#: distinction is not in the string -- see ``sdi_intermediary_warning``.
_IT_FISCAL_CODE = re.compile(r"^(?:[0-9]{11}|[A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z])$")


async def get_sdi_intermediary_id_codice(session: AsyncSession) -> str:
    """The accredited channel's fiscal code: the DB value, else the env one.

    The fallback is what makes the move out of the ConfigMap expand-only. Once
    an operator sets it from Settings the DB wins and the env var can go; until
    then a deployment that never opens the page keeps transmitting exactly as
    before.
    """
    row = await _get_or_create(session)
    return row.sdi_intermediary_id_codice or get_settings().sdi_intermediary_id_codice


async def get_sdi_intermediary_override(session: AsyncSession) -> str:
    """The stored value ALONE, without the env fallback.

    The admin surface needs to tell "configured here" from "still showing the
    deployment's value", which the resolved value cannot express: they are the
    same string when the two agree.
    """
    return (await _get_or_create(session)).sdi_intermediary_id_codice


async def set_sdi_intermediary_id_codice(session: AsyncSession, code: str) -> SystemSettings:
    """Set it, refusing a value that is not shaped like an Italian fiscal code.

    Empty clears the override and returns the deployment to its env value.
    """
    value = (code or "").strip().upper()
    if value and not _IT_FISCAL_CODE.match(value):
        raise DomainError(
            MessageCode.SDI_INTERMEDIARY_CODE_INVALID,
            detail=value[:32],
        )
    row = await _get_or_create(session)
    row.sdi_intermediary_id_codice = value
    await session.flush()
    return row


def sdi_intermediary_warning(code: str) -> str | None:
    """The one thing the shape cannot decide, surfaced as a warning.

    An 11-digit value is correct for a company (whose codice fiscale is its
    P.IVA) and WRONG for a physical person, whose codice fiscale is the
    16-character form and whose 11-digit P.IVA does not exist in Anagrafe
    Tributaria as a codice fiscale. The string carries no clue which case it
    is, so this cannot be a refusal -- it would block every company. It is
    shown next to the field instead, because the failure it predicts is
    otherwise invisible until SdI scarta a real invoice with 00300, by which
    time the numero, the ProgressivoInvio and the NomeFile are already durable.
    """
    if len(code) == 11 and code.isdigit():
        return "physical_person_must_use_16_char_cf"
    return None


async def _get_or_create(session: AsyncSession) -> SystemSettings:
    """The singleton, created on the spot if a database somehow lacks it.

    Migration 0003 seeds the row, so this is a fallback -- but it must be
    a CONCURRENCY-SAFE one. It used to be a plain check-then-INSERT, and
    when the 2026-08-22 squash dropped 0074's seed the first concurrent
    readers of a fresh database all found nothing and all tried to insert:
    the ``id IS TRUE`` primary key let exactly one through and the rest
    died on UniqueViolation. A fallback that only works single-threaded
    turns a missing row into a hard failure instead of self-healing.

    ``ON CONFLICT DO NOTHING`` then re-select: the insert never raises,
    and the re-select returns whichever row won -- ours or the racer's.
    """
    row = (await session.execute(select(SystemSettings))).scalar_one_or_none()
    if row is not None:
        return row
    await session.execute(
        pg_insert(SystemSettings)
        .values(id=True, sdi_environment="test")
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.flush()
    # Re-read rather than trusting our own INSERT: under a race the row
    # that exists may be the other transaction's.
    return (await session.execute(select(SystemSettings))).scalar_one()


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
