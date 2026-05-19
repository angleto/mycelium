"""Attachments on notes / tasks: upload, list (metadata), read, delete.

DB-BYTEA storage (no object store; co-tenant deploy). RBAC is
member-level (notes/tasks are member-level, ADR-0001/0017), the parent
must exist in the current org (RLS), the size is capped server-side
before the bytes are persisted, and the filename is sanitised to a
safe basename. The MIME type is taken from the client content-type and
normalised; an OPTIONAL ``python-magic`` sniff refines it ONLY if the
library is importable (same lesson as the embedder: never hard-require
an optional dependency, fall back to the client mime + an extension
allowlist when libmagic is unavailable). All mutations are audited.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.config import get_settings
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.attachment import Attachment
from flow_core.models.membership import Role
from flow_core.models.note import Note
from flow_core.models.task import Task
from flow_core.services import audit
from flow_core.services.rbac import require_role

# Fallback when no content-type is supplied and the sniff is
# unavailable: the generic binary type (RFC 2046).
_DEFAULT_MIME = "application/octet-stream"

# Extension -> mime allowlist used ONLY as the fallback when
# python-magic/libmagic is not importable AND the client did not send a
# usable content-type. Deliberately small (the common preview/doc set);
# anything unknown stays the generic binary type.
_EXT_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".json": "application/json",
    ".zip": "application/zip",
}


@dataclass(frozen=True)
class AttachmentMeta:
    """List projection: metadata only, NEVER the ``data`` column (the
    list query does not select it, so a large blob is never loaded to
    render a row)."""

    id: uuid.UUID
    note_id: uuid.UUID | None
    task_id: uuid.UUID | None
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime
    uploaded_by: uuid.UUID


def _sanitize_filename(name: str) -> str:
    """Strip any path (defensive: a multipart filename is attacker
    controlled), keep the basename, cap at 255 (the column width).
    Falls back to a stable default if nothing usable remains."""
    base = os.path.basename(name.replace("\\", "/")).strip()
    base = base.lstrip(".") or "file"
    return base[:255]


def _sniff_mime(data: bytes) -> str | None:
    """Best-effort content sniff. ``python-magic`` is OPTIONAL and not a
    declared dependency: if it (or libmagic) is missing we return None
    and the caller falls back to the client mime / extension allowlist.
    Never raises for a missing optional dep (embedder lesson)."""
    try:
        import magic  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except Exception:  # pragma: no cover - optional dep absent in CI
        return None
    try:
        sniffed = magic.from_buffer(data, mime=True)  # pragma: no cover
    except Exception:  # pragma: no cover - libmagic runtime failure
        return None
    return sniffed or None  # pragma: no cover


def _resolve_mime(*, filename: str, client_mime: str | None, data: bytes) -> str:
    """Trust the client content-type but normalise it; refine with an
    optional sniff when available; otherwise fall back to the extension
    allowlist, then the generic binary type."""
    sniffed = _sniff_mime(data)
    if sniffed:
        return sniffed[:160]
    if client_mime:
        mt = client_mime.split(";")[0].strip().lower()
        if mt and mt != _DEFAULT_MIME:
            return mt[:160]
    # Reached only when the client mime is absent or the generic binary
    # type, so the ext allowlist (or the generic fallback) decides.
    _, ext = os.path.splitext(filename.lower())
    return _EXT_MIME.get(ext, _DEFAULT_MIME)[:160]


async def _assert_parent(
    session: AsyncSession,
    *,
    note_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
) -> None:
    """Exactly one parent, and it must exist in the current org (RLS
    already scopes the SELECT; a soft-deleted parent still accepts
    attachments, mirroring how the work note opens a trashed task)."""
    if (note_id is None) == (task_id is None):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if note_id is not None:
        found = (
            await session.execute(select(Note.id).where(Note.id == note_id))
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    else:
        found = (
            await session.execute(select(Task.id).where(Task.id == task_id))
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError(MessageCode.TASK_NOT_FOUND)


async def add_attachment(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    filename: str,
    mime_type: str | None,
    data: bytes,
) -> Attachment:
    """Store a file on a note XOR a task. Member-level. The size is
    enforced against ``settings.attachment_max_bytes`` BEFORE the row is
    added; the filename is sanitised; the mime is normalised."""
    await require_role(session, org_id, actor_id, Role.member)
    await _assert_parent(session, note_id=note_id, task_id=task_id)
    max_bytes = get_settings().attachment_max_bytes
    if len(data) > max_bytes:
        raise DomainError(MessageCode.ATTACHMENT_TOO_LARGE)
    safe_name = _sanitize_filename(filename)
    resolved_mime = _resolve_mime(filename=safe_name, client_mime=mime_type, data=data)
    att = Attachment(
        org_id=org_id,
        note_id=note_id,
        task_id=task_id,
        filename=safe_name,
        mime_type=resolved_mime,
        size_bytes=len(data),
        data=data,
        uploaded_by=actor_id,
    )
    session.add(att)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="attachment",
        entity_id=att.id,
        action="create",
    )
    return att


async def list_attachments(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
) -> list[AttachmentMeta]:
    """Metadata for a parent's attachments, newest first. The query
    selects only the metadata columns: the ``data`` BYTEA is NEVER
    loaded here (no large blob fetched to render a list)."""
    stmt = select(
        Attachment.id,
        Attachment.note_id,
        Attachment.task_id,
        Attachment.filename,
        Attachment.mime_type,
        Attachment.size_bytes,
        Attachment.created_at,
        Attachment.uploaded_by,
    )
    if note_id is not None:
        stmt = stmt.where(Attachment.note_id == note_id)
    if task_id is not None:
        stmt = stmt.where(Attachment.task_id == task_id)
    stmt = stmt.order_by(Attachment.created_at.desc())
    rows = (await session.execute(stmt)).all()
    return [
        AttachmentMeta(
            id=r.id,
            note_id=r.note_id,
            task_id=r.task_id,
            filename=r.filename,
            mime_type=r.mime_type,
            size_bytes=r.size_bytes,
            created_at=r.created_at,
            uploaded_by=r.uploaded_by,
        )
        for r in rows
    ]


async def get_attachment(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> Attachment:
    """Full row including ``data`` (for download). Org-scoped via RLS:
    an attachment in another org is invisible -> NotFoundError."""
    att = (
        await session.execute(select(Attachment).where(Attachment.id == attachment_id))
    ).scalar_one_or_none()
    if att is None:
        raise NotFoundError(MessageCode.ATTACHMENT_NOT_FOUND)
    return att


async def delete_attachment(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> None:
    """Hard-delete (the blob is gone with the row). Member-level,
    org-scoped (RLS); audited."""
    await require_role(session, org_id, actor_id, Role.member)
    att = (
        await session.execute(select(Attachment).where(Attachment.id == attachment_id))
    ).scalar_one_or_none()
    if att is None:
        raise NotFoundError(MessageCode.ATTACHMENT_NOT_FOUND)
    await session.delete(att)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="attachment",
        entity_id=attachment_id,
        action="delete",
    )


__all__: Sequence[str] = (
    "AttachmentMeta",
    "add_attachment",
    "delete_attachment",
    "get_attachment",
    "list_attachments",
)
