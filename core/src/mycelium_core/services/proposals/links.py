"""Link proposals as proposals (ADR-0073, decision 7)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.link_proposal import LinkProposal
from mycelium_core.models.note import Note
from mycelium_core.models.note_link import NOTE_NOTE_LINK_KINDS
from mycelium_core.services import link_proposals as proposals_svc
from mycelium_core.services.note_effective import effective_note_clause
from mycelium_core.services.proposals import (
    Cursor,
    Listing,
    NoteRef,
    Proposal,
    ProposalKind,
    Verdict,
)

#: The kinds a person may choose when accepting, ``related`` first: the one
#: that claims least about the two notes.
_KINDS = ["related", *sorted(NOTE_NOTE_LINK_KINDS - {"related"})]


class _Links:
    kind = ProposalKind.link

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
        src, dst = aliased(Note), aliased(Note)
        # The column the listing orders and pages by.
        when: Any
        if listing is Listing.pending:
            when = LinkProposal.created_at
            states: tuple[str, ...] = ("proposed",)
        else:
            when = LinkProposal.decided_at
            states = ("accepted", "rejected")
        stmt = (
            select(LinkProposal, src.title, dst.title)
            .join(src, src.id == LinkProposal.source_note_id)
            .join(dst, dst.id == LinkProposal.target_note_id)
            .where(
                LinkProposal.org_id == org_id,
                LinkProposal.status.in_(states),
                effective_note_clause(entity=src),
                effective_note_clause(entity=dst),
            )
        )
        if after is not None:
            at, row_id = after
            stmt = stmt.where(or_(when < at, and_(when == at, LinkProposal.id < row_id)))
        rows = (
            await session.execute(stmt.order_by(when.desc(), LinkProposal.id.desc()).limit(limit))
        ).all()
        out: list[Proposal] = []
        seen: set[tuple[str, str]] = set()
        for p, s_title, d_title in rows:
            # One row per pair: several proposers may propose the same pair,
            # and a decision answers all of them at once.
            pair = tuple(sorted((str(p.source_note_id), str(p.target_note_id))))
            if pair in seen:
                continue
            seen.add(pair)  # type: ignore[arg-type]
            state = {"proposed": "pending", "rejected": "declined"}.get(p.status, p.status)
            out.append(
                Proposal(
                    kind=self.kind,
                    id=p.id,
                    version=None,
                    state=state,
                    created_at=p.created_at,
                    decided_at=p.decided_at,
                    subjects=[
                        NoteRef(note_id=p.source_note_id, title=s_title),
                        NoteRef(note_id=p.target_note_id, title=d_title),
                    ],
                    proposer=p.proposer,
                    reopenable=state in ("accepted", "declined"),
                    reason=p.evidence_ref,
                    link_kinds=list(_KINDS) if state == "pending" else [],
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
        if verdict is Verdict.accept:
            if not link_kind:
                raise DomainError(MessageCode.DOMAIN_ERROR)
            await proposals_svc.accept(
                session,
                org_id=org_id,
                actor_id=actor_id,
                proposal_id=proposal_id,
                kind=link_kind,
            )
        else:
            await proposals_svc.reject(
                session, org_id=org_id, actor_id=actor_id, proposal_id=proposal_id
            )

    async def reopen(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        proposal_id: uuid.UUID,
    ) -> None:
        await proposals_svc.reopen_decision(
            session, org_id=org_id, actor_id=actor_id, proposal_id=proposal_id
        )


ADAPTER = _Links()
