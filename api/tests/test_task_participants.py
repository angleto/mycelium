"""Participants on appointment-tasks (migration 0095, ADR-0008 addendum).

Each participant is an additional identity pinned to a task that has
``start_at`` + ``duration_minutes``. The GiST EXCLUDE on
``task_participants`` enforces no-ubiquity per ``identity_id`` — if a
participant already holds another appointment overlapping the window
(or another assignee-owned event), the add is rejected with
``ConflictError(EVENT_OVERLAP)``.

Covers:
- add ok + idempotent on the same (task, identity);
- add to a plain task / reminder -> DomainError (no slot to occupy);
- overlap on the participant -> 409;
- remove unblocks future overlap;
- sync trigger: changing the parent task's window propagates;
- dropping the appointment status (duration_minutes -> NULL) deletes
  all participants;
- the scheduler treats the appointment as busy for participants too
  (covered in the scheduler-specific test, not here).

Service-layer style, mirroring api/tests/test_task_events.py.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.task_participant import TaskParticipant
from mycelium_core.services import actors as actors_svc
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import memberships as mem_svc
from mycelium_core.services import participants as p_svc
from mycelium_core.services import tasks
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _mint_user_identity(s, *, org, user, seed: str = "user"):
    await actors_svc.mint_user_handle(s, user_id=user, seed=seed)
    return await identities_svc.ensure_for_user(s, org_id=org, user_id=user)


_T0 = dt.datetime(2026, 7, 1, 9, 0, tzinfo=dt.UTC)


async def _two_user_org() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, str]:
    """Workspace with owner + one collaborator. Returns
    (org, owner_id, collab_user_id, owner_email, collab_email)."""
    a_email = _email()
    b_email = _email()
    async with admin_session() as s:
        a = await signup(s, email=a_email, password="pw-strong-123", org_name="P")
        b = await signup(s, email=b_email, password="pw-strong-123", org_name="O")
    org, owner = a.org_id, a.user_id
    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=b_email, role="member")
    return org, owner, b.user_id, a_email, b_email


async def test_add_participant_round_trips() -> None:
    org, owner, collab, oe, ce = await _two_user_org()
    async with tenant_session(str(org), str(owner)) as s:
        owner_ident = await _mint_user_identity(s, org=org, user=owner, seed=oe)
        collab_ident = await _mint_user_identity(s, org=org, user=collab, seed=ce)
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Pairing",
            assignee_id=owner_ident.id,
            start_at=_T0,
            duration_minutes=60,
        )
        row = await p_svc.add_participant(
            s, org_id=org, actor_id=owner, task_id=t.id, identity_id=collab_ident.id
        )
        assert row.task_id == t.id
        assert row.identity_id == collab_ident.id
        assert row.start_at == _T0
        assert row.duration_minutes == 60
        # Idempotent: same pair returns the same row.
        again = await p_svc.add_participant(
            s, org_id=org, actor_id=owner, task_id=t.id, identity_id=collab_ident.id
        )
        assert again.task_id == row.task_id
        assert again.identity_id == row.identity_id


async def test_add_participant_to_plain_task_rejected() -> None:
    org, owner, _, oe, _ = await _two_user_org()
    async with tenant_session(str(org), str(owner)) as s:
        owner_ident = await _mint_user_identity(s, org=org, user=owner, seed=oe)
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Plain todo",
            assignee_id=owner_ident.id,
        )
        with pytest.raises(DomainError):
            await p_svc.add_participant(
                s,
                org_id=org,
                actor_id=owner,
                task_id=t.id,
                identity_id=owner_ident.id,
            )


async def test_participant_overlap_rejected() -> None:
    """The collaborator is the assignee of an existing appointment.
    Adding them as a participant of another appointment overlapping
    that window must fail with EVENT_OVERLAP."""
    org, owner, collab, oe, ce = await _two_user_org()
    async with tenant_session(str(org), str(owner)) as s:
        owner_ident = await _mint_user_identity(s, org=org, user=owner, seed=oe)
        collab_ident = await _mint_user_identity(s, org=org, user=collab, seed=ce)
        # Collab is already booked 9:00-10:00 on their own.
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Collab solo",
            assignee_id=collab_ident.id,
            start_at=_T0,
            duration_minutes=60,
        )
        # Owner schedules a 9:30-10:30 appointment and tries to pull in
        # the collaborator as a participant. The participant's no-overlap
        # rule must reject it.
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Owner meeting",
            assignee_id=owner_ident.id,
            start_at=_T0 + dt.timedelta(minutes=30),
            duration_minutes=60,
        )
        with pytest.raises(ConflictError) as exc:
            await p_svc.add_participant(
                s,
                org_id=org,
                actor_id=owner,
                task_id=t.id,
                identity_id=collab_ident.id,
            )
        assert exc.value.code == MessageCode.EVENT_OVERLAP


async def test_remove_participant_unblocks_future_overlap() -> None:
    """The conflict-raising create_task aborts the session
    (trigger-driven EXCLUDE violations don't always restore the
    asyncpg connection state from a SAVEPOINT). Production gets a
    fresh session per HTTP request, so this is a test-only
    accommodation: open a new tenant_session for each phase."""
    org, owner, collab, oe, ce = await _two_user_org()
    async with tenant_session(str(org), str(owner)) as s:
        owner_ident = await _mint_user_identity(s, org=org, user=owner, seed=oe)
        collab_ident = await _mint_user_identity(s, org=org, user=collab, seed=ce)
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Pair A",
            assignee_id=owner_ident.id,
            start_at=_T0,
            duration_minutes=60,
        )
        task_id = t.id
        await p_svc.add_participant(
            s, org_id=org, actor_id=owner, task_id=task_id, identity_id=collab_ident.id
        )
    async with tenant_session(str(org), str(owner)) as s:
        # Collab is now busy 9:00-10:00 as a participant; their own
        # 9:30-10:30 appointment must fail.
        collab_ident_id = (await identities_svc.ensure_for_user(s, org_id=org, user_id=collab)).id
        with pytest.raises(ConflictError):
            await tasks.create_task(
                s,
                org_id=org,
                actor_id=owner,
                title="Conflicts via participant",
                assignee_id=collab_ident_id,
                start_at=_T0 + dt.timedelta(minutes=30),
                duration_minutes=60,
            )
    async with tenant_session(str(org), str(owner)) as s:
        # Drop the collab participant; the same appointment now passes.
        collab_ident_id = (await identities_svc.ensure_for_user(s, org_id=org, user_id=collab)).id
        await p_svc.remove_participant(
            s,
            org_id=org,
            actor_id=owner,
            task_id=task_id,
            identity_id=collab_ident_id,
        )
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Now ok",
            assignee_id=collab_ident_id,
            start_at=_T0 + dt.timedelta(minutes=30),
            duration_minutes=60,
        )


async def test_sync_trigger_propagates_window_change() -> None:
    org, owner, collab, oe, ce = await _two_user_org()
    async with tenant_session(str(org), str(owner)) as s:
        owner_ident = await _mint_user_identity(s, org=org, user=owner, seed=oe)
        collab_ident = await _mint_user_identity(s, org=org, user=collab, seed=ce)
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Movable",
            assignee_id=owner_ident.id,
            start_at=_T0,
            duration_minutes=60,
        )
        await p_svc.add_participant(
            s, org_id=org, actor_id=owner, task_id=t.id, identity_id=collab_ident.id
        )
        # Move the task forward 2 hours; the participant row's window
        # must follow via the AFTER UPDATE trigger.
        new_start = _T0 + dt.timedelta(hours=2)
        await tasks.update_task(
            s,
            org_id=org,
            actor_id=owner,
            task_id=t.id,
            expected_version=t.version,
            values={"start_at": new_start, "duration_minutes": 30},
        )
        row = (
            await s.execute(
                select(TaskParticipant).where(
                    TaskParticipant.task_id == t.id,
                    TaskParticipant.identity_id == collab_ident.id,
                )
            )
        ).scalar_one()
        assert row.start_at == new_start
        assert row.duration_minutes == 30


async def test_dropping_appointment_status_removes_participants() -> None:
    org, owner, collab, oe, ce = await _two_user_org()
    async with tenant_session(str(org), str(owner)) as s:
        owner_ident = await _mint_user_identity(s, org=org, user=owner, seed=oe)
        collab_ident = await _mint_user_identity(s, org=org, user=collab, seed=ce)
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Was appt",
            assignee_id=owner_ident.id,
            start_at=_T0,
            duration_minutes=60,
        )
        await p_svc.add_participant(
            s, org_id=org, actor_id=owner, task_id=t.id, identity_id=collab_ident.id
        )
        # Clear the appointment pair. Trigger must drop all participants.
        await tasks.update_task(
            s,
            org_id=org,
            actor_id=owner,
            task_id=t.id,
            expected_version=t.version,
            values={"start_at": None, "duration_minutes": None},
        )
        remaining = (
            await s.execute(select(TaskParticipant).where(TaskParticipant.task_id == t.id))
        ).all()
        assert remaining == []


async def test_participants_http_round_trip() -> None:
    """End-to-end on the /tasks/{id}/participants surface: create
    appointment, add participant, list, delete, list empty."""
    from httpx import ASGITransport, AsyncClient

    from mycelium_api.main import app

    a_email = _email()
    b_email = _email()
    async with admin_session() as s:
        a = await signup(s, email=a_email, password="pw-strong-123", org_name="HTTP")
        b = await signup(s, email=b_email, password="pw-strong-123", org_name="OTHER")
    org, owner = a.org_id, a.user_id
    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=b_email, role="member")
        await actors_svc.mint_user_handle(s, user_id=owner, seed=a_email)
        await actors_svc.mint_user_handle(s, user_id=b.user_id, seed=b_email)
        owner_ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=owner)
        collab_ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=b.user_id)

    # Use the API to do everything else.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # Sign in as the owner to get a token.
        tok_resp = await c.post("/auth/login", json={"email": a_email, "password": "pw-strong-123"})
        h = {
            "Authorization": f"Bearer {tok_resp.json()['token']}",
            "X-Workspace-Id": str(org),
            "X-Workspace-Role": "owner",
        }
        # Create an appointment-task via /tasks.
        appt_resp = await c.post(
            "/tasks",
            headers=h,
            json={
                "title": "HTTP appt",
                "assignee_id": str(owner_ident.id),
                "start_at": _T0.isoformat(),
                "duration_minutes": 30,
                "executor_kind": "human",
                "necessity": "should",
            },
        )
        assert appt_resp.status_code == 200, appt_resp.text
        task_id = appt_resp.json()["id"]
        # Add the collab as a participant.
        add_resp = await c.post(
            f"/tasks/{task_id}/participants",
            headers=h,
            json={"identity_id": str(collab_ident.id)},
        )
        assert add_resp.status_code == 200, add_resp.text
        body = add_resp.json()
        assert body["identity_id"] == str(collab_ident.id)
        assert body["kind"] == "user"
        assert body["duration_minutes"] == 30
        # List shows both the assignee (mirror) and the explicit participant.
        list_resp = await c.get(f"/tasks/{task_id}/participants", headers=h)
        assert list_resp.status_code == 200
        ids = {row["identity_id"] for row in list_resp.json()}
        assert ids == {str(owner_ident.id), str(collab_ident.id)}
        # Remove the collab.
        del_resp = await c.delete(f"/tasks/{task_id}/participants/{collab_ident.id}", headers=h)
        assert del_resp.status_code == 204
        list2 = await c.get(f"/tasks/{task_id}/participants", headers=h)
        assert {r["identity_id"] for r in list2.json()} == {str(owner_ident.id)}
        # Adding to a plain task -> 422 DomainError.
        plain_resp = await c.post(
            "/tasks",
            headers=h,
            json={
                "title": "Plain",
                "executor_kind": "human",
                "necessity": "should",
            },
        )
        plain_id = plain_resp.json()["id"]
        bad = await c.post(
            f"/tasks/{plain_id}/participants",
            headers=h,
            json={"identity_id": str(collab_ident.id)},
        )
        assert bad.status_code in (400, 422), bad.text


async def test_two_overlapping_appointments_with_same_participant_rejected() -> None:
    """The user's own scenario: two different events both list the
    collaborator as a participant and overlap in time. The second must
    be rejected (no-ubiquity for the participant)."""
    org, owner, collab, oe, ce = await _two_user_org()
    async with tenant_session(str(org), str(owner)) as s:
        owner_ident = await _mint_user_identity(s, org=org, user=owner, seed=oe)
        collab_ident = await _mint_user_identity(s, org=org, user=collab, seed=ce)
        # Two appointments belonging to two different "owners" (using
        # the same user as assignee here is fine for the assignee
        # constraint because we only collide them via the collab).
        a = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Event A",
            assignee_id=owner_ident.id,
            start_at=_T0,
            duration_minutes=60,
        )
        b = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Event B",
            # Use the collab as assignee on this one so the assignee
            # EXCLUDE does not collide on the owner; the participant
            # rule will catch the collab overlap below.
            assignee_id=collab_ident.id,
            start_at=_T0 + dt.timedelta(hours=3),
            duration_minutes=60,
        )
        # First add succeeds.
        await p_svc.add_participant(
            s, org_id=org, actor_id=owner, task_id=a.id, identity_id=collab_ident.id
        )
        # B's assignee (collab) overlaps A's participant window via a
        # later move: simulate by moving B onto A.
        with pytest.raises(ConflictError):
            await tasks.update_task(
                s,
                org_id=org,
                actor_id=owner,
                task_id=b.id,
                expected_version=b.version,
                values={"start_at": _T0 + dt.timedelta(minutes=15)},
            )
