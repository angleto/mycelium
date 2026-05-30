"""Workspace membership + effective-role RBAC (DB-backed).

Two concerns:

- membership management: an owner adds/lists/promotes collaborators by
  email; a plain member cannot manage members; the sole owner cannot be
  removed or demoted; an unknown email is rejected.
- effective-role clamp (the core change): the role a request runs with
  is ``X-Workspace-Role`` clamped DOWN to the caller's entitlement.
  Without the header the effective role is ``member`` (least
  privilege), so a privileged write 403s even for the owner; a member
  forging ``X-Workspace-Role: owner`` is still clamped to member; a
  global admin with no membership may act as owner only in admin-mode.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.bootstrap_admin import ensure_admin


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_ADMIN_PW = "Str0ng-Passw0rd!"


async def _signup(c: AsyncClient, email: str | None = None) -> dict[str, str]:
    return (
        await c.post(
            "/auth/signup",
            json={"email": email or _email(), "password": "pw-strong-123"},
        )
    ).json()


async def test_workspace_membership_management() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner_email = _email()
        owner = await _signup(c, owner_email)
        ws = owner["workspace_id"]
        # Owner acting with full entitlement.
        oh = {
            **_bearer(owner["token"]),
            "X-Workspace-Id": ws,
            "X-Workspace-Role": "owner",
        }

        # A second existing user, to be added to the owner's workspace.
        guest_email = _email()
        guest = await _signup(c, guest_email)
        guest_me = (await c.get("/auth/me", headers=_bearer(guest["token"]))).json()
        guest_id = guest_me["user_id"]

        # /workspaces/me exposes the caller's raw membership role.
        me_ws = (await c.get("/workspaces/me", headers=oh)).json()
        assert me_ws["my_role"] == "owner"

        # Owner adds the existing user by email as a member.
        added = await c.post(
            "/workspaces/me/members",
            headers=oh,
            json={"email": guest_email, "role": "member"},
        )
        assert added.status_code == 200
        roster = {m["email"]: m["role"] for m in added.json()}
        assert roster == {owner_email: "owner", guest_email: "member"}

        # Roster is readable by any member (no role header needed).
        listed = await c.get(
            "/workspaces/me/members",
            headers={**_bearer(guest["token"]), "X-Workspace-Id": ws},
        )
        assert listed.status_code == 200
        assert {m["email"] for m in listed.json()} == {owner_email, guest_email}

        # Promote the member to admin.
        promoted = await c.patch(
            f"/workspaces/me/members/{guest_id}",
            headers=oh,
            json={"role": "admin"},
        )
        assert promoted.status_code == 200
        assert {m["email"]: m["role"] for m in promoted.json()}[guest_email] == "admin"

        # Adding an unknown email => member.not_found.
        nf = await c.post(
            "/workspaces/me/members",
            headers=oh,
            json={"email": _email(), "role": "member"},
        )
        assert nf.status_code == 404
        assert nf.json()["code"] == "member.not_found"

        # The sole owner cannot be demoted nor removed.
        owner_me = (await c.get("/auth/me", headers=_bearer(owner["token"]))).json()
        owner_id = owner_me["user_id"]
        demote = await c.patch(
            f"/workspaces/me/members/{owner_id}",
            headers=oh,
            json={"role": "admin"},
        )
        assert demote.status_code == 400
        assert demote.json()["code"] == "member.last_owner"
        rm = await c.delete(f"/workspaces/me/members/{owner_id}", headers=oh)
        assert rm.status_code == 400
        assert rm.json()["code"] == "member.last_owner"

        # A plain member cannot manage members. The admin we created is
        # demoted back to member first.
        await c.patch(
            f"/workspaces/me/members/{guest_id}",
            headers=oh,
            json={"role": "member"},
        )
        mh = {
            **_bearer(guest["token"]),
            "X-Workspace-Id": ws,
            "X-Workspace-Role": "owner",  # forged: clamped to member
        }
        assert (
            await c.post(
                "/workspaces/me/members",
                headers=mh,
                json={"email": owner_email, "role": "member"},
            )
        ).status_code == 403
        assert (
            await c.patch(
                f"/workspaces/me/members/{owner_id}",
                headers=mh,
                json={"role": "member"},
            )
        ).status_code == 403
        assert (await c.delete(f"/workspaces/me/members/{owner_id}", headers=mh)).status_code == 403


async def test_effective_role_clamp() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup(c)
        ws = owner["workspace_id"]
        base = {**_bearer(owner["token"]), "X-Workspace-Id": ws}
        as_owner = {**base, "X-Workspace-Role": "owner"}

        client_body = {"name": "Acme", "legal_name": "Acme SRL"}

        # OWNER without X-Workspace-Role => effective member => a
        # client/workflow/issuer WRITE is 403.
        assert (await c.post("/clients", headers=base, json=client_body)).status_code == 403
        assert (
            await c.post(
                "/workflows",
                headers=base,
                json={
                    "name": "WF",
                    "states": [{"name": "open", "ord": 1, "is_initial": True}],
                    "transitions": [],
                },
            )
        ).status_code == 403
        assert (
            await c.post(
                "/issuer-profiles",
                headers=base,
                json={"label": "Me", "legal_name": "Me SRL"},
            )
        ).status_code == 403

        # Same owner WITH X-Workspace-Role: owner => 200.
        ok = await c.post("/clients", headers=as_owner, json=client_body)
        assert ok.status_code == 200 and ok.json()["id"]

        # Reads still work for a plain member (no role header): add a
        # second user as member and let them list.
        member_email = _email()
        member = await _signup(c, member_email)
        await c.post(
            "/workspaces/me/members",
            headers=as_owner,
            json={"email": member_email, "role": "member"},
        )
        mh = {**_bearer(member["token"]), "X-Workspace-Id": ws}
        assert (await c.get("/clients", headers=mh)).status_code == 200
        assert (await c.get("/workflows", headers=mh)).status_code == 200

        # A member forging X-Workspace-Role: owner is still clamped to
        # member => write 403 (no escalation).
        assert (
            await c.post(
                "/clients",
                headers={**mh, "X-Workspace-Role": "owner"},
                json=client_body,
            )
        ).status_code == 403

        # A global admin with NO membership in this workspace. The
        # effective-role layer (tenant_ctx, the core change) treats the
        # admin-mode elevation as a ceiling of owner on ANY workspace:
        # the request is admitted and the effective/raw role resolves to
        # owner. Without X-Admin-Mode there is neither a membership nor
        # an elevation, so tenant_ctx rejects with rbac.no_membership
        # (behaviour preserved for non-members). (Deeper service-layer
        # writes like create_client still independently require a real
        # membership row, an existing choke point out of this change's
        # scope; the ceiling is asserted here at the boundary it owns.)
        admin_email = _email()
        await ensure_admin(admin_email, _ADMIN_PW)
        at = (
            await c.post(
                "/auth/login",
                json={"email": admin_email, "password": _ADMIN_PW},
            )
        ).json()["token"]
        no_mode = await c.get(
            "/workspaces/me",
            headers={**_bearer(at), "X-Workspace-Id": ws},
        )
        assert no_mode.status_code == 403
        assert no_mode.json()["code"] == "rbac.no_membership"
        with_mode = await c.get(
            "/workspaces/me",
            headers={**_bearer(at), "X-Workspace-Id": ws, "X-Admin-Mode": "1"},
        )
        assert with_mode.status_code == 200
        # my_role is the entitlement ceiling: owner for a global admin
        # acting via sudo without a membership.
        assert with_mode.json()["my_role"] == "owner"
