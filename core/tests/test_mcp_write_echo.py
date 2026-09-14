"""What a create returns over MCP, and what it costs.

A tool argument is the model's OUTPUT: the characters of a body are
billed on the way in. Returning that same body makes them input again in
the same turn, so a create used to charge twice for one write.
``add_note_part`` returned the part with its ``body``; ``create_note``
and ``create_task_note`` returned the derived ``transcript``, which is
the join of the very text just sent.

The model to copy was already in the same file: ``update_note_part``
returns ``{part_id, version}`` and ``update_note`` ``{note_id,
version}``. The update path has never echoed; only the create path did.

Two properties here, and the second is the one that keeps the first
honest: the create no longer returns the body, AND the body is still
retrievable, so this is economy and not data loss.
"""

from __future__ import annotations

import uuid

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.services import text_patch
from mycelium_core.services.auth import signup
from mycelium_mcp.server import add_note_part, create_note, create_task, create_task_note, get_note

_BODY = "## Sezione\n\nUn corpo che non deve tornare indietro.\n"


async def _tenant() -> tuple[str, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="WriteEcho",
        )
    assert r.token is not None
    return r.token, str(r.org_id)


async def test_add_note_part_does_not_hand_the_body_back() -> None:
    token, org = await _tenant()
    note = await create_note(token=token, org_id=org, kind="text", text="seed")
    part = await add_note_part(token=token, org_id=org, note_id=note["id"], body=_BODY)

    assert "body" not in part
    # What replaces it is not nothing: a byte-exact confirmation of what
    # landed, for about 25 tokens.
    assert part["body_sha256"] == text_patch.body_sha256(_BODY)
    assert part["bytes"] == len(_BODY.encode("utf-8"))


async def test_the_body_is_still_one_call_away() -> None:
    """The half that makes the economy legitimate. If the body were only
    cheap to write and hard to read back, this would be a regression
    wearing a saving's clothes."""
    token, org = await _tenant()
    note = await create_note(token=token, org_id=org, kind="text", text="seed")
    await add_note_part(token=token, org_id=org, note_id=note["id"], body=_BODY)

    full = await get_note(token=token, org_id=org, note_id=note["id"], include_part_bodies=True)
    assert _BODY in [p["body"] for p in full["parts"]]


async def test_create_note_returns_the_outline_and_no_transcript() -> None:
    """``transcript`` is derived by joining the part bodies, so emitting
    it is the same echo by another name."""
    token, org = await _tenant()
    note = await create_note(token=token, org_id=org, kind="text", text=_BODY)

    assert note.get("transcript") is None, note.get("transcript")
    assert note["parts"], note
    assert "body" not in note["parts"][0]
    assert note["parts"][0]["body_sha256"] == text_patch.body_sha256(_BODY)


async def test_create_task_note_returns_the_outline_too() -> None:
    token, org = await _tenant()
    task = await create_task(token=token, org_id=org, title="t")
    note = await create_task_note(token=token, org_id=org, task_id=task["id"], text=_BODY)

    assert note.get("transcript") is None
    assert "body" not in note["parts"][0]


async def test_a_large_body_is_accepted_and_told_where_it_should_have_gone() -> None:
    """A hint on a SUCCESSFUL write, never a refusal.

    The characters were billed as output before the server saw them, so
    refusing cannot save the call it fires on; and for content generated
    in context the economical path has to write those same bytes to a
    file first, so refusing would charge for them twice. What the hint
    can do is make the NEXT write cheaper, which is why it names a tool
    rather than a rule."""
    token, org = await _tenant()
    note = await create_note(token=token, org_id=org, kind="text", text="seed")
    big = "a" * (get_settings().tool_arg_body_hint_chars + 1)

    part = await add_note_part(token=token, org_id=org, note_id=note["id"], body=big)

    assert part["bytes"] == len(big), "the write succeeded"
    assert "hint" in part
    assert "append_note_part" in part["hint"]
    # Not a door that may be closing: the capability-mint tools are
    # deliberately not advertised here (task 1428a184).
    assert "capability" not in part["hint"]


async def test_an_ordinary_body_carries_no_hint() -> None:
    """Otherwise the field is noise on every call, which is how a hint
    stops being read."""
    token, org = await _tenant()
    note = await create_note(token=token, org_id=org, kind="text", text="seed")
    part = await add_note_part(token=token, org_id=org, note_id=note["id"], body=_BODY)
    assert "hint" not in part
