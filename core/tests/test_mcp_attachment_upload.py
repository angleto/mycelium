"""MCP upload_attachment tool: base64 ingest + size guard + parent xor.

Service-level coverage; the MCP tool is a thin wrapper around
``services.attachments.add_attachment`` plus base64 decode + parent
xor validation.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.models.note import NoteKind
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup
from flow_mcp import server as mcp_server


async def _signup() -> tuple[uuid.UUID, uuid.UUID, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCPATT",
        )
    assert r.token is not None
    return r.org_id, r.user_id, r.token


async def test_upload_attachment_to_task() -> None:
    org, user, token = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="PDF host")
    payload = b"%PDF-1.4 test pdf bytes"
    data_b64 = base64.b64encode(payload).decode("ascii")
    out = await mcp_server.upload_attachment(
        token=token,
        org_id=str(org),
        filename="report.pdf",
        data_b64=data_b64,
        task_id=str(t.id),
    )
    assert out["task_id"] == str(t.id)
    assert out["note_id"] is None
    assert out["filename"] == "report.pdf"
    assert out["size_bytes"] == len(payload)
    assert "application/pdf" in (out["mime_type"] or "") or out["mime_type"]
    # Paste-ready link (non-image -> no leading bang), pointing at the
    # bearer-auth download route the SPA resolves through authFetch.
    assert out["markdown_ref"] == f"[report.pdf](/attachments/{out['id']}/download)"


async def test_upload_attachment_to_note() -> None:
    org, user, token = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        n = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="Note host"
        )
    out = await mcp_server.upload_attachment(
        token=token,
        org_id=str(org),
        filename="snippet.txt",
        data_b64=base64.b64encode(b"hello world").decode("ascii"),
        note_id=str(n.id),
    )
    assert out["note_id"] == str(n.id)
    assert out["task_id"] is None
    assert out["size_bytes"] == 11


async def test_upload_attachment_image_markdown_ref_is_embed() -> None:
    """An image attachment yields an inline-embed reference (leading
    bang), so it renders inline in the body rather than as a link."""
    org, user, token = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        n = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="Img host"
        )
    # 1x1 transparent PNG.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    out = await mcp_server.upload_attachment(
        token=token,
        org_id=str(org),
        filename="pixel.png",
        data_b64=base64.b64encode(png).decode("ascii"),
        note_id=str(n.id),
    )
    assert (out["mime_type"] or "").startswith("image/")
    assert out["markdown_ref"] == f"![pixel.png](/attachments/{out['id']}/download)"


async def test_upload_attachment_rejects_both_parents() -> None:
    org, _user, token = await _signup()
    with pytest.raises(ValueError, match="exactly one of"):
        await mcp_server.upload_attachment(
            token=token,
            org_id=str(org),
            filename="x.bin",
            data_b64=base64.b64encode(b"x").decode("ascii"),
            note_id=str(uuid.uuid4()),
            task_id=str(uuid.uuid4()),
        )


async def test_upload_attachment_rejects_neither_parent() -> None:
    org, _user, token = await _signup()
    with pytest.raises(ValueError, match="exactly one of"):
        await mcp_server.upload_attachment(
            token=token,
            org_id=str(org),
            filename="x.bin",
            data_b64=base64.b64encode(b"x").decode("ascii"),
        )


async def test_upload_attachment_rejects_bad_base64() -> None:
    org, user, token = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="X")
    with pytest.raises(ValueError, match="base64"):
        await mcp_server.upload_attachment(
            token=token,
            org_id=str(org),
            filename="x.bin",
            data_b64="not-valid-base64===",
            task_id=str(t.id),
        )


async def test_upload_attachment_instructions_returns_token_free_recipe() -> None:
    """The recipe tool returns a ready-to-run curl that streams the file
    through the BACKEND gateway (frontend origin + ``/api`` -> the API),
    never an S3 URL, and never echoes the secret token (a $FLOW_TOKEN
    placeholder stays in the recipe)."""
    from flow_core.config import get_settings

    org, user, token = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="MRI host")
    out = await mcp_server.upload_attachment_instructions(
        token=token,
        org_id=str(org),
        filename="brain.nii.gz",
        task_id=str(t.id),
        mime_type="application/gzip",
    )
    base = get_settings().frontend_base_url.rstrip("/")
    assert out["endpoint"] == (
        f"{base}/api/attachments/stream?filename=brain.nii.gz&task_id={t.id}"
    )
    assert out["method"] == "POST"
    # Goes through the backend gateway, never directly to object storage.
    assert "/api/attachments/stream" in out["curl"]
    assert "s3" not in out["endpoint"].lower()
    assert "amazonaws" not in out["curl"].lower()
    # The token is NOT leaked; only a placeholder is handed back.
    assert "$FLOW_TOKEN" in out["curl"]
    assert token not in out["curl"]
    assert token not in repr(out)
    assert out["headers"]["X-Workspace-Id"] == str(org)
    assert out["headers"]["Content-Type"] == "application/gzip"
    assert out["markdown_ref_template"] == "[brain.nii.gz](/attachments/<id>/download)"


async def test_upload_attachment_instructions_image_template_is_embed() -> None:
    org, user, token = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="Img host")
    out = await mcp_server.upload_attachment_instructions(
        token=token,
        org_id=str(org),
        filename="scan.png",
        task_id=str(t.id),
        mime_type="image/png",
    )
    assert out["markdown_ref_template"] == "![scan.png](/attachments/<id>/download)"


async def test_upload_attachment_instructions_rejects_both_parents() -> None:
    org, _user, token = await _signup()
    with pytest.raises(ValueError, match="exactly one of"):
        await mcp_server.upload_attachment_instructions(
            token=token,
            org_id=str(org),
            filename="x.bin",
            note_id=str(uuid.uuid4()),
            task_id=str(uuid.uuid4()),
        )


async def test_upload_attachment_instructions_rejects_neither_parent() -> None:
    org, _user, token = await _signup()
    with pytest.raises(ValueError, match="exactly one of"):
        await mcp_server.upload_attachment_instructions(
            token=token,
            org_id=str(org),
            filename="x.bin",
        )


async def test_upload_attachment_size_guard() -> None:
    """The service's ``attachment_max_bytes`` guard still fires for the
    decoded payload (same code path as the REST upload)."""
    from flow_core.config import get_settings

    org, user, token = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="Big")
    too_big = b"\x00" * (get_settings().attachment_max_bytes + 1)
    with pytest.raises(DomainError):
        await mcp_server.upload_attachment(
            token=token,
            org_id=str(org),
            filename="huge.bin",
            data_b64=base64.b64encode(too_big).decode("ascii"),
            task_id=str(t.id),
        )
