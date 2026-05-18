"""Notes / conversational capture + canonical intent (docs/adr/0020,
0021, FR-16).

Capture is unmetered and offline-friendly; STT/LLM/TTS processing is
metered (ADR-0019) and idempotent by operation_id. The canonical
command grammar is deterministic, offline and unmetered; project slots
are resolved by name and ambiguity is surfaced, never mis-scoped
(ADR-0021/0007). Transcripts feed hierarchical memory with note
provenance; erasure cascades there.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.ai_providers import (
    LLMProvider,
    TranscriptionProvider,
    TtsProvider,
    get_llm,
    get_stt,
    get_tts,
)
from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.billing import CostBasis
from flow_core.models.membership import Role
from flow_core.models.note import Note, NoteKind, NoteStatus, NoteTurn, TurnRole
from flow_core.models.note_tag import NoteTag
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.services import audit, billing, taxonomy
from flow_core.services import memory as memory_svc
from flow_core.services.rbac import require_role


@dataclass(frozen=True)
class ParsedCommand:
    action: str  # create_note | start_conversation
    project_name: str | None
    raw: str


@dataclass(frozen=True)
class NoteErasure:
    audio_ref: str | None
    memory_blobs_deleted: int


_CREATE_NOTE = re.compile(r"\b(create|crea|new|nuova|nuovo)\b.*\b(note|nota)\b", re.I)
_START_CONV = re.compile(r"\b(conversation|conversazione|session|sessione)\b", re.I)
_PROJECT = re.compile(
    r"\b(?:in project|nel progetto|in progetto|project|progetto)\s+([\w .\-]+)$",
    re.I,
)


def parse_command(text: str) -> ParsedCommand:
    """Deterministic canonical grammar (no LLM, offline, unmetered).
    LLM free-form fallback is a later, metered refinement (ADR-0021)."""
    raw = text.strip()
    pm = _PROJECT.search(raw)
    project_name = pm.group(1).strip() if pm else None
    head = raw[: pm.start()] if pm else raw
    if _CREATE_NOTE.search(head):
        action = "create_note"
    elif _START_CONV.search(head):
        action = "start_conversation"
    else:
        raise DomainError(MessageCode.INTENT_UNRECOGNIZED, raw=raw)
    return ParsedCommand(action=action, project_name=project_name, raw=raw)


async def resolve_project(
    session: AsyncSession, *, org_id: uuid.UUID, name: str | None
) -> uuid.UUID | None:
    """Exact, then unambiguous fuzzy; ambiguous/unknown -> clarify.
    Never silently defaults to another project (ADR-0007/0021)."""
    if name is None:
        return None  # explicit "no project" = personal inbox scope
    exact = (
        await session.execute(
            select(Tag.id).where(
                Tag.kind == TagKind.project,
                func.lower(Tag.name) == name.lower(),
            )
        )
    ).scalar_one_or_none()
    if exact is not None:
        return exact
    fuzzy = (
        (
            await session.execute(
                select(Tag.id).where(
                    Tag.kind == TagKind.project,
                    Tag.name.ilike(f"%{name}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(fuzzy) == 1:
        return fuzzy[0]
    if not fuzzy:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    raise DomainError(MessageCode.TAG_AMBIGUOUS, name=name)


def _derive_title(text: str | None) -> str | None:
    """Apple Notes style: when no title is given, the first non-empty
    line of the body becomes the title (trimmed, capped at 120)."""
    if not text:
        return None
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:120]
    return None


async def list_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    limit: int = 200,
    include_archived: bool = False,
    include_deleted: bool = False,
    project_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
) -> list[Note]:
    """Notes in the workspace, newest first (for the @note picker and
    the notes list). RLS scopes to the org. Archived/deleted are
    excluded unless explicitly requested (trash & archive view).
    ``project_id`` / ``tag_id`` organize the list (project focus, tag
    filter)."""
    stmt = select(Note)
    if not include_deleted:
        stmt = stmt.where(Note.deleted_at.is_(None))
    if not include_archived:
        stmt = stmt.where(Note.is_archived.is_(False))
    if project_id is not None:
        stmt = stmt.where(Note.project_id == project_id)
    if tag_id is not None:
        stmt = stmt.where(Note.id.in_(select(NoteTag.note_id).where(NoteTag.tag_id == tag_id)))
    stmt = stmt.order_by(Note.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    include_deleted: bool = False,
) -> Note:
    n = (await session.execute(select(Note).where(Note.id == note_id))).scalar_one_or_none()
    if n is None or (n.deleted_at is not None and not include_deleted):
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return n


async def tags_by_note(
    session: AsyncSession, *, note_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Tag]]:
    """Batched note -> tags (chips in the notes list without an N+1)."""
    out: dict[uuid.UUID, list[Tag]] = {}
    if not note_ids:
        return out
    rows = await session.execute(
        select(NoteTag.note_id, Tag)
        .join(Tag, Tag.id == NoteTag.tag_id)
        .where(NoteTag.note_id.in_(note_ids))
    )
    for nid, tag in rows.all():
        out.setdefault(nid, []).append(tag)
    return out


async def attach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await get_note(session, org_id=org_id, note_id=note_id)
    tag = (await session.execute(select(Tag.id).where(Tag.id == tag_id))).scalar_one_or_none()
    if tag is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    try:
        async with session.begin_nested():
            session.add(NoteTag(org_id=org_id, note_id=note_id, tag_id=tag_id))
            await session.flush()
    except IntegrityError:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="attach_tag",
    )


async def detach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(NoteTag).where(NoteTag.note_id == note_id, NoteTag.tag_id == tag_id)
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="detach_tag",
    )


async def _note_set(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
    action: str,
) -> int:
    await require_role(session, org_id, actor_id, Role.member)
    await get_note(session, org_id=org_id, note_id=note_id, include_deleted=True)
    new_version = await optimistic_update(
        session,
        Note,
        pk=note_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action=action,
    )
    return new_version


async def update_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
    title: str | None = None,
    text: str | None = None,
) -> int:
    """Edit title/body. When the title is blank it is re-derived from
    the first line of the body (Apple Notes style)."""
    values: dict[str, Any] = {}
    if text is not None:
        values["transcript"] = text
    eff_title = title if (title and title.strip()) else _derive_title(text)
    values["title"] = eff_title
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values=values,
        action="update",
    )


async def archive_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
    archived: bool = True,
) -> int:
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values={"is_archived": archived},
        action="archive",
    )


async def soft_delete_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values={"deleted_at": dt.datetime.now(tz=dt.UTC)},
        action="delete",
    )


async def restore_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values={"deleted_at": None},
        action="restore",
    )


async def create_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: NoteKind,
    project_id: uuid.UUID | None = None,
    title: str | None = None,
    text: str | None = None,
    audio_ref: str | None = None,
    audio_seconds: int | None = None,
) -> Note:
    """Capture only. NOT metered, works at zero credits (ADR-0020:
    never lose the idea)."""
    await require_role(session, org_id, actor_id, Role.member)
    if kind is NoteKind.text:
        status = NoteStatus.ready
        transcript = text
    elif kind is NoteKind.conversation:
        status = NoteStatus.ready
        transcript = None
    else:  # voice
        status = NoteStatus.captured
        transcript = None
    if not (title and title.strip()):
        title = _derive_title(text)
    note = Note(
        org_id=org_id,
        project_id=project_id,
        kind=kind,
        status=status,
        title=title,
        transcript=transcript,
        audio_ref=audio_ref,
        audio_seconds=audio_seconds,
    )
    session.add(note)
    await session.flush()
    # Every note must belong to a client: the project's client when a
    # project is set, otherwise the "Personal" default. Stored as a
    # NoteTag so notes stay queryable/filterable by client.
    client_tag_id: uuid.UUID | None = None
    if project_id is not None:
        client_tag_id = (
            await session.execute(
                select(ProjectProfile.client_tag_id).where(ProjectProfile.tag_id == project_id)
            )
        ).scalar_one_or_none()
    if client_tag_id is None:
        client_tag_id = await taxonomy.ensure_default_client(
            session, org_id=org_id, actor_id=actor_id
        )
    session.add(NoteTag(org_id=org_id, note_id=note.id, tag_id=client_tag_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note.id,
        action="create",
    )
    return note


async def run_command(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    text: str,
) -> Note:
    """Canonical NL command -> deterministic action (ADR-0021)."""
    cmd = parse_command(text)
    project_id = await resolve_project(session, org_id=org_id, name=cmd.project_name)
    kind = NoteKind.text if cmd.action == "create_note" else NoteKind.conversation
    return await create_note(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=kind,
        project_id=project_id,
        title=cmd.raw[:300],
    )


async def transcribe(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    operation_id: str,
    embed: bool = True,
    stt: TranscriptionProvider | None = None,
) -> Note:
    """STT processing: metered per audio-minute; the transcript feeds
    hierarchical memory with note provenance (ADR-0016/0020)."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await get_note(session, org_id=org_id, note_id=note_id)
    if note.audio_ref is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    note.status = NoteStatus.transcribing
    await session.flush()
    provider = stt or get_stt()
    seconds = note.audio_seconds or 0
    res = await provider.transcribe(audio_ref=note.audio_ref, audio_seconds=seconds)
    await billing.meter(
        session,
        org_id=org_id,
        actor_id=actor_id,
        operation_id=operation_id,
        op="stt",
        model_id=res.model_id,
        units_in=Decimal(res.audio_seconds) / Decimal(60),
        basis=CostBasis.local,
    )
    note.transcript = res.text
    note.status = NoteStatus.ready
    await session.flush()
    if embed and res.text:
        await memory_svc.write_blob(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=note.project_id,
            text_body=res.text,
            operation_id=f"{operation_id}:mem",
            namespace="note",
            sources=[("note", str(note.id))],
        )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note.id,
        action="transcribe",
    )
    return note


