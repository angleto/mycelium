"""Decomposizione fungina (task 4a718dc4, ADR-0034).

When a note is archived (``is_archived = true``), the decomposition
pipeline reads the note body, asks an LLM to extract the
*lessons / atoms / claims*, and writes the synthesis back as a new
``distillation`` note linked to the source. Both the source and
the synthesis flip ``humus_flag`` so the LLM walk (ADR-0034) can
surface them as fertiliser instead of fresh content.

Phase 1 surface: ``distill_note(note_id)``. The pattern-extraction
job (per-cluster) and the quarterly season synthesis are out of
scope for this commit; the schema and the LLM prompt are designed
so adding them is one helper + one cron entry.

The pipeline is idempotent on ``(source_note_id, kind="distillation")``:
re-running for an already-distilled note is a no-op (no second
distillation note is created). The caller decides when to trigger
(sync on archive, async via a worker queue, on-demand).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.ai_providers import LLMProvider, get_llm
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.identity import Identity
from flow_core.models.membership import Role
from flow_core.models.note import Note, NoteKind
from flow_core.models.note_link import NoteNoteLink
from flow_core.services import notes as notes_svc
from flow_core.services.rbac import require_role

_DISTILL_SYSTEM = (
    "You decompose a piece of finished thinking into reusable atoms. "
    "Read the note and reply with: (1) one-sentence lesson, (2) up to "
    "five concrete claims as bullet points, (3) up to three keywords. "
    "No filler, no apology, no restate of the brief. Italian or English "
    "matching the input."
)


@dataclass(frozen=True)
class DistillationResult:
    distilled_note_id: uuid.UUID
    model_id: str
    # False when an existing distillation was returned untouched
    # (idempotent re-run); True when a new distillation was generated.
    created: bool


async def distill_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    llm: LLMProvider | None = None,
) -> DistillationResult:
    """Read the source note's body, generate a distillation via the
    LLM provider, and persist it as a new note linked to the source.

    Idempotent: if a distillation note already derives from this source
    (a ``hypha_of`` link to a note marked ``humus_kind='distillation'``),
    the existing one is returned untouched.
    """
    await require_role(session, org_id, actor_id, Role.member)
    source = await notes_svc.get_note(session, org_id=org_id, note_id=note_id)
    # Idempotency: look for an existing distillation derived from this
    # source (a hypha_of edge source -> a humus_kind='distillation' note).
    existing_row = (
        await session.execute(
            select(NoteNoteLink.child_note_id)
            .join(Note, Note.id == NoteNoteLink.child_note_id)
            .where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.parent_note_id == note_id,
                NoteNoteLink.kind == "hypha_of",
                Note.humus_kind == "distillation",
            )
            .limit(1)
        )
    ).first()
    if existing_row is not None:
        # The previous distillation is still authoritative. The model
        # id is unknown from this row alone (audit log carries it);
        # callers shouldn't rely on this branch's model_id.
        return DistillationResult(
            distilled_note_id=existing_row[0], model_id="cached", created=False
        )
    body = await notes_svc.get_body(session, note_id=note_id)
    if not body or not body.strip():
        raise DomainError(MessageCode.DOMAIN_ERROR)
    provider = llm or get_llm()
    res = await provider.complete(
        system=_DISTILL_SYSTEM,
        messages=[("user", body)],
    )
    title = (source.title or "").strip()
    distill_title = f"Distillation · {title or 'untitled'}"[:300]
    # Migration 0016: the source's project lives in the junction;
    # carry it over so the distillation lands in the same project.
    source_project_id = await notes_svc.project_tag_for_note(session, note_id=source.id)
    distilled = await notes_svc.create_note(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=NoteKind.text,
        title=distill_title,
        text=res.text,
        project_id=source_project_id,
    )
    distilled.humus_kind = "distillation"
    distilled.humus_flag = True
    # The source itself becomes humus too: it's been decomposed; the
    # walk can now surface it as fertiliser.
    source.humus_flag = True
    await session.flush()
    # Link: the distillation DERIVED FROM the source, so it is an
    # ordinary ``hypha_of`` (parent = source / origin, child = the new
    # distillation). The fact that it is humus lives in the node facet
    # (``humus_kind``), not in the link kind (ADR-0040): a 1:1
    # distillation keeps the thread to its single source so the lesson
    # can be decompressed back to the rich note it came from. We write
    # the row directly (the pipeline already gated on role above) rather
    # than via ``link_notes``.
    # ``created_by`` on note_note_link is an Identity FK, not a
    # raw user id — resolve the actor's Identity row in this org.
    identity_id = (
        await session.execute(
            select(Identity.id).where(
                Identity.org_id == org_id,
                Identity.user_id == actor_id,
            )
        )
    ).scalar_one_or_none()
    session.add(
        NoteNoteLink(
            org_id=org_id,
            parent_note_id=source.id,
            child_note_id=distilled.id,
            kind="hypha_of",
            created_by=identity_id,
        )
    )
    await session.flush()
    return DistillationResult(distilled_note_id=distilled.id, model_id=res.model_id, created=True)


__all__ = ["DistillationResult", "distill_note"]
