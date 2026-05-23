"""Actor kind propagation through GUC + audit (migration 0083).

The GUC pattern (``app.current_actor_kind`` /
``app.current_actor_subject``) carries the **type of caller** through
the session; ``services/audit.log`` reads it and persists it on
``activity_log``. The 121 call sites of ``audit.log`` need not know
the caller type.

Coverage:

- backward-compat default: ``tenant_session`` without explicit
  ``actor_kind`` records ``human_direct`` (preserves the 218 existing
  tests' behaviour);
- explicit kind: ``tenant_session(..., actor_kind='human_api')`` flows
  to ``ActivityLog.actor_kind``;
- ``with_actor`` shifts the GUC mid-session and restores it on exit
  (the after-audit reverts to the outer kind);
- ``admin_session`` defaults to ``system``;
- ``agent_run`` shift: ``agent_runtime.start_run`` wraps its post-flush
  side effects in ``with_actor('agent_run', run.id)`` so every audit
  emitted during the loop is attributed to the run, not the dispatcher.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import select

from flow_core.db import admin_session, tenant_session, with_actor
from flow_core.models.activity_log import ActivityLog
from flow_core.services import audit, tasks
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


@pytest.fixture
async def _org_and_user() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ACT")
    yield a.org_id, a.user_id


async def _last_log_entry(
    session_factory: tuple[uuid.UUID, uuid.UUID], action: str = "create"
) -> ActivityLog:
    """Read the most recent activity_log entry of an action.

    ``activity_log`` has RLS keyed on ``app.current_org``, so we must
    read through a ``tenant_session`` (admin_session leaves the GUC
    empty -> fail-closed -> zero rows visible). Reading inside a
    tenant session is also a fairer representation of how the SPA /
    REST will query the log later on.
    """
    org, user = session_factory
    async with tenant_session(str(org), str(user)) as s:
        stmt = (
            select(ActivityLog)
            .where(ActivityLog.org_id == org, ActivityLog.action == action)
            .order_by(ActivityLog.ts.desc())
            .limit(1)
        )
        row = (await s.execute(stmt)).scalar_one()
    return row


async def test_default_kind_is_human_direct(
    _org_and_user: tuple[uuid.UUID, uuid.UUID],
) -> None:
    org, user = _org_and_user
    async with tenant_session(str(org), str(user)) as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="t-direct",
            estimate_effort_h=Decimal(1),
        )
    row = await _last_log_entry((org, user))
    assert row.actor_kind == "human_direct"
    assert row.actor_subject_id is None
    assert row.actor_id == user


async def test_explicit_human_api_kind_flows_through(
    _org_and_user: tuple[uuid.UUID, uuid.UUID],
) -> None:
    org, user = _org_and_user
    async with tenant_session(str(org), str(user), actor_kind="human_api") as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="t-api",
            estimate_effort_h=Decimal(1),
        )
    row = await _last_log_entry((org, user))
    assert row.actor_kind == "human_api"
    assert row.actor_subject_id is None


async def test_explicit_human_telegram_kind(
    _org_and_user: tuple[uuid.UUID, uuid.UUID],
) -> None:
    org, user = _org_and_user
    async with tenant_session(str(org), str(user), actor_kind="human_telegram") as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="t-tg",
            estimate_effort_h=Decimal(1),
        )
    row = await _last_log_entry((org, user))
    assert row.actor_kind == "human_telegram"


async def test_mcp_token_kind_carries_subject_id(
    _org_and_user: tuple[uuid.UUID, uuid.UUID],
) -> None:
    org, user = _org_and_user
    fake_token_id = uuid.uuid4()
    async with tenant_session(
        str(org),
        str(user),
        actor_kind="mcp_token",
        actor_subject_id=str(fake_token_id),
    ) as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="t-mcp",
            estimate_effort_h=Decimal(1),
        )
    row = await _last_log_entry((org, user))
    assert row.actor_kind == "mcp_token"
    assert row.actor_subject_id == fake_token_id


async def test_with_actor_shifts_and_restores_kind(
    _org_and_user: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A nested ``with_actor`` block changes the audit kind for the
    duration of the block; subsequent audits in the same session must
    revert to the outer kind. Single-transaction guarantee: no leak
    across sessions either way."""
    org, user = _org_and_user
    async with tenant_session(str(org), str(user), actor_kind="human_api") as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="outer-before",
            estimate_effort_h=Decimal(1),
        )
        subj = uuid.uuid4()
        async with with_actor(s, actor_kind="agent_run", actor_subject_id=str(subj)):
            await audit.log(
                s,
                org_id=org,
                actor_id=user,
                entity="task",
                entity_id=None,
                action="inside_agent",
            )
        await audit.log(
            s,
            org_id=org,
            actor_id=user,
            entity="task",
            entity_id=None,
            action="outer_after",
        )

    inside = await _last_log_entry((org, user), action="inside_agent")
    after = await _last_log_entry((org, user), action="outer_after")
    assert inside.actor_kind == "agent_run"
    assert inside.actor_subject_id == subj
    assert after.actor_kind == "human_api"
    assert after.actor_subject_id is None


