"""The last word a person hands over to an assistant (ADR-0076, phase B).

In general an agent does not decide what enters the person's memory. For the
decisions the person considers simple, they can hand the last word over, per
kind of decision, in the workspace setting ``garden_delegation``. The set of
kinds is closed and contains only decisions a person can reverse afterwards:
declining a fact is not in it, because a declined fact is frozen by the
database (invalidate-not-delete) and could not be taken back.

Every decision an assistant takes here is recorded in ``delegated_decision``:
the list of what agents decided, the way to reverse each one, and the
measure of how often the person disagrees. Agent decisions are marked so the
measures of a model's or a proposer's quality keep counting a person's
judgements only.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.delegated_decision import DelegatedDecision
from mycelium_core.models.kg import KgEdge
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note
from mycelium_core.models.organization import Organization
from mycelium_core.models.proposal_check import CheckVerdict, ProposalCheck
from mycelium_core.services import audit
from mycelium_core.services import garden_review as review_svc
from mycelium_core.services import kg as kg_svc
from mycelium_core.services import link_proposals as proposals_svc
from mycelium_core.services.rbac import require_role

#: Key of the workspace setting, a list of ``DecisionKind`` values.
SETTINGS_KEY = "garden_delegation"


class DecisionKind(enum.StrEnum):
    #: Decline a pending synthesis (a distillation, a pattern, a season).
    synthesis_decline = "synthesis_decline"
    #: Approve a pending synthesis whose CURRENT version carries a check
    #: with the verdict ``supported``: every claim found in the sources.
    synthesis_approve_checked = "synthesis_approve_checked"
    #: Accept a link proposal, with the kind of link the assistant chooses.
    link_proposal_accept = "link_proposal_accept"
    #: Reject a link proposal (the pair is not proposed again).
    link_proposal_reject = "link_proposal_reject"
    #: Approve a fact a model read from the notes.
    fact_approve = "fact_approve"


class Verdict(enum.StrEnum):
    approve = "approve"
    decline = "decline"


class TargetKind(enum.StrEnum):
    synthesis = "synthesis"
    link_proposal = "link_proposal"
    fact = "fact"


#: Which kind of decision a (target, verdict) pair is. A pair absent here
#: (declining a fact) cannot be delegated at all.
_KIND_OF: dict[tuple[TargetKind, Verdict], DecisionKind] = {
    (TargetKind.synthesis, Verdict.decline): DecisionKind.synthesis_decline,
    (TargetKind.synthesis, Verdict.approve): DecisionKind.synthesis_approve_checked,
    (TargetKind.link_proposal, Verdict.approve): DecisionKind.link_proposal_accept,
    (TargetKind.link_proposal, Verdict.decline): DecisionKind.link_proposal_reject,
    (TargetKind.fact, Verdict.approve): DecisionKind.fact_approve,
}


def normalize(values: list[str]) -> list[str]:
    """Validate a setting value: every entry a ``DecisionKind``, an unknown
    one refused rather than dropped, returned sorted and unique."""
    try:
        kinds = {DecisionKind(v) for v in values}
    except ValueError:
        raise DomainError(MessageCode.DOMAIN_ERROR) from None
    return sorted(k.value for k in kinds)


async def granted(session: AsyncSession, *, org_id: uuid.UUID) -> frozenset[DecisionKind]:
    """The kinds of decision the person has handed over in this workspace.
    A stored value that is no longer a kind grants nothing: the safe reading
    of a setting that says more than the code understands."""
    settings = await session.scalar(select(Organization.settings).where(Organization.id == org_id))
    out: set[DecisionKind] = set()
    for v in (settings or {}).get(SETTINGS_KEY) or []:
        try:
            out.add(DecisionKind(v))
        except ValueError:
            continue
    return frozenset(out)


def _refuse(reason: str) -> DomainError:
    return DomainError(MessageCode.GARDEN_DELEGATION_REFUSED, reason=reason)


async def decide(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    assistant_identity_id: uuid.UUID | None,
    target_kind: TargetKind,
    target_id: uuid.UUID,
    verdict: Verdict,
    expected_version: int | None = None,
    link_kind: str | None = None,
    reason: str | None = None,
) -> DelegatedDecision:
    """Take a decision on the person's behalf, if they handed this kind over.

    Refused with ``garden.delegation_refused`` and a ``reason`` when the
    caller is not an assistant (``not_an_assistant``: a person decides with
    the ordinary operations), when the kind is not delegable at all
    (``not_delegable``) or not handed over here (``not_granted``), and, for
    approving a synthesis, when its current version carries no check with
    the verdict ``supported`` (``no_supporting_check``). The underlying
    operation then runs as the person's ordinary one does, with its own
    version check, and the decision is recorded."""
    await require_role(session, org_id, actor_id, Role.member)
    if assistant_identity_id is None:
        raise _refuse("not_an_assistant")
    kind = _KIND_OF.get((target_kind, verdict))
    if kind is None:
        raise _refuse("not_delegable")
    if kind not in await granted(session, org_id=org_id):
        raise _refuse("not_granted")
    result_id: uuid.UUID | None = None
    if target_kind is TargetKind.synthesis:
        if expected_version is None:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        if verdict is Verdict.approve:
            note = await session.get(Note, target_id)
            if note is None or note.org_id != org_id or note.review_state != "proposed":
                raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
            if note.version != expected_version:
                raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
            supported = await session.scalar(
                select(func.count())
                .select_from(ProposalCheck)
                .where(
                    ProposalCheck.org_id == org_id,
                    ProposalCheck.note_id == note.id,
                    ProposalCheck.note_version == note.version,
                    ProposalCheck.verdict == CheckVerdict.supported.value,
                )
            )
            if not supported:
                raise _refuse("no_supporting_check")
            await review_svc.approve_node(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=target_id,
                expected_version=expected_version,
                delegated_by_identity_id=assistant_identity_id,
            )
        else:
            await review_svc.decline_node(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=target_id,
                reason=reason,
                expected_version=expected_version,
                delegated_by_identity_id=assistant_identity_id,
            )
    elif target_kind is TargetKind.link_proposal:
        if verdict is Verdict.approve:
            if not link_kind:
                raise DomainError(MessageCode.DOMAIN_ERROR)
            link = await proposals_svc.accept(
                session,
                org_id=org_id,
                actor_id=actor_id,
                proposal_id=target_id,
                kind=link_kind,
                delegated=True,
            )
            result_id = link.id
        else:
            await proposals_svc.reject(
                session, org_id=org_id, actor_id=actor_id, proposal_id=target_id, delegated=True
            )
    else:
        await kg_svc.approve_fact(
            session,
            org_id=org_id,
            actor_id=actor_id,
            edge_id=target_id,
            expected_version=expected_version,
        )
    row = DelegatedDecision(
        org_id=org_id,
        kind=kind.value,
        target_kind=target_kind.value,
        target_id=target_id,
        verdict=verdict.value,
        assistant_identity_id=assistant_identity_id,
        reason=(reason or "").strip()[:2000] or None,
        result_id=result_id,
    )
    session.add(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="delegated_decision",
        entity_id=row.id,
        action="garden_delegation:decide",
        diff={
            "kind": kind.value,
            "target_id": str(target_id),
            "assistant_identity_id": str(assistant_identity_id),
        },
    )
    return row


async def revert(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, decision_id: uuid.UUID
) -> DelegatedDecision:
    """A person takes back what an assistant decided; the thing is pending
    again, as before the decision:

    - a declined synthesis is reopened, an approved one withdrawn;
    - an accepted link proposal: the link it created is removed and the
      pair's proposals are open again;
    - a rejected link proposal: the pair is no longer remembered as
      rejected and its proposals are open again;
    - an approved fact is proposed again.

    Reverting twice is a no-op. A thing that has moved since (someone else
    decided it again) is refused as stale rather than undone blindly."""
    await require_role(session, org_id, actor_id, Role.member)
    row = await session.get(DelegatedDecision, decision_id)
    if row is None or row.org_id != org_id:
        raise NotFoundError(MessageCode.GARDEN_DELEGATED_DECISION_NOT_FOUND)
    if row.reversed_at is not None:
        return row
    target_kind, verdict = TargetKind(row.target_kind), Verdict(row.verdict)
    if target_kind is TargetKind.synthesis:
        note = await session.get(Note, row.target_id)
        if note is None or note.org_id != org_id:
            raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
        expected = "approved" if verdict is Verdict.approve else "declined"
        if note.review_state != expected:
            raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
        if verdict is Verdict.approve:
            await review_svc.withdraw_approval(
                session, org_id=org_id, actor_id=actor_id, note_id=note.id
            )
        else:
            await review_svc.reopen_node(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=note.id,
                expected_version=note.version,
            )
    elif target_kind is TargetKind.link_proposal:
        # The same reopening a person's own decision gets (ADR-0075), kept
        # to what the assistant closed.
        await proposals_svc.reopen_decision(
            session,
            org_id=org_id,
            actor_id=actor_id,
            proposal_id=row.target_id,
            delegated_only=True,
        )
    else:
        edge = await session.get(KgEdge, row.target_id)
        if edge is None or edge.org_id != org_id:
            raise NotFoundError(MessageCode.KG_FACT_NOT_FOUND)
        if edge.review_state != "approved" or edge.invalidated_at is not None:
            raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
        await kg_svc.withdraw_fact_approval(
            session, org_id=org_id, actor_id=actor_id, edge_id=edge.id
        )
    row.reversed_at = dt.datetime.now(dt.UTC)
    row.reversed_by = actor_id
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="delegated_decision",
        entity_id=row.id,
        action="garden_delegation:revert",
        diff={"kind": row.kind, "target_id": str(row.target_id)},
    )
    return row


@dataclass(frozen=True)
class DecisionStats:
    """How often a person reversed what agents decided, per kind."""

    kind: str
    decided: int
    reversed: int


async def list_decisions(
    session: AsyncSession, *, org_id: uuid.UUID, limit: int = 50
) -> list[DelegatedDecision]:
    """What agents decided, most recent first."""
    return list(
        (
            await session.execute(
                select(DelegatedDecision)
                .where(DelegatedDecision.org_id == org_id)
                .order_by(DelegatedDecision.created_at.desc(), DelegatedDecision.id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def stats(session: AsyncSession, *, org_id: uuid.UUID) -> list[DecisionStats]:
    rows = (
        await session.execute(
            select(
                DelegatedDecision.kind,
                func.count(),
                func.count().filter(DelegatedDecision.reversed_at.is_not(None)),
            )
            .where(DelegatedDecision.org_id == org_id)
            .group_by(DelegatedDecision.kind)
            .order_by(DelegatedDecision.kind)
        )
    ).all()
    return [DecisionStats(kind=k, decided=int(n), reversed=int(r)) for k, n, r in rows]


__all__ = [
    "SETTINGS_KEY",
    "DecisionKind",
    "DecisionStats",
    "TargetKind",
    "Verdict",
    "decide",
    "granted",
    "list_decisions",
    "normalize",
    "revert",
    "stats",
]
