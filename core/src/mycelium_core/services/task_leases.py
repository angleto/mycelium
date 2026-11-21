"""Possession of a task: take it, keep it, hand it back, reclaim it.

State says where a task is; possession says who is on it and until when.
Mycelium modelled the first and not the second, and every collision
between concurrent agent sessions is that absence (migration 0017 has
the argument, including why a closed design refused a lease table and
why its own criterion selects for this one).

**The two places the exclusion actually lives, neither of which is a
check in this file.**

1. ``uq_task_leases_live``, a partial unique index on ``task_id WHERE
   released_at IS NULL``: at most one live lease per task, decided by the
   datastore.
2. ``SELECT ... FOR UPDATE OF tasks SKIP LOCKED`` on the task row, taken
   by BOTH entry points (:func:`acquire` by id and :func:`pull` from a
   queue) before either touches the lease table. That is what makes the
   two paths serialise against each other on one lock rather than
   against the index from two directions.

The second is load-bearing and was not obvious. Without it, ``pull``
picking a task whose expired-but-unreleased lease row still occupies the
index would insert, lose on the index, and report an empty queue while
work was waiting -- a silent false negative, which is the worst failure
shape a queue has. With the row lock, a puller cannot even select a task
another caller is acquiring, so the insert cannot lose.

``SKIP LOCKED`` also means a puller steps over a task that any other
transaction happens to be updating. That is correct rather than merely
tolerable: the task is momentarily unavailable, there are others, and
the alternative is fifteen sessions queueing behind one row lock.

**What this does not do.** It does not stop two agents editing the same
files. Mycelium is not a filesystem mutex and a lease here is advisory on
that plane: it says who holds the work, not who holds the file. The
enforceable answer there is per-agent working-tree isolation, on the
client side.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.errors import ConflictError, ForbiddenError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_worker import AgentWorker
from mycelium_core.models.membership import Role
from mycelium_core.models.task import Task
from mycelium_core.models.task_lease import LeaseRelease, TaskLease
from mycelium_core.models.user import User
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import audit
from mycelium_core.services import caller as caller_svc
from mycelium_core.services.rbac import require_role

# How many times ``pull`` re-picks after losing a candidate. It is small
# and it is stated because SCL-07 asks a bound to say what happens when
# it is reached: on exhaustion the queue reports empty, which is the
# truthful answer -- every candidate it could see was taken by somebody
# while it looked. The row lock makes losing rare; the bound exists for
# the case where it is not.
_PULL_ATTEMPTS = 4


class Holder:
    """Who is asking.

    ``worker_id`` is the discriminator and the SERVER issued it
    (``agent_workers``); the other three are what the request already
    carried. Assembled once per call rather than threaded through
    signatures, because three of the four travel on the session -- they
    are the GUCs the audit log has always read -- and only the worker is
    named by the caller, with an id it was given rather than one it
    invented.
    """

    __slots__ = ("caller", "identity_id", "token_id", "user_id", "worker_id")

    def __init__(
        self,
        *,
        worker_id: uuid.UUID | None,
        user_id: uuid.UUID,
        identity_id: uuid.UUID | None,
        caller: caller_svc.Caller,
    ) -> None:
        self.worker_id = worker_id
        self.user_id = user_id
        self.identity_id = identity_id
        self.caller = caller
        self.token_id = caller.token_id

    def holds(self, lease: TaskLease) -> bool:
        """Is this lease mine?

        Two rules, because a lease has two kinds of holder and conflating
        them would be wrong in both directions.

        A lease taken BY A WORKER is matched only by that worker. Falling
        back to "or the same identity" would make every session in this
        workspace the holder of every other session's lease, since they
        share one credential, and that is the exact failure the worker
        exists to prevent.

        A lease taken with NO worker -- the UI, anything that predates
        this -- is matched by its user, because that is the only
        identity such a caller has. A caller holding a worker never
        matches one of those: it has a worker, so it is asked the first
        question.
        """
        if lease.holder_worker_id is not None:
            return self.worker_id is not None and lease.holder_worker_id == self.worker_id
        return self.worker_id is None and lease.holder_user_id == self.user_id


async def resolve_holder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    worker_id: uuid.UUID | None,
) -> Holder:
    """Fill the holder slots from the session and the caller's worker.

    ``worker_id`` omitted is legitimate and is not a shared bucket: the
    caller is held as its user, which is the truth about a client that
    never opened a working session.

    What it must NOT do is derive a fallback from the credential, which
    is what an earlier version of this function did. Every agent session
    here shares one credential, so that fallback quietly made fifteen
    sessions one holder -- the very failure the mechanism exists to
    prevent, reintroduced by its own default.

    A worker id that does not resolve, that has been closed, or that
    another caller opened, is refused rather than ignored. A caller that believes it holds a
    session and does not is about to take a lease under an identity it
    cannot renew.
    """
    caller = await caller_svc.current(session)
    # The token an agent presented, over MCP or over REST. Reading the kind
    # alone took only MCP: REST publishes an agent token as ``human_api``
    # with the token as subject, and that agent was held as a person.
    token_id = caller.token_id

    identity_id = await workers_svc.caller_identity(
        session, org_id=org_id, actor_id=actor_id, token_id=token_id
    )

    if worker_id is not None:
        # Checked here, once, rather than in each caller. A closed worker
        # has already given its work back; taking new work under it would
        # put a lease on a session that has announced it is gone.
        worker = await workers_svc.get_worker(session, org_id=org_id, worker_id=worker_id)
        # Another caller's worker is refused as not found, the same answer
        # as an id that does not exist: acting under it would record this
        # caller as that session (``workers_svc.belongs_to``).
        if worker.closed_at is not None or not workers_svc.belongs_to(
            worker, actor_id=actor_id, identity_id=identity_id, token_id=token_id
        ):
            raise NotFoundError(MessageCode.WORKER_NOT_FOUND)
        await workers_svc.touch(session, worker_id=worker_id)

    return Holder(
        worker_id=worker_id,
        user_id=actor_id,
        identity_id=identity_id,
        caller=caller,
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _ends_work(session: AsyncSession, state_id: uuid.UUID) -> bool:
    """Does entering this state end the work: terminal, or declared done
    (``closes_work``)? The same two flags ``tasks.set_state`` reads."""
    row = (
        await session.execute(
            select(WorkflowState.is_terminal, WorkflowState.closes_work).where(
                WorkflowState.id == state_id
            )
        )
    ).one_or_none()
    return row is not None and bool(row[0] or row[1])


def _deadline(now: dt.datetime, ttl_seconds: int | None) -> dt.datetime:
    s = get_settings()
    ttl = ttl_seconds if ttl_seconds is not None else s.task_lease_ttl_seconds
    ttl = max(s.task_lease_min_ttl_seconds, min(int(ttl), s.task_lease_max_ttl_seconds))
    return now + dt.timedelta(seconds=ttl)


async def held_by(session: AsyncSession, lease: TaskLease) -> str:
    """A name for whoever is holding a task, for the refusal message.

    Resolved server-side and put in the error's params rather than left
    to the caller, because every surface needs it and none of them can
    get it cheaply: a refused write is exactly the moment a caller has no
    second round trip to spend, and an agent reading the prose has no
    other way to learn it at all.

    The worker's own label when it gave one, its id when it did not, and
    the user's handle for a lease nobody took under a worker. Never
    nothing: "held by another worker" with no name is the message this
    exists to replace.
    """
    if lease.holder_worker_id is not None:
        label = (
            await session.execute(
                select(AgentWorker.label).where(AgentWorker.id == lease.holder_worker_id)
            )
        ).scalar_one_or_none()
        return label or str(lease.holder_worker_id)
    handle = (
        await session.execute(select(User.handle).where(User.id == lease.holder_user_id))
    ).scalar_one_or_none()
    return handle or str(lease.holder_user_id)


async def live_lease(session: AsyncSession, *, task_id: uuid.UUID) -> TaskLease | None:
    """The unreleased lease row for a task, expired or not.

    "Unreleased" rather than "unexpired" on purpose: an expired row still
    occupies the unique index, so a caller deciding what to do next needs
    to see it. Ask :meth:`TaskLease.is_live` whether it still binds.
    """
    return (
        await session.execute(
            select(TaskLease).where(TaskLease.task_id == task_id, TaskLease.released_at.is_(None))
        )
    ).scalar_one_or_none()


async def _lock_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    """Take the task row for this transaction. See the module docstring:
    this, not the index, is what makes the two acquire paths serialise
    against each other."""
    return (
        await session.execute(
            select(Task)
            .where(Task.id == task_id, Task.deleted_at.is_(None))
            .with_for_update(of=Task)
        )
    ).scalar_one_or_none()


async def _end_leases(
    session: AsyncSession,
    *,
    lease_ids: Sequence[uuid.UUID],
    reason: LeaseRelease,
    at: dt.datetime,
    expired_by: dt.datetime | None = None,
) -> set[uuid.UUID]:
    """End the leases that are still live, and say which ones THIS call ended.

    The one place a lease is released. ``released_at IS NULL`` is in the
    UPDATE, and the caller logs only what came back, because every path that
    ends a lease can race another one on the same row: the sweep and the
    next holder reclaiming it, the sweep and a worker closing, the sweep and
    the holder giving it back. Writing by primary key, as three of the four
    paths did, made the second writer wait on the first's row lock and then
    release the row again -- a second activity row for one release, and the
    first reason overwritten (verification of task 9d4088e9, 2026-11-16).
    Under READ COMMITTED the waiting UPDATE re-reads the row after the first
    commits, sees it released, and takes nothing.

    ``expired_by`` is for the two paths that end a lease BECAUSE its deadline
    passed, the sweep and a reclaim: the deadline goes into the same WHERE,
    so it is re-read with the row. Without it a renewal committed while the
    sweep waited on the row was ended anyway, as expired, and its holder had
    been told it was extended (third verification of task 9d4088e9). A
    release the holder or the owner asked for does not pass it: those end
    the lease whatever its deadline says.
    """
    if not lease_ids:
        return set()
    still_held: list[ColumnElement[bool]] = [
        TaskLease.id.in_(list(lease_ids)),
        TaskLease.released_at.is_(None),
    ]
    if expired_by is not None:
        still_held.append(TaskLease.expires_at <= expired_by)
    return set(
        (
            await session.execute(
                update(TaskLease)
                .where(*still_held)
                .values(
                    released_at=at,
                    release_reason=reason.value,
                    version=TaskLease.version + 1,
                )
                .returning(TaskLease.id)
                .execution_options(synchronize_session="fetch")
            )
        ).scalars()
    )


async def _log_expiry(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lease_id: uuid.UUID,
    task_id: uuid.UUID,
    holder_worker_id: uuid.UUID | None,
    expires_at: dt.datetime,
) -> None:
    """The activity row for a lease that ended because its deadline passed.

    One writer for both places that end such a lease -- the sweep, and the
    next holder reclaiming it on ``acquire`` or ``pull`` -- because they
    record the same event: a session stopped answering. No actor, because
    nobody released it; the row names the worker that went silent and the
    deadline it missed. The caller that happened to find it writes its own
    ``acquire`` row next to this one.
    """
    await audit.log(
        session,
        org_id=org_id,
        actor_id=None,
        entity="task_lease",
        entity_id=lease_id,
        action="release",
        diff={
            "task_id": str(task_id),
            "reason": LeaseRelease.expired.value,
            "holder_worker_id": str(holder_worker_id) if holder_worker_id else None,
            "expired_at": expires_at.isoformat(),
        },
    )


async def _extend(
    session: AsyncSession, *, lease: TaskLease, now: dt.datetime, ttl_seconds: int | None
) -> bool:
    """Push the deadline of a lease that is still unreleased. False when it
    was ended between the caller's read and this write.

    The mirror of :func:`_end_leases`, and conditional for the same reason:
    renewing and the idempotent re-acquire wrote by primary key, so a lease
    the sweep had just ended got a new deadline on a released row, and the
    caller was told it held a task nobody held (third verification of task
    9d4088e9).
    """
    await session.flush()
    extended = (
        await session.execute(
            update(TaskLease)
            .where(TaskLease.id == lease.id, TaskLease.released_at.is_(None))
            .values(
                expires_at=_deadline(now, ttl_seconds),
                renewed_at=now,
                version=TaskLease.version + 1,
            )
            .returning(TaskLease.id)
            .execution_options(synchronize_session="fetch")
        )
    ).scalar_one_or_none()
    await session.refresh(lease)
    return extended is not None


async def _reclaim_expired(session: AsyncSession, *, lease: TaskLease, now: dt.datetime) -> bool:
    """Release an expired row so the index is free for the next holder.
    True when the index is free, whoever freed it.

    Called with the task row already locked, so no second caller can be
    reclaiming the same row concurrently. Logged like the sweep's releases:
    whether the sweep or the next holder gets there first is timing, and
    the activity log must not depend on it.

    The holder's own renewal does not take the task lock, so it can land
    between the caller's read and this write. The deadline is re-checked in
    the UPDATE, and a lease renewed in that window is left alone: False,
    and the caller treats the task as held.
    """
    await session.flush()
    if lease.id not in await _end_leases(
        session, lease_ids=[lease.id], reason=LeaseRelease.expired, at=now, expired_by=now
    ):
        await session.refresh(lease)
        # Released: the sweep ended it first, and logged it. Unreleased:
        # its holder renewed it, and it binds again.
        return lease.released_at is not None
    await _log_expiry(
        session,
        org_id=lease.org_id,
        lease_id=lease.id,
        task_id=lease.task_id,
        holder_worker_id=lease.holder_worker_id,
        expires_at=lease.expires_at,
    )
    return True


async def _insert_lease(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    state_id: uuid.UUID,
    holder: Holder,
    now: dt.datetime,
    expires_at: dt.datetime,
    fence: int,
) -> TaskLease:
    lease = TaskLease(
        org_id=org_id,
        task_id=task_id,
        state_id=state_id,
        holder_worker_id=holder.worker_id,
        holder_user_id=holder.user_id,
        holder_identity_id=holder.identity_id,
        holder_token_id=holder.token_id,
        acquired_at=now,
        expires_at=expires_at,
        fence=fence,
    )
    session.add(lease)
    await session.flush()
    return lease


async def _next_fence(session: AsyncSession, task_id: uuid.UUID) -> int:
    """One past the highest fence this task has ever issued.

    Monotone per task and never reset, so a holder reclaimed long ago
    carries a number that can no longer match. Counted over released rows
    too, which is the point: the reclaimed holder's own row is released.
    """
    highest = (
        await session.execute(select(func.max(TaskLease.fence)).where(TaskLease.task_id == task_id))
    ).scalar_one_or_none()
    return int(highest or 0) + 1


async def acquire(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_id: uuid.UUID | None = None,
    ttl_seconds: int | None = None,
    preempt: bool = False,
) -> TaskLease:
    """Take possession of one named task.

    Idempotent for the same worker: re-acquiring a lease you already hold
    extends it rather than failing, because a session that lost its reply
    to a timeout must be able to retry without having to know whether the
    first call landed.

    ``preempt`` is the owner taking a task back from whoever holds it.
    Owner-gated like the other privileged task operations, and recorded
    as its own release reason so a sweep's numbers stay a measure of
    sessions that died rather than of decisions somebody took.
    """
    await require_role(session, org_id, actor_id, Role.member)
    now = _now()
    holder = await resolve_holder(session, org_id=org_id, actor_id=actor_id, worker_id=worker_id)

    task = await _lock_task(session, task_id)
    if task is None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)

    existing = await live_lease(session, task_id=task_id)
    if existing is not None:
        if holder.holds(existing):
            # Mine already: extend, do not fail. Same row, so the fence
            # does not move -- nobody else ever held it.
            if await _extend(session, lease=existing, now=now, ttl_seconds=ttl_seconds):
                return existing
            # The sweep ended it between the read and the write. The task
            # row is locked here, so nobody else can have taken it since:
            # take it again as a new possession, with a new fence and its
            # own acquire row, below.
        elif existing.is_live(now):
            if not preempt:
                raise ConflictError(
                    MessageCode.LEASE_HELD_BY_OTHER,
                    holder=await held_by(session, existing),
                    expires_at=existing.expires_at.isoformat(),
                )
            await require_role(session, org_id, actor_id, Role.owner)
            # Through ``release`` so the preempted lease gets its own
            # activity row; only the new holder's ``acquire`` was written
            # before, and the one interrupted was nowhere.
            await release(
                session,
                org_id=org_id,
                actor_id=actor_id,
                task_id=task_id,
                reason=LeaseRelease.preempted,
                require_holder=False,
            )
        elif not await _reclaim_expired(session, lease=existing, now=now):
            raise ConflictError(
                MessageCode.LEASE_HELD_BY_OTHER,
                holder=await held_by(session, existing),
                expires_at=existing.expires_at.isoformat(),
            )

    lease = await _insert_lease(
        session,
        org_id=org_id,
        task_id=task_id,
        state_id=task.state_id,
        holder=holder,
        now=now,
        expires_at=_deadline(now, ttl_seconds),
        fence=await _next_fence(session, task_id),
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_lease",
        entity_id=lease.id,
        action="acquire",
        diff={"task_id": str(task_id), "worker_id": str(holder.worker_id)},
    )
    return lease


def _unheld(now: dt.datetime) -> Any:
    """The predicate for "nobody is on this task".

    Expired-and-unreleased counts as unheld, which is why ``pull`` must
    reclaim before it inserts: the row is still in the unique index even
    though this predicate has stopped seeing it.
    """
    return ~exists().where(
        and_(
            TaskLease.task_id == Task.id,
            TaskLease.released_at.is_(None),
            TaskLease.expires_at > now,
        )
    )


def _not_my_handoff(holder: Holder) -> Any:
    """Tasks this worker did not hand off itself.

    The workflow's own description of the checking station says the check
    is done by somebody other than whoever did the work. This is that
    sentence as a predicate. It keys on the worker, and on ``handoff``
    specifically: a task the worker merely finished (``done``) or gave up
    (``explicit``) is not excluded, because neither is the author
    presenting their own work to be checked.

    A caller with no worker is excluded from nothing, which is correct
    and is not a hole: it has never handed anything off under a worker,
    so there is nothing of its own to skip. The rule constrains the
    sessions it can actually tell apart.
    """
    if holder.worker_id is None:
        return true()
    return ~exists().where(
        and_(
            TaskLease.task_id == Task.id,
            TaskLease.holder_worker_id == holder.worker_id,
            TaskLease.release_reason == LeaseRelease.handoff.value,
        )
    )


async def pull(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    state_id: uuid.UUID,
    worker_id: uuid.UUID | None = None,
    ttl_seconds: int | None = None,
    transition_to: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    exclude_own_handoffs: bool = True,
) -> tuple[Task, TaskLease]:
    """Take the next unheld task in a station, in one round trip.

    This is the call that removes the pile-up. Today every session reads
    a deterministic ranking and then writes, and fifteen sessions asking
    a deterministic ranking the same question get the same answer; the
    gap between the read and the write is all the room the collision
    needs. Here the choosing and the taking are one statement under one
    row lock.

    The ordering is ``list_tasks``'s default, unchanged and on purpose:
    the head of the queue has to be the head of the list an agent was
    shown, or the two surfaces disagree about what "next" means.

    ``exclude_own_handoffs`` is the verification rule (see
    :func:`_not_my_handoff`), and it defaults ON.

    Its precondition is not what it first looks like. It is not that
    every agent has its own identity: the predicate keys on the WORKER
    id, so distinct workers are enough and two sessions on one assistant
    already satisfy it as long as each names itself. What would break it
    is callers that omit ``worker_id`` and fall back to the credential,
    because then every checker really is the author and every check is
    refused. Provisioning one credential per agent closes that too, by
    making the fallback itself distinct -- which is why it is on by
    default here and not before that provisioning existed.

    It stays a parameter because a caller reprocessing its own work
    deliberately (a re-check after a fix, a single-agent workspace) has a
    legitimate reason to turn it off, and that is a decision for the
    caller rather than a state of the world.
    """
    await require_role(session, org_id, actor_id, Role.member)
    holder = await resolve_holder(session, org_id=org_id, actor_id=actor_id, worker_id=worker_id)

    for _ in range(_PULL_ATTEMPTS):
        now = _now()
        stmt: Select[Any] = (
            select(Task.id)
            .where(
                Task.state_id == state_id,
                Task.deleted_at.is_(None),
                Task.is_archived.is_(False),
                _unheld(now),
            )
            .order_by(Task.priority.asc(), Task.created_at.desc(), Task.id.asc())
            .limit(1)
            .with_for_update(of=Task, skip_locked=True)
        )
        if tag_id is not None:
            from mycelium_core.models.task_tag import TaskTag

            stmt = stmt.where(
                exists().where(and_(TaskTag.task_id == Task.id, TaskTag.tag_id == tag_id))
            )
        if assignee_id is not None:
            stmt = stmt.where(Task.assignee_id == assignee_id)
        if exclude_own_handoffs:
            stmt = stmt.where(_not_my_handoff(holder))

        candidate_id = (await session.execute(stmt)).scalar_one_or_none()
        if candidate_id is None:
            raise NotFoundError(MessageCode.LEASE_QUEUE_EMPTY)

        # The row is locked for this transaction, so nothing else can be
        # acquiring it. An expired row may still hold the index: release
        # it before inserting, or the insert loses and the queue lies
        # about being empty.
        stale = await live_lease(session, task_id=candidate_id)
        if stale is not None:
            if stale.is_live(now):
                # Only reachable if the lease was taken between the
                # predicate and the lock, which the lock is supposed to
                # prevent; treat it as a lost candidate and re-pick
                # rather than asserting it cannot happen.
                continue
            if not await _reclaim_expired(session, lease=stale, now=now):
                continue  # renewed by its holder meanwhile: a lost candidate

        task = await _lock_task(session, candidate_id)
        if task is None:
            continue

        state_for_lease = state_id
        if transition_to is not None and transition_to != task.state_id:
            from mycelium_core.services import tasks as tasks_svc

            if not await _ends_work(session, transition_to):
                # Taken where it was found, then moved: the same two acts as
                # take and set_state, so the move is recorded as this
                # worker passing the task on, a handoff lease and
                # ``handed_over_by_worker_id``, like any other. Moving it
                # before any lease existed recorded nobody, and a task
                # pulled into a checking station could then be closed by no
                # agent at all. A move that ends the work is left without a
                # lease on purpose: ``assert_may_close`` must find none, and
                # refuse the take-and-close in one call.
                await _insert_lease(
                    session,
                    org_id=org_id,
                    task_id=task.id,
                    state_id=task.state_id,
                    holder=holder,
                    now=now,
                    expires_at=_deadline(now, ttl_seconds),
                    fence=await _next_fence(session, task.id),
                )
            await tasks_svc.set_state(
                session,
                org_id=org_id,
                actor_id=actor_id,
                task_id=task.id,
                expected_version=task.version,
                state_id=transition_to,
                # The worker that pulls is the one moving it: checked as such
                # on a close (``assert_may_close``) and recorded on the move.
                worker_id=holder.worker_id,
                _lease_checked=True,
            )
            await session.refresh(task)
            state_for_lease = transition_to

        lease = await _insert_lease(
            session,
            org_id=org_id,
            task_id=task.id,
            state_id=state_for_lease,
            holder=holder,
            now=now,
            expires_at=_deadline(now, ttl_seconds),
            fence=await _next_fence(session, task.id),
        )
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="task_lease",
            entity_id=lease.id,
            action="pull",
            diff={"task_id": str(task.id), "worker_id": str(holder.worker_id)},
        )
        return task, lease

    # Every candidate visible in this window was taken by somebody else
    # while we looked at it. "Empty" is the truthful answer, and the
    # bound is declared at _PULL_ATTEMPTS.
    raise NotFoundError(MessageCode.LEASE_QUEUE_EMPTY)


async def renew(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_id: uuid.UUID | None = None,
    ttl_seconds: int | None = None,
    fence: int | None = None,
) -> TaskLease:
    """Push the deadline out on a lease you hold.

    ``fence`` is optional and checked when given: a holder that was
    reclaimed, came back and is renewing on the strength of what it
    remembers gets :data:`MessageCode.LEASE_FENCE_STALE` instead of
    extending somebody else's possession.
    """
    await require_role(session, org_id, actor_id, Role.member)
    now = _now()
    holder = await resolve_holder(session, org_id=org_id, actor_id=actor_id, worker_id=worker_id)
    lease = await live_lease(session, task_id=task_id)
    if lease is None or not holder.holds(lease):
        raise ConflictError(MessageCode.LEASE_NOT_HELD)
    if fence is not None and int(fence) != int(lease.fence):
        raise ConflictError(MessageCode.LEASE_FENCE_STALE)
    # Expired but not yet swept is still ours on the index, and renewing it
    # is safe because nobody could have taken it. Swept meanwhile is not:
    # the conditional write says which of the two it found.
    if not await _extend(session, lease=lease, now=now, ttl_seconds=ttl_seconds):
        raise ConflictError(MessageCode.LEASE_NOT_HELD)
    return lease


async def release(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    reason: LeaseRelease,
    worker_id: uuid.UUID | None = None,
    require_holder: bool = True,
) -> TaskLease | None:
    """Hand a task back. Returns None when nothing was held.

    ``require_holder=False`` is the internal path used by the state
    transition hook, which has already established who may move the task
    and must not refuse the release afterwards: refusing there would
    leave a task in a new state with a lease naming the old one, which is
    worse than either outcome it was trying to protect.
    """
    now = _now()
    lease = await live_lease(session, task_id=task_id)
    if lease is None:
        return None
    if require_holder:
        holder = await resolve_holder(
            session, org_id=org_id, actor_id=actor_id, worker_id=worker_id
        )
        if not holder.holds(lease):
            raise ForbiddenError(MessageCode.LEASE_NOT_HELD)
    await session.flush()
    if lease.id not in await _end_leases(session, lease_ids=[lease.id], reason=reason, at=now):
        # Ended between the read and the write -- by the sweep, after its
        # deadline passed. Nothing was held any more, which is what this
        # function returns None for; the release that happened is logged
        # by whoever made it, with its own reason.
        await session.refresh(lease)
        return None
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_lease",
        entity_id=lease.id,
        action="release",
        diff={"task_id": str(task_id), "reason": reason.value},
    )
    return lease


async def release_all_for_worker(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    worker_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Give back everything one worker holds. Returns the task ids.

    The fast half of recovery. A session being shut down knows its work
    has stopped, so its tasks are free immediately instead of waiting out
    a deadline that exists for the case where nobody knows anything. The
    slow half is the sweep, for a session that died without saying so;
    the outcome is identical and neither needs a person.

    ``explicit`` rather than ``expired``, so the sweep's count stays a
    measure of sessions that died rather than of sessions that ended.
    """
    now = _now()
    rows = (
        await session.execute(
            select(TaskLease.id, TaskLease.task_id).where(
                TaskLease.org_id == org_id,
                TaskLease.holder_worker_id == worker_id,
                TaskLease.released_at.is_(None),
            )
        )
    ).all()
    if not rows:
        return []
    ended = await _end_leases(
        session, lease_ids=[r[0] for r in rows], reason=LeaseRelease.explicit, at=now
    )
    await session.flush()
    rows = [r for r in rows if r[0] in ended]
    for _lease_id, task_id in rows:
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="task_lease",
            entity_id=_lease_id,
            action="release",
            diff={"task_id": str(task_id), "reason": LeaseRelease.explicit.value},
        )
    return [r[1] for r in rows]


