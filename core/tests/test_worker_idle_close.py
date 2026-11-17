"""A working session that ended without saying so is closed by the sweep.

Only ``worker_close`` used to close a worker, and a session that is killed
calls nothing: production had 157 open workers out of 167 on 2026-11-17,
one of them doing anything. These tests pin the two conditions the close
needs (unseen for the threshold, holding no unreleased lease), the one thing
it must never do (release work), and the race with a session taking work at
the same moment, in both orders, each forced rather than hoped for.
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
from mycelium_core.errors import NotFoundError
from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.agent_worker import AgentWorker
from mycelium_core.models.task_lease import TaskLease
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import task_leases as leases_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup

IDLE = dt.timedelta(hours=6)


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
            org_name="IDLE",
        )
    return r.org_id, r.user_id


async def _worker(org: uuid.UUID, user: uuid.UUID, label: str) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        return (await workers_svc.open_worker(s, org_id=org, actor_id=user, label=label)).id


async def _task(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        return (await tasks_svc.create_task(s, org_id=org, actor_id=user, title="idle")).id


async def _age(org: uuid.UUID, user: uuid.UUID, worker_id: uuid.UUID, by: dt.timedelta) -> None:
    """Move the worker's last sighting into the past. The time is simulated
    rather than waited out, and nothing else about the row is touched. In
    the workspace's own session, because the row is behind RLS."""
    async with tenant_session(str(org), str(user)) as s:
        n = (
            await s.execute(
                text(
                    "UPDATE agent_workers SET last_seen_at = now() - make_interval(secs => :by) "
                    "WHERE id = :w RETURNING id"
                ),
                {"by": by.total_seconds(), "w": worker_id},
            )
        ).all()
    assert len(n) == 1


async def _close_idle(org: uuid.UUID, user: uuid.UUID) -> list[uuid.UUID]:
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        return await workers_svc.close_idle(s, idle_for=IDLE)


async def _row(org: uuid.UUID, user: uuid.UUID, worker_id: uuid.UUID) -> AgentWorker:
    async with tenant_session(str(org), str(user)) as s:
        row = await s.get(AgentWorker, worker_id)
        assert row is not None
        return row


async def _acquire(org: uuid.UUID, user: uuid.UUID, task_id: uuid.UUID, w: uuid.UUID) -> TaskLease:
    async with tenant_session(str(org), str(user)) as s:
        return await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=w)


