"""Perf: the note LIST endpoint ships a bounded one-line ``preview``, never
the body; the DETAIL endpoint still returns the full ``transcript``.

A note body is unbounded, so carrying it per row made ``GET /notes`` cost
O(total content of the org) in bytes rather than O(rows shown). Free-text
matching over bodies stays available server-side through ``q``.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.services.notes import _PREVIEW_MAX_CHARS


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123"},
        )
    ).json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }


async def test_list_ships_preview_not_body_detail_keeps_body() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        body = "prima riga\n\nseconda riga con altro testo"
        n = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "T", "text": body},
            )
        ).json()
        nid = n["id"]

        # LIST: no body at all, only the first non-empty line.
        row = next(x for x in (await c.get("/notes", headers=h)).json() if x["id"] == nid)
        assert "transcript" not in row
        assert row["preview"] == "prima riga"

        # DETAIL: the full body is returned.
        detail = (await c.get(f"/notes/{nid}", headers=h)).json()
        assert detail["transcript"] == body


async def test_preview_skips_leading_blank_parts_and_is_capped() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # Leading blank lines must not swallow the preview: the SPA used
        # to find the first NON-EMPTY line by scanning the whole body,
        # and the server-side preview has to keep that semantics.
        long_line = "x" * (_PREVIEW_MAX_CHARS + 200)
        n = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "text": f"\n   \n{long_line}"},
            )
        ).json()

        row = next(x for x in (await c.get("/notes", headers=h)).json() if x["id"] == n["id"])
        preview = row["preview"]
        assert preview is not None
        assert preview.startswith("xxx")
        # Bounded: this is the whole point of the field.
        assert len(preview) == _PREVIEW_MAX_CHARS


async def test_empty_note_has_null_preview() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        n = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "solo titolo"},
            )
        ).json()

        row = next(x for x in (await c.get("/notes", headers=h)).json() if x["id"] == n["id"])
        # null-vs-empty stays distinguishable, like the body path did.
        assert row["preview"] is None


async def test_body_match_still_reachable_server_side_via_q() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # The term lives deep in the body, past any preview window, so a
        # client filtering on ``preview`` could never find it. ``q`` is
        # what replaces the client-side scan over shipped bodies.
        deep = "y" * (_PREVIEW_MAX_CHARS + 500) + "\nzarabaz"
        n = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "T", "text": deep},
            )
        ).json()

        hits = (await c.get("/notes", headers=h, params={"q": "zarabaz"})).json()
        assert [x["id"] for x in hits] == [n["id"]]
