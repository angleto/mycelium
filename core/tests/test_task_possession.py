"""Possession of a task, against the database, with real concurrency.

Three failures were reported from a workspace running fifteen agent
sessions at once, and they are one absence seen from three angles: two
sessions take the same task, a session moves a task on and keeps working
on it, a session dies and its task is held forever. Migration 0017 says
why the answer is a lease; this file is what makes the answer checkable.

**The test that carries the weight is the concurrent one**, and it is
concurrent for a reason that is not thoroughness. Every part of this
mechanism passes a sequential test trivially: call pull, get a task, call
pull again, get another one. The failure it exists to prevent only
appears when two callers are inside the choose-then-take window at the
same time, which means two sessions, two transactions, and both in flight
at once. A sequential test of a queue proves the queue can hand out
tasks, not that it can hand out each one once.

The others pin the two consequences that are easy to implement halfway:
that moving a task to another state ENDS the possession (not merely
records something), and that an expired holder does not get to come back
and write over whoever holds the task now.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ConflictError, NotFoundError
from mycelium_core.models.task_lease import LeaseRelease, TaskLease
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import task_leases as leases_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import workflow as wf_svc
from mycelium_core.services.auth import signup


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="POSSESS")
    return r.org_id, r.user_id


async def _states(org: uuid.UUID, user: uuid.UUID) -> dict[str, uuid.UUID]:
    """A workflow with a CHECKING station, made for this test.

    The seeded default has three stations (todo, in_progress, done) and
    no intermediate one, while the workspace these failures came from has
    a fourth where finished work waits to be checked. The mechanism under
    test is about handing possession over at a non-terminal station, so
    the test builds the workflow it needs and makes it the default.

    Building it rather than assuming it is also the stronger test. The
    service never names a state, on the grounds that the workflow is
    configuration a project can override; a test that read the shipped
    names back would agree with that claim without ever exercising it.
    """
    async with tenant_session(str(org), str(user)) as s:
        w = await wf_svc.create_workflow(
            s,
            org_id=org,
            actor_id=user,
            name=f"stations-{uuid.uuid4().hex[:8]}",
            states=[
                wf_svc.StateSpec(name="queued", ord=0, is_initial=True),
                wf_svc.StateSpec(name="working", ord=1),
                wf_svc.StateSpec(name="checking", ord=2),
                wf_svc.StateSpec(name="closed", ord=3, is_terminal=True),
            ],
            transitions=[
                ("queued", "working"),
                ("working", "checking"),
                ("checking", "closed"),
                ("checking", "queued"),
            ],
        )
        await wf_svc.set_default_workflow(s, org_id=org, actor_id=user, workflow_id=w.id)
        rows = (
            (await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == w.id)))
            .scalars()
            .all()
        )
    return {r.name: r.id for r in rows}


async def _make_tasks(org: uuid.UUID, user: uuid.UUID, n: int) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    async with tenant_session(str(org), str(user)) as s:
        for i in range(n):
            t = await tasks_svc.create_task(s, org_id=org, actor_id=user, title=f"task-{i}")
            out.append(t.id)
    return out


async def _lease_rows(org: uuid.UUID, user: uuid.UUID, task_id: uuid.UUID) -> list[TaskLease]:
    async with tenant_session(str(org), str(user)) as s:
        return list(
            (
                await s.execute(
                    select(TaskLease)
                    .where(TaskLease.task_id == task_id)
                    .order_by(TaskLease.acquired_at)
                )
            )
            .scalars()
            .all()
        )


async def test_two_workers_pulling_at_once_get_two_different_tasks(_embedder: None) -> None:
    """The one that matters, and the reason it is written concurrently.

    Ten tasks, ten pullers, all released into the same station at the
    same moment with their own sessions. The assertion is not "everyone
    got something" but "no task was handed to two workers", which is the
    only statement that distinguishes this mechanism from the
    list-then-transition it replaces: that one also hands everybody a
    task, it just hands several of them the same one.
    """
    org, user = await _org()
    states = await _states(org, user)
    queued, working = states["queued"], states["working"]
    await _make_tasks(org, user, 10)

    async def worker(name: str) -> uuid.UUID | None:
        async with tenant_session(str(org), str(user)) as s:
            try:
                task, _lease = await leases_svc.pull(
                    s,
                    org_id=org,
                    actor_id=user,
                    state_id=queued,
                    worker_id=name,
                    transition_to=working,
                )
            except NotFoundError:
                return None
            return task.id

    got = await asyncio.gather(*(worker(f"w{i}") for i in range(10)))
    taken = [t for t in got if t is not None]

    # Every worker got work: ten unheld tasks, ten pullers.
    assert len(taken) == 10
    # And no task went to two of them. This is the assertion.
    assert len(set(taken)) == len(taken)


async def test_concurrent_pull_without_a_transition_still_hands_each_task_once(
    _embedder: None,
) -> None:
    """The checking station's path, which has no version gate under it.

    Measured, because the first version of this file did not have this
    test and the one above passes for the wrong reason without it. Remove
    the row lock and the test above still fails -- but it fails on a
    stale-version conflict raised by the transition, which is
    ``set_state``'s existing gate doing the catching, not possession. A
    puller that takes a task WITHOUT moving it runs no such gate: it is
    the predicate, the insert, and nothing else.

    That is the station where work waits to be checked, which is exactly
    the queue the sessions here pull from without transitioning, so it is
    the case least protected by what was already there.
    """
    org, user = await _org()
    states = await _states(org, user)
    ids = await _make_tasks(org, user, 6)
    async with tenant_session(str(org), str(user)) as s:
        for tid in ids:
            t = await tasks_svc.get_task(s, org_id=org, task_id=tid)
            v = await tasks_svc.set_state(
                s,
                org_id=org,
                actor_id=user,
                task_id=tid,
                expected_version=t.version,
                state_id=states["working"],
            )
            await tasks_svc.set_state(
                s,
                org_id=org,
                actor_id=user,
                task_id=tid,
                expected_version=v,
                state_id=states["checking"],
            )

    async def checker(name: str) -> uuid.UUID | None:
        async with tenant_session(str(org), str(user)) as s:
            try:
                task, _ = await leases_svc.pull(
                    s, org_id=org, actor_id=user, state_id=states["checking"], worker_id=name
                )
            except NotFoundError:
                return None
            return task.id

    got = await asyncio.gather(*(checker(f"c{i}") for i in range(6)))
    taken = [t for t in got if t is not None]
    assert len(taken) == 6
    assert len(set(taken)) == len(taken)


async def test_a_second_worker_cannot_take_what_is_already_held(_embedder: None) -> None:
    """And the refusal names the deadline, so the caller can choose
    between waiting and moving on rather than just retrying."""
    org, user = await _org()
    (task_id,) = await _make_tasks(org, user, 1)

    async with tenant_session(str(org), str(user)) as s:
        first = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id="w1"
        )
    assert first.holder_worker_id == "w1"

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(ConflictError) as err:
            await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id="w2")
    assert "expires_at" in err.value.params


async def test_re_acquiring_your_own_lease_extends_it_instead_of_failing(
    _embedder: None,
) -> None:
    """A session whose reply was lost to a timeout has to be able to
    retry without first finding out whether the call landed."""
    org, user = await _org()
    (task_id,) = await _make_tasks(org, user, 1)

    async with tenant_session(str(org), str(user)) as s:
        first = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id="w1", ttl_seconds=120
        )
        first_id, first_deadline = first.id, first.expires_at
    async with tenant_session(str(org), str(user)) as s:
        again = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id="w1", ttl_seconds=600
        )
    assert again.id == first_id
    assert again.expires_at > first_deadline


async def test_moving_a_task_to_another_state_releases_the_possession(
    _embedder: None,
) -> None:
    """The reported symptom, as a mechanism.

    An agent that moves a task to the checking station and carries on
    working was described as a discipline problem. It is not: it had
    nothing to hand back, because possession did not exist. Here the
    transition ends it, and the worker's next write to the task is
    refused with a code that says why -- which is the difference between
    a rule written in a workflow description and a rule.
    """
    org, user = await _org()
    states = await _states(org, user)
    await _make_tasks(org, user, 1)

    async with tenant_session(str(org), str(user)) as s:
        task, lease = await leases_svc.pull(
            s,
            org_id=org,
            actor_id=user,
            state_id=states["queued"],
            worker_id="implementer",
            transition_to=states["working"],
        )
        task_id, version = task.id, task.version
    assert lease.state_id == states["working"]

    async with tenant_session(str(org), str(user)) as s:
        await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=version,
            state_id=states["checking"],
            worker_id="implementer",
        )

    rows = await _lease_rows(org, user, task_id)
    assert len(rows) == 1
    assert rows[0].released_at is not None
    # ``handoff`` and not ``done``: a reader asking what this worker
    # passed on for checking must not be answered with work it finished.
    assert rows[0].release_reason == LeaseRelease.handoff.value

    # And it really is gone: renewing it now says so rather than
    # quietly succeeding.
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(ConflictError):
            await leases_svc.renew(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id="implementer"
            )


async def test_the_checker_is_not_the_worker_who_handed_the_task_over(
    _embedder: None,
) -> None:
    """The workflow's own description of the checking station says the
    check is done by somebody other than whoever did the work. This is
    that sentence as a predicate, and the test runs both halves: the
    author is skipped, and somebody else gets the task."""
    org, user = await _org()
    states = await _states(org, user)
    await _make_tasks(org, user, 1)

    async with tenant_session(str(org), str(user)) as s:
        task, _ = await leases_svc.pull(
            s,
            org_id=org,
            actor_id=user,
            state_id=states["queued"],
            worker_id="implementer",
            transition_to=states["working"],
        )
        task_id, version = task.id, task.version
    async with tenant_session(str(org), str(user)) as s:
        await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=version,
            state_id=states["checking"],
            worker_id="implementer",
        )

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError):
            await leases_svc.pull(
                s,
                org_id=org,
                actor_id=user,
                state_id=states["checking"],
                worker_id="implementer",
                exclude_own_handoffs=True,
            )

    async with tenant_session(str(org), str(user)) as s:
        checked, _ = await leases_svc.pull(
            s,
            org_id=org,
            actor_id=user,
            state_id=states["checking"],
            worker_id="checker",
            exclude_own_handoffs=True,
        )
    assert checked.id == task_id


async def test_an_expired_lease_is_reclaimed_and_the_old_holder_is_fenced_out(
    _embedder: None,
) -> None:
    """The dead-session backstop, plus the thing that makes reclaiming
    safe: a holder that comes back carries a fence that no longer
    matches, so it is refused instead of extending a possession that is
    now somebody else's."""
    org, user = await _org()
    (task_id,) = await _make_tasks(org, user, 1)

    async with tenant_session(str(org), str(user)) as s:
        dead = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id="dead", ttl_seconds=60
        )
        dead_fence = int(dead.fence)
        # Reach past the TTL clamp by moving the deadline directly: the
        # alternative is sleeping for the minimum TTL, and a test that
        # sleeps a minute is a test nobody runs.
        dead.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)

    async with tenant_session(str(org), str(user)) as s:
        reclaimed = await leases_svc.sweep_expired(s)
    assert reclaimed == [task_id]

    async with tenant_session(str(org), str(user)) as s:
        fresh = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id="alive"
        )
    assert int(fresh.fence) > dead_fence

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(ConflictError):
            await leases_svc.renew(
                s,
                org_id=org,
                actor_id=user,
                task_id=task_id,
                worker_id="dead",
                fence=dead_fence,
            )


