"""Hardened workspace role model (authoritative semantics).

Three properties, all DB-backed through the real ASGI app:

1. **Owner protection.** Only an *owner* manages members. A user added
   to a namespace later (role ``member``) can never list-then-mutate
   the roster: not add a member, not re-role the owner, not remove the
   owner, not even remove itself via the members endpoint, even when it
   forges ``X-Workspace-Role: owner`` (the effective role is clamped
   AND the owner gate rejects it). The sole owner cannot demote or
   remove itself. Once promoted to owner, the new owner may remove the
   original owner, but the *last* remaining owner is irremovable.

2. **Privileged writes = owner.** Modifying clients, workflows or
   issuer profiles requires the effective role *owner*. A plain member
   is 403; the owner WITHOUT ``X-Workspace-Role`` (effective member by
   least privilege) is also 403; the owner WITH
   ``X-Workspace-Role: owner`` is 200. Reads stay open to any member.

3. **Namespace isolation (zero cross-workspace leakage).** A user with
   no membership in a workspace cannot read or write anything in it,
   cannot see its data from their own workspace, and cannot add itself
   to it; the workspace switcher lists only the workspaces they belong
   to. Forging ``X-Workspace-Role: owner`` does not help (no membership
   ⇒ rejected before the role is even consulted).

Mirrors the httpx ASGITransport style of ``test_workspace_members``.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(c: AsyncClient, email: str | None = None) -> dict[str, str]:
    """Sign up a fresh user. The caller becomes the owner of an
    auto-provisioned personal workspace (its id is ``workspace_id``)."""
    return (
        await c.post(
            "/auth/signup",
            json={"email": email or _email(), "password": "pw-strong-123"},
        )
    ).json()


async def _user_id(c: AsyncClient, token: str) -> str:
    return (await c.get("/auth/me", headers=_bearer(token))).json()["user_id"]


# ---------------------------------------------------------------------------
# 1. Owner protection: a later-added member cannot touch the owner.
# ---------------------------------------------------------------------------


async def test_owner_protection_against_later_added_member() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner_email = _email()
        owner = await _signup(c, owner_email)
        ws = owner["workspace_id"]
        owner_id = await _user_id(c, owner["token"])
        oh = {**_bearer(owner["token"]), "X-Workspace-Id": ws, "X-Workspace-Role": "owner"}

        # Owner adds U as a plain member.
        u_email = _email()
        u = await _signup(c, u_email)
        u_id = await _user_id(c, u["token"])
        added = await c.post(
            "/workspaces/me/members", headers=oh, json={"email": u_email, "role": "member"}
        )
        assert added.status_code == 200
        assert {m["email"]: m["role"] for m in added.json()} == {
            owner_email: "owner",
            u_email: "member",
        }

        # U may LIST the roster (any member) ...
        u_plain = {**_bearer(u["token"]), "X-Workspace-Id": ws}
        u_forged = {**u_plain, "X-Workspace-Role": "owner"}  # clamped to member
        listed = await c.get("/workspaces/me/members", headers=u_plain)
        assert listed.status_code == 200
        assert {m["email"] for m in listed.json()} == {owner_email, u_email}

        # ... but U CANNOT mutate it, with or without the forged header.
        for hdr in (u_plain, u_forged):
            assert (
                await c.post(
                    "/workspaces/me/members",
                    headers=hdr,
                    json={"email": _email(), "role": "member"},
                )
            ).status_code == 403
            assert (
                await c.patch(
                    f"/workspaces/me/members/{owner_id}",
                    headers=hdr,
                    json={"role": "member"},
                )
            ).status_code == 403
            # U cannot eject the owner ...
            assert (
                await c.delete(f"/workspaces/me/members/{owner_id}", headers=hdr)
            ).status_code == 403
            # ... nor remove itself via the members endpoint (not owner).
            assert (
                await c.delete(f"/workspaces/me/members/{u_id}", headers=hdr)
            ).status_code == 403

        # The sole owner cannot demote nor remove itself.
        demote = await c.patch(
            f"/workspaces/me/members/{owner_id}", headers=oh, json={"role": "member"}
        )
        assert demote.status_code == 400 and demote.json()["code"] == "member.last_owner"
        rm_self = await c.delete(f"/workspaces/me/members/{owner_id}", headers=oh)
        assert rm_self.status_code == 400 and rm_self.json()["code"] == "member.last_owner"

        # Owner promotes U to owner. Now there are two owners.
        promoted = await c.patch(
            f"/workspaces/me/members/{u_id}", headers=oh, json={"role": "owner"}
        )
        assert promoted.status_code == 200
        assert {m["email"]: m["role"] for m in promoted.json()}[u_email] == "owner"

        # U (now owner) may remove the original owner O.
        uh = {**_bearer(u["token"]), "X-Workspace-Id": ws, "X-Workspace-Role": "owner"}
        rm_o = await c.delete(f"/workspaces/me/members/{owner_id}", headers=uh)
        assert rm_o.status_code == 204
        roster = (await c.get("/workspaces/me/members", headers=uh)).json()
        assert {m["email"] for m in roster} == {u_email}

        # U is now the LAST owner: irremovable, and cannot self-demote.
        rm_last = await c.delete(f"/workspaces/me/members/{u_id}", headers=uh)
        assert rm_last.status_code == 400 and rm_last.json()["code"] == "member.last_owner"
        demote_last = await c.patch(
            f"/workspaces/me/members/{u_id}", headers=uh, json={"role": "member"}
        )
        assert demote_last.status_code == 400
        assert demote_last.json()["code"] == "member.last_owner"


async def test_member_added_later_clamped_even_forging_owner() -> None:
    """A user added with role ``member``, sending ``X-Workspace-Role:
    owner``, gets 403 on EVERY member mutation (effective-role clamp +
    owner-only gate, defense in depth)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup(c)
        ws = owner["workspace_id"]
        owner_id = await _user_id(c, owner["token"])
        oh = {**_bearer(owner["token"]), "X-Workspace-Id": ws, "X-Workspace-Role": "owner"}

        m_email = _email()
        m = await _signup(c, m_email)
        await c.post(
            "/workspaces/me/members", headers=oh, json={"email": m_email, "role": "member"}
        )
        forged = {**_bearer(m["token"]), "X-Workspace-Id": ws, "X-Workspace-Role": "owner"}

        assert (
            await c.post(
                "/workspaces/me/members",
                headers=forged,
                json={"email": _email(), "role": "member"},
            )
        ).status_code == 403
        assert (
            await c.patch(
                f"/workspaces/me/members/{owner_id}",
                headers=forged,
                json={"role": "member"},
            )
        ).status_code == 403
        assert (
            await c.delete(f"/workspaces/me/members/{owner_id}", headers=forged)
        ).status_code == 403


