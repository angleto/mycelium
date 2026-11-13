"""Client strategies over HTTP: configuration, CIMD, native redirects, issuer.

The second half of ADR-0069. The unit tests pin each layer alone; this file
pins that the endpoints really are driven by them:

- switching a strategy off in configuration removes it from the discovery
  document AND from resolution, in the same request cycle;
- a Client ID Metadata Document client runs the whole consent flow, and the
  token exchange does no network I/O (the document is read at /authorize);
- a private-use redirect scheme works only once allowlisted, and the
  consent page shows its scheme and host;
- the token response is built by whatever credential issuer is installed,
  so replacing it (short-lived tokens, refresh) needs no endpoint change.

Observed failing before the refactor (2026-11-13): the metadata ignored the
settings, an https client id was answered "unknown client_id", the
private-use scheme was refused even when configured, and the installed issuer
was never called.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from starlette.routing import Mount
from tests_helpers import random_ula48, streamed_response

from mycelium_api.app import create_app
from mycelium_core.config import get_settings
from mycelium_core.oauth import cimd, issuance
from mycelium_mcp.gateway import gateway

ISSUER = "https://mcp.example"
CALLBACK = "https://host.example/oauth/callback"
CIMD_ID = "https://app.example/oauth/client.json"
CIMD_CALLBACK = "https://app.example/cb"


@contextlib.contextmanager
def _env(**overrides: str) -> Iterator[None]:
    """Settings as the deployment would set them, restored unconditionally."""
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


def random_origin() -> str:
    return f"{random_ula48()}::1"


@contextlib.asynccontextmanager
async def _client() -> AsyncIterator[AsyncClient]:
    """The real app with its MCP mount running; see test_oauth_public_clients
    for why the SDK's session-manager slot is swapped and restored."""
    saved = gateway._session_manager
    gateway._session_manager = None
    try:
        app = create_app()
        mount = next(r for r in app.routes if isinstance(r, Mount) and r.path == "/mcp")
        async with mount.app.router.lifespan_context(mount.app):  # type: ignore[attr-defined]
            async with AsyncClient(
                # Its own IPv6 /64 per client, the bucket the per-origin
                # bounds count by, so repeated runs on one database never
                # spend each other's budget.
                transport=ASGITransport(app=app, client=(random_origin(), 123)),
                base_url="http://backend",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "mcp.example"},
            ) as c:
                yield c
    finally:
        gateway._session_manager = saved


