"""Change watermark over the append-only activity log.

A client holding a rendered view (the task list, the board) has no way to
learn that an MCP session, the CLI or the worker moved something under it.
This is the cheap question it can ask repeatedly: "how many things have
happened in my workspace, in the part of it this view draws, since the
instant you gave me?". A number that moves is the whole signal; what to do
about it is the caller's business, and the answer is always to re-read the
view through the ordinary authorised path, never to patch it from here.

Why the activity log and not ``tasks.updated_at``: attaching a tag or a
collaborator writes a junction row and NEVER touches the task row (see
``services.tasks.attach_tag`` / ``assign``), yet both are drawn on the list
row. The audit entry, on the other hand, is written by the same choke point
that made the change whatever table it touched (docs/adr/0002), so it sees
the edits an agent makes most.

Why a scope rather than "everything": the covering index leads on ``entity``
(``ix_activity_log_org_entity_ts``), so a probe that does not name the kinds
cannot use it. Measured on 200k rows, one org: 0.22 ms with the enumeration,
25.8 ms without it, the second being a parallel sequential scan that grows
with the workspace's history while the first does not. The enumeration is
what makes polling free, not an optimisation on top of it.
"""

from __future__ import annotations

import datetime
import enum
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.activity_log import ActivityLog

# How far back a bootstrap places its own starting instant. ``ts`` on an
# audit row is ``now()``, which in Postgres is the TRANSACTION's start time,
# so a write that began before this call and commits after it carries a
# timestamp older than the instant we are about to hand out, and would never
# be counted. Two seconds covers the mutating transactions on this path
# (single-statement updates); it cannot cover an arbitrarily long one, and a
# cursor that could would have to be commit-ordered (a snapshot xmin) rather
# than clock-ordered. The cost of the overlap is that a bootstrap starts with
# a non-zero count, which is exactly why the caller compares the number
# against the one it last accepted rather than against zero.
BOOTSTRAP_OVERLAP = datetime.timedelta(seconds=2)


class WatchScope(enum.StrEnum):
    """What a watcher is watching. A closed set, because it decides which
    index range is read: an unrecognised value must fail rather than fall
    back to a probe that reads the whole log."""

    tasks = "tasks"


# The audit entity kinds whose change can alter what a task list row or a
# board card draws. ``task`` carries more than the table's own columns: the
# tag choke point logs an attach/detach against the ENTITY it moved the tag
# on (services/tag_assignment), not against the tag, and so does assignment.
# ``tag`` is here for the other direction, a tag renamed or recoloured under
# a chip that is already drawn, and ``workflow`` for a state renamed under
# the state select.
_SCOPE_ENTITIES: dict[WatchScope, tuple[str, ...]] = {
    WatchScope.tasks: (
        "task",
        "task_checklist",
        "task_checklist_item",
        "task_handoff",
        "task_lease",
        "task_relation",
        "tag",
        "workflow",
    ),
}


@dataclass(frozen=True, slots=True)
class Watermark:
    """``changes`` entries landed in the scope after ``since``.

    ``since`` is always the instant the SERVER used, echoed back, so a client
    never has to trust its own clock against the database's: it holds this
    value and hands it back unchanged on the next probe.
    """

    since: datetime.datetime
    changes: int


def scope_entities(scope: WatchScope) -> tuple[str, ...]:
    """The audit entity kinds a scope watches. Exposed for the test that
    pins them against the kinds the services actually write."""
    return _SCOPE_ENTITIES[scope]


async def watermark(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    scope: WatchScope,
    since: datetime.datetime | None = None,
) -> Watermark:
    """Count what has landed in ``scope`` since ``since``.

    With no ``since``, this is a bootstrap: the starting instant is the
    database's clock less :data:`BOOTSTRAP_OVERLAP`, and the count that comes
    back is the caller's baseline rather than a reason to refresh anything.
    """
    if since is None:
        now = (await session.execute(select(func.now()))).scalar_one()
        since = now - BOOTSTRAP_OVERLAP
    stmt = select(func.count()).where(
        ActivityLog.org_id == org_id,
        ActivityLog.entity.in_(_SCOPE_ENTITIES[scope]),
        ActivityLog.ts > since,
    )
    changes = (await session.execute(stmt)).scalar_one()
    return Watermark(since=since, changes=int(changes))