async def test_a_task_nobody_holds_is_still_movable_by_anybody(_embedder: None) -> None:
    """Possession constrains only what it covers.

    Every path that predates it -- the web UI, the CLI, the scheduler, an
    agent that never took a lease -- has to keep working, and a lease
    nobody took covers nothing. This is the test that would catch a
    version of the hook that made ``set_state`` require a lease.
    """
    org, user = await _org()
    states = await _states(org, user)
    (task_id,) = await _make_tasks(org, user, 1)

    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=task.version,
            state_id=states["working"],
        )

    async with tenant_session(str(org), str(user)) as s:
        moved = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
    assert moved.state_id == states["working"]
    assert await _lease_rows(org, user, task_id) == []


async def test_an_agent_credential_cannot_move_a_task_another_worker_holds(
    _embedder: None,
) -> None:
    """The other half of the rule, and the half that was inert.

    The exemption that lets somebody unstick a workspace was written as
    ``task.owner_id == actor_id``. In a workspace where one person owns
    every task and every agent authenticates as that person, that
    exempts the agents too, and the rule protected nothing. It is now
    narrowed by actor kind: an agent credential never takes the
    exemption, whoever it authenticates as.

    Run under an ``mcp_token`` actor, which is what an agent session is,
    against a task the same user owns. Before the narrowing this passed
    the move through; it now refuses it and names the deadline.
    """
    org, user = await _org()
    states = await _states(org, user)
    (task_id,) = await _make_tasks(org, user, 1)

    async with tenant_session(str(org), str(user)) as s:
        await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id="holder")
        task = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        version = task.version

    async with tenant_session(
        str(org), str(user), actor_kind="mcp_token", actor_subject_id=str(uuid.uuid4())
    ) as s:
        with pytest.raises(ConflictError) as err:
            await tasks_svc.set_state(
                s,
                org_id=org,
                actor_id=user,
                task_id=task_id,
                expected_version=version,
                state_id=states["working"],
                worker_id="intruder",
            )
    assert "expires_at" in err.value.params

    # And the holder still holds it: a refused move changes nothing.
    rows = await _lease_rows(org, user, task_id)
    assert len(rows) == 1
    assert rows[0].released_at is None


async def test_the_person_can_still_unstick_a_task_an_agent_is_holding(
    _embedder: None,
) -> None:
    """The exemption that survives the narrowing, and the reason it has
    to: without it a workspace whose agent died waits out the deadline
    before anybody can touch the task, and a human looking at a stuck
    board has no lever.

    Same task, same holder, same foreign worker id. The only difference
    from the test above is the actor kind, which is the whole point.
    """
    org, user = await _org()
    states = await _states(org, user)
    (task_id,) = await _make_tasks(org, user, 1)

    async with tenant_session(str(org), str(user)) as s:
        await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id="holder")
        task = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        version = task.version

    async with tenant_session(str(org), str(user)) as s:
        await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=version,
            state_id=states["working"],
        )

    rows = await _lease_rows(org, user, task_id)
    assert rows[0].release_reason == LeaseRelease.handoff.value
