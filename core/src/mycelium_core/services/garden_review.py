"""Human-gated review state for AUTONOMOUSLY-generated nodes (ADR-0043,
task e87daff4).

The generalisation of "proposal-not-imposition" (ADR-0037, which gates
*facets* on existing nodes) to whole NODES. A node a generator produced
without the user asking -- today the humus a future autonomous sweep
distils/patterns/synthesises -- is born ``review_state='proposed'`` and is
withheld from every retrieval/listing surface until a human approves it. The
state lives on the node (``notes.review_state``, a cheap retrieval filter);
the approve/reject *events* ride the existing bus (ADR-0036, audit/learning);
neither layer is overloaded.

This module owns the review lifecycle:

- :func:`list_pending` -- the review inbox: the org's ``proposed`` notes, each
  carrying its ``origin_model_id`` so the human sees WHICH model produced the
  summary before deciding (Ollama 3B local != GPT != Scaleway).
- :func:`approve_node` -- ``proposed`` -> ``approved``; the note becomes
  effective. Audited; emits a bus ``commit`` event. Approving a
  distillation is also the one moment its source may become humus.
- :func:`reject_node` -- soft-delete the note (a reject never pollutes);
  audited; emits a bus ``reject`` event carrying ``origin_model_id``.
  Reversible via the normal recovery/restore path like any soft-delete.
  Rejecting a distillation takes the humus mark back off its source.

No ``'rejected'`` is ever stored on ``review_state`` -- a reject removes the
node, so a rejected summary never lingers. Member role; RLS-scoped.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.classification_feedback import ClassificationFeedback
from mycelium_core.models.event_outbox import EventOutbox
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.models.note_part import NotePart
from mycelium_core.services import annotations as annotations_svc
from mycelium_core.services import audit, event_bus, note_inert
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.rbac import require_role

_PROPOSED = "proposed"
#: ``payload["review"]`` of the event that takes a rejection back. It rides
#: on ``kind='propose'``: after the undo the node is a pending proposal
#: again, which is exactly what that kind means -- no new event kind, no
#: migration of the outbox CHECK, and every existing consumer renders it.
_UNREJECT = "unreject"
_APPROVED = "approved"
_PREVIEW_CHARS = 280
#: Recorded on the humus feedback rows so the learning loop can tell this
#: policy (a person's review decides the source) from the retired one, under
#: which the distiller flipped the source by itself (``auto_humus_v1``).
_SOURCE_HUMUS_POLICY = "humus_on_review_v1"


async def _distillation_sources(
    session: AsyncSession, *, org_id: uuid.UUID, atom: Note
) -> list[Note]:
    """The live notes a distillation was drawn from (its ``hypha_of``
    parents). Empty for any other kind of node: a pattern or a season reads
    its sources but never decides anything about them."""
    if atom.humus_kind != "distillation":
        return []
    return list(
        (
            await session.execute(
                select(Note)
                .join(NoteNoteLink, NoteNoteLink.parent_note_id == Note.id)
                .where(
                    NoteNoteLink.org_id == org_id,
                    NoteNoteLink.child_note_id == atom.id,
                    NoteNoteLink.kind == "hypha_of",
                    Note.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


async def _set_source_humus(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source: Note,
    atom: Note,
    value: bool,
) -> None:
    """Set or clear a source's humus mark because a person reviewed ``atom``.

    The update matches only while the flag still holds the opposite value and
    the version is the one just read, so a repeat or a concurrent write is a
    skip, never a second mutation. When it fires it leaves the same two
    traces every garden mutation does: an audit row naming the distillation
    that justified it, and an append-only feedback row for the learning
    loop, with the reviewer's verdict as the action."""
    row = (
        await session.execute(
            update(Note)
            .where(
                Note.id == source.id,
                Note.version == source.version,
                Note.humus_flag.is_(not value),
            )
            .values(humus_flag=value, version=Note.version + 1)
            .returning(Note.version)
        )
    ).first()
    if row is None:
        return
    await session.refresh(source)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=source.id,
        action="garden_review:source_humus",
        diff={
            "humus_flag": {"old": not value, "new": value},
            "distilled_note_id": str(atom.id),
        },
    )
    session.add(
        ClassificationFeedback(
            org_id=org_id,
            user_id=actor_id,
            node_id=source.id,
            suggestion_type="humus",
            suggestion_value={"humus_flag": value},
            action="accept" if value else "reject",
            model_version=_SOURCE_HUMUS_POLICY,
            signals_snapshot={
                "trigger": "review",
                "distilled_note_id": str(atom.id),
                "distill_model_id": atom.origin_model_id,
            },
        )
    )
    await session.flush()


