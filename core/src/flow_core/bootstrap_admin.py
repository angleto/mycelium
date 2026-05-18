"""One-shot, idempotent admin bootstrap for production.

Creates the admin account (``FLOW_ADMIN_EMAIL`` / ``FLOW_ADMIN_PASSWORD``
from the deploy secret) via the normal signup path, so they own a
personal workspace (owner ⊇ admin). Idempotent: if the user already
exists it is left untouched (rotate via the password-reset flow, never
silently here). Fail-closed: a missing or weak password aborts — there
is deliberately no default.

Run: ``python -m flow_core.bootstrap_admin`` (uses the backend image in
the deploy; same DB URL as the app).
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import select

from flow_core.db import admin_session
from flow_core.models.user import User
from flow_core.services.auth import signup

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
        raise SystemExit(
            "FLOW_ADMIN_PASSWORD is too weak; needs: " + ", ".join(problems)
        )


async def ensure_admin(email: str, password: str) -> str:
    email = email.strip().lower()
    async with admin_session() as s:
        existing = (
            await s.execute(select(User.id).where(User.email == email))
        ).scalar_one_or_none()
        if existing is not None:
            return f"admin {email} already exists; left untouched"
        await signup(s, email=email, password=password, org_name="Personal")
    return f"admin {email} created (owner of a personal workspace)"


def main() -> None:
    email = os.environ.get("FLOW_ADMIN_EMAIL", "").strip()
    password = os.environ.get("FLOW_ADMIN_PASSWORD", "")
    if not email or not password:
        raise SystemExit(
            "FLOW_ADMIN_EMAIL and FLOW_ADMIN_PASSWORD must both be set"
        )
    _check_password(password)
    msg = asyncio.run(ensure_admin(email, password))
    sys.stdout.write(msg + "\n")


if __name__ == "__main__":
    main()
