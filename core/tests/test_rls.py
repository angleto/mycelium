"""F0 verification (DB-backed): RLS isolation, optimistic concurrency,
append-only. Requires a migrated database and the runtime role.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from mycelium_core.concurrency import optimistic_update
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, NotFoundError
from mycelium_core.models.membership import Role
from mycelium_core.models.organization import Organization
from mycelium_core.services import rbac
from mycelium_core.services.auth import signup


async def _signup(name: str) -> tuple[uuid.UUID, uuid.UUID]:
    email = f"{uuid.uuid4().hex[:10]}@example.test"
    async with admin_session() as s:
        r = await signup(s, email=email, password="pw-strong-123", org_name=name)
    return r.org_id, r.user_id


async def test_fail_closed_without_guc() -> None:
    # Migration 0029 opens organizations/memberships/calendar_subscriptions
    # to a SYSTEM session with no current org (worker enumeration) -- but
    # only those tables and only the system actor. Everything else stays
    # fail-closed under RLS without a tenant GUC.
    async with admin_session(actor_kind="human_direct") as s:
        # a non-system no-tenant session sees no orgs at all
        assert (await s.execute(text("SELECT count(*) FROM organizations"))).scalar_one() == 0
    async with admin_session() as s:  # actor_kind defaults to "system"
        # a non-enumeration org-scoped table is still fail-closed for system
        assert (await s.execute(text("SELECT count(*) FROM tasks"))).scalar_one() == 0


async def test_org_isolation() -> None:
    org_a, user_a = await _signup("OrgA")
    org_b, user_b = await _signup("OrgB")
    async with tenant_session(str(org_a), str(user_a)) as s:
        rows = (await s.execute(select(Organization.id))).scalars().all()
        assert rows == [org_a]
    async with tenant_session(str(org_b), str(user_b)) as s:
        rows = (await s.execute(select(Organization.id))).scalars().all()
        assert rows == [org_b]


async def test_memory_project_isolation() -> None:
    org_a, user_a = await _signup("OrgMem")
    org_b, user_b = await _signup("OrgMemB")
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(str(org_a), str(user_a)) as s:
        for proj in (p1, p2):
            await s.execute(
                text("INSERT INTO memory_blobs(org_id, project_id, text) VALUES (:o, :p, 't')"),
                {"o": org_a, "p": proj},
            )
    async with tenant_session(str(org_a), str(user_a), str(p1)) as s:
        assert (await s.execute(text("SELECT count(*) FROM memory_blobs"))).scalar_one() == 1
    async with tenant_session(str(org_a), str(user_a)) as s:
        assert (await s.execute(text("SELECT count(*) FROM memory_blobs"))).scalar_one() == 2
    async with tenant_session(str(org_b), str(user_b)) as s:
        assert (await s.execute(text("SELECT count(*) FROM memory_blobs"))).scalar_one() == 0


async def test_append_only_activity_log() -> None:
    org_a, user_a = await _signup("OrgAudit")
    async with tenant_session(str(org_a), str(user_a)) as s:
        await s.execute(
            text("INSERT INTO activity_log(org_id, entity, action) VALUES (:o, 'x', 'create')"),
            {"o": org_a},
        )
    with pytest.raises(Exception):  # noqa: B017 (driver-specific error)
        async with tenant_session(str(org_a), str(user_a)) as s:
            await s.execute(text("UPDATE activity_log SET action = 'y'"))


async def test_optimistic_concurrency() -> None:
    org_a, user_a = await _signup("OrgOcc")
    async with tenant_session(str(org_a), str(user_a)) as s:
        new_version = await optimistic_update(
            s,
            Organization,
            pk=org_a,
            expected_version=1,
            values={"name": "Renamed"},
        )
    assert new_version == 2
    with pytest.raises(ConflictError):
        async with tenant_session(str(org_a), str(user_a)) as s:
            await optimistic_update(
                s,
                Organization,
                pk=org_a,
                expected_version=1,
                values={"name": "X"},
            )


async def test_rbac_roles() -> None:
    org_a, user_a = await _signup("OrgRbac")
    async with tenant_session(str(org_a), str(user_a)) as s:
        assert await rbac.get_role(s, org_a, user_a) == Role.owner
        await rbac.require_role(s, org_a, user_a, Role.admin)
    with pytest.raises(NotFoundError):
        async with tenant_session(str(org_a), str(user_a)) as s:
            await rbac.get_role(s, org_a, uuid.uuid4())