async def test_admin_session_default_is_system() -> None:
    """``admin_session`` defaults to ``actor_kind='system'``. We assert
    this by reading the GUC the helper sets: ``activity_log`` RLS
    forbids inserts from a no-tenant session by design (fail-closed
    on missing ``app.current_org``), so the GUC is the right point of
    observation here."""
    from sqlalchemy import text

    async with admin_session() as s:
        kind = (
            await s.execute(text("SELECT current_setting('app.current_actor_kind', true)"))
        ).scalar_one()
        subj = (
            await s.execute(text("SELECT current_setting('app.current_actor_subject', true)"))
        ).scalar_one()
    assert kind == "system"
    assert subj == ""


async def test_admin_session_can_override_actor_kind() -> None:
    """A CLI tool can request a specific kind via ``admin_session(
    actor_kind=...)``. Lets a one-off script attribute its writes to
    a defined caller type rather than the default ``system``."""
    from sqlalchemy import text

    async with admin_session(actor_kind="human_api") as s:
        kind = (
            await s.execute(text("SELECT current_setting('app.current_actor_kind', true)"))
        ).scalar_one()
    assert kind == "human_api"


async def test_explicit_agent_run_through_with_actor_inside_tenant(
    _org_and_user: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Validate the seam ``agent_runtime.start_run`` relies on: a
    nested ``with_actor('agent_run', subject)`` inside a human-opened
    tenant session shifts the audit attribution to the agent run while
    the human-context audits before/after remain attributed to the
    caller. This is what powers the dispatch-then-run flow without
    requiring a second session."""
    org, user = _org_and_user
    run_subject = uuid.uuid4()
    async with tenant_session(str(org), str(user), actor_kind="human_api") as s:
        await audit.log(
            s,
            org_id=org,
            actor_id=user,
            entity="task",
            entity_id=None,
            action="dispatcher_pre",
        )
        async with with_actor(s, actor_kind="agent_run", actor_subject_id=str(run_subject)):
            await audit.log(
                s,
                org_id=org,
                actor_id=user,
                entity="agent_run",
                entity_id=None,
                action="run_step",
            )
            await audit.log(
                s,
                org_id=org,
                actor_id=user,
                entity="agent_run",
                entity_id=None,
                action="run_artifact",
            )
        await audit.log(
            s,
            org_id=org,
            actor_id=user,
            entity="task",
            entity_id=None,
            action="dispatcher_post",
        )

    async with tenant_session(str(org), str(user)) as s2:
        rows = {
            r.action: r
            for r in (
                (
                    await s2.execute(
                        select(ActivityLog)
                        .where(ActivityLog.org_id == org)
                        .order_by(ActivityLog.ts)
                    )
                )
                .scalars()
                .all()
            )
        }
    assert rows["dispatcher_pre"].actor_kind == "human_api"
    assert rows["dispatcher_pre"].actor_subject_id is None
    assert rows["run_step"].actor_kind == "agent_run"
    assert rows["run_step"].actor_subject_id == run_subject
    assert rows["run_artifact"].actor_kind == "agent_run"
    assert rows["dispatcher_post"].actor_kind == "human_api"
