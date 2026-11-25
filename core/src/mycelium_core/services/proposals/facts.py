"""Facts a model read from the notes, as proposals (ADR-0071)."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.models.kg import KgEdge, KgEntity
from mycelium_core.models.note import Note
from mycelium_core.services import kg as kg_svc
from mycelium_core.services.proposals import (
    Cursor,
    Listing,
    NoteRef,
    Proposal,
    ProposalKind,
    Verdict,
)


class _Facts:
    kind = ProposalKind.fact

    async def list(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        listing: Listing,
        limit: int,
        after: Cursor | None,
    ) -> list[Proposal]:
        subj, obj = aliased(KgEntity), aliased(KgEntity)
        if listing is Listing.pending:
            when = KgEdge.created_at
            where = and_(KgEdge.review_state == "proposed", KgEdge.invalidated_at.is_(None))
        else:
            # A decided fact: approved and live, or rejected (rejecting
            # invalidates it in the same write). A fact a person asserted
            # never went through review and is not listed.
            when = KgEdge.updated_at
            where = or_(
                and_(KgEdge.review_state == "approved", KgEdge.invalidated_at.is_(None)),
                KgEdge.review_state == "rejected",
            )
        stmt = (
            select(KgEdge, subj.name, obj.name, Note.title)
            .join(subj, subj.id == KgEdge.subject_id)
            .join(obj, obj.id == KgEdge.object_id)
            .outerjoin(Note, Note.id == KgEdge.source_note_id)
            .where(KgEdge.org_id == org_id, where)
        )
        if after is not None:
            at, row_id = after
            stmt = stmt.where(or_(when < at, and_(when == at, KgEdge.id < row_id)))
        rows = (
            await session.execute(stmt.order_by(when.desc(), KgEdge.id.desc()).limit(limit))
        ).all()
        out: list[Proposal] = []
        for e, s_name, o_name, note_title in rows:
            state = {"proposed": "pending", "approved": "accepted", "rejected": "declined"}[
                e.review_state
            ]
            out.append(
                Proposal(
                    kind=self.kind,
                    id=e.id,
                    version=e.version,
                    state=state,
                    created_at=e.created_at,
                    decided_at=None if state == "pending" else e.updated_at,
                    subjects=(
                        [NoteRef(note_id=e.source_note_id, title=note_title)]
                        if e.source_note_id
                        else []
                    ),
                    proposer=e.origin_model_id,
                    # A rejected fact is frozen (invalidate-not-delete): it
                    # cannot be reopened, and the listing says so.
                    reopenable=state == "accepted",
                    fact_subject=s_name,
                    fact_predicate=e.predicate,
                    fact_object=o_name,
                )
            )
        return out

    async def decide(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        proposal_id: uuid.UUID,
        verdict: Verdict,
        expected_version: int | None,
        link_kind: str | None,
    ) -> None:
        run = kg_svc.approve_fact if verdict is Verdict.accept else kg_svc.reject_fact
        await run(
            session,
            org_id=org_id,
            actor_id=actor_id,
            edge_id=proposal_id,
            expected_version=expected_version,
        )

    async def reopen(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        proposal_id: uuid.UUID,
    ) -> None:
        await kg_svc.withdraw_fact_approval(
            session, org_id=org_id, actor_id=actor_id, edge_id=proposal_id
        )


ADAPTER = _Facts()
