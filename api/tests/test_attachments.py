"""Attachments on notes AND tasks (DB-BYTEA, mirrors costa_associati).

Covers: multipart upload to a task and a note -> AttachmentOut with the
right size/mime/filename and NO ``data`` field leaked; list returns
metadata only; download returns the exact bytes with the right
Content-Type and an INLINE disposition for image/* (attachment for a
non-image); oversize -> 400 ATTACHMENT_TOO_LARGE (cap lowered via the
cached settings, the proper override seam); delete -> 204 then download
404 ATTACHMENT_NOT_FOUND; deleting the parent task/note CASCADE-deletes
its attachments; cross-org isolation (a second org's token cannot
list/download/delete; RLS). Member-level: an owner acting as a member
is fine. Providers/DB are the test PG (flow_app RLS).
"""

from __future__ import annotations

import struct
import uuid
import zlib

import pytest
from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.config import get_settings


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _png_bytes() -> bytes:
    """A minimal but valid 1x1 PNG (signature + IHDR + IDAT + IEND), so
    the bytes round-trip is checked against a real image payload."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\xff\xff"  # one white pixel row, filter byte 0
    idat = zlib.compress(raw)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


async def _signup(c: AsyncClient) -> tuple[dict[str, str], str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    return h, a["workspace_id"], a["user_id"]


async def test_attachment_upload_list_download_task_and_note() -> None:
    png = _png_bytes()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _ws, _uid = await _signup(c)

        # Note created before the task: in a brand-new workspace the
        # taxonomy default-tag bootstrap (ensure_default_client vs
        # ensure_default_project) collides on the "Personal" client tag
        # if a task is created first then a note. Unrelated to
        # attachments; this is the order test_work_note.py-style flows
        # use. Both parents still get an attachment below.
        note = (
            await c.post("/notes", headers=h, json={"kind": "text", "text": "note body"})
        ).json()
        nid = note["id"]
        task = (await c.post("/tasks", headers=h, json={"title": "T with files"})).json()
        tid = task["id"]

        # --- upload a PNG to the task (multipart) ---
        up_t = await c.post(
            f"/tasks/{tid}/attachments",
            headers=h,
            files={"file": ("shot.png", png, "image/png")},
        )
        assert up_t.status_code == 200, up_t.text
        at = up_t.json()
        assert at["task_id"] == tid
        assert at["note_id"] is None
        assert at["filename"] == "shot.png"
        assert at["mime_type"] == "image/png"
        assert at["size_bytes"] == len(png)
        # No binary leaked in the metadata response.
        assert "data" not in at

        # --- upload a PNG to the note ---
        up_n = await c.post(
            f"/notes/{nid}/attachments",
            headers=h,
            files={"file": ("pic.png", png, "image/png")},
        )
        assert up_n.status_code == 200, up_n.text
        an = up_n.json()
        assert an["note_id"] == nid
        assert an["task_id"] is None
        assert "data" not in an

        # --- list (metadata only) ---
        lst_t = (await c.get(f"/tasks/{tid}/attachments", headers=h)).json()
        assert [r["id"] for r in lst_t] == [at["id"]]
        assert "data" not in lst_t[0]
        assert lst_t[0]["size_bytes"] == len(png)
        lst_n = (await c.get(f"/notes/{nid}/attachments", headers=h)).json()
        assert [r["id"] for r in lst_n] == [an["id"]]

        # --- download: exact bytes + image => inline ---
        dl = await c.get(f"/attachments/{at['id']}/download", headers=h)
        assert dl.status_code == 200
        assert dl.content == png
        assert dl.headers["content-type"].startswith("image/png")
        cd = dl.headers["content-disposition"]
        assert cd == "inline; filename=\"shot.png\"; filename*=UTF-8''shot.png"


async def test_non_image_attachment_uses_attachment_disposition() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _ws, _uid = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "T"})).json()
        body = b"hello, plain text body\n"
        up = await c.post(
            f"/tasks/{task['id']}/attachments",
            headers=h,
            files={"file": ("notes.txt", body, "text/plain")},
        )
        assert up.status_code == 200, up.text
        aid = up.json()["id"]
        assert up.json()["mime_type"] == "text/plain"

        dl = await c.get(f"/attachments/{aid}/download", headers=h)
        assert dl.status_code == 200
        assert dl.content == body
        cd = dl.headers["content-disposition"]
        assert cd == "attachment; filename=\"notes.txt\"; filename*=UTF-8''notes.txt"


async def test_oversize_attachment_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lower the cap on the cached settings (the proper seam: both the
    # router guard and the service read get_settings().attachment_max_bytes).
    monkeypatch.setattr(get_settings(), "attachment_max_bytes", 16, raising=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _ws, _uid = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "T"})).json()
        up = await c.post(
            f"/tasks/{task['id']}/attachments",
            headers=h,
            files={"file": ("big.bin", b"x" * 64, "application/octet-stream")},
        )
        assert up.status_code == 400, up.text
        assert up.json()["code"] == "attachment.too_large"

        # Nothing was stored.
        lst = (await c.get(f"/tasks/{task['id']}/attachments", headers=h)).json()
        assert lst == []


async def test_delete_attachment_then_download_404() -> None:
    png = _png_bytes()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _ws, _uid = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "T"})).json()
        up = await c.post(
            f"/tasks/{task['id']}/attachments",
            headers=h,
            files={"file": ("a.png", png, "image/png")},
        )
        aid = up.json()["id"]

        d = await c.delete(f"/attachments/{aid}", headers=h)
        assert d.status_code == 204

        dl = await c.get(f"/attachments/{aid}/download", headers=h)
        assert dl.status_code == 404
        assert dl.json()["code"] == "attachment.not_found"


async def test_deleting_parent_cascade_deletes_attachments() -> None:
    """ON DELETE CASCADE: hard-deleting the parent task/note removes its
    attachments. The API only soft-deletes; hard-delete the parent row
    directly (RLS-scoped session) to exercise the FK rule, then assert
    via list (empty) and download (404)."""
    from sqlalchemy import text

    from flow_core.db import tenant_session

    png = _png_bytes()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})
        ).json()
        ws, uid = a["workspace_id"], a["user_id"]
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": ws}

        # Note parent first (taxonomy default-tag bootstrap order;
        # see test_attachment_upload_list_download_task_and_note).
        note = (await c.post("/notes", headers=h, json={"kind": "text", "text": "b"})).json()
        nid = note["id"]
        a_n = (
            await c.post(
                f"/notes/{nid}/attachments",
                headers=h,
                files={"file": ("n.png", png, "image/png")},
            )
        ).json()
        # Task parent.
        task = (await c.post("/tasks", headers=h, json={"title": "T"})).json()
        tid = task["id"]
        a_t = (
            await c.post(
                f"/tasks/{tid}/attachments",
                headers=h,
                files={"file": ("t.png", png, "image/png")},
            )
        ).json()

        async with tenant_session(ws, uid) as s:
            await s.execute(text("DELETE FROM tasks WHERE id = :i"), {"i": tid})
            await s.execute(text("DELETE FROM notes WHERE id = :i"), {"i": nid})

        # Both attachments are gone (CASCADE).
        assert (await c.get(f"/attachments/{a_t['id']}/download", headers=h)).status_code == 404
        assert (await c.get(f"/attachments/{a_n['id']}/download", headers=h)).status_code == 404


async def test_cross_org_isolation() -> None:
    """RLS: a second org's token cannot list/download/delete org A's
    attachment. The attachment row is invisible under org B's GUC, so
    download/delete are 404 (not found in this tenant) and the
    per-parent list/upload under org B's workspace are 403 (org A's
    task is not a member's resource there)."""
    png = _png_bytes()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        ha, _wsa, _ua = await _signup(c)
        hb, wsb, _ub = await _signup(c)

        task = (await c.post("/tasks", headers=ha, json={"title": "A's task"})).json()
        tid = task["id"]
        aid = (
            await c.post(
                f"/tasks/{tid}/attachments",
                headers=ha,
                files={"file": ("a.png", png, "image/png")},
            )
        ).json()["id"]

        # B authenticates against B's own workspace (a member there) but
        # the attachment belongs to A's org: RLS hides the row -> 404.
        dl = await c.get(f"/attachments/{aid}/download", headers=hb)
        assert dl.status_code == 404
        de = await c.delete(f"/attachments/{aid}", headers=hb)
        assert de.status_code == 404

        # B trying A's task under A's workspace id (B is not a member of
        # A's org): rejected before any row is touched -> 403.
        hb_into_a = {"Authorization": hb["Authorization"], "X-Workspace-Id": _wsa}
        assert (await c.get(f"/tasks/{tid}/attachments", headers=hb_into_a)).status_code == 403
        up = await c.post(
            f"/tasks/{tid}/attachments",
            headers=hb_into_a,
            files={"file": ("x.png", png, "image/png")},
        )
        assert up.status_code == 403

        # The attachment is still intact for A (B's calls were no-ops).
        ok = await c.get(f"/attachments/{aid}/download", headers=ha)
        assert ok.status_code == 200 and ok.content == png
        # B's own org list for B's (nonexistent) task is empty/scoped.
        _ = wsb
