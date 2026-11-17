"""The sweep frees a dead session's task AND closes the session, in one pass.

The order inside the pass is what this pins: the reclaim commits first, so a
worker whose only lease has just expired holds nothing by the time the idle
close looks at it. Reversed, the worker would survive the pass and wait for
the next one; dropped, it would survive forever, which is what production
showed on 2026-11-17 (157 open workers, one alive).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import text

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.agent_worker import AgentWorker
from mycelium_core.models.task_lease import TaskLease
from mycelium_core.services import agent_workers as workers_svc
from mycelium_core.services import task_leases as leases_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup
from mycelium_worker import task_leases as sweep


async def test_one_pass_reclaims_the_task_and_closes_the_dead_session() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="SWEEP",
        )
    org, user = r.org_id, r.user_id
    idle = get_settings().agent_worker_idle_close_seconds

    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="held")
        dead = await workers_svc.open_worker(s, org_id=org, actor_id=user, label="dead")
        alive = await workers_svc.open_worker(s, org_id=org, actor_id=user, label="alive")
        lease = await leases_svc.acquire(
            s, org_id=org, actor_id=user, task_id=task.id, worker_id=dead.id, ttl_seconds=60
        )
        lease.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        lease_id, dead_id, alive_id = lease.id, dead.id, alive.id
        await s.execute(
            text(
                "UPDATE agent_workers SET last_seen_at = now() - make_interval(secs => :by) "
                "WHERE id = :w"
            ),
            {"by": idle + 60, "w": dead_id},
        )

    await sweep.run_once()

    async with tenant_session(str(org), str(user)) as s:
        released = await s.get(TaskLease, lease_id)
        dead_row = await s.get(AgentWorker, dead_id)
        alive_row = await s.get(AgentWorker, alive_id)
    assert released is not None and released.released_at is not None
    assert dead_row is not None and dead_row.closed_at is not None
    assert alive_row is not None and alive_row.closed_at is None
