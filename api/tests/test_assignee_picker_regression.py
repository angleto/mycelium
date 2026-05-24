"""Regression coverage for the assignee picker bugs reported on v2.0:

- Self-assigning a task (PATCH /tasks/{id} with the caller's own handle)
  used to raise ``DomainError(DOMAIN_ERROR)`` because the membership
  insert trigger short-circuited on the empty users.handle sentinel
  at signup time, so the user appeared in /actors (sourced from the
  source tables) but had no row in identities (queried by
  update_task → lookup_by_handle).
- AI assistants created via ``create_assistant`` had handle='' and no
  identity row, so the picker never listed them at all.

The fixes mint the handle + materialise the identity at row-creation
time in auth.signup and ai_assistants.create_assistant. This test
exercises both flows end-to-end at the service layer (which is where
the bug lived; the HTTP layer is a thin pass-through).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from flow_core.db import admin_session, tenant_session
from flow_core.models.identity import Identity
from flow_core.services import actors as actors_svc
from flow_core.services import ai_assistants as ai_svc
from flow_core.services import identities as identities_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_signup_creates_user_handle_and_identity_row() -> None:
    """After signup the user has a non-empty handle AND a matching
    identity row in their org. Both are required for the assignee
    picker (sourced from users.handle) and PATCH /tasks
    (resolved via identities.handle) to agree."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        actors = await actors_svc.list_actors(s, org_id=org)
        user_actors = [a for a in actors if a.kind == "user" and a.ref_id == user]
        assert len(user_actors) == 1
        assert user_actors[0].handle != ""
        ident = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == org,
                    Identity.user_id == user,
                )
            )
        ).scalar_one_or_none()
        assert ident is not None, "signup must materialise identity row"
        assert ident.handle == user_actors[0].handle


async def test_self_assign_does_not_raise_domain_error() -> None:
    """A user assigning a task to themselves must succeed. This is the
    user-reported "Domain error" symptom — the picker accepts the click
    but the PATCH used to fail at update_task → lookup_by_handle."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="t")
        task_id, base_v = task.id, task.version
        actors = await actors_svc.list_actors(s, org_id=org)
        self_handle = next(a.handle for a in actors if a.kind == "user" and a.ref_id == user)
        new_v = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=base_v,
            values={"assignee_handle": self_handle},
        )
        assert new_v == base_v + 1
    async with tenant_session(str(org), str(user)) as s:
        reloaded = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        assert reloaded.assignee_id is not None


async def test_create_assistant_appears_in_picker_and_is_assignable() -> None:
    """An AI assistant (the Claude MCP path) must be both visible in the
    picker AND resolvable as a task assignee. Pre-fix both were broken:
    create_assistant left handle='' so list_actors filtered it out, and
    no identity row existed so PATCH /tasks would have raised."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        made = await ai_svc.create_assistant(s, org_id=org, actor_id=user, label="Claude")
        assert made.assistant.handle != ""
        actors = await actors_svc.list_actors(s, org_id=org)
        matches = [a for a in actors if a.kind == "ai_assistant" and a.ref_id == made.assistant.id]
        assert len(matches) == 1
        assistant_handle = matches[0].handle
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="ask claude")
        task_id, base_v = task.id, task.version
        new_v = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=base_v,
            values={"assignee_handle": assistant_handle},
        )
        assert new_v == base_v + 1
    async with tenant_session(str(org), str(user)) as s:
        reloaded = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        ident = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == org,
                    Identity.ai_assistant_id == made.assistant.id,
                )
            )
        ).scalar_one_or_none()
        assert ident is not None
        assert reloaded.assignee_id == ident.id


async def test_lookup_by_handle_self_heals_when_identity_is_missing() -> None:
    """Pre-Stage-A users (and any post-migration drift) can land in a
    state where users.handle is set but the matching identity row was
    never materialised. lookup_by_handle now falls back to the source
    tables and ensures the identity on the spot. Simulates the bug by
    deleting the auto-created identity, then triggers an assign that
    must succeed (re-creating the identity transparently)."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        actors = await actors_svc.list_actors(s, org_id=org)
        self_handle = next(a.handle for a in actors if a.kind == "user" and a.ref_id == user)
        await s.execute(
            delete(Identity).where(
                Identity.org_id == org,
                Identity.user_id == user,
            )
        )
        await s.flush()
        ident = await identities_svc.lookup_by_handle(s, org_id=org, handle=self_handle)
        assert ident is not None
        assert ident.user_id == user
        assert ident.handle == self_handle


async def test_lookup_by_handle_self_heals_when_assistant_identity_is_missing() -> None:
    """Same self-heal path, for ai_assistant identities."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        made = await ai_svc.create_assistant(s, org_id=org, actor_id=user, label="Claude")
        await s.execute(
            delete(Identity).where(
                Identity.org_id == org,
                Identity.ai_assistant_id == made.assistant.id,
            )
        )
        await s.flush()
        ident = await identities_svc.lookup_by_handle(s, org_id=org, handle=made.assistant.handle)
        assert ident is not None
        assert ident.ai_assistant_id == made.assistant.id
        assert ident.handle == made.assistant.handle
