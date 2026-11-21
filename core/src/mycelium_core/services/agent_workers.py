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

:func:`close_idle` is the same argument for the worker itself. A session
that dies never calls :func:`close_worker`, so without it the worker stays
open forever and the sessions panel counts the dead as working.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_token import AgentToken
from mycelium_core.models.agent_worker import AgentWorker
from mycelium_core.models.identity import Identity
from mycelium_core.models.membership import Role
from mycelium_core.models.task_lease import TaskLease
from mycelium_core.services import audit
from mycelium_core.services import caller as caller_svc
from mycelium_core.services.rbac import require_role


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def caller_identity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    token_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """The identity behind the calling credential: token -> assistant ->
    identity, the chain ``whoami`` walks, else the user's own identity.

    A bare or legacy token has no assistant row and falls through to the
    user, which is recorded as what it is rather than guessed at. One
    function, because the identity recorded when a worker is opened and
    the identity a later call is compared with must be the same answer
    to the same question (:func:`belongs_to`).
    """
    identity_id: uuid.UUID | None = None
    if token_id is not None:
        identity_id = (
            await session.execute(
                select(Identity.id)
                .select_from(AgentToken)
                .join(Identity, Identity.ai_assistant_id == AgentToken.assistant_id)
                .where(AgentToken.id == token_id, Identity.org_id == org_id)
            )
        ).scalar_one_or_none()
    if identity_id is None:
        identity_id = (
            await session.execute(
                select(Identity.id).where(Identity.user_id == actor_id, Identity.org_id == org_id)
            )
        ).scalar_one_or_none()
    return identity_id


def belongs_to(
    worker: AgentWorker,
    *,
    actor_id: uuid.UUID,
    identity_id: uuid.UUID | None,
    token_id: uuid.UUID | None,
) -> bool:
    """Was this worker opened by the caller asking to act under it?

    A worker id is visible to every member (``list_workers``), and the
    close check records it as the session that checked the work. Accepting
    any id in the workspace let one assistant name another assistant's
    worker and be recorded as it, which made that record forgeable.

    The binding is to the IDENTITY, not to the token. Every session of one
    assistant shares its identity, so naming a sibling session's worker is
    still possible, and that is the limit ADR-0072 already states: one
    model can open two workers. The token would be finer and wrong: a
    credential is refreshed under a running session, and the session's
    worker must survive that.

    A worker opened before the identity was recorded carries none. It is
    matched on its user and, where both sides have one, its token: the
    finest key it has, for the short time such a worker can still be open.
    """
    if worker.opened_by_user_id != actor_id:
        return False
    if worker.opened_by_identity_id is not None:
        return worker.opened_by_identity_id == identity_id
    if worker.opened_by_token_id is not None and token_id is not None:
        return worker.opened_by_token_id == token_id
    return True