async def _mark_sources_on_approve(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, atom: Note
) -> None:
    """An approved distillation stands in for its source, so the source
    becomes humus -- but only if it is still inert. The person may have taken
    the note up again while the summary waited for review, and a live note is
    never demoted behind a summary of an older state of it."""
    for src in await _distillation_sources(session, org_id=org_id, atom=atom):
        if await note_inert.is_inert(session, note=src):
            await _set_source_humus(
                session, org_id=org_id, actor_id=actor_id, source=src, atom=atom, value=True
            )


async def _clear_sources_on_reject(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, atom: Note
) -> None:
    """A rejected or withdrawn distillation no longer justifies its source's
    humus mark, so the mark comes off -- unless another distillation of the
    same source is still effective. Also repairs the state the old distiller
    left behind, which marked the source when the summary was created."""
    for src in await _distillation_sources(session, org_id=org_id, atom=atom):
        if not src.humus_flag:
            continue
        other = NoteNoteLink.__table__.alias("other_link")
        still_justified = (
            await session.execute(
                select(
                    exists().where(
                        other.c.org_id == org_id,
                        other.c.parent_note_id == src.id,
                        other.c.kind == "hypha_of",
                        other.c.child_note_id != atom.id,
                        other.c.child_note_id == Note.id,
                        Note.humus_kind == "distillation",
                        Note.deleted_at.is_(None),
                        Note.review_state.is_distinct_from(_PROPOSED),
                    )
                )
            )
        ).scalar_one()
        if not still_justified:
            await _set_source_humus(
                session, org_id=org_id, actor_id=actor_id, source=src, atom=atom, value=False
            )


@dataclass(frozen=True)
class PendingNode:
    """One node awaiting human review (the inbox row). ``origin_model_id`` is
    the transparency requirement: the human sees the producing model before
    approving/rejecting."""

    note_id: uuid.UUID
    title: str | None
    humus_kind: str | None
    origin_model_id: str | None
    preview: str
    created_at: dt.datetime
    # The optimistic-concurrency pin the reviewer must echo back on
    # approve/reject: guarantees the human approves the content they SAW
    # (TOCTOU guard, task 2e36e732).
    version: int


