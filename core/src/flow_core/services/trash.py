"""Empty the workspace recycle bin.

Hard-deletes every soft-deleted task and note for the current
workspace (and their attachment blobs in the object store, if any).
Restricted to ``Role.admin`` (so owners and admins, never members).

Mirrors the cascading-delete strategy of
``services.taxonomy._purge_project_subgraph`` (off-DB attachment
bytes are dropped first, then the row DELETE lets the FK CASCADEs
clear satellites).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.attachment_store import get_attachment_store
from flow_core.config import get_settings
from flow_core.models.attachment import Attachment
from flow_core.models.membership import Role
from flow_core.models.note import Note
from flow_core.models.task import Task
from flow_core.services import audit
from flow_core.services.rbac import require_role


async def empty_trash(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> dict[str, int]:
    """Permanently delete every soft-deleted task/note in the workspace.

    Returns ``{"tasks": n, "notes": m}`` with the counts purged.
    """
    await require_role(session, org_id, actor_id, Role.admin)

    task_ids = list(
        (
            await session.execute(
                select(Task.id).where(
                    Task.org_id == org_id,
                    Task.deleted_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    note_ids = list(
        (
            await session.execute(
                select(Note.id).where(
                    Note.org_id == org_id,
                    Note.deleted_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    if task_ids or note_ids:
        # S3-backed attachments need the bucket cleanup before the row
        # CASCADE drops their metadata (otherwise we orphan objects).
        conds = []
        if task_ids:
            conds.append(Attachment.task_id.in_(task_ids))
        if note_ids:
            conds.append(Attachment.note_id.in_(note_ids))
        rows = await session.execute(
            select(Attachment.storage_key).where(
                or_(*conds), Attachment.storage_key.is_not(None)
            )
        )
        keys = [k for k in rows.scalars().all() if k is not None]
        if keys:
            store = get_attachment_store(get_settings())
            for key in keys:
                await store.delete(key)

    if task_ids:
        await session.execute(delete(Task).where(Task.id.in_(task_ids)))
    if note_ids:
        await session.execute(delete(Note).where(Note.id.in_(note_ids)))
    await session.flush()

    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="workspace",
        entity_id=org_id,
        action="empty_trash",
        diff={"tasks": len(task_ids), "notes": len(note_ids)},
    )
    return {"tasks": len(task_ids), "notes": len(note_ids)}
