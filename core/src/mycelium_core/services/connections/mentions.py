"""Mentions a person wrote in a body (ADR-0073), as connections. Listed
only: a mention changes when the text changes."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.body_reference import BodyReference, BodyReferenceSource
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart
from mycelium_core.models.task import Task
from mycelium_core.services.connections import (
    Connection,
    Direction,
    Item,
    ItemKind,
    Relation,
)
from mycelium_core.services.note_effective import effective_note_clause

N, T = ItemKind.note, ItemKind.task


class _Mentions:
    name = "mention"
    creates: frozenset[Any] = frozenset()

    async def _titles(
        self, session: AsyncSession, kind: ItemKind, ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, str | None]:
        if not ids:
            return {}
        if kind is N:
            rows = await session.execute(
                select(Note.id, Note.title).where(Note.id.in_(ids), effective_note_clause())
            )
        else:
            rows = await session.execute(
                select(Task.id, Task.title).where(Task.id.in_(ids), Task.deleted_at.is_(None))
            )
        return {i: t for i, t in rows.all()}

    async def list(
        self, session: AsyncSession, *, org_id: uuid.UUID, item: Item
    ) -> list[Connection]:
        resolved = (BodyReference.org_id == org_id, BodyReference.status == "resolved")
        # What this item's body mentions.
        out_stmt = (
            select(BodyReference.target_kind, BodyReference.target_id)
            .select_from(BodyReference)
            .join(BodyReferenceSource, BodyReferenceSource.id == BodyReference.source_id)
            .where(*resolved, BodyReference.target_kind.in_(("note", "task")))
        )
        if item.kind is N:
            out_stmt = out_stmt.join(
                NotePart, NotePart.id == BodyReferenceSource.note_part_id
            ).where(NotePart.note_id == item.id)
        else:
            out_stmt = out_stmt.where(BodyReferenceSource.task_id == item.id)
        outgoing = {
            (ItemKind(k), i) for k, i in (await session.execute(out_stmt)).all() if i != item.id
        }
        # What mentions this item.
        src_part = aliased(NotePart)
        in_stmt = (
            select(src_part.note_id, BodyReferenceSource.task_id)
            .select_from(BodyReference)
            .join(BodyReferenceSource, BodyReferenceSource.id == BodyReference.source_id)
            .outerjoin(src_part, src_part.id == BodyReferenceSource.note_part_id)
            .where(
                *resolved,
                BodyReference.target_kind == item.kind.value,
                BodyReference.target_id == item.id,
            )
        )
        incoming: set[tuple[ItemKind, uuid.UUID]] = set()
        for note_id, task_id in (await session.execute(in_stmt)).all():
            if note_id is not None and note_id != item.id:
                incoming.add((N, note_id))
            elif task_id is not None and task_id != item.id:
                incoming.add((T, task_id))
        titles: dict[uuid.UUID, str | None] = {}
        for kind in (N, T):
            ids = {i for k, i in outgoing | incoming if k is kind}
            titles.update(await self._titles(session, kind, ids))
        out: list[Connection] = []
        for direction, pairs in ((Direction.out, outgoing), (Direction.in_, incoming)):
            for kind, other in sorted(pairs, key=lambda p: str(p[1])):
                if other not in titles:
                    continue  # in the bin, or awaiting review: not followable
                out.append(
                    Connection(
                        handle=f"{self.name}:{direction.value}:{kind.value}:{other}",
                        relation=Relation.mentions,
                        direction=direction,
                        other_kind=kind,
                        other_id=other,
                        other_title=titles[other],
                        removable=False,
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
        raise DomainError(MessageCode.CONNECTION_INVALID)

    async def unlink(
        self, session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, key: str
    ) -> None:
        # A mention is the text: it is removed by editing the body.
        raise DomainError(MessageCode.CONNECTION_INVALID)


ADAPTER = _Mentions()
