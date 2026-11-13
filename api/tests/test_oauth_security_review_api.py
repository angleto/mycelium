"""Findings of the independent security review of ADR-0069, over HTTP.

The reviewer's demonstrations turned around: each test asserts the safe
behaviour and was observed failing against the tree the review read
(2026-11-13) before the fix it guards. The unit-level findings are in
``core/tests/test_oauth_security_review.py``.

Every client here comes from its own random IPv6 /64, the bucket the
per-origin bounds count by, so the file can run any number of times against
one database without one run's registrations spending the next run's budget.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from starlette.routing import Mount
from tests_helpers import random_ula48

from mycelium_api.app import create_app
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, get_engine
from mycelium_core.models.oauth_client import OAuthClient
from mycelium_core.oauth import cimd
from mycelium_core.services import oauth_clients as clients_svc
from mycelium_core.services import oauth_consent as consent_svc
from mycelium_mcp.gateway import gateway

CALLBACK = "https://host.example/oauth/callback"


def random_origin() -> str:
    """An address in its own /64 of the documentation prefix."""
    return f"{random_ula48()}::1"


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
                transport=ASGITransport(app=app, client=(origin or random_origin(), 123)),
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
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _signup(c: AsyncClient) -> dict[str, str]:
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


async def _register(c: AsyncClient) -> Response:
    return await c.post("/oauth/register", json={"client_name": "X", "redirect_uris": [CALLBACK]})


async def _authorize(c: AsyncClient, client_id: str, challenge: str, **extra: str) -> Response:
    return await c.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st",
            **extra,
        },
    )


def _rid(resp: Response) -> str:
    assert resp.status_code == 302, resp.text
    return parse_qs(urlsplit(resp.headers["location"]).query)["request"][0]


async def _approved_code(
    c: AsyncClient, person: dict[str, str], client_id: str, challenge: str, **extra: str
) -> str:
    rid = _rid(await _authorize(c, client_id, challenge, **extra))
    r = await c.post(
        "/oauth/consent/approve",
        headers=person,
        json={"request": rid, "label": "x", "scope": ["tasks:read"]},
    )
    assert r.status_code == 200, r.text
    return parse_qs(urlsplit(r.json()["redirect_url"]).query)["code"][0]


@pytest.fixture
def fake_cimd_network() -> Iterator[list[str]]:
    fetched: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(404)

    previous = cimd.set_shared_fetcher(
        cimd.MetadataFetcher(resolve=resolve, transport=httpx.MockTransport(handler))
    )
    try:
        yield fetched
    finally:
        cimd.set_shared_fetcher(previous)


# ----- 2. the fetch holds no database connection ----------------------------------


async def test_a_metadata_document_fetch_holds_no_database_connection() -> None:
    """Reviewer: twelve slow ids held twelve pooled connections, so fifteen
    exhaust the pool. The fetch now runs before any session is opened, and
    the excess over the concurrency cap is refused at once."""
    gate = asyncio.Event()
    inside: list[int] = []
    measured: list[int] = []

    async def slow(host: str, port: int) -> list[str]:
        inside.append(1)
        await gate.wait()
        return ["93.184.216.34"]

    previous = cimd.set_shared_fetcher(
        cimd.MetadataFetcher(resolve=slow, transport=httpx.MockTransport(lambda r: Response(404)))
    )
    try:
        async with _client() as c:
            _, challenge = _pkce()

            async def one(i: int) -> int:
                r = await _authorize(c, f"https://slow.example/{uuid.uuid4().hex}", challenge)
                return r.status_code

            tasks = [asyncio.create_task(one(i)) for i in range(12)]
            while len(inside) < cimd.MAX_CONCURRENT_FETCHES:  # noqa: ASYNC110 (polls a fake)
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.5)  # the refused ones finish
            measured.append(get_engine().pool.checkedout())  # type: ignore[attr-defined]
            gate.set()
            codes = await asyncio.gather(*tasks)
    finally:
        cimd.set_shared_fetcher(previous)
    assert measured == [0], f"connections held while fetching: {measured}"
    assert codes.count(400) == 12


# ----- 3. the fetch is metered --------------------------------------------------------


async def test_metadata_document_fetches_are_metered_per_origin(
    fake_cimd_network: list[str],
) -> None:
    async with _client() as c:
        _, challenge = _pkce()
        for i in range(20):
            r = await _authorize(c, f"https://victim.example/x{i}", challenge)
            assert r.status_code in (400, 429)
    assert len(fake_cimd_network) == cimd.FETCHES_PER_ORIGIN


async def test_a_non_https_port_is_never_fetched(fake_cimd_network: list[str]) -> None:
    async with _client() as c:
        _, challenge = _pkce()
        r = await _authorize(c, "https://victim.example:6379/x", challenge)
        assert r.status_code == 400
    assert fake_cimd_network == []


# ----- 5. ceilings evict, they do not lock out ------------------------------------------


async def test_the_registration_ceiling_evicts_the_oldest_unused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with admin_session() as s:
        unused = (
            await s.execute(
                select(func.count())
                .select_from(OAuthClient)
                .where(OAuthClient.registration == "dynamic", OAuthClient.first_grant_at.is_(None))
            )
        ).scalar_one()
    monkeypatch.setattr(clients_svc, "REGISTER_GLOBAL_CEILING", unused + 1)
    async with _client() as c:
        first = await _register(c)
        assert first.status_code == 201, first.text
    # A newcomer from anywhere is accepted; the oldest unused row makes room.
    async with _client() as c:
        second = await _register(c)
        assert second.status_code == 201, second.text
    async with admin_session() as s:
        after = (
            await s.execute(
                select(func.count())
                .select_from(OAuthClient)
                .where(OAuthClient.registration == "dynamic", OAuthClient.first_grant_at.is_(None))
            )
        ).scalar_one()
    assert after <= unused + 1


async def test_the_open_request_ceiling_evicts_the_oldest_unanswered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a ceiling of one, every new request makes room by evicting all
    older unanswered ones -- here including this test's first -- and is
    itself accepted. Nobody is refused for somebody else's requests."""
    async with _client() as c:
        reg = (await _register(c)).json()
        _, challenge = _pkce()
        first = _rid(await _authorize(c, reg["client_id"], challenge))
        monkeypatch.setattr(consent_svc, "OPEN_GLOBAL_CEILING", 1)
        second = await _authorize(c, reg["client_id"], challenge)
        assert second.status_code == 302, second.text
        person = await _signup(c)
        gone = await c.get("/oauth/consent", headers=person, params={"request": first})
        assert gone.status_code == 404
        kept = await c.get("/oauth/consent", headers=person, params={"request": _rid(second)})
        assert kept.status_code == 200, kept.text


