"""Admin surface: /auth/me identity, sudo-style elevation gate
(capability AND X-Admin-Mode), user administration + self-guard.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.bootstrap_admin import ensure_admin


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_ADMIN_PW = "Str0ng-Passw0rd!"
_ELEVATE = {"X-Admin-Mode": "1"}


async def test_admin_elevation_and_user_administration() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # A normal user (signup) and an admin user (bootstrap).
        normal_email = _email()
        normal = (
            await c.post(
                "/auth/signup",
                json={"email": normal_email, "password": "pw-strong-123"},
            )
        ).json()
        nt = normal["token"]

        admin_email = _email()
        await ensure_admin(admin_email, _ADMIN_PW)
        at = (
            await c.post(
                "/auth/login",
                json={"email": admin_email, "password": _ADMIN_PW},
            )
        ).json()["token"]

        # /auth/me reflects the capability (not the mode).
        me_n = (await c.get("/auth/me", headers=_bearer(nt))).json()
        assert me_n["email"] == normal_email
        assert me_n["is_admin"] is False
        me_a = (await c.get("/auth/me", headers=_bearer(at))).json()
        assert me_a["is_admin"] is True
        admin_id = me_a["user_id"]
        normal_id = me_n["user_id"]

        # Normal user can never reach the admin surface, even with a
        # forged elevation header (no capability => header ignored).
        assert (await c.get("/admin/users", headers=_bearer(nt))).status_code == 403
        assert (await c.get("/admin/users", headers={**_bearer(nt), **_ELEVATE})).status_code == 403

        # Admin WITHOUT elevation is treated like a normal user.
        assert (await c.get("/admin/users", headers=_bearer(at))).status_code == 403

        # Admin WITH elevation: full access.
        listed = await c.get("/admin/users", headers={**_bearer(at), **_ELEVATE})
        assert listed.status_code == 200
        emails = {u["email"] for u in listed.json()}
        assert {normal_email, admin_email} <= emails

        # Promote the normal user.
        patched = await c.patch(
            f"/admin/users/{normal_id}",
            headers={**_bearer(at), **_ELEVATE},
            json={"is_admin": True},
        )
        assert patched.status_code == 200
        assert patched.json()["is_admin"] is True

        # Self-guard: an admin cannot strip their own admin role nor
        # deactivate themselves.
        assert (
            await c.patch(
                f"/admin/users/{admin_id}",
                headers={**_bearer(at), **_ELEVATE},
                json={"is_admin": False},
            )
        ).status_code == 400
        assert (
            await c.patch(
                f"/admin/users/{admin_id}",
                headers={**_bearer(at), **_ELEVATE},
                json={"is_active": False},
            )
        ).status_code == 400

        # Unknown user => 404.
        assert (
            await c.patch(
                f"/admin/users/{uuid.uuid4()}",
                headers={**_bearer(at), **_ELEVATE},
                json={"is_admin": True},
            )
        ).status_code == 404

        # Deactivating a user takes effect on their next request.
        await c.patch(
            f"/admin/users/{normal_id}",
            headers={**_bearer(at), **_ELEVATE},
            json={"is_active": False},
        )
        assert (await c.get("/auth/me", headers=_bearer(nt))).status_code == 401
