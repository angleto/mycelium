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


async def _admin(c: AsyncClient) -> tuple[str, str]:
    """Bootstrap an admin and log in. Returns ``(token, email)``."""
    email = _email()
    await ensure_admin(email, _ADMIN_PW)
    token = (await c.post("/auth/login", json={"email": email, "password": _ADMIN_PW})).json()[
        "token"
    ]
    return token, email


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

        # Admin WITH elevation: full access. Reach the two users by SEARCH,
        # not by scanning the default page -- the shared test database holds
        # a user for every signup every other test ever made, so any
        # position-based assertion here rots the moment the suite grows.
        for email in (normal_email, admin_email):
            found = await c.get(
                "/admin/users", params={"q": email}, headers={**_bearer(at), **_ELEVATE}
            )
            assert found.status_code == 200
            assert [u["email"] for u in found.json()] == [email]

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


async def test_user_list_is_bounded_and_pageable() -> None:
    """The list used to select EVERY user row into one response. On a
    workspace with tens of thousands of accounts that is a slow query, a
    huge payload and a DOM the browser cannot render -- the e2e suite hit
    exactly that. The page is capped, the bounds are validated rather
    than silently clamped, and paging is total: no row is dropped or
    repeated across pages."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        at, _admin_email = await _admin(c)
        h = {**_bearer(at), **_ELEVATE}

        # The default page is capped, whatever the table holds.
        first = await c.get("/admin/users", headers=h)
        assert first.status_code == 200
        assert len(first.json()) <= 50

        # Out-of-range bounds are a 422, not a silent clamp: a caller that
        # asks for limit=100000 must learn it cannot have it.
        for bad in ({"limit": 0}, {"limit": 201}, {"limit": -1}, {"offset": -1}):
            r = await c.get("/admin/users", params=bad, headers=h)
            assert r.status_code == 422, bad

        # Paging is total. ``created_at`` alone is NOT unique here (the
        # suite signs users up in a tight loop), so this is the assertion
        # that catches an ordering without the id tiebreak: it would drop
        # and repeat rows across the boundary.
        p0 = await c.get("/admin/users", params={"limit": 5, "offset": 0}, headers=h)
        p1 = await c.get("/admin/users", params={"limit": 5, "offset": 5}, headers=h)
        ids0 = [u["id"] for u in p0.json()]
        ids1 = [u["id"] for u in p1.json()]
        assert len(ids0) == 5
        assert len(set(ids0)) == 5
        assert not set(ids0) & set(ids1)


async def test_user_search_matches_email_and_display_name() -> None:
    """Search is what makes the page usable once paging bounds it: an
    admin looking for one person cannot walk a thousand pages."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        at, admin_email = await _admin(c)
        h = {**_bearer(at), **_ELEVATE}

        # Case-insensitive substring of the email.
        local = admin_email.split("@")[0]
        hit = await c.get("/admin/users", params={"q": local.upper()}, headers=h)
        assert hit.status_code == 200
        assert admin_email in {u["email"] for u in hit.json()}

        # A term that matches nothing is an empty list, not everything.
        miss = await c.get("/admin/users", params={"q": f"nope-{uuid.uuid4().hex}"}, headers=h)
        assert miss.status_code == 200
        assert miss.json() == []
