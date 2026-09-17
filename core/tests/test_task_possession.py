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

The last section calls the MCP tools rather than the service. Everything
before it passes a holder that is already a ``UUID``; the tools take it
as an optional string, and that conversion is where a caller who never
took a lease -- the UI, the CLI, every agent session today -- was being
refused.
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
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import task_leases as leases_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import workflow as wf_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.gateway import execute_tool
from mycelium_mcp.server import _PRINCIPAL


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


async def _worker(org: uuid.UUID, user: uuid.UUID, label: str) -> uuid.UUID:
    """A working session, minted by the server.

    The tests call this instead of inventing a string, which is the
    change the whole mechanism turns on: an id the caller chooses can
    collide by accident, and two sessions that collide would each read as
    the holder of the other's lease. The label is only for a human
    reading a failure.
    """
    async with tenant_session(str(org), str(user)) as s:
        w = await workers_svc.open_worker(s, org_id=org, actor_id=user, label=label)
        return w.id


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

    async def worker(wid: uuid.UUID) -> uuid.UUID | None:
        async with tenant_session(str(org), str(user)) as s:
            try:
                task, _lease = await leases_svc.pull(
                    s,
                    org_id=org,
                    actor_id=user,
                    state_id=queued,
                    worker_id=wid,
                    transition_to=working,
                )
            except NotFoundError:
                return None
            return task.id

    ids = [await _worker(org, user, f"w{i}") for i in range(10)]
    got = await asyncio.gather(*(worker(w) for w in ids))
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

    async def checker(wid: uuid.UUID) -> uuid.UUID | None:
        async with tenant_session(str(org), str(user)) as s:
            try:
                task, _ = await leases_svc.pull(
                    s, org_id=org, actor_id=user, state_id=states["checking"], worker_id=wid
                )
            except NotFoundError:
                return None
            return task.id

    cids = [await _worker(org, user, f"c{i}") for i in range(6)]
    got = await asyncio.gather(*(checker(w) for w in cids))
    taken = [t for t in got if t is not None]
    assert len(taken) == 6
    assert len(set(taken)) == len(taken)


async def test_a_second_worker_cannot_take_what_is_already_held(_embedder: None) -> None:
    """And the refusal names the deadline, so the caller can choose
    between waiting and moving on rather than just retrying."""
    org, user = await _org()
    (task_id,) = await _make_tasks(org, user, 1)
    w1_w = await _worker(org, user, "w1")
    w2_w = await _worker(org, user, "w2")

    async with tenant_session(str(org), str(user)) as s:
        first = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w1_w
        )
    assert first.holder_worker_id == w1_w

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(ConflictError) as err:
            await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=w2_w)
    assert "expires_at" in err.value.params


