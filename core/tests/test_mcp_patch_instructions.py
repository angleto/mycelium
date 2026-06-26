"""MCP capability-token block tools: upload / get / set / patch recipes.

Each tool mints a real ``mycelium_cap_`` token (so it touches the DB via
``_tenant`` + ``cap_svc.mint``) and returns a ready ``curl`` with that
ephemeral token baked into the Authorization header -- no ``$MYCELIUM_TOKEN``
placeholder and no ``X-Workspace-Id`` (the org is in the token). The
caller's session ``token`` must never leak into the recipe. Synthetic
target ids are enough: ``mint`` does not require the resource to exist.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp import server as mcp_server


async def _signup() -> tuple[uuid.UUID, uuid.UUID, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCPCAP",
        )
    assert r.token is not None
    return r.org_id, r.user_id, r.token


def _assert_capability_curl(out: dict[str, Any], *, method: str, token: str) -> None:
    """Shared invariants: the recipe carries an ephemeral mycelium_cap_ token
    in the Authorization header, never the caller's session token, never a
    $MYCELIUM_TOKEN placeholder or an X-Workspace-Id (the org is in the token),
    and never an S3 URL (gateway rule)."""
    base = get_settings().frontend_base_url.rstrip("/")
    assert out["endpoint"].startswith(f"{base}/api/")
    assert out["method"] == method
    assert out["headers"]["Authorization"].startswith("Bearer mycelium_cap_")
    assert "mycelium_cap_" in out["curl"]
    assert "expires_at" in out
    # The caller's secret session token never leaks.
    assert token not in out["curl"]
    assert token not in repr(out)
    # Capability recipes bake the token in: no placeholder, no workspace header.
    assert "$MYCELIUM_TOKEN" not in out["curl"]
    assert "X-Workspace-Id" not in out["curl"]
    assert "X-Workspace-Id" not in out["headers"]
    # Gateway rule: never an object-store URL.
    assert "amazonaws" not in out["curl"].lower()
    assert "s3" not in out["endpoint"].lower()


async def test_upload_attachment_capability_recipe() -> None:
    org, _user, token = await _signup()
    tid = uuid.uuid4()
    out = await mcp_server.upload_attachment_capability(
        token=token, org_id=str(org), parent_kind="task", parent_id=str(tid)
    )
    _assert_capability_curl(out, method="POST", token=token)
    assert f"/api/tasks/{tid}/attachments" in out["endpoint"]
    assert "-X POST" in out["curl"]
    assert "-F 'file=@<path-to-file>'" in out["curl"]
    assert out["max_bytes"] == get_settings().attachment_max_bytes


async def test_upload_attachment_capability_rejects_bad_parent() -> None:
    org, _user, token = await _signup()
    with pytest.raises(ValueError):
        await mcp_server.upload_attachment_capability(
            token=token, org_id=str(org), parent_kind="widget", parent_id=str(uuid.uuid4())
        )


async def test_get_note_part_body_capability_recipe() -> None:
    org, _user, token = await _signup()
    nid, pid = uuid.uuid4(), uuid.uuid4()
    out = await mcp_server.get_note_part_body_capability(
        token=token, org_id=str(org), note_id=str(nid), part_id=str(pid)
    )
    _assert_capability_curl(out, method="GET", token=token)
    assert f"/api/notes/{nid}/parts/{pid}/body/raw" in out["endpoint"]
    # download recipe dumps headers (-D -) so the caller captures the base gate
    assert "-D -" in out["curl"]
    assert "-o <path-to-file>" in out["curl"]


async def test_patch_note_part_body_capability_recipe() -> None:
    org, _user, token = await _signup()
    nid, pid = uuid.uuid4(), uuid.uuid4()
    digest = "a" * 64
    out = await mcp_server.patch_note_part_body_capability(
        token=token,
        org_id=str(org),
        note_id=str(nid),
        part_id=str(pid),
        expected_version=7,
        base_sha256=digest,
    )
    _assert_capability_curl(out, method="POST", token=token)
    assert f"/api/notes/{nid}/parts/{pid}/body/patch" in out["endpoint"]
    assert "expected_version=7" in out["endpoint"]
    assert f"base_sha256={digest}" in out["endpoint"]
    assert "-X POST" in out["curl"]
    assert "--data-binary @<path-to-patch.diff>" in out["curl"]
    assert out["headers"]["Content-Type"] == "text/x-diff"
    assert out["max_bytes"] == get_settings().note_patch_max_bytes


@pytest.mark.parametrize(
    "kind, collection, leaf, write_method",
    [
        ("task_description", "tasks", "description", "PUT"),
        ("annotation", "annotations", "body", "PATCH"),
    ],
)
async def test_text_block_capability_recipes(
    kind: str, collection: str, leaf: str, write_method: str
) -> None:
    org, _user, token = await _signup()
    rid = uuid.uuid4()
    digest = "b" * 64

    get_out = await mcp_server.get_text_block_capability(
        token=token, org_id=str(org), kind=kind, resource_id=str(rid)
    )
    _assert_capability_curl(get_out, method="GET", token=token)
    assert f"/api/{collection}/{rid}/{leaf}/raw" in get_out["endpoint"]
    assert "-D -" in get_out["curl"]

    set_out = await mcp_server.set_text_block_capability(
        token=token, org_id=str(org), kind=kind, resource_id=str(rid), expected_version=3
    )
    _assert_capability_curl(set_out, method=write_method, token=token)
    assert f"/api/{collection}/{rid}/{leaf}/stream" in set_out["endpoint"]
    assert "expected_version=3" in set_out["endpoint"]
    assert "--data-binary @<path-to-file>" in set_out["curl"]

    patch_out = await mcp_server.patch_text_block_capability(
        token=token,
        org_id=str(org),
        kind=kind,
        resource_id=str(rid),
        expected_version=3,
        base_sha256=digest,
    )
    _assert_capability_curl(patch_out, method="POST", token=token)
    assert f"/api/{collection}/{rid}/{leaf}/patch" in patch_out["endpoint"]
    assert f"base_sha256={digest}" in patch_out["endpoint"]
    assert patch_out["headers"]["Content-Type"] == "text/x-diff"


async def test_text_block_capability_rejects_note_part_kind() -> None:
    org, _user, token = await _signup()
    # Note parts have a two-id path and use the dedicated tools.
    with pytest.raises(ValueError):
        await mcp_server.get_text_block_capability(
            token=token, org_id=str(org), kind="note_part", resource_id=str(uuid.uuid4())
        )
