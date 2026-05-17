"""F1 verification (DB-backed): taxonomy + task service layer."""

from __future__ import annotations

import uuid

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.errors import ConflictError, DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.tag import TagKind
from flow_core.models.task import TaskStatus
from flow_core.services import tasks, taxonomy
from flow_core.services.auth import signup
from flow_core.services.taxonomy import ClientInput


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="Org",
        )
    return r.org_id, r.user_id


async def test_taxonomy_and_duplicate() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        cl = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Acme",
            profile=ClientInput(ragione_sociale="Acme SRL", id_paese="IT"),
        )
        pr = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name="Site", client_tag_id=cl.id
        )
        found = await taxonomy.find_tag_by_name(s, org_id=org, kind=TagKind.project, name="Site")
        assert found.id == pr.id
        await taxonomy.create_tag(s, org_id=org, actor_id=user, kind=TagKind.generic, name="x")
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await taxonomy.create_tag(s, org_id=org, actor_id=user, kind=TagKind.generic, name="x")
    assert ei.value.code is MessageCode.TAG_DUPLICATE


async def test_task_crud_and_isolation() -> None:
    org_a, user_a = await _org()
    org_b, user_b = await _org()
    async with tenant_session(str(org_a), str(user_a)) as s:
        pr = await taxonomy.create_project(s, org_id=org_a, actor_id=user_a, name="P")
        t = await tasks.create_task(
            s,
            org_id=org_a,
            actor_id=user_a,
            title="T1",
            tag_ids=[pr.id],
            assignee_ids=[user_a],
        )
        tid, tv = t.id, t.version
    async with tenant_session(str(org_a), str(user_a)) as s:
        rows = await tasks.list_tasks(s, org_id=org_a, tag_id=pr.id)
        assert [r.id for r in rows] == [tid]
        v2 = await tasks.update_task(
            s,
            org_id=org_a,
            actor_id=user_a,
            task_id=tid,
            expected_version=tv,
            values={"title": "T1b"},
        )
        assert v2 == tv + 1
    with pytest.raises(ConflictError):
        async with tenant_session(str(org_a), str(user_a)) as s:
            await tasks.update_task(
                s,
                org_id=org_a,
                actor_id=user_a,
                task_id=tid,
                expected_version=tv,
                values={"title": "stale"},
            )
    async with tenant_session(str(org_a), str(user_a)) as s:
        v3 = await tasks.set_status(
            s,
            org_id=org_a,
            actor_id=user_a,
            task_id=tid,
            expected_version=v2,
            status=TaskStatus.done,
        )
        c = await tasks.add_comment(s, org_id=org_a, actor_id=user_a, task_id=tid, body="n")
        assert (await tasks.list_comments(s, org_id=org_a, task_id=tid))[0].id == c.id
        v4 = await tasks.soft_delete_task(
            s,
            org_id=org_a,
            actor_id=user_a,
            task_id=tid,
            expected_version=v3,
        )
        assert await tasks.list_tasks(s, org_id=org_a) == []
        await tasks.restore_task(
            s,
            org_id=org_a,
            actor_id=user_a,
            task_id=tid,
            expected_version=v4,
        )
        assert len(await tasks.list_tasks(s, org_id=org_a)) == 1
    async with tenant_session(str(org_b), str(user_b)) as s:
        assert await tasks.list_tasks(s, org_id=org_b) == []
        with pytest.raises(NotFoundError):
            await tasks.get_task(s, org_id=org_b, task_id=tid)
