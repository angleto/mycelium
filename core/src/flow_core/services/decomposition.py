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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.ai_providers import LLMProvider
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.classification_feedback import ClassificationFeedback
from flow_core.models.identity import Identity
from flow_core.models.membership import Role
from flow_core.models.note import Note, NoteKind
from flow_core.models.note_link import NoteNoteLink
from flow_core.services import audit, note_inert
from flow_core.services import notes as notes_svc
from flow_core.services.llm_resolver import resolve_llm
from flow_core.services.rbac import require_role

# Version tag for the inert-flip heuristic that decides a source becomes
# humus. Recorded on the feedback row so the learning loop can tell which
# policy produced the decision (the LLM model that distilled lives in the
# signals snapshot, since the flip is decided by ``is_inert``, not the LLM).
_HUMUS_MODEL_VERSION = "auto_humus_v1"

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


async def _flip_source_to_humus(
    session: AsyncSession, *, note_id: uuid.UUID, expected_version: int
) -> bool:
    """Flip a source note's ``humus_flag`` to True under optimistic
    concurrency (WS-F3, §12).

    The UPDATE matches only when the version is still ``expected_version``
    AND the flag is still False, and it bumps the version like every other
    note mutation. So a concurrent edit/unarchive (version moved) or a
    double-distill (flag already set) is a no-op. Returns True when the row
    was flipped, False when it was skipped. Never raises: a race here is a
    deliberate skip, not a 409 -- the distillation must stand regardless.
    """
    row = (
        await session.execute(
            update(Note)
            .where(
                Note.id == note_id,
                Note.version == expected_version,
                Note.humus_flag.is_(False),
            )
            .values(humus_flag=True, version=Note.version + 1)
            .returning(Note.version)
        )
    ).first()
    return row is not None


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
    # WS-F3: snapshot the source version BEFORE the slow LLM call, so the
    # humus flip at the end can be guarded with optimistic concurrency -- a
    # concurrent edit/unarchive in that window bumps the version and the
    # flip is skipped instead of mutating a note that turned live.
    source_version = source.version
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
    # Route through the per-org METERED seam (WS-C3): an org on a hosted
    # provider (anthropic/scaleway/openai) gets ITS model and is charged,
    # instead of silently falling back to the local model for free -- the
    # bug this fixes, where the metering the docstrings/ADRs promise was
    # bypassed by a bare get_llm(). ``llm`` stays an explicit test/override
    # injection. The operation_id is deterministic so a retried distill
    # never double-charges (the idempotency guard above already prevents a
    # second LLM call once the humus atom exists).
    provider = llm or await resolve_llm(
        session,
        org_id,
        actor_id=actor_id,
        operation_id=f"distill:{org_id}:{note_id}",
        op="distill",
    )
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
    # Anti-mutation invariant (task 8a26c000): the source becomes humus
    # only if it is inert (archived/dormant, no open linked work, past the
    # quiet window). A live source -- one being actively worked -- is left
    # untouched; only the derived distillation node is created.
    #
    # WS-F3 (§12 concurrency): re-check inertia here, then flip under
    # optimistic concurrency. The is_inert re-check catches a linked task
    # reopening (which does not bump the note version); the version guard in
    # _flip_source_to_humus catches a concurrent edit/unarchive (which does).
    # On either race the flip is skipped -- the distillation still stands and
    # a now-live source is never mutated.
    flipped = await note_inert.is_inert(session, note=source) and await _flip_source_to_humus(
        session, note_id=source.id, expected_version=source_version
    )
    if flipped:
        # §12 "every mutation is tracked" (WS-F2): this is the one autonomous
        # note mutation that used to bypass _note_set entirely -- no revision,
        # no audit, no feedback. Trace it explicitly with an audit row
        # (action auto_humus) and an append-only classification_feedback row
        # (action 'auto', the system-initiated kind) so the flip is auditable
        # and replayable by the learning loop, exactly like auto_promote_mature.
        # The guarded UPDATE only fires when humus_flag was still False, so the
        # diff is always old=False; a live source leaves no trace.
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=source.id,
            action="auto_humus",
            diff={
                "humus_flag": {"old": False, "new": True},
                "distilled_note_id": str(distilled.id),
            },
        )
        session.add(
            ClassificationFeedback(
                org_id=org_id,
                user_id=actor_id,
                node_id=source.id,
                suggestion_type="humus",
                suggestion_value={"humus_flag": True},
                action="auto",
                model_version=_HUMUS_MODEL_VERSION,
                signals_snapshot={
                    "trigger": "distill",
                    "distilled_note_id": str(distilled.id),
                    "distill_model_id": res.model_id,
                },
            )
        )
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
