"""Generated notes (distillations, patterns, seasons) as proposals (ADR-0043)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import Note
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.services import garden_review as review_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.note_effective import effective_note_clause
from mycelium_core.services.proposals import (
    Cursor,
    Listing,
    NoteRef,
    Proposal,
    ProposalKind,
    Verdict,
)

_PREVIEW = 280


class _Synthesis:
    kind = ProposalKind.synthesis

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
        # The column the listing orders and pages by.
        when: Any
        if listing is Listing.pending:
            when = Note.created_at
            stmt = select(Note).where(
                Note.org_id == org_id,
                Note.review_state == "proposed",
                Note.deleted_at.is_(None),
            )
        else:
            when = Note.review_decided_at
            stmt = select(Note).where(
                Note.org_id == org_id,
                Note.review_state.in_(("declined", "approved")),
                Note.review_decided_at.is_not(None),
                Note.deleted_at.is_(None),
            )
        if after is not None:
            at, row_id = after
            stmt = stmt.where(or_(when < at, and_(when == at, Note.id < row_id)))
        notes = list(
            (await session.execute(stmt.order_by(when.desc(), Note.id.desc()).limit(limit)))
            .scalars()
            .all()
        )
        out: list[Proposal] = []
        for n in notes:
            parents = (
                await session.execute(
                    select(Note.id, Note.title)
                    .join(NoteNoteLink, NoteNoteLink.parent_note_id == Note.id)
                    .where(
                        NoteNoteLink.org_id == org_id,
                        NoteNoteLink.child_note_id == n.id,
                        NoteNoteLink.kind == "hypha_of",
                        effective_note_clause(),
                    )
                    .order_by(Note.created_at.asc())
                )
            ).all()
            body = (await notes_svc.get_body(session, note_id=n.id)) or ""
            state = {"proposed": "pending", "approved": "accepted"}.get(
                n.review_state or "", n.review_state or ""
            )
            out.append(
                Proposal(
                    kind=self.kind,
                    id=n.id,
                    version=n.version,
                    state=state,
                    created_at=n.created_at,
                    decided_at=n.review_decided_at,
                    subjects=[NoteRef(note_id=i, title=t) for i, t in parents],
                    proposer=n.origin_model_id,
                    reopenable=state in ("declined", "accepted"),
                    title=n.title,
                    preview=body.strip()[:_PREVIEW],
                    humus_kind=n.humus_kind,
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
        if expected_version is None:
            # A synthesis is decided on the text the person read.
            raise DomainError(MessageCode.DOMAIN_ERROR)
        if verdict is Verdict.accept:
            await review_svc.approve_node(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=proposal_id,
                expected_version=expected_version,
            )
        else:
            await review_svc.decline_node(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=proposal_id,
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
        note = await session.get(Note, proposal_id)
        if note is None or note.org_id != org_id or note.deleted_at is not None:
            raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
        if note.review_state == "approved":
            await review_svc.withdraw_approval(
                session, org_id=org_id, actor_id=actor_id, note_id=proposal_id
            )
        else:
            await review_svc.reopen_node(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=proposal_id,
                expected_version=note.version,
            )


ADAPTER = _Synthesis()
