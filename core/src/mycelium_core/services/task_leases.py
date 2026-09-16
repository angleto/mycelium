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

from sqlalchemy import Select, and_, exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.errors import ConflictError, ForbiddenError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_token import AgentToken
from mycelium_core.models.identity import Identity
from mycelium_core.models.membership import Role
from mycelium_core.models.task import Task
from mycelium_core.models.task_lease import LeaseRelease, TaskLease
from mycelium_core.services import audit
from mycelium_core.services.rbac import require_role

# How many times ``pull`` re-picks after losing a candidate. It is small
# and it is stated because SCL-07 asks a bound to say what happens when
# it is reached: on exhaustion the queue reports empty, which is the
# truthful answer -- every candidate it could see was taken by somebody
# while it looked. The row lock makes losing rare; the bound exists for
# the case where it is not.
_PULL_ATTEMPTS = 4


class Holder:
    """Who is asking, at every granularity the system has.

    Assembled once per call rather than threaded through signatures,
    because three of the four slots travel on the session (the GUCs the
    audit log already reads) and only the worker id comes from the
    caller.
    """

    __slots__ = ("identity_id", "token_id", "user_id", "worker_id")

    def __init__(
        self,
        *,
        worker_id: str,
        user_id: uuid.UUID,
        identity_id: uuid.UUID | None,
        token_id: uuid.UUID | None,
    ) -> None:
        self.worker_id = worker_id
        self.user_id = user_id
        self.identity_id = identity_id
        self.token_id = token_id

    def holds(self, lease: TaskLease) -> bool:
        """Is this lease mine?

        The worker id alone answers it, and deliberately so. Adding "or
        the same identity" would make every session in this workspace the
        holder of every other session's lease, since they share one
        identity -- which is the exact failure the worker id exists to
        avoid. A session that loses its worker id has lost its lease and
        must take it again; that is a lease working, not a lease failing.
        """
        return lease.holder_worker_id == self.worker_id


