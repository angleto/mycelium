"""Agent tokens HTTP surface (v1.1).

Real DB + real app via ASGITransport, no mocking. Covers:

- POST returns the raw value exactly once
- GET never echoes the raw value (only metadata)
- DELETE revokes; idempotent
- a revoked token no longer authenticates against the MCP / bearer path
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.services import agent_tokens as svc


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup_and_headers(c: AsyncClient) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": _email(),
                "password": "pw-strong-123",
                "workspace_name": "T",
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        # The owner of a fresh workspace defaults to ``member`` under
        # the sudo header model; ask for ``owner`` so the owner-gated
        # mint endpoint passes the RBAC choke point.
        "X-Workspace-Role": "owner",
    }


async def test_post_returns_raw_and_get_hides_it() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        headers = await _signup_and_headers(c)

        # Empty list to start.
        r = await c.get("/agent-tokens", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == []

        # Mint a token: the response carries ``raw`` exactly here.
        r = await c.post(
            "/agent-tokens",
            headers=headers,
            json={"name": "Claude Desktop"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Claude Desktop"
        assert body["scope"] == "mcp"
        assert body["prefix"].startswith(svc.RAW_PREFIX)
        assert body["raw"].startswith(svc.RAW_PREFIX)
        assert body["expires_at"] is not None
        token_id = body["id"]
        raw = body["raw"]

        # GET never echoes the raw value; just metadata.
        r = await c.get("/agent-tokens", headers=headers)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1
        listed = rows[0]
        assert "raw" not in listed
        assert listed["id"] == token_id
        assert listed["prefix"] == body["prefix"]
        assert listed["revoked_at"] is None

        # Authenticate the raw against the service to confirm it works
        # end-to-end (and that we have the right value out of the wire).
        result = await svc.authenticate(raw)
        assert result is not None


async def test_delete_revokes_and_post_revoke_auth_fails() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        headers = await _signup_and_headers(c)

        mint = (
            await c.post(
                "/agent-tokens",
                headers=headers,
                json={"name": "cli", "scope": "mcp"},
            )
        ).json()
        raw = mint["raw"]
        token_id = mint["id"]

        # The token works.
        assert await svc.authenticate(raw) is not None

        # Revoke it.
        r = await c.delete(f"/agent-tokens/{token_id}", headers=headers)
        assert r.status_code == 204, r.text

        # A revoked token does not authenticate.
        assert await svc.authenticate(raw) is None

        # Idempotent re-revoke is also 204.
        r = await c.delete(f"/agent-tokens/{token_id}", headers=headers)
        assert r.status_code == 204, r.text

        # The row is still listed (audit visibility), with ``revoked_at``
        # populated.
        rows = (await c.get("/agent-tokens", headers=headers)).json()
        assert len(rows) == 1
        assert rows[0]["revoked_at"] is not None


async def test_api_accepts_agent_token_as_bearer() -> None:
    """Regression: the REST API ``current_claims`` dependency must
    accept ``flow_at_...`` agent tokens (used by the CLI), not just
    session JWTs. Previously ``decode_token`` (JWT-only) was wired in,
    so every CLI command after ``flow auth login`` returned 401
    ``auth.token_invalid`` despite a freshly-minted, valid PAT."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        login_headers = await _signup_and_headers(c)
        workspace_id = login_headers["X-Workspace-Id"]

        # Mint a PAT via the JWT-authed mint endpoint.
        mint = (
            await c.post(
                "/agent-tokens",
                headers=login_headers,
                json={"name": "cli-regression", "scope": "cli"},
            )
        ).json()
        pat = mint["raw"]
        assert pat.startswith(svc.RAW_PREFIX)

        # Hit a protected, tenant-scoped route with ONLY the PAT as
        # Bearer (no JWT). Pre-fix this returned 401
        # auth.token_invalid because the API tried jwt.decode() on the
        # opaque agent token. Post-fix decode_token_async branches on
        # the prefix and the request goes through normally.
        pat_headers = {
            "Authorization": f"Bearer {pat}",
            "X-Workspace-Id": workspace_id,
        }
        r = await c.get("/tasks", headers=pat_headers)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)


async def test_ttl_zero_disables_expiry() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        headers = await _signup_and_headers(c)
        r = await c.post(
            "/agent-tokens",
            headers=headers,
            json={"name": "forever", "ttl_days": 0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["expires_at"] is None


async def test_non_owner_cannot_mint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # Owner signs up + mints OK; a second account is invited as a
        # member and the same POST must be rejected with 403.
        from flow_core.db import admin_session, tenant_session
        from flow_core.models.membership import Membership, Role
        from flow_core.services.auth import login, signup

        owner_email = _email()
        member_email = _email()
        async with admin_session() as s:
            owner = await signup(s, email=owner_email, password="pw-strong-123", org_name="A")
            member = await signup(s, email=member_email, password="pw-strong-123", org_name="B")
        async with tenant_session(str(owner.org_id), str(owner.user_id)) as s:
            s.add(Membership(org_id=owner.org_id, user_id=member.user_id, role=Role.member))
            await s.flush()

        async with admin_session() as s:
            member_jwt = await login(s, email=member_email, password="pw-strong-123")
        headers = {
            "Authorization": f"Bearer {member_jwt}",
            "X-Workspace-Id": str(owner.org_id),
            # Even asking for owner explicitly: the ceiling is the
            # caller's stored membership (member), so it clamps down.
            "X-Workspace-Role": "owner",
        }
        r = await c.post(
            "/agent-tokens",
            headers=headers,
            json={"name": "shouldnt"},
        )
        assert r.status_code == 403, r.text
