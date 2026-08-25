"""Admin router: the global SdI environment switch (test <-> production).

Gated by ``require_admin`` (a User capability + active elevation), like
``admin_users``. The setting is global (single-row ``system_settings``), so it
uses an admin session and never touches tenant data. Flipping to 'production'
means the next real invoice transmit targets the production RiceviFile endpoint
-- a real fiscal send -- without a redeploy.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from mycelium_api.deps import require_admin
from mycelium_api.schemas import SdiEnvironmentIn, SdiEnvironmentOut, SdiIntermediaryIn
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.models.user import User
from mycelium_core.services import system_settings as svc

router = APIRouter(prefix="/admin/sdi-environment", tags=["admin"])


def _out(environment: str, id_codice: str = "") -> SdiEnvironmentOut:
    s = get_settings()
    effective = id_codice
    return SdiEnvironmentOut(
        environment=environment,
        sdicoop_active=s.sdicoop_active,
        test_url=s.sdi_endpoint_url_test or s.sdi_endpoint_url,
        prod_url=s.sdi_endpoint_url_prod or s.sdi_endpoint_url,
        active_endpoint=svc.endpoint_for(environment),
        intermediary_id_paese=s.sdi_intermediary_id_paese,
        intermediary_id_codice=effective,
        intermediary_id_codice_warning=svc.sdi_intermediary_warning(effective),
        client_cert_configured=bool(s.sdi_client_cert),
        client_key_configured=bool(s.sdi_client_key),
        ca_bundle_configured=bool(s.sdi_ca_bundle),
    )


@router.get("", response_model=SdiEnvironmentOut)
async def get_sdi_environment(
    _admin: Annotated[User, Depends(require_admin)],
) -> SdiEnvironmentOut:
    async with admin_session() as s:
        env = await svc.get_sdi_environment(s)
        code = await svc.get_sdi_intermediary_id_codice(s)
    return _out(env, code)


@router.put("", response_model=SdiEnvironmentOut)
async def set_sdi_environment(
    body: SdiEnvironmentIn,
    _admin: Annotated[User, Depends(require_admin)],
) -> SdiEnvironmentOut:
    async with admin_session() as s:
        row = await svc.set_sdi_environment(s, body.environment)
        env, code = row.sdi_environment, row.sdi_intermediary_id_codice
    return _out(env, code)


@router.put("/intermediary", response_model=SdiEnvironmentOut)
async def set_sdi_intermediary(
    body: SdiIntermediaryIn,
    _admin: Annotated[User, Depends(require_admin)],
) -> SdiEnvironmentOut:
    """Set the accredited channel's fiscal code, without a redeploy.

    It used to exist only as ``MYCELIUM_SDI_INTERMEDIARY_ID_CODICE``, so
    correcting it meant editing a ConfigMap and rolling the deployment -- for a
    value FatturaPA is specific about (1.1.1.2 wants the trasmittente's CODICE
    FISCALE, and SdI verifies it in Anagrafe Tributaria as such) and that a
    deployment can therefore hold wrongly for a long time without knowing.
    """
    async with admin_session() as s:
        row = await svc.set_sdi_intermediary_id_codice(s, body.id_codice)
        env, code = row.sdi_environment, row.sdi_intermediary_id_codice
    return _out(env, code)
