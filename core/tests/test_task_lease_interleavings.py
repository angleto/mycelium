"""Interleavings of lease paths not covered by test_task_lease_races.py:
closing a worker against the same worker taking or keeping work, a state
change against an idempotent acquire, two renewals, a reclaim against the
holder's re-acquire, and a pull against the sweep.

Written by the fourth and fifth independent checks of task 9d4088e9.
Against the close that released before writing ``closed_at``, the deadlock
case and the close waiting on an acquire fail; the cases where the close
commits first already passed, because ``touch`` refuses a closed worker.

Same schema: a first transaction stays open until ``pg_stat_activity`` shows
the second one waiting on a lock, then commits. Where the window lives inside
one service call, a gate on a module function holds the first call at that
point instead.
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
from mycelium_core.models.agent_worker import AgentWorker
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
            org_name="RACE-V",
        )
    return r.org_id, r.user_id


async def _worker(org: uuid.UUID, user: uuid.UUID, label: str) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        return (await workers_svc.open_worker(s, org_id=org, actor_id=user, label=label)).id


async def _task(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        return (await tasks_svc.create_task(s, org_id=org, actor_id=user, title="race")).id


async def _lease(
    org: uuid.UUID, user: uuid.UUID, task_id: uuid.UUID, w: uuid.UUID, *, expired: bool
) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        lease = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=600
        )
        if expired:
            lease.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        return lease.id


async def _log(org: uuid.UUID, user: uuid.UUID, lease_id: uuid.UUID, action: str) -> list[Any]:
    async with tenant_session(str(org), str(user)) as s:
        return list(
            (
                await s.execute(
                    select(ActivityLog).where(
                        ActivityLog.entity == "task_lease",
                        ActivityLog.entity_id == lease_id,
                        ActivityLog.action == action,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _live(org: uuid.UUID, user: uuid.UUID, task_id: uuid.UUID) -> list[TaskLease]:
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


async def _row(org: uuid.UUID, user: uuid.UUID, lease_id: uuid.UUID) -> TaskLease:
    async with tenant_session(str(org), str(user)) as s:
        row = await s.get(TaskLease, lease_id)
        assert row is not None
        return row


async def _pid(s: AsyncSession) -> int:
    return int((await s.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _awaited(prefix: str, blocker: int) -> bool:
    """A statement starting with ``prefix`` is waiting on a lock that
    ``blocker`` holds. Naming the blocker is what keeps another test, or a
    suite running on the same database, from satisfying the probe."""
    async with admin_session() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND query ILIKE :q "
                    "AND :blocker = ANY(pg_blocking_pids(pid))"
                ),
                {"q": prefix + "%", "blocker": blocker},
            )
        ).scalar_one()
    return int(n) > 0


async def _until_awaited(prefix: str, blocker: int) -> None:
    for _ in range(250):
        if await _awaited(prefix, blocker):
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"nothing waited on a lock held by {blocker} for {prefix!r}")


async def _interleave(
    org: uuid.UUID,
    user: uuid.UUID,
    first: Callable[[AsyncSession], Awaitable[Any]],
    second: Callable[[], Coroutine[Any, Any, Any]],
    waits_on: str,
    *,
    first_kind: str = "human_direct",
) -> tuple[Any, Any]:
    async with tenant_session(str(org), str(user), actor_kind=first_kind) as s:
        a = await first(s)
        blocker = await _pid(s)
        pending = asyncio.create_task(second())
        try:
            await _until_awaited(waits_on, blocker)
        except BaseException:
            pending.cancel()
            raise
    try:
        b = await pending
    except Exception as exc:
        b = exc
    return a, b


async def _worker_closed(org: uuid.UUID, user: uuid.UUID, w: uuid.UUID) -> bool:
    async with tenant_session(str(org), str(user)) as s:
        row = await s.get(AgentWorker, w)
        assert row is not None
        return row.closed_at is not None


# --- close_worker against the same worker taking or keeping work --------------


async def test_an_acquire_waiting_on_a_close_does_not_leave_a_closed_worker_holding(
    _embedder: None,
) -> None:
    """The worker is being closed; its own acquire on another task read the
    worker as open and waits on the worker row the close is updating. After
    the close commits, no live lease may name the closed worker: it can no
    longer renew or release it, and the close told its caller everything was
    given back."""
    org, user = await _org()
    held = await _task(org, user)
    fresh = await _task(org, user)
    w = await _worker(org, user, "closing")
    await _lease(org, user, held, w, expired=False)

    async def close(s: AsyncSession) -> list[uuid.UUID]:
        return (await workers_svc.close_worker(s, org_id=org, actor_id=user, worker_id=w))[1]

    async def acquire() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=fresh, worker_id=w
            )

    freed, got = await _interleave(org, user, close, acquire, "UPDATE agent_workers")
    assert freed == [held]
    assert await _worker_closed(org, user, w)
    orphans = [x for x in await _live(org, user, fresh) if x.holder_worker_id == w]
    if orphans:
        async with tenant_session(str(org), str(user)) as s:
            with pytest.raises(NotFoundError):
                await leases_svc.renew(s, org_id=org, actor_id=user, task_id=fresh, worker_id=w)
        async with tenant_session(str(org), str(user)) as s:
            with pytest.raises(NotFoundError):
                await leases_svc.release(
                    s,
                    org_id=org,
                    actor_id=user,
                    task_id=fresh,
                    reason=LeaseRelease.explicit,
                    worker_id=w,
                )
    assert not orphans, (
        f"acquire returned {type(got).__name__}; the closed worker {w} holds a live lease "
        f"on {fresh} that it can neither renew nor release"
    )


async def test_an_idempotent_acquire_waiting_on_a_close_is_not_told_it_holds(
    _embedder: None,
) -> None:
    """Same race, on the task the worker already holds: the close ended that
    lease; the re-acquire must not hand the closed worker a new one."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "closing")
    await _lease(org, user, task_id, w, expired=False)

    async def close(s: AsyncSession) -> list[uuid.UUID]:
        return (await workers_svc.close_worker(s, org_id=org, actor_id=user, worker_id=w))[1]

    async def again() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=w
            )

    freed, got = await _interleave(org, user, close, again, "UPDATE agent_workers")
    assert freed == [task_id]
    live = await _live(org, user, task_id)
    assert not [x for x in live if x.holder_worker_id == w], (
        f"re-acquire after close returned {type(got).__name__}; live leases of the closed "
        f"worker: {[x.id for x in live]}"
    )


