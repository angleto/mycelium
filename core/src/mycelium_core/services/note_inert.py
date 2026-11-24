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

A third clause gates nothing: ``work_closed`` is the read-side signal that
a note's linked work is all over, shown to a person who then decides what
the note becomes. It reads the same ``_task_is_open`` as ``open_work_exists``
so the garden has one definition of "open".

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

from sqlalchemy import ColumnElement, Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.note import Note, NoteMaturity
from mycelium_core.models.note_link import NoteTaskLink
from mycelium_core.models.task import Task
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import note_touch

# A note edited within this window is treated as live (quiet-window
# default; the spec's configurable 14 days).
QUIET_WINDOW_DAYS = 14


def _task_is_open() -> ColumnElement[bool]:
    """What "open" means for a linked task, once for every reader below: a
    non-terminal workflow state, not archived, not in the bin. Written over
    ``Task`` and ``WorkflowState`` as joined by the caller."""
    return and_(
        Task.is_archived.is_(False),
        Task.deleted_at.is_(None),
        WorkflowState.is_terminal.is_(False),
    )


def open_work_exists(note_id_col: Any) -> ColumnElement[bool]:
    """A correlated ``EXISTS`` clause: the note (``note_id_col``) has at
    least one linked task still open -- a non-terminal workflow state, not
    archived, not soft-deleted. Use ``~open_work_exists(Note.id)`` in a
    ``WHERE`` to exclude live notes from a bulk autonomous transition."""
    return (
        select(NoteTaskLink.note_id)
        .join(Task, Task.id == NoteTaskLink.task_id)
        .join(WorkflowState, WorkflowState.id == Task.state_id)
        .where(NoteTaskLink.note_id == note_id_col, _task_is_open())
        .exists()
    )


def _all_work_over() -> ColumnElement[bool | None]:
    """A note's linked work folded to one value, over the rows of
    ``_linked_tasks``: TRUE when every linked task is closed, FALSE when one
    is open, NULL when there is no link.

    The rule (task e584ed61): a person decides what a note becomes once its
    work is over, and work that was abandoned (archived, in the bin) is over
    too. It is computed on read and nothing is written on the note, so
    restoring a task turns the signal off again.

    - A note with no link is not "closed": it never had work, and flagging
      it would put every plain note on the person's desk. ``bool_and`` over
      no rows is NULL, and only TRUE passes.
    - The join to ``Task`` is what makes a link count. A link whose task the
      session cannot see (another workspace, under row-level security) or
      whose row is gone is not work of this note.
    - A task whose state the outer join cannot find counts as not open, as
      it does in ``open_work_exists``, whose inner join drops it.

    ``work_closed_note_ids`` and ``work_closed`` read this same fold."""
    return func.bool_and(~func.coalesce(_task_is_open(), False))


def _linked_tasks(*columns: Any) -> Select[Any]:
    """``columns`` over a note's links joined to their tasks and states."""
    return (
        select(*columns)
        .select_from(NoteTaskLink)
        .join(Task, Task.id == NoteTaskLink.task_id)
        .outerjoin(WorkflowState, WorkflowState.id == Task.state_id)
    )


# Two plans for one rule, chosen by measurement (EXPLAIN ANALYZE as the
# application role under row-level security, one workspace of 20k notes,
# 30k links and 5k tasks spread over open, terminal, archived and binned):
#
#   reader                    two EXISTS   per-note scalar   grouped by link
#   flag of a 200-id page     ~9 ms (*)    ~9 ms              ~11 ms
#   filtered list, 201 rows   ~220 ms      ~1500 ms           ~130 ms
#
#   (*) the planner chose per-note probes for it on this seed; on another
#       seed of the same size it hashed every link of the workspace, 110-230
#       ms for one page. The scalar form cannot be planned that way.
#   The unfiltered page itself measured ~35 ms. Medians of seven
#   interleaved runs on a loaded machine: the ratios are the finding, not
#   the milliseconds. The filter still reads every link of the workspace
#   once; only a stored flag would avoid that, and the rule is that nothing
#   is written on the note.
#
# The page's flag therefore probes each note (cost follows the page, and a
# scalar subquery with an aggregate is never flattened into a hash join),
# and the filter, which has to look at the whole workspace anyway, groups the
# links once and joins the notes to the result.


async def work_closed_note_ids(
    session: AsyncSession, *, org_id: uuid.UUID, note_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of ``note_ids`` have their linked work over: the per-row flag of
    a list page, one probe of ``ix_note_task_link_note`` per note.

    ``org_id`` is stated in the query as well as enforced by row-level
    security, like the list's other per-row reads: a note id of another
    workspace passed in by mistake answers nothing even on a session the
    policies do not narrow."""
    if not note_ids:
        return set()
    all_over = (
        _linked_tasks(_all_work_over())
        .where(NoteTaskLink.note_id == Note.id, NoteTaskLink.org_id == org_id)
        .scalar_subquery()
    )
    rows = await session.scalars(
        select(Note.id).where(
            Note.org_id == org_id,
            Note.id.in_(note_ids),
            Note.promoted_at.is_(None),
            all_over.is_(True),
        )
    )
    return set(rows.all())


def work_closed(org_id: uuid.UUID) -> ColumnElement[bool]:
    """A ``WHERE`` clause over ``Note``: the list's "to decide" filter, the
    same rule as ``work_closed_note_ids`` over the whole workspace.

    A transplanted note is left out, here and in the flag: it became a task
    through its ``promoted_from`` link and is read-only, so there is nothing
    left for a person to decide on it, and the board gives it no action."""
    closed = (
        _linked_tasks(NoteTaskLink.note_id)
        .where(NoteTaskLink.org_id == org_id)
        .group_by(NoteTaskLink.note_id)
        .having(_all_work_over().is_(True))
    )
    # The org is stated on the links and not on ``Note``: the list negates
    # this clause for ``work_closed=false``, and a negated org test would
    # admit every other workspace's notes on a session the policies do not
    # narrow. The list's own perimeter stays the list's.
    return and_(Note.promoted_at.is_(None), Note.id.in_(closed))


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