class _FakeNet:
    """The network a CIMD fetch sees: one public name, one document."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.requests: list[httpx.Request] = []

    async def resolve(self, host: str, port: int) -> list[str]:
        return ["93.184.216.34"] if host == "app.example" else []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return streamed_response(
                200,
                content=json.dumps(self.doc).encode(),
                headers={"Content-Type": "application/json"},
            )

        return httpx.MockTransport(handler)


@pytest.fixture
def fake_net() -> Iterator[_FakeNet]:
    net = _FakeNet(
        {
            "client_id": CIMD_ID,
            "client_name": "Example App",
            "redirect_uris": [CIMD_CALLBACK],
            "token_endpoint_auth_method": "none",
        }
    )
    previous = cimd.set_shared_fetcher(
        cimd.MetadataFetcher(resolve=net.resolve, transport=net.transport())
    )
    try:
        yield net
    finally:
        cimd.set_shared_fetcher(previous)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _signup(c: AsyncClient) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": "Studio",
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _authorize(
    c: AsyncClient, client_id: str, challenge: str, redirect_uri: str, **extra: str
) -> Response:
    return await c.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st",
            **extra,
        },
    )


def _request_id(resp: Response) -> str:
    assert resp.status_code == 302, resp.text
    loc = urlsplit(resp.headers["location"])
    assert loc.path == "/oauth/consent", resp.headers["location"]
    return parse_qs(loc.query)["request"][0]


async def _approve(c: AsyncClient, person: dict[str, str], rid: str) -> str:
    r = await c.post(
        "/oauth/consent/approve",
        headers=person,
        json={"request": rid, "label": "x", "scope": ["tasks:read"]},
    )
    assert r.status_code == 200, r.text
    return parse_qs(urlsplit(r.json()["redirect_url"]).query)["code"][0]


# ----- configuration ---------------------------------------------------------


async def test_switching_off_registration_removes_it_everywhere() -> None:
    with _env(MYCELIUM_OAUTH_DYNAMIC_REGISTRATION_ENABLED="false"):
        async with _client() as c:
            meta = (await c.get("/.well-known/oauth-authorization-server")).json()
            assert "registration_endpoint" not in meta
            r = await c.post(
                "/oauth/register", json={"client_name": "X", "redirect_uris": [CALLBACK]}
            )
            assert r.status_code == 404, r.text


async def test_switching_off_metadata_documents_removes_them_everywhere(
    fake_net: _FakeNet,
) -> None:
    with _env(MYCELIUM_OAUTH_CLIENT_METADATA_DOCUMENTS_ENABLED="false"):
        async with _client() as c:
            meta = (await c.get("/.well-known/oauth-authorization-server")).json()
            assert "client_id_metadata_document_supported" not in meta
            _, challenge = _pkce()
            r = await _authorize(c, CIMD_ID, challenge, CIMD_CALLBACK)
            assert r.status_code == 400, r.text
    assert fake_net.requests == [], "a disabled strategy must not fetch anything"


async def test_switching_off_assistant_secrets_removes_them_everywhere() -> None:
    with _env(MYCELIUM_OAUTH_ASSISTANT_SECRET_CLIENTS_ENABLED="false"):
        async with _client() as c:
            meta = (await c.get("/.well-known/oauth-authorization-server")).json()
            assert "client_secret_post" not in meta["token_endpoint_auth_methods_supported"]
            _, challenge = _pkce()
            r = await _authorize(c, str(uuid.uuid4()), challenge, CALLBACK)
            assert r.status_code == 400, r.text


# ----- CIMD --------------------------------------------------------------------


async def test_a_metadata_document_client_runs_the_whole_flow(fake_net: _FakeNet) -> None:
    async with _client() as c:
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert meta["client_id_metadata_document_supported"] is True

        person = await _signup(c)
        verifier, challenge = _pkce()
        rid = _request_id(await _authorize(c, CIMD_ID, challenge, CIMD_CALLBACK))
        assert len(fake_net.requests) == 1

        shown = (await c.get("/oauth/consent", headers=person, params={"request": rid})).json()
        assert shown["client_id"] == CIMD_ID
        assert shown["client_name"] == "Example App"
        assert shown["registration"] == "metadata_document"
        assert shown["redirect_target"] == "https://app.example"

        code = await _approve(c, person, rid)
        tok = await c.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": CIMD_ID,
                "redirect_uri": CIMD_CALLBACK,
                "code_verifier": verifier,
            },
        )
        assert tok.status_code == 200, tok.text
        assert tok.json()["access_token"].startswith("mycelium_at_")
        # The consent page and the exchange read the stored request, never
        # the network: one fetch, at /authorize, for the whole flow.
        assert len(fake_net.requests) == 1


async def test_a_metadata_document_client_cannot_use_an_unlisted_redirect(
    fake_net: _FakeNet,
) -> None:
    async with _client() as c:
        _, challenge = _pkce()
        r = await _authorize(c, CIMD_ID, challenge, "https://app.example/other")
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "invalid_request"


async def test_a_document_naming_another_client_is_refused(fake_net: _FakeNet) -> None:
    fake_net.doc = dict(fake_net.doc, client_id="https://app.example/oauth/other.json")
    async with _client() as c:
        _, challenge = _pkce()
        r = await _authorize(c, CIMD_ID, challenge, CIMD_CALLBACK)
        assert r.status_code == 400, r.text


# ----- native redirects ------------------------------------------------------------


async def test_a_private_use_scheme_needs_the_allowlist() -> None:
    native = "cursor://anysphere.cursor-mcp/oauth/callback"
    async with _client() as c:
        refused = await c.post(
            "/oauth/register", json={"client_name": "Cursor", "redirect_uris": [native]}
        )
        assert refused.status_code == 400, refused.text
    with _env(MYCELIUM_OAUTH_PRIVATE_USE_REDIRECT_SCHEMES='["cursor"]'):
        async with _client() as c:
            person = await _signup(c)
            reg = await c.post(
                "/oauth/register", json={"client_name": "Cursor", "redirect_uris": [native]}
            )
            assert reg.status_code == 201, reg.text
            _, challenge = _pkce()
            rid = _request_id(await _authorize(c, reg.json()["client_id"], challenge, native))
            shown = (await c.get("/oauth/consent", headers=person, params={"request": rid})).json()
            assert shown["redirect_target"] == "cursor://anysphere.cursor-mcp"
            js = await c.post(
                "/oauth/register",
                json={"client_name": "x", "redirect_uris": ["javascript:alert(1)"]},
            )
            assert js.status_code == 400


# ----- the credential issuer -------------------------------------------------------


class _ShortLivedIssuer:
    """What a future issuer looks like: short-lived token plus refresh."""

    def __init__(self) -> None:
        self.grants: list[object] = []

    async def issue(self, session: Any, grant: object) -> issuance.TokenResponse:
        self.grants.append(grant)
        return issuance.TokenResponse(
            access_token="short-lived",
            expires_in=900,
            scope=("tasks:read",),
            refresh_token="refresh-me",
        )


async def test_the_token_response_is_built_by_the_installed_issuer() -> None:
    fake = _ShortLivedIssuer()
    previous = issuance.set_credential_issuer(fake)
    try:
        async with _client() as c:
            person = await _signup(c)
            reg = (
                await c.post(
                    "/oauth/register", json={"client_name": "X", "redirect_uris": [CALLBACK]}
                )
            ).json()
            verifier, challenge = _pkce()
            rid = _request_id(await _authorize(c, reg["client_id"], challenge, CALLBACK))
            code = await _approve(c, person, rid)
            tok = await c.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": reg["client_id"],
                    "redirect_uri": CALLBACK,
                    "code_verifier": verifier,
                },
            )
    finally:
        issuance.set_credential_issuer(previous)
    assert tok.status_code == 200, tok.text
    assert tok.json() == {
        "access_token": "short-lived",
        "token_type": "Bearer",
        "expires_in": 900,
        "scope": "tasks:read",
        "refresh_token": "refresh-me",
    }
    (grant,) = fake.grants
    assert isinstance(grant, issuance.ApprovedConsent)
    assert grant.scope == ("tasks:read",)