async def test_a_pull_waiting_on_a_close_does_not_leave_a_closed_worker_holding(
    _embedder: None,
) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "closing")
    async with tenant_session(str(org), str(user)) as s:
        state_id = (await tasks_svc.get_task(s, org_id=org, task_id=task_id)).state_id

    async def close(s: AsyncSession) -> list[uuid.UUID]:
        return (await workers_svc.close_worker(s, org_id=org, actor_id=user, worker_id=w))[1]

    async def pull() -> Any:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.pull(
                s, org_id=org, actor_id=user, state_id=state_id, worker_id=w
            )

    _freed, got = await _interleave(org, user, close, pull, "UPDATE agent_workers")
    assert not [x for x in await _live(org, user, task_id) if x.holder_worker_id == w], (
        f"pull returned {type(got).__name__} to a worker closed meanwhile"
    )


async def test_a_renewal_waiting_on_a_close_is_refused(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "closing")
    lease_id = await _lease(org, user, task_id, w, expired=False)

    async def close(s: AsyncSession) -> list[uuid.UUID]:
        return (await workers_svc.close_worker(s, org_id=org, actor_id=user, worker_id=w))[1]

    async def renew() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.renew(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=600
            )

    freed, got = await _interleave(org, user, close, renew, "UPDATE agent_workers")
    assert freed == [task_id]
    assert isinstance(got, (ConflictError, NotFoundError)), repr(got)
    assert (await _row(org, user, lease_id)).released_at is not None
    assert len(await _log(org, user, lease_id, "release")) == 1


async def test_a_close_waiting_on_a_renewal_releases_once(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "closing")
    lease_id = await _lease(org, user, task_id, w, expired=False)

    async def renew(s: AsyncSession) -> TaskLease:
        return await leases_svc.renew(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=600
        )

    async def close() -> list[uuid.UUID]:
        async with tenant_session(str(org), str(user)) as s:
            return (await workers_svc.close_worker(s, org_id=org, actor_id=user, worker_id=w))[1]

    _renewed, freed = await _interleave(org, user, renew, close, "UPDATE agent_workers")
    assert freed == [task_id], repr(freed)
    rows = await _log(org, user, lease_id, "release")
    assert len(rows) == 1 and rows[0].diff["reason"] == LeaseRelease.explicit.value