async def preempt(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
) -> TaskLease | None:
    """Owner: take a held task back, freeing it. Returns the lease that
    was ended, or None when nothing was held.

    Distinct from ``acquire(preempt=True)``, which takes the task FOR the
    caller, and the difference is what somebody actually wants. A person
    looking at a board and finding a task held by a session that is not
    coming back wants it AVAILABLE, not assigned to their browser tab --
    which would need the tab to hold a lease it will never release when
    the window closes, trading one stuck task for another.

    Owner-gated, like the other privileged task operations: interrupting
    a worker that may still be running is a decision, and the release
    reason records it as one so the sweep's numbers stay a count of
    sessions that died.
    """
    await require_role(session, org_id, actor_id, Role.owner)
    lease = await live_lease(session, task_id=task_id)
    if lease is None:
        return None
    return await release(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        reason=LeaseRelease.preempted,
        require_holder=False,
    )


async def assert_may_move(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_id: uuid.UUID | None = None,
) -> TaskLease | None:
    """Refuse a state transition by somebody who is not holding the task.

    An unheld task is movable by anyone, which keeps every path that
    predates possession working: the web UI, the scheduler, and a
    dispatched run. An agent credential, the CLI's included, is the
    exception below. Possession constrains only what it actually covers,
    and a lease nobody took covers nothing.

    **The owner exemption is narrower than "the owner", and the first
    version of it was useless.** Somebody has to be able to unstick a
    workspace without waiting out a deadline, and that somebody is the
    person whose workspace it is. But exempting `task.owner_id ==
    actor_id` exempts the AGENTS too: they authenticate as that same
    user, and in a workspace where one person owns every task the
    exemption swallowed the whole rule. So it is narrowed by the kind of
    credential (``services.caller``): the person's own session takes it, and
    so does the server acting for itself. An agent token over MCP or REST, a
    dispatched run and a Telegram turn do not, whoever they authenticate as,
    because each of them is a model deciding to take somebody else's work.
    """
    lease = await live_lease(session, task_id=task_id)
    if lease is None or not lease.is_live(_now()):
        # Nobody holds it, which used to end the question: a task nobody
        # holds was movable by anyone, and that is still true of every
        # caller that is not an agent credential.
        #
        # It is no longer true of one that is, and the measurement is why.
        # Possession was voluntary, so an agent that never took a task
        # moved it anyway and appeared nowhere: two holders across
        # twenty-five tasks in a working state on 2026-09-18, and the
        # twenty-three others were not refusals but sessions that had
        # never been told. Saying it in the instructions is the cheap
        # half; this is the half that cannot be skipped, because the
        # failure it prevents is SILENT -- two sessions doing the same
        # work, each believing the task free, neither ever learning
        # otherwise.
        #
        # Scoped by the discriminator this module already trusts for the
        # owner exemption: a credential, not a person. A human at the
        # board, a dispatched agent run and the scheduler all keep
        # moving tasks they never took, which is what ADR-0063 protected
        # and is still protected here.
        if get_settings().task_lease_required_for_agents:
            holder = await resolve_holder(
                session, org_id=org_id, actor_id=actor_id, worker_id=worker_id
            )
            if holder.token_id is not None:
                raise ConflictError(MessageCode.LEASE_REQUIRED)
        return lease
    holder = await resolve_holder(session, org_id=org_id, actor_id=actor_id, worker_id=worker_id)
    if holder.holds(lease):
        return lease
    if not holder.caller.is_model:
        owner_id = (
            await session.execute(select(Task.owner_id).where(Task.id == task_id))
        ).scalar_one_or_none()
        if owner_id is not None and owner_id == actor_id:
            return lease
    raise ConflictError(
        MessageCode.LEASE_HELD_BY_OTHER,
        holder=await held_by(session, lease),
        expires_at=lease.expires_at.isoformat(),
    )


