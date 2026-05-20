"""Attachments on notes / tasks: upload, list (metadata), read, delete.

Pluggable storage (``attachment_store.py``): the DEFAULT ``pg`` backend
keeps the bytes in the ``attachments.data`` BYTEA column (atomic with
the row, no object store -- the original co-tenant design, byte-for-
byte unchanged); the ``s3`` backend offloads the bytes to an object
store and the row carries only ``storage_key``. RBAC is member-level
(notes/tasks are member-level, ADR-0001/0017), the parent must exist in
the current org (RLS), the size is capped server-side before the bytes
are persisted, and the filename is sanitised to a safe basename. The
MIME type is taken from the client content-type and normalised; an
OPTIONAL ``python-magic`` sniff refines it ONLY if the library is
importable (same lesson as the embedder: never hard-require an optional
dependency, fall back to the client mime + an extension allowlist when
libmagic is unavailable). All mutations are audited.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.attachment_store import PgAttachmentStore, get_attachment_store
from flow_core.config import get_settings
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.attachment import Attachment
from flow_core.models.membership import Role
from flow_core.models.note import Note
from flow_core.models.note_tag import NoteTag
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import Task
from flow_core.models.task_tag import TaskTag
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


async def _resolve_client_tag_id(
    session: AsyncSession,
    *,
    task_id: uuid.UUID | None,
    note_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """The client tag (UUID) the parent belongs to, or None.

    A task / note carries a client either directly (a ``client`` tag) or
    via its project (a ``project`` tag whose ProjectProfile.client_tag_id
    points at the client). The first match wins; both queries are RLS-
    scoped so they only see the current org's rows. Returns None when
    the parent has no client/project tag (-> stored under .../misc).
    """
    if task_id is not None:
        # Direct client tag on the task?
        row = (
            await session.execute(
                select(Tag.id)
                .join(TaskTag, TaskTag.tag_id == Tag.id)
                .where(TaskTag.task_id == task_id, Tag.kind == TagKind.client)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
        # Project tag -> ProjectProfile.client_tag_id ?
        row = (
            await session.execute(
                select(ProjectProfile.client_tag_id)
                .join(TaskTag, TaskTag.tag_id == ProjectProfile.tag_id)
                .where(TaskTag.task_id == task_id, ProjectProfile.client_tag_id.is_not(None))
                .limit(1)
            )
        ).scalar_one_or_none()
        return row
    if note_id is not None:
        row = (
            await session.execute(
                select(Tag.id)
                .join(NoteTag, NoteTag.tag_id == Tag.id)
                .where(NoteTag.note_id == note_id, Tag.kind == TagKind.client)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
        row = (
            await session.execute(
                select(ProjectProfile.client_tag_id)
                .join(NoteTag, NoteTag.tag_id == ProjectProfile.tag_id)
                .where(NoteTag.note_id == note_id, ProjectProfile.client_tag_id.is_not(None))
                .limit(1)
            )
        ).scalar_one_or_none()
        return row
    return None


def _build_storage_key(
    *,
    org_id: uuid.UUID,
    client_tag_id: uuid.UUID | None,
    parent_kind: str,  # "tasks" | "notes"
    parent_id: uuid.UUID,
    attachment_id: uuid.UUID,
    filename: str,
) -> str:
    """Hierarchical S3 key. All material related to a client lives under
    one folder (``org/<org>/client/<client_uid>/...``) so opening a
    client folder surfaces everything at a glance; orphan attachments
    (no client / no project tag) go under ``org/<org>/misc/...``.

    The filename is appended for human browsing (the bucket UI shows it),
    and was already sanitised upstream (``_sanitize_filename``)."""
    if client_tag_id is None:
        return f"org/{org_id}/misc/{attachment_id}/{filename}"
    return (
        f"org/{org_id}/client/{client_tag_id}/{parent_kind}/"
        f"{parent_id}/{attachment_id}/{filename}"
    )


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
    store = get_attachment_store(get_settings())
    is_pg = isinstance(store, PgAttachmentStore)
    att = Attachment(
        org_id=org_id,
        note_id=note_id,
        task_id=task_id,
        filename=safe_name,
        mime_type=resolved_mime,
        size_bytes=len(data),
        # pg backend: bytes in the row, exactly as before this seam.
        # s3 backend: data stays NULL; storage_key is set after flush
        # (the key is the attachment id, only known once generated).
        data=data if is_pg else None,
        uploaded_by=actor_id,
    )
    session.add(att)
    await session.flush()
    if not is_pg:
        # Hierarchical S3 key: opens a client's folder to find tasks/,
        # notes/, invoicing/ side by side. Orphans (no client) go under
        # .../misc/. The key is computed AFTER the row exists because the
        # attachment_id is part of the path.
        client_tag_id = await _resolve_client_tag_id(
            session, task_id=task_id, note_id=note_id
        )
        parent_kind = "tasks" if task_id is not None else "notes"
        # _assert_parent enforces task_id XOR note_id, so one of the two
        # is set here; fall back to the attachment id (impossible at
        # runtime) only to keep the type checker satisfied.
        parent_id = task_id or note_id or att.id
        key = _build_storage_key(
            org_id=org_id,
            client_tag_id=client_tag_id,
            parent_kind=parent_kind,
            parent_id=parent_id,
            attachment_id=att.id,
            filename=safe_name,
        )
        await store.put(key, data, resolved_mime)
        att.storage_key = key
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
    """Full row (for download). Org-scoped via RLS: an attachment in
    another org is invisible -> NotFoundError. The bytes are fetched via
    ``read_attachment_bytes`` (legacy/pg: the ``data`` column; s3: the
    object store), so the HTTP download is byte-identical either way."""
    att = (
        await session.execute(select(Attachment).where(Attachment.id == attachment_id))
    ).scalar_one_or_none()
    if att is None:
        raise NotFoundError(MessageCode.ATTACHMENT_NOT_FOUND)
    return att


async def read_attachment_bytes(att: Attachment) -> bytes:
    """The file bytes for download, backend-agnostic. Legacy / ``pg``
    rows have them inline in ``data``; ``s3`` rows have ``storage_key``
    set and the bytes in the object store. Returns exactly the bytes
    that were uploaded so the HTTP response is unchanged."""
    if att.storage_key is not None:
        store = get_attachment_store(get_settings())
        return await store.get(att.storage_key)
    if att.data is None:  # pragma: no cover - defensive: a row with neither
        raise NotFoundError(MessageCode.ATTACHMENT_NOT_FOUND)
    return att.data


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
    # s3 backend: drop the object too, else the row goes but the bytes
    # leak in the bucket. Done before the row delete so a store failure
    # aborts the whole unit of work (no orphaned object, no lost row).
    # pg backend: the bytes are in the row and go with it (no-op here).
    if att.storage_key is not None:
        store = get_attachment_store(get_settings())
        await store.delete(att.storage_key)
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
    "read_attachment_bytes",
)
