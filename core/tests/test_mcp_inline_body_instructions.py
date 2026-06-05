"""MCP ``*_instructions`` tools: token-free inline-body write recipes.

Each tool returns a ready-to-run ``curl`` that streams a local markdown
file as the raw request body into a TEXT column (a note part / annotation
body), mirroring ``upload_attachment_instructions`` but with NO S3 and no
markdown-ref (the body IS the content, not a downloadable blob). The
recipe is pure string-building after an org/token fail-fast, so synthetic
target ids are enough to assert the URL / curl shape and that the secret
token is never echoed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from flow_core.config import get_settings
from flow_core.db import admin_session
from flow_core.services.auth import signup
from flow_mcp import server as mcp_server


async def _signup() -> tuple[uuid.UUID, uuid.UUID, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCPINLINE",
        )
    assert r.token is not None
    return r.org_id, r.user_id, r.token


def _assert_recipe(
    out: dict[str, Any],
    *,
    method: str,
    org: uuid.UUID,
    token: str,
    path_fragment: str,
) -> None:
    base = get_settings().frontend_base_url.rstrip("/")
    assert out["endpoint"].startswith(f"{base}/api/")
    assert path_fragment in out["endpoint"]
    assert out["method"] == method
    assert f"curl -fsS -X {method} '" in out["curl"]
    assert path_fragment in out["curl"]
    assert "--data-binary @<path-to-file>" in out["curl"]
    assert out["headers"]["Content-Type"].startswith("text/markdown")
    assert out["headers"]["X-Workspace-Id"] == str(org)
    assert out["max_bytes"] == get_settings().note_body_max_bytes
    # token never leaked; only the placeholder is handed back
    assert "$FLOW_TOKEN" in out["curl"]
    assert token not in out["curl"]
    assert token not in repr(out)
    # inline body: no S3, no markdown-ref (unlike the attachment recipe)
    assert "s3" not in out["endpoint"].lower()
    assert "amazonaws" not in out["curl"].lower()
    assert "markdown_ref_template" not in out


async def test_add_note_part_instructions() -> None:
    org, _user, token = await _signup()
    nid = uuid.uuid4()
    out = await mcp_server.add_note_part_instructions(
        token=token, org_id=str(org), note_id=str(nid), title="Intro", lang="en", ord=2
    )
    _assert_recipe(
        out,
        method="POST",
        org=org,
        token=token,
        path_fragment=f"/api/notes/{nid}/parts/stream",
    )
    assert "title=Intro" in out["endpoint"]
    assert "lang=en" in out["endpoint"]
    assert "ord=2" in out["endpoint"]


async def test_set_note_part_body_instructions() -> None:
    org, _user, token = await _signup()
    nid, pid = uuid.uuid4(), uuid.uuid4()
    out = await mcp_server.set_note_part_body_instructions(
        token=token, org_id=str(org), note_id=str(nid), part_id=str(pid), expected_version=3
    )
    _assert_recipe(
        out,
        method="PUT",
        org=org,
        token=token,
        path_fragment=f"/api/notes/{nid}/parts/{pid}/body/stream",
    )
    assert "expected_version=3" in out["endpoint"]


async def test_add_comment_instructions() -> None:
    org, _user, token = await _signup()
    did = uuid.uuid4()
    out = await mcp_server.add_comment_instructions(
        token=token,
        org_id=str(org),
        doc_kind="note_part",
        doc_id=str(did),
        anchor_quote="brown fox",
    )
    _assert_recipe(
        out,
        method="POST",
        org=org,
        token=token,
        path_fragment="/api/annotations/comment/stream",
    )
    assert "doc_kind=note_part" in out["endpoint"]
    assert f"doc_id={did}" in out["endpoint"]
    assert "anchor_quote=brown%20fox" in out["endpoint"]


async def test_add_comment_instructions_rejects_bad_doc_kind() -> None:
    org, _user, token = await _signup()
    with pytest.raises(ValueError, match="doc_kind"):
        await mcp_server.add_comment_instructions(
            token=token, org_id=str(org), doc_kind="bogus", doc_id=str(uuid.uuid4())
        )


async def test_propose_suggestion_instructions() -> None:
    org, _user, token = await _signup()
    did = uuid.uuid4()
    out = await mcp_server.propose_suggestion_instructions(
        token=token,
        org_id=str(org),
        doc_kind="task_description",
        doc_id=str(did),
        original_text="old text",
        rationale="why",
    )
    _assert_recipe(
        out,
        method="POST",
        org=org,
        token=token,
        path_fragment="/api/annotations/suggestion/stream",
    )
    assert "original_text=old%20text" in out["endpoint"]
    assert "rationale=why" in out["endpoint"]


async def test_edit_annotation_body_instructions() -> None:
    org, _user, token = await _signup()
    aid = uuid.uuid4()
    out = await mcp_server.edit_annotation_body_instructions(
        token=token, org_id=str(org), annotation_id=str(aid), expected_version=5
    )
    _assert_recipe(
        out,
        method="PATCH",
        org=org,
        token=token,
        path_fragment=f"/api/annotations/{aid}/body/stream",
    )
    assert "expected_version=5" in out["endpoint"]
