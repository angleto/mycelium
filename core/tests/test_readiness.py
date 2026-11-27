"""One readiness rule by dependency type, and every reader goes through it.

Before 2026-11-26 the blocked overlay held a task back on ANY non-terminal
predecessor, whatever the dependency type, while ``task_pull`` and the
dispatch loop's admitted set did not look at dependencies at all: an agent
could pull, or be dispatched on, a task whose finish-to-start predecessor
was still open. The rule now lives in ``services.readiness``:

- FS holds back until the predecessor is in a terminal state;
- SS holds back until the predecessor has started (an ``actual_start``, or
  out of the initial state);
- FF and SF never hold a start back;
- a soft-deleted predecessor holds nothing back.

The dispatch reader is covered in ``api/tests/test_dispatch_p5.py`` next to
the loop's other tests.

Observed failing, 2026-11-26, against the code before ``readiness`` existed:
every test here except the finish-to-start one (whose rule is unchanged),
and the dispatch test in ``test_dispatch_p5.py``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.dependency import DependencyType
from mycelium_core.models.task import Task
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import connections as conn_svc
from mycelium_core.services import dependencies as dep_svc
from mycelium_core.services import task_leases as leases_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


@pytest.fixture(autouse=True)
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="READY",
        )
    return r.org_id, r.user_id


async def _state(
    s: AsyncSession, task_id: uuid.UUID, *, initial: bool, terminal: bool
) -> uuid.UUID:
    """A state of the task's own workflow with the given flags."""
    task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
    own = (
        await s.execute(select(WorkflowState).where(WorkflowState.id == task.state_id))
    ).scalar_one()
    return (
        await s.execute(
            select(WorkflowState.id)
            .where(
                WorkflowState.workflow_id == own.workflow_id,
                WorkflowState.is_initial.is_(initial),
                WorkflowState.is_terminal.is_(terminal),
            )
            .order_by(WorkflowState.ord)
            .limit(1)
        )
    ).scalar_one()


async def _move(
    s: AsyncSession, org: uuid.UUID, user: uuid.UUID, task_id: uuid.UUID, state_id: uuid.UUID
) -> None:
    task = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
    await tasks_svc.set_state(
        s,
        org_id=org,
        actor_id=user,
        task_id=task_id,
        expected_version=task.version,
        state_id=state_id,
    )


async def _pair(
    s: AsyncSession, org: uuid.UUID, user: uuid.UUID, dep_type: DependencyType
) -> tuple[uuid.UUID, uuid.UUID]:
    pred = await tasks_svc.create_task(s, org_id=org, actor_id=user, title=f"before {dep_type}")
    succ = await tasks_svc.create_task(s, org_id=org, actor_id=user, title=f"after {dep_type}")
    await dep_svc.add_dependency(
        s,
        org_id=org,
        actor_id=user,
        predecessor_id=pred.id,
        successor_id=succ.id,
        type=dep_type,
    )
    return pred.id, succ.id


async def _blocked(s: AsyncSession, org: uuid.UUID, task_id: uuid.UUID) -> bool:
    return task_id in await dep_svc.blocked_task_ids(s, org_id=org, node_ids={task_id})


async def test_finish_to_start_waits_for_a_terminal_predecessor() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        pred, succ = await _pair(s, org, user, DependencyType.FS)
        assert await _blocked(s, org, succ)
        await _move(s, org, user, pred, await _state(s, pred, initial=False, terminal=False))
        assert await _blocked(s, org, succ), "started is not finished"
        await _move(s, org, user, pred, await _state(s, pred, initial=False, terminal=True))
        assert not await _blocked(s, org, succ)


async def test_start_to_start_waits_for_a_started_predecessor() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        pred, succ = await _pair(s, org, user, DependencyType.SS)
        assert await _blocked(s, org, succ)
        await _move(s, org, user, pred, await _state(s, pred, initial=False, terminal=False))
        assert not await _blocked(s, org, succ)

        # Started through time tracking while still in the initial state.
        pred2, succ2 = await _pair(s, org, user, DependencyType.SS)
        assert await _blocked(s, org, succ2)
        await s.execute(
            update(Task)
            .where(Task.id == pred2)
            .values(actual_start=dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC))
        )
        assert not await _blocked(s, org, succ2)


@pytest.mark.parametrize("dep_type", [DependencyType.FF, DependencyType.SF])
async def test_finish_constraints_never_hold_a_start_back(dep_type: DependencyType) -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        _pred, succ = await _pair(s, org, user, dep_type)
        assert not await _blocked(s, org, succ)


async def test_a_deleted_predecessor_holds_nothing_back() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        pred, succ = await _pair(s, org, user, DependencyType.FS)
        assert await _blocked(s, org, succ)
        await s.execute(
            update(Task)
            .where(Task.id == pred)
            .values(deleted_at=dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC))
        )
        assert not await _blocked(s, org, succ)


async def test_pulling_from_the_initial_state_skips_a_task_held_back() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        pred, succ = await _pair(s, org, user, DependencyType.FS)
        initial = await _state(s, succ, initial=True, terminal=False)
        worker = (await workers_svc.open_worker(s, org_id=org, actor_id=user, label="ready")).id
        task, _lease = await leases_svc.pull(
            s, org_id=org, actor_id=user, state_id=initial, worker_id=worker
        )
        assert task.id == pred
        # The only other task in the station waits for the one just taken.
        with pytest.raises(NotFoundError) as exc:
            await leases_svc.pull(s, org_id=org, actor_id=user, state_id=initial, worker_id=worker)
        assert exc.value.code == MessageCode.LEASE_QUEUE_EMPTY


async def test_the_connections_panel_says_which_dependency_still_holds() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        pred, succ = await _pair(s, org, user, DependencyType.FS)
        _p2, succ_ff = await _pair(s, org, user, DependencyType.FF)

        async def holding(task_id: uuid.UUID) -> list[bool]:
            listing = await conn_svc.list_connections(
                s, org_id=org, item=conn_svc.Item(kind=conn_svc.ItemKind.task, id=task_id)
            )
            return [
                bool(c.detail.get("holding"))
                for c in listing
                if c.relation is conn_svc.Relation.depends_on
            ]

        assert await holding(succ) == [True]
        assert await holding(succ_ff) == [False]
        await _move(s, org, user, pred, await _state(s, pred, initial=False, terminal=False))
        await _move(s, org, user, pred, await _state(s, pred, initial=False, terminal=True))
        assert await holding(succ) == [False]
