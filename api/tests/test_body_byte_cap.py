"""``note_body_max_bytes`` on the writers that take a WHOLE body.

The cap was believed to be enforced and was enforced only by the
incremental helpers -- append / prepend / replace / patch, all of which
check the body they would PRODUCE. Every writer that is handed a
complete body checked nothing: a part created or replaced, a note's flat
body, a task description. Four surfaces (REST, MCP, CLI, and the SPA
through REST) wrote an over-cap body and succeeded.

The sharpest case is the last test-but-one here: ``append_note_part``
exists to stream a large body past the per-call payload ceiling, and its
FIRST chunk created the part through the uncapped verb. Sending the
whole body as chunk one walked around the cap using the very tool built
to respect it.

Each test states which writer it stands for; between them they cover the
six call sites the predicate now sits on, plus the boundary that keeps
the comparison strict.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.errors import DomainError
from mycelium_core.services.auth import signup
from mycelium_mcp.server import append_note_part
from mycelium_mcp.server import create_note as mcp_create_note

_CODE = "body.limit_exceeded"


def _cap() -> int:
    """Read the cap rather than restate it: a hand-written constant here
    would keep passing after somebody retunes the setting."""
    return get_settings().note_body_max_bytes


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _note(c: AsyncClient, h: dict[str, str]) -> str:
    r = await c.post("/notes", headers=h, json={"kind": "text", "title": "cap"})
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


async def test_creating_a_part_over_the_cap_is_refused() -> None:
    """``note_parts.create_part``, reached through the REST route.

    Also the proof that the route inherits the core's cap instead of
    carrying one of its own: nothing in the API layer was changed, and
    ``NotePartCreateIn.body`` still has no ``max_length``."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _note(c, h)
        r = await c.post(f"/notes/{nid}/parts", headers=h, json={"body": "a" * (_cap() + 1)})
        assert r.status_code == 400, r.text
        assert r.json()["code"] == _CODE
        # Nothing was written: the refusal precedes the insert.
        assert (await c.get(f"/notes/{nid}/parts", headers=h)).json() == []


async def test_replacing_a_part_body_over_the_cap_is_refused() -> None:
    """``note_parts.update_part``, the verb an editor saves through."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _note(c, h)
        pid = (await c.post(f"/notes/{nid}/parts", headers=h, json={"body": "ok"})).json()["id"]
        r = await c.patch(
            f"/notes/{nid}/parts/{pid}",
            headers=h,
            json={"expected_version": 1, "body": "a" * (_cap() + 1)},
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == _CODE
        # The live body survived the refused write.
        assert (await c.get(f"/notes/{nid}/parts", headers=h)).json()[0]["body"] == "ok"


async def test_a_flat_note_body_over_the_cap_is_refused() -> None:
    """``notes.update_note``, which a cap on the part verbs alone does
    NOT cover.

    It does not call ``update_part``: it writes part zero through
    ``_upsert_part_zero``, so a cap mounted only on the part mutators
    would leave open exactly the door the setting's own comment named
    first."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _note(c, h)
        r = await c.patch(
            f"/notes/{nid}", headers=h, json={"expected_version": 1, "text": "a" * (_cap() + 1)}
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == _CODE


async def test_a_task_description_over_the_cap_is_refused() -> None:
    """``tasks.create_task``. The description shares the cap with note
    bodies and shared none of its enforcement."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.post(
            "/tasks", headers=h, json={"title": "t", "description": "a" * (_cap() + 1)}
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == _CODE


async def test_the_first_chunk_of_append_note_part_is_capped() -> None:
    """The hole inside the recommended path for large bodies.

    ``append_note_part`` chunks a body past the per-call payload ceiling.
    Chunk one CREATES the part (``create_part``) and every later chunk
    extends it (``append_to_part``) -- so the cap applied from the second
    chunk onward and not to the first. A caller sending everything as
    chunk one walked around the cap using the tool built to respect it."""
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="CapChunk")
    assert r.token is not None
    token, org = r.token, str(r.org_id)
    note = await mcp_create_note(token=token, org_id=org, kind="text", text="seed")

    with pytest.raises(DomainError) as exc:
        await append_note_part(
            token=token, org_id=org, note_id=note["id"], chunk="a" * (_cap() + 1)
        )
    assert exc.value.code.value == _CODE


async def test_a_body_exactly_at_the_cap_is_accepted() -> None:
    """The boundary, so the comparison stays ``>`` and never drifts to
    ``>=``. Every pre-existing call site used strict greater; a body of
    exactly ``note_body_max_bytes`` is conforming and must stay so."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _note(c, h)
        r = await c.post(f"/notes/{nid}/parts", headers=h, json={"body": "a" * _cap()})
        assert r.status_code == 200, r.text[:200]


async def test_the_cap_counts_bytes_and_not_characters() -> None:
    """Non-ASCII, which is the half a ``len(body)`` check gets wrong.

    A body of accented text is two bytes per character, so a string of
    ``cap`` CHARACTERS is roughly twice the cap in bytes. Refused, and
    refused for the right reason: this is the shape under which a
    character-counting cap would let through twice what the cap allows,
    and this workspace writes Italian."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = await _note(c, h)
        body = "à" * (_cap() // 2 + 1)
        assert len(body) <= _cap() < len(body.encode("utf-8"))
        r = await c.post(f"/notes/{nid}/parts", headers=h, json={"body": body})
        assert r.status_code == 400, r.text[:200]
        assert r.json()["code"] == _CODE
