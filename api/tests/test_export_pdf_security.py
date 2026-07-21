"""POST /export/pdf is fenced against LFI / SSRF (task c19f2f63).

The document HTML is caller-supplied and rendered by WeasyPrint, which by
default resolves ``file:///...`` (reading backend-pod files into the PDF) and
``http(s)://`` (SSRF). ``_safe_url_fetcher`` allows only inline ``data:`` and
``file://`` under the bundled static dir; everything else is refused, and
WeasyPrint then simply skips the resource.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_api.routers.export import _STATIC, _safe_url_fetcher


def test_url_fetcher_refuses_local_files_and_network() -> None:
    # LFI: an absolute file:// outside the static dir is refused.
    for bad in (
        "file:///etc/passwd",
        "file:///app/secrets/token",
        f"file://{_STATIC.parent}/config.py",  # a sibling, still outside static
        "http://169.254.169.254/latest/meta-data/",  # SSRF (cloud metadata)
        "https://evil.example/x",
        "ftp://host/x",
    ):
        with pytest.raises(ValueError):
            _safe_url_fetcher(bad)


def test_url_fetcher_allows_data_and_bundled_static() -> None:
    # Inline images (the SPA inlines attachments as data:) are fine.
    assert _safe_url_fetcher("data:text/plain;base64,aGk=")
    # print.css's own bundled assets under the static dir are fine.
    assert _safe_url_fetcher((_STATIC / "print.css").as_uri())


async def _owner_headers(c: AsyncClient) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": "EXP",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {su['token']}", "X-Workspace-Id": su["workspace_id"]}


async def test_export_renders_and_a_hostile_file_url_does_not_crash() -> None:
    """A benign export succeeds; an export whose HTML tries to pull
    ``file:///etc/passwd`` still returns a PDF (the fetcher blocked the file, so
    WeasyPrint skipped it) rather than 500ing or leaking the file."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner_headers(c)

        ok = await c.post("/export/pdf", headers=h, json={"title": "Doc", "html": "<p>hello</p>"})
        assert ok.status_code == 200, ok.text
        assert ok.headers["content-type"] == "application/pdf"
        assert ok.content[:5] == b"%PDF-"

        hostile = await c.post(
            "/export/pdf",
            headers=h,
            json={"title": "x", "html": '<p>hi</p><img src="file:///etc/passwd"/>'},
        )
        assert hostile.status_code == 200, hostile.text
        assert hostile.content[:5] == b"%PDF-"
        assert b"root:" not in hostile.content  # /etc/passwd content did not leak in
