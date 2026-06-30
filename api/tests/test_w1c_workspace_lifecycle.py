"""W1c (DB-backed): workspace lifecycle - archive/unarchive and the
guarded hard-delete that cascades all tenant data (ON DELETE CASCADE,
migration 0019). Pre-tenant operations: the switcher calls them with
no org context.
"""

from __future__ import annotations

import base64
import json
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mycelium_api.main import app
from mycelium_core.db import tenant_session


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sub(token: str) -> str:
    """User id from the JWT payload (no verification: test helper)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return str(json.loads(base64.urlsafe_b64decode(payload))["sub"])


async def test_archive_unarchive_workspace() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        ta = a["token"]
        second = (
            await c.post("/workspaces", headers=_bearer(ta), json={"name": "Client X"})
        ).json()

        # New workspaces start active.
        ws = (await c.get("/workspaces", headers=_bearer(ta))).json()
        assert {w["status"] for w in ws} == {"active"}

        r = await c.post(f"/workspaces/{second['id']}/archive", headers=_bearer(ta))
        assert r.status_code == 204
        ws = {w["id"]: w for w in (await c.get("/workspaces", headers=_bearer(ta))).json()}
        assert ws[second["id"]]["status"] == "archived"
        assert ws[a["workspace_id"]]["status"] == "active"

        # Archived workspace stays fully usable (status does not gate RLS).
        me = await c.get(
            "/workspaces/me",
            headers={**_bearer(ta), "X-Workspace-Id": second["id"]},
        )
        assert me.status_code == 200

        r = await c.post(f"/workspaces/{second['id']}/unarchive", headers=_bearer(ta))
        assert r.status_code == 204
        ws = {w["id"]: w for w in (await c.get("/workspaces", headers=_bearer(ta))).json()}
        assert ws[second["id"]]["status"] == "active"


async def test_delete_workspace_cascades_and_safeguards() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        ta = a["token"]
        personal = a["workspace_id"]

        # Cannot delete the sole workspace.
        r = await c.delete(f"/workspaces/{personal}", headers=_bearer(ta))
        assert r.status_code == 400

        second = (
            await c.post("/workspaces", headers=_bearer(ta), json={"name": "Client X"})
        ).json()
        sid = second["id"]
        hdr = {**_bearer(ta), "X-Workspace-Id": sid}

        # Tenant data in the second workspace, to prove the cascade.
        task = await c.post("/tasks", headers=hdr, json={"title": "doomed"})
        assert task.status_code == 200
        assert len((await c.get("/tasks", headers=hdr)).json()) == 1

        # A non-member cannot delete it (scoped out -> not found).
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        r = await c.delete(f"/workspaces/{sid}", headers=_bearer(b["token"]))
        assert r.status_code == 404

        # Owner deletes it; ON DELETE CASCADE removes the tenant rows.
        r = await c.delete(f"/workspaces/{sid}", headers=_bearer(ta))
        assert r.status_code == 204
        ws = (await c.get("/workspaces", headers=_bearer(ta))).json()
        assert [w["id"] for w in ws] == [personal]

        # The cascade removed its tenant rows (RLS-scoped count = 0) and
        # the membership is gone, so the now-defunct header is rejected
        # (403: no membership in the current context).
        async with tenant_session(sid, _sub(ta)) as s:
            n = await s.scalar(
                text("SELECT count(*) FROM tasks WHERE org_id = :o"),
                {"o": sid},
            )
        assert n == 0
        me = await c.get("/workspaces/me", headers=hdr)
        assert me.status_code == 403
