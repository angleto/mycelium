"""Typed links between two notes (ADR-0040): related, hypha_of, supersedes,
contradicts."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import Note
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services.connections import (
    Connection,
    Direction,
    Item,
    ItemKind,
    Relation,
)
from mycelium_core.services.note_effective import effective_note_clause

#: Stored kind -> relation. ``hypha_of`` stores (parent = origin, child =
#: derived); ``supersedes`` and ``contradicts`` store (parent = the one that
#: replaces or refutes, child = the one replaced or refuted).
_RELATION = {
    "related": Relation.related,
    "hypha_of": Relation.derives_from,
    "supersedes": Relation.supersedes,
    "contradicts": Relation.contradicts,
}
_KIND = {v: k for k, v in _RELATION.items()}
N = ItemKind.note


class _NoteNotes:
    name = "notelink"
    creates = frozenset((r, N, N) for r in _RELATION.values())

    async def list(
        self, session: AsyncSession, *, org_id: uuid.UUID, item: Item
    ) -> list[Connection]:
        if item.kind is not N:
            return []
        parent, child = aliased(Note), aliased(Note)
        rows = (
            await session.execute(
                select(NoteNoteLink, parent.title, child.title)
                .join(parent, parent.id == NoteNoteLink.parent_note_id)
                .join(child, child.id == NoteNoteLink.child_note_id)
                .where(
                    NoteNoteLink.org_id == org_id,
                    or_(
                        NoteNoteLink.parent_note_id == item.id,
                        NoteNoteLink.child_note_id == item.id,
                    ),
                    effective_note_clause(entity=parent),
                    effective_note_clause(entity=child),
                )
            )
        ).all()
        out: list[Connection] = []
        for link, p_title, c_title in rows:
            relation = _RELATION.get(link.kind)
            if relation is None:
                continue
            item_is_child = link.child_note_id == item.id
            other_id = link.parent_note_id if item_is_child else link.child_note_id
            other_title = p_title if item_is_child else c_title
            if relation is Relation.related:
                direction = Direction.none
            elif relation is Relation.derives_from:
                # The derived note is the child: seen from it, it derives
                # from the other.
                direction = Direction.out if item_is_child else Direction.in_
            else:
                # The note that replaces or refutes is the parent.
                direction = Direction.in_ if item_is_child else Direction.out
            out.append(
                Connection(
                    handle=f"{self.name}:{link.id}",
                    relation=relation,
                    direction=direction,
                    other_kind=N,
                    other_id=other_id,
                    other_title=other_title,
                    removable=True,
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
        # "source derives from target": the target is the origin (parent).
        # "source supersedes/contradicts target": the source is the parent.
        if relation is Relation.derives_from:
            parent, child = target.id, source.id
        else:
            parent, child = source.id, target.id
        await note_links_svc.link_notes(
            session,
            org_id=org_id,
            actor_id=actor_id,
            parent_note_id=parent,
            child_note_id=child,
            kind=_KIND[relation],
        )

    async def unlink(
        self, session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, key: str
    ) -> None:
        link = await session.get(NoteNoteLink, uuid.UUID(key))
        if link is None or link.org_id != org_id:
            raise NotFoundError(MessageCode.CONNECTION_NOT_FOUND)
        await note_links_svc.unlink_notes(
            session,
            org_id=org_id,
            actor_id=actor_id,
            parent_note_id=link.parent_note_id,
            child_note_id=link.child_note_id,
            kind=link.kind,
        )


ADAPTER = _NoteNotes()
