"""E2E: the attachment download endpoint accepts a parent-scoped,
multi-use capability token (``mycelium_cap_``) in addition to a normal bearer.

Covers the agent-without-a-CLI path: an MCP-minted capability token scoped
to ``attachment:read`` on a task/note pulls that parent's attachments to
disk with no PAT and no X-Workspace-Id. Unlike the note-part write
capability it is NOT single-use: one mint downloads every attachment of the
parent until its TTL. It still confines access -- only attachments of the
scoped parent, and only the download endpoint understands ``mycelium_cap_``.
The normal-bearer path stays covered by ``test_attachments.py``.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.db import tenant_session
from mycelium_core.services import capability_tokens as svc


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> tuple[dict[str, str], uuid.UUID, uuid.UUID]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    return h, uuid.UUID(a["workspace_id"]), uuid.UUID(a["user_id"])


async def _task_with_file(c: AsyncClient, h: dict[str, str], body: bytes) -> tuple[str, str]:
    task = (await c.post("/tasks", headers=h, json={"title": "T with files"})).json()
    up = await c.post(
        f"/tasks/{task['id']}/attachments",
        headers=h,
        files={"file": ("doc.txt", body, "text/plain")},
    )
    assert up.status_code == 200, up.text
    return task["id"], up.json()["id"]


async def _mint_attachment_read(
    org: uuid.UUID, user: uuid.UUID, *, resource_id: uuid.UUID, kind: str = svc.RESOURCE_TASK
) -> str:
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.mint(
            s,
            org_id=org,
            actor_id=user,
            action=svc.ACTION_ATTACHMENT_READ,
            resource_kind=kind,
            resource_id=resource_id,
        )
    return res.raw


async def test_download_with_capability_token_is_multi_use() -> None:
    body = b"the financing conditions PDF stand-in\n"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        tid, aid = await _task_with_file(c, h, body)
        raw = await _mint_attachment_read(org, user, resource_id=uuid.UUID(tid))
        # No X-Workspace-Id: the org is baked into the capability token.
        cap_h = {"Authorization": f"Bearer {raw}"}

        first = await c.get(f"/attachments/{aid}/download", headers=cap_h)
        assert first.status_code == 200, first.text
        assert first.content == body

        # Multi-use: the same token still works on a second download (a
        # download is idempotent and the token is bounded only by its TTL).
        second = await c.get(f"/attachments/{aid}/download", headers=cap_h)
        assert second.status_code == 200, second.text
        assert second.content == body


async def test_capability_token_other_parent_is_forbidden() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        _, aid = await _task_with_file(c, h, b"x")
        # Token scoped to a DIFFERENT (random) task: the attachment's real
        # parent never matches, so it is out of scope -> 403 (bytes never leave).
        raw = await _mint_attachment_read(org, user, resource_id=uuid.uuid4())
        r = await c.get(f"/attachments/{aid}/download", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 403, r.text


async def test_capability_token_wrong_action_is_forbidden() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        _, aid = await _task_with_file(c, h, b"x")
        # A note-part-body write capability must not unlock a download.
        async with tenant_session(str(org), str(user)) as s:
            grant = await svc.mint(
                s,
                org_id=org,
                actor_id=user,
                action=svc.ACTION_NOTE_PART_BODY_WRITE,
                resource_kind=svc.RESOURCE_NOTE_PART,
                resource_id=uuid.uuid4(),
            )
        r = await c.get(
            f"/attachments/{aid}/download",
            headers={"Authorization": f"Bearer {grant.raw}"},
        )
        assert r.status_code == 403, r.text


async def test_mint_capability_endpoint_then_download() -> None:
    """The CLI's server side: POST /attachments/capability mints a grant +
    lists the parent's files; the returned token downloads them."""
    body = b"financing conditions\n"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _, _ = await _signup(c)
        tid, aid = await _task_with_file(c, h, body)

        minted = await c.post(
            "/attachments/capability",
            headers=h,
            json={"parent_kind": "task", "parent_id": tid, "ttl_seconds": 120},
        )
        assert minted.status_code == 201, minted.text
        grant = minted.json()
        assert grant["token"].startswith("mycelium_cap_")
        assert grant["parent_kind"] == "task"
        assert [a["id"] for a in grant["attachments"]] == [aid]

        # The minted token works on the download endpoint, no X-Workspace-Id.
        dl = await c.get(
            f"/attachments/{aid}/download",
            headers={"Authorization": f"Bearer {grant['token']}"},
        )
        assert dl.status_code == 200, dl.text
        assert dl.content == body


async def test_unknown_capability_token_is_unauthorized() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _, _ = await _signup(c)
        _, aid = await _task_with_file(c, h, b"x")
        # A well-formed but never-minted capability token: unknown hash.
        bogus = f"{svc.RAW_PREFIX}{'a' * 43}"
        r = await c.get(
            f"/attachments/{aid}/download", headers={"Authorization": f"Bearer {bogus}"}
        )
        assert r.status_code == 401, r.text
