"""Hard-delete (purge) for archived clients/projects.

Service-level coverage for ``taxonomy.purge_project`` /
``taxonomy.purge_client``:

- precondition checks (archived only, default-protected),
- the project subgraph (tasks via task_tags, notes by project_id) is
  wiped together with its CASCADE descendants,
- the client purge recurses across its archived projects and refuses
  when invoices reference it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.invoice import (
    DocumentType,
    Invoice,
    InvoiceKind,
)
from flow_core.models.note import Note, NoteKind, NoteStatus
from flow_core.models.tag import Tag
from flow_core.models.task import Task
from flow_core.services import tasks as tasks_svc
from flow_core.services import taxonomy
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


async def _archive(s, *, org_id: uuid.UUID, tag_id: uuid.UUID) -> None:
    tag = (await s.execute(select(Tag).where(Tag.id == tag_id))).scalar_one()
    tag.status = "archived"
    tag.version += 1
    await s.flush()


async def test_purge_project_rejects_non_archived() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        pr = await taxonomy.create_project(s, org_id=org, actor_id=user, name="P1")
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await taxonomy.purge_project(s, org_id=org, actor_id=user, tag_id=pr.id)
    assert ei.value.code is MessageCode.TAG_NOT_ARCHIVED


async def test_purge_project_rejects_default_general() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        general_id = await taxonomy.ensure_default_project(s, org_id=org, actor_id=user)
        await _archive(s, org_id=org, tag_id=general_id)
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await taxonomy.purge_project(s, org_id=org, actor_id=user, tag_id=general_id)
    assert ei.value.code is MessageCode.TAG_DEFAULT_PROTECTED


async def test_purge_project_wipes_subgraph() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        pr = await taxonomy.create_project(s, org_id=org, actor_id=user, name="Wipe")
        t = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Doomed task",
            tag_ids=[pr.id],
        )
        task_id = t.id
        # A note scoped to the project (project_id is a hard boundary,
        # not a FK, so purge enumerates explicitly).
        s.add(
            Note(
                org_id=org,
                project_id=pr.id,
                kind=NoteKind.text,
                status=NoteStatus.captured,
                title="Note in project",
            )
        )
        await s.flush()
    async with tenant_session(str(org), str(user)) as s:
        await _archive(s, org_id=org, tag_id=pr.id)
    async with tenant_session(str(org), str(user)) as s:
        await taxonomy.purge_project(s, org_id=org, actor_id=user, tag_id=pr.id)
    async with tenant_session(str(org), str(user)) as s:
        tag_row = (await s.execute(select(Tag).where(Tag.id == pr.id))).scalar_one_or_none()
        assert tag_row is None
        task_row = (
            await s.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        assert task_row is None
        notes_left = (
            await s.execute(select(Note).where(Note.project_id == pr.id))
        ).scalars().all()
        assert notes_left == []


async def test_purge_client_recurses_across_projects() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        cl = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="DoomedCo",
            profile=ClientInput(ragione_sociale="DoomedCo SRL"),
        )
        p1 = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name="P1", client_tag_id=cl.id
        )
        p2 = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name="P2", client_tag_id=cl.id
        )
        await _archive(s, org_id=org, tag_id=cl.id)
        # purge_client also wipes archived projects under it without
        # requiring them to be individually archived first.
    async with tenant_session(str(org), str(user)) as s:
        await taxonomy.purge_client(s, org_id=org, actor_id=user, tag_id=cl.id)
    async with tenant_session(str(org), str(user)) as s:
        assert (await s.execute(select(Tag).where(Tag.id == cl.id))).scalar_one_or_none() is None
        assert (await s.execute(select(Tag).where(Tag.id == p1.id))).scalar_one_or_none() is None
        assert (await s.execute(select(Tag).where(Tag.id == p2.id))).scalar_one_or_none() is None


async def test_purge_client_blocks_on_invoices() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        cl = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="HasInvoice",
            profile=ClientInput(ragione_sociale="HasInvoice SRL"),
        )
        # Minimal invoice row tied to the client. The block is a row-
        # count test in purge_client, so only client_tag_id matters here.
        inv = Invoice(
            org_id=org,
            client_tag_id=cl.id,
            kind=InvoiceKind.invoice,
            document_type=DocumentType.TD01,
            year=2026,
        )
        s.add(inv)
        await s.flush()
        await _archive(s, org_id=org, tag_id=cl.id)
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await taxonomy.purge_client(s, org_id=org, actor_id=user, tag_id=cl.id)
    assert ei.value.code is MessageCode.CLIENT_HAS_INVOICES
