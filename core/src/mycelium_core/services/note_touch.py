"""When a person or an agent last worked on a note: the garden's liveness clock.

Every rule that ages a note asks this one question: the maturity sweep
(seed -> growing, growing/mature -> dormant, dormant -> growing), the inert
predicate that gates decomposition, and the distillation candidates. This
module is the only place that answers it.

Why not ``notes.updated_at``. It is a row-write clock: ``TimestampMixin``
bumps it on ANY write to the row, so the sweep's own maturity label counts as
a touch. Measured in production on 2026-11-10, the first time the garden loop
ran: the sweep made f8983562 and ca95b2fc dormant at 15:29:06, the flush
stamped ``updated_at`` with that instant, and the next tick (15:33:08) read
the stamp as a fresh touch and promoted both back to growing. Dormancy lasted
one tick. The same clock is blind the other way: an edit to a note's parts
never moves the note row, so a note worked on every day through its parts
read as untouched since it was created, and was made dormant while in use.
``updated_at`` stays what it is (the row-write clock the list sync and the
``updated_since`` filter rely on); it just stops being read as liveness.

The clock is the later of the note's creation and its last touch in the
append-only activity log, which records the actor's kind on every row:

- ``actor_kind = 'system'`` rows are excluded, so no worker, sweep or
  migration can keep a note alive or wake it up;
- part touches resolve to their owning note, so working through parts counts
  the same as editing the note row;
- the touch vocabulary is the co-activity one (``refresh_coactivity`` reads
  the same two sets), plus a part ``create``: adding a part to an existing
  note is working on it. Co-activity leaves ``create`` out for its own reason
  (a bulk import would forge a co-work clique), which does not apply to the
  question of whether one note is alive. A note's own ``create`` is covered by
  ``created_at`` and is not counted twice.

What it does not cover. A part that has since been deleted no longer joins
back to its note, so its touches stop counting: the clock can only read a
note as OLDER than it is, never younger, which errs toward dormancy and never
toward keeping a dead note alive. The aggregate scans the org's note and part
rows of the activity log through ``ix_activity_log_org_entity_ts``: linear
in the org's history, and not measured on a large org. If it shows up in the
sweep's timing, the remedy is an index on ``(org_id, entity, entity_id, ts)``,
not a second copy of the clock on the note row.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import ColumnElement, DateTime, Subquery, func, select, union_all

from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart

# Allow-lists of "working on it" actions, so a new action defaults to not
# counted. Verified against the audit vocabulary: ``entity="note"`` rows are
# the note-level lifecycle/content touches; ``entity="note_part"`` rows are
# part edits, keyed by the part id. ``create``, ``archive``, ``delete``,
# ``restore``, ``gdpr_erase`` and the machine actions (``auto_*``,
# ``maturity_tick``) are excluded as genesis, removal or machine events.
# ``set_maturity`` is the MANUAL override; the automatic tick is a system row
# and is dropped by the actor filter anyway.
NOTE_TOUCH_ACTIONS: frozenset[str] = frozenset(
    {
        "update",
        "append",
        "attach_tag",
        "detach_tag",
        "set_maturity",
        "parts_reorder",
        "restore_revision",
        "transcribe",
        "append_message",
    }
)
PART_TOUCH_ACTIONS: frozenset[str] = frozenset({"update", "append", "prepend", "replace"})
_PART_LIVENESS_ACTIONS: frozenset[str] = PART_TOUCH_ACTIONS | {"create"}


def last_touch_subquery(org_id: uuid.UUID, *, note_id: uuid.UUID | None = None) -> Subquery:
    """``(note_id, touched_at)``: the latest non-system touch per note.

    A note with no such touch has no row; join it with an OUTER join and read
    the clock through :func:`touched_at`, which falls back to ``created_at``.
    ``note_id`` narrows the aggregate to one note for the single-note checks.
    """
    note_events = select(ActivityLog.entity_id.label("note_id"), ActivityLog.ts).where(
        ActivityLog.org_id == org_id,
        ActivityLog.entity == "note",
        ActivityLog.entity_id.is_not(None),
        ActivityLog.actor_kind != "system",
        ActivityLog.action.in_(NOTE_TOUCH_ACTIONS),
    )
    part_events = (
        select(NotePart.note_id.label("note_id"), ActivityLog.ts)
        .join(NotePart, NotePart.id == ActivityLog.entity_id)
        .where(
            ActivityLog.org_id == org_id,
            ActivityLog.entity == "note_part",
            ActivityLog.actor_kind != "system",
            ActivityLog.action.in_(_PART_LIVENESS_ACTIONS),
            NotePart.org_id == org_id,
        )
    )
    if note_id is not None:
        note_events = note_events.where(ActivityLog.entity_id == note_id)
        part_events = part_events.where(NotePart.note_id == note_id)
    events = union_all(note_events, part_events).subquery("note_touch_events")
    return (
        select(events.c.note_id, func.max(events.c.ts).label("touched_at"))
        .group_by(events.c.note_id)
        .subquery("note_last_touch")
    )


def touched_at(last_touch: Subquery) -> ColumnElement[datetime.datetime]:
    """The liveness clock of ``Note``: its last touch, or its creation.

    ``GREATEST`` ignores NULLs in PostgreSQL, so a note nobody touched since
    it was written reads as its ``created_at``."""
    return func.greatest(Note.created_at, last_touch.c.touched_at, type_=DateTime(timezone=True))


__all__ = [
    "NOTE_TOUCH_ACTIONS",
    "PART_TOUCH_ACTIONS",
    "last_touch_subquery",
    "touched_at",
]
