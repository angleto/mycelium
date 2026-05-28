"""/export/pdf: WeasyPrint renders markdown editor HTML to a vector
PDF. The endpoint must be authenticated, must return a non-trivial
``application/pdf``, and the bytes must start with the PDF signature
so we know we got an actual document (not an HTML error page)."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_export_pdf_returns_real_pdf() -> None:
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

        body_html = (
            "<h1>Hello</h1>"
            "<p>Body with <strong>bold</strong> and a "
            '<a href="https://example.com">link</a>.</p>'
            "<ul><li>one</li><li>two</li></ul>"
            "<pre><code>print('x')</code></pre>"
        )
        r = await c.post(
            "/export/pdf",
            headers=h,
            json={"title": "My export / draft 1", "html": body_html},
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/pdf"
        assert "attachment" in r.headers["content-disposition"]
        # Forbidden char slugified to '-', not silently dropped.
        assert 'filename="My export - draft 1.pdf"' in r.headers["content-disposition"]
        body = r.content
        # %PDF-1.x signature; rules out an HTML error page being
        # returned with the wrong content-type.
        assert body[:5] == b"%PDF-", body[:64]
        # WeasyPrint output for a heading + paragraph + list weighs in
        # at several KB; under 1 KB would mean we serialised an empty
        # document.
        assert len(body) > 1024


async def test_export_pdf_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/export/pdf", json={"title": "x", "html": "<p>x</p>"})
        assert r.status_code == 401


async def test_export_pdf_rejects_huge_payload() -> None:
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
        # 9 MiB > the 8 MiB cap.
        huge = "<p>" + ("a" * (9 * 1024 * 1024)) + "</p>"
        r = await c.post("/export/pdf", headers=h, json={"title": "huge", "html": huge})
        assert r.status_code == 413
