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
