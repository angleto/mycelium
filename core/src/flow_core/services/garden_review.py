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
  effective. Audited; emits a bus ``commit`` event.
- :func:`reject_node` -- soft-delete the note (a reject never pollutes);
  audited; emits a bus ``reject`` event carrying ``origin_model_id``.
  Reversible via the normal recovery/restore path like any soft-delete.

No ``'rejected'`` is ever stored on ``review_state`` -- a reject removes the
node, so a rejected summary never lingers. Member role; RLS-scoped.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.event_outbox import EventOutbox
from flow_core.models.membership import Role
from flow_core.models.note import Note
from flow_core.services import audit, event_bus
from flow_core.services import notes as notes_svc
from flow_core.services.rbac import require_role

_PROPOSED = "proposed"
_APPROVED = "approved"
_PREVIEW_CHARS = 280


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


async def approve_node(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, note_id: uuid.UUID
) -> Note:
    """Approve a ``proposed`` node: ``review_state`` -> ``'approved'`` so it
    becomes effective (eligible for every retrieval/listing surface).
    Audited; emits a bus ``commit`` event (idempotent on the note id). A
    re-approve of an already-approved node is a no-op (idempotent); a node
    that was never proposed is 404 (it is not a pending proposal)."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await _load_proposed(session, org_id=org_id, note_id=note_id)
    if note.review_state == _APPROVED:
        return note  # idempotent: already approved
    if note.review_state != _PROPOSED:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    note.review_state = _APPROVED
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="garden_review:approve",
        diff={"review_state": {"old": _PROPOSED, "new": _APPROVED}},
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
        },
        applied_state="committed",
        idempotency_key=f"garden_review:approve:{note_id}",
    )
    return note


async def reject_node(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    reason: str | None = None,
) -> Note:
    """Reject a ``proposed`` node: soft-delete it (it never pollutes the
    corpus) and emit a bus ``reject`` event carrying ``origin_model_id`` (so
    the per-model accept-ratio / earned-autonomy signal, ADR-0043 D4, can be
    derived later). Reversible via the normal restore path. Idempotent if
    already soft-deleted; 404 if the node was never proposed."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await session.get(Note, note_id)
    if note is None or note.org_id != org_id:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    if note.deleted_at is not None:
        return note  # idempotent: already rejected/removed
    if note.review_state != _PROPOSED:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    note.deleted_at = dt.datetime.now(dt.UTC)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="garden_review:reject",
        diff={"reason": reason} if reason else None,
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
            "origin_model_id": note.origin_model_id,
            "humus_kind": note.humus_kind,
            "reason": reason,
        },
        applied_state="rejected",
        idempotency_key=f"garden_review:reject:{note_id}",
    )
    return note


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
    both outcomes. Most-decided model first. RLS-scoped."""
    model = EventOutbox.payload["origin_model_id"].astext
    review = EventOutbox.payload["review"].astext
    rows = (
        await session.execute(
            select(
                model,
                func.count().filter(review == "approve"),
                func.count().filter(review == "reject"),
            )
            .where(
                EventOutbox.org_id == org_id,
                review.in_(("approve", "reject")),
                model.isnot(None),
            )
            .group_by(model)
        )
    ).all()
    out: list[ModelAcceptRatio] = []
    for model_id, approved, rejected in rows:
        a, r = int(approved), int(rejected)
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
    "ModelAcceptRatio",
    "PendingNode",
    "accept_ratio_by_model",
    "accept_ratio_overall",
    "approve_node",
    "list_pending",
    "reject_node",
]
