"""F3 MCP co-equality (DB-backed): appointment-tasks (no-ubiquity via
the GiST EXCLUDE on task_participants, migration 0094/0095/0096/0097)
and the deterministic schedule reuse the same service layer as REST
(docs/adr/0001). The legacy ``create_event`` MCP tool was removed in
migration 0097; appointments are created through ``create_task`` with
``start_at`` + ``duration_minutes``."""

from __future__ import annotations

import uuid

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.errors import ConflictError
from flow_core.services import actors as actors_svc
from flow_core.services import identities as identities_svc
from flow_core.services.auth import signup
from flow_mcp.server import (
    create_task,
    list_schedule,
    recompute_schedule,
    set_task_schedule,
)


async def test_mcp_events_and_schedule() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP3",
        )
    token, org, me_user = r.token, str(r.org_id), r.user_id
    # Mint the user's identity so we can assign appointment-tasks.
    async with tenant_session(org, str(me_user)) as s:
        await actors_svc.mint_user_handle(s, user_id=me_user, seed="mcp3")
        ident = await identities_svc.ensure_for_user(s, org_id=r.org_id, user_id=me_user)
    me_ident = str(ident.id)

    await create_task(
        token=token,
        org_id=org,
        title="Standup",
        start_at="2026-01-12T09:00:00+00:00",
        duration_minutes=30,
        assignee_id=me_ident,
    )
    with pytest.raises(ConflictError):
        await create_task(
            token=token,
            org_id=org,
            title="Clash",
            start_at="2026-01-12T09:15:00+00:00",
            duration_minutes=30,
            assignee_id=me_ident,
        )

    t = await create_task(token=token, org_id=org, title="MCP task")
    await set_task_schedule(
        token=token,
        org_id=org,
        task_id=t["id"],
        expected_version=t["version"],
        remaining_effort_h=2.0,
    )
    out = await recompute_schedule(token=token, org_id=org, as_of="2026-01-12T08:00:00+00:00")
    # 1 plain work task + 1 appointment-task = 2 rows scheduled.
    assert out["count"] == 2
    rows = await list_schedule(token=token, org_id=org)
    assert any(r["task_id"] == t["id"] for r in rows)
    plain = next(r for r in rows if r["task_id"] == t["id"])
    assert plain["scheduled_start"] is not None


async def test_mcp_task_participants() -> None:
    """The participants tools on appointment-tasks: add / list /
    remove via MCP, mirror behaviour of the REST endpoints."""
    from flow_mcp.server import (
        add_task_participant,
        list_task_participants,
        remove_task_participant,
    )

    a_email = f"{uuid.uuid4().hex[:10]}@example.test"
    b_email = f"{uuid.uuid4().hex[:10]}@example.test"
    async with admin_session() as s:
        a = await signup(s, email=a_email, password="pw-strong-123", org_name="MCP3p")
        b = await signup(s, email=b_email, password="pw-strong-123", org_name="MCP3pB")
    token, org_uuid, owner = a.token, a.org_id, a.user_id
    org = str(org_uuid)
    # Make user B a member of A's org so we can pin them as a participant.
    from flow_core.services import memberships as mem_svc

    async with tenant_session(org, str(owner)) as s:
        await mem_svc.add_member(
            s, org_id=org_uuid, actor_id=owner, email=b_email, role="member"
        )
        await actors_svc.mint_user_handle(s, user_id=owner, seed=a_email)
        await actors_svc.mint_user_handle(s, user_id=b.user_id, seed=b_email)
        owner_ident = await identities_svc.ensure_for_user(
            s, org_id=org_uuid, user_id=owner
        )
        collab_ident = await identities_svc.ensure_for_user(
            s, org_id=org_uuid, user_id=b.user_id
        )

    # Create appointment-task through MCP.
    appt = await create_task(
        token=token,
        org_id=org,
        title="Pairing",
        start_at="2026-02-09T09:00:00+00:00",
        duration_minutes=60,
        assignee_id=str(owner_ident.id),
    )
    # Add collab as participant.
    added = await add_task_participant(
        token=token,
        org_id=org,
        task_id=appt["id"],
        identity_id=str(collab_ident.id),
    )
    assert added["task_id"] == appt["id"]
    assert added["duration_minutes"] == 60
    # List: assignee mirror + collab.
    rows = await list_task_participants(token=token, org_id=org, task_id=appt["id"])
    ids = {r["identity_id"] for r in rows}
    assert ids == {str(owner_ident.id), str(collab_ident.id)}
    # Remove the collab.
    out = await remove_task_participant(
        token=token,
        org_id=org,
        task_id=appt["id"],
        identity_id=str(collab_ident.id),
    )
    assert out["removed"] is True
    rows2 = await list_task_participants(token=token, org_id=org, task_id=appt["id"])
    assert {r["identity_id"] for r in rows2} == {str(owner_ident.id)}
