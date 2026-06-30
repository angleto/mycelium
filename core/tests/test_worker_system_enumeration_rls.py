"""Migration 0029: the worker enumerates workspaces via ``admin_session``
(system actor, no ``current_org``). Under FORCE row-level security, as the
non-bypass ``flow_app`` runtime role, those reads returned zero rows in
production -- the reminders / dispatch / calendar / revisions / embedding
sweeps were silent no-ops. The system-actor ``FOR SELECT`` policies open
exactly that enumeration window (``organizations`` + ``memberships``) and
only it.

The isolation assertions are vacuous on a BYPASSRLS dev role (it sees
everything regardless), so they skip with a clear reason rather than pass
falsely; the positive enumeration test always runs.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.organization import Organization
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _make_workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="WS")
    return a.org_id, a.user_id


async def _rls_enforced() -> bool:
    """True when the async runtime role is subject to RLS (the prod shape).
    A BYPASSRLS dev role makes the isolation assertions vacuous."""
    async with admin_session() as s:
        row = (
            await s.execute(text("select rolbypassrls from pg_roles where rolname = current_user"))
        ).scalar()
    return not bool(row)


async def test_system_actor_enumerates_orgs_and_owner() -> None:
    """The exact reads ``reminders._all_workspaces`` / ``_owner_of`` do:
    a system, no-tenant session must see the org and its owner membership."""
    org, user = await _make_workspace()
    async with admin_session() as s:  # actor_kind defaults to "system"
        org_ids = set((await s.execute(select(Organization.id))).scalars().all())
        assert org in org_ids
        owner = (
            (
                await s.execute(
                    select(Membership.user_id).where(
                        Membership.org_id == org, Membership.role == Role.owner
                    )
                )
            )
            .scalars()
            .first()
        )
        assert owner == user


async def test_non_system_session_sees_no_orgs() -> None:
    """The enumeration policy is system-only: a non-system no-tenant session
    still sees nothing (fail-closed), so isolation is not widened."""
    if not await _rls_enforced():
        pytest.skip("async role is BYPASSRLS; RLS policy not exercisable here")
    org, _ = await _make_workspace()
    async with admin_session(actor_kind="human_direct") as s:
        org_ids = set((await s.execute(select(Organization.id))).scalars().all())
        assert org not in org_ids


async def test_system_policy_dormant_once_scoped_to_org() -> None:
    """Once a job narrows to one org (``tenant_session`` sets current_org),
    the system-enum policy goes dormant: another org stays invisible."""
    if not await _rls_enforced():
        pytest.skip("async role is BYPASSRLS; RLS policy not exercisable here")
    org_a, user_a = await _make_workspace()
    org_b, _ = await _make_workspace()
    async with tenant_session(str(org_a), str(user_a), actor_kind="system") as s:
        org_ids = set((await s.execute(select(Organization.id))).scalars().all())
        assert org_a in org_ids
        assert org_b not in org_ids
