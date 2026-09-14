"""The derived flat body is the caller's choice on the HTTP surface.

``NoteOut.transcript`` is the join of the part bodies, so a response that
also carries ``parts`` carries the note twice. The MCP twin simply
stopped emitting it -- nothing there reads it when the bodies are
present. This surface cannot: ``transcript`` seeds the editor, labels the
derive-task control and feeds the revisions panel, five TypeScript
consumers in all.

So the default does not move, and a machine client that already reads
``parts`` asks for one copy. The two tests below are a pair on purpose:
one holds the economy, the other holds the SPA, and a change that breaks
either is not the change this item wanted.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app

_BODY = "Titolo\n\nUn corpo che vale la pena non mandare due volte.\n"


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _note_with_body(c: AsyncClient, h: dict[str, str]) -> str:
    r = await c.post("/notes", headers=h, json={"kind": "text", "text": _BODY})
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


async def test_the_flat_body_is_there_by_default() -> None:
    """The half that protects the SPA. Dropping the field, or flipping the
    default, would blank the editor on every note."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _note_with_body(c, h)
        got = (await c.get(f"/notes/{nid}", headers=h)).json()
        assert got["transcript"] == _BODY
        assert [p["body"] for p in got["parts"]] == [_BODY]


async def test_a_client_that_reads_parts_can_ask_for_one_copy() -> None:
    """The half that is the point of the item: ``parts`` still complete,
    the derived duplicate gone."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _note_with_body(c, h)
        got = (await c.get(f"/notes/{nid}?include_transcript=false", headers=h)).json()
        assert got["transcript"] is None
        assert [p["body"] for p in got["parts"]] == [_BODY]
        # The body is in the payload once, not twice.
        assert str(got).count("non mandare due volte") == 1


async def test_merge_offers_the_same_choice() -> None:
    """The response most worth trimming: a merge returns a note that just
    grew by the whole of another one."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        target = await _note_with_body(c, h)
        source = await _note_with_body(c, h)
        merged = (
            await c.post(
                "/notes/merge?include_transcript=false",
                headers=h,
                json={"source_note_id": source, "target_note_id": target},
            )
        ).json()
        assert merged["transcript"] is None
        assert len(merged["parts"]) == 2