async def assert_may_close(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    worker_id: uuid.UUID | None,
) -> dict[str, str | None] | None:
    """Refuse a model declaring work done that it did, or that nobody else
    handed over. Returns what the activity row records about the check, or
    None when the caller is not a model.

    Called by ``tasks.set_state`` when the move enters a ``closes_work``
    state. The Default workflow said it in prose -- a task enters ``done``
    after a check passed, or by the person's explicit decision, and an
    agent does not take that second decision alone -- and nothing applied
    it: an agent could move its own work to ``done``, and
    ``pull(transition_to=done)`` did it in one call.

    A person and the system are not asked anything here: the person's
    decision is the second way in, and the system acts on rules a person
    set. A model (an agent, or a Telegram turn, ``services.caller``) needs
    all three of:

    - a live lease on the task held by the worker it names. A check is done
      by a session that took the task; a Telegram turn has no worker, so it
      closes nothing until the person confirms through a channel the model
      does not write (task 04d8b905).
    - no handoff of this task by that worker, ever: whoever passed the work
      on for checking is its author, and may not be its checker.
    - the task was handed into the state it is in now by another worker
      (``tasks.handed_over_by_worker_id``): the check is of work somebody
      else passed on, in this round. A handoff from before the task was
      last reopened does not count; the work since then is unchecked. A
      person moving the task does not count, and that is a choice, not an
      oversight: a person moving a task into a working state and a person
      moving it into a checking one are the same write, and only the
      workflow's prose says which is which. Counting it let a worker take a
      task the person had queued, do it and close it.
      Work a person did and an agent checked is closed by the person.

    What it does not prove: the same model can open a second worker and
    check its own work. On the agent side this is hygiene, not evidence of
    independence.
    """
    caller = await caller_svc.current(session)
    if not caller.is_model:
        return None
    lease = await live_lease(session, task_id=task_id)
    if (
        worker_id is None
        or lease is None
        or not lease.is_live(_now())
        or lease.holder_worker_id != worker_id
    ):
        raise ConflictError(MessageCode.TASK_CLOSE_REQUIRES_CHECK, missing="lease")
    own_handoff = (
        await session.execute(
            select(TaskLease.id)
            .where(
                TaskLease.task_id == task_id,
                TaskLease.holder_worker_id == worker_id,
                TaskLease.release_reason == LeaseRelease.handoff.value,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if own_handoff is not None:
        raise ConflictError(MessageCode.LEASE_SELF_VERIFICATION)
    handed_over_by = (
        await session.execute(select(Task.handed_over_by_worker_id).where(Task.id == task_id))
    ).scalar_one_or_none()
    if handed_over_by == worker_id:
        raise ConflictError(MessageCode.LEASE_SELF_VERIFICATION)
    if handed_over_by is None:
        raise ConflictError(MessageCode.TASK_CLOSE_REQUIRES_CHECK, missing="handoff")
    return {
        "checked_by_worker": str(worker_id),
        "handed_over_by_worker": str(handed_over_by),
    }


async def release_on_transition(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    new_state_id: uuid.UUID,
    ends_work: bool,
    worker_id: uuid.UUID | None = None,
) -> LeaseRelease | None:
    """A lease is held for the duration of ONE state; leaving it ends the
    possession. Returns why it ended, or None when nothing was held.

    This is where "moved it on and kept working" stops being a matter of
    discipline. The rule is stated without naming a state, which is not
    tidiness: the workflow is configuration, a project can override it,
    and ``tasks.set_state`` already refuses to hardcode a state name for
    the same reason. Whatever the stations are called, holding one does
    not carry into the next.

    ``done`` and ``handoff`` are told apart because a reader asking what
    a worker handed off for checking must not be answered with work the
    worker finished. ``ends_work`` is a terminal state or one that declares
    the work done (``closes_work``), which a workflow may leave non-terminal:
    the checker that closes into it has handed nothing on.

    Both are what the HOLDER did, so they are recorded only when the holder
    is the one moving the task. A move by somebody else (the owner taking
    the task back, or anyone moving it off a lease whose deadline passed)
    ends the possession as ``preempted`` or ``expired``. Recording it as the
    holder's handoff put words in its mouth, and ``assert_may_close`` reads
    those words: the holder could never check that task again, and any
    other worker could close it as if the dead session had passed it on.
    """
    lease = await live_lease(session, task_id=task_id)
    if lease is None or lease.state_id == new_state_id:
        return None
    holder = await resolve_holder(session, org_id=org_id, actor_id=actor_id, worker_id=worker_id)
    if holder.holds(lease):
        reason = LeaseRelease.done if ends_work else LeaseRelease.handoff
    elif lease.is_live(_now()):
        reason = LeaseRelease.preempted
    else:
        reason = LeaseRelease.expired
    await release(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        reason=reason,
        require_holder=False,
    )
    return reason


async def labels_for(session: AsyncSession, leases: Sequence[TaskLease]) -> dict[uuid.UUID, str]:
    """Holder names for a page of leases, in two queries whatever the page.

    The projections need them and cannot each go and look: a list of
    twenty leases resolved one at a time is twenty round trips for a
    column, which is how a read that exists to be cheap stops being
    cheap. It said that and then did it anyway -- a call to ``held_by``
    per row -- which nobody noticed while the only caller was one task's
    history. A board asks for every live possession at once, so the
    figure went from twenty to as many as there are sessions, twice.

    Same resolution as ``held_by``, deliberately: a person must see one
    name for one holder wherever they meet it, in a refusal or in a list.
    """
    worker_ids = {x.holder_worker_id for x in leases if x.holder_worker_id is not None}
    user_ids = {x.holder_user_id for x in leases if x.holder_worker_id is None}
    workers: dict[uuid.UUID, str | None] = {}
    users: dict[uuid.UUID, str | None] = {}
    if worker_ids:
        workers = {
            wid: label
            for wid, label in (
                await session.execute(
                    select(AgentWorker.id, AgentWorker.label).where(AgentWorker.id.in_(worker_ids))
                )
            ).all()
        }
    if user_ids:
        users = {
            uid: handle
            for uid, handle in (
                await session.execute(select(User.id, User.handle).where(User.id.in_(user_ids)))
            ).all()
        }
    out: dict[uuid.UUID, str] = {}
    for x in leases:
        if x.holder_worker_id is not None:
            out[x.id] = workers.get(x.holder_worker_id) or str(x.holder_worker_id)
        else:
            out[x.id] = users.get(x.holder_user_id) or str(x.holder_user_id)
    return out


async def last_handoffs(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    limit: int = 500,
) -> Sequence[TaskLease]:
    """The most recent HANDOFF per task: who passed this one on.

    The board's other question, and it is not the same as who holds it.
    A card nobody holds is a card somebody may pick up, and the one fact
    that decides whether they should is who did the work before it
    arrived here -- the checking station's rule is that the check is done
    by somebody other than whoever did it.

    ``handoff`` and not every release: ``done`` is work finished and
    ``expired`` is a session that died, and neither answers "who passed
    this to me". That distinction is why the reason column exists.

    One row per task, chosen by the datastore (``DISTINCT ON``) rather
    than by reading the history and filtering it here: the history grows
    without bound and per task only its head is ever wanted.
    """
    stmt = (
        select(TaskLease)
        .where(
            TaskLease.org_id == org_id,
            TaskLease.released_at.is_not(None),
            TaskLease.release_reason == LeaseRelease.handoff.value,
        )
        .distinct(TaskLease.task_id)
        .order_by(TaskLease.task_id, TaskLease.released_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_leases(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    worker_id: uuid.UUID | None = None,
    include_released: bool = False,
    after: tuple[dt.datetime, uuid.UUID] | None = None,
    limit: int = 50,
) -> Sequence[TaskLease]:
    """Who holds what, and since when.

    Not optional furniture: a possession nobody can see is an invisible
    lock, which is worse than no lock, because the agent that cannot
    proceed also cannot say why.

    Two collections behind one signature, and only one of them is
    bounded. Live possessions are bounded by the sessions running, so a
    page covers them; with ``include_released`` this is the HISTORY, one
    row per acquisition for as long as the retention keeps it, and a
    limit alone would drop the oldest rows in silence. Hence ``after``:
    the keyset is ``(acquired_at, id)``, the order's own key, and both
    columns are NOT NULL so the predicate is exact -- no page repeats a
    row and none skips one.
    """
    stmt = select(TaskLease).where(TaskLease.org_id == org_id)
    if task_id is not None:
        stmt = stmt.where(TaskLease.task_id == task_id)
    if worker_id is not None:
        stmt = stmt.where(TaskLease.holder_worker_id == worker_id)
    if not include_released:
        stmt = stmt.where(TaskLease.released_at.is_(None))
    if after is not None:
        at, last_id = after
        stmt = stmt.where(
            or_(
                TaskLease.acquired_at < at,
                and_(TaskLease.acquired_at == at, TaskLease.id > last_id),
            )
        )
    stmt = stmt.order_by(TaskLease.acquired_at.desc(), TaskLease.id.asc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def sweep_expired(
    session: AsyncSession,
    *,
    limit: int = 200,
    now: dt.datetime | None = None,
) -> list[uuid.UUID]:
    """Reclaim the leases of sessions that died. Returns the task ids.

    The backstop that did not exist: nothing in the worker looked at
    tasks left in a working state by a session that never came back, so
    one crashed agent stranded its task permanently.

    It releases possession and does NOT move the task's state. Where the
    task should go back to is a workflow question with a workflow answer,
    and a sweep guessing it would write a transition nobody chose. An
    unheld task in a working state is visible, pullable by anybody, and
    honest about what happened; a task the sweep marched backwards is
    none of those.

    Every release it makes is written to the activity log, like every other
    release (ADR-0063): it is the one nobody asked for, so it is the one a
    person most needs to find afterwards. No actor, because nobody acted;
    the session's ``system`` kind says who did. The row names the worker
    that stopped answering and the deadline it missed.
    """
    at = now or _now()
    rows = (
        await session.execute(
            select(
                TaskLease.id,
                TaskLease.task_id,
                TaskLease.org_id,
                TaskLease.holder_worker_id,
                TaskLease.expires_at,
            )
            .where(TaskLease.released_at.is_(None), TaskLease.expires_at <= at)
            .order_by(TaskLease.expires_at.asc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return []
    released = await _end_leases(
        session,
        lease_ids=[r.id for r in rows],
        reason=LeaseRelease.expired,
        at=at,
        expired_by=at,
    )
    await session.flush()
    for r in rows:
        if r.id not in released:
            continue  # released or renewed by its holder between the read and the write
        await _log_expiry(
            session,
            org_id=r.org_id,
            lease_id=r.id,
            task_id=r.task_id,
            holder_worker_id=r.holder_worker_id,
            expires_at=r.expires_at,
        )
    return [r.task_id for r in rows if r.id in released]
