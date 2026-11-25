"""One way to connect two things, notes or tasks (ADR-0077).

Seven mechanisms connect two items in Mycelium, each with its own table and
invariants: typed note-to-note links, typed note-to-task links, related
tasks, subtasks, task dependencies, and the mentions a person writes in a
body. A person met them in seven tabs, under different names for the same
act, with the direction left to guess. This package gives them one contract:

- a ``Connection`` row says, from the point of view of one item, which
  ``Relation`` binds it to which other item and in which direction (``out``:
  this item <relation> the other; ``in``: the other <relation> this item;
  ``none`` for a relation without direction);
- ``link`` and ``unlink`` are the same two verbs for every mechanism;
- each mechanism publishes an ``ADAPTER`` in a module of this package and
  declares the (relation, from kind, to kind) triples it carries; the set of
  valid connections is read from the adapters, so it has one source.

Storage and rules stay where they are enforced today: the dependency DAG and
its lag, the canonical related pair, the decay a ``supersedes`` sets, the
promotion that unlinking cannot undo.
"""

from __future__ import annotations

import datetime as dt
import enum
import importlib
import logging
import pkgutil
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode

log = logging.getLogger(__name__)


class ItemKind(enum.StrEnum):
    note = "note"
    task = "task"


class Relation(enum.StrEnum):
    #: The two are connected, no direction (note-note, task-task).
    related = "related"
    #: The task is the work on the note (note-task ``subject``).
    works_on = "works_on"
    #: One comes from the other (note-note ``hypha_of``; a task that fell
    #: out of a note, ``derived_from``).
    derives_from = "derives_from"
    #: The note became the task (``promoted_from``). Read-only.
    promoted_to = "promoted_to"
    #: The note is what the task produced (``artifact``).
    result_of = "result_of"
    supersedes = "supersedes"
    contradicts = "contradicts"
    #: The task is part of the other (``parent_task_id``).
    subtask_of = "subtask_of"
    #: The task waits for the other (``task_dependencies``).
    depends_on = "depends_on"
    #: The body names the other (``body_reference``). Read-only here: it is
    #: changed by editing the text.
    mentions = "mentions"


class Direction(enum.StrEnum):
    out = "out"
    in_ = "in"
    none = "none"


@dataclass(frozen=True)
class Connection:
    """One connection of an item, seen from that item. ``handle`` names it
    for ``unlink``; ``removable`` says whether unlink can remove it (a
    promotion and a mention cannot)."""

    handle: str
    relation: Relation
    direction: Direction
    other_kind: ItemKind
    other_id: uuid.UUID
    other_title: str | None
    removable: bool
    created_at: dt.datetime | None = None
    #: Mechanism-specific detail worth showing: a dependency's type and lag.
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Item:
    kind: ItemKind
    id: uuid.UUID


Triple = tuple[Relation, ItemKind, ItemKind]


class Adapter(Protocol):
    #: The prefix of this adapter's handles.
    name: str
    #: (relation, from kind, to kind) this adapter can CREATE: "from
    #: <relation> to". Relations it only lists (a promotion, a mention) are
    #: not here.
    creates: frozenset[Triple]

    async def list(
        self, session: AsyncSession, *, org_id: uuid.UUID, item: Item
    ) -> list[Connection]: ...

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
    ) -> None: ...

    async def unlink(
        self, session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, key: str
    ) -> None: ...


def _discover() -> dict[str, Adapter]:
    found: dict[str, Adapter] = {}
    for mod in pkgutil.iter_modules(__path__):
        try:
            module = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception:  # a broken adapter fails alone and is reported
            log.exception("connection adapter %s failed to load", mod.name)
            continue
        adapter = getattr(module, "ADAPTER", None)
        if adapter is not None:
            found[adapter.name] = adapter
    return found


_ADAPTERS: dict[str, Adapter] | None = None


def adapters() -> dict[str, Adapter]:
    global _ADAPTERS
    if _ADAPTERS is None:
        _ADAPTERS = _discover()
    return _ADAPTERS


def valid_triples() -> frozenset[Triple]:
    """Every connection a person can create: the union of what the adapters
    declare. The one source of which relations apply to which pair."""
    out: set[Triple] = set()
    for a in adapters().values():
        out |= a.creates
    return frozenset(out)


def _invalid() -> DomainError:
    return DomainError(MessageCode.CONNECTION_INVALID)


async def list_connections(
    session: AsyncSession, *, org_id: uuid.UUID, item: Item
) -> list[Connection]:
    """Every connection of ``item``, from every mechanism, in a stable order:
    by relation, then direction, then the other item's title."""
    rows: list[Connection] = []
    for name in sorted(adapters()):
        rows.extend(await adapters()[name].list(session, org_id=org_id, item=item))
    order = {r: i for i, r in enumerate(Relation)}
    return sorted(
        rows,
        key=lambda c: (order[c.relation], c.direction.value, (c.other_title or "").lower()),
    )


async def link(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source: Item,
    relation: Relation,
    target: Item,
    options: dict[str, Any] | None = None,
) -> None:
    """Create "source <relation> target". Refused with
    ``connection.invalid`` when no mechanism carries that relation between
    those kinds of item, or when both ends are the same item."""
    if source == target:
        raise _invalid()
    triple = (relation, source.kind, target.kind)
    for adapter in adapters().values():
        if triple in adapter.creates:
            await adapter.link(
                session,
                org_id=org_id,
                actor_id=actor_id,
                source=source,
                relation=relation,
                target=target,
                options=options or {},
            )
            return
    raise _invalid()


async def unlink(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, handle: str
) -> None:
    """Remove the connection ``handle`` names (as ``list_connections`` gave
    it). A connection that cannot be removed this way is refused."""
    name, _, key = handle.partition(":")
    adapter = adapters().get(name)
    if adapter is None or not key:
        raise _invalid()
    await adapter.unlink(session, org_id=org_id, actor_id=actor_id, key=key)


__all__ = [
    "Adapter",
    "Connection",
    "Direction",
    "Item",
    "ItemKind",
    "Relation",
    "Triple",
    "adapters",
    "link",
    "list_connections",
    "unlink",
    "valid_triples",
]
