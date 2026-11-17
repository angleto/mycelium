"""A state that declares the work done need not be terminal.

A workflow may close work into ``accepted`` and archive it later. Entering
``accepted`` was released as the checker's ``handoff``, because the reason
was picked from ``is_terminal`` alone: the checker became an author of the
task, and ``handed_over_by_worker_id`` named it, which let a third worker
close again on a handover nobody made.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.task import Task
from mycelium_core.models.task_lease import LeaseRelease, TaskLease
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import task_leases as leases_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import workflow as wf_svc
from mycelium_core.services.auth import signup

AGENT = {"actor_kind": "mcp_token", "actor_subject_id": str(uuid.uuid4())}
PERSON = {"actor_kind": "human_api"}


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def test_entering_a_nonterminal_closing_state_is_not_a_handoff(_embedder: None) -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="V2NT",
        )
    org, user = r.org_id, r.user_id

    def sess(who: dict[str, str]):  # type: ignore[no-untyped-def]
        return tenant_session(str(org), str(user), **who)  # type: ignore[arg-type]

    async with sess(PERSON) as s:
        w = await wf_svc.create_workflow(
            s,
            org_id=org,
            actor_id=user,
            name=f"nt-{uuid.uuid4().hex[:6]}",
            states=[
                wf_svc.StateSpec(name="queued", ord=0, is_initial=True),
                wf_svc.StateSpec(name="working", ord=1),
                wf_svc.StateSpec(name="checking", ord=2),
                wf_svc.StateSpec(name="accepted", ord=3, closes_work=True),
                wf_svc.StateSpec(name="archived", ord=4, is_terminal=True),
            ],
            transitions=[
                ("queued", "working"),
                ("working", "checking"),
                ("checking", "accepted"),
                ("accepted", "archived"),
            ],
        )
        await wf_svc.set_default_workflow(s, org_id=org, actor_id=user, workflow_id=w.id)
        st = {
            x.name: x.id
            for x in (
                await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == w.id))
            )
            .scalars()
            .all()
        }
        task_id = (await tasks_svc.create_task(s, org_id=org, actor_id=user, title="t")).id

    async def worker(label: str) -> uuid.UUID:
        async with sess(AGENT) as s:
            return (await workers_svc.open_worker(s, org_id=org, actor_id=user, label=label)).id

    async def move(who: dict[str, str], state: str, wid: uuid.UUID | None = None) -> None:
        async with sess(PERSON) as s:
            v = (await tasks_svc.get_task(s, org_id=org, task_id=task_id)).version
        async with sess(who) as s:
            await tasks_svc.set_state(
                s,
                org_id=org,
                actor_id=user,
                task_id=task_id,
                expected_version=v,
                state_id=st[state],
                worker_id=wid,
            )

    async def take(wid: uuid.UUID) -> None:
        async with sess(AGENT) as s:
            await leases_svc.acquire(s, org_id=org, actor_id=user, task_id=task_id, worker_id=wid)

    author, checker = await worker("author"), await worker("checker")
    await move(PERSON, "working")
    await take(author)
    await move(AGENT, "checking", author)
    await take(checker)
    await move(AGENT, "accepted", checker)  # the checked close: allowed

    async with sess(PERSON) as s:
        reason = (
            await s.execute(
                select(TaskLease.release_reason).where(
                    TaskLease.task_id == task_id, TaskLease.holder_worker_id == checker
                )
            )
        ).scalar_one()
        handed = (
            await s.execute(select(Task.handed_over_by_worker_id).where(Task.id == task_id))
        ).scalar_one()
    # The checker declared the work done. It did not pass the work on.
    assert reason != LeaseRelease.handoff.value
    assert handed is None
