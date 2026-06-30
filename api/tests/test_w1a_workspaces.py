"""W1a (DB-backed): personal-first signup (no workspace name) and the
pre-tenant workspace listing/creation that powers the in-app switcher,
so a user with several workspaces never logs out to switch.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_personal_signup_and_in_app_workspace_switch() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # Personal-first: no workspace name at signup.
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        assert a["workspace_id"] and a["token"]
        ta = a["token"]

        # Exactly one (personal) workspace, as owner.
        ws = (await c.get("/workspaces", headers=_bearer(ta))).json()
        assert len(ws) == 1
        assert ws[0]["id"] == a["workspace_id"]
        assert ws[0]["role"] == "owner"
        assert ws[0]["name"] == "Personal"

        # Create a second workspace in-app (no re-auth).
        second = (
            await c.post(
                "/workspaces",
                headers=_bearer(ta),
                json={"name": "Client X"},
            )
        ).json()
        assert second["name"] == "Client X"
        assert second["version"] == 1

        ws2 = (await c.get("/workspaces", headers=_bearer(ta))).json()
        assert {w["name"] for w in ws2} == {"Personal", "Client X"}

        # Same session, switch context purely via the header.
        me = await c.get(
            "/workspaces/me",
            headers={**_bearer(ta), "X-Workspace-Id": second["id"]},
        )
        assert me.status_code == 200
        assert me.json()["name"] == "Client X"

        # Isolation: another user does not see this user's workspaces.
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        wsb = (await c.get("/workspaces", headers=_bearer(b["token"]))).json()
        assert len(wsb) == 1
        assert wsb[0]["id"] != a["workspace_id"]
