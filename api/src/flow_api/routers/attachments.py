"""Shared attachment routes: download + delete by id. Thin adapter
(docs/adr/0001). Per-parent upload/list live in the notes and tasks
routers; the binary download and the delete are parent-agnostic so
they sit here on ``/attachments/{id}``. Member-level, org-scoped via
``tenant_ctx`` (RLS): an attachment in another org is invisible."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from flow_api.deps import TenantCtx, attachment_read_ctx, tenant_ctx
from flow_api.schemas import AttachmentCapabilityIn, AttachmentCapabilityOut, AttachmentOut
from flow_core.config import get_settings
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.attachment import Attachment
from flow_core.services import attachments as svc
from flow_core.services import capability_tokens

router = APIRouter(prefix="/attachments", tags=["attachments"])


def att_out(a: svc.AttachmentMeta | Attachment) -> AttachmentOut:
    """Serialise either the metadata projection (list) or the ORM row
    (upload) to the same wire shape. The binary ``data`` is never
    included (not a field of AttachmentOut)."""
    return AttachmentOut(
        id=a.id,
        note_id=a.note_id,
        task_id=a.task_id,
        filename=a.filename,
        mime_type=a.mime_type,
        size_bytes=a.size_bytes,
        created_at=a.created_at,
    )


async def read_capped(file: UploadFile) -> bytes:
    """Read the upload while guarding the size: read one byte past the
    cap and reject early, so an oversize body is never fully buffered
    nor stored (the service re-checks defensively)."""
    cap = get_settings().attachment_max_bytes
    data = await file.read(cap + 1)
    if len(data) > cap:
        raise DomainError(MessageCode.ATTACHMENT_TOO_LARGE)
    return data


# Re-exported for the per-parent POST/GET handlers in the notes/tasks
# routers, so the serializer and the capped read live in exactly one
# place (the binary upload field used there).
upload_file_field = Annotated[UploadFile, File()]


def _content_disposition(filename: str, mime_type: str) -> str:
    # Images render in the browser (inline) for the preview; everything
    # else is offered as a download (attachment).
    disp = "inline" if mime_type.startswith("image/") else "attachment"
    # The ASGI server latin-1 encodes header values, so a filename with
    # non-latin-1 characters (emoji, smart quotes) must not go in the bare
    # ``filename=`` -- it would raise UnicodeEncodeError and 500 the
    # download. RFC 6266: an ASCII fallback in ``filename=`` plus the full
    # UTF-8 name in ``filename*=`` (percent-encoded, RFC 5987); modern
    # browsers prefer ``filename*``.
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").replace('"', "").strip()
    if not ascii_name:
        ascii_name = "download"
    quoted = quote(filename, safe="")
    return f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


@router.post("/stream", status_code=status.HTTP_201_CREATED)
async def stream_attachment(
    request: Request,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    note_id: Annotated[uuid.UUID | None, Query()] = None,
    task_id: Annotated[uuid.UUID | None, Query()] = None,
) -> AttachmentOut:
    """Token-free large-file upload. The raw request body is streamed
    straight through the backend gateway to the object store, chunk by
    chunk: the whole file is never buffered in memory, never written to
    local disk, and S3 is never exposed to the client (medical data, the
    gateway model). The bytes ride the HTTP body, not an MCP tool
    argument, so the upload costs zero tokens. Requires the s3 attachment
    backend (``ATTACHMENT_STREAM_UNSUPPORTED`` otherwise). Exactly one
    parent (``note_id`` xor ``task_id``) must be given; the file name is
    a query param and the mime type the request ``Content-Type``."""
    att = await svc.stream_attachment(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        note_id=note_id,
        task_id=task_id,
        filename=filename,
        mime_type=request.headers.get("content-type"),
        chunks=request.stream(),
    )
    return att_out(att)


@router.post("/capability", status_code=status.HTTP_201_CREATED)
async def mint_download_capability(
    payload: AttachmentCapabilityIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AttachmentCapabilityOut:
    """Mint a parent-scoped, multi-use ``attachment:read`` capability token
    (``flow_cap_``) that downloads EVERY attachment of a note or task with no
    PAT and no X-Workspace-Id, and return the parent's attachment metadata so
    the caller can build a ``curl`` per file. Member-gated (``mint`` enforces
    the same floor a download does, so the token grants nothing the caller did
    not already hold). The raw token is returned exactly once; it is multi-use
    until ``expires_at`` and never consumed. Powers ``flow attachments
    download-capability``; the MCP ``download_attachment_capability`` tool mints
    the same grant directly through the service."""
    is_note = payload.parent_kind == "note"
    resource_kind = capability_tokens.RESOURCE_NOTE if is_note else capability_tokens.RESOURCE_TASK
    grant = await capability_tokens.mint(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        action=capability_tokens.ACTION_ATTACHMENT_READ,
        resource_kind=resource_kind,
        resource_id=payload.parent_id,
        ttl_seconds=payload.ttl_seconds,
    )
    metas = await svc.list_attachments(
        ctx.session,
        org_id=ctx.org_id,
        note_id=payload.parent_id if is_note else None,
        task_id=None if is_note else payload.parent_id,
    )
    return AttachmentCapabilityOut(
        token=grant.raw,
        expires_at=grant.expires_at,
        parent_kind=payload.parent_kind,
        parent_id=payload.parent_id,
        attachments=[att_out(m) for m in metas],
    )


@router.get("/{attachment_id}/download")
async def download_attachment(
    attachment_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(attachment_read_ctx, scope="function")],
) -> Response:
    # ``attachment_read_ctx`` accepts a normal bearer (SPA / CLI, byte
    # identical to before) OR a parent-scoped ``flow_cap_`` capability
    # token, so an agent with no PAT can fetch the file with the ephemeral
    # token alone. The scope (org + parent) is already enforced by the dep.
    att = await svc.get_attachment(
        ctx.session,
        org_id=ctx.org_id,
        attachment_id=attachment_id,
    )
    # Backend-agnostic: pg => the row's ``data`` column; s3 => the
    # object store. The response (bytes, media type, disposition) is
    # byte-identical either way, so the SPA / E2E need no change.
    body = await svc.read_attachment_bytes(att)
    return Response(
        content=body,
        media_type=att.mime_type,
        headers={
            "Content-Disposition": _content_disposition(att.filename, att.mime_type),
        },
    )


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    attachment_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.delete_attachment(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        attachment_id=attachment_id,
    )
