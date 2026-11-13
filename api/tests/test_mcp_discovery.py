"""What an MCP client meets before it has a credential.

Two defects measured in production, each of which stops a standards-
compliant client before OAuth even starts:

1. ``POST /mcp`` answered ``307 -> /mcp/``, because the MCP app is a
   Starlette mount and a mount only matches below its prefix. Several
   clients do not follow a redirect on POST (they would have to resend the
   body), so the server URL people paste -- without a trailing slash --
   failed. The fix makes ``/mcp`` reach the MCP app itself.
2. The MCP 401s carried no ``WWW-Authenticate`` header. The MCP
   authorization specification (and RFC 9728 §5.1) has the client read the
   protected-resource metadata URL from that header; without it a client has
   to guess, and the ones that do not guess stop.

Observed failing before the fix (2026-11-13): the first test got 307 on the
slash-less path, and every 401 test found no ``www-authenticate`` header.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.routing import Mount
from tests_helpers import random_ula48

from mycelium_api.app import create_app
from mycelium_mcp.gateway import gateway

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "discovery-test", "version": "0"},
    },
}
_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_EXPECTED_CHALLENGE = (
    'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource/mcp"'
)


def random_origin() -> str:
    return f"{random_ula48()}::1"


@contextlib.asynccontextmanager
async def _client() -> AsyncIterator[AsyncClient]:
    """The real API app, reached the way nginx reaches it, with its MCP
    mount's lifespan running so the streamable-http transport answers.

    The MCP SDK keeps ONE session manager per FastMCP instance and lets it
    run once per process, while every test here needs a running one in its
    own task. So each app is built with a fresh manager, by clearing the
    SDK's slot before ``create_app`` and putting the previous one back
    afterwards, unconditionally: the process-wide app other tests import
    keeps the manager it was built with."""
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
                # What nginx forwards; every URL the server publishes is
                # built from it.
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "mcp.example"},
            ) as c:
                yield c
    finally:
        gateway._session_manager = saved


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
async def test_post_mcp_is_answered_without_a_redirect(path: str) -> None:
    async with _client() as client:
        resp = await client.post(path, json=_INIT, headers=_HEADERS)
        assert resp.status_code != 307, resp.headers.get("location")
        # No bearer: the MCP app itself answered, with its own 401.
        assert resp.status_code == 401, resp.text


async def test_missing_bearer_401_names_the_resource_metadata() -> None:
    async with _client() as client:
        resp = await client.post("/mcp", json=_INIT, headers=_HEADERS)
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == _EXPECTED_CHALLENGE


async def test_non_agent_bearer_401_names_the_resource_metadata() -> None:
    async with _client() as client:
        resp = await client.post(
            "/mcp/", json=_INIT, headers={**_HEADERS, "Authorization": "Bearer not-ours"}
        )
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == _EXPECTED_CHALLENGE


async def test_unknown_agent_token_401_names_the_resource_metadata() -> None:
    async with _client() as client:
        resp = await client.post(
            "/mcp/",
            json=_INIT,
            headers={**_HEADERS, "Authorization": "Bearer mycelium_at_doesnotexist"},
        )
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == _EXPECTED_CHALLENGE


async def test_the_metadata_the_challenge_names_exists_and_names_the_server() -> None:
    """The URL in the challenge must resolve to the metadata it promises,
    and that metadata must point at this server as the authorization
    server, or the client has followed the header into a dead end."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://backend",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "mcp.example"},
    ) as c:
        meta = await c.get("/.well-known/oauth-protected-resource/mcp")
    assert meta.status_code == 200
    body = meta.json()
    assert body["resource"] == "https://mcp.example/mcp"
    assert body["authorization_servers"] == ["https://mcp.example"]
