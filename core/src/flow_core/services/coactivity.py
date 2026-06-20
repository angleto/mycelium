"""Co-activity aggregation: the third ``note_edge_strength`` source
(task f0a15247, ADR-0031 v2+).

ADR-0031 defers the co-activity weight to "once the activity log carries
the requisite shape". The shape is here (the append-only ``activity_log``
records every note touch with an actor and a timestamp); what was missing
was the aggregation worker. This module is it.

Signal: two notes are *co-active* when a human/agent touches both inside
one **working session** — a run of that actor's note touches with no gap
longer than ``COACTIVITY_SESSION_GAP``. Each session contributes +1 to
``session_count`` for every distinct co-touched pair. The read side
(``graph.coactivity_weight``) squashes the count into a saturating
``[0, 1]`` contribution that the soft-OR folds in next to the per-kind
base and the shared-tag overlap.

Why a materialised table and not an inline pass: the activity log is an
unbounded stream and ``compute_note_edge_weights`` is on the request path
(PageRank, betweenness, Leiden, the walk all fan through it). The
aggregation is O(events) per org and runs offline in the garden sweep,
mirroring the betweenness/snapshot split (task d8664631). A full per-org
replace keeps the table a pure projection of the window — no staleness,
no incremental-merge bookkeeping.

A note's body is multi-part (note_part): the ord-0 text edit logs an
``entity="note"`` ``update``, but every other part edit logs only an
``entity="note_part"`` row keyed by the *part* id. Both streams are
ingested — the part touches are resolved back to their owning note — so
working a multi-part note counts the same as a single-part one.

Deliberate exclusions, each one a correctness guard:
- ``actor_kind = 'system'`` events (workers, schedulers, migrations): a
  maturity sweep or a bulk re-tier touches many notes in one tick and
  would forge a spurious co-activity clique. Co-activity is about *human/
  agent* working context, not machine bookkeeping.
- ``create`` / removal actions: only genuine "working on it" touches
  count (see the allow-lists below). Bulk creation (import, onboarding)
  is not co-work; archive/delete is the opposite of working together.
- **oversized sessions**: a session that touches more than
  ``_MAX_SESSION_NOTES`` distinct notes is a *human* bulk operation (the
  bulk-tag / bulk-edit path), not focused co-work. It is dropped whole —
  otherwise N bulk touches forge an N²/2 fake clique, the same spurious
  signal the system-actor exclusion guards against, just human-initiated.
- rows with a NULL ``actor_id``: no actor to attribute a session to.
- soft-deleted notes: filtered against the live note set before write.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.activity_log import ActivityLog
from flow_core.models.note import Note
from flow_core.models.note_coactivity import NoteCoactivity
from flow_core.models.note_part import NotePart

# Rolling lookback: only the recent past shapes the live weave. Matches
# the "last 30-90 days" framing in ADR-0031's co-activity note; 90 days
# keeps a season of working context without letting ancient sessions
# dominate.
COACTIVITY_WINDOW_DAYS = 90
# Gap that splits one actor's touch stream into sessions. 30 minutes: a
# coffee break ends a session, a focused stretch stays one. Larger than
# the 60s edit-conflict window (note_conflict) on purpose — that detects
# collisions, this groups intentional co-work.
COACTIVITY_SESSION_GAP = datetime.timedelta(minutes=30)
# A session touching more distinct notes than this is a bulk operation,
# not co-work: dropped whole (see module docstring). Bounds the per-
# session pair fan-out at K²/2 and keeps the signal "focused co-work".
# Generous vs a typical focused stretch (a handful of notes); a bulk
# tag/edit run lands well above it.
_MAX_SESSION_NOTES = 25

# Allow-lists of "working on it" actions (so a *new* action defaults to
# not-counted). Verified against the real audit vocabulary:
#   - ``entity="note"``  : the note-level lifecycle/content touches.
#   - ``entity="note_part"`` : part-level content edits (resolved to the
#     owning note). ``create``/``delete`` excluded as genesis/removal.
# ``create``, ``archive``, ``delete``, ``restore``, ``gdpr_erase`` and the
# autonomous ``auto_*`` / ``maturity_tick`` actions are all excluded as
# genesis, removal or machine events. ``set_maturity`` is the *manual*
# maturity override (the automatic ``maturity_tick`` carries
# actor_kind='system' and is dropped by the actor filter).
_NOTE_TOUCH_ACTIONS: frozenset[str] = frozenset(
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
_PART_TOUCH_ACTIONS: frozenset[str] = frozenset({"update", "append", "prepend", "replace"})


async def _live_note_ids(session: AsyncSession, *, org_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(
        select(Note.id).where(Note.org_id == org_id, Note.deleted_at.is_(None))
    )
    return {r[0] for r in rows}


def _aggregate_sessions(
    events: list[tuple[uuid.UUID, uuid.UUID, datetime.datetime]],
) -> dict[tuple[uuid.UUID, uuid.UUID], tuple[int, datetime.datetime]]:
    """Fold ``(actor_id, note_id, ts)`` rows — pre-sorted by actor then
    ts — into ``{pair: (session_count, last_coactive_at)}``.

    A session is a maximal run of one actor's touches with no gap longer
    than ``COACTIVITY_SESSION_GAP``. Every distinct pair of notes touched
    in a session gets +1; the pair's ``last_coactive_at`` is the latest
    session end it appears in.
    """
    pairs: dict[tuple[uuid.UUID, uuid.UUID], tuple[int, datetime.datetime]] = {}

    cur_actor: uuid.UUID | None = None
    prev_ts: datetime.datetime | None = None
    # note_id -> latest ts within the current session (dedupes repeated
    # touches of the same note and dates the session by its last event).
    session_notes: dict[uuid.UUID, datetime.datetime] = {}

    def flush(notes: dict[uuid.UUID, datetime.datetime]) -> None:
        # < 2 notes: no pair. > cap: a bulk op, not co-work -> drop whole
        # (the N²/2 fake-clique guard, see module docstring).
        if len(notes) < 2 or len(notes) > _MAX_SESSION_NOTES:
            return
        session_end = max(notes.values())
        ids = sorted(notes.keys(), key=str)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pk = (ids[i], ids[j])  # already canonical: sorted by str
                count, last = pairs.get(pk, (0, session_end))
                pairs[pk] = (count + 1, max(last, session_end))

    for actor_id, note_id, ts in events:
        new_session = (
            actor_id != cur_actor or prev_ts is None or (ts - prev_ts) > COACTIVITY_SESSION_GAP
        )
        if new_session and session_notes:
            flush(session_notes)
            session_notes = {}
        if actor_id != cur_actor:
            cur_actor = actor_id
        session_notes[note_id] = ts
        prev_ts = ts
    if session_notes:
        flush(session_notes)
    return pairs


async def refresh_coactivity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> int:
    """Recompute the org's co-activity edges from the activity log over
    the rolling window and replace the stored rows. Returns the number of
    pair rows written. Idempotent: the same window yields the same rows.

    Runs offline (garden sweep). A pure projection — the org's previous
    rows are deleted and the current window re-materialised — so a pair
    that ages out of the window correctly disappears.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    since = now - datetime.timedelta(days=COACTIVITY_WINDOW_DAYS)

    # Note-level touches: entity_id is already the note id.
    note_rows = (
        await session.execute(
            select(ActivityLog.actor_id, ActivityLog.entity_id, ActivityLog.ts).where(
                ActivityLog.org_id == org_id,
                ActivityLog.entity == "note",
                ActivityLog.entity_id.is_not(None),
                ActivityLog.actor_id.is_not(None),
                ActivityLog.actor_kind != "system",
                ActivityLog.action.in_(_NOTE_TOUCH_ACTIONS),
                ActivityLog.ts >= since,
            )
        )
    ).all()
    # Part-level touches: resolve the part id back to its owning note. A
    # since-deleted part drops out of the join (its note is gone anyway).
    part_rows = (
        await session.execute(
            select(ActivityLog.actor_id, NotePart.note_id, ActivityLog.ts)
            .join(NotePart, NotePart.id == ActivityLog.entity_id)
            .where(
                ActivityLog.org_id == org_id,
                ActivityLog.entity == "note_part",
                ActivityLog.actor_id.is_not(None),
                ActivityLog.actor_kind != "system",
                ActivityLog.action.in_(_PART_TOUCH_ACTIONS),
                ActivityLog.ts >= since,
                NotePart.org_id == org_id,
            )
        )
    ).all()

    # Merge the two streams and sort by (actor, ts) so the fold can split
    # each actor's touches into sessions in one linear pass.
    events = [(r[0], r[1], r[2]) for r in note_rows] + [(r[0], r[1], r[2]) for r in part_rows]
    events.sort(key=lambda e: (str(e[0]), e[2]))
    pairs = _aggregate_sessions(events)

    # Drop pairs touching a note that no longer exists / was soft-deleted.
    live = await _live_note_ids(session, org_id=org_id)
    pairs = {pk: v for pk, v in pairs.items() if pk[0] in live and pk[1] in live}

    # Full per-org replace: the table is a projection of the window.
    await session.execute(delete(NoteCoactivity).where(NoteCoactivity.org_id == org_id))
    if pairs:
        session.add_all(
            NoteCoactivity(
                org_id=org_id,
                note_a_id=a,
                note_b_id=b,
                session_count=count,
                last_coactive_at=last,
            )
            for (a, b), (count, last) in pairs.items()
        )
        await session.flush()
    return len(pairs)


__all__ = [
    "COACTIVITY_SESSION_GAP",
    "COACTIVITY_WINDOW_DAYS",
    "refresh_coactivity",
]
