"""Server-side hierarchy invariant: a task that carries a project tag
must also carry the project's client tag.

create_task and attach_tag both enforce it; passing only the project
yields a task whose ``tags`` set includes the client transitively
without the caller having to opt in.

Since docs/adr/0003 the pair is also EXACTLY one of each and the write
goes through the choke point (services/tag_assignment): attaching a
project is a MOVE that evicts the previous pair, a bag holding two
clients or two projects is refused, and neither half can be detached.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="AUTOTAG",
        )
    return r.org_id, r.user_id


async def _two_hierarchies(
    s: AsyncSession, *, org: uuid.UUID, user: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Two independent client -> project chains (c1, p1, c2, p2) in one
    workspace: the minimum shape a MOVE or a contradiction needs."""
    c1 = await taxonomy.create_client(
        s, org_id=org, actor_id=user, name="Cee", profile=ClientInput(legal_name="Cee SRL")
    )
    p1 = await taxonomy.create_project(
        s, org_id=org, actor_id=user, name="Roof", client_tag_id=c1.id
    )
    c2 = await taxonomy.create_client(
        s, org_id=org, actor_id=user, name="Dee", profile=ClientInput(legal_name="Dee SRL")
    )
    p2 = await taxonomy.create_project(
        s, org_id=org, actor_id=user, name="Cellar", client_tag_id=c2.id
    )
    return c1.id, p1.id, c2.id, p2.id


async def test_create_task_auto_attaches_client_tag() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        cli = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Acme",
            profile=ClientInput(legal_name="Acme SRL"),
        )
        proj = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name="Site", client_tag_id=cli.id
        )
        t = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="T1",
            tag_ids=[proj.id],
        )
        rows = list(
            (await s.execute(select(TaskTag.tag_id).where(TaskTag.task_id == t.id))).scalars().all()
        )
        assert proj.id in rows
        assert cli.id in rows, "create_task should auto-attach the project's client tag"


async def test_attach_tag_with_project_pulls_client_tag() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        cli = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Bee",
            profile=ClientInput(legal_name="Bee SRL"),
        )
        proj = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name="Hive", client_tag_id=cli.id
        )
        # Start the task WITHOUT any project tag (falls back to default
        # General/Personal). The default Personal client gets carried
        # along (the invariant already holds for the default chain).
        t = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="T2")
        # Now attach the explicit project. The client must come along.
        await tasks_svc.attach_tag(s, org_id=org, actor_id=user, task_id=t.id, tag_id=proj.id)
        rows = set(
            (await s.execute(select(TaskTag.tag_id).where(TaskTag.task_id == t.id))).scalars().all()
        )
        assert proj.id in rows
        assert cli.id in rows


async def test_attaching_another_project_evicts_the_previous_pair() -> None:
    """Attaching a project is a MOVE, not an addition
    (services/tag_assignment.move_to_project): the previous project AND
    the client it had dragged in both leave, so the task never carries
    two clients."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        c1, p1, c2, p2 = await _two_hierarchies(s, org=org, user=user)
        t = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="T3",
            tag_ids=[p1],
        )
        await tasks_svc.attach_tag(s, org_id=org, actor_id=user, task_id=t.id, tag_id=p2)
        rows = set(
            (await s.execute(select(TaskTag.tag_id).where(TaskTag.task_id == t.id))).scalars().all()
        )
    assert {p2, c2} <= rows
    assert p1 not in rows and c1 not in rows


async def test_create_task_with_two_clients_is_rejected() -> None:
    """Invariant (a): exactly one client. Two client tags in the same
    call is a contradiction the caller stated, so it is refused instead
    of being narrowed to whichever one the query plan returned first."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        c1, _p1, c2, _p2 = await _two_hierarchies(s, org=org, user=user)
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await tasks_svc.create_task(
                s,
                org_id=org,
                actor_id=user,
                title="two-clients",
                tag_ids=[c1, c2],
            )
    assert ei.value.code is MessageCode.TAG_MULTIPLE_CLIENTS


async def test_create_task_with_two_projects_is_rejected() -> None:
    """Invariant (a): exactly one project. Picking one of the two would
    silently decide the task's client as well (the project is truth)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _c1, p1, _c2, p2 = await _two_hierarchies(s, org=org, user=user)
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await tasks_svc.create_task(
                s,
                org_id=org,
                actor_id=user,
                title="two-projects",
                tag_ids=[p1, p2],
            )
    assert ei.value.code is MessageCode.TAG_MULTIPLE_PROJECTS


async def test_detaching_a_structural_tag_from_a_task_is_rejected() -> None:
    """A task has no legal state to detach INTO -- exactly one client
    and exactly one project -- so re-pointing it is a MOVE (attach the
    wanted project), never a detach. Both halves answer with the same
    code."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        c1, p1, _c2, _p2 = await _two_hierarchies(s, org=org, user=user)
        t = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="T4",
            tag_ids=[p1],
        )
        task_id = t.id
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await tasks_svc.detach_tag(s, org_id=org, actor_id=user, task_id=task_id, tag_id=c1)
    assert ei.value.code is MessageCode.TAG_STRUCTURAL_REQUIRED
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await tasks_svc.detach_tag(s, org_id=org, actor_id=user, task_id=task_id, tag_id=p1)
    assert ei.value.code is MessageCode.TAG_STRUCTURAL_REQUIRED
