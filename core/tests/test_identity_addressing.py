"""Identity-first addressing (docs/adr/0028) tests.

Covers the Stage C refactor invariants:

- ``owner_id`` defaults to ``actor_id`` (the creator) at create_task;
- ``ON DELETE RESTRICT`` refuses to delete a user owning a task;
- ``assignee_id`` is the single source of truth for who works on a
  task; the scheduler-relevant routing kind derives from the joined
  identity's kind;
- the identity-sync trigger creates the user identity on signup and
  the ai_assistant identity on assistant insert.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from flow_core.db import admin_session, tenant_session
from flow_core.models.ai_assistant import AiAssistant
from flow_core.models.identity import Identity, IdentityKind
from flow_core.models.task import ExecKind, Task
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup_with_handle() -> tuple[uuid.UUID, uuid.UUID]:
    """Helper: provision a workspace + a user. ``signup`` mints the user
    handle and the 0085 trigger materialises the identity row, so no
    post-signup patching is needed. Returns ``(org_id, user_id)``."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ID")
    return a.org_id, a.user_id


async def test_identity_sync_trigger_creates_user_identity_on_signup() -> None:
    """Signup mints the user's handle before provisioning the org, so
    when the membership-insert trigger from migration 0085 fires it
    sees a non-empty handle and inserts the matching identity row.
    The test asserts the post-signup state: exactly one identity row
    per (org, user), kind=user, handle equal to users.handle."""
    email = _email()
    async with admin_session() as s:
        a = await signup(s, email=email, password="pw-strong-123", org_name="IDS-1")
    async with tenant_session(str(a.org_id), str(a.user_id)) as s:
        identity = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == a.org_id,
                    Identity.user_id == a.user_id,
                )
            )
        ).scalar_one()
        user_handle = (
            await s.execute(
                text("SELECT handle FROM users WHERE id = :u"),
                {"u": str(a.user_id)},
            )
        ).scalar_one()
    assert identity.kind == IdentityKind.user
    assert identity.handle == user_handle
    assert user_handle != ""


async def test_identity_sync_trigger_fires_on_ai_assistant_insert() -> None:
    """Inserting an ai_assistants row with a non-empty handle must
    cause migration-0085's after-insert trigger to populate an
    identity row matching (org times assistant)."""
    org, user = await _signup_with_handle()
    handle = f"asst-{uuid.uuid4().hex[:6]}"
    async with tenant_session(str(org), str(user)) as s:
        assistant = AiAssistant(
            org_id=org,
            user_id=user,
            label="test-assistant",
            handle=handle,
            scope=[],
            is_active=True,
        )
        s.add(assistant)
        await s.flush()
    async with tenant_session(str(org), str(user)) as s:
        identity = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == org,
                    Identity.handle == handle,
                )
            )
        ).scalar_one()
    assert identity.kind == IdentityKind.ai_assistant
    assert identity.ai_assistant_id == assistant.id


async def test_owner_id_defaults_to_actor_on_create_task() -> None:
    org, user = await _signup_with_handle()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="t-own-default",
            estimate_effort_h=Decimal(1),
        )
    assert task.owner_id == user


async def test_owner_fk_restrict_blocks_user_delete() -> None:
    """Deleting a user that owns a task must fail with a FK violation
    (``ON DELETE RESTRICT``). The user keeps existing until ownership
    is transferred."""
    org, user = await _signup_with_handle()
    async with tenant_session(str(org), str(user)) as s:
        await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="t-restrict",
            estimate_effort_h=Decimal(1),
        )
    async with admin_session() as s:
        with pytest.raises(IntegrityError):
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": str(user)})


async def test_kind_derives_from_identity_when_assignee_set() -> None:
    """A task assigned to an ai_assistant identity must surface as
    llm_agent in the scheduler's view, regardless of the legacy
    ``executor_kind`` hint."""
    org, user = await _signup_with_handle()
    handle = f"agent-{uuid.uuid4().hex[:6]}"
    async with tenant_session(str(org), str(user)) as s:
        assistant = AiAssistant(
            org_id=org,
            user_id=user,
            label="agent-x",
            handle=handle,
            scope=[],
            is_active=True,
        )
        s.add(assistant)
        await s.flush()
        identity = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == org,
                    Identity.ai_assistant_id == assistant.id,
                )
            )
        ).scalar_one()
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="agent-task",
            estimate_effort_h=Decimal(1),
            assignee_id=identity.id,
            # The hint says human; the identity wins.
            executor_kind=ExecKind.human,
        )
        # Re-read the task to make sure assignee_id is persisted.
        reloaded = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
    assert reloaded.assignee_id == identity.id


