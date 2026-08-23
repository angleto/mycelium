"""Global system settings: the runtime SdI environment switch."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import system_settings as ss
from mycelium_core.services.auth import signup


async def test_sdi_environment_flips_and_persists() -> None:
    # The singleton seeds 'test' (model + migration server_default: a fresh
    # deploy never sends a real invoice by accident). It is global shared state
    # across the suite, so set explicitly rather than asserting the default.
    async with admin_session() as s:
        await ss.set_sdi_environment(s, "test")
        assert await ss.get_sdi_environment(s) == "test"
        row = await ss.set_sdi_environment(s, "production")
        assert row.sdi_environment == "production"
    async with admin_session() as s:
        assert await ss.get_sdi_environment(s) == "production"
        await ss.set_sdi_environment(s, "test")  # reset: good citizen for shared state
    async with admin_session() as s:
        assert await ss.get_sdi_environment(s) == "test"


async def test_set_sdi_environment_rejects_unknown() -> None:
    from mycelium_core.errors import DomainError

    async with admin_session() as s:
        raised = False
        try:
            await ss.set_sdi_environment(s, "staging")
        except DomainError:
            raised = True
        assert raised


def test_endpoint_for_selects_per_environment_with_legacy_fallback() -> None:
    class _S:
        sdi_endpoint_url = "https://legacy"
        sdi_endpoint_url_test = "https://testservizi.example/RiceviFile"
        sdi_endpoint_url_prod = "https://servizi.example/RiceviFile"

    with patch("mycelium_core.services.system_settings.get_settings", return_value=_S()):
        assert ss.endpoint_for("test") == "https://testservizi.example/RiceviFile"
        assert ss.endpoint_for("production") == "https://servizi.example/RiceviFile"

    class _LegacyOnly:
        sdi_endpoint_url = "https://legacy-only"
        sdi_endpoint_url_test = ""
        sdi_endpoint_url_prod = ""

    with patch("mycelium_core.services.system_settings.get_settings", return_value=_LegacyOnly()):
        # Falls back to the single legacy URL when the env-specific one is unset.
        assert ss.endpoint_for("test") == "https://legacy-only"
        assert ss.endpoint_for("production") == "https://legacy-only"


async def test_sdi_environment_readable_from_a_tenant_session() -> None:
    # transmit() reads the switch on a tenant (RLS) session; system_settings is
    # a non-RLS global table granted to the app role, so the read must work.
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="T",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as ts:
        assert await ss.get_sdi_environment(ts) in ("test", "production")


async def test_the_singleton_self_heals_under_concurrency() -> None:
    """The fallback has to work when it is actually needed: concurrently.

    ``_get_or_create`` used to be a plain check-then-INSERT. Migration
    0074 seeded the row, so single-threaded callers never noticed --
    until the 2026-08-22 squash dropped that seed and the first
    concurrent readers of a fresh database all found nothing, all tried
    to insert, and the ``id IS TRUE`` primary key let exactly one
    through. Four of five invoice transmissions died on
    UniqueViolationError; that is what turned CI red on v2.2.19.

    Migration 0003 puts the row back, so this deletes it first: the
    point is that the FALLBACK is safe, independently of the seed. A
    fallback that only works single-threaded turns a missing row into a
    hard failure instead of self-healing.
    """
    import asyncio

    from sqlalchemy import delete, select

    from mycelium_core.models.system_settings import SystemSettings

    async with admin_session() as s:
        await s.execute(delete(SystemSettings))

    # A barrier, because without one the five coroutines serialise: each
    # is short enough to commit before the next acquires a connection, so
    # only the first ever sees an empty table and the old check-then-INSERT
    # passes. Holding all five inside an open transaction until every one
    # has entered reproduces what five concurrent invoice transmissions
    # did on a fresh CI database.
    started = asyncio.Barrier(5)

    async def _read() -> str:
        async with admin_session() as s:
            # Force connection acquisition + transaction start BEFORE the
            # barrier, so releasing it puts all five at the SELECT.
            await s.execute(select(SystemSettings))
            await started.wait()
            return await ss.get_sdi_environment(s)

    results = await asyncio.gather(*[_read() for _ in range(5)])
    assert results == ["test"] * 5

    async with admin_session() as s:
        rows = (await s.execute(select(SystemSettings))).scalars().all()
        assert len(rows) == 1, "still a singleton, whoever won the race"
        # Leave the shared row as the rest of the suite expects it.
        await ss.set_sdi_environment(s, "test")
