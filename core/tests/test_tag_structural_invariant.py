"""Database-level guards for the structural tag invariant
(migration 0086, docs/adr/0003 + docs/adr/0021).

``services/tag_assignment`` is the primary enforcement; these tests are
about what happens when a caller goes AROUND it, and about the
operations the guards must NOT break. Two properties are asserted that
no service-level test can see:

- the constraint triggers are DEFERRABLE INITIALLY DEFERRED, so an
  illegal junction row is accepted by the INSERT and only rejected at
  COMMIT. That is not an accident: ``set_structural`` deliberately
  passes through an intermediate state while it swaps the pair, and an
  entity is only ever required to be consistent at the end of the
  transaction.
- ``purge_project`` / ``purge_client`` / ``delete_organization`` still
  commit. They delete the parent rows the guards check, and the guards
  fire AFTER the CASCADEs: the "parent is gone" early return, plus the
  ``ON DELETE NO ACTION DEFERRABLE`` (never RESTRICT) on
  ``project_profile.client_tag_id``, are what keeps workspace teardown
  working.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.tag import Tag
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.services import auth, taxonomy
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="STRUCT",
        )
    return r.org_id, r.user_id


async def _client_and_project(
    s: AsyncSession, *, org: uuid.UUID, user: uuid.UUID, label: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """One client -> project chain named after ``label`` (tag names are
    unique per org+kind, so every chain in a test needs its own)."""
    client = await taxonomy.create_client(
        s,
        org_id=org,
        actor_id=user,
        name=label,
        profile=ClientInput(legal_name=f"{label} SRL"),
    )
    project = await taxonomy.create_project(
        s, org_id=org, actor_id=user, name=f"{label}-proj", client_tag_id=client.id
    )
    return client.id, project.id


async def _archive(s: AsyncSession, *, tag_id: uuid.UUID) -> None:
    tag = (await s.execute(select(Tag).where(Tag.id == tag_id))).scalar_one()
    tag.status = "archived"
    tag.version += 1
    await s.flush()


async def _task_tags(s: AsyncSession, task_id: uuid.UUID) -> set[uuid.UUID]:
    rows = (await s.execute(select(TaskTag.tag_id).where(TaskTag.task_id == task_id))).scalars()
    return set(rows.all())


async def _note_tags(s: AsyncSession, note_id: uuid.UUID) -> set[uuid.UUID]:
    rows = (await s.execute(select(NoteTag.tag_id).where(NoteTag.note_id == note_id))).scalars()
    return set(rows.all())


async def test_second_client_tag_on_a_task_is_rejected_at_commit() -> None:
    """Invariant (a) at the DB level, and DEFERRED: the illegal INSERT
    must reach the table (the flush passes) and die at COMMIT."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        other = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Intruder",
            profile=ClientInput(legal_name="Intruder SRL"),
        )
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="two-clients")
        task_id, other_id = task.id, other.id
    flushed = False
    with pytest.raises(IntegrityError):
        async with tenant_session(str(org), str(user)) as s:
            s.add(TaskTag(org_id=org, task_id=task_id, tag_id=other_id))
            await s.flush()
            flushed = True
    assert flushed, "trg_task_tags_structural fired at flush; it must be DEFERRED"


async def test_second_client_tag_on_a_note_is_rejected_at_commit() -> None:
    """Invariant (b) at the DB level: AT MOST one project, but never
    more than one client -- and, like the task guard, only at COMMIT."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        other = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Intruder",
            profile=ClientInput(legal_name="Intruder SRL"),
        )
        note = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text)
        note_id, other_id = note.id, other.id
    flushed = False
    with pytest.raises(IntegrityError):
        async with tenant_session(str(org), str(user)) as s:
            s.add(NoteTag(org_id=org, note_id=note_id, tag_id=other_id))
            await s.flush()
            flushed = True
    assert flushed, "trg_note_tags_structural fired at flush; it must be DEFERRED"


async def test_task_client_must_be_the_owner_of_its_project() -> None:
    """Invariant (c): swapping the client for another one keeps the
    COUNTS legal (one client, one project) and is still rejected --
    counting rows is not enough, the guard also joins through
    ``project_profile``."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        c1, p1 = await _client_and_project(s, org=org, user=user, label="Owner")
        c2, _p2 = await _client_and_project(s, org=org, user=user, label="Stranger")
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="incoherent", tag_ids=[p1]
        )
        task_id = task.id
    with pytest.raises(IntegrityError):
        async with tenant_session(str(org), str(user)) as s:
            await s.execute(delete(TaskTag).where(TaskTag.task_id == task_id, TaskTag.tag_id == c1))
            s.add(TaskTag(org_id=org, task_id=task_id, tag_id=c2))
            await s.flush()
            assert await _task_tags(s, task_id) == {p1, c2}