async def test_a_close_waiting_on_an_acquire_gives_the_new_lease_back(
    _embedder: None,
) -> None:
    """The acquire stamps the worker row first and holds it; the close waits.
    The release that follows the close must read after the acquire's commit
    and give that lease back. The old close released first, on a snapshot
    without the new lease, and left it live on a closed worker."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "closing")

    async def acquire(s: AsyncSession) -> TaskLease:
        return await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=w)

    async def close() -> tuple[AgentWorker, list[uuid.UUID]]:
        async with tenant_session(str(org), str(user)) as s:
            return await workers_svc.close_worker(s, org_id=org, actor_id=user, worker_id=w)

    lease, closed = await _interleave(
        org, user, acquire, close, "UPDATE agent_workers SET closed_at"
    )
    assert not isinstance(closed, BaseException), repr(closed)
    assert closed[1] == [task_id]
    assert await _live(org, user, task_id) == []
    rel = await _log(org, user, lease.id, "release")
    assert len(rel) == 1 and rel[0].diff["reason"] == LeaseRelease.explicit.value


async def test_a_renewal_between_its_touch_and_its_write_does_not_deadlock_a_close(
    _embedder: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``renew`` stamps the worker row (``touch``) before it writes the lease.
    ``close_worker`` used to write the lease before the worker row, and a
    renewal held between its two writes while the close ran made the two
    wait on each other: a database deadlock, 3 runs out of 3. The close now
    takes the worker row first, the same order as every other path."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "closing")
    lease_id = await _lease(org, user, task_id, w, expired=False)

    reached = asyncio.Event()
    go = asyncio.Event()
    real_extend = leases_svc._extend

    renewer: list[int] = []

    async def gated_extend(session: AsyncSession, **kw: Any) -> bool:
        renewer.append(await _pid(session))
        reached.set()
        await go.wait()
        return await real_extend(session, **kw)

    monkeypatch.setattr(leases_svc, "_extend", gated_extend)

    async def renew() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.renew(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=600
            )

    async def close() -> list[uuid.UUID]:
        async with tenant_session(str(org), str(user)) as s:
            return (await workers_svc.close_worker(s, org_id=org, actor_id=user, worker_id=w))[1]

    r = asyncio.create_task(renew())
    await asyncio.wait_for(reached.wait(), 10)
    c = asyncio.create_task(close())
    await _until_awaited("UPDATE agent_workers", renewer[0])
    go.set()
    results = await asyncio.gather(r, c, return_exceptions=True)
    errors = [
        f"{side}: {x!r}"
        for side, x in zip(("renew", "close"), results, strict=True)
        if isinstance(x, BaseException)
    ]
    assert not [e for e in errors if "deadlock" in e.lower()], errors
    assert len(await _log(org, user, lease_id, "release")) <= 1


# --- set_task_state against an idempotent acquire ------------------------------


async def _target_state(org: uuid.UUID, user: uuid.UUID, task_id: uuid.UUID) -> tuple[Any, int]:
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
        return target, task.version


async def test_an_idempotent_acquire_waiting_on_a_state_change_takes_a_new_possession(
    _embedder: None,
) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    target, version = await _target_state(org, user, task_id)
    old = await _lease(org, user, task_id, w, expired=False)

    async def move(s: AsyncSession) -> object:
        return await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=version,
            state_id=target,
            worker_id=w,
        )

    async def again() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=w
            )

    _moved, got = await _interleave(org, user, move, again, "UPDATE agent_workers")
    assert isinstance(got, TaskLease), repr(got)
    assert got.id != old
    live = await _live(org, user, task_id)
    assert [x.id for x in live] == [got.id] and live[0].state_id == target
    rel = await _log(org, user, old, "release")
    assert len(rel) == 1 and rel[0].diff["reason"] == LeaseRelease.handoff.value
    assert len(await _log(org, user, got.id, "acquire")) == 1


async def test_a_state_change_waiting_on_an_idempotent_acquire_releases_once(
    _embedder: None,
) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    target, version = await _target_state(org, user, task_id)
    lease_id = await _lease(org, user, task_id, w, expired=True)

    async def again(s: AsyncSession) -> TaskLease:
        return await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=w)

    async def move() -> object:
        async with tenant_session(str(org), str(user)) as s:
            return await tasks_svc.set_state(
                s,
                org_id=org,
                actor_id=user,
                task_id=task_id,
                expected_version=version,
                state_id=target,
                worker_id=w,
            )

    got, moved = await _interleave(org, user, again, move, "UPDATE agent_workers")
    assert got.id == lease_id
    assert not isinstance(moved, Exception), repr(moved)
    assert await _live(org, user, task_id) == []
    rel = await _log(org, user, lease_id, "release")
    assert len(rel) == 1 and rel[0].diff["reason"] == LeaseRelease.handoff.value


# --- two renewals --------------------------------------------------------------


async def test_two_renewals_at_once_both_hold_a_live_lease(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    lease_id = await _lease(org, user, task_id, w, expired=True)

    async def renew_s(s: AsyncSession) -> TaskLease:
        return await leases_svc.renew(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=600
        )

    async def renew() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.renew(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=900
            )

    a, b = await _interleave(org, user, renew_s, renew, "UPDATE")
    assert isinstance(a, TaskLease) and isinstance(b, TaskLease), (a, b)
    row = await _row(org, user, lease_id)
    assert row.released_at is None and row.expires_at > dt.datetime.now(dt.UTC)
    assert await _log(org, user, lease_id, "release") == []


# --- an idempotent acquire against another worker's reclaim --------------------


async def test_a_reclaim_waiting_on_the_holders_reacquire_is_refused(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    other = await _worker(org, user, "other")
    lease_id = await _lease(org, user, task_id, w, expired=True)

    async def again(s: AsyncSession) -> TaskLease:
        return await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=w)

    async def reclaim() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=other
            )

    got, taken = await _interleave(org, user, again, reclaim, "SELECT tasks")
    assert got.id == lease_id
    assert isinstance(taken, ConflictError) and taken.code == MessageCode.LEASE_HELD_BY_OTHER
    assert (await _row(org, user, lease_id)).released_at is None
    assert await _log(org, user, lease_id, "release") == []


async def test_the_holders_reacquire_waiting_on_a_reclaim_is_refused(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "holder")
    other = await _worker(org, user, "other")
    lease_id = await _lease(org, user, task_id, w, expired=True)

    async def reclaim(s: AsyncSession) -> TaskLease:
        return await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=other
        )

    async def again() -> TaskLease:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=w
            )

    taken, got = await _interleave(org, user, reclaim, again, "SELECT tasks")
    assert isinstance(got, ConflictError) and got.code == MessageCode.LEASE_HELD_BY_OTHER
    rel = await _log(org, user, lease_id, "release")
    assert len(rel) == 1 and rel[0].diff["reason"] == LeaseRelease.expired.value
    assert [x.id for x in await _live(org, user, task_id)] == [taken.id]


# --- pull against the sweep ----------------------------------------------------


async def test_a_pull_waiting_on_the_sweep_takes_the_task_and_logs_once(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "dead")
    other = await _worker(org, user, "puller")
    lease_id = await _lease(org, user, task_id, w, expired=True)
    async with tenant_session(str(org), str(user)) as s:
        state_id = (await tasks_svc.get_task(s, org_id=org, task_id=task_id)).state_id

    async def sweep(s: AsyncSession) -> list[uuid.UUID]:
        return await leases_svc.sweep_expired(s)

    async def pull() -> Any:
        async with tenant_session(str(org), str(user)) as s:
            return await leases_svc.pull(
                s, org_id=org, actor_id=user, state_id=state_id, worker_id=other
            )

    swept, got = await _interleave(
        org, user, sweep, pull, "UPDATE task_leases", first_kind="system"
    )
    assert task_id in swept
    assert isinstance(got, tuple), repr(got)
    assert len(await _log(org, user, lease_id, "release")) == 1
    assert [x.id for x in await _live(org, user, task_id)] == [got[1].id]
    assert len(await _log(org, user, got[1].id, "pull")) == 1


async def test_the_sweep_waiting_on_a_pull_logs_once(_embedder: None) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "dead")
    other = await _worker(org, user, "puller")
    lease_id = await _lease(org, user, task_id, w, expired=True)
    async with tenant_session(str(org), str(user)) as s:
        state_id = (await tasks_svc.get_task(s, org_id=org, task_id=task_id)).state_id

    async def pull(s: AsyncSession) -> Any:
        return await leases_svc.pull(
            s, org_id=org, actor_id=user, state_id=state_id, worker_id=other
        )

    async def sweep() -> list[uuid.UUID]:
        async with tenant_session(str(org), str(user), actor_kind="system") as s:
            return await leases_svc.sweep_expired(s)

    got, swept = await _interleave(org, user, pull, sweep, "UPDATE task_leases")
    assert task_id not in swept
    assert len(await _log(org, user, lease_id, "release")) == 1
    assert [x.id for x in await _live(org, user, task_id)] == [got[1].id]
