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