async def open_worker(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    label: str | None = None,
    operation_id: str | None = None,
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

    Who opened it is read from the session, not taken from the caller:
    it is what :func:`belongs_to` compares every later use with, and a
    provenance the caller supplies is one it can choose.
    """
    await require_role(session, org_id, actor_id, Role.member)
    if token_id is None:
        token_id = (await caller_svc.current(session)).token_id
    identity_id = await caller_identity(
        session, org_id=org_id, actor_id=actor_id, token_id=token_id
    )
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
    """Stamp ``last_seen_at`` on an OPEN worker, and refuse a closed one.

    A reading for a human, and since the idle close it is also what keeps a
    quiet session from being taken for a dead one: :func:`close_idle` only
    closes a worker whose ``last_seen_at`` is old.

    The refusal lives in the UPDATE, not in the read before it, because the
    read can be stale. A caller that read the worker as open while the idle
    close was committing would otherwise stamp a closed row and go on to
    take a lease under it, and a lease on a closed worker is one no
    ``worker_close`` will ever give back. With ``closed_at IS NULL`` in the
    predicate the two orders both come out right: if the close commits
    first, this UPDATE waits on its row lock, re-reads the row, finds it
    closed and matches nothing; if this UPDATE runs first, the close waits
    on the row lock this transaction now holds and re-reads a
    ``last_seen_at`` that is no longer old.
    """
    stamped = (
        await session.execute(
            update(AgentWorker)
            .where(AgentWorker.id == worker_id, AgentWorker.closed_at.is_(None))
            .values(last_seen_at=_now())
            .returning(AgentWorker.id)
        )
    ).scalar_one_or_none()
    if stamped is None:
        raise NotFoundError(MessageCode.WORKER_NOT_FOUND)


async def list_workers(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    token_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    include_closed: bool = False,
    limit: int = 50,
) -> Sequence[AgentWorker]:
    """Open workers, most recent first.

    ``token_id`` narrows to the ones opened on one credential, which is
    the question a resumed session asks about itself. Left out, it
    answers the question a human asks about the workspace.

    ``user_id`` is the same question asked where there is no credential
    to key on. A browser session has no token, so narrowing it by one
    would return nothing at all; every worker records who opened it, and
    for a person that is the finest key there is. The two are not the
    same narrowing and must not be described as one: a credential is
    shared by the sessions launched on a machine, and a user by every
    credential they hold.
    """
    stmt = select(AgentWorker).where(AgentWorker.org_id == org_id)
    if token_id is not None:
        stmt = stmt.where(AgentWorker.opened_by_token_id == token_id)
    if user_id is not None:
        stmt = stmt.where(AgentWorker.opened_by_user_id == user_id)
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
    numbers stay a count of sessions that died, and a clean shutdown does
    not pollute it.

    **The worker row is written first, and conditionally.** Every path that
    takes or keeps work under a worker stamps its row first (``touch``, an
    UPDATE refused on a closed worker) and only then writes a lease. Closing
    used to go the other way, leases and then the worker row, and the two
    orders deadlocked: a renewal holding the worker row and waiting on its
    lease, against a close holding the lease and waiting on the worker row
    (measured 3 runs out of 3). Taking the worker row first puts every path
    in one order, and it also decides the race with an acquire of the same
    worker. If the close's UPDATE wins, the acquire's ``touch`` waits on it,
    then finds the worker closed and is refused. If the acquire's ``touch``
    wins, the close waits until the acquire commits its lease, and the
    release that follows reads after that commit, so it gives the new lease
    back too. Either way no lease is left on a closed worker.
    """
    await require_role(session, org_id, actor_id, Role.member)
    worker = await get_worker(session, org_id=org_id, worker_id=worker_id)
    # Imported here rather than at module scope: task_leases imports this
    # module for holder resolution, and the cycle is real.
    from mycelium_core.services import task_leases as _leases

    closed_now = (
        await session.execute(
            update(AgentWorker)
            .where(AgentWorker.id == worker_id, AgentWorker.closed_at.is_(None))
            .values(closed_at=_now(), version=AgentWorker.version + 1)
            .returning(AgentWorker.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    # A second close still gives back whatever is left: it frees nothing
    # when the first one did its job, and that costs one read.
    freed = await _leases.release_all_for_worker(
        session, org_id=org_id, actor_id=actor_id, worker_id=worker_id
    )
    await session.refresh(worker)
    if closed_now is not None:
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


async def close_idle(
    session: AsyncSession,
    *,
    idle_for: dt.timedelta,
    limit: int = 200,
    now: dt.datetime | None = None,
) -> list[uuid.UUID]:
    """Close the workers of sessions that ended without saying so.

    A worker used to close only through :func:`close_worker`, and a session
    that is killed, crashes or runs out of context calls nothing. Measured
    in production on 2026-11-17: 167 workers opened since the feature
    shipped, 10 closed, 157 still open, one of them holding a task. The
    sessions panel counted all 157 as working.

    Closed here: a worker nobody has seen for ``idle_for`` that holds no
    unreleased lease. Both conditions, because each alone is wrong. Silence
    alone would close a session in the middle of a long lease it took with
    a long TTL; holding nothing alone would close a session between two
    tasks. A worker that still holds an expired lease is left for the next
    pass, after the lease sweep has reclaimed it: this function never
    releases anything, so it never competes with the paths that do.

    The decision is one conditional UPDATE, so nothing between a read and
    a write can make it stale; :func:`touch` carries the other half of the
    argument. A session that was only quiet and comes back is refused with
    ``worker.not_found``, opens a new worker and carries on: it held
    nothing, so it lost nothing.

    Logged as a ``close`` with no actor, like the lease sweep's releases:
    nobody acted, and the ``system`` kind of the session says who did.
    """
    at = now or _now()
    cutoff = at - idle_for
    live_lease = (
        select(TaskLease.id)
        .where(
            TaskLease.holder_worker_id == AgentWorker.id,
            TaskLease.released_at.is_(None),
        )
        .exists()
    )
    idle = and_(
        AgentWorker.closed_at.is_(None),
        func.coalesce(AgentWorker.last_seen_at, AgentWorker.opened_at) < cutoff,
        ~live_lease,
    )
    candidates = (
        select(AgentWorker.id)
        .where(idle)
        .order_by(func.coalesce(AgentWorker.last_seen_at, AgentWorker.opened_at).asc())
        .limit(limit)
        .scalar_subquery()
    )
    # The predicate is repeated on the UPDATE itself: the candidates were
    # chosen on a snapshot, and only the UPDATE's own WHERE is re-checked
    # against a row that a concurrent ``touch`` changed under it.
    closed = (
        await session.execute(
            update(AgentWorker)
            .where(AgentWorker.id.in_(candidates), idle)
            .values(closed_at=at, version=AgentWorker.version + 1)
            .returning(AgentWorker.id, AgentWorker.org_id, AgentWorker.last_seen_at)
            .execution_options(synchronize_session=False)
        )
    ).all()
    for row in closed:
        await audit.log(
            session,
            org_id=row.org_id,
            actor_id=None,
            entity="agent_worker",
            entity_id=row.id,
            action="close",
            diff={
                "reason": "idle",
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else "",
                "freed": "0",
            },
        )
    return [row.id for row in closed]
