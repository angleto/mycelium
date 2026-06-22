"""Comment assignment + "assigned to me" inbox (task 861b360b, annotations
backlog 1f161485 #1).

An annotation can be assigned to a workspace identity (Google-Docs "assign to
@someone"), cleared, and listed as an inbox. Assigning is coordination (member
role), not authorship; an unknown or cross-org identity is refused; the
assignment is optimistic-versioned + audited.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flow_core.db import admin_session, tenant_session  # noqa: E402
from flow_core.errors import DomainError, NotFoundError  # noqa: E402
from flow_core.i18n import MessageCode  # noqa: E402
from flow_core.models.activity_log import ActivityLog  # noqa: E402
from flow_core.models.note import NoteKind  # noqa: E402
from flow_core.services import annotations as svc  # noqa: E402
from flow_core.services import identities as identities_svc  # noqa: E402
from flow_core.services import note_parts as np  # noqa: E402
from flow_core.services import notes as nt  # noqa: E402
from flow_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="ASSIGN")
    return r.org_id, r.user_id


async def _comment_on_a_note(s: object, org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    note = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title="t",
        text="a body to comment on",
    )
    parts = await np.list_parts(s, org_id=org, note_id=note.id)  # type: ignore[arg-type]
    ann = await svc.create_comment(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        doc_kind="note_part",
        doc_id=parts[0].id,
        body="please look at this",
    )
    return ann.id


async def test_assign_by_handle_then_clear() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        ann_id = await _comment_on_a_note(s, org, user)
        handle = await identities_svc.handle_for_user(s, user_id=user)
        assert handle is not None
        my_identity = (await identities_svc.ensure_for_user(s, org_id=org, user_id=user)).id

        v1 = await svc.assign(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=ann_id,
            expected_version=1,
            assignee_handle=handle,
        )
        ann = await svc.get_annotation(s, org_id=org, annotation_id=ann_id)
        assert ann.assigned_to_identity_id == my_identity
        assert ann.version == v1

        # The inbox surfaces it for that identity.
        inbox = await svc.list_assigned(s, org_id=org, assignee_identity_id=my_identity)
        assert ann_id in {a.id for a in inbox}

        # Clear unassigns; the inbox no longer lists it.
        await svc.assign(
            s, org_id=org, actor_id=user, annotation_id=ann_id, expected_version=v1, clear=True
        )
        ann = await svc.get_annotation(s, org_id=org, annotation_id=ann_id)
        assert ann.assigned_to_identity_id is None
        inbox = await svc.list_assigned(s, org_id=org, assignee_identity_id=my_identity)
        assert ann_id not in {a.id for a in inbox}


async def test_assign_by_identity_id_and_audit() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        ann_id = await _comment_on_a_note(s, org, user)
        my_identity = (await identities_svc.ensure_for_user(s, org_id=org, user_id=user)).id
        await svc.assign(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=ann_id,
            expected_version=1,
            assignee_identity_id=my_identity,
        )
        ann = await svc.get_annotation(s, org_id=org, annotation_id=ann_id)
        assert ann.assigned_to_identity_id == my_identity
        log = (
            await s.execute(
                select(ActivityLog).where(
                    ActivityLog.entity == "annotation",
                    ActivityLog.entity_id == ann_id,
                    ActivityLog.action == "assign",
                )
            )
        ).scalar_one()
        assert log is not None


async def test_assign_unknown_handle_is_identity_not_found() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        ann_id = await _comment_on_a_note(s, org, user)
        with pytest.raises(NotFoundError) as e:
            await svc.assign(
                s,
                org_id=org,
                actor_id=user,
                annotation_id=ann_id,
                expected_version=1,
                assignee_handle="nobody-here",
            )
        assert e.value.code == MessageCode.IDENTITY_NOT_FOUND


async def test_assign_cross_org_identity_is_refused() -> None:
    """Security: an identity from another workspace cannot be assigned (the
    direct-id path is org-scoped, not just FK-valid)."""
    org_a, user_a = await _org()
    org_b, user_b = await _org()
    async with tenant_session(str(org_b), str(user_b)) as s:
        foreign_identity = (
            await identities_svc.ensure_for_user(s, org_id=org_b, user_id=user_b)
        ).id
    async with tenant_session(str(org_a), str(user_a)) as s:
        ann_id = await _comment_on_a_note(s, org_a, user_a)
        with pytest.raises(NotFoundError) as e:
            await svc.assign(
                s,
                org_id=org_a,
                actor_id=user_a,
                annotation_id=ann_id,
                expected_version=1,
                assignee_identity_id=foreign_identity,
            )
        assert e.value.code == MessageCode.IDENTITY_NOT_FOUND


async def test_assign_without_target_or_clear_is_rejected() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        ann_id = await _comment_on_a_note(s, org, user)
        with pytest.raises(DomainError):
            await svc.assign(s, org_id=org, actor_id=user, annotation_id=ann_id, expected_version=1)


async def test_list_assigned_excludes_resolved_by_default() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        ann_id = await _comment_on_a_note(s, org, user)
        my_identity = (await identities_svc.ensure_for_user(s, org_id=org, user_id=user)).id
        v = await svc.assign(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=ann_id,
            expected_version=1,
            assignee_identity_id=my_identity,
        )
        # Resolve it: drops out of the actionable inbox, back in with the flag.
        await svc.resolve(s, org_id=org, actor_id=user, annotation_id=ann_id, expected_version=v)
        assert ann_id not in {
            a.id for a in await svc.list_assigned(s, org_id=org, assignee_identity_id=my_identity)
        }
        assert ann_id in {
            a.id
            for a in await svc.list_assigned(
                s, org_id=org, assignee_identity_id=my_identity, include_resolved=True
            )
        }
