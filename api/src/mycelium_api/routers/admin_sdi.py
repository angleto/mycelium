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
from mycelium_api.schemas import SdiEnvironmentIn, SdiEnvironmentOut
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.models.user import User
from mycelium_core.services import system_settings as svc

router = APIRouter(prefix="/admin/sdi-environment", tags=["admin"])


def _out(environment: str) -> SdiEnvironmentOut:
    s = get_settings()
    return SdiEnvironmentOut(
        environment=environment,
        sdicoop_active=s.sdicoop_active,
        test_url=s.sdi_endpoint_url_test or s.sdi_endpoint_url,
        prod_url=s.sdi_endpoint_url_prod or s.sdi_endpoint_url,
        active_endpoint=svc.endpoint_for(environment),
        intermediary_id_paese=s.sdi_intermediary_id_paese,
        intermediary_id_codice=s.sdi_intermediary_id_codice,
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
    return _out(env)


@router.put("", response_model=SdiEnvironmentOut)
async def set_sdi_environment(
    body: SdiEnvironmentIn,
    _admin: Annotated[User, Depends(require_admin)],
) -> SdiEnvironmentOut:
    async with admin_session() as s:
        row = await svc.set_sdi_environment(s, body.environment)
        env = row.sdi_environment
    return _out(env)