async def resolve_holder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    worker_id: str | None,
) -> Holder:
    """Fill the four holder slots from the session and the caller.

    ``worker_id`` omitted is not an error and is not a shared bucket: it
    falls back to a label derived from the credential, which is the
    truthful thing to say about a caller that did not name itself (a
    human on the web UI is one session; two agents on one token that both
    omit it ARE indistinguishable, and the fallback makes them collide
    loudly on the first acquire rather than quietly forever).
    """
    row = (
        await session.execute(
            text(
                "SELECT current_setting('app.current_actor_kind', true),"
                "       current_setting('app.current_actor_subject', true)"
            )
        )
    ).one()
    actor_kind = row[0] or "human_direct"
    subject_raw = row[1] or ""
    token_id: uuid.UUID | None = None
    if actor_kind == "mcp_token" and subject_raw:
        try:
            token_id = uuid.UUID(subject_raw)
        except ValueError:
            token_id = None

    identity_id: uuid.UUID | None = None
    if token_id is not None:
        # token -> assistant -> identity, the chain ``whoami`` walks. A
        # bare/legacy token has no assistant row and yields nothing,
        # which is recorded as nothing rather than guessed at.
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

    if not worker_id:
        worker_id = f"token:{token_id}" if token_id is not None else f"user:{actor_id}"
    return Holder(
        worker_id=worker_id[:128],
        user_id=actor_id,
        identity_id=identity_id,
        token_id=token_id,
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _deadline(now: dt.datetime, ttl_seconds: int | None) -> dt.datetime:
    s = get_settings()
    ttl = ttl_seconds if ttl_seconds is not None else s.task_lease_ttl_seconds
    ttl = max(s.task_lease_min_ttl_seconds, min(int(ttl), s.task_lease_max_ttl_seconds))
    return now + dt.timedelta(seconds=ttl)


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


async def _reclaim_expired(session: AsyncSession, *, lease: TaskLease, now: dt.datetime) -> None:
    """Release an expired row so the index is free for the next holder.

    Called with the task row already locked, so no second caller can be
    reclaiming the same row concurrently.
    """
    lease.released_at = now
    lease.release_reason = LeaseRelease.expired.value
    lease.version += 1
    await session.flush()


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
    worker_id: str | None = None,
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
            existing.expires_at = _deadline(now, ttl_seconds)
            existing.renewed_at = now
            existing.version += 1
            await session.flush()
            return existing
        if existing.is_live(now):
            if not preempt:
                raise ConflictError(
                    MessageCode.LEASE_HELD_BY_OTHER,
                    expires_at=existing.expires_at.isoformat(),
                )
            await require_role(session, org_id, actor_id, Role.owner)
            existing.released_at = now
            existing.release_reason = LeaseRelease.preempted.value
            existing.version += 1
            await session.flush()
        else:
            await _reclaim_expired(session, lease=existing, now=now)

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
        diff={"task_id": str(task_id), "worker_id": holder.worker_id},
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

    The workflow's own description of the verification station says the
    check is done by somebody other than whoever did the work. This is
    that sentence as a predicate. It keys on the worker id and on
    ``handoff`` specifically: a task the worker merely finished
    (``done``) or gave up (``explicit``) is not excluded, because neither
    of those is the author presenting their own work for checking.
    """
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
    worker_id: str | None = None,
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
            await _reclaim_expired(session, lease=stale, now=now)

        task = await _lock_task(session, candidate_id)
        if task is None:
            continue

        state_for_lease = state_id
        if transition_to is not None and transition_to != task.state_id:
            from mycelium_core.services import tasks as tasks_svc

            await tasks_svc.set_state(
                session,
                org_id=org_id,
                actor_id=actor_id,
                task_id=task.id,
                expected_version=task.version,
                state_id=transition_to,
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
            diff={"task_id": str(task.id), "worker_id": holder.worker_id},
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
    worker_id: str | None = None,
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
    if not lease.is_live(now):
        # Expired but not yet swept, and still ours on the index. Renewing
        # it is safe precisely because nobody could have taken it: the
        # index was occupied the whole time.
        pass
    lease.expires_at = _deadline(now, ttl_seconds)
    lease.renewed_at = now
    lease.version += 1
    await session.flush()
    return lease


async def release(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    reason: LeaseRelease,
    worker_id: str | None = None,
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
    lease.released_at = now
    lease.release_reason = reason.value
    lease.version += 1
    await session.flush()
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


async def assert_may_move(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_id: str | None = None,
) -> TaskLease | None:
    """Refuse a state transition by somebody who is not holding the task.

    An unheld task is movable by anyone, which keeps every path that
    predates possession working: the web UI, the CLI, the scheduler, and
    an agent that never took a lease. Possession constrains only what it
    actually covers, and a lease nobody took covers nothing.

    The owner may always move a task. Somebody has to be able to unstick
    a workspace without waiting out a deadline, and that somebody is the
    person whose workspace it is.
    """
    lease = await live_lease(session, task_id=task_id)
    if lease is None or not lease.is_live(_now()):
        return lease
    holder = await resolve_holder(session, org_id=org_id, actor_id=actor_id, worker_id=worker_id)
    if holder.holds(lease):
        return lease
    task = (
        await session.execute(select(Task.owner_id).where(Task.id == task_id))
    ).scalar_one_or_none()
    if task is not None and task == actor_id:
        return lease
    raise ConflictError(MessageCode.LEASE_HELD_BY_OTHER, expires_at=lease.expires_at.isoformat())


async def release_on_transition(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    new_state_id: uuid.UUID,
    now_terminal: bool,
) -> None:
    """A lease is held for the duration of ONE state; leaving it ends the
    possession.

    This is where "moved it on and kept working" stops being a matter of
    discipline. The rule is stated without naming a state, which is not
    tidiness: the workflow is configuration, a project can override it,
    and ``tasks.set_state`` already refuses to hardcode a state name for
    the same reason. Whatever the stations are called, holding one does
    not carry into the next.

    ``done`` and ``handoff`` are told apart because a reader asking what
    a worker handed off for checking must not be answered with work the
    worker finished.
    """
    lease = await live_lease(session, task_id=task_id)
    if lease is None or lease.state_id == new_state_id:
        return
    await release(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        reason=LeaseRelease.done if now_terminal else LeaseRelease.handoff,
        require_holder=False,
    )


async def list_leases(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    worker_id: str | None = None,
    include_released: bool = False,
    limit: int = 50,
) -> Sequence[TaskLease]:
    """Who holds what, and since when.

    Not optional furniture: a possession nobody can see is an invisible
    lock, which is worse than no lock, because the agent that cannot
    proceed also cannot say why.
    """
    stmt = select(TaskLease).where(TaskLease.org_id == org_id)
    if task_id is not None:
        stmt = stmt.where(TaskLease.task_id == task_id)
    if worker_id is not None:
        stmt = stmt.where(TaskLease.holder_worker_id == worker_id)
    if not include_released:
        stmt = stmt.where(TaskLease.released_at.is_(None))
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
    """
    at = now or _now()
    rows = (
        await session.execute(
            select(TaskLease.id, TaskLease.task_id)
            .where(TaskLease.released_at.is_(None), TaskLease.expires_at <= at)
            .order_by(TaskLease.expires_at.asc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    await session.execute(
        update(TaskLease)
        .where(TaskLease.id.in_(ids), TaskLease.released_at.is_(None))
        .values(
            released_at=at,
            release_reason=LeaseRelease.expired.value,
            version=TaskLease.version + 1,
        )
    )
    await session.flush()
    return [r[1] for r in rows]
