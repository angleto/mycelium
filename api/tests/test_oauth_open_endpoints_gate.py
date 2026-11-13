"""Dynamic registration and metadata documents are OFF unless attributable.

The two open OAuth strategies act for a caller holding nothing, and are safe
only when that caller's origin can be attributed (trusted proxies) and the
issuer's name cannot be chosen by it (public base URL). The previous gate
switched them off only when the environment was RECOGNISED as production,
and ``MYCELIUM_ENV`` defaults to ``dev`` and is set nowhere: it failed open.

The gate no longer depends on recognising the environment. In ANY
environment both settings are required; the only way round them is an
explicit opt-out whose name says what it costs, and that opt-out is ignored
in a declared production.

The suite's own conftest sets the opt-out (tests run with no proxy chain);
every test here clears or sets the relevant variables itself.

Observed failing before the fix (2026-11-13): with the default settings,
/register answered 201 and the metadata advertised both strategies.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from tests_helpers import random_ula48

from mycelium_api.app import create_app
from mycelium_core.config import get_settings

_KEYS = (
    "MYCELIUM_ENV",
    "MYCELIUM_PUBLIC_BASE_URL",
    "MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES",
    "MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK",
)


@contextlib.contextmanager
def _exactly(**values: str) -> Iterator[None]:
    """These four variables set to exactly ``values``; the rest unset."""
    saved = {key: os.environ.get(key) for key in _KEYS}
    for key in _KEYS:
        os.environ.pop(key, None)
    os.environ.update(values)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        get_settings.cache_clear()


@contextlib.asynccontextmanager
async def _client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(), client=(f"{random_ula48()}::1", 1)),
        base_url="http://backend",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "mcp.example"},
    ) as c:
        yield c


async def _state() -> tuple[bool, bool, int, int]:
    async with _client() as c:
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        reg = await c.post(
            "/oauth/register",
            json={"client_name": "X", "redirect_uris": ["https://h.example/cb"]},
        )
        cimd = await c.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "https://app.example/meta.json",
                "redirect_uri": "https://app.example/cb",
                "code_challenge": "a" * 43,
                "code_challenge_method": "S256",
            },
        )
    return (
        "registration_endpoint" in meta,
        bool(meta.get("client_id_metadata_document_supported")),
        reg.status_code,
        cimd.status_code,
    )


def _off(state: tuple[bool, bool, int, int]) -> None:
    has_reg, has_cimd, reg_status, cimd_status = state
    assert not has_reg and not has_cimd
    assert reg_status == 404
    # Refused as an unknown client id, before any fetch is attempted.
    assert cimd_status == 400


async def test_the_default_settings_keep_them_off() -> None:
    with _exactly():
        _off(await _state())


@pytest.mark.parametrize(
    "partial",
    [
        {"MYCELIUM_PUBLIC_BASE_URL": "https://m.example"},
        {"MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES": '["10.0.0.0/8"]'},
    ],
)
async def test_one_setting_is_not_enough(partial: dict[str, str]) -> None:
    with _exactly(**partial):
        _off(await _state())


async def test_both_settings_turn_them_on() -> None:
    with _exactly(
        MYCELIUM_PUBLIC_BASE_URL="https://m.example",
        MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES='["10.0.0.0/8"]',
    ):
        has_reg, has_cimd, reg_status, _ = await _state()
    assert has_reg and has_cimd
    assert reg_status == 201


async def test_the_opt_out_turns_them_on_outside_production() -> None:
    with _exactly(MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK="true"):
        has_reg, has_cimd, reg_status, _ = await _state()
    assert has_reg and has_cimd
    assert reg_status == 201


async def test_the_opt_out_is_ignored_in_a_declared_production() -> None:
    with _exactly(MYCELIUM_ENV="production", MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK="true"):
        _off(await _state())
