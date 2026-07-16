"""Regression: an agent's authorship of a comment is transport-invariant.

If an agent (an ai_assistant identity behind an agent token) creates a
comment, it must be able to edit and delete that comment *as the author*
over EVERY write surface -- the JSON REST endpoints, the streaming
endpoints, the MCP tools -- without needing an admin role.

The bug this pins: the JSON ``POST /annotations/comment``,
``PATCH /annotations/{id}`` and ``DELETE /annotations/{id}`` endpoints
dropped the agent's identity badge, attributing the write to the
token-owner's *user* identity instead of the ai_assistant. The
author-or-admin gate then fell through to the admin fallback. That
fallback silently rescued a normally-minted (owner-owned) token, so the
defect was invisible for an owner -- but the moment the effective role was
clamped below admin (the SPA per-tab "act as" downgrade, or a member
principal / PAT) it surfaced as a real 403 ``rbac.role_insufficient
(requires >= admin)`` while editing YOUR OWN comment. The streaming
endpoints and the MCP tools already threaded the badge, which is why the
same edit worked through those surfaces: the seam was JSON-vs-stream.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from tests_helpers import seed_ai_assistant_identity

from mycelium_api.main import app
from mycelium_core.db import tenant_session
from mycelium_core.services import agent_tokens


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _note_with_part(c: AsyncClient, h: dict[str, str], body: str) -> str:
    note = (await c.post("/notes", headers=h, json={"kind": "text", "text": body})).json()
    full = (await c.get(f"/notes/{note['id']}", headers=h)).json()
    return str(full["parts"][0]["id"])


async def _agent_setup(
    c: AsyncClient,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], str, str]:
    """Owner signs up, seeds an ai_assistant, and mints an agent token bound
    to it. Returns ``(human_headers, agent_headers, agent_as_member_headers,
    ai_identity_id, note_part_id)``. ``agent_as_member_headers`` is the same
    agent token clamped to ``member`` via the ``X-Workspace-Role`` downgrade
    lever -- the effective role a real 'act as member' / member principal has,
    the condition under which the admin fallback stops masking the bug."""
    su = (
        await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})
    ).json()
    org = uuid.UUID(su["workspace_id"])
    user = uuid.UUID(su["user_id"])
    h = {"Authorization": f"Bearer {su['token']}", "X-Workspace-Id": su["workspace_id"]}
    pid = await _note_with_part(c, h, "Reviewable body.")

    async with tenant_session(str(org), str(user)) as s:
        ai_ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        ai_ident_id = str(ai_ident.id)
        ai_assistant_id = ai_ident.ai_assistant_id
    async with tenant_session(str(org), str(user)) as s:
        minted = await agent_tokens.mint(
            s, org_id=org, actor_id=user, name="copilot", assistant_id=ai_assistant_id
        )
        raw = minted.raw

    ah = {"Authorization": f"Bearer {raw}", "X-Workspace-Id": su["workspace_id"]}
    ahm = {**ah, "X-Workspace-Role": "member"}
    return h, ah, ahm, ai_ident_id, pid


# --------------------------------------------------------------------------
# create-side: the JSON create must record the ai_assistant badge (role-free)
# --------------------------------------------------------------------------
async def test_json_create_comment_attributes_assistant_identity() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, ah, _, ai_id, pid = await _agent_setup(c)
        made = (
            await c.post(
                "/annotations/comment",
                headers=ah,
                json={"doc_kind": "note_part", "doc_id": pid, "body": "made via json"},
            )
        ).json()
        # The JSON create endpoint must attribute to the assistant, exactly
        # like /comment/stream and the MCP add_annotation tool.
        assert made["author_identity_id"] == ai_id
        assert made["author_kind"] == "ai_assistant"


async def test_json_create_suggestion_attributes_assistant_identity() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, ah, _, ai_id, pid = await _agent_setup(c)
        made = (
            await c.post(
                "/annotations/suggestion",
                headers=ah,
                json={
                    "doc_kind": "note_part",
                    "doc_id": pid,
                    "original_text": "Reviewable",
                    "proposed_text": "Reviewed",
                    "rationale": "clarity",
                },
            )
        ).json()
        assert made["author_identity_id"] == ai_id


# --------------------------------------------------------------------------
# edit/delete-side: the author can edit+delete its OWN comment via the JSON
# endpoints even when the effective role is clamped below admin (the reported
# 403). The author gate must pass on AUTHORSHIP, not on the admin fallback.
# --------------------------------------------------------------------------
async def _agent_stream_comment(c: AsyncClient, ah: dict[str, str], pid: str) -> tuple[str, int]:
    """Author a comment through the stream endpoint (which already threads the
    assistant badge), so the row's author is the ai_assistant -- the exact
    state left by the MCP add_comment tool. Returns ``(id, version)``."""
    made = (
        await c.post(
            "/annotations/comment/stream",
            headers=ah,
            params={"doc_kind": "note_part", "doc_id": pid},
            content=b"authored by the assistant",
        )
    ).json()
    return str(made["id"]), int(made["version"])


async def test_agent_edits_own_comment_via_json_under_member_role() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, ah, ahm, _, pid = await _agent_setup(c)
        cid, ver = await _agent_stream_comment(c, ah, pid)
        # Edit its OWN comment via the JSON endpoint, acting as member.
        r = await c.patch(
            f"/annotations/{cid}",
            headers=ahm,
            json={"body": "edited by its author", "expected_version": ver},
        )
        assert r.status_code == 200, r.text
        got = (await c.get(f"/annotations/{cid}", headers=ah)).json()
        assert got["body"] == "edited by its author"


async def test_agent_deletes_own_comment_via_json_under_member_role() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, ah, ahm, _, pid = await _agent_setup(c)
        cid, ver = await _agent_stream_comment(c, ah, pid)
        r = await c.delete(f"/annotations/{cid}", headers=ahm, params={"expected_version": ver})
        assert r.status_code == 200, r.text


async def test_agent_edits_own_comment_via_stream_under_member_role() -> None:
    """Control: the streaming edit already threads the badge, so it works
    under the member clamp today. Proves the seam is JSON-vs-stream, not the
    clamp itself."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, ah, ahm, _, pid = await _agent_setup(c)
        cid, ver = await _agent_stream_comment(c, ah, pid)
        r = await c.patch(
            f"/annotations/{cid}/body/stream",
            headers=ahm,
            params={"expected_version": ver},
            content=b"edited via stream by its author",
        )
        assert r.status_code == 200, r.text


# --------------------------------------------------------------------------
# attribution parity on the resolve branch: an agent resolving via the JSON
# endpoint stamps resolved_by with its ai_assistant badge (as the MCP tool
# does), not the token-owner's user identity.
# --------------------------------------------------------------------------
async def test_json_resolve_attributes_assistant_identity() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        _, ah, _, ai_id, pid = await _agent_setup(c)
        cid, ver = await _agent_stream_comment(c, ah, pid)
        r = await c.post(f"/annotations/{cid}/resolve", headers=ah, json={"expected_version": ver})
        assert r.status_code == 200, r.text
        got = (await c.get(f"/annotations/{cid}", headers=ah)).json()
        assert got["resolved_by_identity_id"] == ai_id
