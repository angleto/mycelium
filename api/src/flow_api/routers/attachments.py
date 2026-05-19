"""Shared attachment routes: download + delete by id. Thin adapter
(docs/adr/0001). Per-parent upload/list live in the notes and tasks
routers; the binary download and the delete are parent-agnostic so
they sit here on ``/attachments/{id}``. Member-level, org-scoped via
``tenant_ctx`` (RLS): an attachment in another org is invisible."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Response, UploadFile, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import AttachmentOut
from flow_core.config import get_settings
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.attachment import Attachment
from flow_core.services import attachments as svc

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
    # else is offered as a download (attachment). The filename is
    # already sanitised in the service; quote it defensively.
    disp = "inline" if mime_type.startswith("image/") else "attachment"
    safe = filename.replace('"', "")
    return f'{disp}; filename="{safe}"'


@router.get("/{attachment_id}/download")
async def download_attachment(
    attachment_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> Response:
    att = await svc.get_attachment(
        ctx.session,
        org_id=ctx.org_id,
        attachment_id=attachment_id,
    )
    return Response(
        content=att.data,
        media_type=att.mime_type,
        headers={
            "Content-Disposition": _content_disposition(att.filename, att.mime_type),
        },
    )


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    attachment_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.delete_attachment(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        attachment_id=attachment_id,
    )
