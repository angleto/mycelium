"""Server-side hierarchy invariant: a task that carries a project tag
must also carry the project's client tag.

create_task and attach_tag both enforce it; passing only the project
yields a task whose ``tags`` set includes the client transitively
without the caller having to opt in.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
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
