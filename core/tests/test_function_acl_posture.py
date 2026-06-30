"""Regression guard for the function-execute posture (ADR-0015).

Production grants ``mycelium_app`` only what is explicitly granted: the
default PUBLIC execute on our functions is revoked. The test suite
reproduces that via the session fixture ``_reproduce_prod_function_acls``
(conftest) running ``deploy/local/harden_function_acls.sql``. Without it a
function the app calls directly but never `GRANT`ed would pass tests and
500 in prod -- the /advisory/what-now -> ``tasks_event_end`` incident
(migration 0059).

Two assertions:
  1. ``mycelium_app`` CAN execute ``tasks_event_end`` (the 0059 fix, and the
     general contract that every directly-called function is granted).
  2. The posture is ACTUALLY reproduced here: PUBLIC execute is revoked, so
     the app role canNOT directly execute an internal trigger function it
     never calls. This fails loudly if the harden step is skipped, so a
     silent skip cannot reopen the gap unnoticed.
"""

from __future__ import annotations

from sqlalchemy import text

from mycelium_core.db import admin_session


async def _app_can_execute(proname: str) -> bool:
    """Whether ``mycelium_app`` has EXECUTE on the public function ``proname``
    (asserted unique by signature for the names used here)."""
    async with admin_session() as s:
        return (
            await s.execute(
                text(
                    "SELECT has_function_privilege('mycelium_app', p.oid, 'EXECUTE') "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.proname = :name"
                ),
                {"name": proname},
            )
        ).scalar_one()


async def test_app_role_can_execute_tasks_event_end() -> None:
    """``advisory._user_busy`` calls ``tasks_event_end`` directly; the app
    role must be able to execute it (migration 0059). Under the reproduced
    prod posture this is true only because of the explicit grant."""
    assert await _app_can_execute("tasks_event_end") is True


async def test_prod_function_acl_posture_is_reproduced() -> None:
    """Meta-guard: the test DB must reproduce prod's execute posture, else a
    missing grant on a directly-called function would pass silently. An
    internal trigger function (never called directly by the app role) must
    NOT be executable by ``mycelium_app`` -- i.e. PUBLIC execute is revoked."""
    assert await _app_can_execute("forbid_mutation") is False, (
        "harden_function_acls.sql did not run: PUBLIC still has EXECUTE on our "
        "functions, so the test DB no longer reproduces production and a missing "
        "GRANT EXECUTE ... TO mycelium_app would pass tests but 500 in prod"
    )
