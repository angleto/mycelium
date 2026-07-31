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

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.invoice import (
    DocumentType,
    Invoice,
    InvoiceKind,
)
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.models.task import Task
from mycelium_core.services import notes as nt
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
        # A note scoped to the project (migration 0016: the project tag
        # in note_tags is the hard boundary, mirroring task_tags). Built
        # through the service rather than by hand: since migration 0086 a
        # note also needs the project's client (invariant (b)) and only
        # tag_assignment may write that pair.
        n = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            project_id=pr.id,
            title="Note in project",
        )
        note_id = n.id
    async with tenant_session(str(org), str(user)) as s:
        await _archive(s, org_id=org, tag_id=pr.id)
    async with tenant_session(str(org), str(user)) as s:
        await taxonomy.purge_project(s, org_id=org, actor_id=user, tag_id=pr.id)
    async with tenant_session(str(org), str(user)) as s:
        tag_row = (await s.execute(select(Tag).where(Tag.id == pr.id))).scalar_one_or_none()
        assert tag_row is None
        task_row = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
        assert task_row is None
        # By note id, not through note_tags: the junction rows are
        # CASCADE-deleted with the tag, so that subquery would be empty
        # even if the note itself had survived.
        note_row = (await s.execute(select(Note).where(Note.id == note_id))).scalar_one_or_none()
        assert note_row is None


async def test_purge_client_recurses_across_projects() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        cl = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="DoomedCo",
            profile=ClientInput(legal_name="DoomedCo SRL"),
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
            profile=ClientInput(legal_name="HasInvoice SRL"),
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


async def test_purge_client_rehomes_a_client_only_note() -> None:
    """A note carrying ONLY the purged client (no project) sits in no
    project subgraph, so no project purge reaches it: the ``note_tags``
    CASCADE would strip its last client and leave it with none, which
    invariant (b) forbids (docs/adr/0021, migration 0086). It falls back
    to the workspace default client -- the same perimeter a note created
    without a project lands on -- and the purge commits, which is what
    the DEFERRED guards make non-obvious."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        default_client = await taxonomy.ensure_default_client(s, org_id=org, actor_id=user)
        cl = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Leaving",
            profile=ClientInput(legal_name="Leaving SRL"),
        )
        n = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="Client-only note"
        )
        # No project: the client tag IS the whole perimeter of this note.
        await nt.attach_tag(s, org_id=org, actor_id=user, note_id=n.id, tag_id=cl.id)
        note_id = n.id
        await _archive(s, org_id=org, tag_id=cl.id)
    async with tenant_session(str(org), str(user)) as s:
        await taxonomy.purge_client(s, org_id=org, actor_id=user, tag_id=cl.id)
    async with tenant_session(str(org), str(user)) as s:
        assert (await s.execute(select(Tag).where(Tag.id == cl.id))).scalar_one_or_none() is None
        assert (
            await s.execute(select(Note).where(Note.id == note_id))
        ).scalar_one_or_none() is not None
        clients = list(
            (
                await s.execute(
                    select(NoteTag.tag_id)
                    .join(Tag, Tag.id == NoteTag.tag_id)
                    .where(NoteTag.note_id == note_id, Tag.kind == TagKind.client)
                )
            )
            .scalars()
            .all()
        )
        assert clients == [default_client]
