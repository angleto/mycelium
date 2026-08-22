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


async def test_workspace_me_reports_its_own_archived_state() -> None:
    """The SPA's switcher badges the workspace you are STANDING IN as
    archived. Before ``status`` was on ``WorkspaceOut`` that fact was
    only obtainable by fetching the whole roster."""
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
        hdr = {**_bearer(ta), "X-Workspace-Id": second["id"]}

        assert (await c.get("/workspaces/me", headers=hdr)).json()["status"] == "active"

        assert (
            await c.post(f"/workspaces/{second['id']}/archive", headers=_bearer(ta))
        ).status_code == 204
        me = (await c.get("/workspaces/me", headers=hdr)).json()
        assert me["status"] == "archived"

        assert (
            await c.post(f"/workspaces/{second['id']}/unarchive", headers=_bearer(ta))
        ).status_code == 204
        assert (await c.get("/workspaces/me", headers=hdr)).json()["status"] == "active"


async def test_member_cannot_archive_or_delete_and_is_told_why() -> None:
    """A plain member is refused both lifecycle operations, with a
    RENDERED message. The archive path used to raise the templated
    ``rbac.role_insufficient`` with no params, so the user was shown the
    literal ``Role {current} is insufficient, requires >= {minimum}``."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        to = owner["token"]
        shared = (await c.post("/workspaces", headers=_bearer(to), json={"name": "Shared"})).json()
        sid = shared["id"]

        guest_email = _email()
        guest = (
            await c.post(
                "/auth/signup",
                json={"email": guest_email, "password": "pw-strong-123"},
            )
        ).json()
        tg = guest["token"]

        # The owner adds them as a plain member (owner-gated, so the
        # request is made with the elevated workspace role).
        added = await c.post(
            "/workspaces/me/members",
            headers={**_bearer(to), "X-Workspace-Id": sid, "X-Workspace-Role": "owner"},
            json={"email": guest_email, "role": "member"},
        )
        assert added.status_code == 200

        r = await c.post(f"/workspaces/{sid}/archive", headers=_bearer(tg))
        assert r.status_code == 403
        body = r.json()
        assert body["code"] == "workspace.not_owner"
        # The whole point: a rendered sentence, not an unfilled template.
        assert "{" not in body["detail"]

        r = await c.delete(f"/workspaces/{sid}", headers=_bearer(tg))
        assert r.status_code == 403
        assert r.json()["code"] == "workspace.not_owner"

        # And the workspace is untouched by either attempt.
        ws = {w["id"]: w for w in (await c.get("/workspaces", headers=_bearer(to))).json()}
        assert ws[sid]["status"] == "active"
