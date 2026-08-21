"""Markdown bodies are stored as the bytes the caller sent.

``docs/markdown-syntax.md`` states it as a guarantee ("MCP, the CLI and
imports write the bytes they were given"), and the note-part CRUD path
honours it. These tests cover the three service-layer places that did
not, each of which was reachable from MCP or the CLI:

- ``notes.update_note(text=...)`` wrote the FLAT body (the ``\\n\\n`` join
  of every part, i.e. exactly what ``get_body`` returns) into part 0 and
  left parts 1..N alive, so an ordinary read/modify/write-back DUPLICATED
  every part after the first;
- ``notes._collapsed_concat`` tested the separator with
  ``separator.rstrip(" \\t\\n\\r")``, which is ``""`` for every whitespace
  separator, so the branch always fired: it ran ``base.rstrip()`` on every
  blank-line append (eating a two-space hard break off the stored body) and
  dropped any other whitespace separator entirely;
- ``task_checklist`` stripped a field documented as markdown, demoting a
  body that opens with an indented code block to a paragraph.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_part import NotePart
from mycelium_core.services import note_parts as np
from mycelium_core.services import notes as nt
from mycelium_core.services import task_checklist as tcl
from mycelium_core.services.auth import signup
from mycelium_core.services.notes import _collapsed_concat


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"bytes-{uuid.uuid4().hex[:10]}@example.com",
            password="pw-strong-123",
            org_name="BYTES",
        )
    return r.org_id, r.user_id


async def _part_bodies(s, note_id: uuid.UUID) -> list[str]:
    rows = (
        (
            await s.execute(
                select(NotePart.body)
                .where(NotePart.note_id == note_id)
                .order_by(NotePart.ord, NotePart.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# --- the flat body writer ---------------------------------------------------


async def test_multipart_read_write_back_is_the_identity() -> None:
    """The bug this file exists for: ``get_body`` then ``update_note(text=)``
    with the same string used to come back with every part after the first
    duplicated ('AAA\\n\\nBBB' -> 'AAA\\n\\nBBB\\n\\nBBB')."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title=None, text="AAA"
        )
        await np.create_part(s, org_id=org, actor_id=user, note_id=note.id, body="BBB")
        body = await nt.get_body(s, note_id=note.id)
        assert body == "AAA\n\nBBB"

        n = await nt.get_note(s, org_id=org, note_id=note.id)
        await nt.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=n.version,
            text=body,
        )
        assert await nt.get_body(s, note_id=note.id) == body
        # And the STRUCTURE survived: writing the flat body back is a no-op on
        # the parts, not a collapse. Collapsing would cascade-delete every
        # annotation anchored to parts 1..N.
        assert await _part_bodies(s, note.id) == ["AAA", "BBB"]


async def test_multipart_changed_flat_body_is_refused() -> None:
    """A flat string cannot say where the part boundaries are, so a CHANGED
    flat body on a multi-part note is refused rather than guessed at."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title=None, text="AAA"
        )
        await np.create_part(s, org_id=org, actor_id=user, note_id=note.id, body="BBB")
        n = await nt.get_note(s, org_id=org, note_id=note.id)
        with pytest.raises(UnprocessableError) as ei:
            await nt.update_note(
                s,
                org_id=org,
                actor_id=user,
                note_id=note.id,
                expected_version=n.version,
                text="AAA\n\nCCC",
            )
        assert ei.value.code is MessageCode.NOTE_BODY_MULTIPART
        # Refused means refused: nothing was written.
        assert await _part_bodies(s, note.id) == ["AAA", "BBB"]


async def test_single_part_flat_body_still_writes() -> None:
    """The overwhelmingly common case is unchanged: one part, or none, and
    ``text`` replaces the body."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title=None, text="AAA"
        )
        n = await nt.get_note(s, org_id=org, note_id=note.id)
        await nt.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=n.version,
            text="  indented\n\ttab\n",
        )
        assert await _part_bodies(s, note.id) == ["  indented\n\ttab\n"]


async def test_flat_body_keeps_its_own_bytes() -> None:
    """Leading indentation, tabs, blank-line runs and the trailing newline
    are markdown, and survive a single-part write unchanged."""
    org, user = await _org()
    verbatim = "    code block\n\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nriga  \nrotta\n"
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title=None, text=verbatim
        )
        assert await nt.get_body(s, note_id=note.id) == verbatim


# --- the append separator ---------------------------------------------------


def test_collapsed_concat_keeps_a_hard_break() -> None:
    """A two-space hard break at the end of the stored body is markdown.
    ``base.rstrip()`` used to eat it on every append."""
    assert _collapsed_concat("riga  \n", "\n\n", "nuova") == "riga  \n\nnuova"
    assert _collapsed_concat("riga  ", "\n\n", "nuova") == "riga  \n\nnuova"


def test_collapsed_concat_does_not_drop_a_non_blank_separator() -> None:
    """``separator`` is an MCP tool parameter. Any whitespace separator other
    than the blank line used to be dropped outright, gluing the two texts
    together ('a' + '\\n' + 'b' came back as 'ab')."""
    assert _collapsed_concat("a", "\n", "b") == "a\nb"
    assert _collapsed_concat("a", " ", "b") == "a b"
    assert _collapsed_concat("a", "---", "b") == "a---b"


def test_collapsed_concat_still_collapses_the_blank_line() -> None:
    """The behaviour the helper is named for: exactly one blank line between
    the two, never two, never zero."""
    assert _collapsed_concat("a", "\n\n", "b") == "a\n\nb"
    assert _collapsed_concat("a\n", "\n\n", "b") == "a\n\nb"
    assert _collapsed_concat("a\n\n", "\n\n", "b") == "a\n\nb"
    assert _collapsed_concat("a\n\n\n", "\n\n", "b") == "a\n\n\nb"
    assert _collapsed_concat("", "\n\n", "b") == "b"
    assert _collapsed_concat(None, "\n\n", "b") == "b"


# --- checklist item bodies --------------------------------------------------


async def test_checklist_body_is_not_stripped() -> None:
    """``body`` is markdown. Stripping demoted a body opening with a 4-space
    indented code block to a paragraph, and ate a trailing hard break."""
    org, user = await _org()
    md = "    indented code\n\nprosa  \n"
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title=None, text="host"
        )
        item = await tcl.add_item(
            s, org_id=org, actor_id=user, note_id=note.id, text="voce", body=md
        )
        assert item.body == md
        updated = await tcl.update_item(
            s,
            org_id=org,
            actor_id=user,
            item_id=item.id,
            expected_version=item.version,
            body="  \tancora\n",
        )
        assert updated.body == "  \tancora\n"


async def test_checklist_blank_body_still_clears() -> None:
    """The caller-visible contract stays: a blank body clears the comment."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title=None, text="host"
        )
        item = await tcl.add_item(
            s, org_id=org, actor_id=user, note_id=note.id, text="voce", body="   \n "
        )
        assert item.body is None
        withbody = await tcl.update_item(
            s,
            org_id=org,
            actor_id=user,
            item_id=item.id,
            expected_version=item.version,
            body="reale",
        )
        assert withbody.body == "reale"
        cleared = await tcl.update_item(
            s,
            org_id=org,
            actor_id=user,
            item_id=item.id,
            expected_version=withbody.version,
            body="",
        )
        assert cleared.body is None
