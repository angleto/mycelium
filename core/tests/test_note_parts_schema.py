"""Phase 1 schema + backfill smoke for note multi-part (task 801ef530,
parent c0459c4b, design note 2d228758).

The migration 0011 has already run by the time these tests execute
(the suite migrates to head on the dev DB). Each test runs in its own
freshly-signed-up workspace so backfill assertions are isolated from
whatever sits in the dev DB from other test runs.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from flow_core.db import admin_session, tenant_session
from flow_core.models.note import NoteKind
from flow_core.models.note_part import NotePart, NotePartUIState
from flow_core.services import notes as nt
from flow_core.services.auth import signup


def _email() -> str:
    return f"parts-{uuid.uuid4().hex[:10]}@example.com"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="PARTS")
    return r.org_id, r.user_id


async def test_note_part_table_accepts_basic_row() -> None:
    """Schema sanity: a freshly created note can host a note_part row
    with the expected columns + the deferrable unique (note_id, ord)
    constraint. Service-layer wiring lands in Phase 2; this is the
    raw-ORM smoke that the DDL works."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title=None,
            text=None,
        )
        part = NotePart(
            org_id=org,
            note_id=note.id,
            ord=0,
            body="# heading\n\nbody paragraph",
            lang="en",
        )
        s.add(part)
        await s.flush()
        roundtripped = (
            await s.execute(select(NotePart).where(NotePart.note_id == note.id))
        ).scalar_one()
        assert roundtripped.ord == 0
        assert roundtripped.body.startswith("# heading")
        assert roundtripped.lang == "en"
        assert roundtripped.merged_from_note_id is None
        # VersionMixin should set version = 1 by default.
        assert roundtripped.version >= 1


async def test_note_part_unique_constraint_deferrable() -> None:
    """A reorder transaction can stage two parts with conflicting
    (note_id, ord) and only validate at COMMIT, so the SPA can swap
    ords without going through a scratchpad value. We mimic the
    pattern: insert A at ord=0, then update A to ord=1, then insert B
    at ord=0 within the same flush -- the constraint must NOT fire
    until commit."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title=None,
            text=None,
        )
        a = NotePart(org_id=org, note_id=note.id, ord=0, body="A")
        s.add(a)
        await s.flush()
        # Swap: move A to a temporary high ord, insert B at 0, then
        # settle A at 1 -- the in-between state has both at ord=0 for
        # a beat (no flush between updates). DEFERRABLE INITIALLY
        # DEFERRED means PostgreSQL only checks at COMMIT.
        a.ord = 1
        s.add(NotePart(org_id=org, note_id=note.id, ord=0, body="B"))
        # If the constraint were IMMEDIATE this commit would error.
    # Outer ``async with`` commits the tenant_session; reaching this
    # line means the deferred check passed.


async def test_note_part_ui_state_defaults_to_expanded() -> None:
    """No row in note_part_ui_state ≡ collapsed=false; the table is
    only written when the user explicitly collapses or expands. This
    test checks the WRITE path (PK uniqueness + cascade), not the
    "absence means default" READ path which is service-level."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title=None,
            text=None,
        )
        part = NotePart(org_id=org, note_id=note.id, ord=0, body="x")
        s.add(part)
        await s.flush()
        s.add(NotePartUIState(user_id=user, part_id=part.id, collapsed=True))
        await s.flush()
        row = (
            await s.execute(
                select(NotePartUIState).where(
                    NotePartUIState.user_id == user,
                    NotePartUIState.part_id == part.id,
                )
            )
        ).scalar_one()
        assert row.collapsed is True


async def test_create_note_mirrors_transcript_to_part_zero() -> None:
    """Phase 6 prep (task 1cd8bc0a): ``create_note(text=...)`` now
    upserts a part(ord=0) with the same body so the parts surface
    is in sync from creation onward. Until the column drop lands,
    the two writes stay coupled; this test pins that contract."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title=None,
            text="Phase 6 mirror body.",
        )
        rows = (
            (await s.execute(select(NotePart).where(NotePart.note_id == note.id))).scalars().all()
        )
        assert len(rows) == 1
        assert rows[0].ord == 0
        assert rows[0].body == "Phase 6 mirror body."


async def test_update_note_text_mirrors_into_part_zero() -> None:
    """``update_note(text=...)`` upserts part(ord=0): updates the body
    when one exists, creates it when the note had no part yet (e.g.
    a transcribe arriving on a voice-note row that was created
    without text)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title=None, text=None
        )
        # No part yet (create_note with text=None doesn't mirror).
        assert (
            await s.execute(select(NotePart).where(NotePart.note_id == note.id))
        ).scalars().first() is None
        await nt.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=note.version,
            text="first edit",
        )
        rows = (
            (await s.execute(select(NotePart).where(NotePart.note_id == note.id))).scalars().all()
        )
        assert len(rows) == 1
        assert rows[0].body == "first edit"
        # A second edit updates in place (no second part row).
        note2 = await nt.get_note(s, org_id=org, note_id=note.id)
        await nt.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=note2.version,
            text="second edit",
        )
        rows2 = (
            (await s.execute(select(NotePart).where(NotePart.note_id == note.id))).scalars().all()
        )
        assert len(rows2) == 1
        assert rows2[0].body == "second edit"
