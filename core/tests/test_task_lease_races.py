"""Lease releases racing each other, each interleaving forced rather than hoped for.

Every test keeps a first transaction open until ``pg_stat_activity`` shows the
second one waiting on a lock on ``task_leases``, then commits the first. The
second path has therefore read the lease before the first one's write became
final, which is the window every race below lives in.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from typing import Any

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ConflictError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.task_lease import LeaseRelease, TaskLease
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import task_leases as leases_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="RACE",
        )
    return r.org_id, r.user_id


async def _worker(org: uuid.UUID, user: uuid.UUID, label: str) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        return (await workers_svc.open_worker(s, org_id=org, actor_id=user, label=label)).id


async def _task(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        return (await tasks_svc.create_task(s, org_id=org, actor_id=user, title="race")).id


async def _expired_lease(
    org: uuid.UUID, user: uuid.UUID, task_id: uuid.UUID, w: uuid.UUID
) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        lease = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=60
        )
        lease.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        return lease.id


async def _lease_log(org: uuid.UUID, user: uuid.UUID, lease_id: uuid.UUID) -> list[ActivityLog]:
    async with tenant_session(str(org), str(user)) as s:
        return list(
            (
                await s.execute(
                    select(ActivityLog).where(
                        ActivityLog.entity == "task_lease",
                        ActivityLog.entity_id == lease_id,
                        ActivityLog.action == "release",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _row(org: uuid.UUID, user: uuid.UUID, lease_id: uuid.UUID) -> TaskLease:
    async with tenant_session(str(org), str(user)) as s:
        row = await s.get(TaskLease, lease_id)
        assert row is not None
        return row


async def _live_rows(org: uuid.UUID, user: uuid.UUID, task_id: uuid.UUID) -> list[TaskLease]:
    async with tenant_session(str(org), str(user)) as s:
        return list(
            (
                await s.execute(
                    select(TaskLease).where(
                        TaskLease.task_id == task_id, TaskLease.released_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )


async def _a_lease_lock_is_awaited() -> bool:
    async with admin_session() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND query ILIKE 'UPDATE task_leases%'"
                )
            )
        ).scalar_one()
    return int(n) > 0


async def _interleave(
    org: uuid.UUID,
    user: uuid.UUID,
    first: Callable[[AsyncSession], Awaitable[Any]],
    second: Callable[[], Coroutine[Any, Any, Any]],
    *,
    first_kind: str = "human_direct",
) -> tuple[Any, Any]:
    """Run ``first`` in a transaction kept open until ``second`` waits on a
    lease row lock, then commit ``first`` and let ``second`` finish."""
    async with tenant_session(str(org), str(user), actor_kind=first_kind) as s:
        a = await first(s)
        pending = asyncio.create_task(second())
        for _ in range(200):
            if await _a_lease_lock_is_awaited():
                break
            await asyncio.sleep(0.02)
        else:
            pending.cancel()
            pytest.fail("the second path never waited on the first one's row lock")
    try:
        b = await pending
    except Exception as exc:
        b = exc
    return a, b


async def _sweep(s: AsyncSession) -> list[uuid.UUID]:
    return await leases_svc.sweep_expired(s)


async def _system_sweep(org: uuid.UUID, user: uuid.UUID) -> list[uuid.UUID]:
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        return await leases_svc.sweep_expired(s)


async def test_a_renewal_committed_while_the_sweep_waits_is_not_swept(_embedder: None) -> None:
    """The holder renews an expired-but-unswept lease (``renew`` says that is
    safe), and the sweep, which read the row as expired, waits on the
    renewal's row lock. When the renewal commits, the sweep's UPDATE re-reads
    the row: ``released_at IS NULL`` still holds, so without the deadline in
    the predicate it ends a lease whose holder was just told it was extended."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    lease_id = await _expired_lease(org, user, task_id, w)

    async def renew(s: AsyncSession) -> TaskLease:
        return await leases_svc.renew(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=600
        )

    renewed, swept = await _interleave(org, user, renew, lambda: _system_sweep(org, user))
    assert isinstance(renewed, TaskLease)
    row = await _row(org, user, lease_id)
    assert row.released_at is None, (
        f"renew succeeded, then the sweep released it: swept={swept} "
        f"reason={row.release_reason} log={len(await _lease_log(org, user, lease_id))}"
    )
    assert swept == []
    assert await _lease_log(org, user, lease_id) == []