async def list_pending(
    session: AsyncSession, *, org_id: uuid.UUID, limit: int = 50
) -> list[PendingNode]:
    """The review inbox: the org's ``proposed`` (autonomously generated,
    not-yet-approved) notes, newest first, with a body preview + the model
    that produced each. RLS-scoped; a pure read (no event)."""
    rows = (
        (
            await session.execute(
                select(Note)
                .where(
                    Note.org_id == org_id,
                    Note.review_state == _PROPOSED,
                    Note.deleted_at.is_(None),
                )
                .order_by(Note.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    out: list[PendingNode] = []
    for n in rows:
        body = await notes_svc.get_body(session, note_id=n.id)
        out.append(
            PendingNode(
                note_id=n.id,
                title=n.title,
                humus_kind=n.humus_kind,
                origin_model_id=n.origin_model_id,
                preview=(body or "").strip()[:_PREVIEW_CHARS],
                created_at=n.created_at,
                version=n.version,
            )
        )
    return out


@dataclass(frozen=True)
class RejectedNode:
    """One REJECTED proposal: the review bin's row. ``reject_node`` rejects
    by soft-deleting while leaving ``review_state='proposed'``, and that
    pair of states is invisible everywhere -- the inbox filters
    ``deleted_at IS NULL``, the trash filters the proposed leg -- so
    without this list an undo is unreachable even though it works."""

    note_id: uuid.UUID
    title: str | None
    humus_kind: str | None
    origin_model_id: str | None
    preview: str
    created_at: dt.datetime
    #: ``notes.deleted_at``: non-NULL by construction, this IS the moment
    #: the human declined it.
    rejected_at: dt.datetime
    #: The same TOCTOU pin ``PendingNode`` carries, to echo back to the
    #: restore that undoes the rejection.
    version: int


async def list_rejected(
    session: AsyncSession, *, org_id: uuid.UUID, limit: int = 50
) -> list[RejectedNode]:
    """The review BIN: proposals a human declined, most recently rejected
    first. Mirror of :func:`list_pending` on the other leg of the
    perimeter (``deleted_at IS NOT NULL`` instead of ``IS NULL``).

    The undo is not a verb of its own: it is ``notes.restore_note``, which
    accepts exactly this state and puts the node back in the inbox. This
    list is what makes that reachable. RLS-scoped; a pure read.

    These rows are soft-deleted notes like any other, so emptying the
    workspace trash purges them too even though that view does not show
    them, and the retention sweep reaches them on its own schedule.
    """
    rows = (
        (
            await session.execute(
                select(Note)
                .where(
                    Note.org_id == org_id,
                    Note.review_state == _PROPOSED,
                    Note.deleted_at.is_not(None),
                )
                # Most recently declined first: the order an undo is looked
                # for in, and the one ``note_parts.list_trashed`` uses for
                # the other bin. The id tiebreak keeps paging stable.
                .order_by(Note.deleted_at.desc(), Note.id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    out: list[RejectedNode] = []
    for n in rows:
        body = await notes_svc.get_body(session, note_id=n.id)
        # Non-NULL by the WHERE above; the fallback keeps the type honest.
        rejected_at = n.deleted_at or n.created_at
        out.append(
            RejectedNode(
                note_id=n.id,
                title=n.title,
                humus_kind=n.humus_kind,
                origin_model_id=n.origin_model_id,
                preview=(body or "").strip()[:_PREVIEW_CHARS],
                created_at=n.created_at,
                rejected_at=rejected_at,
                version=n.version,
            )
        )
    return out


async def _load_proposed(session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID) -> Note:
    """Load a note for review by id (bypassing the proposed-exclusion the
    retrieval surfaces apply). 404 if it does not exist in this org or is
    already soft-deleted."""
    note = await session.get(Note, note_id)
    if note is None or note.org_id != org_id or note.deleted_at is not None:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return note


@dataclass(frozen=True)
class Decision:
    """A person's decision on a proposed node, carried out by someone else.

    Recorded on the audit row and the bus event of the approve or reject, so
    the two roles stay apart: ``decided_by`` is the person, ``executed_by``
    the agent that ran it, ``comment_id`` where the person wrote it."""

    decided_by_identity_id: uuid.UUID
    comment_id: uuid.UUID
    executed_by_identity_id: uuid.UUID | None

    def record(self) -> dict[str, str | None]:
        return {
            "decided_by_identity_id": str(self.decided_by_identity_id),
            "decision_comment_id": str(self.comment_id),
            "executed_by_identity_id": (
                str(self.executed_by_identity_id) if self.executed_by_identity_id else None
            ),
        }


async def _verify_review_decision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    node: Note,
    decision_comment_id: uuid.UUID,
    verb: str,
) -> uuid.UUID:
    """Check that ``decision_comment_id`` is a person's decision to ``verb``
    (``approve`` or ``reject``) ``node``, and return that person's identity.

    The checks on who wrote the comment are the ones a suggestion's decision
    passes (ADR-0066, :func:`annotations.person_who_wrote`). What differs is
    where the comment lives. A proposed node cannot carry a comment -- its
    text must not leave the review perimeter, and a comment quotes the text
    it hangs on -- so the decision is written on one of its SOURCES, the
    notes it was generated from, which the person can read. On top of the
    writer checks:

    - the comment is on a live ``hypha_of`` parent of the node;
    - it was written after the node, so it cannot predate what it decides;
    - it names the node on a ``verb`` line, does not also name it on the
      opposite line, and the name matches no other node generated from the
      same source, so one decision cannot be spent on another node or as
      the opposite verdict.

    A ``reason`` names the clause that failed; nothing is changed."""

    def refuse(reason: str) -> DomainError:
        return DomainError(MessageCode.GARDEN_REVIEW_DECISION_INVALID, reason=reason, verb=verb)

    dec = await annotations_svc.load_decision_comment(
        session, org_id=org_id, comment_id=decision_comment_id, refuse=refuse
    )
    source_id = None
    if dec.doc_kind == "note_part" and dec.note_part_id is not None:
        source_id = await session.scalar(
            select(NotePart.note_id).where(NotePart.id == dec.note_part_id)
        )
    is_source = source_id is not None and await session.scalar(
        select(
            exists().where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.parent_note_id == source_id,
                NoteNoteLink.child_note_id == node.id,
                NoteNoteLink.kind == "hypha_of",
            )
        )
    )
    if not is_source:
        raise refuse("not_on_a_source")
    if dec.created_at <= node.created_at:
        raise refuse("before_proposal")

    decided_by = await annotations_svc.person_who_wrote(
        session, org_id=org_id, dec=dec, refuse=refuse
    )

    mine = node.id.hex
    hits = [p for p in annotations_svc.named_prefixes(dec.body, verb) if mine.startswith(p)]
    if not hits:
        raise refuse("node_not_named")
    opposite = "reject" if verb == "approve" else "approve"
    if any(mine.startswith(p) for p in annotations_svc.named_prefixes(dec.body, opposite)):
        raise refuse("contradictory")
    siblings = (
        await session.execute(
            select(NoteNoteLink.child_note_id).where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.parent_note_id == source_id,
                NoteNoteLink.kind == "hypha_of",
                NoteNoteLink.child_note_id != node.id,
            )
        )
    ).scalars()
    if any(o.hex.startswith(p) for o in siblings for p in hits):
        raise refuse("ambiguous_id")
    return decided_by


async def approve_node(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int | None = None,
    decision: Decision | None = None,
) -> Note:
    """Approve a ``proposed`` node: ``review_state`` -> ``'approved'`` so it
    becomes effective (eligible for every retrieval/listing surface).
    Audited; emits a bus ``commit`` event (idempotent on the note id). A
    re-approve of an already-approved node is a no-op (idempotent); a node
    that was never proposed is 404 (it is not a pending proposal).

    ``expected_version`` is the TOCTOU guard (task 2e36e732): pass the
    ``version`` the reviewer READ (``list_pending`` serves it) and the
    approve fails with ``stale_version`` if the node changed in between --
    the human never blesses content they have not seen. The check runs
    BEFORE the idempotent short-circuit on purpose: a stale view is an
    error even when the end state would coincide. ``None`` keeps the
    legacy unguarded behaviour (idempotent MCP retries included).

    ``decision`` is set only by :func:`approve_node_on_decision`: the person
    who decided and where, recorded next to the executor."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await _load_proposed(session, org_id=org_id, note_id=note_id)
    if expected_version is not None and note.version != expected_version:
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
    if note.review_state == _APPROVED:
        return note  # idempotent: already approved
    if note.review_state != _PROPOSED:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    note.review_state = _APPROVED
    note.version += 1
    await session.flush()
    await _mark_sources_on_approve(session, org_id=org_id, actor_id=actor_id, atom=note)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="garden_review:approve",
        diff={
            "review_state": {"old": _PROPOSED, "new": _APPROVED},
            **(decision.record() if decision else {}),
        },
    )
    actor_kind = await event_bus.session_actor_kind(session)
    await event_bus.emit_event(
        session,
        org_id=org_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        kind="commit",
        node_kind="note",
        node_id=note_id,
        payload={
            "review": "approve",
            "origin_model_id": note.origin_model_id,
            "humus_kind": note.humus_kind,
            **(decision.record() if decision else {}),
        },
        applied_state="committed",
        # Versioned: a node decided again after an un-reject is a NEW
        # decision, not a retry the 24h window should swallow. A true retry
        # never reaches here -- the idempotent short-circuit above returns
        # first -- so nothing is double-counted.
        idempotency_key=f"garden_review:approve:{note_id}:{note.version}",
    )
    return note


async def reject_node(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    reason: str | None = None,
    expected_version: int | None = None,
    attribute_to_model: bool = True,
    decision: Decision | None = None,
) -> Note:
    """Reject a ``proposed`` node: soft-delete it (it never pollutes the
    corpus) and emit a bus ``reject`` event carrying ``origin_model_id`` (so
    the per-model accept-ratio / earned-autonomy signal, ADR-0043 D4, can be
    derived later). Reversible via the normal restore path. Idempotent if
    already soft-deleted; 404 if the node was never proposed.

    ``expected_version`` mirrors :func:`approve_node` (TOCTOU guard, task
    2e36e732): checked before the idempotent short-circuit, ``None`` =
    legacy unguarded behaviour.

    ``attribute_to_model=False`` drops ``origin_model_id`` from the event,
    which is what the accept-ratio groups by, so the rejection stays in the
    audit trail without counting against the model. It is for the one
    caller that retires a node for a reason that is not a verdict on what
    the model wrote: :func:`restore_source` withdrawing an atom to bring
    its sources back up. Charging that to the model would make a
    provenance decision read as a quality signal.

    ``decision`` mirrors :func:`approve_node`."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await session.get(Note, note_id)
    if note is None or note.org_id != org_id:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    if expected_version is not None and note.version != expected_version:
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
    if note.deleted_at is not None:
        return note  # idempotent: already rejected/removed
    if note.review_state != _PROPOSED:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    note.deleted_at = dt.datetime.now(dt.UTC)
    note.version += 1
    await session.flush()
    await _clear_sources_on_reject(session, org_id=org_id, actor_id=actor_id, atom=note)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="garden_review:reject",
        diff={
            **({"reason": reason} if reason else {}),
            **(decision.record() if decision else {}),
        }
        or None,
    )
    actor_kind = await event_bus.session_actor_kind(session)
    await event_bus.emit_event(
        session,
        org_id=org_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        kind="reject",
        node_kind="note",
        node_id=note_id,
        payload={
            "review": "reject",
            "origin_model_id": note.origin_model_id if attribute_to_model else None,
            "humus_kind": note.humus_kind,
            "reason": reason,
            **(decision.record() if decision else {}),
        },
        applied_state="rejected",
        # Versioned, same reason as approve: after an un-reject, rejecting
        # again must count again.
        idempotency_key=f"garden_review:reject:{note_id}:{note.version}",
    )
    return note


async def approve_node_on_decision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
    decision_comment_id: uuid.UUID,
    executed_by_identity_id: uuid.UUID | None,
) -> Note:
    """Approve a proposed node that a PERSON decided to approve, on their
    behalf.

    Approving is a person's act, and the plain :func:`approve_node` stays
    out of every scoped credential's reach. This is how an agent carries out
    a decision the person already wrote: it cites the comment, the server
    verifies it (:func:`_verify_review_decision`), and only then runs the
    same approve the UI runs, with the same stale check. An instruction the
    agent merely reports would not do: the server cannot tell a real one
    from an invented one.

    ``expected_version`` is required here, not optional: the agent must
    have read the node it is acting on."""
    node = await _load_proposed(session, org_id=org_id, note_id=note_id)
    if node.version != expected_version:
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
    decided_by = await _verify_review_decision(
        session, org_id=org_id, node=node, decision_comment_id=decision_comment_id, verb="approve"
    )
    return await approve_node(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        decision=Decision(decided_by, decision_comment_id, executed_by_identity_id),
    )


async def reject_node_on_decision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
    decision_comment_id: uuid.UUID,
    executed_by_identity_id: uuid.UUID | None,
) -> Note:
    """Reject a proposed node that a PERSON decided to reject, on their
    behalf. The mirror of :func:`approve_node_on_decision`: the comment
    must name the node on a ``reject`` line. The reason is the person's
    comment, which the audit row points to."""
    node = await _load_proposed(session, org_id=org_id, note_id=note_id)
    if node.version != expected_version:
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
    decided_by = await _verify_review_decision(
        session, org_id=org_id, node=node, decision_comment_id=decision_comment_id, verb="reject"
    )
    return await reject_node(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        decision=Decision(decided_by, decision_comment_id, executed_by_identity_id),
    )


async def record_unreject(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, note: Note
) -> None:
    """Emit the event that takes a rejection back, for a note whose restore
    has just put it back in the review inbox.

    The reliability signal (ADR-0043 D4) counts EVENTS, so a rejection the
    human undid has to leave its own mark or the model keeps paying for a
    decision that no longer stands -- and, once the node is approved on the
    second look, pays twice over.

    It rides on ``kind='propose'`` rather than a new kind: after the undo
    the node IS a pending proposal again, which is what that kind means in
    ADR-0036's vocabulary, and ``payload['review']`` is what the ratio
    query actually discriminates on. The alternative would cost a
    migration of the outbox CHECK, the ``EventKind`` literal, the API
    schema and the generated SPA types to say something the existing
    vocabulary already says. ``applied_state`` stays NULL: nobody
    adjudicated anything, the node is pending again.

    ``parent_event_id`` points at the reject being undone, so the pair is
    readable without archaeology on the payload -- and that same lookup is
    where the attribution comes from, because an undo has to mirror the
    rejection it cancels rather than re-derive it. The idempotency key
    carries the version, so a second undo after a second reject is its own
    event rather than a duplicate swallowed by the 24h window.

    It rides a WRITE kind, so an agent restoring at more than the bus quota
    would meet a 429 on a restore -- a restore by an agent is a write and
    belongs under the same anti-runaway cap; humans are not capped.

    Lives here, not in ``notes``: this is review vocabulary. ``restore_note``
    calls it because it is the only door that clears ``deleted_at``, and it
    already knows -- by hand, on the one state that means it -- that this
    particular restore is an un-reject.
    """
    row = (
        await session.execute(
            select(EventOutbox.id, EventOutbox.payload["origin_model_id"].astext)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.node_id == note.id,
                EventOutbox.payload["review"].astext == "reject",
            )
            # Newest first, id as the tiebreak: two events emitted inside one
            # transaction share ``ts`` (Postgres ``now()`` is the transaction
            # clock), so the order needs something else to be total.
            .order_by(EventOutbox.ts.desc(), EventOutbox.id.desc())
            .limit(1)
        )
    ).first()
    parent, attributed_model = (row[0], row[1]) if row is not None else (None, None)
    actor_kind = await event_bus.session_actor_kind(session)
    await event_bus.emit_event(
        session,
        org_id=org_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        kind="propose",
        node_kind="note",
        node_id=note.id,
        parent_event_id=parent,
        payload={
            # MIRROR the rejection being undone, do not re-derive it. A
            # withdrawal that was deliberately not charged to the model
            # (``restore_source``) carries no model id, and an undo that
            # named one anyway would subtract a rejection the model never
            # got -- cancelling a real one from another node.
            "review": _UNREJECT,
            "origin_model_id": attributed_model,
            "humus_kind": note.humus_kind,
        },
        idempotency_key=f"garden_review:unreject:{note.id}:{note.version}",
    )


# ── D4 earned-autonomy telemetry: per-model accept ratio ───────────────────


@dataclass(frozen=True)
class ModelAcceptRatio:
    """How reliably a model's AUTONOMOUSLY-generated proposals were accepted
    (ADR-0043 D4): ``approved / (approved + rejected)`` for one
    ``origin_model_id``. ``ratio`` is None when there are no decisions yet --
    an empty denominator is "no signal", not 0%. This is the reliability
    signal that a future per-workspace policy can use to *earn* a model
    auto-approve (never assumed)."""

    model_id: str
    approved: int
    rejected: int
    ratio: float | None


async def accept_ratio_by_model(
    session: AsyncSession, *, org_id: uuid.UUID
) -> list[ModelAcceptRatio]:
    """Per-model accept ratio over the review bus events. Reads the durable
    approve/reject events ``approve_node`` / ``reject_node`` emit (each
    carrying ``origin_model_id`` in its payload): a reject soft-deletes the
    node, so the EVENTS -- not the note table -- are the complete record of
    both outcomes. Most-decided model first. RLS-scoped.

    A rejection the human then TOOK BACK is not a rejection: restoring a
    rejected proposal emits its own compensating event, and each one
    cancels a reject for that model. Without the subtraction the sequence
    reject -> restore -> approve would charge the model a decision that
    was undone AND credit it with the approval, which is a worse reading
    of reliability than either outcome alone."""
    model = EventOutbox.payload["origin_model_id"].astext
    review = EventOutbox.payload["review"].astext
    rows = (
        await session.execute(
            select(
                model,
                func.count().filter(review == "approve"),
                func.count().filter(review == "reject"),
                func.count().filter(review == _UNREJECT),
            )
            .where(
                EventOutbox.org_id == org_id,
                review.in_(("approve", "reject", _UNREJECT)),
                model.isnot(None),
            )
            .group_by(model)
        )
    ).all()
    out: list[ModelAcceptRatio] = []
    for model_id, approved, rejected, undone in rows:
        # A count of decisions never goes below zero. With the attribution
        # mirrored and the decision keys versioned there should be no undo
        # without its reject, so this is the floor of a metric, not a
        # correction: if it ever bites, the pairing upstream is broken.
        a, r = int(approved), max(0, int(rejected) - int(undone))
        total = a + r
        out.append(
            ModelAcceptRatio(
                model_id=model_id,
                approved=a,
                rejected=r,
                ratio=round(a / total, 4) if total else None,
            )
        )
    out.sort(key=lambda m: m.approved + m.rejected, reverse=True)
    return out


# ── Fase P (task 561c6aca): the hypha_of chain IS the stack ────────────────


@dataclass(frozen=True)
class RestoreSourceResult:
    """Outcome of :func:`restore_source`: the atom retired and the preserved
    sources brought back to life. ``restored_source_ids`` are the sources this
    call actually un-archived (an already-live source needs no restore)."""

    atom_note_id: uuid.UUID
    source_ids: list[uuid.UUID]
    restored_source_ids: list[uuid.UUID]
    atom_retired: bool


async def restore_source(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    atom_note_id: uuid.UUID,
    reason: str | None = None,
) -> RestoreSourceResult:
    """Design §6 ("ripristina"): choose the layer under the lens. Un-archive
    the atom's ``hypha_of`` sources (the originals are preserved, never
    mutated -- invariant 8a26c000) and retire the atom. Nothing is ever
    hard-deleted.

    Retiring an EFFECTIVE (approved) atom has no direct house verb --
    ``reject_node`` only accepts ``review_state='proposed'``, and archiving
    would NOT retire it (archived blobs stay retrievable; only the
    ``proposed`` filter withholds blobs at the retrieval layer, see
    ``proposed_note_blob_exclusion``). So an approved atom is first demoted
    back to ``proposed`` (audited, the exact inverse of ``approve_node``) and
    then rejected: the end state (``review_state='proposed'`` +
    ``deleted_at``) is byte-identical to a normal reject -- withheld from
    retrieval, hidden from listings, reversible via the normal restore path.

    Idempotent: an already-retired atom skips the retire step but still
    revives any archived source."""
    await require_role(session, org_id, actor_id, Role.member)
    atom = await session.get(Note, atom_note_id)
    if atom is None or atom.org_id != org_id:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    if not atom.humus_flag:
        # Only a humus atom is a lens over preserved sources.
        raise DomainError(MessageCode.DOMAIN_ERROR)
    sources = list(
        (
            await session.execute(
                select(Note)
                .join(NoteNoteLink, NoteNoteLink.parent_note_id == Note.id)
                .where(
                    NoteNoteLink.org_id == org_id,
                    NoteNoteLink.child_note_id == atom_note_id,
                    NoteNoteLink.kind == "hypha_of",
                    Note.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not sources:
        # An atom without hypha_of provenance has no layer to restore.
        raise DomainError(MessageCode.DOMAIN_ERROR)
    restored: list[uuid.UUID] = []
    for src in sources:
        if src.is_archived:
            await notes_svc.archive_note(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=src.id,
                expected_version=src.version,
                archived=False,
            )
            restored.append(src.id)
    retired = False
    if atom.deleted_at is None:
        if atom.review_state == _APPROVED:
            atom.review_state = _PROPOSED
            atom.version += 1
            await session.flush()
            await audit.log(
                session,
                org_id=org_id,
                actor_id=actor_id,
                entity="note",
                entity_id=atom_note_id,
                action="garden_review:demote",
                diff={"review_state": {"old": _APPROVED, "new": _PROPOSED}},
            )
        # Pin the version this flow just observed (post-demote): the same
        # TOCTOU guard reviewers get, applied to the internal composition
        # (task 2e36e732).
        await reject_node(
            session,
            org_id=org_id,
            actor_id=actor_id,
            note_id=atom_note_id,
            reason=reason or "restore_source",
            expected_version=atom.version,
            # Retiring the lens to bring its sources back up is a choice
            # about which layer to look at, not a verdict on the model that
            # wrote the atom: it stays in the audit trail and out of the
            # accept ratio.
            attribute_to_model=False,
        )
        retired = True
    return RestoreSourceResult(
        atom_note_id=atom_note_id,
        source_ids=[s.id for s in sources],
        restored_source_ids=restored,
        atom_retired=retired,
    )


async def accept_ratio_overall(session: AsyncSession, *, org_id: uuid.UUID) -> float | None:
    """The workspace-wide accept ratio over every model's autonomous proposals
    (the aggregate of :func:`accept_ratio_by_model`). None when there are no
    decisions yet -- the garden-health sensor renders that as "no signal"."""
    by_model = await accept_ratio_by_model(session, org_id=org_id)
    approved = sum(m.approved for m in by_model)
    rejected = sum(m.rejected for m in by_model)
    total = approved + rejected
    if not total:
        return None
    return round(approved / total, 4)


__all__ = [
    "Decision",
    "ModelAcceptRatio",
    "PendingNode",
    "RejectedNode",
    "accept_ratio_by_model",
    "accept_ratio_overall",
    "approve_node",
    "approve_node_on_decision",
    "list_pending",
    "list_rejected",
    "reject_node",
    "reject_node_on_decision",
]
