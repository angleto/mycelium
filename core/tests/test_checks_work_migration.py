"""Migration 0030: the checking station is flagged where it is known, and
nowhere else.

Run against real rows: the statement the migration executes, on a workspace
whose default workflow has a ``verify`` station and one whose ``verify``
lives in a workflow that is not the default.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from sqlalchemy import select

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import workflow as wf_svc
from mycelium_core.services.auth import signup

_ROOT = Path(__file__).resolve().parents[2]


def _migration() -> ModuleType:
    path = next((_ROOT / "core" / "migrations" / "versions").glob("0030_*.py"))
    spec = importlib.util.spec_from_file_location("m0030", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="BACKFILL",
        )
    return r.org_id, r.user_id


async def _states(org: uuid.UUID, user: uuid.UUID) -> dict[tuple[str, str], bool]:
    async with tenant_session(str(org), str(user)) as s:
        default = (await wf_svc.get_default_workflow(s, org)).id
        rows = (await s.execute(select(WorkflowState))).scalars().all()
        return {
            ("default" if r.workflow_id == default else "other", r.name): r.checks_work
            for r in rows
        }


async def test_the_backfill_flags_verify_in_the_default_workflow_only() -> None:
    with_station, elsewhere = await _org(), await _org()

    org, user = with_station
    async with tenant_session(str(org), str(user)) as s:
        default = await wf_svc.get_default_workflow(s, org)
        current = await wf_svc.get_states(s, default.id)
        await wf_svc.update_workflow(
            s,
            org_id=org,
            actor_id=user,
            workflow_id=default.id,
            name=default.name,
            states=[
                *(
                    wf_svc.StateEdit(
                        id=st.id,
                        name=st.name,
                        ord=st.ord,
                        is_initial=st.is_initial,
                        is_terminal=st.is_terminal,
                    )
                    for st in current
                ),
                wf_svc.StateEdit(name="verify", ord=10),
            ],
            transitions=[("todo", "in_progress"), ("in_progress", "verify"), ("verify", "done")],
        )

    org, user = elsewhere
    async with tenant_session(str(org), str(user)) as s:
        await wf_svc.create_workflow(
            s,
            org_id=org,
            actor_id=user,
            name="review",
            states=[
                wf_svc.StateSpec(name="open", ord=0, is_initial=True),
                wf_svc.StateSpec(name="verify", ord=1),
                wf_svc.StateSpec(name="shut", ord=2, is_terminal=True),
            ],
            transitions=[("open", "verify"), ("verify", "shut")],
        )

    engine = sa.create_engine(get_settings().database_url_sync)
    try:
        for _ in range(2):  # idempotent
            with engine.begin() as conn:
                conn.execute(sa.text(_migration().BACKFILL))
    finally:
        engine.dispose()

    got = await _states(*with_station)
    assert got[("default", "verify")] is True
    assert not any(v for k, v in got.items() if k != ("default", "verify"))
    # A verify state outside the default workflow is not known to be a
    # station: it is left for the person to flag.
    assert not any((await _states(*elsewhere)).values())
