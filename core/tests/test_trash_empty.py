"""Empty-the-recycle-bin hard delete.

Covers ``services.trash.empty_trash``: RBAC (member denied, admin
allowed, owner allowed), only soft-deleted rows are purged (live
ones untouched), and the audit row is written.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from flow_core.db import admin_session, tenant_session
from flow_core.errors import ForbiddenError
from flow_core.models.activity_log import ActivityLog
from flow_core.models.note import Note, NoteKind
from flow_core.models.task import Task
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services import trash
from flow_core.services.auth import signup
from flow_core.services.memberships import add_member


async def _owner_org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="Org",
        )
    return r.org_id, r.user_id


async def _member(org_id: uuid.UUID, owner_id: uuid.UUID, role: str) -> uuid.UUID:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="Other",
        )
    async with admin_session() as s:
        from flow_core.models.user import User as UserModel

        u = (await s.execute(select(UserModel).where(UserModel.id == r.user_id))).scalar_one()
        email = u.email
    async with tenant_session(str(org_id), str(owner_id)) as s:
        await add_member(s, org_id=org_id, actor_id=owner_id, email=email, role=role)
    return r.user_id


async def test_empty_trash_member_denied() -> None:
    org, owner = await _owner_org()
    member = await _member(org, owner, "member")
    with pytest.raises(ForbiddenError):
        async with tenant_session(str(org), str(member)) as s:
            await trash.empty_trash(s, org_id=org, actor_id=member)


async def test_empty_trash_purges_only_soft_deleted() -> None:
    org, owner = await _owner_org()
    async with tenant_session(str(org), str(owner)) as s:
        live = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="Live")
        doomed = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="Doom")
        live_note = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=owner,
            kind=NoteKind.text,
            title="LiveNote",
        )
        doomed_note = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=owner,
            kind=NoteKind.text,
            title="DoomNote",
        )
        await tasks_svc.soft_delete_task(
            s, org_id=org, actor_id=owner, task_id=doomed.id, expected_version=doomed.version
        )
        await notes_svc.soft_delete_note(
            s,
            org_id=org,
            actor_id=owner,
            note_id=doomed_note.id,
            expected_version=doomed_note.version,
        )
    async with tenant_session(str(org), str(owner)) as s:
        counts = await trash.empty_trash(s, org_id=org, actor_id=owner)
    assert counts == {"tasks": 1, "notes": 1}

    async with tenant_session(str(org), str(owner)) as s:
        assert (await s.execute(select(Task).where(Task.id == live.id))).scalar_one_or_none()
        assert (
            await s.execute(select(Task).where(Task.id == doomed.id))
        ).scalar_one_or_none() is None
        assert (await s.execute(select(Note).where(Note.id == live_note.id))).scalar_one_or_none()
        assert (
            await s.execute(select(Note).where(Note.id == doomed_note.id))
        ).scalar_one_or_none() is None

        log_count = (
            await s.execute(
                select(func.count())
                .select_from(ActivityLog)
                .where(
                    ActivityLog.org_id == org,
                    ActivityLog.action == "empty_trash",
                )
            )
        ).scalar_one()
        assert log_count == 1


async def test_empty_trash_admin_allowed() -> None:
    org, owner = await _owner_org()
    admin = await _member(org, owner, "admin")
    async with tenant_session(str(org), str(owner)) as s:
        t = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="X")
        await tasks_svc.soft_delete_task(
            s, org_id=org, actor_id=owner, task_id=t.id, expected_version=t.version
        )
    async with tenant_session(str(org), str(admin)) as s:
        counts = await trash.empty_trash(s, org_id=org, actor_id=admin)
    assert counts["tasks"] == 1


async def test_empty_trash_noop_when_empty() -> None:
    org, owner = await _owner_org()
    async with tenant_session(str(org), str(owner)) as s:
        counts = await trash.empty_trash(s, org_id=org, actor_id=owner)
    assert counts == {"tasks": 0, "notes": 0}
