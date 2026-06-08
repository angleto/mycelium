"""E2E: the note-part body/stream endpoint accepts a scoped, single-use
capability token (``flow_cap_``) in addition to a normal bearer.

Covers the agent-without-a-CLI path: an MCP-minted capability token is
streamed straight to ``PUT /notes/{id}/parts/{pid}/body/stream`` with no
PAT and no X-Workspace-Id, is confined to its exact part, and is burned
on first success. The normal-bearer path stays covered by
``test_inline_body_stream.py``.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.db import tenant_session
from flow_core.services import capability_tokens as svc


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> tuple[dict[str, str], uuid.UUID, uuid.UUID]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    headers = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    return headers, uuid.UUID(a["workspace_id"]), uuid.UUID(a["user_id"])


async def _note_with_part(c: AsyncClient, h: dict[str, str], body: str) -> tuple[str, str, int]:
    note = (await c.post("/notes", headers=h, json={"kind": "text", "text": body})).json()
    full = (await c.get(f"/notes/{note['id']}", headers=h)).json()
    p0 = full["parts"][0]
    return note["id"], p0["id"], p0["version"]


async def _mint_for(org: uuid.UUID, user: uuid.UUID, part_id: uuid.UUID) -> str:
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.mint(
            s,
            org_id=org,
            actor_id=user,
            action=svc.ACTION_NOTE_PART_BODY_WRITE,
            resource_kind=svc.RESOURCE_NOTE_PART,
            resource_id=part_id,
        )
    return res.raw


async def test_stream_with_capability_token_writes_and_is_single_use() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid, pid, v = await _note_with_part(c, h, "original")
        raw = await _mint_for(org, user, uuid.UUID(pid))
        # No X-Workspace-Id: the org is baked into the capability token.
        cap_h = {"Authorization": f"Bearer {raw}"}

        r = await c.put(
            f"/notes/{nid}/parts/{pid}/body/stream",
            headers=cap_h,
            params={"expected_version": v},
            content=b"via capability",
        )
        assert r.status_code == 200, r.text
        assert r.json()["version"] == v + 1

        full = (await c.get(f"/notes/{nid}", headers=h)).json()
        assert next(p for p in full["parts"] if p["id"] == pid)["body"] == "via capability"

        # Single-use: the token is consumed, so a second write is rejected.
        r2 = await c.put(
            f"/notes/{nid}/parts/{pid}/body/stream",
            headers=cap_h,
            params={"expected_version": v + 1},
            content=b"again",
        )
        assert r2.status_code == 401, r2.text


async def test_capability_token_wrong_part_is_forbidden() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        nid, pid, v = await _note_with_part(c, h, "original")
        # Mint a token scoped to a DIFFERENT part id.
        raw = await _mint_for(org, user, uuid.uuid4())

        r = await c.put(
            f"/notes/{nid}/parts/{pid}/body/stream",
            headers={"Authorization": f"Bearer {raw}"},
            params={"expected_version": v},
            content=b"nope",
        )
        assert r.status_code == 403, r.text

        # The body is unchanged (the rejected request never wrote).
        full = (await c.get(f"/notes/{nid}", headers=h)).json()
        assert next(p for p in full["parts"] if p["id"] == pid)["body"] == "original"