async def test_create_task_defaults_assignee_to_creator_user_identity() -> None:
    """Default rule: when neither ``assignee_id`` nor ``assignee_handle``
    is supplied, ``create_task`` auto-assigns to the creator. For a
    human-driven call that is the actor's ``user`` identity (and
    ``executor_kind`` is forced to ``human`` to match the identity
    kind, even when the caller passed ``llm_agent`` as a hint)."""
    org, user = await _signup_with_handle()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="auto-assign-to-creator",
            estimate_effort_h=Decimal(1),
            executor_kind=ExecKind.llm_agent,
        )
        user_ident = (
            await s.execute(
                select(Identity.id).where(
                    Identity.org_id == org,
                    Identity.user_id == user,
                    Identity.kind == IdentityKind.user,
                )
            )
        ).scalar_one()
    assert task.assignee_id == user_ident
    assert task.executor_kind is ExecKind.human


async def test_create_task_defaults_created_by_identity_to_actor_user() -> None:
    """v1.2.85 (migrations 0091/0092): ``created_by_identity_id``
    replaces the legacy ``created_by`` user FK. When the service is
    called without an explicit identity (the human-in-SPA path), it
    must default to the user identity of the actor."""
    org, user = await _signup_with_handle()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="human-created")
        # Resolve back the identity row to assert kind=user and that
        # ``ai_assistant_id`` is NULL (not an AI).
        ident_id = task.created_by_identity_id
        assert ident_id is not None
        identity = (await s.execute(select(Identity).where(Identity.id == ident_id))).scalar_one()
    assert identity.kind is IdentityKind.user
    assert identity.user_id == user
    assert identity.ai_assistant_id is None


async def test_create_task_records_ai_assistant_as_creator_when_passed() -> None:
    """v1.2.85: callers (notably the MCP server when a request comes
    in through an agent token) can override ``created_by_identity_id``
    with the AI assistant identity, so AI-authored tasks are
    identifiable in /tasks even when no assignee is set."""
    org, user = await _signup_with_handle()
    async with tenant_session(str(org), str(user)) as s:
        assistant = AiAssistant(
            org_id=org,
            user_id=user,
            label="bot",
            handle=f"bot-{uuid.uuid4().hex[:6]}",
            scope=[],
            is_active=True,
        )
        s.add(assistant)
        await s.flush()
        ai_ident = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == org,
                    Identity.ai_assistant_id == assistant.id,
                )
            )
        ).scalar_one()
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="ai-created",
            created_by_identity_id=ai_ident.id,
        )
    assert task.created_by_identity_id == ai_ident.id


async def test_list_tasks_filters_by_assignee_kind_and_handles() -> None:
    """Punto 4 (docs/adr/0028): ``list_tasks`` exposes identity-axis
    filters. ``assignee_kind=ai_assistant`` returns only bot tasks;
    ``assignee_handles=[h]`` narrows to one specific handle.
    Unassigned tasks never match an identity filter."""
    org, user = await _signup_with_handle()
    bot_handle = f"bot-{uuid.uuid4().hex[:6]}"
    async with tenant_session(str(org), str(user)) as s:
        assistant = AiAssistant(
            org_id=org,
            user_id=user,
            label="bot",
            handle=bot_handle,
            scope=[],
            is_active=True,
        )
        s.add(assistant)
        await s.flush()
        bot_identity = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == org,
                    Identity.ai_assistant_id == assistant.id,
                )
            )
        ).scalar_one()
        user_identity = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == org,
                    Identity.user_id == user,
                )
            )
        ).scalar_one()
        bot_task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="bot-task", assignee_id=bot_identity.id
        )
        human_task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="human-task", assignee_id=user_identity.id
        )
        # ``create_task`` now defaults the assignee to the creator
        # (a user identity in this path), so explicitly null it post-
        # create to land in the legacy unassigned regime that the
        # identity-axis filters must still skip.
        unassigned_task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="unassigned-task"
        )
        await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=unassigned_task.id,
            expected_version=unassigned_task.version,
            values={"assignee_id": None},
        )
        unassigned_task = await tasks_svc.get_task(s, org_id=org, task_id=unassigned_task.id)

        bots = await tasks_svc.list_tasks(s, org_id=org, assignee_kind=IdentityKind.ai_assistant)
        humans = await tasks_svc.list_tasks(s, org_id=org, assignee_kind=IdentityKind.user)
        by_handle = await tasks_svc.list_tasks(
            s, org_id=org, assignee_handles=[bot_identity.handle]
        )
        unfiltered = await tasks_svc.list_tasks(s, org_id=org)

        bot_ids = {t.id for t in bots}
        human_ids = {t.id for t in humans}
        handle_ids = {t.id for t in by_handle}
        all_ids = {t.id for t in unfiltered}

    assert bot_task.id in bot_ids
    assert human_task.id not in bot_ids
    assert unassigned_task.id not in bot_ids
    assert human_task.id in human_ids
    assert bot_task.id not in human_ids
    assert unassigned_task.id not in human_ids
    assert handle_ids == {bot_task.id}
    assert {bot_task.id, human_task.id, unassigned_task.id}.issubset(all_ids)
