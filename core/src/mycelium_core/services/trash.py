"""Empty the workspace recycle bin.

Hard-deletes every soft-deleted task and note for the current
workspace (and their attachment blobs in the object store, if any),
plus every trashed note PART -- those live in their own side table
(migration 0089) and belong to notes that are usually still alive, so
no note purge would ever reach them.
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

from mycelium_core.attachment_store import get_attachment_store
from mycelium_core.config import get_settings
from mycelium_core.models.attachment import Attachment
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart, NotePartTrash
from mycelium_core.models.task import Task
from mycelium_core.services import audit
from mycelium_core.services.memory import erase_blobs_for_sources
from mycelium_core.services.rbac import require_role


async def empty_trash(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> dict[str, int]:
    """Permanently delete every soft-deleted task/note in the workspace,
    plus every trashed note part.

    Returns ``{"tasks": n, "notes": m, "note_parts": p}`` with the counts
    purged.
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
            select(Attachment.storage_key).where(or_(*conds), Attachment.storage_key.is_not(None))
        )
        keys = [k for k in rows.scalars().all() if k is not None]
        if keys:
            store = get_attachment_store(get_settings())
            for key in keys:
                await store.delete(key)

    # Erase the search blobs by provenance BEFORE the row DELETEs (task
    # c5da112c): the bulk Core DELETE below bypasses the ORM mapper
    # listeners that normally clean the index blobs, and the note_part
    # rows (whose ids the provenance carries) die in the row CASCADE --
    # collecting them afterwards would be too late. Without this, a
    # purged note/task leaves orphaned, still-retrievable blobs.
    # Whole-entity pairs are included ON PURPOSE: emptying the bin is a
    # SOVEREIGN human act (admin-gated), so it mirrors ``gdpr_erase`` --
    # a derived memory whose only provenance was a purged row dies with
    # it. The autonomous retention sweep deliberately does NOT do this
    # (see ``entity_revisions.hard_delete_soft_deleted``).
    sources: list[tuple[str, str]] = [("task", str(tid)) for tid in task_ids]
    sources.extend(("note", str(nid)) for nid in note_ids)
    if note_ids:
        part_ids = (
            (await session.execute(select(NotePart.id).where(NotePart.note_id.in_(note_ids))))
            .scalars()
            .all()
        )
        sources.extend(("note_part", str(pid)) for pid in part_ids)
    blobs_deleted = await erase_blobs_for_sources(session, sources=sources)

    if task_ids:
        await session.execute(delete(Task).where(Task.id.in_(task_ids)))
    if note_ids:
        await session.execute(delete(Note).where(Note.id.in_(note_ids)))
    # Trashed note PARTS are in the bin too (migration 0089), so emptying
    # the bin empties them -- including those belonging to notes that are
    # very much alive. Entries whose note was purged above are already
    # gone with it (FK note_id ON DELETE CASCADE); this DELETE catches the
    # rest. No blob sweep: a part's search blob is dropped when it is
    # trashed, not when it is purged.
    trashed_part_ids = list(
        (await session.execute(select(NotePartTrash.id).where(NotePartTrash.org_id == org_id)))
        .scalars()
        .all()
    )
    if trashed_part_ids:
        await session.execute(delete(NotePartTrash).where(NotePartTrash.id.in_(trashed_part_ids)))
    parts_purged = len(trashed_part_ids)
    await session.flush()

    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="workspace",
        entity_id=org_id,
        action="empty_trash",
        diff={
            "tasks": len(task_ids),
            "notes": len(note_ids),
            "note_parts": parts_purged,
            "blobs": blobs_deleted,
        },
    )
    return {"tasks": len(task_ids), "notes": len(note_ids), "note_parts": parts_purged}
