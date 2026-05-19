"""Attachment storage backend: with the s3 backend selected (bytes
off-DB, ``data`` NULL + ``storage_key`` set) the HTTP contract is
byte-identical to the default pg path.

The s3 store is the in-memory ``FakeAttachmentStore`` injected via
``set_attachment_store_override`` (same seam as the LLM/embedder
fakes); no boto3, no network. The default pg path is covered byte-for-
byte by the unchanged ``test_attachments.py``; this only adds the s3
behaviour so the existing API/SPA/E2E stay untouched."""

from __future__ import annotations

import struct
import uuid
import zlib
from collections.abc import Iterator

import pytest
from _fake_attachment_store import FakeAttachmentStore
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from flow_api.main import app
from flow_core.attachment_store import set_attachment_store_override
from flow_core.db import tenant_session


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _png_bytes() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xff\xff")
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


@pytest.fixture
def _s3_backend() -> Iterator[FakeAttachmentStore]:
    """Back the store with the Fake via the override ONLY. The override
    short-circuits ``get_attachment_store`` before settings are read, so
    the global lru_cached ``Settings`` singleton is NOT mutated (doing
    so leaked the s3 backend into other tests, e.g. test_attachments.py
    rows ending up S3-backed). Restored after the test."""
    fake = FakeAttachmentStore()
    set_attachment_store_override(lambda: fake)
    try:
        yield fake
    finally:
        set_attachment_store_override(None)


async def _signup(c: AsyncClient) -> tuple[dict[str, str], str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    return h, a["workspace_id"], a["user_id"]


async def test_s3_backend_upload_download_byte_identical(
    _s3_backend: FakeAttachmentStore,
) -> None:
    png = _png_bytes()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, ws, uid = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "T s3"})).json()
        tid = task["id"]

        up = await c.post(
            f"/tasks/{tid}/attachments",
            headers=h,
            files={"file": ("shot.png", png, "image/png")},
        )
        assert up.status_code == 200, up.text
        at = up.json()
        # Wire shape is unchanged: still no binary / storage_key leaked.
        assert at["filename"] == "shot.png"
        assert at["mime_type"] == "image/png"
        assert at["size_bytes"] == len(png)
        assert "data" not in at
        assert "storage_key" not in at
        aid = at["id"]

        # The bytes went to the object store, NOT the row.
        assert await _s3_backend.get(aid) == png
        async with tenant_session(ws, uid) as s:
            row = (
                await s.execute(
                    text("SELECT data, storage_key FROM attachments WHERE id = :i"),
                    {"i": aid},
                )
            ).one()
            assert row.data is None
            assert row.storage_key == aid

        # Download is byte-identical (same bytes, type, disposition).
        dl = await c.get(f"/attachments/{aid}/download", headers=h)
        assert dl.status_code == 200
        assert dl.content == png
        assert dl.headers["content-type"].startswith("image/png")
        assert dl.headers["content-disposition"] == 'inline; filename="shot.png"'

        # Self-cleanup: an S3-backed row left in the shared test DB has
        # data NULL, which (by design) would block the migration
        # downgrade gate. Delete it (also drops the Fake object).
        assert (await c.delete(f"/attachments/{aid}", headers=h)).status_code == 204


async def test_s3_backend_delete_removes_object_and_row(
    _s3_backend: FakeAttachmentStore,
) -> None:
    body = b"plain s3 body\n"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _ws, _uid = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "T"})).json()
        up = await c.post(
            f"/tasks/{task['id']}/attachments",
            headers=h,
            files={"file": ("a.txt", body, "text/plain")},
        )
        aid = up.json()["id"]
        assert await _s3_backend.get(aid) == body

        d = await c.delete(f"/attachments/{aid}", headers=h)
        assert d.status_code == 204
        # Object gone from the store AND the row gone (download 404).
        assert aid not in _s3_backend.objects
        dl = await c.get(f"/attachments/{aid}/download", headers=h)
        assert dl.status_code == 404
        assert dl.json()["code"] == "attachment.not_found"