# ---------------------------------------------------------------------------
# 2. Privileged writes (clients / workflows / issuer profiles) = owner.
# ---------------------------------------------------------------------------

_WF_BODY = {
    "name": "WF",
    "states": [{"name": "open", "ord": 1, "is_initial": True}],
    "transitions": [],
}
_CLIENT_BODY = {"name": "Acme", "ragione_sociale": "Acme SRL"}
_IP_BODY = {"label": "Me", "denominazione": "Me SRL"}


async def test_privileged_writes_require_owner() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner_email = _email()
        owner = await _signup(c, owner_email)
        ws = owner["workspace_id"]
        base = {**_bearer(owner["token"]), "X-Workspace-Id": ws}  # effective member
        as_owner = {**base, "X-Workspace-Role": "owner"}

        # Owner WITHOUT the role header => effective member => every
        # privileged write is 403 (least privilege).
        assert (await c.post("/clients", headers=base, json=_CLIENT_BODY)).status_code == 403
        assert (await c.post("/workflows", headers=base, json=_WF_BODY)).status_code == 403
        assert (await c.post("/issuer-profiles", headers=base, json=_IP_BODY)).status_code == 403

        # Owner WITH X-Workspace-Role: owner => 200.
        ok = await c.post("/clients", headers=as_owner, json=_CLIENT_BODY)
        assert ok.status_code == 200 and ok.json()["id"]
        assert (await c.post("/workflows", headers=as_owner, json=_WF_BODY)).status_code == 200
        assert (
            await c.post("/issuer-profiles", headers=as_owner, json=_IP_BODY)
        ).status_code == 200

        # A real member of the workspace: reads OK, privileged writes 403.
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
        assert (await c.post("/clients", headers=mh, json=_CLIENT_BODY)).status_code == 403
        assert (await c.post("/workflows", headers=mh, json=_WF_BODY)).status_code == 403
        assert (await c.post("/issuer-profiles", headers=mh, json=_IP_BODY)).status_code == 403
        # Even forging owner: still clamped to member => 403.
        assert (
            await c.post(
                "/clients",
                headers={**mh, "X-Workspace-Role": "owner"},
                json=_CLIENT_BODY,
            )
        ).status_code == 403