async def append_message(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    content: str,
    operation_id: str,
    llm: LLMProvider | None = None,
) -> NoteTurn:
    """Conversation turn: user message + a metered LLM reply, both
    saved on the conversation note (ADR-0020)."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await get_note(session, org_id=org_id, note_id=note_id)
    if note.kind is not NoteKind.conversation:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    turns = list(
        (
            await session.execute(
                select(NoteTurn).where(NoteTurn.note_id == note_id).order_by(NoteTurn.ord)
            )
        )
        .scalars()
        .all()
    )
    next_ord = (turns[-1].ord + 1) if turns else 0
    session.add(
        NoteTurn(
            org_id=org_id,
            note_id=note_id,
            role=TurnRole.user,
            content=content,
            ord=next_ord,
        )
    )
    await session.flush()
    history: list[tuple[str, str]] = [(t.role.value, t.content) for t in turns]
    history.append(("user", content))
    provider = llm or get_llm()
    res = await provider.complete(system=None, messages=history)
    await billing.meter(
        session,
        org_id=org_id,
        actor_id=actor_id,
        operation_id=operation_id,
        op="llm",
        model_id=res.model_id,
        units_in=Decimal(res.tokens_in),
        units_out=Decimal(res.tokens_out),
        basis=CostBasis.local,
    )
    reply = NoteTurn(
        org_id=org_id,
        note_id=note_id,
        role=TurnRole.assistant,
        content=res.text,
        ord=next_ord + 1,
    )
    session.add(reply)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="append_message",
    )
    return reply


async def list_turns(
    session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID
) -> list[NoteTurn]:
    return list(
        (
            await session.execute(
                select(NoteTurn).where(NoteTurn.note_id == note_id).order_by(NoteTurn.ord)
            )
        )
        .scalars()
        .all()
    )


async def synthesize(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    text: str,
    operation_id: str,
    tts: TtsProvider | None = None,
) -> dict[str, str]:
    """TTS voice-out: metered per character (ADR-0019/0020)."""
    await require_role(session, org_id, actor_id, Role.member)
    provider = tts or get_tts()
    res = await provider.synthesize(text=text)
    await billing.meter(
        session,
        org_id=org_id,
        actor_id=actor_id,
        operation_id=operation_id,
        op="tts",
        model_id=res.model_id,
        units_in=Decimal(res.chars),
        basis=CostBasis.local,
    )
    return {"audio_ref": res.audio_ref, "model_id": res.model_id}


async def gdpr_erase_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
) -> NoteErasure:
    """Cascade: memory blobs (by note provenance) + note + turns. The
    S3 audio_ref is returned for the caller/worker to delete."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await get_note(session, org_id=org_id, note_id=note_id)
    audio_ref = note.audio_ref
    blobs_deleted = await memory_svc.gdpr_erase(
        session,
        org_id=org_id,
        actor_id=actor_id,
        source_kind="note",
        source_id=str(note_id),
    )
    await session.delete(note)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="gdpr_erase",
        diff={"memory_blobs": str(blobs_deleted)},
    )
    return NoteErasure(audio_ref=audio_ref, memory_blobs_deleted=blobs_deleted)
