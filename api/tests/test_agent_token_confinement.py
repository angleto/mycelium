"""An agent token acts in the workspace it was minted for, and nowhere else.

The token has carried ``org_id`` since it was introduced and every client
already believes the binding: the CLI stores a workspace beside each
credential, refuses to switch, and prints the ``auth login`` to re-mint,
telling the user in its own words that "agent tokens are workspace-scoped;
there is no side-effect switch".

The server did not check it. The header decided the tenant, so a credential
minted for one workspace worked in every other workspace its user happened to
belong to. That is not a leak between people -- RLS still confines the query
to whatever tenant was named, and the caller had to be a member -- but it
makes the per-workspace credential model a convention held up by clients
rather than a boundary, which is exactly the thing a boundary is not allowed
to be.

The MCP surface never had the gap: there the tenant comes from the principal
rather than from a header. These assert the REST door now matches it, on both
of the two paths that open a tenant.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


async def _signup(c: AsyncClient, name: str) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": name,
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _second_workspace(c: AsyncClient, owner: dict[str, str]) -> str:
    created = await c.post("/workspaces", headers=owner, json={"name": "Second"})
    assert created.status_code in (200, 201), created.text
    return str(created.json()["id"])


async def _agent_token(c: AsyncClient, owner: dict[str, str]) -> str:
    created = await c.post(
        "/ai-assistants",
        headers=owner,
        json={
            "label": "Ext",
            "handle": f"e{uuid.uuid4().hex[:8]}",
            "scope": ["tasks:read", "notes:read"],
        },
    )
    assert created.status_code in (200, 201), created.text
    return str(created.json()["raw_secret"])


async def test_a_credential_is_refused_in_another_workspace_of_the_same_person() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup(c, "First")
        first = owner["X-Workspace-Id"]
        second = await _second_workspace(c, owner)
        secret = await _agent_token(c, owner)

        headers = {"Authorization": f"Bearer {secret}"}

        # Where it was minted: fine.
        ok = await c.get("/tasks", headers={**headers, "X-Workspace-Id": first})
        assert ok.status_code == 200, ok.text

        # Anywhere else: refused, even though the SAME PERSON owns both.
        # Membership is not the question; which credential this is, is.
        denied = await c.get("/tasks", headers={**headers, "X-Workspace-Id": second})
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "agent.workspace_mismatch"


async def test_the_refusal_says_the_credential_is_wrong_not_that_it_is_absent() -> None:
    """403 rather than 404 on purpose. The caller may well be a member of
    the workspace they asked for, and answering "absent" would tell them
    it does not exist -- sending them to create a duplicate. What is
    wrong is the credential, and the fix is to mint one there."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup(c, "First")
        second = await _second_workspace(c, owner)
        secret = await _agent_token(c, owner)
        res = await c.get(
            "/tasks",
            headers={"Authorization": f"Bearer {secret}", "X-Workspace-Id": second},
        )
        assert res.status_code == 403
        assert "another workspace" in res.json()["detail"]


async def test_a_human_session_still_moves_between_its_own_workspaces() -> None:
    """The whole difference between a session and a credential. A person
    legitimately switches workspace on one login, and a session JWT
    carries no org_id for exactly that reason."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup(c, "First")
        second = await _second_workspace(c, owner)
        moved = await c.get("/tasks", headers={**owner, "X-Workspace-Id": second})
        assert moved.status_code == 200, moved.text


async def test_the_confinement_holds_on_the_capability_or_bearer_door_too() -> None:
    """There are two functions that open a tenant: the ordinary dependency
    and the one the block/stream routes use, which must branch on a
    capability bearer before it can resolve a user. A rule enforced on one
    of two doors is not enforced."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup(c, "First")
        second = await _second_workspace(c, owner)
        secret = await _agent_token(c, owner)
        task = (await c.post("/tasks", headers=owner, json={"title": "t"})).json()

        # A description-body route: the second door.
        res = await c.get(
            f"/tasks/{task['id']}/description/raw",
            headers={"Authorization": f"Bearer {secret}", "X-Workspace-Id": second},
        )
        assert res.status_code == 403, res.text
        assert res.json()["code"] == "agent.workspace_mismatch"
