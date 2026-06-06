"""Regression: an attachment whose filename has non-latin-1 characters
(emoji, typographic apostrophe) must download without a 500. The ASGI
server latin-1 encodes header values, so the bare ``filename=`` could not
hold the raw name; Content-Disposition now carries an ASCII fallback plus a
percent-encoded ``filename*=UTF-8''`` (RFC 6266).
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app

# The exact name from the bug report: backpack emoji + smart apostrophe.
BUG_NAME = "\U0001f392 LISTA DELL’OCCORRENTE.pdf"  # noqa: RUF001


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_download_attachment_non_latin1_filename() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
        }
        tk = (await c.post("/tasks", headers=h, json={"title": "T"})).json()

        files = {"file": (BUG_NAME, b"%PDF-1.4 fake body", "application/pdf")}
        up = await c.post(f"/tasks/{tk['id']}/attachments", headers=h, files=files)
        assert up.status_code == 200
        aid = up.json()["id"]

        r = await c.get(f"/attachments/{aid}/download", headers=h)
        assert r.status_code == 200  # was 500 before the fix
        cd = r.headers["content-disposition"]
        cd.encode("latin-1")  # the ASGI server does this; must not raise
        assert "filename*=UTF-8''" in cd  # full UTF-8 name preserved
        assert r.content == b"%PDF-1.4 fake body"
