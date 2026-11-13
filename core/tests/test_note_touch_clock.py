"""The garden's liveness clock: what counts as a note being touched.

The maturity sweep, the inert predicate and the distillation candidates all
ask one question, "has a person or an agent worked on this note lately?".
They used to answer it with ``notes.updated_at``, which is a row-write clock:
``TimestampMixin`` bumps it on ANY write to the row, the sweep's own maturity
label included. Two defects followed, both observed in production on
2026-11-10 when the garden loop was switched on:

- the sweep made a note dormant, the flush stamped ``updated_at = now``, and
  the very next tick read that stamp as a fresh touch and promoted the note
  back to growing (f8983562 / ca95b2fc: dormant at 15:29:06, growing again at
  15:33:08). Dormancy lasted one tick and could never be observed;
- an edit to any part but the note row itself never moves ``updated_at``, so
  a note worked on every day through its parts read as untouched since its
  creation.

The clock is now read from the activity log, which records every touch with
the actor's kind: system writes are excluded by construction, and part
touches resolve to their note.

Observed failing against the defect, on the tree at c4f9e1a5 before the fix,
2026-11-10: the dormancy test (second tick promoted it back, 1 == 0), the
part-edit test (0 == 1), the quiet-window test (is_inert False) and the
recent-touch test (a note being worked on through its parts was made dormant,
1 == 0). The system-write test passed there too: it guards the new clock, not
the old one, which never saw part writes of any kind.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import Note, NoteKind, NoteMaturity
from mycelium_core.models.note_part import NotePart
from mycelium_core.services import note_inert, note_links, note_parts
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.auth import signup


async def _ws() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="CLOCK",
        )
    return a.org_id, a.user_id


async def _aged_note(org: uuid.UUID, user: uuid.UUID, *, maturity: str, days: int) -> uuid.UUID:
    """A note a person wrote ``days`` ago and has not touched since: its
    creation and its row clock are moved back. The activity log is
    append-only even for the test role, so it is left alone: creating a
    note logs only ``create``, which is genesis and not a touch."""
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="old", text="body"
        )
        note_id = note.id
        past = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        await s.execute(
            text("UPDATE notes SET maturity = :m, created_at = :t, updated_at = :t WHERE id = :id"),
            {"m": maturity, "t": past, "id": str(note_id)},
        )
    return note_id


async def _tick(org: uuid.UUID, user: uuid.UUID) -> dict[str, int]:
    # The production sweep runs as the system actor (worker/garden.py).
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        return await note_links.tick_maturity_transitions(s, org_id=org, actor_id=user)


async def _maturity(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> str:
    async with tenant_session(str(org), str(user)) as s:
        return (await s.execute(select(Note.maturity).where(Note.id == note_id))).scalar_one()


async def test_dormancy_written_by_the_sweep_survives_the_next_tick() -> None:
    org, user = await _ws()
    note_id = await _aged_note(org, user, maturity=NoteMaturity.growing.value, days=120)

    first = await _tick(org, user)
    assert first["to_dormant"] == 1
    assert await _maturity(org, user, note_id) == NoteMaturity.dormant.value

    second = await _tick(org, user)
    assert second["dormant_to_growing"] == 0
    assert await _maturity(org, user, note_id) == NoteMaturity.dormant.value


async def test_a_part_edit_is_a_touch() -> None:
    org, user = await _ws()
    note_id = await _aged_note(org, user, maturity=NoteMaturity.dormant.value, days=120)
    async with tenant_session(str(org), str(user)) as s:
        part = (await s.execute(select(NotePart).where(NotePart.note_id == note_id))).scalar_one()
        await note_parts.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=part.id,
            expected_version=part.version,
            body="edited today",
        )

    counters = await _tick(org, user)
    assert counters["dormant_to_growing"] == 1
    assert await _maturity(org, user, note_id) == NoteMaturity.growing.value


async def test_a_system_write_is_not_a_touch() -> None:
    org, user = await _ws()
    note_id = await _aged_note(org, user, maturity=NoteMaturity.seed.value, days=120)
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        part = (await s.execute(select(NotePart).where(NotePart.note_id == note_id))).scalar_one()
        await note_parts.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=part.id,
            expected_version=part.version,
            body="rewritten by a machine",
        )

    counters = await _tick(org, user)
    assert counters["seed_to_growing"] == 0
    assert await _maturity(org, user, note_id) == NoteMaturity.seed.value


async def test_the_sweeps_own_write_does_not_reopen_the_quiet_window() -> None:
    org, user = await _ws()
    note_id = await _aged_note(org, user, maturity=NoteMaturity.growing.value, days=120)
    await _tick(org, user)
    async with tenant_session(str(org), str(user)) as s:
        note = (await s.execute(select(Note).where(Note.id == note_id))).scalar_one()
        assert note.maturity == NoteMaturity.dormant.value
        assert await note_inert.is_inert(s, note=note) is True


async def test_a_recent_human_touch_keeps_a_note_alive() -> None:
    org, user = await _ws()
    note_id = await _aged_note(org, user, maturity=NoteMaturity.growing.value, days=120)
    async with tenant_session(str(org), str(user)) as s:
        part = (await s.execute(select(NotePart).where(NotePart.note_id == note_id))).scalar_one()
        await note_parts.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=part.id,
            expected_version=part.version,
            body="still working on it",
        )

    counters = await _tick(org, user)
    assert counters["to_dormant"] == 0
    assert await _maturity(org, user, note_id) == NoteMaturity.growing.value
