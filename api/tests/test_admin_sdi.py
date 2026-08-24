"""Admin SdI environment switch: capability+elevation gating and the
test<->production flip (the runtime endpoint switch, no redeploy)."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.bootstrap_admin import ensure_admin
from mycelium_core.db import admin_session
from mycelium_core.errors import DomainError


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_ELEVATE = {"X-Admin-Mode": "1"}
_ADMIN_PW = "Str0ng-Passw0rd!"


async def test_sdi_environment_admin_gated_and_flips() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        normal = (
            await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})
        ).json()
        nt = normal["token"]
        admin_email = _email()
        await ensure_admin(admin_email, _ADMIN_PW)
        at = (
            await c.post("/auth/login", json={"email": admin_email, "password": _ADMIN_PW})
        ).json()["token"]

        # Non-admin is refused even with a forged elevation header; an admin
        # WITHOUT elevation is treated like a normal user.
        assert (await c.get("/admin/sdi-environment", headers=_bearer(nt))).status_code == 403
        assert (
            await c.get("/admin/sdi-environment", headers={**_bearer(nt), **_ELEVATE})
        ).status_code == 403
        assert (await c.get("/admin/sdi-environment", headers=_bearer(at))).status_code == 403

        h = {**_bearer(at), **_ELEVATE}
        got = await c.get("/admin/sdi-environment", headers=h)
        assert got.status_code == 200
        body = got.json()
        assert body["environment"] in ("test", "production")
        assert "active_endpoint" in body and "sdicoop_active" in body
        # The accredited channel is REFLECTED read-only, so an admin can see
        # what the running process holds. The incident this answers: a setting
        # nobody had heard of was demanded by the fail-closed boot check while
        # being read by nothing (ADR-0053).
        assert "intermediary_id_codice" in body and "intermediary_id_paese" in body
        # Certificates are reported as present or missing, NEVER as values:
        # they name files mounted from k8s secrets.
        for key in ("client_cert_configured", "client_key_configured", "ca_bundle_configured"):
            assert isinstance(body[key], bool), key
        assert not any("BEGIN" in str(v) for v in body.values()), "no key material may leak here"

        # Flip to production, read it back, then reset to test (shared singleton).
        put = await c.put("/admin/sdi-environment", headers=h, json={"environment": "production"})
        assert put.status_code == 200 and put.json()["environment"] == "production"
        assert (await c.get("/admin/sdi-environment", headers=h)).json()[
            "environment"
        ] == "production"
        back = await c.put("/admin/sdi-environment", headers=h, json={"environment": "test"})
        assert back.status_code == 200 and back.json()["environment"] == "test"

        # Unknown environment rejected by the Literal schema (422), not silently.
        bad = await c.put("/admin/sdi-environment", headers=h, json={"environment": "staging"})
        assert bad.status_code == 422


async def test_the_transmitter_fiscal_code_is_configurable_from_the_platform() -> None:
    """It lived only in MYCELIUM_SDI_INTERMEDIARY_ID_CODICE, so correcting it
    meant editing a ConfigMap and rolling the deployment -- for a value
    FatturaPA is specific about and that a deployment can hold wrongly for a
    long time without knowing."""
    from mycelium_core.services import system_settings as svc

    async with admin_session() as s:
        # Empty column: the deployment's env value still stands.
        assert await svc.get_sdi_intermediary_override(s) == ""

        await svc.set_sdi_intermediary_id_codice(s, "  ltengl79m31i356x ")
        assert await svc.get_sdi_intermediary_override(s) == "LTENGL79M31I356X"
        # And it now wins over the env value.
        assert await svc.get_sdi_intermediary_id_codice(s) == "LTENGL79M31I356X"

        # Shape is enforced: FatturaPA wants a fiscal code, not free text.
        for bad in ("1234", "NOT A CODE", "1234567890123"):
            with pytest.raises(DomainError):
                await svc.set_sdi_intermediary_id_codice(s, bad)

        # A company's code is 11 digits and legitimate, so it cannot be
        # refused -- but it is the exact shape that is wrong for a physical
        # person, so it comes back as a warning instead of silence.
        await svc.set_sdi_intermediary_id_codice(s, "13438810015")
        assert svc.sdi_intermediary_warning("13438810015") == (
            "physical_person_must_use_16_char_cf"
        )
        assert svc.sdi_intermediary_warning("LTENGL79M31I356X") is None

        # Clearing returns the deployment to its env value.
        await svc.set_sdi_intermediary_id_codice(s, "")
        assert await svc.get_sdi_intermediary_override(s) == ""
