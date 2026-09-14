"""What a read surface sends when nobody asked for everything.

Two independent duplications, one shape.

``_note`` emitted ``transcript`` -- which is DERIVED by joining the part
bodies -- alongside those same bodies, with both defaults on. The design
note of this workstream has a 30,314-byte part, so one ``get_note``
returned 60,628 bytes where 30,314 say everything.

``_blob`` returned the whole memory text on every recall hit. A result
list is an index: enough to decide which memory to open, not the memory.
The sharpest case is ``whoami``, which the server's own instructions tell
every client to call at session start, and which hard-wires ``limit=5``
and takes no arguments -- the one recall path where a caller cannot ask
for less.

The pair of properties each test holds: the surface sends less, AND the
whole is still one call away. Economy that loses the escape hatch is not
economy.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.db import admin_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    _PRINCIPAL,
    add_note_part,
    memory_get_blob,
    memory_search,
    memory_write,
    whoami,
)
from mycelium_mcp.server import create_note as mcp_create_note
from mycelium_mcp.server import get_note as mcp_get_note
from mycelium_mcp.server import list_notes as mcp_list_notes

_LONG = "riga di corpo che vale la pena non mandare due volte.\n" * 40


@pytest.fixture()
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _tenant() -> tuple[str, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="Economy",
        )
    assert r.token is not None
    return r.token, str(r.org_id)


async def test_get_note_sends_the_body_once(_fake_embedder: None) -> None:
    """(a) parts XOR transcript. The bodies are there; the join is not.

    Two DISTINCT bodies, each carrying a marker that appears once inside
    it: counting a substring of a body made of one repeated line counts
    the repetitions, not the copies, and would pass whatever the payload
    contained."""
    token, org = await _tenant()
    # The marker sits AFTER the first line on purpose: ``create_note``
    # derives the note TITLE from the first line of the body, so a marker
    # placed there is legitimately in the payload twice and the count
    # below would fail for a reason that has nothing to do with this fix.
    alfa, beta = f"ALFA-{uuid.uuid4().hex}", f"BETA-{uuid.uuid4().hex}"
    first, second = f"Titolo uno\n\n{alfa}\n{_LONG}", f"Titolo due\n\n{beta}\n{_LONG}"
    note = await mcp_create_note(token=token, org_id=org, kind="text", text=first)
    await add_note_part(token=token, org_id=org, note_id=note["id"], body=second)

    full = await mcp_get_note(token=token, org_id=org, note_id=note["id"])

    assert [p["body"] for p in full["parts"]] == [first, second]
    assert "transcript" not in full or full["transcript"] is None
    # The measure the whole item is about: each body appears ONCE in the
    # payload. It used to appear twice -- once in ``parts`` and once
    # inside the ``transcript`` join of those same parts.
    flat = str(full)
    assert flat.count(alfa) == 1
    assert flat.count(beta) == 1


async def test_list_notes_still_gets_its_flat_body(_fake_embedder: None) -> None:
    """The regression the builder-level fix could have caused. ``list_notes``
    passes no parts and an explicit transcript, so it is the one caller for
    which the flat body is the ONLY channel -- and it must keep it."""
    token, org = await _tenant()
    await mcp_create_note(token=token, org_id=org, kind="text", text="corpo piatto")

    rows = await mcp_list_notes(token=token, org_id=org, include_transcript=True)

    assert rows["items"], rows
    assert any((r.get("transcript") or "") == "corpo piatto" for r in rows["items"]), rows


async def test_recall_returns_a_snippet_and_says_it_is_one(_fake_embedder: None) -> None:
    """(d) A cut body that does not admit it is worse than an absent one:
    the reader cannot tell a memory that ends there from one that was cut,
    and will summarise the cut as if it were the whole."""
    token, org = await _tenant()
    reset = _PRINCIPAL.set((uuid.UUID(int=0), uuid.UUID(org), None))
    _PRINCIPAL.reset(reset)

    async with admin_session():
        pass

    hits = await _write_and_search(token, org)
    blob = hits["hits"][0]["blob"]

    assert len(blob["text"]) == 500
    assert blob["text_truncated"] is True
    assert blob["text_chars"] == len(_LONG)
    # And it names the way out instead of leaving the reader to find it.
    assert blob["read_whole"] == "memory_get_blob"


async def _write_and_search(token: str, org: str) -> dict:
    await memory_write(
        token=token, org_id=org, text=_LONG, operation_id=f"w-{uuid.uuid4().hex[:6]}"
    )
    return await memory_search(
        token=token,
        org_id=org,
        query="riga di corpo",
        operation_id=f"s-{uuid.uuid4().hex[:6]}",
        limit=5,
    )


async def test_a_caller_can_ask_for_the_whole_text(_fake_embedder: None) -> None:
    """The opt-out. A default that cannot be turned off is a limit, not a
    default."""
    token, org = await _tenant()
    await memory_write(token=token, org_id=org, text=_LONG, operation_id="w-full")
    hits = await memory_search(
        token=token,
        org_id=org,
        query="riga di corpo",
        operation_id="s-full",
        limit=5,
        snippet_chars=None,
    )
    blob = hits["hits"][0]["blob"]
    assert blob["text"] == _LONG
    assert "text_truncated" not in blob


async def test_memory_get_blob_is_never_capped(_fake_embedder: None) -> None:
    """(f) The escape hatch. Capping this one would turn a cap into data
    loss: it is what every truncated hit points at."""
    token, org = await _tenant()
    written = await memory_write(token=token, org_id=org, text=_LONG, operation_id="w-escape")
    whole = await memory_get_blob(token=token, org_id=org, blob_id=written["id"])
    assert whole["text"] == _LONG
    assert "text_truncated" not in whole


async def test_the_write_result_is_not_capped(_fake_embedder: None) -> None:
    """A write returns what the caller just sent: capping it would hide a
    caller's own text from it, for no saving -- the characters were already
    spent on the way in."""
    token, org = await _tenant()
    written = await memory_write(token=token, org_id=org, text=_LONG, operation_id="w-echo")
    assert written["text"] == _LONG


async def test_whoami_recall_is_tighter_than_a_search(_fake_embedder: None) -> None:
    """(e) The bootstrap path, and the reason it gets its own number: the
    server's instructions tell every client to call ``whoami`` at session
    start, it hard-wires ``limit=5`` and takes no arguments, so it is the
    one recall a caller cannot ask for less on. Five whole documents in
    every session's first turn is the shape this prevents.

    Today the lane is empty in production, so this is prevention rather
    than a saving already banked -- which is exactly why it needs a test
    rather than an observation."""
    token, org = await _tenant()
    await memory_write(
        token=token, org_id=org, text=_LONG, operation_id="w-whoami", channel_key="agent"
    )

    me = await whoami(token=token, org_id=org)
    recall = me["memory_lane"]["recall"]

    assert recall, me["memory_lane"]
    text = recall[0]["blob"]["text"]
    assert len(text) == 200
    assert recall[0]["blob"]["text_truncated"] is True
    # Tighter than the search default, not merely capped.
    assert len(text) < 500
