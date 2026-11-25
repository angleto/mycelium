"""Connections between notes and tasks, one way for every kind (ADR-0077).

Bound to REST and MCP by the operation registry (ADR-0068). An operation
about a note and a task needs the scope of each kind it touches: reading a
task's connections needs ``tasks:read``, and a connection to a note is left
out of the answer for a caller that cannot read notes; creating or removing
a connection needs write on both ends.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, Field

from mycelium_core.errors import ForbiddenError
from mycelium_core.i18n import MessageCode
from mycelium_core.operations import Kind, OpCtx, operation
from mycelium_core.services import connections as connections_svc
from mycelium_core.services.connections import Direction, Item, ItemKind, Relation

DOMAIN = "connections"

_READ = {ItemKind.note: "notes:read", ItemKind.task: "tasks:read"}
_WRITE = {ItemKind.note: "notes:write", ItemKind.task: "tasks:write"}


def _require(ctx: OpCtx, key: str) -> None:
    if not ctx.may(key):
        raise ForbiddenError(MessageCode.AGENT_SCOPE_DENIED)


class ConnectionsIn(BaseModel):
    # ``this_*`` / ``that_*``, not item / other: the MCP gateway expands
    # short ids by argument name, and both of those names already mean
    # something else on that surface (a checklist item, a related task).
    # The kind beside each id tells the gateway what to expand.
    this_kind: ItemKind
    this_id: uuid.UUID


class ConnectionOut(BaseModel):
    """One connection seen from the item asked about. ``direction``:
    ``out`` (this item <relation> the other), ``in`` (the other <relation>
    this item), ``none`` (no direction, ``related``). ``handle`` names it
    for disconnect; ``removable`` is false for a promotion and for a
    mention, which changes only when the text changes."""

    handle: str
    relation: Relation
    direction: Direction
    other_kind: ItemKind
    other_id: uuid.UUID
    other_title: str | None = None
    removable: bool
    created_at: datetime.datetime | None = None
    detail: dict[str, Any] = {}


class ConnectionChoice(BaseModel):
    """A connection this item can make: "this item <relation> other" for
    ``out``, "other <relation> this item" for ``in``, to an item of
    ``other_kind``."""

    relation: Relation
    direction: Direction
    other_kind: ItemKind


class ConnectionsOut(BaseModel):
    items: list[ConnectionOut]
    choices: list[ConnectionChoice]


def _choices(kind: ItemKind) -> list[ConnectionChoice]:
    out: set[tuple[Relation, Direction, ItemKind]] = set()
    for relation, src, dst in connections_svc.valid_triples():
        if relation is Relation.related:
            if src is kind:
                out.add((relation, Direction.none, dst))
            continue
        if src is kind:
            out.add((relation, Direction.out, dst))
        if dst is kind:
            out.add((relation, Direction.in_, src))
    order = {r: i for i, r in enumerate(Relation)}
    return [
        ConnectionChoice(relation=r, direction=d, other_kind=k)
        for r, d, k in sorted(out, key=lambda x: (order[x[0]], x[1].value, x[2].value))
    ]


@operation(
    "connections.list",
    domain=DOMAIN,
    kind=Kind.READ,
    access=frozenset({"notes:read", "tasks:read"}),
    params=ConnectionsIn,
    result=ConnectionsOut,
)
async def connections_list(ctx: OpCtx, p: ConnectionsIn) -> ConnectionsOut:
    """Every connection of a note or a task, from every mechanism (typed
    links between notes, between a note and a task, related tasks,
    subtasks, dependencies, mentions written in a body), each with its
    relation and direction, and the connections this item can make
    (``choices``)."""
    _require(ctx, _READ[p.this_kind])
    rows = await connections_svc.list_connections(
        ctx.session, org_id=ctx.org_id, item=Item(kind=p.this_kind, id=p.this_id)
    )
    return ConnectionsOut(
        items=[
            ConnectionOut(
                handle=c.handle,
                relation=c.relation,
                direction=c.direction,
                other_kind=c.other_kind,
                other_id=c.other_id,
                other_title=c.other_title,
                removable=c.removable,
                created_at=c.created_at,
                detail=dict(c.detail),
            )
            for c in rows
            if ctx.may(_READ[c.other_kind])
        ],
        choices=[c for c in _choices(p.this_kind) if ctx.may(_WRITE[c.other_kind])],
    )


class ConnectIn(BaseModel):
    """Connect this item to another: ``direction`` ``out`` makes "this item
    <relation> other", ``in`` makes "other <relation> this item", ``none``
    for ``related``. A dependency takes ``dependency_type`` (FS, SS, FF,
    SF; default FS) and ``lag_minutes``."""

    this_kind: ItemKind
    this_id: uuid.UUID
    relation: Relation
    direction: Direction
    that_kind: ItemKind
    that_id: uuid.UUID
    dependency_type: str | None = Field(None, max_length=2)
    lag_minutes: int | None = None


class DisconnectIn(BaseModel):
    """``handle`` as connections.list gave it, and the item it was listed
    for (whose write scope the disconnect needs)."""

    this_kind: ItemKind
    this_id: uuid.UUID
    handle: str = Field(min_length=3, max_length=200)
    that_kind: ItemKind


class ConnectionActionOut(BaseModel):
    done: bool


@operation(
    "connections.connect",
    domain=DOMAIN,
    kind=Kind.WRITE,
    access=frozenset({"notes:write", "tasks:write"}),
    params=ConnectIn,
    result=ConnectionActionOut,
)
async def connections_connect(ctx: OpCtx, p: ConnectIn) -> ConnectionActionOut:
    """Connect a note or a task to another note or task with one of the
    relations connections.list offers as ``choices``. Refused with
    connection.invalid otherwise."""
    _require(ctx, _WRITE[p.this_kind])
    _require(ctx, _WRITE[p.that_kind])
    me, other = Item(kind=p.this_kind, id=p.this_id), Item(kind=p.that_kind, id=p.that_id)
    source, target = (other, me) if p.direction is Direction.in_ else (me, other)
    await connections_svc.link(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.actor_id,
        source=source,
        relation=p.relation,
        target=target,
        options={"dependency_type": p.dependency_type, "lag_minutes": p.lag_minutes},
    )
    return ConnectionActionOut(done=True)


@operation(
    "connections.disconnect",
    domain=DOMAIN,
    kind=Kind.WRITE,
    access=frozenset({"notes:write", "tasks:write"}),
    params=DisconnectIn,
    result=ConnectionActionOut,
)
async def connections_disconnect(ctx: OpCtx, p: DisconnectIn) -> ConnectionActionOut:
    """Remove a connection by the ``handle`` connections.list gave. A
    promotion and a mention cannot be removed this way."""
    _require(ctx, _WRITE[p.this_kind])
    _require(ctx, _WRITE[p.that_kind])
    await connections_svc.unlink(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.actor_id, handle=p.handle
    )
    return ConnectionActionOut(done=True)
