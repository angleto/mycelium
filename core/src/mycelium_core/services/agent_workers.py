"""Open a working session, find the ones you left behind, close them.

The model module carries the argument for why this exists at all. The
short version: one authorization yields one credential for every session
on a machine, the MCP transport is stateless on purpose so there is no
session id either, and a name the client invents can collide by accident
with nothing to notice. So the server mints the id.

Three calls, and the shape of each is decided by a failure it prevents.

:func:`open_worker` takes an ``operation_id`` because a session whose
reply was lost to a timeout would otherwise retry and end up with two
workers, holding leases under the first and renewing under the second.

:func:`list_workers` exists because a resumed session has to be able to
find what it was. Without it, a session that lost its context also lost
every lease it held, and nothing short of the deadline would free them.

:func:`close_worker` releases everything the worker still holds, and
that is the point of having it. A session being shut down is the case
where the system KNOWS the work has stopped, and making it wait out a
deadline it does not need is holding a lock for no reason. The two
recovery paths are deliberately different in cost and identical in
outcome: a session that is stopped frees its tasks now, a session that
dies frees them when its lease expires and the sweep reclaims. Neither
needs a person.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_worker import AgentWorker
from mycelium_core.models.membership import Role
from mycelium_core.services import audit
from mycelium_core.services.rbac import require_role


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def open_worker(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    label: str | None = None,
    operation_id: str | None = None,
    identity_id: uuid.UUID | None = None,
    token_id: uuid.UUID | None = None,
) -> AgentWorker:
    """Mint a worker for the calling session and return it.

    Member-level, and deliberately no higher. Opening a worker grants
    nothing: the row carries no permission, is never read as authority,
    and a caller with one can do exactly what the same caller could do
    without one. Gating it above the level at which work is done would
    make the identity harder to obtain than the actions it labels.

    Retrying with the same ``operation_id`` returns the existing row
    rather than a second worker. The uniqueness is per credential, not
    global: two different credentials may use the same key and mean
    different sessions.
    """
    await require_role(session, org_id, actor_id, Role.member)
    if operation_id and token_id is not None:
        existing = (
            await session.execute(
                select(AgentWorker).where(
                    AgentWorker.opened_by_token_id == token_id,
                    AgentWorker.operation_id == operation_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    worker = AgentWorker(
        org_id=org_id,
        opened_by_user_id=actor_id,
        opened_by_identity_id=identity_id,
        opened_by_token_id=token_id,
        label=(label[:128] if label else None),
        operation_id=(operation_id[:128] if operation_id else None),
        opened_at=_now(),
        last_seen_at=_now(),
    )
    session.add(worker)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_worker",
        entity_id=worker.id,
        action="open",
        diff={"label": worker.label or ""},
    )
    return worker


async def get_worker(
    session: AsyncSession, *, org_id: uuid.UUID, worker_id: uuid.UUID
) -> AgentWorker:
    worker = (
        await session.execute(
            select(AgentWorker).where(AgentWorker.id == worker_id, AgentWorker.org_id == org_id)
        )
    ).scalar_one_or_none()
    if worker is None:
        raise NotFoundError(MessageCode.WORKER_NOT_FOUND)
    return worker


async def touch(session: AsyncSession, *, worker_id: uuid.UUID) -> None:
    """Stamp ``last_seen_at``. A reading, not a heartbeat: nothing
    expires on it, and a worker nobody has seen for an hour still holds
    whatever it holds until the LEASE expires. Possession carries the
    deadline; this only lets a human tell a busy session from a quiet
    one."""
    await session.execute(
        update(AgentWorker).where(AgentWorker.id == worker_id).values(last_seen_at=_now())
    )


async def list_workers(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    token_id: uuid.UUID | None = None,
    include_closed: bool = False,
    limit: int = 50,
) -> Sequence[AgentWorker]:
    """Open workers, most recent first.

    ``token_id`` narrows to the ones opened on one credential, which is
    the question a resumed session asks about itself. Left out, it
    answers the question a human asks about the workspace.
    """
    stmt = select(AgentWorker).where(AgentWorker.org_id == org_id)
    if token_id is not None:
        stmt = stmt.where(AgentWorker.opened_by_token_id == token_id)
    if not include_closed:
        stmt = stmt.where(AgentWorker.closed_at.is_(None))
    stmt = stmt.order_by(AgentWorker.opened_at.desc(), AgentWorker.id.asc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def close_worker(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    worker_id: uuid.UUID,
) -> tuple[AgentWorker, list[uuid.UUID]]:
    """Mark a session finished and give back everything it holds.

    Returns the worker and the tasks that were freed. Idempotent: a
    second close frees nothing because there is nothing left to free.

    **Releasing is the whole point, not a convenience.** Being shut down
    is the one case where the system KNOWS the work has stopped, and
    making the tasks wait out a deadline nobody needs is holding a lock
    for no reason. It leaves the two recovery paths different in cost and
    identical in outcome: a session that is stopped frees its tasks now, a
    session that dies frees them when the lease expires and the sweep
    reclaims. No person is needed on either.

    The release reason is ``explicit`` rather than ``expired``: the sweep's
    numbers stay a count of sessions that died, which is the signal worth
    watching, and a clean shutdown does not pollute it.
    """
    await require_role(session, org_id, actor_id, Role.member)
    worker = await get_worker(session, org_id=org_id, worker_id=worker_id)
    # Imported here rather than at module scope: task_leases imports this
    # module for holder resolution, and the cycle is real.
    from mycelium_core.services import task_leases as _leases

    freed = await _leases.release_all_for_worker(
        session, org_id=org_id, actor_id=actor_id, worker_id=worker_id
    )
    if worker.closed_at is None:
        worker.closed_at = _now()
        worker.version += 1
        await session.flush()
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="agent_worker",
            entity_id=worker.id,
            action="close",
            diff={"freed": str(len(freed))},
        )
    return worker, freed
