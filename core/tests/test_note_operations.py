"""Reading a note through the registry operation (ADR-0068): one answer
model for both surfaces, each with its own defaults, the MCP one lean.

The first of these failed on the first run of the migration: the model
lost its ``outline`` field in the move to core and the outline read came
back with no parts at all, a shape every structural check passed."""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import NoteKind
from mycelium_core.services import agent_tokens as at_svc
from mycelium_core.services import note_parts as parts_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.tools_notes import TOOLS


async def _note_with_parts() -> tuple[str, uuid.UUID, list[str]]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="NOTE-OPS",
        )
    bodies = ["first block\nsecond line", "## heading\n\nbody of part two"]
    async with tenant_session(str(a.org_id), str(a.user_id)) as s:
        n = await notes_svc.create_note(
            s, org_id=a.org_id, actor_id=a.user_id, kind=NoteKind.text, title="T", text=bodies[0]
        )
        await parts_svc.create_part(
            s, org_id=a.org_id, actor_id=a.user_id, note_id=n.id, body=bodies[1]
        )
        raw = (await at_svc.mint(s, org_id=a.org_id, actor_id=a.user_id, name="r")).raw
    return raw, n.id, bodies


async def test_the_outline_of_a_note_has_every_part_and_no_body() -> None:
    raw, note_id, bodies = await _note_with_parts()
    out = await TOOLS["get_note"](
        token=raw, org_id="", note_id=str(note_id), include_part_bodies=False
    )
    parts = out["parts"]
    assert [p["ord"] for p in parts] == [0, 1]
    assert [p["head"] for p in parts] == ["first block", "## heading"]
    assert [p["bytes"] for p in parts] == [len(b.encode()) for b in bodies]
    assert all("body" not in p and p["body_sha256"] for p in parts)
    assert "outline" not in out and "transcript" not in out


async def test_the_full_read_carries_each_body_once() -> None:
    raw, note_id, bodies = await _note_with_parts()
    out = await TOOLS["get_note"](token=raw, org_id="", note_id=str(note_id))
    assert [p["body"] for p in out["parts"]] == bodies
    # The joined body would be the note a second time: off by default here.
    assert "transcript" not in out
    # A part does not repeat the id of the note it is in.
    assert all("note_id" not in p for p in out["parts"])
    both = await TOOLS["get_note"](
        token=raw, org_id="", note_id=str(note_id), include_transcript=True
    )
    assert both["transcript"] == "\n\n".join(bodies)


async def test_the_parts_list_defaults_to_the_outline_and_gives_bodies_on_request() -> None:
    raw, note_id, bodies = await _note_with_parts()
    outline = await TOOLS["list_note_parts"](token=raw, org_id="", note_id=str(note_id))
    assert all("body" not in p and "head" in p for p in outline)
    full = await TOOLS["list_note_parts"](
        token=raw, org_id="", note_id=str(note_id), include_body=True
    )
    assert [p["body"] for p in full] == bodies