async def test_project_profile_client_tag_id_rejects_null() -> None:
    """Invariant (d): since migration 0086 the pointer is NOT NULL (it
    used to be nullable with ``ON DELETE SET NULL``), so every
    project -> client lookup is total and ``_client_of_project`` can
    treat a missing row as broken taxonomy rather than a normal case.
    Written in SQL because the mapped column refuses ``None`` in
    Python -- the point is that the DB refuses it too."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _client, project = await _client_and_project(s, org=org, user=user, label="Solid")
    with pytest.raises(IntegrityError):
        async with tenant_session(str(org), str(user)) as s:
            await s.execute(
                text("UPDATE project_profile SET client_tag_id = NULL WHERE tag_id = :p"),
                {"p": str(project)},
            )


async def test_reassign_project_client_retags_dependents() -> None:
    """Invariant (c) is a property of the subgraph, not of the profile
    row: moving a project under another client must drag every task and
    note carrying it in the SAME transaction, or the dependents keep the
    previous client and the commit-time guard rejects the move."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        c1, project = await _client_and_project(s, org=org, user=user, label="Before")
        c2, _p2 = await _client_and_project(s, org=org, user=user, label="After")
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="dependent", tag_ids=[project]
        )
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, project_id=project
        )
        task_id, note_id = task.id, note.id
    async with tenant_session(str(org), str(user)) as s:
        await taxonomy.reassign_project_client(
            s,
            org_id=org,
            actor_id=user,
            project_tag_id=project,
            new_client_tag_id=c2,
        )
    async with tenant_session(str(org), str(user)) as s:
        task_tags = await _task_tags(s, task_id)
        note_tags = await _note_tags(s, note_id)
    assert {project, c2} <= task_tags and c1 not in task_tags
    assert {project, c2} <= note_tags and c1 not in note_tags


async def test_purge_project_commits_with_the_guards_installed() -> None:
    """The subgraph wipe deletes the tasks and notes the guards check,
    and the guards only fire at COMMIT: without their "parent is gone"
    early return this transaction would be rejected instead of landing."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _client, project = await _client_and_project(s, org=org, user=user, label="Doomed")
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="doomed task", tag_ids=[project]
        )
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, project_id=project
        )
        task_id, note_id = task.id, note.id
        await _archive(s, tag_id=project)
    async with tenant_session(str(org), str(user)) as s:
        await taxonomy.purge_project(s, org_id=org, actor_id=user, tag_id=project)
    # A fresh transaction: the rows are gone because the purge COMMITTED,
    # which is the whole assertion here.
    async with tenant_session(str(org), str(user)) as s:
        assert (await s.execute(select(Tag).where(Tag.id == project))).scalar_one_or_none() is None
        assert await _task_tags(s, task_id) == set()
        assert await _note_tags(s, note_id) == set()


async def test_purge_client_commits_with_the_guards_installed() -> None:
    """Same for the client purge, which additionally deletes the client
    tag that ``project_profile.client_tag_id`` points at: the FK is
    ``NO ACTION DEFERRABLE``, so it is checked at COMMIT, after the
    project rows have gone. RESTRICT would reject this."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client, project = await _client_and_project(s, org=org, user=user, label="Gone")
        await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="gone task", tag_ids=[project]
        )
        await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, project_id=project)
        await _archive(s, tag_id=client)
    async with tenant_session(str(org), str(user)) as s:
        await taxonomy.purge_client(s, org_id=org, actor_id=user, tag_id=client)
    async with tenant_session(str(org), str(user)) as s:
        assert (await s.execute(select(Tag).where(Tag.id == client))).scalar_one_or_none() is None
        assert (await s.execute(select(Tag).where(Tag.id == project))).scalar_one_or_none() is None


async def test_delete_organization_commits_with_the_guards_installed() -> None:
    """Workspace teardown is a single ``DELETE FROM organizations`` that
    relies on CASCADE reaching ``tags`` and ``project_profile``. The
    guards are SECURITY INVOKER and run under the caller's RLS (pinned to
    the SURVIVING org here), so they see nothing of the deleted
    workspace; the deferred FK is satisfied by then as well."""
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="Keep",
        )
    user, keep = r.user_id, r.org_id
    # signup gives the user one org; a second one makes the delete legal
    # (auth refuses to remove a user's sole workspace).
    async with tenant_session(str(keep), str(user)) as s:
        doomed = await auth.create_org_for_user(s, user_id=user, name="Doomed")
    async with tenant_session(str(doomed), str(user)) as s:
        _client, project = await _client_and_project(s, org=doomed, user=user, label="Tenant")
        await tasks_svc.create_task(
            s, org_id=doomed, actor_id=user, title="tenant task", tag_ids=[project]
        )
        await nt.create_note(
            s, org_id=doomed, actor_id=user, kind=NoteKind.text, project_id=project
        )
    async with tenant_session(str(keep), str(user)) as s:
        await auth.delete_org_for_user(s, user_id=user, org_id=doomed)
    async with tenant_session(str(keep), str(user)) as s:
        orgs = await auth.list_user_orgs(s, user_id=user)
    assert [o.id for o in orgs] == [keep]
