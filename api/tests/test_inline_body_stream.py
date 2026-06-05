"""Token-free inline-body writes: stream a markdown body straight into a
note part / annotation TEXT column over the HTTP body (``--data-binary``)
instead of a tool argument.

Full-stack coverage (router -> service -> DB, under RLS) of the streaming
endpoints that back the MCP ``*_instructions`` recipes:

- create a note part and full-replace a part body by streaming (with
  optimistic-concurrency stale -> 409);
- comment / suggestion / edit-body on annotations by streaming, with the
  bounded anchor fields riding the query string;
- an oversize body is rejected before it is persisted, an empty comment
  body is refused, and an agent token attributes the comment to its
  AI-assistant identity badge (same as the MCP-tool path), not the human.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from tests_helpers import seed_ai_assistant_identity

from flow_api.main import app
from flow_core.config import get_settings
from flow_core.db import tenant_session
from flow_core.services import agent_tokens


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _note_with_part(c: AsyncClient, h: dict[str, str], body: str) -> tuple[str, str, int]:
    """Create a text note and return ``(note_id, part0_id, part0_version)``."""
    note = (await c.post("/notes", headers=h, json={"kind": "text", "text": body})).json()
    full = (await c.get(f"/notes/{note['id']}", headers=h)).json()
    p0 = full["parts"][0]
    return note["id"], p0["id"], p0["version"]


# --------------------------------------------------------------------------
# note parts
# --------------------------------------------------------------------------
async def test_note_part_create_stream() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = (await c.post("/notes", headers=h, json={"kind": "text", "text": "seed"})).json()[
            "id"
        ]
        body = "# Streamed\n\nLots of *markdown* the LLM never re-echoes."
        r = await c.post(
            f"/notes/{nid}/parts/stream",
            headers=h,
            params={"title": "Streamed", "lang": "en"},
            content=body.encode(),
        )
        assert r.status_code == 201, r.text
        part = r.json()
        assert part["title"] == "Streamed"
        assert part["lang"] == "en"
        assert part["body"] == body
        full = (await c.get(f"/notes/{nid}", headers=h)).json()
        assert any(p["body"] == body for p in full["parts"])


async def test_note_part_body_replace_stream_and_stale() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid, pid, v = await _note_with_part(c, h, "original body")
        r = await c.put(
            f"/notes/{nid}/parts/{pid}/body/stream",
            headers=h,
            params={"expected_version": v},
            content=b"replaced body",
        )
        assert r.status_code == 200, r.text
        assert r.json()["version"] == v + 1
        full = (await c.get(f"/notes/{nid}", headers=h)).json()
        part0 = next(p for p in full["parts"] if p["id"] == pid)
        assert part0["body"] == "replaced body"
        # the stale cursor is rejected (no last-write-wins)
        r2 = await c.put(
            f"/notes/{nid}/parts/{pid}/body/stream",
            headers=h,
            params={"expected_version": v},
            content=b"again",
        )
        assert r2.status_code == 409, r2.text


async def test_note_part_stream_oversize_rejected() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = (await c.post("/notes", headers=h, json={"kind": "text", "text": "x"})).json()["id"]
        big = b"a" * (get_settings().note_body_max_bytes + 1)
        r = await c.post(f"/notes/{nid}/parts/stream", headers=h, content=big)
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "body.limit_exceeded"


async def test_note_part_stream_invalid_utf8_rejected() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = (await c.post("/notes", headers=h, json={"kind": "text", "text": "x"})).json()["id"]
        # raw bytes that are not valid UTF-8 (a lone 0xff / 0xfe BOM-ish pair)
        r = await c.post(f"/notes/{nid}/parts/stream", headers=h, content=b"\xff\xfe\x00bad")
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "body.invalid_encoding"


# --------------------------------------------------------------------------
# annotations
# --------------------------------------------------------------------------
async def test_comment_stream_on_note_part() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        _nid, pid, _v = await _note_with_part(c, h, "The quick brown fox jumps.")
        body = "This passage is verbose; consider trimming it. " * 4
        r = await c.post(
            "/annotations/comment/stream",
            headers=h,
            params={"doc_kind": "note_part", "doc_id": pid, "anchor_quote": "brown fox"},
            content=body.encode(),
        )
        assert r.status_code == 200, r.text
        a = r.json()
        assert a["kind"] == "comment"
        assert a["body"] == body
        assert a["anchor_quote"] == "brown fox"
        assert a["doc_id"] == pid
        assert a["author_identity_id"] is not None  # human author via Identity


async def test_comment_stream_empty_body_rejected() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        _nid, pid, _v = await _note_with_part(c, h, "body")
        r = await c.post(
            "/annotations/comment/stream",
            headers=h,
            params={"doc_kind": "note_part", "doc_id": pid},
            content=b"",
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "annotation.body_required"


async def test_suggestion_stream_proposes_streamed_replacement() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        _nid, pid, _v = await _note_with_part(c, h, "The quick brown fox jumps.")
        proposed = "swift auburn fox"
        r = await c.post(
            "/annotations/suggestion/stream",
            headers=h,
            params={
                "doc_kind": "note_part",
                "doc_id": pid,
                "original_text": "quick brown fox",
                "rationale": "tighter",
            },
            content=proposed.encode(),
        )
        assert r.status_code == 200, r.text
        a = r.json()
        assert a["kind"] == "suggestion"
        assert a["original_text"] == "quick brown fox"
        assert a["proposed_text"] == proposed
        assert a["anchor_quote"] == "quick brown fox"  # the struck text is the anchor
        assert a["body"] == "tighter"  # rationale is stored in body


async def test_edit_annotation_body_stream() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        _nid, pid, _v = await _note_with_part(c, h, "body")
        a = (
            await c.post(
                "/annotations/comment",
                headers=h,
                json={"doc_kind": "note_part", "doc_id": pid, "body": "orig"},
            )
        ).json()
        aid, ver = a["id"], a["version"]
        r = await c.patch(
            f"/annotations/{aid}/body/stream",
            headers=h,
            params={"expected_version": ver},
            content=b"edited via stream",
        )
        assert r.status_code == 200, r.text
        assert r.json()["version"] == ver + 1
        got = (await c.get(f"/annotations/{aid}", headers=h)).json()
        assert got["body"] == "edited via stream"
        assert got["edited_at"] is not None


async def test_comment_stream_attributes_ai_assistant_identity() -> None:
    """An agent token streams the comment with its AI-assistant identity
    badge (same attribution as the MCP ``add_annotation`` tool); a human
    session bearer is attributed to a different identity."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        su = (
            await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})
        ).json()
        org = uuid.UUID(su["workspace_id"])
        user = uuid.UUID(su["user_id"])
        h = {"Authorization": f"Bearer {su['token']}", "X-Workspace-Id": su["workspace_id"]}
        _nid, pid, _v = await _note_with_part(c, h, "Reviewable body.")

        async with tenant_session(str(org), str(user)) as s:
            ai_ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        async with tenant_session(str(org), str(user)) as s:
            minted = await agent_tokens.mint(
                s,
                org_id=org,
                actor_id=user,
                name="copilot",
                assistant_id=ai_ident.ai_assistant_id,
            )
            raw = minted.raw

        ah = {"Authorization": f"Bearer {raw}", "X-Workspace-Id": su["workspace_id"]}
        ai = (
            await c.post(
                "/annotations/comment/stream",
                headers=ah,
                params={"doc_kind": "note_part", "doc_id": pid},
                content=b"reviewed by the assistant",
            )
        ).json()
        assert ai["author_identity_id"] == str(ai_ident.id)  # AI badge, not the human

        human = (
            await c.post(
                "/annotations/comment/stream",
                headers=h,
                params={"doc_kind": "note_part", "doc_id": pid},
                content=b"a human note",
            )
        ).json()
        assert human["author_identity_id"] is not None
        assert human["author_identity_id"] != str(ai_ident.id)
