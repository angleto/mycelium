"""Links nobody wrote are proposals, judged by a person (ADR-0073 decision 7).

A proposer is anything that suggests a note-to-note link: the built-in
proposers in :mod:`link_proposers`, run by the garden loop, or any identity
calling ``garden.link_proposals.submit`` over the public API. They all go
through :func:`submit`, and the core works fully with none but its own.

The rules this module enforces, each a sentence of the ADR:

* a proposal is born ``proposed`` and takes effect only when a person
  accepts it (:func:`accept`, a person's act on every surface);
* accepting creates a typed link of the kind the person picks and never
  edits a body; the link keeps the proposer and its evidence reference
  beside the person who accepted it;
* a rejection is remembered per pair, and no proposer proposes that pair
  again (:func:`submit` refuses it); a person can still link it by hand;
* every decision is attributed to the proposer, so each has a measured
  precision with its interval (:func:`precision`);
* each showing of a proposal to a person is recorded (:func:`record_shown`),
  and a proposal left undecided expires as such (:func:`expire`);
* a proposer has at most ``OPEN_PER_NOTE`` open proposals on a note, so a
  noisy one cannot bury the queue while its precision is measured;
* the score orders the queue and is never returned with a proposal.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import ConflictError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.link_proposal import (
    LinkProposal,
    LinkProposalExposure,
    LinkRejection,
)
from mycelium_core.models.note import Note
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.services import eval_stats
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services.note_effective import effective_note_clause

#: Open proposals one proposer may hold on one note (as source or target).
OPEN_PER_NOTE = 3
#: An undecided proposal older than this expires, and is recorded as expired.
TTL_DAYS = 30


def _canonical(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) < str(b) else (b, a)


def identity_proposer(actor_id: uuid.UUID) -> str:
    """The proposer name of an identity calling the API."""
    return f"identity:{actor_id}"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """``proposal_id`` is set when a proposal is open for the pair after the
    call; otherwise ``skipped`` says why nothing was opened."""

    proposal_id: uuid.UUID | None
    skipped: str | None


async def _pair_state(
    session: AsyncSession, *, org_id: uuid.UUID, a: uuid.UUID, b: uuid.UUID
) -> str | None:
    """Why the pair cannot be proposed, or None when it can."""
    ca, cb = _canonical(a, b)
    rejected = (
        await session.execute(
            select(LinkRejection.id).where(
                LinkRejection.org_id == org_id,
                LinkRejection.note_a_id == ca,
                LinkRejection.note_b_id == cb,
            )
        )
    ).first()
    if rejected is not None:
        return "rejected"
    linked = (
        await session.execute(
            select(NoteNoteLink.id).where(
                NoteNoteLink.org_id == org_id,
                or_(
                    and_(NoteNoteLink.parent_note_id == a, NoteNoteLink.child_note_id == b),
                    and_(NoteNoteLink.parent_note_id == b, NoteNoteLink.child_note_id == a),
                ),
            )
        )
    ).first()
    if linked is not None:
        return "linked"
    effective = (
        await session.execute(
            select(func.count()).where(Note.id.in_([a, b]), effective_note_clause())
        )
    ).scalar_one()
    if effective != 2:
        return "not_effective"
    return None


async def submit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    proposer: str,
    source_note_id: uuid.UUID,
    target_note_id: uuid.UUID,
    evidence: dict[str, Any] | None = None,
    evidence_ref: str | None = None,
    score: float | None = None,
) -> SubmitResult:
    """Open a proposal, or say why not. Idempotent per (proposer, source,
    target): a second submission of an open proposal returns it."""
    if source_note_id == target_note_id:
        return SubmitResult(None, "self")
    why = await _pair_state(session, org_id=org_id, a=source_note_id, b=target_note_id)
    if why is not None:
        return SubmitResult(None, why)
    open_on_note = (
        await session.execute(
            select(func.count()).where(
                LinkProposal.org_id == org_id,
                LinkProposal.proposer == proposer,
                LinkProposal.status == "proposed",
                or_(
                    LinkProposal.source_note_id.in_([source_note_id, target_note_id]),
                    LinkProposal.target_note_id.in_([source_note_id, target_note_id]),
                ),
                ~and_(
                    LinkProposal.source_note_id == source_note_id,
                    LinkProposal.target_note_id == target_note_id,
                ),
            )
        )
    ).scalar_one()
    if open_on_note >= OPEN_PER_NOTE:
        return SubmitResult(None, "bound")
    await session.execute(
        pg_insert(LinkProposal)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            proposer=proposer,
            source_note_id=source_note_id,
            target_note_id=target_note_id,
            evidence=evidence or {},
            evidence_ref=evidence_ref,
            score=score,
        )
        .on_conflict_do_nothing(
            index_elements=["org_id", "proposer", "source_note_id", "target_note_id"],
            index_where=LinkProposal.status == "proposed",
        )
    )
    pid = (
        await session.execute(
            select(LinkProposal.id).where(
                LinkProposal.org_id == org_id,
                LinkProposal.proposer == proposer,
                LinkProposal.source_note_id == source_note_id,
                LinkProposal.target_note_id == target_note_id,
                LinkProposal.status == "proposed",
            )
        )
    ).scalar_one()
    return SubmitResult(pid, None)


@dataclass(frozen=True, slots=True)
class QueueItem:
    """A proposal as a person sees it: no score."""

    proposal_id: uuid.UUID
    proposer: str
    source_note_id: uuid.UUID
    target_note_id: uuid.UUID
    evidence: dict[str, Any]
    created_at: datetime.datetime


async def queue(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID | None = None,
    limit: int = 20,
) -> list[QueueItem]:
    """Open proposals, ordered by score (best first) then age, both ends
    effective. ``note_id`` narrows to proposals touching that note."""
    src = Note.__table__.alias("src")
    dst = Note.__table__.alias("dst")
    q = (
        select(LinkProposal)
        .join(src, src.c.id == LinkProposal.source_note_id)
        .join(dst, dst.c.id == LinkProposal.target_note_id)
        .where(
            LinkProposal.org_id == org_id,
            LinkProposal.status == "proposed",
            src.c.deleted_at.is_(None),
            dst.c.deleted_at.is_(None),
            src.c.review_state.is_distinct_from("proposed"),
            dst.c.review_state.is_distinct_from("proposed"),
        )
        .order_by(LinkProposal.score.desc().nulls_last(), LinkProposal.created_at)
        .limit(limit)
    )
    if note_id is not None:
        q = q.where(
            or_(LinkProposal.source_note_id == note_id, LinkProposal.target_note_id == note_id)
        )
    rows = (await session.execute(q)).scalars().all()
    return [
        QueueItem(
            proposal_id=r.id,
            proposer=r.proposer,
            source_note_id=r.source_note_id,
            target_note_id=r.target_note_id,
            evidence=dict(r.evidence or {}),
            created_at=r.created_at,
        )
        for r in rows
    ]


async def record_shown(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    surface: str,
    proposal_ids: list[uuid.UUID],
) -> int:
    """Record that these proposals were shown to this person, on this
    surface, in this order (position 1 first). Unknown ids are ignored."""
    known = set(
        (
            await session.execute(
                select(LinkProposal.id).where(
                    LinkProposal.org_id == org_id, LinkProposal.id.in_(proposal_ids)
                )
            )
        ).scalars()
    )
    n = 0
    for pos, pid in enumerate(proposal_ids, start=1):
        if pid in known:
            session.add(
                LinkProposalExposure(
                    org_id=org_id,
                    proposal_id=pid,
                    shown_to=actor_id,
                    surface=surface[:40],
                    position=pos,
                )
            )
            n += 1
    await session.flush()
    return n


async def _open_proposal(
    session: AsyncSession, *, org_id: uuid.UUID, proposal_id: uuid.UUID
) -> LinkProposal:
    p = (
        await session.execute(
            select(LinkProposal)
            .where(LinkProposal.org_id == org_id, LinkProposal.id == proposal_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if p is None:
        raise NotFoundError(MessageCode.PROPOSAL_NOT_FOUND)
    if p.status != "proposed":
        raise ConflictError(MessageCode.PROPOSAL_DECIDED)
    return p


async def _close_pair(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    a: uuid.UUID,
    b: uuid.UUID,
    status: str,
    kind: str | None,
    delegated: bool = False,
) -> None:
    """A person's decision on a pair answers every open proposal of that
    pair, whoever proposed it: each proposer is credited (or debited) for
    the same judgement. ``delegated`` marks a decision an assistant took
    under the workspace delegation (ADR-0076 phase B): it closes the pair
    the same way and counts for no proposer."""
    await session.execute(
        update(LinkProposal)
        .where(
            LinkProposal.org_id == org_id,
            LinkProposal.status == "proposed",
            or_(
                and_(LinkProposal.source_note_id == a, LinkProposal.target_note_id == b),
                and_(LinkProposal.source_note_id == b, LinkProposal.target_note_id == a),
            ),
        )
        .values(
            status=status,
            decided_at=func.now(),
            decided_by=actor_id,
            accepted_kind=kind,
            delegated=delegated,
        )
    )


async def accept(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    proposal_id: uuid.UUID,
    kind: str,
    swap: bool = False,
    delegated: bool = False,
) -> NoteNoteLink:
    """A person accepts: a typed link of ``kind`` is created between the two
    notes (source as parent, or target as parent with ``swap``), carrying
    the proposer and its evidence reference. No body is edited."""
    p = await _open_proposal(session, org_id=org_id, proposal_id=proposal_id)
    parent, child = (
        (p.target_note_id, p.source_note_id) if swap else (p.source_note_id, p.target_note_id)
    )
    link = await note_links_svc.link_notes(
        session,
        org_id=org_id,
        actor_id=actor_id,
        parent_note_id=parent,
        child_note_id=child,
        kind=kind,
    )
    link.origin_proposal_id = p.id
    link.origin_proposer = p.proposer
    link.origin_evidence_ref = p.evidence_ref
    await _close_pair(
        session,
        org_id=org_id,
        actor_id=actor_id,
        a=p.source_note_id,
        b=p.target_note_id,
        status="accepted",
        kind=kind,
        delegated=delegated,
    )
    await session.flush()
    return link


async def reject(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    proposal_id: uuid.UUID,
    delegated: bool = False,
) -> None:
    """A person rejects: the pair is remembered and no proposer proposes it
    again."""
    p = await _open_proposal(session, org_id=org_id, proposal_id=proposal_id)
    a, b = _canonical(p.source_note_id, p.target_note_id)
    await session.execute(
        pg_insert(LinkRejection)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            note_a_id=a,
            note_b_id=b,
            proposal_id=p.id,
            rejected_by=actor_id,
        )
        .on_conflict_do_nothing(index_elements=["org_id", "note_a_id", "note_b_id"])
    )
    await _close_pair(
        session,
        org_id=org_id,
        actor_id=actor_id,
        a=p.source_note_id,
        b=p.target_note_id,
        status="rejected",
        kind=None,
        delegated=delegated,
    )
    await session.flush()


async def reopen_decision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    proposal_id: uuid.UUID,
    delegated_only: bool = False,
) -> None:
    """Take back the decision on a proposal's pair, whoever took it (ADR-0075
    decision 4): every proposal of the pair closed by it is open again, an
    accepted pair loses the link the acceptance created, and a rejected pair
    is no longer remembered as rejected, so it can be proposed again.
    ``delegated_only`` limits it to what an assistant decided, for the
    reversal of a delegated decision (ADR-0076). An open proposal is a
    no-op; an expired one is not a decision and is absent. Role-gating is
    the caller's, as for :func:`accept` and :func:`reject`."""
    p = await session.get(LinkProposal, proposal_id)
    if p is None or p.org_id != org_id or p.status == "expired":
        raise NotFoundError(MessageCode.PROPOSAL_NOT_FOUND)
    if p.status == "proposed":
        return
    a, b = p.source_note_id, p.target_note_id
    pair = or_(
        and_(LinkProposal.source_note_id == a, LinkProposal.target_note_id == b),
        and_(LinkProposal.source_note_id == b, LinkProposal.target_note_id == a),
    )
    closed = [
        LinkProposal.org_id == org_id,
        LinkProposal.status == p.status,
        pair,
    ]
    if delegated_only:
        closed.append(LinkProposal.delegated.is_(True))
    ids = list((await session.execute(select(LinkProposal.id).where(*closed))).scalars().all())
    if p.status == "accepted":
        links = (
            (
                await session.execute(
                    select(NoteNoteLink).where(
                        NoteNoteLink.org_id == org_id, NoteNoteLink.origin_proposal_id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        for link in links:
            await note_links_svc.unlink_notes(
                session,
                org_id=org_id,
                actor_id=actor_id,
                parent_note_id=link.parent_note_id,
                child_note_id=link.child_note_id,
                kind=link.kind,
            )
    else:
        lo, hi = _canonical(a, b)
        await session.execute(
            delete(LinkRejection).where(
                LinkRejection.org_id == org_id,
                LinkRejection.note_a_id == lo,
                LinkRejection.note_b_id == hi,
            )
        )
    await session.execute(
        update(LinkProposal)
        .where(LinkProposal.id.in_(ids))
        .values(
            status="proposed",
            decided_at=None,
            decided_by=None,
            accepted_kind=None,
            delegated=False,
        )
    )
    await session.flush()


async def expire(
    session: AsyncSession, *, org_id: uuid.UUID, now: datetime.datetime | None = None
) -> int:
    """Close proposals nobody decided within ``TTL_DAYS`` as ``expired``."""
    cutoff = (now or datetime.datetime.now(datetime.UTC)) - datetime.timedelta(days=TTL_DAYS)
    res = await session.execute(
        update(LinkProposal)
        .where(
            LinkProposal.org_id == org_id,
            LinkProposal.status == "proposed",
            LinkProposal.created_at < cutoff,
        )
        .values(status="expired", decided_at=func.now())
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class ProposerPrecision:
    proposer: str
    accepted: int
    rejected: int
    expired: int
    open: int
    shown: int
    precision: float | None
    ci_low: float | None
    ci_high: float | None


async def precision(session: AsyncSession, *, org_id: uuid.UUID) -> list[ProposerPrecision]:
    """Per proposer: accepted over decided (accepted + rejected) with its
    Wilson interval, plus expired, open and how many showings were recorded.
    Expired proposals are not decisions, so they are reported, not counted."""
    rows = (
        await session.execute(
            select(LinkProposal.proposer, LinkProposal.status, func.count())
            # A person's judgements only (ADR-0076 phase B).
            .where(LinkProposal.org_id == org_id, LinkProposal.delegated.is_(False))
            .group_by(LinkProposal.proposer, LinkProposal.status)
        )
    ).all()
    shown_rows = (
        await session.execute(
            select(LinkProposal.proposer, func.count())
            .join(LinkProposalExposure, LinkProposalExposure.proposal_id == LinkProposal.id)
            .where(LinkProposal.org_id == org_id)
            .group_by(LinkProposal.proposer)
        )
    ).all()
    shown = {p: int(n) for p, n in shown_rows}
    by: dict[str, dict[str, int]] = {}
    for proposer, status, n in rows:
        by.setdefault(proposer, {})[status] = int(n)
    out: list[ProposerPrecision] = []
    for proposer in sorted(by):
        c = by[proposer]
        acc, rej = c.get("accepted", 0), c.get("rejected", 0)
        decided = acc + rej
        lo = hi = prec = None
        if decided:
            prec = acc / decided
            lo, hi = eval_stats.wilson_ci(acc, decided)
        out.append(
            ProposerPrecision(
                proposer=proposer,
                accepted=acc,
                rejected=rej,
                expired=c.get("expired", 0),
                open=c.get("proposed", 0),
                shown=shown.get(proposer, 0),
                precision=prec,
                ci_low=lo,
                ci_high=hi,
            )
        )
    return out
