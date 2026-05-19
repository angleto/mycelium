"""Per-workspace settings: configurable task-estimate presets.
Tenant-scoped GET/PATCH with optimistic concurrency + validation.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_estimate_presets_default_update_validation() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        # Owner with full entitlement: PATCH /workspaces/me/settings
        # needs the effective role admin, which is X-Workspace-Role
        # clamped to the membership (absent header => member).
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        me = (await c.get("/workspaces/me", headers=h)).json()
        # Defaults when nothing was configured yet.
        assert me["settings"]["estimate_presets"] == ["0.5", "1", "4", "8"]
        ver = me["version"]

        # Update; the validator sorts + dedups.
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={"expected_version": ver, "estimate_presets": [2, 0.25, 2, 1]},
        )
        assert r.status_code == 200
        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["estimate_presets"] == ["0.25", "1", "2"]

        # Stale version -> 409.
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={"expected_version": ver, "estimate_presets": [1]},
        )
        assert r.status_code == 409

        # Non-positive preset -> 422 (schema validator).
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={"expected_version": me["version"], "estimate_presets": [0]},
        )
        assert r.status_code == 422
