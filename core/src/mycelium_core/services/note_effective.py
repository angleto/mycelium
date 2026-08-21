"""The note-side effective-node predicate: ONE definition of "this note
counts" for the note/graph surfaces (task f8402e7f).

ADR-0043 D1 already writes the definition in prose -- a note is
**effective** iff ``review_state IS DISTINCT FROM 'proposed'`` AND
``deleted_at IS NULL`` -- but the code kept re-deriving it per call site,
and the copies drifted: the centrality node set, the bounded
neighbourhood and the link-prediction candidate set each filtered only
the ``proposed`` leg, so a note in the TRASH still moved the PageRank of
live notes, still surfaced in an agent's working set and could still be
proposed as a link target. This module is the note-side twin of
:func:`~mycelium_core.services.retrieval.stages.humus.ineffective_source_blob_exclusion`
(the blob-side perimeter, task c5da112c): same shape, same rule -- the
perimeter is DERIVED at query time from the note row, so a soft-delete
or a restore changes every surface at once, with nothing to re-index and
no divergence window.

Three forms, one rule:

- :func:`effective_note_clause` -- the SQL predicate, for the surfaces
  that SELECT notes (node set, candidate set, listings);
- :func:`ineffective_note_ids` -- its complement as a set of ids, for the
  builders that stream edge rows (links, co-activity, edge usage) and
  must drop a row when EITHER endpoint is ineffective;
- :func:`note_is_effective` -- the row-level mirror, for code that
  already holds the note (or its two columns) and decides in Python.

``is_archived`` is deliberately NOT one of the axes, and this is the
place that says so. Archiving is a shelf, not a bin: the archive is the
INPUT of the decomposition pipeline, and ``candidates`` ranks the
archived notes it offers for distillation by their stored centrality and
groups them by their Leiden community (ADR-0034/0039 push this further
still, selecting structural humus by top-20% PageRank). An archived note
that stopped being a graph node would have no centrality and no cluster,
so those candidates would degrade to a date sort and the pattern
candidates would vanish by construction. The listing surfaces do hide
archived notes by default, but that is a presentation choice with its own
``include_archived`` opt-in (``notes.list_notes``, ``task_search``), not
a statement about whether the note is effective -- which is exactly why
it stays out of here instead of becoming a third, silently-diverging
axis.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, and_, not_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.note import Note

#: The un-approved autonomous proposal state (ADR-0043 D1). NULL (human /
#: legacy / user-initiated) and 'approved' are both effective.
PROPOSED = "proposed"

#: Chunk size for the ``among`` narrowing of :func:`ineffective_note_ids`.
#: An expanding ``IN`` binds one parameter per id and asyncpg caps a
#: statement at 32767, so a caller with a very wide candidate set (a note
#: carrying a tag shared by tens of thousands of notes) would otherwise
#: fail on the driver. Same guard, same reason, as ``memory._ERASE_CHUNK``.
_AMONG_CHUNK = 5000


def effective_note_clause(
    *, include_deleted: bool = False, include_proposed: bool = False
) -> ColumnElement[bool]:
    """SQL predicate selecting the EFFECTIVE notes (ADR-0043 D1):
    ``review_state IS DISTINCT FROM 'proposed' AND deleted_at IS NULL``.

    ``IS DISTINCT FROM`` (not ``!=``) is what makes the NULL default --
    every human-authored, legacy or user-initiated note -- pass, so the
    clause is a no-op until a workspace actually holds a proposed note.

    ``include_deleted=True`` is the trash-view opt-in (``list_notes``,
    ``lookup``, the unified search, the merge idempotency branch): it
    drops ONLY the soft-delete leg.

    ``include_proposed=True`` is NOT a listing option: no surface may mix
    un-approved proposals into what it shows. It exists for the
    PHOTOGRAPHERS -- the revision logger and ``snapshot_note`` -- which
    must record whatever state a note is in, including a state no reader
    is allowed to see. A snapshot that silently dropped the parts of a
    gated note would restore as an empty note, so being permissive there
    is the safe direction, and being permissive anywhere else is not.
    The review inbox itself does not use this: it selects
    ``review_state = 'proposed'`` positively.

    Both legs are NULL-safe booleans (``IS DISTINCT FROM`` / ``IS NULL``
    never evaluate to NULL), so :func:`ineffective_note_ids` can negate
    the whole clause without the usual three-valued-logic hazard.
    """
    legs: list[ColumnElement[bool]] = []
    if not include_proposed:
        legs.append(Note.review_state.is_distinct_from(PROPOSED))
    if not include_deleted:
        legs.append(Note.deleted_at.is_(None))
    if not legs:
        return true()
    if len(legs) == 1:
        return legs[0]
    return and_(*legs)


async def ineffective_note_ids(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    among: Iterable[uuid.UUID] | None = None,
) -> set[uuid.UUID]:
    """The complement of :func:`effective_note_clause` as an id set: the
    org's notes that are NOT effective (proposed or in the trash).

    For the weave builders, which stream rows keyed by note id pairs
    (``note_links``, ``note_coactivity``, ``note_edge_usage``, and the
    note endpoint of ``note_task_links``) and must drop an edge when
    EITHER endpoint is ineffective -- an edge that
    survived would resurrect its endpoint as a phantom node in every
    consumer that derives its node set from the edges
    (``compute_betweenness``), exactly the way the soft-deleted-task
    guard already prevents on the task side.

    ``among`` narrows the query to a candidate set (the bounded local
    surfaces know their neighbours already, so they never scan the org).
    An empty ``among`` short-circuits without a query; a very wide one is
    chunked, since an expanding ``IN`` binds one parameter per id.

    The set is usually small (proposals are rare, the bin is bounded by
    what the user has thrown away and by the retention sweep), and it is
    empty on a workspace with neither -- in which case every caller's
    behaviour is byte-identical to not filtering at all.
    """
    stmt = select(Note.id).where(Note.org_id == org_id, not_(effective_note_clause()))
    if among is None:
        return {r[0] for r in (await session.execute(stmt)).all()}
    ids = list(among)
    if not ids:
        return set()
    out: set[uuid.UUID] = set()
    for start in range(0, len(ids), _AMONG_CHUNK):
        rows = await session.execute(stmt.where(Note.id.in_(ids[start : start + _AMONG_CHUNK])))
        out.update(r[0] for r in rows.all())
    return out


def note_is_effective(
    *,
    review_state: str | None,
    deleted_at: dt.datetime | None,
    include_deleted: bool = False,
    include_proposed: bool = False,
) -> bool:
    """Row-level mirror of :func:`effective_note_clause`, for the code
    paths that already hold the note (or just its two state columns:
    ``task_search`` reads them as a projection, not as an ORM object).

    ``include_proposed`` opens the review gate, and the callers allowed
    to ask for it are counted on one hand: the review inbox path of
    ``notes.get_note`` (approving a proposal is how it stops being one),
    the un-reject in ``notes.restore_note``, and the photographers that
    must record a state nobody may read. It is never a listing option.

    All three forms are pinned against each other by test over the full
    (review_state x deleted_at) matrix, so this can never drift from the
    clause above nor from its complement.
    """
    if review_state == PROPOSED and not include_proposed:
        return False
    return deleted_at is None or include_deleted
