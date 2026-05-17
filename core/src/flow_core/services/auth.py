"""Signup e login.

signup gira senza contesto tenant (admin_session): `users` non e
org-scoped e la creazione org+membership passa dalla funzione
SECURITY DEFINER `provision_organization` (docs/adr/0015), unico punto
che crea un tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import AuthError
from flow_core.models.user import User
from flow_core.security import create_access_token, hash_password, verify_password


@dataclass(frozen=True, slots=True)
class SignupResult:
    user_id: uuid.UUID
    org_id: uuid.UUID
    token: str


async def signup(
    session: AsyncSession, *, email: str, password: str, org_name: str
) -> SignupResult:
    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    await session.flush()  # popola user.id
    result = await session.execute(
        text("SELECT provision_organization(:n, :u)"),
        {"n": org_name, "u": str(user.id)},
    )
    org_id = uuid.UUID(str(result.scalar_one()))
    return SignupResult(
        user_id=user.id,
        org_id=org_id,
        token=create_access_token(user_id=str(user.id)),
    )


async def login(session: AsyncSession, *, email: str, password: str) -> str:
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or not verify_password(password, user.password_hash)
    ):
        raise AuthError("credenziali non valide")
    return create_access_token(user_id=str(user.id))