async def test_a_session_unseen_for_hours_and_holding_nothing_is_closed(_embedder: None) -> None:
    org, user = await _org()
    dead = await _worker(org, user, "dead")
    await _age(org, user, dead, IDLE + dt.timedelta(minutes=1))

    assert await _close_idle(org, user) == [dead]

    row = await _row(org, user, dead)
    assert row.closed_at is not None
    async with tenant_session(str(org), str(user)) as s:
        listed = [w.id for w in await workers_svc.list_workers(s, org_id=org)]
        log = (
            (
                await s.execute(
                    select(ActivityLog).where(
                        ActivityLog.entity == "agent_worker",
                        ActivityLog.entity_id == dead,
                        ActivityLog.action == "close",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert dead not in listed
    assert len(log) == 1
    assert log[0].diff["reason"] == "idle"
    assert log[0].actor_id is None

    # A second pass has nothing to do: the close is not repeated or re-logged.
    assert await _close_idle(org, user) == []


async def test_a_session_seen_within_the_threshold_stays_open(_embedder: None) -> None:
    org, user = await _org()
    quiet = await _worker(org, user, "quiet")
    await _age(org, user, quiet, IDLE - dt.timedelta(minutes=1))

    assert await _close_idle(org, user) == []
    assert (await _row(org, user, quiet)).closed_at is None


async def test_a_silent_session_holding_a_long_lease_is_not_closed(_embedder: None) -> None:
    """Silence alone is not death: a lease taken with a long TTL is the
    session's own statement that it will be quiet for a while."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "long-lease")
    async with tenant_session(str(org), str(user)) as s:
        await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=86400
        )
    await _age(org, user, w, IDLE + dt.timedelta(hours=1))

    assert await _close_idle(org, user) == []
    assert (await _row(org, user, w)).closed_at is None


async def test_an_expired_lease_is_reclaimed_first_and_never_released_by_the_close(
    _embedder: None,
) -> None:
    """The close releases nothing. A dead session that still has an expired,
    unreclaimed lease keeps its worker until the lease sweep has run, and the
    release is then the sweep's, with the sweep's reason."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "died-holding")
    async with tenant_session(str(org), str(user)) as s:
        lease = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w, ttl_seconds=60
        )
        lease.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        lease_id = lease.id
    await _age(org, user, w, IDLE + dt.timedelta(minutes=1))

    assert await _close_idle(org, user) == []
    async with tenant_session(str(org), str(user)) as s:
        still = await s.get(TaskLease, lease_id)
        assert still is not None and still.released_at is None

    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        assert await leases_svc.sweep_expired(s) == [task_id]
    assert await _close_idle(org, user) == [w]


async def test_a_session_that_comes_back_after_the_close_is_told_to_open_a_new_one(
    _embedder: None,
) -> None:
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "came-back")
    await _age(org, user, w, IDLE + dt.timedelta(minutes=1))
    assert await _close_idle(org, user) == [w]

    with pytest.raises(NotFoundError):
        await _acquire(org, user, task_id, w)
    fresh = await _worker(org, user, "came-back-again")
    assert (await _acquire(org, user, task_id, fresh)).holder_worker_id == fresh


async def _a_worker_row_lock_is_awaited() -> bool:
    async with admin_session() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND query ILIKE 'UPDATE agent_workers%'"
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
    first_kind: str,
) -> tuple[Any, Any]:
    """Run ``first`` in a transaction kept open until ``second`` waits on a
    worker row lock, then commit ``first`` and let ``second`` finish."""
    async with tenant_session(str(org), str(user), actor_kind=first_kind) as s:
        a = await first(s)
        pending = asyncio.create_task(second())
        for _ in range(200):
            if await _a_worker_row_lock_is_awaited():
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


async def test_a_take_racing_a_close_that_commits_first_is_refused(_embedder: None) -> None:
    """The close commits while a session that had read its worker as open is
    stamping it. Without ``closed_at IS NULL`` in the stamp, the session would
    take a lease under a closed worker, which no ``worker_close`` frees."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "raced")
    await _age(org, user, w, IDLE + dt.timedelta(minutes=1))

    async def close(s: AsyncSession) -> list[uuid.UUID]:
        return await workers_svc.close_idle(s, idle_for=IDLE)

    closed, taken = await _interleave(
        org, user, close, lambda: _acquire(org, user, task_id, w), first_kind="system"
    )
    assert closed == [w]
    assert isinstance(taken, NotFoundError), f"took a lease under a closed worker: {taken!r}"
    async with tenant_session(str(org), str(user)) as s:
        leases = (
            (await s.execute(select(TaskLease).where(TaskLease.holder_worker_id == w)))
            .scalars()
            .all()
        )
    assert leases == []


async def test_a_close_racing_a_take_that_commits_first_closes_nothing(_embedder: None) -> None:
    """The session stamps its worker and takes a task; the close, which chose
    the worker as idle on its snapshot, waits on the row lock. When the take
    commits, the close re-reads a fresh ``last_seen_at`` and leaves it."""
    org, user = await _org()
    task_id = await _task(org, user)
    w = await _worker(org, user, "busy")
    await _age(org, user, w, IDLE + dt.timedelta(minutes=1))

    async def take(s: AsyncSession) -> TaskLease:
        return await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=w)

    taken, closed = await _interleave(
        org, user, take, lambda: _close_idle(org, user), first_kind="human_direct"
    )
    assert isinstance(taken, TaskLease)
    assert closed == [], f"closed a worker that had just taken a task: {closed!r}"
    assert (await _row(org, user, w)).closed_at is None
