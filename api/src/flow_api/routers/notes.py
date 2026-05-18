"""Notes / conversation / canonical-intent router. Thin adapter
(docs/adr/0001, 0020, 0021, FR-16). Capture is unmetered; STT/LLM/TTS
processing is metered in the service. Providers are injected via the
seam (fakes in tests)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    AppendMessageIn,
    CommandIn,
    ConversationStartIn,
    NoteCreateIn,
    NoteEraseOut,
    NoteOut,
    NoteTranscribeIn,
    NoteTurnOut,
    SynthesizeIn,
    SynthOut,
)
from flow_core.models.note import Note, NoteKind, NoteTurn
from flow_core.services import notes as svc

router = APIRouter(prefix="/notes", tags=["notes"])


def _out(n: Note) -> NoteOut:
    return NoteOut(
        id=n.id,
        project_id=n.project_id,
        kind=n.kind,
        status=n.status,
        title=n.title,
        transcript=n.transcript,
        summary=n.summary,
        audio_ref=n.audio_ref,
        version=n.version,
    )


def _turn(t: NoteTurn) -> NoteTurnOut:
    return NoteTurnOut(id=t.id, role=t.role, content=t.content, ord=t.ord)


@router.post("", response_model=NoteOut)
async def create_note(
    body: NoteCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> NoteOut:
    n = await svc.create_note(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        kind=body.kind,
        project_id=body.project_id,
        title=body.title,
        text=body.text,
        audio_ref=body.audio_ref,
        audio_seconds=body.audio_seconds,
    )
    return _out(n)


@router.get("/{note_id}", response_model=NoteOut)
async def get_note(
    note_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> NoteOut:
    return _out(await svc.get_note(ctx.session, org_id=ctx.org_id, note_id=note_id))


@router.post("/{note_id}/transcribe", response_model=NoteOut)
async def transcribe(
    note_id: uuid.UUID,
    body: NoteTranscribeIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> NoteOut:
    n = await svc.transcribe(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        note_id=note_id,
        operation_id=body.operation_id,
        embed=body.embed,
    )
    return _out(n)


@router.post("/conversations", response_model=NoteOut)
async def start_conversation(
    body: ConversationStartIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> NoteOut:
    n = await svc.create_note(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        kind=NoteKind.conversation,
        project_id=body.project_id,
        title=body.title,
    )
    return _out(n)


@router.post("/{note_id}/messages", response_model=NoteTurnOut)
async def append_message(
    note_id: uuid.UUID,
    body: AppendMessageIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> NoteTurnOut:
    reply = await svc.append_message(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        note_id=note_id,
        content=body.content,
        operation_id=body.operation_id,
    )
    return _turn(reply)


@router.get("/{note_id}/turns", response_model=list[NoteTurnOut])
async def list_turns(
    note_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[NoteTurnOut]:
    rows = await svc.list_turns(ctx.session, org_id=ctx.org_id, note_id=note_id)
    return [_turn(t) for t in rows]


@router.post("/synthesize", response_model=SynthOut)
async def synthesize(
    body: SynthesizeIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> SynthOut:
    res = await svc.synthesize(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        text=body.text,
        operation_id=body.operation_id,
    )
    return SynthOut(audio_ref=res["audio_ref"], model_id=res["model_id"])


@router.post("/command", response_model=NoteOut)
async def command(
    body: CommandIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> NoteOut:
    n = await svc.run_command(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        text=body.text,
    )
    return _out(n)


@router.post("/{note_id}/erase", response_model=NoteEraseOut)
async def erase(
    note_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> NoteEraseOut:
    res = await svc.gdpr_erase_note(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        note_id=note_id,
    )
    return NoteEraseOut(
        audio_ref=res.audio_ref,
        memory_blobs_deleted=res.memory_blobs_deleted,
    )
