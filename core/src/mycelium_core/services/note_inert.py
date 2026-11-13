"""Anti-mutation invariant: the system never decays or decomposes a
*live* note (task 8a26c000, decision 2026-05-30 / note 90e4db3e §12).

A note is "live" while it is being actively worked: it has at least one
linked task still open, or it was edited within a recent quiet window.
Autonomous lifecycle operations (the maturity sweep's dormant decay, the
supersedes/contradicts decay, the value-axis auto-promotion, and the
on-demand distillation's humus side-effect) must skip such notes.

Two predicates, because the mutations they gate differ:

- ``note_has_open_work`` / ``open_work_exists`` -- the "live" signal
  (an open linked task). Gates the lifecycle transitions: do not dormant
  or crystallise a note that has active work.
- ``is_inert`` -- eligibility for autonomous *decomposition*: the note is
  archived or dormant, has no open work, and is past the quiet window.
  Gates the distillation humus side-effect.

Notes:
- "Open task" = any ``NoteTaskLink`` to a task whose workflow state is
  non-terminal and which is not archived/soft-deleted. Any link kind
  counts (a safety invariant errs toward not touching the note); this
  subsumes the "active watering" clause (watering is the ``subject``
  link kind).
- The quiet window reads the liveness clock of ``note_touch`` (last
  non-system touch of the note or its parts, else its creation), not
  ``updated_at``, which every system write bumps. The spec's draft/lock
  clauses are no-ops until those columns exist (kept here as a documented
  gap, not a silent omission);
  the ``protected`` clause is live since migration 0081 (Fase P, task
  561c6aca): protected prose is never eligible for decomposition.
- All callers run inside a tenant session, so RLS scopes the queries to
  the org; the predicates add no explicit org filter.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.note import Note, NoteMaturity
from mycelium_core.models.note_link import NoteTaskLink
from mycelium_core.models.task import Task
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import note_touch

# A note edited within this window is treated as live (quiet-window
# default; the spec's configurable 14 days).
QUIET_WINDOW_DAYS = 14


def open_work_exists(note_id_col: Any) -> ColumnElement[bool]:
    """A correlated ``EXISTS`` clause: the note (``note_id_col``) has at
    least one linked task still open -- a non-terminal workflow state, not
    archived, not soft-deleted. Use ``~open_work_exists(Note.id)`` in a
    ``WHERE`` to exclude live notes from a bulk autonomous transition."""
    return (
        select(NoteTaskLink.note_id)
        .join(Task, Task.id == NoteTaskLink.task_id)
        .join(WorkflowState, WorkflowState.id == Task.state_id)
        .where(
            NoteTaskLink.note_id == note_id_col,
            Task.is_archived.is_(False),
            Task.deleted_at.is_(None),
            WorkflowState.is_terminal.is_(False),
        )
        .exists()
    )


async def note_has_open_work(session: AsyncSession, *, note_id: uuid.UUID) -> bool:
    """True if the note has an open linked task (the per-note "live"
    check, for the single-note guards)."""
    return bool(await session.scalar(select(open_work_exists(note_id))))


async def is_inert(
    session: AsyncSession,
    *,
    note: Note,
    now: dt.datetime | None = None,
    quiet_days: int = QUIET_WINDOW_DAYS,
) -> bool:
    """True if the note is eligible for autonomous decomposition: it is
    not ``protected``, is archived or dormant, has no open linked work,
    and has not been edited within the quiet window. (Draft/lock clauses
    from the spec are no-ops until those columns exist.)"""
    if note.protected:
        return False
    if not (note.is_archived or note.maturity == NoteMaturity.dormant.value):
        return False
    now = now or dt.datetime.now(dt.UTC)
    # The quiet window is read on the liveness clock, not on updated_at: a
    # system write (the maturity sweep's own label) must not reopen it, and a
    # part edit, which never moves the note row, must.
    last_touch = note_touch.last_touch_subquery(note.org_id, note_id=note.id)
    touched = await session.scalar(
        select(note_touch.touched_at(last_touch))
        .select_from(Note)
        .outerjoin(last_touch, last_touch.c.note_id == Note.id)
        .where(Note.id == note.id)
    )
    if touched is not None and touched > now - dt.timedelta(days=quiet_days):
        return False
    return not await note_has_open_work(session, note_id=note.id)
