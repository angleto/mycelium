"""Email router: account CRUD (secret never echoed), idempotent sync,
email-to-task, send/reply. Thin adapter over the service layer
(docs/adr/0001, 0023, FR-7)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    DraftIdOut,
    EmailAccountCreateIn,
    EmailAccountOut,
    EmailAccountPatchIn,
    EmailDefaultTagsIn,
    EmailDraftApproveIn,
    EmailDraftOut,
    EmailMessageOut,
    EmailReplyIn,
    EmailSecretIn,
    EmailSendIn,
    EmailToNoteIn,
    EmailToTaskIn,
    NoteIdOut,
    SentOut,
    SyncResultOut,
    TagBrief,
    TaskIdOut,
    VersionOut,
)
from mycelium_core.models.email import EmailAccount, EmailMessage, EmailResponderJob
from mycelium_core.models.tag import Tag
from mycelium_core.services import email as svc

router = APIRouter(prefix="/email", tags=["email"])


def _tag_brief(t: Tag) -> TagBrief:
    return TagBrief(id=t.id, kind=t.kind, name=t.name, color=t.color)


def _account_out(a: EmailAccount, default_tags: list[Tag] | None = None) -> EmailAccountOut:
    return EmailAccountOut(
        id=a.id,
        provider=a.provider,
        email_address=a.email_address,
        display_name=a.display_name,
        imap_host=a.imap_host,
        imap_port=a.imap_port,
        smtp_host=a.smtp_host,
        smtp_port=a.smtp_port,
        status=a.status,
        last_sync_at=a.last_sync_at,
        last_error=a.last_error,
        ingest_to_memory=a.ingest_to_memory,
        auto_draft_replies=a.auto_draft_replies,
        default_tags=[_tag_brief(t) for t in (default_tags or [])],
        version=a.version,
    )


def _msg_out(m: EmailMessage) -> EmailMessageOut:
    return EmailMessageOut(
        id=m.id,
        account_id=m.account_id,
        provider_message_id=m.provider_message_id,
        thread_id=m.thread_id,
        message_id=m.message_id,
        in_reply_to=m.in_reply_to,
        from_addr=m.from_addr,
        to_addrs=m.to_addrs,
        subject=m.subject,
        body_text=m.body_text,
        snippet=m.snippet,
        received_at=m.received_at,
        is_read=m.is_read,
        linked_task_id=m.linked_task_id,
        linked_note_id=m.linked_note_id,
        version=m.version,
    )


def _draft_out(j: EmailResponderJob) -> EmailDraftOut:
    return EmailDraftOut(
        id=j.id,
        message_id=j.message_id,
        status=j.status,
        draft_reply=j.draft_reply,
        origin_model_id=j.origin_model_id,
        error=j.error,
        created_at=j.created_at,
        finished_at=j.finished_at,
    )


@router.post("/accounts", response_model=EmailAccountOut)
async def create_account(
    body: EmailAccountCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> EmailAccountOut:
    a = await svc.create_account(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        provider=body.provider,
        email_address=body.email_address,
        secret=body.secret,
        display_name=body.display_name,
        imap_host=body.imap_host,
        imap_port=body.imap_port,
        smtp_host=body.smtp_host,
        smtp_port=body.smtp_port,
    )
    return _account_out(a)


@router.get("/accounts", response_model=list[EmailAccountOut])
async def list_accounts(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[EmailAccountOut]:
    accounts = await svc.list_accounts(ctx.session, org_id=ctx.org_id)
    tags_by = await svc.default_tags_by_account(ctx.session, account_ids=[a.id for a in accounts])
    return [_account_out(a, tags_by.get(a.id, [])) for a in accounts]


@router.get("/accounts/{account_id}", response_model=EmailAccountOut)
async def get_account(
    account_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> EmailAccountOut:
    a = await svc.get_account(ctx.session, org_id=ctx.org_id, account_id=account_id)
    tags_by = await svc.default_tags_by_account(ctx.session, account_ids=[a.id])
    return _account_out(a, tags_by.get(a.id, []))


@router.patch("/accounts/{account_id}", response_model=VersionOut)
async def update_account(
    account_id: uuid.UUID,
    body: EmailAccountPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    values = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    version = await svc.update_account(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        account_id=account_id,
        expected_version=body.expected_version,
        values=values,
    )
    return VersionOut(id=account_id, version=version)


@router.put("/accounts/{account_id}/secret", response_model=VersionOut)
async def set_secret(
    account_id: uuid.UUID,
    body: EmailSecretIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    version = await svc.set_secret(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        account_id=account_id,
        expected_version=body.expected_version,
        secret=body.secret,
    )
    return VersionOut(id=account_id, version=version)


@router.put("/accounts/{account_id}/default-tags", response_model=VersionOut)
async def set_default_tags(
    account_id: uuid.UUID,
    body: EmailDefaultTagsIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    version = await svc.set_default_tags(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        account_id=account_id,
        expected_version=body.expected_version,
        tag_ids=body.tag_ids,
    )
    return VersionOut(id=account_id, version=version)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.delete_account(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        account_id=account_id,
    )


@router.post("/accounts/{account_id}/sync", response_model=SyncResultOut)
async def sync_account(
    account_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    limit: int = 50,
) -> SyncResultOut:
    r = await svc.sync_account(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        account_id=account_id,
        limit=limit,
    )
    return SyncResultOut(
        account_id=r.account_id,
        fetched=r.fetched,
        created=r.created,
        ok=r.ok,
        error=r.error,
    )


@router.get("/messages", response_model=list[EmailMessageOut])
async def list_messages(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    account_id: uuid.UUID | None = None,
    linked: bool | None = None,
) -> list[EmailMessageOut]:
    rows = await svc.list_messages(
        ctx.session, org_id=ctx.org_id, account_id=account_id, linked=linked
    )
    return [_msg_out(m) for m in rows]


@router.get("/messages/{message_id}", response_model=EmailMessageOut)
async def get_message(
    message_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> EmailMessageOut:
    return _msg_out(await svc.get_message(ctx.session, org_id=ctx.org_id, message_id=message_id))


@router.get("/threads/{thread_id}", response_model=list[EmailMessageOut])
async def get_thread(
    thread_id: str,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[EmailMessageOut]:
    rows = await svc.get_thread(ctx.session, org_id=ctx.org_id, thread_id=thread_id)
    return [_msg_out(m) for m in rows]


@router.post("/messages/{message_id}/to-task", response_model=TaskIdOut)
async def email_to_task(
    message_id: uuid.UUID,
    body: EmailToTaskIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TaskIdOut:
    task_id = await svc.email_to_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        message_id=message_id,
        project_tag_id=body.project_tag_id,
        tag_ids=body.tag_ids,
        assignee_ids=body.assignee_ids,
    )
    return TaskIdOut(task_id=task_id)


@router.post("/messages/{message_id}/to-note", response_model=NoteIdOut)
async def email_to_note(
    message_id: uuid.UUID,
    body: EmailToNoteIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> NoteIdOut:
    note_id = await svc.email_to_note(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        message_id=message_id,
        tag_ids=body.tag_ids,
    )
    return NoteIdOut(note_id=note_id)


@router.post("/accounts/{account_id}/send", response_model=SentOut)
async def send_message(
    account_id: uuid.UUID,
    body: EmailSendIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> SentOut:
    sent_id = await svc.send_message(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        account_id=account_id,
        to_addrs=body.to_addrs,
        subject=body.subject,
        body_text=body.body_text,
        in_reply_to=body.in_reply_to,
        references=body.references,
    )
    return SentOut(sent_id=sent_id)


@router.post("/messages/{message_id}/reply", response_model=SentOut)
async def reply(
    message_id: uuid.UUID,
    body: EmailReplyIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> SentOut:
    sent_id = await svc.reply_to_message(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        message_id=message_id,
        body_text=body.body_text,
    )
    return SentOut(sent_id=sent_id)


# --- WS-4: autonomous responder (draft review) ---


@router.post("/messages/{message_id}/draft", response_model=DraftIdOut)
async def draft_reply(
    message_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> DraftIdOut:
    """On-demand: queue a draft-reply job for this message (idempotent)."""
    job_id = await svc.enqueue_draft(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, message_id=message_id
    )
    return DraftIdOut(job_id=job_id)


@router.get("/drafts", response_model=list[EmailDraftOut])
async def list_drafts(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[EmailDraftOut]:
    rows = await svc.list_drafts(ctx.session, org_id=ctx.org_id)
    return [_draft_out(j) for j in rows]


@router.post("/drafts/{job_id}/approve", response_model=SentOut)
async def approve_draft(
    job_id: uuid.UUID,
    body: EmailDraftApproveIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> SentOut:
    sent_id = await svc.approve_draft(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        job_id=job_id,
        body_text=body.body_text,
    )
    return SentOut(sent_id=sent_id)


@router.post("/drafts/{job_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_draft(
    job_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.reject_draft(ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, job_id=job_id)
