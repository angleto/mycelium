"""One-shot, idempotent admin bootstrap for production.

Creates the admin account (``MYCELIUM_ADMIN_EMAIL`` / ``MYCELIUM_ADMIN_PASSWORD``
from the deploy secret) via the normal signup path, so they own a
personal workspace (owner ⊇ admin). Idempotent: if the user already
exists it is left untouched (rotate via the password-reset flow, never
silently here). Fail-closed: a missing or weak password aborts — there
is deliberately no default.

Run: ``python -m mycelium_core.bootstrap_admin`` (uses the backend image in
the deploy; same DB URL as the app).
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import select, text

from mycelium_core.db import admin_session
from mycelium_core.models.user import User
from mycelium_core.services.auth import signup

_MIN_LEN = 12


def _check_password(pw: str) -> None:
    """Refuse weak admin passwords (deploy-time guard, stricter than
    the API's 8-char floor)."""
    problems: list[str] = []
    if len(pw) < _MIN_LEN:
        problems.append(f"at least {_MIN_LEN} characters")
    if pw.lower() == pw or pw.upper() == pw:
        problems.append("mixed upper/lower case")
    if not any(c.isdigit() for c in pw):
        problems.append("at least one digit")
    if len(set(pw)) < 5:
        problems.append("more variety")
    if problems:
        raise SystemExit("MYCELIUM_ADMIN_PASSWORD is too weak; needs: " + ", ".join(problems))


async def ensure_admin(email: str, password: str) -> str:
    email = email.strip().lower()
    async with admin_session() as s:
        existing = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is not None:
            # Idempotent: never reset the password here (rotate via the
            # reset flow), but make sure the account actually carries the
            # admin flag (the whole point of this bootstrap).
            promoted = False
            if not existing.is_admin:
                existing.is_admin = True
                promoted = True
            # Self-heal: a partial signup (e.g. an older migration where
            # provision_organization could not bypass RLS) may have left
            # the user without any membership. Detect that case and
            # provision the default "Personal" org now; without this the
            # SPA's post-login GET /workspaces returns [] and the user
            # cannot enter the app. We count via list_user_organizations
            # (SECURITY DEFINER) because admin_session() has no tenant
            # GUC and a direct memberships SELECT would be filtered out
            # by RLS to 0 -- which would defeat the idempotency check.
            ocount = (
                await s.execute(
                    text("SELECT count(*) FROM list_user_organizations(CAST(:u AS uuid))"),
                    {"u": str(existing.id)},
                )
            ).scalar_one()
            if ocount == 0:
                await s.execute(
                    text("SELECT provision_organization(:n, CAST(:u AS uuid))"),
                    {"n": "Personal", "u": str(existing.id)},
                )
                if promoted:
                    return (
                        f"admin {email} already existed; promoted to admin "
                        f"and provisioned missing Personal workspace"
                    )
                return f"admin {email} already existed; provisioned missing Personal workspace"
            if promoted:
                return f"admin {email} already existed; promoted to admin"
            return f"admin {email} already exists; left untouched"
        result = await signup(s, email=email, password=password, org_name="Personal")
        created = (await s.execute(select(User).where(User.id == result.user_id))).scalar_one()
        created.is_admin = True
    return f"admin {email} created (admin, owner of a personal workspace)"


def main() -> None:
    email = os.environ.get("MYCELIUM_ADMIN_EMAIL", "").strip()
    password = os.environ.get("MYCELIUM_ADMIN_PASSWORD", "")
    if not email or not password:
        raise SystemExit("MYCELIUM_ADMIN_EMAIL and MYCELIUM_ADMIN_PASSWORD must both be set")
    _check_password(password)
    msg = asyncio.run(ensure_admin(email, password))
    sys.stdout.write(msg + "\n")


if __name__ == "__main__":
    main()
