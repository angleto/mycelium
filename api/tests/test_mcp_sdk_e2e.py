"""End to end with the official MCP SDK client, over a real HTTP server.

Everything else in the OAuth suite drives the routes in-process. This file
behaves like a real host, because the defects that stopped Claude Desktop in
production (a 307 on ``/mcp``, a 401 with no ``WWW-Authenticate``) were
invisible in-process and visible only to a client:

- the server is uvicorn on a free 127.0.0.1 port, so redirects, headers and
  streaming are the real ones;
- the client is ``mcp.client.streamable_http.streamable_http_client`` with ``mcp.client.auth.
  OAuthClientProvider``, pointed at ``<base>/mcp`` WITHOUT the trailing slash,
  as Claude Desktop is. It gets the 401, reads ``WWW-Authenticate``, fetches
  the protected-resource and authorization-server metadata, registers (or
  uses a metadata-document URL), opens the authorization URL, and exchanges
  the code with PKCE;
- the test plays the person: it follows the authorization URL to the
  consent page, approves with a session JWT, and hands the code to the SDK's
  callback handler.

Two stand-ins, both at the edge and both named here. ``_NginxLike`` does the
one thing nginx does that the backend relies on -- strip ``/api`` and send
the rest of the SPA's paths nowhere -- so the URLs the metadata advertises
resolve as they do in production. And the metadata-document case fetches its
document through a fake network installed on the server's fetcher, because a
real fetch to 127.0.0.1 is exactly what the fetcher refuses.

Observed failing before the fixes it guards (2026-11-13): run against the
phase-one tree, ``POST /mcp`` got 307 and the SDK raised on the redirect; the
SDK requested every scope in ``scopes_supported`` and the consent page
preselected the dangerous ones (now fixed in ``oauth_consent.suggested_scope``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl
from starlette.routing import Mount
from tests_helpers import random_ula48, streamed_response

from mycelium_api.app import create_app
from mycelium_core.config import get_settings
from mycelium_core.oauth import cimd
from mycelium_mcp.gateway import gateway

CALLBACK = "http://127.0.0.1:65000/callback"
CIMD_ID = "https://app.example/oauth/client.json"


class _NginxLike:
    """The routing nginx does in front of the backend, and nothing else:
    ``/api/*`` stripped of ``/api``; the two OAuth well-known paths and
    ``/mcp*`` passed through; any other path (the SPA's) 404. Also records
    every response status, so the test can assert no redirect was ever sent."""

    def __init__(self, app: Any) -> None:
        self.app = app
        # Every caller of this server is one random IPv6 /64, as distinct
        # hosts would be: the per-origin bounds then count this run alone,
        # and the file can run any number of times against one database.
        self.origin = f"{random_ula48()}::1"
        self.seen: list[tuple[str, str, int]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope = dict(scope, client=(self.origin, 1))
        path: str = scope["path"]
        passthrough = path.startswith(
            (
                "/mcp",
                "/.well-known/oauth-authorization-server",
                "/.well-known/oauth-protected-resource",
            )
        )
        if path.startswith("/api/"):
            scope = dict(scope, path=path[4:], raw_path=path[4:].encode())
        elif not passthrough:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        method = scope["method"]

        async def recording_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                self.seen.append((method, path, message["status"]))
            await send(message)

        await self.app(scope, receive, recording_send)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def _server() -> AsyncIterator[tuple[str, _NginxLike]]:
    # A loopback server on a random port has no public name to pin and no
    # proxy chain to anchor, so dynamic registration and metadata documents
    # run through their explicit opt-out (ADR-0069, production configuration),
    # stated here rather than inherited from the suite's conftest.
    saved_opt_out = os.environ.get("MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK")
    os.environ["MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK"] = "true"
    get_settings.cache_clear()
    saved = gateway._session_manager
    gateway._session_manager = None
    try:
        app = create_app()
        mount = next(r for r in app.routes if isinstance(r, Mount) and r.path == "/mcp")
        edge = _NginxLike(app)
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                edge,
                host="127.0.0.1",
                port=port,
                lifespan="off",
                log_level="warning",
                # No websocket stack: MCP does not use it, and importing it
                # raises a deprecation warning the suite treats as an error.
                ws="none",
            )
        )
        async with mount.app.router.lifespan_context(mount.app):  # type: ignore[attr-defined]
            task = asyncio.create_task(server.serve())
            try:
                # Generous: on a loaded machine startup has taken seconds.
                for _ in range(600):
                    if server.started or task.done():
                        break
                    await asyncio.sleep(0.05)
                if task.done():
                    task.result()  # surface why uvicorn stopped
                assert server.started, "uvicorn did not start"
                yield f"http://127.0.0.1:{port}", edge
            finally:
                server.should_exit = True
                await task
    finally:
        gateway._session_manager = saved
        if saved_opt_out is None:
            os.environ.pop("MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK", None)
        else:
            os.environ["MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK"] = saved_opt_out
        get_settings.cache_clear()


class _MemoryStorage:
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client = client_info


class _Person:
    """Plays the human: has a session, follows the authorization URL to the
    consent page, approves, and hands the code to the SDK."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.headers: dict[str, str] = {}
        self.code: tuple[str, str | None] | None = None
        self.consent: dict[str, Any] = {}

    async def sign_up(self) -> None:
        async with httpx.AsyncClient() as h:
            su = (
                await h.post(
                    f"{self.base}/api/auth/signup",
                    json={
                        "email": f"{uuid.uuid4().hex[:10]}@example.test",
                        "password": "pw-strong-123",
                        "workspace_name": "SDK",
                    },
                )
            ).json()
        self.headers = {
            "Authorization": f"Bearer {su['token']}",
            "X-Workspace-Id": su["workspace_id"],
            "X-Workspace-Role": "owner",
        }

    async def open_authorization_url(self, url: str) -> None:
        async with httpx.AsyncClient() as h:
            first = await h.get(url, follow_redirects=False)
            assert first.status_code == 302, first.text
            consent = urlsplit(first.headers["location"])
            assert consent.path == "/oauth/consent", first.headers["location"]
            rid = parse_qs(consent.query)["request"][0]
            shown = await h.get(
                f"{self.base}/api/oauth/consent", headers=self.headers, params={"request": rid}
            )
            assert shown.status_code == 200, shown.text
            self.consent = shown.json()
            approved = await h.post(
                f"{self.base}/api/oauth/consent/approve",
                headers=self.headers,
                json={
                    "request": rid,
                    "label": "SDK host",
                    "scope": self.consent["suggested_scope"],
                },
            )
            assert approved.status_code == 200, approved.text
        back = parse_qs(urlsplit(approved.json()["redirect_url"]).query)
        self.code = (back["code"][0], back.get("state", [None])[0])

    async def callback(self) -> tuple[str, str | None]:
        assert self.code is not None
        return self.code


async def _use_mcp(
    url: str, *, auth: httpx.Auth | None = None, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Connect as a host would. Redirects are NOT followed: the SDK's own
    default client follows them, Claude Desktop does not, and the stricter
    of the two is the one that catches a 307."""
    client = httpx.AsyncClient(
        auth=auth,
        headers=headers,
        follow_redirects=False,
        timeout=httpx.Timeout(30.0, read=300.0),
    )
    async with client, streamable_http_client(url, http_client=client) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            who = await session.call_tool("execute_tool", {"name": "whoami", "arguments": {}})
    return {
        "server": init.serverInfo.name,
        "tools": sorted(t.name for t in tools.tools),
        "who_error": who.isError,
        "who_text": " ".join(getattr(c, "text", "") for c in who.content),
    }


def _metadata() -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name="SDK e2e host",
        redirect_uris=[AnyUrl(CALLBACK)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def _assert_no_redirects(edge: _NginxLike) -> None:
    redirects = [s for s in edge.seen if 300 <= s[2] < 400 and s[1].startswith("/mcp")]
    assert redirects == [], redirects
    assert any(m == "POST" and p == "/mcp" and st == 200 for m, p, st in edge.seen), edge.seen


async def test_the_sdk_connects_through_dynamic_registration() -> None:
    async with _server() as (base, edge):
        person = _Person(base)
        await person.sign_up()
        storage = _MemoryStorage()
        auth = OAuthClientProvider(
            server_url=f"{base}/mcp",
            client_metadata=_metadata(),
            storage=storage,
            redirect_handler=person.open_authorization_url,
            callback_handler=person.callback,
        )
        used = await _use_mcp(f"{base}/mcp", auth=auth)

    assert used["tools"] == ["describe_tools", "execute_tool", "ping", "search_tools"]
    assert used["who_error"] is False, used["who_text"]
    assert storage.client is not None
    assert storage.client.client_id.startswith("mycelium_pc_")  # registered dynamically
    assert storage.tokens is not None and storage.tokens.access_token.startswith("mycelium_at_")
    assert person.consent["registration"] == "dynamic"
    assert person.consent["redirect_target"] == "http://127.0.0.1:65000"
    # The SDK asks for everything advertised; nothing dangerous was ticked.
    assert not any(k.startswith("delete:") for k in person.consent["suggested_scope"])
    _assert_no_redirects(edge)


async def test_the_sdk_connects_with_a_client_metadata_document() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        doc = {
            "client_id": CIMD_ID,
            "client_name": "SDK via metadata document",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
        }
        return streamed_response(
            200, content=json.dumps(doc).encode(), headers={"Content-Type": "application/json"}
        )

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"] if host == "app.example" else []

    previous = cimd.set_shared_fetcher(
        cimd.MetadataFetcher(resolve=resolve, transport=httpx.MockTransport(handler))
    )
    try:
        async with _server() as (base, edge):
            person = _Person(base)
            await person.sign_up()
            storage = _MemoryStorage()
            auth = OAuthClientProvider(
                server_url=f"{base}/mcp",
                client_metadata=_metadata(),
                storage=storage,
                redirect_handler=person.open_authorization_url,
                callback_handler=person.callback,
                client_metadata_url=CIMD_ID,
            )
            used = await _use_mcp(f"{base}/mcp", auth=auth)
    finally:
        cimd.set_shared_fetcher(previous)

    assert used["who_error"] is False, used["who_text"]
    assert storage.client is not None and storage.client.client_id == CIMD_ID
    assert person.consent["registration"] == "metadata_document"
    assert len(requests) == 1, "the document is fetched once, at /authorize"
    assert not any(p.endswith("/oauth/register") for _, p, _ in edge.seen), "no registration"
    _assert_no_redirects(edge)


async def test_a_static_bearer_on_the_slashless_url_needs_no_redirect() -> None:
    async with _server() as (base, edge):
        person = _Person(base)
        await person.sign_up()
        async with httpx.AsyncClient() as h:
            created = await h.post(
                f"{base}/api/ai-assistants",
                headers=person.headers,
                json={"label": "static", "scope": ["tasks:read"]},
            )
        assert created.status_code == 200, created.text
        secret = created.json()["raw_secret"]
        used = await _use_mcp(f"{base}/mcp", headers={"Authorization": f"Bearer {secret}"})

    assert used["tools"] == ["describe_tools", "execute_tool", "ping", "search_tools"]
    assert used["who_error"] is False, used["who_text"]
    _assert_no_redirects(edge)