# ---------------------------------------------------------------------------
# 3. Namespace isolation: zero cross-workspace leakage.
# ---------------------------------------------------------------------------


async def test_namespace_isolation_no_leakage() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a_email = _email()
        a = await _signup(c, a_email)
        wa = a["workspace_id"]
        a_owner = {**_bearer(a["token"]), "X-Workspace-Id": wa, "X-Workspace-Role": "owner"}

        b_email = _email()
        b = await _signup(c, b_email)
        wb = b["workspace_id"]
        b_id = await _user_id(c, b["token"])

        # B has NO membership in WA. Pointing B's token at WA: every
        # tenant-scoped surface is rejected (membership is checked
        # before the role; the forged owner header is irrelevant).
        b_in_wa = {**_bearer(b["token"]), "X-Workspace-Id": wa}
        b_in_wa_forged = {**b_in_wa, "X-Workspace-Role": "owner"}

        no_ws = await c.get("/workspaces/me", headers=b_in_wa)
        assert no_ws.status_code == 403 and no_ws.json()["code"] == "rbac.no_membership"
        assert (await c.get("/workspaces/me", headers=b_in_wa_forged)).status_code == 403
        assert (await c.get("/workspaces/me/members", headers=b_in_wa)).status_code == 403
        assert (await c.get("/clients", headers=b_in_wa)).status_code == 403
        assert (await c.get("/tasks", headers=b_in_wa)).status_code == 403
        assert (await c.get("/notes", headers=b_in_wa)).status_code == 403
        # A write into WA, even forging owner, is rejected (no membership).
        assert (
            await c.post("/clients", headers=b_in_wa_forged, json=_CLIENT_BODY)
        ).status_code == 403

        # Data isolation: A creates a client in WA; B (in WB) never sees
        # it. (Listing clients in WB returns only WB's own data.)
        secret = await c.post(
            "/clients",
            headers=a_owner,
            json={"name": "SECRET-A", "ragione_sociale": "Secret A SRL"},
        )
        assert secret.status_code == 200
        b_in_wb = {**_bearer(b["token"]), "X-Workspace-Id": wb}
        b_clients = await c.get("/clients", headers=b_in_wb)
        assert b_clients.status_code == 200
        assert all(cl.get("name") != "SECRET-A" for cl in b_clients.json())

        # B cannot add ITSELF to WA's roster (not a member ⇒ not owner).
        assert (
            await c.post(
                "/workspaces/me/members",
                headers=b_in_wa_forged,
                json={"email": b_email, "role": "owner"},
            )
        ).status_code == 403
        # And B's user id is still absent from WA's roster (seen by A).
        wa_roster = (await c.get("/workspaces/me/members", headers=a_owner)).json()
        assert b_id not in {m["user_id"] for m in wa_roster}
        assert {m["email"] for m in wa_roster} == {a_email}

        # The switcher lists only the workspaces the user belongs to:
        # B sees WB, never WA.
        b_ws = await c.get("/workspaces", headers=_bearer(b["token"]))
        assert b_ws.status_code == 200
        b_ws_ids = {w["id"] for w in b_ws.json()}
        assert wb in b_ws_ids
        assert wa not in b_ws_ids
