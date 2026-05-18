"""Router auth: signup e login. Adapter sottile su flow_core."""

from __future__ import annotations

from fastapi import APIRouter

from flow_api.schemas import LoginIn, SignupIn, SignupOut, TokenOut
from flow_core.db import admin_session
from flow_core.services.auth import login, signup

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=SignupOut)
async def signup_endpoint(body: SignupIn) -> SignupOut:
    # Personal-first: never force "create an organization". A personal
    # workspace is auto-provisioned; naming it is optional.
    workspace_name = body.workspace_name or body.display_name or "Personal"
    async with admin_session() as session:
        result = await signup(
            session,
            email=body.email,
            password=body.password,
            org_name=workspace_name,
        )
    return SignupOut(
        user_id=result.user_id,
        workspace_id=result.org_id,
        token=result.token,
    )


@router.post("/login", response_model=TokenOut)
async def login_endpoint(body: LoginIn) -> TokenOut:
    async with admin_session() as session:
        token = await login(session, email=body.email, password=body.password)
    return TokenOut(token=token)