async def test_re_acquiring_your_own_lease_extends_it_instead_of_failing(
    _embedder: None,
) -> None:
    """A session whose reply was lost to a timeout has to be able to
    retry without first finding out whether the call landed."""
    org, user = await _org()
    (task_id,) = await _make_tasks(org, user, 1)
    w1_w = await _worker(org, user, "w1")

    async with tenant_session(str(org), str(user)) as s:
        first = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w1_w, ttl_seconds=120
        )
        first_id, first_deadline = first.id, first.expires_at
    async with tenant_session(str(org), str(user)) as s:
        again = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=w1_w, ttl_seconds=600
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
    implementer_w = await _worker(org, user, "implementer")

    async with tenant_session(str(org), str(user)) as s:
        task, lease = await leases_svc.pull(
            s,
            org_id=org,
            actor_id=user,
            state_id=states["queued"],
            worker_id=implementer_w,
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
            worker_id=implementer_w,
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
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=implementer_w
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
    implementer_w = await _worker(org, user, "implementer")
    checker_w = await _worker(org, user, "checker")

    async with tenant_session(str(org), str(user)) as s:
        task, _ = await leases_svc.pull(
            s,
            org_id=org,
            actor_id=user,
            state_id=states["queued"],
            worker_id=implementer_w,
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
            worker_id=implementer_w,
        )

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError):
            await leases_svc.pull(
                s,
                org_id=org,
                actor_id=user,
                state_id=states["checking"],
                worker_id=implementer_w,
                exclude_own_handoffs=True,
            )

    async with tenant_session(str(org), str(user)) as s:
        checked, _ = await leases_svc.pull(
            s,
            org_id=org,
            actor_id=user,
            state_id=states["checking"],
            worker_id=checker_w,
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
    dead_w = await _worker(org, user, "dead")
    alive_w = await _worker(org, user, "alive")

    async with tenant_session(str(org), str(user)) as s:
        dead = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=dead_w, ttl_seconds=60
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
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=alive_w
        )
    assert int(fresh.fence) > dead_fence

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(ConflictError):
            await leases_svc.renew(
                s,
                org_id=org,
                actor_id=user,
                task_id=task_id,
                worker_id=dead_w,
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
    holder_w = await _worker(org, user, "holder")
    intruder_w = await _worker(org, user, "intruder")

    async with tenant_session(str(org), str(user)) as s:
        await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=holder_w)
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
                worker_id=intruder_w,
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
    holder_w = await _worker(org, user, "holder")

    async with tenant_session(str(org), str(user)) as s:
        await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=holder_w)
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


async def test_a_worker_that_is_shut_down_frees_its_tasks_at_once(_embedder: None) -> None:
    """The first of the two recovery paths, and the fast one.

    A session being stopped is the case where the system KNOWS the work
    has ended, so making its tasks wait out a deadline would be holding a
    lock for no reason. Closing gives them back in the same call, and
    another worker can take them immediately.
    """
    org, user = await _org()
    states = await _states(org, user)
    ids = await _make_tasks(org, user, 2)
    leaving = await _worker(org, user, "leaving")
    arriving = await _worker(org, user, "arriving")

    async with tenant_session(str(org), str(user)) as s:
        for tid in ids:
            await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=tid, worker_id=leaving)

    async with tenant_session(str(org), str(user)) as s:
        worker, freed = await workers_svc.close_worker(
            s, org_id=org, actor_id=user, worker_id=leaving
        )
    assert worker.closed_at is not None
    assert sorted(freed) == sorted(ids)

    # Free means free: somebody else takes one without waiting.
    async with tenant_session(str(org), str(user)) as s:
        taken, _ = await leases_svc.pull(
            s, org_id=org, actor_id=user, state_id=states["queued"], worker_id=arriving
        )
    assert taken.id in ids

    # And a closed worker cannot take anything more: it has announced it
    # is gone, and a lease under it would have nobody to renew it.
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError):
            await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=ids[1], worker_id=leaving
            )


async def test_a_worker_that_dies_frees_its_tasks_without_anybody_asking(
    _embedder: None,
) -> None:
    """The second recovery path, and the one that must need nobody.

    A session that is killed calls nothing. What frees its work is the
    deadline plus the sweep, and this runs the whole way through: the
    task is held, the holder goes silent, the sweep reclaims, another
    worker takes it. No human anywhere in it.

    The deadline is moved directly rather than waited out. The TTL clamp
    has a floor of a minute and a test that sleeps a minute is a test
    nobody runs, so what is simulated is the passage of time and nothing
    else.
    """
    org, user = await _org()
    states = await _states(org, user)
    (task_id,) = await _make_tasks(org, user, 1)
    killed = await _worker(org, user, "killed")
    successor = await _worker(org, user, "successor")

    async with tenant_session(str(org), str(user)) as s:
        task, lease = await leases_svc.pull(
            s,
            org_id=org,
            actor_id=user,
            state_id=states["queued"],
            worker_id=killed,
            transition_to=states["working"],
        )
        assert task.id == task_id
        lease.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)

    # Nobody calls anything on behalf of the dead session. The worker row
    # is still open -- it never got to say otherwise -- and that is
    # exactly the state the sweep has to cope with.
    async with tenant_session(str(org), str(user)) as s:
        reclaimed = await leases_svc.sweep_expired(s)
    assert reclaimed == [task_id]

    async with tenant_session(str(org), str(user)) as s:
        rows = await workers_svc.list_workers(s, org_id=org)
    assert killed in [w.id for w in rows], "the sweep frees the work, not the session"

    # The task stayed where the dead session left it, which is the truth
    # about how far it got, and it is takeable again.
    async with tenant_session(str(org), str(user)) as s:
        again = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        assert again.state_id == states["working"]
        retaken, _ = await leases_svc.pull(
            s, org_id=org, actor_id=user, state_id=states["working"], worker_id=successor
        )
    assert retaken.id == task_id


