"""The agent's upload path end to end: MCP mints, REST receives.

``upload_attachment_capability`` mints one single-use ``attachment:write``
token per file; each curl it returns is a multipart POST to the task's
attachments route, authenticated by that token alone (no PAT, no
X-Workspace-Id). This drives the same POST through the ASGI app with the
minted header, so what is checked is the pair as an agent meets it: the
upload lands, the response carries the ``markdown_ref`` to paste, a token
authenticates exactly one upload, and a token minted for one task does not
open another.

Origin: 2026-11-22, an agent attached four PDFs this way and then had to
build ``[name](/attachments/<id>/download)`` by hand for each, because only
the base64 MCP tool returned the reference. Observed failing (2026-11-22)
against the REST schema without ``markdown_ref``: KeyError on the upload
response.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlsplit

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_mcp import server as mcp_server

_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


async def _signup(c: AsyncClient) -> tuple[dict[str, str], str, str]:
    email = f"{uuid.uuid4().hex[:10]}@example.test"
    a = (await c.post("/auth/signup", json={"email": email, "password": "pw-strong-123"})).json()
    h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    return h, a["workspace_id"], a["token"]


def _route(upload: dict[str, Any]) -> str:
    """The public endpoint is the SPA origin + ``/api``; the ASGI app is
    mounted without that prefix (the proxy strips it)."""
    path = urlsplit(upload["endpoint"]).path
    assert path.startswith("/api/")
    return path.removeprefix("/api")


async def test_minted_uploads_land_and_answer_with_a_markdown_ref() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h, ws, token = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "T"})).json()["id"]
        out = await mcp_server.upload_attachment_capability(
            token=token,
            org_id=ws,
            parent_kind="task",
            parent_id=tid,
            files=["/home/me/q/fattura giugno.pdf", "/home/me/q/fattura luglio.pdf"],
        )
        refs = []
        for up, name in zip(
            out["uploads"], ["fattura giugno.pdf", "fattura luglio.pdf"], strict=True
        ):
            r = await c.post(
                _route(up),
                headers=up["headers"],
                files={"file": (name, _PDF, "application/pdf")},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["task_id"] == tid
            assert body["size_bytes"] == len(_PDF)
            assert body["markdown_ref"] == f"[{name}](/attachments/{body['id']}/download)"
            refs.append(body["markdown_ref"])

        # The list carries the same reference, so it is not a property of the
        # upload response alone.
        listed = (await c.get(f"/tasks/{tid}/attachments", headers=h)).json()
        assert sorted(a["markdown_ref"] for a in listed) == sorted(refs)


async def test_a_minted_token_authenticates_one_upload_only() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h, ws, token = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "T"})).json()["id"]
        out = await mcp_server.upload_attachment_capability(
            token=token, org_id=ws, parent_kind="task", parent_id=tid
        )
        up = out["uploads"][0]
        first = await c.post(
            _route(up), headers=up["headers"], files={"file": ("a.pdf", _PDF, "application/pdf")}
        )
        assert first.status_code == 200, first.text
        again = await c.post(
            _route(up), headers=up["headers"], files={"file": ("b.pdf", _PDF, "application/pdf")}
        )
        assert again.status_code == 401, again.text
        assert len((await c.get(f"/tasks/{tid}/attachments", headers=h)).json()) == 1


async def test_a_token_minted_for_one_task_does_not_open_another() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h, ws, token = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "T"})).json()["id"]
        other = (await c.post("/tasks", headers=h, json={"title": "U"})).json()["id"]
        out = await mcp_server.upload_attachment_capability(
            token=token, org_id=ws, parent_kind="task", parent_id=tid
        )
        r = await c.post(
            f"/tasks/{other}/attachments",
            headers=out["uploads"][0]["headers"],
            files={"file": ("a.pdf", _PDF, "application/pdf")},
        )
        assert r.status_code in (401, 403, 404), r.text
        assert (await c.get(f"/tasks/{other}/attachments", headers=h)).json() == []
