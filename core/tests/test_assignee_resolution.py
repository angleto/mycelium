"""Identity/assignee DX follow-up (task 2d3abdc3, follow-up of 901f0f9f).

Covers the three criteria:

  #2 ``set_task_assignee`` / create / update accept a member's *user* id
     too (auto-resolved 1:1 to their identity), not just an identity id;
  #3 unresolved handle/id raises ``identity.not_found`` with informative
     ``params`` (passed / expected / valid_handles);
  #4 read-back: get_task resolves assignee_handle / owner_handle and the
     collaborator set; list_tasks carries owner_id + a collaborator count.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.identity import Identity
from mycelium_core.models.task import Task
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.server import create_task, get_task, list_tasks, set_task_assignee


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org_user() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="AR")
    return a.org_id, a.user_id


async def _identity_id(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        return (
            await s.execute(
                select(Identity.id).where(Identity.org_id == org, Identity.user_id == user)
            )
        ).scalar_one()


# --- Criterion #2: resolve_assignee unifies identity id + member user id ---


async def test_resolve_assignee_accepts_identity_id() -> None:
    org, user = await _org_user()
    ident = await _identity_id(org, user)
    async with tenant_session(str(org), str(user)) as s:
        assert await identities_svc.resolve_assignee(s, org_id=org, assignee_id=ident) == ident


async def test_resolve_assignee_accepts_member_user_id() -> None:
    org, user = await _org_user()
    ident = await _identity_id(org, user)
    async with tenant_session(str(org), str(user)) as s:
        # The pain point: a valid member user id used to fail with
        # identity.not_found; now it maps 1:1 to the member's identity.
        assert await identities_svc.resolve_assignee(s, org_id=org, assignee_id=user) == ident


async def test_create_task_accepts_assignee_by_user_id() -> None:
    org, user = await _org_user()
    ident = await _identity_id(org, user)
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="by-user-id", assignee_id=user
        )
        reloaded = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
    assert reloaded.assignee_id == ident


async def test_update_task_accepts_assignee_by_user_id() -> None:
    org, user = await _org_user()
    ident = await _identity_id(org, user)
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="upd")
        await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=task.id,
            expected_version=task.version,
            values={"assignee_id": user},
        )
        reloaded = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
    assert reloaded.assignee_id == ident


# --- Criterion #3: informative not-found params ---


async def test_resolve_assignee_unknown_raises_with_params() -> None:
    org, user = await _org_user()
    ghost = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError) as ei:
            await identities_svc.resolve_assignee(s, org_id=org, assignee_id=ghost)
    err = ei.value
    assert err.code == MessageCode.IDENTITY_NOT_FOUND
    assert err.params["passed"] == str(ghost)
    assert "identity id or member user id" in err.params["expected"]
    assert isinstance(err.params["valid_handles"], list)
    assert err.params["valid_handles"]  # the owner's handle is addressable


async def test_update_task_unknown_handle_raises_with_params() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="h")
        with pytest.raises(NotFoundError) as ei:
            await tasks_svc.update_task(
                s,
                org_id=org,
                actor_id=user,
                task_id=task.id,
                expected_version=task.version,
                values={"assignee_handle": "ghost-handle"},
            )
    err = ei.value
    assert err.code == MessageCode.IDENTITY_NOT_FOUND
    assert err.params["passed"] == "ghost-handle"
    assert "valid_handles" in err.params


async def test_get_identity_unknown_raises_with_params() -> None:
    org, user = await _org_user()
    ghost = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError) as ei:
            await identities_svc.get_identity(s, org_id=org, identity_id=ghost)
    assert ei.value.params["passed"] == str(ghost)


# --- Criterion #4: read-back of handles + collaborators ---


async def test_list_collaborators_and_counts() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="collab", assignee_ids=[user]
        )
        collabs = await tasks_svc.list_collaborators(s, org_id=org, task_id=task.id)
        counts = await tasks_svc.collaborator_counts(s, org_id=org, task_ids=[task.id])
    assert [c["user_id"] for c in collabs] == [str(user)]
    assert collabs[0]["handle"]
    assert counts[task.id] == 1


# --- MCP wiring (tools are directly callable, cf. test_f1_mcp) ---


async def test_mcp_set_task_assignee_accepts_user_id() -> None:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="MCP-AR")
    token, org, user = r.token, str(r.org_id), r.user_id
    created = await create_task(token=token, org_id=org, title="assign-by-uid")
    # The user id used to surface identity.not_found; now it resolves.
    res = await set_task_assignee(
        token=token,
        org_id=org,
        task_id=created["id"],
        expected_version=created["version"],
        assignee_id=str(user),
    )
    assert res["cleared"] is False
    full = await get_task(token=token, org_id=org, task_id=created["id"])
    assert full["assignee_handle"]


async def test_mcp_get_task_readback_and_list_fields() -> None:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="MCP-RB")
    token, org, user = r.token, str(r.org_id), r.user_id
    created = await create_task(token=token, org_id=org, title="rb", assignee_ids=[str(user)])
    full = await get_task(token=token, org_id=org, task_id=created["id"])
    assert full["owner_handle"]
    assert any(c["user_id"] == str(user) for c in full["collaborators"])

    listed = next(
        t for t in (await list_tasks(token=token, org_id=org))["items"] if t["id"] == created["id"]
    )
    assert "owner_id" in listed
    assert listed["collaborators_count"] == 1
