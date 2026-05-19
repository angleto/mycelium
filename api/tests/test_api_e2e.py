"""F0 API end-to-end (DB-backed): isolation, optimistic concurrency,
i18n error codes. Requires a migrated database.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_api_flow() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}

        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "B"},
            )
        ).json()
        # Owner with full entitlement: PATCH /workspaces/me needs the
        # effective role admin, which is X-Workspace-Role clamped to
        # the membership (absent header => member, least privilege).
        headers = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        own = await c.get("/workspaces/me", headers=headers)
        assert own.status_code == 200 and own.json()["name"] == "A"

        cross = await c.get(
            "/workspaces/me",
            headers={"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": b["workspace_id"]},
        )
        assert cross.status_code == 403
        assert cross.json()["code"] == "rbac.no_membership"

        ok = await c.patch(
            "/workspaces/me", headers=headers, json={"name": "A2", "expected_version": 1}
        )
        assert ok.status_code == 200 and ok.json()["version"] == 2

        stale = await c.patch(
            "/workspaces/me", headers=headers, json={"name": "X", "expected_version": 1}
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "concurrency.stale_version"

        noauth = await c.get("/workspaces/me", headers={"X-Workspace-Id": a["workspace_id"]})
        assert noauth.status_code == 401
        assert noauth.json()["code"] == "auth.missing_bearer"
