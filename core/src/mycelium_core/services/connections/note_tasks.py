"""Typed links between a note and a task (ADR-0029): subject, artifact,
derived_from, promoted_from."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import Note
from mycelium_core.models.note_link import NoteTaskLink
from mycelium_core.models.task import Task
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services.connections import (
    Connection,
    Direction,
    Item,
    ItemKind,
    Relation,
)
from mycelium_core.services.note_effective import effective_note_clause

N, T = ItemKind.note, ItemKind.task

#: Stored kind -> (relation, the kind of item that is the subject of the
#: sentence). ``subject``: the task works on the note. ``artifact``: the
#: note is the result of the task. ``derived_from``: the task came out of
#: the note. ``promoted_from``: the note became the task.
_MEANING: dict[str, tuple[Relation, ItemKind]] = {
    "subject": (Relation.works_on, T),
    "artifact": (Relation.result_of, N),
    "derived_from": (Relation.derives_from, T),
    "promoted_from": (Relation.promoted_to, N),
}


class _NoteTasks:
    name = "notetask"
    #: Only the two free links: a derivation and a promotion are made by
    #: the actions that create the task, not by connecting two items.
    creates = frozenset({(Relation.works_on, T, N), (Relation.result_of, N, T)})

    async def list(
        self, session: AsyncSession, *, org_id: uuid.UUID, item: Item
    ) -> list[Connection]:
        stmt = (
            select(NoteTaskLink, Note.title, Task.title)
            .join(Note, Note.id == NoteTaskLink.note_id)
            .join(Task, Task.id == NoteTaskLink.task_id)
            .where(
                NoteTaskLink.org_id == org_id,
                effective_note_clause(),
                Task.deleted_at.is_(None),
            )
        )
        stmt = stmt.where(
            NoteTaskLink.note_id == item.id if item.kind is N else NoteTaskLink.task_id == item.id
        )
        out: list[Connection] = []
        for link, note_title, task_title in (await session.execute(stmt)).all():
            meaning = _MEANING.get(link.kind)
            if meaning is None:
                continue
            relation, subject_kind = meaning
            out.append(
                Connection(
                    handle=f"{self.name}:{link.note_id}:{link.task_id}:{link.kind}",
                    relation=relation,
                    direction=Direction.out if item.kind is subject_kind else Direction.in_,
                    other_kind=T if item.kind is N else N,
                    other_id=link.task_id if item.kind is N else link.note_id,
                    other_title=task_title if item.kind is N else note_title,
                    # The promotion cannot be undone by unlinking (the note
                    # keeps ``promoted_at``); everything else can.
                    removable=link.kind != "promoted_from",
                    created_at=link.created_at,
                )
            )
        return out

    async def link(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID,
        source: Item,
        relation: Relation,
        target: Item,
        options: dict[str, Any],
    ) -> None:
        if relation is Relation.works_on:
            await note_links_svc.start_task_on_note(
                session, org_id=org_id, actor_id=actor_id, task_id=source.id, note_id=target.id
            )
        else:
            await note_links_svc.record_task_artifact(
                session, org_id=org_id, actor_id=actor_id, task_id=target.id, note_id=source.id
            )

    async def unlink(
        self, session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, key: str
    ) -> None:
        try:
            note_id, task_id, kind = key.split(":")
            note_uuid, task_uuid = uuid.UUID(note_id), uuid.UUID(task_id)
        except ValueError:
            raise DomainError(MessageCode.CONNECTION_INVALID) from None
        if kind == "promoted_from":
            raise DomainError(MessageCode.CONNECTION_INVALID)
        await note_links_svc.unlink_note_task(
            session,
            org_id=org_id,
            actor_id=actor_id,
            note_id=note_uuid,
            task_id=task_uuid,
            kind=kind,
        )


ADAPTER = _NoteTasks()