async def test_a_renewal_after_the_sweep_does_not_extend_a_released_lease(
    _embedder: None,
) -> None:
    """The sweep releases the lease; the holder, which read it as unreleased,
    renews it by primary key after the sweep commits. The renewal must be
    refused: the caller does not hold the task any more."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    lease_id = await _expired_lease(org, user, task_id, w)

    async def renew() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.renew(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=600
            )

    _swept, renewed = await _interleave(org, user, _sweep, renew, first_kind="system")
    row = await _row(org, user, lease_id)
    assert isinstance(renewed, ConflictError), (
        f"renew returned {renewed!r} on a released lease: released_at={row.released_at} "
        f"expires_at={row.expires_at} live_rows={len(await _live_rows(org, user, task_id))}"
    )
    assert renewed.code == MessageCode.LEASE_NOT_HELD
    assert row.released_at is not None
    assert row.expires_at < dt.datetime.now(dt.UTC), "the released row was given a new deadline"


async def test_an_idempotent_acquire_after_the_sweep_takes_the_task_again(
    _embedder: None,
) -> None:
    """The same worker re-acquires its expired lease while the sweep releases
    it. ``acquire`` locks the task row, the sweep does not, so ``acquire`` reads
    the lease as its own, extends it by primary key after the sweep commits,
    and returns it. The caller must end up holding a live lease, logged."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    old_id = await _expired_lease(org, user, task_id, w)

    async def again() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=w
            )

    _swept, got = await _interleave(org, user, _sweep, again, first_kind="system")
    assert isinstance(got, TaskLease)
    live = await _live_rows(org, user, task_id)
    assert len(live) == 1, (
        f"acquire returned lease {got.id} as held, but the task has {len(live)} live leases; "
        f"returned row released_at={(await _row(org, user, got.id)).released_at}"
    )
    assert live[0].id == got.id
    # A new possession, not the old one revived: its own fence and its own row.
    assert got.id != old_id and got.fence > (await _row(org, user, old_id)).fence
    async with tenant_session(str(org), str(user)) as s:
        acquired = (
            (
                await s.execute(
                    select(ActivityLog).where(
                        ActivityLog.entity == "task_lease",
                        ActivityLog.entity_id == got.id,
                        ActivityLog.action == "acquire",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(acquired) == 1


async def test_a_reclaim_does_not_end_a_lease_its_holder_just_renewed(_embedder: None) -> None:
    """Another worker reclaims the expired lease while its holder renews it.
    If the renewal commits first, the reclaim must not end it as expired."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    other = await _worker(org, user, "other")
    lease_id = await _expired_lease(org, user, task_id, w)

    async def renew(s: AsyncSession) -> TaskLease:
        return await leases_svc.renew(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=600
        )

    async def reclaim() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=other
            )

    _renewed, taken = await _interleave(org, user, renew, reclaim)
    row = await _row(org, user, lease_id)
    assert row.released_at is None, (
        f"holder renewed, then the reclaim ended it: reason={row.release_reason}, "
        f"other got {taken!r}"
    )
    assert isinstance(taken, ConflictError) and taken.code == MessageCode.LEASE_HELD_BY_OTHER
    assert await _lease_log(org, user, lease_id) == []


async def test_a_pull_does_not_end_a_lease_its_holder_just_renewed(_embedder: None) -> None:
    """The same race through ``pull``: the candidate was expired when chosen,
    renewed before the reclaim wrote. The pull treats it as a lost candidate
    and finds nothing else to take."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    other = await _worker(org, user, "other")
    lease_id = await _expired_lease(org, user, task_id, w)
    async with tenant_session(str(org), str(user)) as s:
        state_id = (await tasks_svc.get_task(s, org_id=org, task_id=task_id)).state_id

    async def renew(s: AsyncSession) -> TaskLease:
        return await leases_svc.renew(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=600
        )

    async def pull() -> Any:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.pull(
                s, org_id=org, actor_id=user, state_id=state_id, worker_id=other
            )

    _renewed, pulled = await _interleave(org, user, renew, pull)
    row = await _row(org, user, lease_id)
    assert row.released_at is None, f"pull ended a renewed lease: {pulled!r}"
    assert isinstance(pulled, NotFoundError) and pulled.code == MessageCode.LEASE_QUEUE_EMPTY
    assert await _lease_log(org, user, lease_id) == []


async def test_a_worker_closing_while_another_reclaims_logs_once(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "dying")
    other = await _worker(org, user, "other")
    lease_id = await _expired_lease(org, user, task_id, w)

    async def reclaim(s: AsyncSession) -> TaskLease:
        return await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=other
        )

    async def close() -> list[uuid.UUID]:
        async with tenant_session(str(org), str(user)) as s:
            return (await workers_svc.close_worker(s, org_id=org, actor_id=user, worker_id=w))[1]

    _taken, freed = await _interleave(org, user, reclaim, close)
    assert freed == []
    rows = await _lease_log(org, user, lease_id)
    assert len(rows) == 1 and rows[0].diff is not None
    assert rows[0].diff["reason"] == LeaseRelease.expired.value
    assert len(await _live_rows(org, user, task_id)) == 1


async def test_a_reclaim_while_the_worker_closes_logs_once(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "dying")
    other = await _worker(org, user, "other")
    lease_id = await _expired_lease(org, user, task_id, w)

    async def close(s: AsyncSession) -> list[uuid.UUID]:
        return (await workers_svc.close_worker(s, org_id=org, actor_id=user, worker_id=w))[1]

    async def reclaim() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=other
            )

    freed, taken = await _interleave(org, user, close, reclaim)
    assert freed == [task_id]
    assert isinstance(taken, TaskLease) and taken.holder_worker_id == other
    rows = await _lease_log(org, user, lease_id)
    assert len(rows) == 1 and rows[0].diff is not None
    assert rows[0].diff["reason"] == LeaseRelease.explicit.value


async def test_a_preempt_racing_the_sweep_logs_once(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    lease_id = await _expired_lease(org, user, task_id, w)

    async def preempt() -> TaskLease | None:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.preempt(s, org_id=org, actor_id=user, task_id=task_id)

    _swept, got = await _interleave(org, user, _sweep, preempt, first_kind="system")
    assert got is None
    assert len(await _lease_log(org, user, lease_id)) == 1


async def test_the_sweep_waiting_on_a_preempt_logs_once(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    lease_id = await _expired_lease(org, user, task_id, w)

    async def preempt(s: AsyncSession) -> TaskLease | None:
        return await leases_svc.preempt(s, org_id=org, actor_id=user, task_id=task_id)

    got, swept = await _interleave(org, user, preempt, lambda: _system_sweep(org, user))
    assert isinstance(got, TaskLease)
    assert swept == []
    rows = await _lease_log(org, user, lease_id)
    assert len(rows) == 1 and rows[0].diff is not None
    assert rows[0].diff["reason"] == LeaseRelease.preempted.value


async def test_a_state_change_racing_the_sweep_logs_once(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        wf_states = await s.execute(
            text(
                "SELECT t.to_state_id FROM workflow_transitions t "
                "JOIN workflow_states s ON s.id = t.from_state_id "
                "WHERE t.from_state_id = :sid LIMIT 1"
            ),
            {"sid": task.state_id},
        )
        target = wf_states.scalar_one()
        version = task.version
    lease_id = await _expired_lease(org, user, task_id, w)

    async def move() -> object:
        async with tenant_session(str(org), str(user)) as s:
            return await tasks_svc.set_state(
                s,
                org_id=org,
                actor_id=user,
                task_id=task_id,
                expected_version=version,
                state_id=target,
            )

    _swept, moved = await _interleave(org, user, _sweep, move, first_kind="system")
    assert not isinstance(moved, Exception), repr(moved)
    rows = await _lease_log(org, user, lease_id)
    assert len(rows) == 1 and rows[0].diff is not None
    assert rows[0].diff["reason"] == LeaseRelease.expired.value


async def test_the_sweep_waiting_on_a_state_change_logs_once(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        target = (
            await s.execute(
                text(
                    "SELECT t.to_state_id FROM workflow_transitions t "
                    "WHERE t.from_state_id = :sid LIMIT 1"
                ),
                {"sid": task.state_id},
            )
        ).scalar_one()
        version = task.version
    lease_id = await _expired_lease(org, user, task_id, w)

    async def move(s: AsyncSession) -> object:
        return await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=version,
            state_id=target,
        )

    _moved, swept = await _interleave(org, user, move, lambda: _system_sweep(org, user))
    assert swept == []
    rows = await _lease_log(org, user, lease_id)
    assert len(rows) == 1 and rows[0].diff is not None
    assert rows[0].diff["reason"] == LeaseRelease.handoff.value
