"""Regression: the "act as" lever (sudo de-escalation) must bind at
the service RBAC choke point, not only in the API boundary.

Before the fix, ``rbac.require_role`` re-derived the role from stored
membership, ignoring the sudo-clamped effective role published by
``tenant_ctx``. A workspace owner who dropped to "user" in the SPA
could still perform owner/admin operations (e.g. self-grant billing
credits) because their stored membership was owner. The effective
role is now published as the ``app.current_role`` GUC and enforced by
``require_role``; it is clamped DOWN to entitlement so it can only
de-escalate, never escalate.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> tuple[str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    # The signup user is OWNER of the freshly created workspace, and is
    # NOT a platform admin.
    return a["token"], a["workspace_id"]


async def test_owner_acting_as_user_cannot_grant_credits() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        token, ws = await _signup(c)
        base = {"Authorization": f"Bearer {token}", "X-Workspace-Id": ws}

        # Acting as "user": no X-Workspace-Role header -> effective
        # role is clamped to member (least privilege). grant_credits
        # requires admin -> must be forbidden (was 200 via the bug).
        r = await c.post("/billing/grant", headers=base, json={"amount": "100.00"})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "rbac.role_insufficient"

        # Explicitly requesting member is the same.
        r = await c.post(
            "/billing/grant",
            headers={**base, "X-Workspace-Role": "member"},
            json={"amount": "100.00"},
        )
        assert r.status_code == 403, r.text

        # Reading the balance is not privileged.
        r = await c.get("/billing/balance", headers=base)
        assert r.status_code == 200, r.text
        assert Decimal(r.json()["balance"]) == Decimal(0)


async def test_owner_acting_as_owner_can_grant_credits() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        token, ws = await _signup(c)
        base = {"Authorization": f"Bearer {token}", "X-Workspace-Id": ws}

        # Same account, now explicitly acting as owner (entitled: the
        # signup user's membership IS owner) -> the clamp lets it
        # through and the privileged op succeeds.
        r = await c.post(
            "/billing/grant",
            headers={**base, "X-Workspace-Role": "owner"},
            json={"amount": "250.00", "reason": "topup"},
        )
        assert r.status_code == 200, r.text
        assert Decimal(r.json()["balance"]) == Decimal(250)
