"""MFA router: TOTP enrolment, status, disable. QR rendering is an
adapter concern and lives here, not in core (ADR-0024). Co-equal with
the rest of the API; the user is resolved from the JWT (no tenant)."""

from __future__ import annotations

import base64
import io
import uuid
from typing import Annotated

import qrcode
from fastapi import APIRouter, Depends, Response, status

from flow_api.deps import current_user_id
from flow_api.schemas import (
    MfaActivateIn,
    MfaActivateOut,
    MfaDisableIn,
    MfaSetupOut,
    MfaStatusOut,
)
from flow_core.db import admin_session
from flow_core.services import mfa as mfa_svc
from flow_core.services.auth import get_user

router = APIRouter(prefix="/mfa", tags=["mfa"])


def _qr_png_base64(data: str) -> str:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@router.get("/status", response_model=MfaStatusOut)
async def mfa_status(
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> MfaStatusOut:
    async with admin_session() as s:
        st = mfa_svc.status(await get_user(s, user_id=user_id))
    return MfaStatusOut(
        enabled=st.enabled,
        pending=st.pending,
        enabled_at=st.enabled_at,
        backup_codes_remaining=st.backup_codes_remaining,
    )


@router.post("/setup", response_model=MfaSetupOut)
async def mfa_setup(
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> MfaSetupOut:
    async with admin_session() as s:
        out = mfa_svc.setup(user=await get_user(s, user_id=user_id))
    return MfaSetupOut(
        provisioning_uri=out.provisioning_uri,
        qr_png_base64=_qr_png_base64(out.provisioning_uri),
        secret=out.secret,
    )


@router.post("/activate", response_model=MfaActivateOut)
async def mfa_activate(
    body: MfaActivateIn,
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> MfaActivateOut:
    async with admin_session() as s:
        user = await get_user(s, user_id=user_id)
        res = mfa_svc.activate(user=user, totp_code=body.totp_code)
    return MfaActivateOut(backup_codes=res.backup_codes, enabled_at=res.enabled_at)


@router.post("/disable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_disable(
    body: MfaDisableIn,
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> Response:
    async with admin_session() as s:
        user = await get_user(s, user_id=user_id)
        mfa_svc.disable(user=user, code=body.code)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
