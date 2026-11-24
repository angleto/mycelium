"""The note-side effective-node predicate: ONE definition of "this note
counts" for the note/graph surfaces (task f8402e7f).

ADR-0043 D1 already writes the definition in prose -- a note is
**effective** iff it is not held at the review gate (``review_state`` is
neither ``'proposed'`` nor, since ADR-0075, ``'declined'``) AND
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

from sqlalchemy import ColumnElement, and_, func, not_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.util import AliasedClass

from mycelium_core.models.note import Note

#: A generated note waiting for a person's decision (ADR-0043 D1).
PROPOSED = "proposed"
#: A generated note a person turned down (ADR-0075). It used to be a
#: soft-delete, which put the proposal in reach of the trash purge and the
#: retention sweep although no trash view showed it: declining is a
#: decision, kept and reversible, not a deletion.
DECLINED = "declined"
#: Every state held at the review gate: withheld from every reading,
#: listing and retrieval surface, readable only through the review
#: surfaces. NULL (human / legacy / user-initiated) and 'approved' are
#: effective. This tuple is the one place that says which states those
#: are; the SQL and row-level forms below and every surface that has to
#: speak about the gate directly (the blob perimeter, the classification
#: queue, the fact extractor) read it from here.
GATED_STATES: tuple[str, ...] = (PROPOSED, DECLINED)


def review_gated(
    entity: type[Note] | AliasedClass[Note] = Note,
) -> ColumnElement[bool]:
    """SQL: the note is held at the review gate. NULL-safe by construction:
    ``coalesce`` turns the NULL of every ordinary note into a value outside
    the set, so the predicate is never NULL and can be negated without the
    three-valued-logic hazard (a bare ``IN`` on NULL is NULL, and ``NOT``
    of it would drop exactly the ordinary notes)."""
    return func.coalesce(entity.review_state, "").in_(GATED_STATES)


def is_review_gated(review_state: str | None) -> bool:
    """Row-level twin of :func:`review_gated`."""
    return review_state in GATED_STATES


#: Chunk size for the ``among`` narrowing of :func:`ineffective_note_ids`.
#: An expanding ``IN`` binds one parameter per id and asyncpg caps a
#: statement at 32767, so a caller with a very wide candidate set (a note
#: carrying a tag shared by tens of thousands of notes) would otherwise
#: fail on the driver. Same guard, same reason, as ``memory._ERASE_CHUNK``.
_AMONG_CHUNK = 5000


def effective_note_clause(
    *,
    include_deleted: bool = False,
    include_gated: bool = False,
    entity: type[Note] | AliasedClass[Note] = Note,
) -> ColumnElement[bool]:
    """SQL predicate selecting the EFFECTIVE notes (ADR-0043 D1, ADR-0075):
    not :func:`review_gated` AND ``deleted_at IS NULL``.

    The gate leg is NULL-safe (see :func:`review_gated`), which is what
    makes the NULL default -- every human-authored, legacy or
    user-initiated note -- pass, so the clause is a no-op until a workspace
    actually holds a proposal.

    ``include_deleted=True`` is the trash-view opt-in (``list_notes``,
    ``lookup``, the unified search, the merge idempotency branch): it
    drops ONLY the soft-delete leg.

    ``include_gated=True`` is NOT a listing option: no surface may mix
    proposals, pending or declined, into what it shows. It exists for the
    PHOTOGRAPHERS -- the revision logger and ``snapshot_note`` -- which
    must record whatever state a note is in, including a state no reader
    is allowed to see. A snapshot that silently dropped the parts of a
    gated note would restore as an empty note, so being permissive there
    is the safe direction, and being permissive anywhere else is not.
    The review surfaces do not use this: they select the state they list
    positively.

    ``entity`` is the mapped ``Note`` by default and an ``aliased(Note)``
    when a query has to speak about two notes at once -- the two ends of
    a link, say. It is what keeps this a single definition instead of a
    hand-written copy per self-join.

    Both legs are NULL-safe booleans (``IS DISTINCT FROM`` / ``IS NULL``
    never evaluate to NULL), so :func:`ineffective_note_ids` can negate
    the whole clause without the usual three-valued-logic hazard.
    """
    legs: list[ColumnElement[bool]] = []
    if not include_gated:
        legs.append(not_(review_gated(entity)))
    if not include_deleted:
        legs.append(entity.deleted_at.is_(None))
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
    org's notes that are NOT effective (held at the review gate, or in the trash).

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
    include_gated: bool = False,
) -> bool:
    """Row-level mirror of :func:`effective_note_clause`, for the code
    paths that already hold the note (or just its two state columns:
    ``task_search`` reads them as a projection, not as an ORM object).

    ``include_gated`` opens the review gate, and the callers allowed
    to ask for it are counted on one hand: the review inbox path of
    ``notes.get_note`` (approving a proposal is how it stops being one),
    the un-reject in ``notes.restore_note``, and the photographers that
    must record a state nobody may read. It is never a listing option.

    All three forms are pinned against each other by test over the full
    (review_state x deleted_at) matrix, so this can never drift from the
    clause above nor from its complement.
    """
    if is_review_gated(review_state) and not include_gated:
        return False
    return deleted_at is None or include_deleted