async def test_the_per_origin_bound_counts_an_ipv6_64_as_one_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clients_svc, "REGISTER_LIMIT_PER_ORIGIN", 2)
    prefix = f"{random_ula48()}"
    for host in ("::1", "::2"):
        async with _client(origin=f"{prefix}{host}") as c:
            assert (await _register(c)).status_code == 201
    async with _client(origin=f"{prefix}::3") as c:
        assert (await _register(c)).status_code == 429
    async with _client() as c:  # another /64
        assert (await _register(c)).status_code == 201


# ----- 6. client-controlled issuer --------------------------------------------------------


async def test_a_forwarded_host_cannot_inject_into_the_challenge() -> None:
    async with _client(**{"X-Forwarded-Host": 'evil.example", error="injected'}) as c:
        r = await c.post("/mcp", json={})
        assert r.status_code == 401
        assert "injected" not in r.headers["www-authenticate"]
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert "evil" not in meta["issuer"]


async def test_the_configured_base_url_wins_over_forwarded_headers() -> None:
    with _env(MYCELIUM_PUBLIC_BASE_URL="https://mycelium.example"):
        async with _client(**{"X-Forwarded-Host": "evil.example"}) as c:
            meta = (await c.get("/.well-known/oauth-authorization-server")).json()
            r = await c.post("/mcp", json={})
    assert meta["issuer"] == "https://mycelium.example"
    assert "https://mycelium.example/.well-known/" in r.headers["www-authenticate"]


# ----- 8. strategy flags, resource binding, verifier ------------------------------------


async def test_approve_refuses_a_client_whose_strategy_was_switched_off() -> None:
    async with _client() as c:
        person = await _signup(c)
        reg = (await _register(c)).json()
        _, challenge = _pkce()
        rid = _rid(await _authorize(c, reg["client_id"], challenge))
    with _env(MYCELIUM_OAUTH_REGISTERED_CLIENTS_ENABLED="false"):
        async with _client() as c:
            shown = await c.get("/oauth/consent", headers=person, params={"request": rid})
            assert shown.status_code == 404, shown.text
            r = await c.post(
                "/oauth/consent/approve",
                headers=person,
                json={"request": rid, "label": "x", "scope": ["tasks:read"]},
            )
            assert r.status_code == 404, r.text


async def test_hand_registered_clients_follow_the_registered_strategy_flag() -> None:
    with _env(MYCELIUM_OAUTH_REGISTERED_CLIENTS_ENABLED="false"):
        async with _client() as c:
            person = await _signup(c)
            listed = await c.get("/oauth/clients", headers=person)
            created = await c.post(
                "/oauth/clients",
                headers=person,
                json={"client_name": "X", "redirect_uris": [CALLBACK]},
            )
    assert listed.status_code == 404, listed.text
    assert created.status_code == 404, created.text


async def test_the_token_resource_must_be_the_authorized_one() -> None:
    """A resource this server would accept is still refused if it is not the
    one the person authorized: here the code was issued for the MCP endpoint
    as reached at one host, and is redeemed naming it at another."""
    async with _client() as c:
        person = await _signup(c)
        reg = (await _register(c)).json()
        verifier, challenge = _pkce()
        code = await _approved_code(
            c, person, reg["client_id"], challenge, resource="https://mcp.example/mcp"
        )
    async with _client(**{"X-Forwarded-Host": "other.example"}) as c:
        r = await c.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": reg["client_id"],
                "redirect_uri": CALLBACK,
                "code_verifier": verifier,
                "resource": "https://other.example/mcp",
            },
        )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_target"


async def test_a_verifier_outside_rfc7636_is_refused() -> None:
    short = "a" * 42
    challenge = base64.urlsafe_b64encode(hashlib.sha256(short.encode()).digest())
    challenge_s = challenge.rstrip(b"=").decode()
    async with _client() as c:
        person = await _signup(c)
        reg = (await _register(c)).json()
        code = await _approved_code(c, person, reg["client_id"], challenge_s)
        r = await c.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": reg["client_id"],
                "redirect_uri": CALLBACK,
                "code_verifier": short,
            },
        )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"