async def test_the_refusal_names_who_is_holding_it_and_until_when(_embedder: None) -> None:
    """The message a person actually meets, and the reason it carries
    params.

    A refused write is exactly the moment a caller has no second round
    trip to spend finding out who blocked it. Before this the refusal
    said "another worker" and the interface showed the generic conflict
    sentence, which tells a person to reload -- round a loop that cannot
    terminate, because reloading changes nothing about who holds it.
    """
    org, user = await _org()
    states = await _states(org, user)
    (task_id,) = await _make_tasks(org, user, 1)
    holder_w = await _worker(org, user, "verify-3")
    other_w = await _worker(org, user, "verify-4")

    async with tenant_session(str(org), str(user)) as s:
        lease = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task_id, worker_id=holder_w
        )
        deadline = lease.expires_at

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(ConflictError) as err:
            await leases_svc.acquire(
                s, org_id=org, actor_id=user, task_id=task_id, worker_id=other_w
            )
    # The worker's OWN label, not its uuid: a person reading a board
    # cannot resolve an id, and neither can an agent reading the prose.
    assert err.value.params["holder"] == "verify-3"
    assert err.value.params["expires_at"] == deadline.isoformat()

    # The same name the projection shows, so one holder reads as one
    # holder wherever it is met.
    async with tenant_session(str(org), str(user)) as s:
        live = await leases_svc.live_lease(s, task_id=task_id)
        assert live is not None
        assert await leases_svc.held_by(s, live) == "verify-3"

    # And the transition refusal carries it too, which is the path the
    # web UI takes.
    async with tenant_session(
        str(org), str(user), actor_kind="mcp_token", actor_subject_id=str(uuid.uuid4())
    ) as s:
        task = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        with pytest.raises(ConflictError) as err2:
            await tasks_svc.set_state(
                s,
                org_id=org,
                actor_id=user,
                task_id=task_id,
                expected_version=task.version,
                state_id=states["working"],
                worker_id=other_w,
            )
    assert err2.value.params["holder"] == "verify-3"


async def test_a_person_can_free_a_held_task_without_taking_it(_embedder: None) -> None:
    """The lever the interface needed and the service did not have.

    Freeing is not the same as acquiring, and the difference is what
    somebody at a stuck board wants: the task AVAILABLE, not assigned to
    their browser tab -- which would have to hold a lease it will never
    release when the window closes, trading one stuck task for another.
    """
    org, user = await _org()
    states = await _states(org, user)
    (task_id,) = await _make_tasks(org, user, 1)
    stuck = await _worker(org, user, "stuck")
    next_up = await _worker(org, user, "next")

    async with tenant_session(str(org), str(user)) as s:
        await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=stuck)

    async with tenant_session(str(org), str(user)) as s:
        freed = await leases_svc.preempt(s, org_id=org, actor_id=user, task_id=task_id)
    assert freed is not None
    # Its own reason, so the sweep's count stays a measure of sessions
    # that died rather than of decisions somebody took.
    assert freed.release_reason == LeaseRelease.preempted.value

    # Free means free, and free for ANYBODY rather than for the person
    # who pressed it.
    async with tenant_session(str(org), str(user)) as s:
        taken, _ = await leases_svc.pull(
            s, org_id=org, actor_id=user, state_id=states["queued"], worker_id=next_up
        )
    assert taken.id == task_id

    # Nothing held: a second press says so instead of inventing a lease.
    async with tenant_session(str(org), str(user)) as s:
        await leases_svc.preempt(s, org_id=org, actor_id=user, task_id=task_id)
        assert await leases_svc.preempt(s, org_id=org, actor_id=user, task_id=task_id) is None


