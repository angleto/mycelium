"""Findings of the second independent security review of ADR-0069, over HTTP.

The reviewer's probes (2026-11-13) turned around: each asserts the safe
behaviour, and was observed failing against the tree the review read. Each
client comes from its own random IPv6 /48, the bucket the per-origin bounds
count by, so the file can run any number of times against one database.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from starlette.routing import Mount
from tests_helpers import streamed_response

from mycelium_api.app import create_app
from mycelium_core.config import get_settings
from mycelium_core.oauth import cimd
from mycelium_core.services import oauth_clients as clients_svc
from mycelium_core.services import oauth_consent as consent_svc
from mycelium_mcp.gateway import gateway

CALLBACK = "https://host.example/oauth/callback"


def _ula48() -> str:
    """A random /48 under fd00::/8 (unique local addresses)."""
    a, b, c = secrets.randbelow(0xFF), secrets.randbelow(0xFFFF), secrets.randbelow(0xFFFF)
    return f"fd{a:02x}:{b:x}:{c:x}"


def random_origin() -> str:
    """An address in its own /48."""
    return f"{_ula48()}::1"


@contextlib.contextmanager
def _env(**overrides: str) -> Iterator[None]:
    saved = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
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
async def _client(origin: str | None = None, **headers: str) -> AsyncIterator[AsyncClient]:
    saved = gateway._session_manager
    gateway._session_manager = None
    try:
        app = create_app()
        mount = next(r for r in app.routes if isinstance(r, Mount) and r.path == "/mcp")
        async with mount.app.router.lifespan_context(mount.app):  # type: ignore[attr-defined]
            async with AsyncClient(
                transport=ASGITransport(
                    app=app,
                    client=(origin or random_origin(), 123),
                    raise_app_exceptions=False,
                ),
                base_url="http://backend",
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "mcp.example",
                    **headers,
                },
            ) as c:
                yield c
    finally:
        gateway._session_manager = saved


def _pkce() -> tuple[str, str]:
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


async def _authorize(c: AsyncClient, cid: str, ch: str, redirect: str = CALLBACK) -> Response:
    return await c.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": redirect,
            "code_challenge": ch,
            "code_challenge_method": "S256",
            "state": "st",
        },
    )


async def _person(c: AsyncClient) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={"email": f"{uuid.uuid4().hex[:10]}@example.test", "password": "pw-strong-123"},
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


def _rid(r: Response) -> str:
    assert r.status_code == 302, r.text
    return parse_qs(urlsplit(r.headers["location"]).query)["request"][0]


async def _approve(c: AsyncClient, person: dict[str, str], rid: str) -> str:
    a = await c.post(
        "/oauth/consent/approve",
        headers=person,
        json={"request": rid, "label": "x", "scope": ["tasks:read"]},
    )
    assert a.status_code == 200, a.text
    return parse_qs(urlsplit(a.json()["redirect_url"]).query)["code"][0]


async def _token(c: AsyncClient, cid: str, code: str, verifier: str) -> Response:
    return await c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
        },
    )


# ----- L3: the nesting bomb is a 400, not a 500 ------------------------------------------


async def test_a_nested_document_is_refused_not_a_server_error() -> None:
    cid = "https://bomb.example/m.json"

    async def resolve(h: str, p: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(req: httpx.Request) -> httpx.Response:
        return streamed_response(
            200, content=b"[" * 16000, headers={"content-type": "application/json"}
        )

    prev = cimd.set_shared_fetcher(
        cimd.MetadataFetcher(resolve=resolve, transport=httpx.MockTransport(handler))
    )
    try:
        async with _client() as c:
            _, ch = _pkce()
            r = await _authorize(c, cid, ch, redirect="https://bomb.example/cb")
    finally:
        cimd.set_shared_fetcher(prev)
    assert r.status_code == 400, r.text


# ----- M1a/b: an approved consent is never evicted -------------------------------------------


async def test_an_approved_consent_survives_the_request_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client() as victim:
        reg = (
            await victim.post(
                "/oauth/register", json={"client_name": "V", "redirect_uris": [CALLBACK]}
            )
        ).json()
        person = await _person(victim)
        verifier, ch = _pkce()
        code = await _approve(victim, person, _rid(await _authorize(victim, reg["client_id"], ch)))
    monkeypatch.setattr(consent_svc, "OPEN_GLOBAL_CEILING", 1)
    async with _client() as attacker:
        areg = (
            await attacker.post(
                "/oauth/register", json={"client_name": "A", "redirect_uris": [CALLBACK]}
            )
        ).json()
        _, ach = _pkce()
        assert (await _authorize(attacker, areg["client_id"], ach)).status_code == 302
    async with _client() as victim:
        t = await _token(victim, reg["client_id"], code, verifier)
    assert t.status_code == 200, t.text


async def test_a_client_with_an_approved_consent_survives_the_registration_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client() as victim:
        reg = (
            await victim.post(
                "/oauth/register", json={"client_name": "V", "redirect_uris": [CALLBACK]}
            )
        ).json()
        person = await _person(victim)
        verifier, ch = _pkce()
        code = await _approve(victim, person, _rid(await _authorize(victim, reg["client_id"], ch)))
    monkeypatch.setattr(clients_svc, "REGISTER_GLOBAL_CEILING", 1)
    async with _client() as attacker:
        r = await attacker.post(
            "/oauth/register", json={"client_name": "A", "redirect_uris": [CALLBACK]}
        )
        assert r.status_code == 201, r.text
    async with _client() as victim:
        t = await _token(victim, reg["client_id"], code, verifier)
    assert t.status_code == 200, t.text


# ----- M1c: production refuses to run the open strategies unattributed ----------------------


async def test_production_without_trusted_proxies_has_no_open_registration() -> None:
    with _env(
        MYCELIUM_ENV="production",
        MYCELIUM_PUBLIC_BASE_URL="https://mycelium.example",
        MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES="[]",
    ):
        async with _client(**{"X-Forwarded-For": "203.0.113.9"}) as c:
            meta = (await c.get("/.well-known/oauth-authorization-server")).json()
            r = await c.post(
                "/oauth/register", json={"client_name": "X", "redirect_uris": [CALLBACK]}
            )
    assert "registration_endpoint" not in meta
    assert "client_id_metadata_document_supported" not in meta
    assert r.status_code == 404


# ----- M1d: one /48 is one origin -----------------------------------------------------------


async def test_the_per_origin_bound_counts_an_ipv6_48_as_one_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clients_svc, "REGISTER_LIMIT_PER_ORIGIN", 2)
    prefix = _ula48()
    statuses = []
    for subnet in ("1", "2", "3"):  # three different /64s of one /48
        async with _client(origin=f"{prefix}:{subnet}::1") as c:
            r = await c.post(
                "/oauth/register", json={"client_name": "X", "redirect_uris": [CALLBACK]}
            )
            statuses.append(r.status_code)
    assert statuses == [201, 201, 429]


# ----- L4: provenance, audit, and what the Connections tab shows ---------------------------


async def test_an_oauth_assistant_records_who_holds_it_and_the_answers_are_audited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="mycelium.security")
    async with _client() as c:
        reg = (
            await c.post(
                "/oauth/register", json={"client_name": "Claude", "redirect_uris": [CALLBACK]}
            )
        ).json()
        person = await _person(c)
        verifier, ch = _pkce()
        code = await _approve(c, person, _rid(await _authorize(c, reg["client_id"], ch)))
        assert (await _token(c, reg["client_id"], code, verifier)).status_code == 200
        _, ch2 = _pkce()
        denied = await c.post(
            "/oauth/consent/deny",
            headers=person,
            json={"request": _rid(await _authorize(c, reg["client_id"], ch2))},
        )
        assert denied.status_code == 200
        rows = (await c.get("/ai-assistants", headers=person)).json()
    (row,) = [r for r in rows if r.get("oauth")]
    assert row["oauth"]["client_id"] == reg["client_id"]
    assert row["oauth"]["client_name"] == "Claude"
    assert row["oauth"]["client_registration"] == "dynamic"
    assert row["oauth"]["redirect_target"] == "https://host.example"
    assert row["oauth"]["approved_at"]
    assert row["oauth"]["approved_by_id"]
    assert row["token_expires_at"]
    events = " ".join(r.getMessage() for r in caplog.records)
    assert "oauth.consent_approved" in events
    assert "oauth.consent_denied" in events
