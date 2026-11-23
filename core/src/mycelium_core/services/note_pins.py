"""A person's pinned notes (migration 0033).

Pinning is the person keeping a note at hand: it writes one row of their own
and nothing on the note, and nothing in the activity log either. The garden
ages a note by the last time someone worked on it (``note_touch``), and a pin
is not work: a pinned note that nobody opens still goes dormant. Both
directions are idempotent, because the control that sets and clears a pin is
a toggle pressed from several lists at once, and a second press racing the
first must land on the state the person sees rather than on an error.

A person holds at most ``MAX_PINS`` pinned notes in a workspace. The pinned
list is read as one page, and the list's page ceiling is the same number:
past it a pin could be neither seen nor cleared from the list that shows
pins. The cap refuses a NEW pin; re-pinning a pinned note is still the
idempotent no-op it always was. It counts every pin row, a pin on a note in
the bin included: that note comes back to the list when it is restored, and
counting only the visible ones would let a restore push the list past one
page. Such a pin stops counting when the note is purged and the row cascades.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import ConflictError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note_pin import NotePin
from mycelium_core.services import notes as notes_svc

#: The most notes one person can keep pinned in one workspace. Equal to the
#: note list's page ceiling, so the whole pinned list is always one page.
MAX_PINS = 200


async def pin(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, note_id: uuid.UUID
) -> dt.datetime:
    """Pin a note for ``user_id``. Pinning a pinned note keeps the first
    pin's time, so the order of the pinned list is the order the person
    pinned things in, not the order they last pressed the button. A note in
    the bin, or a proposal still awaiting review, cannot be pinned
    (``NOTE_NOT_FOUND``, the answer the normal read path gives); an archived
    one can, which is the point: the person reaches it from here."""
    await notes_svc.get_note(session, org_id=org_id, note_id=note_id)
    # Two presses racing past the count would both see room for one more.
    # A transaction-scoped lock on this person's pins in this workspace
    # makes count-then-insert one step; it is released at commit and
    # touches no row anyone else writes.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"note_pins:{org_id}:{user_id}"},
    )
    already = await session.scalar(
        select(NotePin.pinned_at).where(NotePin.user_id == user_id, NotePin.note_id == note_id)
    )
    if already is not None:
        return already
    held = await session.scalar(
        select(func.count())
        .select_from(NotePin)
        .where(NotePin.org_id == org_id, NotePin.user_id == user_id)
    )
    if (held or 0) >= MAX_PINS:
        raise ConflictError(MessageCode.NOTE_PIN_LIMIT, limit=MAX_PINS)
    await session.execute(
        insert(NotePin)
        .values(user_id=user_id, note_id=note_id, org_id=org_id)
        .on_conflict_do_nothing(index_elements=["user_id", "note_id"])
    )
    pinned_at = (
        await session.execute(
            select(NotePin.pinned_at).where(NotePin.user_id == user_id, NotePin.note_id == note_id)
        )
    ).scalar_one()
    return pinned_at


async def unpin(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, note_id: uuid.UUID
) -> None:
    """Clear the pin. Unpinning what is not pinned is not an error, and it
    works on a note in the bin too: a pin must always be removable."""
    await session.execute(
        delete(NotePin).where(
            NotePin.org_id == org_id, NotePin.user_id == user_id, NotePin.note_id == note_id
        )
    )


async def pinned_note_ids(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """The person's pins in this workspace, most recently pinned first."""
    rows = await session.execute(
        select(NotePin.note_id)
        .where(NotePin.org_id == org_id, NotePin.user_id == user_id)
        .order_by(NotePin.pinned_at.desc(), NotePin.note_id)
    )
    return list(rows.scalars())