# --- through the MCP adapter -----------------------------------------------
#
# Everything above calls the service, where ``worker_id`` is already a
# ``UUID | None``. The tool that agents actually call takes it as an
# optional STRING, and the conversion between the two is a layer no test
# reached: the first defect below made every transition through MCP fail,
# and every test above passed while it did.


async def test_moving_a_task_through_mcp_without_a_worker_is_the_ordinary_call(
    _embedder: None,
) -> None:
    """The adapter, not the service, and that difference is the defect.

    ``set_task_state`` converted its optional ``worker_id`` string with
    an unguarded ``uuid.UUID(...)``, so omitting it -- which is what
    every caller that never took a lease does, and today that is all of
    them -- raised ``TypeError`` in the prologue, before any rule these
    tests cover ran. Reported from a session closing two cards on
    2026-09-16: three tasks in two projects, the same
    ``one of the hex, bytes, bytes_le, fields, or int arguments must be
    given``, and finished work left sitting in ``in_progress`` because no
    client could move it.

    The assertion is deliberately the whole reply rather than its absence
    of an error: a guard that swallowed the argument and moved nothing
    would satisfy the weaker one.
    """
    org, user = await _org()
    states = await _states(org, user)
    (task_id,) = await _make_tasks(org, user, 1)
    async with tenant_session(str(org), str(user)) as s:
        version = (await tasks_svc.get_task(s, org_id=org, task_id=task_id)).version

    principal = _PRINCIPAL.set((user, org, None))
    try:
        res = await execute_tool(
            name="set_task_state",
            arguments={
                "task_id": str(task_id),
                "expected_version": version,
                "state_id": str(states["working"]),
            },
        )
    finally:
        _PRINCIPAL.reset(principal)

    # The id comes back in the short form this surface speaks (ADR-0064): the
    # gateway shortens a task id on the way out and expands one on the way in,
    # including in an echo-back like this one, so that every id a caller reads
    # from Mycelium is spelled the same way whatever call produced it.
    assert res == {"task_id": str(task_id)[:8], "version": version + 1}
    async with tenant_session(str(org), str(user)) as s:
        assert (await tasks_svc.get_task(s, org_id=org, task_id=task_id)).state_id == (
            states["working"]
        )


async def test_reading_who_holds_what_through_mcp_without_naming_a_worker(
    _embedder: None,
) -> None:
    """The same conversion, in the tool that exists to prevent conflicts.

    ``task_leases_list`` is how a session sees its collaborators instead
    of discovering them at a refused write, and ``worker_id`` NARROWS it
    to one holder. The unfiltered call is the one ADR-0063 describes, and
    it raised the same ``TypeError``: the only tool that answers "who
    holds this" could not be called without already knowing the answer.
    """
    org, user = await _org()
    await _states(org, user)
    (task_id,) = await _make_tasks(org, user, 1)
    holder = await _worker(org, user, "holder-1")
    async with tenant_session(str(org), str(user)) as s:
        await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=holder)

    principal = _PRINCIPAL.set((user, org, None))
    try:
        rows = await execute_tool(name="task_leases_list", arguments={})
    finally:
        _PRINCIPAL.reset(principal)

    assert [r["task_id"] for r in rows] == [str(task_id)[:8]]  # short form, see above
    assert rows[0]["worker_id"] == str(holder)
    # The NAME travels with it, because neither a person nor an agent can
    # resolve a uuid, and a refusal that names nobody sends both round a
    # loop that cannot terminate.
    assert rows[0]["holder"] == "holder-1"
