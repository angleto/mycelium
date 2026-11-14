"""OAuth for any MCP host: registration, consent, exchange (ADR-0069).

Driven over the real routes in the order the parties act:

    an MCP host with nothing      POST /oauth/register
                                  GET  /oauth/authorize  -> consent page
    a person with a session       GET  /oauth/consent
                                  POST /oauth/consent/approve | deny
    the host, with the code       POST /oauth/token
    the host, with the token      POST /mcp/  (initialize)

and then the refusals, each of which is a way a code or a credential could
end up with somebody who should not hold it. The last step of the happy
path is the one no mock can stand in for: the bearer the exchange produced
opens an MCP session and carries exactly the scope the person ticked.

The legacy flow (AI-assistant UUID + its secret) is asserted here too,
because nothing else exercised it over HTTP and it must keep working.

Observed failing before the implementation (2026-11-13): register 404'd,
/authorize answered a public client id with 400 "client_id must be the AI
assistant UUID", the consent routes 404'd, and the metadata carried no
``registration_endpoint`` and no ``none`` auth method.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update
from starlette.routing import Mount
from tests_helpers import random_ula48

from mycelium_api.app import create_app
from mycelium_api.routers import oauth as oauth_router
from mycelium_core.db import admin_session
from mycelium_core.mcp_scopes import DEFAULT_SCOPES
from mycelium_core.models.oauth_authorization_request import OAuthAuthorizationRequest
from mycelium_core.models.oauth_code import OAuthCode
from mycelium_core.services import oauth_clients as clients_svc
from mycelium_mcp.gateway import gateway

ISSUER = "https://mcp.example"
CALLBACK = "https://host.example/oauth/callback"


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


async def _register(c: AsyncClient, **body: Any) -> Response:
    payload: dict[str, Any] = {"client_name": "Test Host", "redirect_uris": [CALLBACK]}
    payload.update(body)
    return await c.post("/oauth/register", json=payload)


async def _client_id(c: AsyncClient, redirect_uris: list[str] | None = None) -> str:
    r = await _register(c, redirect_uris=redirect_uris or [CALLBACK])
    assert r.status_code == 201, r.text
    return str(r.json()["client_id"])


async def _authorize(
    c: AsyncClient,
    client_id: str,
    challenge: str,
    *,
    redirect_uri: str = CALLBACK,
    state: str = "st-123",
    **extra: str,
) -> Response:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        **extra,
    }
    return await c.get("/oauth/authorize", params=params)


def _consent_request_id(resp: Response) -> str:
    assert resp.status_code == 302, resp.text
    loc = urlsplit(resp.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}" == ISSUER
    assert loc.path == "/oauth/consent"
    return parse_qs(loc.query)["request"][0]


async def _approve(
    c: AsyncClient,
    person: dict[str, str],
    request_id: str,
    *,
    scope: list[str] | None = None,
    label: str = "My host",
) -> dict[str, list[str]]:
    r = await c.post(
        "/oauth/consent/approve",
        headers=person,
        json={
            "request": request_id,
            "label": label,
            "scope": list(DEFAULT_SCOPES) if scope is None else scope,
        },
    )
    assert r.status_code == 200, r.text
    target = urlsplit(r.json()["redirect_url"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == CALLBACK
    return parse_qs(target.query)


async def _token(c: AsyncClient, **form: str) -> Response:
    return await c.post("/oauth/token", data={"grant_type": "authorization_code", **form})


async def _code_for(
    c: AsyncClient, person: dict[str, str], client_id: str, challenge: str, **extra: Any
) -> str:
    rid = _consent_request_id(await _authorize(c, client_id, challenge))
    return (await _approve(c, person, rid, **extra))["code"][0]


# ----- metadata --------------------------------------------------------------


async def test_metadata_advertises_registration_and_public_clients() -> None:
    async with _client() as c:
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert meta["issuer"] == ISSUER
        assert meta["registration_endpoint"] == f"{ISSUER}/api/oauth/register"
        assert meta["authorization_endpoint"] == f"{ISSUER}/api/oauth/authorize"
        assert meta["token_endpoint"] == f"{ISSUER}/api/oauth/token"
        methods = meta["token_endpoint_auth_methods_supported"]
        # "none" added for public clients, the legacy secret methods kept.
        assert {"none", "client_secret_post", "client_secret_basic"} <= set(methods)
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert set(DEFAULT_SCOPES) <= set(meta["scopes_supported"])


# ----- registration ----------------------------------------------------------


async def test_register_returns_a_public_client_and_no_secret() -> None:
    async with _client() as c:
        r = await _register(
            c,
            client_name="Claude",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["client_id"].startswith(clients_svc.PUBLIC_CLIENT_PREFIX)
        assert isinstance(body["client_id_issued_at"], int)
        assert body["client_name"] == "Claude"
        assert body["redirect_uris"] == [CALLBACK]
        # What is registered, not what was asked: no refresh tokens are issued,
        # so the response says so rather than echoing the request.
        assert body["grant_types"] == ["authorization_code"]
        assert body["response_types"] == ["code"]
        assert body["token_endpoint_auth_method"] == "none"
        assert "client_secret" not in body


@pytest.mark.parametrize(
    "redirect_uris",
    [
        None,  # missing
        [],
        [f"https://host.example/cb{i}" for i in range(11)],  # too many
        ["http://host.example/cb"],  # cleartext, not loopback
        ["https://host.example/cb#frag"],
        ["https://host.example@evil.example/cb"],  # userinfo: reads as one host
        ["javascript:alert(1)"],
        ["https://"],
        ["https://host.example/" + "a" * 2100],
        ["https://host.example/cb\tx"],
        "https://host.example/cb",  # not a list
    ],
)
async def test_register_refuses_unacceptable_redirects(redirect_uris: object) -> None:
    async with _client() as c:
        body: dict[str, Any] = {"client_name": "X"}
        if redirect_uris is not None:
            body["redirect_uris"] = redirect_uris
        r = await c.post("/oauth/register", json=body)
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "extra",
    [
        {"grant_types": ["client_credentials"]},
        {"grant_types": ["implicit"]},
        {"response_types": ["token"]},
        {"client_name": "x" * 101},
        {"client_name": 42},
    ],
)
async def test_register_refuses_metadata_it_cannot_honour(extra: dict[str, Any]) -> None:
    async with _client() as c:
        r = await _register(c, **extra)
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "invalid_client_metadata"


async def test_register_accepts_loopback_hosts() -> None:
    async with _client() as c:
        r = await _register(
            c,
            redirect_uris=[
                "http://127.0.0.1:33418/callback",
                "http://localhost:6274/oauth/callback",
                "http://[::1]/cb",
            ],
        )
        assert r.status_code == 201, r.text


async def test_unfinished_registrations_from_one_origin_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint is open to the internet, so the rows it writes are
    bounded per network origin. The address is the one the server resolves
    at the edge, replaced here with a fresh one so the count starts at 0."""
    async with _client() as c:
        origin = random_origin()
        monkeypatch.setattr(oauth_router, "_resolve_issuer_client_ip", lambda _req: origin)
        monkeypatch.setattr(clients_svc, "REGISTER_LIMIT_PER_ORIGIN", 2)
        assert (await _register(c)).status_code == 201
        assert (await _register(c)).status_code == 201
        refused = await _register(c)
        assert refused.status_code == 429, refused.text


# ----- authorize -------------------------------------------------------------


async def test_public_authorize_goes_to_consent_and_mints_no_code() -> None:
    async with _client() as c:
        client_id = await _client_id(c)
        _, challenge = _pkce()
        resp = await _authorize(c, client_id, challenge)
        request_id = _consent_request_id(resp)
        assert "code=" not in resp.headers["location"]
        async with admin_session() as s:
            legacy = (
                (await s.execute(select(OAuthCode).where(OAuthCode.client_id == client_id)))
                .scalars()
                .all()
            )
            pending = (
                (
                    await s.execute(
                        select(OAuthAuthorizationRequest).where(
                            OAuthAuthorizationRequest.client_id == client_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert legacy == []
        assert len(pending) == 1
        assert pending[0].code_hash is None
        # The id in the URL is not what is stored.
        assert pending[0].request_id_hash != request_id


async def test_authorize_refuses_an_unregistered_redirect_without_redirecting() -> None:
    async with _client() as c:
        client_id = await _client_id(c)
        _, challenge = _pkce()
        resp = await _authorize(c, client_id, challenge, redirect_uri="https://evil.example/cb")
        # Never a 302: redirecting an error to an unverified URI is the open
        # redirect the check exists to prevent.
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "invalid_request"


async def test_authorize_requires_s256_pkce() -> None:
    async with _client() as c:
        client_id = await _client_id(c)
        missing = await c.get(
            "/oauth/authorize",
            params={"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK},
        )
        assert missing.status_code in (302, 400)
        if missing.status_code == 302:
            assert "error=invalid_request" in missing.headers["location"]
        plain = await _authorize(c, client_id, "x" * 43, code_challenge_method="plain")
        assert plain.status_code in (302, 400)
        if plain.status_code == 302:
            assert "error=invalid_request" in plain.headers["location"]


async def test_authorize_refuses_a_foreign_resource() -> None:
    async with _client() as c:
        client_id = await _client_id(c)
        _, challenge = _pkce()
        resp = await _authorize(c, client_id, challenge, resource="https://other.example/mcp")
        assert resp.status_code == 302
        target = urlsplit(resp.headers["location"])
        assert f"{target.scheme}://{target.netloc}{target.path}" == CALLBACK
        q = parse_qs(target.query)
        assert q["error"] == ["invalid_target"]
        assert q["state"] == ["st-123"]


async def test_authorize_accepts_the_mcp_resource_with_or_without_slash() -> None:
    async with _client() as c:
        client_id = await _client_id(c)
        _, challenge = _pkce()
        for resource in (f"{ISSUER}/mcp", f"{ISSUER}/mcp/"):
            _consent_request_id(await _authorize(c, client_id, challenge, resource=resource))


async def test_loopback_redirect_matches_on_any_port_and_nothing_else() -> None:
    async with _client() as c:
        client_id = await _client_id(c, ["http://127.0.0.1:33418/callback"])
        _, challenge = _pkce()
        _consent_request_id(
            await _authorize(
                c, client_id, challenge, redirect_uri="http://127.0.0.1:50999/callback"
            )
        )
        _consent_request_id(
            await _authorize(c, client_id, challenge, redirect_uri="http://127.0.0.1/callback")
        )
        for wrong in (
            "http://127.0.0.1:50999/other",
            "http://localhost:50999/callback",
            "https://127.0.0.1:50999/callback",
            "http://127.0.0.1:50999/callback?x=1",
        ):
            resp = await _authorize(c, client_id, challenge, redirect_uri=wrong)
            assert resp.status_code == 400, (wrong, resp.text)


# ----- consent ---------------------------------------------------------------


async def test_consent_shows_the_client_and_where_the_code_goes() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        _, challenge = _pkce()
        rid = _consent_request_id(await _authorize(c, client_id, challenge, scope="tasks:read"))
        shown = await c.get("/oauth/consent", headers=person, params={"request": rid})
        assert shown.status_code == 200, shown.text
        body = shown.json()
        assert body["client_id"] == client_id
        assert body["client_name"] == "Test Host"
        assert body["redirect_target"] == "https://host.example"
        assert body["redirect_uri"] == CALLBACK
        assert body["registration"] == "dynamic"
        assert body["suggested_scope"] == ["tasks:read"]


async def test_consent_answers_need_a_person_not_a_credential() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        verifier, challenge = _pkce()
        code = await _code_for(c, person, client_id, challenge)
        issued = (
            await _token(
                c, code=code, client_id=client_id, redirect_uri=CALLBACK, code_verifier=verifier
            )
        ).json()
        agent = {
            "Authorization": f"Bearer {issued['access_token']}",
            "X-Workspace-Id": person["X-Workspace-Id"],
        }
        _, challenge2 = _pkce()
        rid = _consent_request_id(await _authorize(c, client_id, challenge2))
        looked = await c.get("/oauth/consent", headers=agent, params={"request": rid})
        assert looked.status_code == 403, looked.text
        approved = await c.post(
            "/oauth/consent/approve",
            headers=agent,
            json={"request": rid, "label": "x", "scope": list(DEFAULT_SCOPES)},
        )
        assert approved.status_code == 403, approved.text
        denied = await c.post("/oauth/consent/deny", headers=agent, json={"request": rid})
        assert denied.status_code == 403, denied.text
        # And the request is still open for the person: the refusals answered it.
        still = await c.get("/oauth/consent", headers=person, params={"request": rid})
        assert still.status_code == 200, still.text


async def test_deny_sends_access_denied_and_spends_the_request() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        _, challenge = _pkce()
        rid = _consent_request_id(await _authorize(c, client_id, challenge))
        r = await c.post("/oauth/consent/deny", headers=person, json={"request": rid})
        assert r.status_code == 200, r.text
        target = urlsplit(r.json()["redirect_url"])
        assert f"{target.scheme}://{target.netloc}{target.path}" == CALLBACK
        assert parse_qs(target.query) == {"error": ["access_denied"], "state": ["st-123"]}
        again = await c.post(
            "/oauth/consent/approve",
            headers=person,
            json={"request": rid, "label": "x", "scope": list(DEFAULT_SCOPES)},
        )
        assert again.status_code == 404, again.text


async def test_a_request_is_answered_once() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        _, challenge = _pkce()
        rid = _consent_request_id(await _authorize(c, client_id, challenge))
        await _approve(c, person, rid)
        second = await c.post(
            "/oauth/consent/approve",
            headers=person,
            json={"request": rid, "label": "x", "scope": list(DEFAULT_SCOPES)},
        )
        assert second.status_code == 404, second.text
        unknown = await c.get(
            "/oauth/consent", headers=person, params={"request": secrets.token_urlsafe(32)}
        )
        assert unknown.status_code == 404


async def test_a_member_cannot_grant_what_only_an_owner_may() -> None:
    """The role rule of creating an assistant by hand applies to consent:
    the read scopes are owner-gated, so a plain member is refused at the
    button rather than left with a client that fails at the exchange."""
    async with _client() as c:
        owner_su = await c.post(
            "/auth/signup",
            json={"email": f"{uuid.uuid4().hex[:10]}@example.test", "password": "pw-strong-123"},
        )
        owner = owner_su.json()
        member_email = f"{uuid.uuid4().hex[:10]}@example.test"
        member = (
            await c.post("/auth/signup", json={"email": member_email, "password": "pw-strong-123"})
        ).json()
        added = await c.post(
            "/workspaces/me/members",
            headers={
                "Authorization": f"Bearer {owner['token']}",
                "X-Workspace-Id": owner["workspace_id"],
                "X-Workspace-Role": "owner",
            },
            json={"email": member_email, "role": "member"},
        )
        assert added.status_code == 200, added.text
        as_member = {
            "Authorization": f"Bearer {member['token']}",
            "X-Workspace-Id": owner["workspace_id"],
        }
        client_id = await _client_id(c)
        _, challenge = _pkce()
        rid = _consent_request_id(await _authorize(c, client_id, challenge))
        r = await c.post(
            "/oauth/consent/approve",
            headers=as_member,
            json={"request": rid, "label": "x", "scope": list(DEFAULT_SCOPES)},
        )
        assert r.status_code == 403, r.text


# ----- token -----------------------------------------------------------------


async def test_the_whole_flow_and_the_token_it_produces() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        verifier, challenge = _pkce()
        chosen = ["tasks:read", "notes:read"]
        rid = _consent_request_id(
            await _authorize(c, client_id, challenge, resource=f"{ISSUER}/mcp")
        )
        back = await _approve(c, person, rid, scope=chosen, label="Claude on my laptop")
        assert back["state"] == ["st-123"]

        # Approval minted a code, not a credential: nothing on the settings page.
        listed = (await c.get("/ai-assistants", headers=person)).json()
        assert [a for a in listed if a["label"] == "Claude on my laptop"] == []

        tok = await _token(
            c,
            code=back["code"][0],
            client_id=client_id,
            redirect_uri=CALLBACK,
            code_verifier=verifier,
            resource=f"{ISSUER}/mcp",
        )
        assert tok.status_code == 200, tok.text
        assert tok.headers["cache-control"] == "no-store"
        issued = tok.json()
        assert issued["token_type"] == "Bearer"
        assert issued["access_token"].startswith("mycelium_at_")
        assert sorted(issued["scope"].split()) == sorted(chosen)
        assert issued["expires_in"] > 0
        assert "refresh_token" not in issued

        # The assistant now exists, with the label and scope the person chose.
        rows = [
            a
            for a in (await c.get("/ai-assistants", headers=person)).json()
            if a["label"] == "Claude on my laptop"
        ]
        assert len(rows) == 1
        assert sorted(rows[0]["scope"]) == sorted(chosen)

        # It opens an MCP session ...
        init = await c.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {issued['access_token']}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "oauth-test", "version": "0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        assert "serverInfo" in init.text
        # ... and over REST it is the scope that was chosen and nothing more.
        me = await c.get(
            "/agent/self",
            headers={
                "Authorization": f"Bearer {issued['access_token']}",
                "X-Workspace-Id": person["X-Workspace-Id"],
            },
        )
        assert me.status_code == 200, me.text
        assert sorted(me.json()["scope"]) == sorted(chosen)


async def test_a_wrong_verifier_is_refused_and_burns_the_code() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        verifier, challenge = _pkce()
        code = await _code_for(c, person, client_id, challenge)
        bad = await _token(
            c, code=code, client_id=client_id, redirect_uri=CALLBACK, code_verifier="x" * 50
        )
        assert bad.status_code == 400
        assert bad.json()["error"] == "invalid_grant"
        good = await _token(
            c, code=code, client_id=client_id, redirect_uri=CALLBACK, code_verifier=verifier
        )
        assert good.status_code == 400, good.text
        assert good.json()["error"] == "invalid_grant"


async def test_a_code_is_redeemed_once() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        verifier, challenge = _pkce()
        code = await _code_for(c, person, client_id, challenge)
        form = {
            "code": code,
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
        }
        assert (await _token(c, **form)).status_code == 200
        replay = await _token(c, **form)
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"


async def test_an_unapproved_or_unknown_code_is_refused() -> None:
    async with _client() as c:
        client_id = await _client_id(c)
        verifier, challenge = _pkce()
        _consent_request_id(await _authorize(c, client_id, challenge))  # never answered
        r = await _token(
            c,
            code=secrets.token_urlsafe(32),
            client_id=client_id,
            redirect_uri=CALLBACK,
            code_verifier=verifier,
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"


async def test_a_mismatched_redirect_or_client_is_refused() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c, [CALLBACK, "https://host.example/other"])
        other_client = await _client_id(c)
        verifier, challenge = _pkce()

        code = await _code_for(c, person, client_id, challenge)
        r = await _token(
            c,
            code=code,
            client_id=client_id,
            redirect_uri="https://host.example/other",
            code_verifier=verifier,
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

        code2 = await _code_for(c, person, client_id, challenge)
        r2 = await _token(
            c, code=code2, client_id=other_client, redirect_uri=CALLBACK, code_verifier=verifier
        )
        assert r2.status_code == 400 and r2.json()["error"] == "invalid_grant"

        code3 = await _code_for(c, person, client_id, challenge)
        r3 = await _token(c, code=code3, client_id=client_id, code_verifier=verifier)
        assert r3.status_code == 400 and r3.json()["error"] == "invalid_request"


async def test_a_foreign_resource_at_the_token_endpoint_is_refused() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        verifier, challenge = _pkce()
        code = await _code_for(c, person, client_id, challenge)
        r = await _token(
            c,
            code=code,
            client_id=client_id,
            redirect_uri=CALLBACK,
            code_verifier=verifier,
            resource="https://other.example/mcp",
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_target"


# ----- hand-registered clients -----------------------------------------------


async def test_a_person_registers_a_client_by_hand_and_it_works() -> None:
    async with _client() as c:
        person = await _signup(c)
        created = await c.post(
            "/oauth/clients",
            headers=person,
            json={"client_name": "My Cursor", "redirect_uris": ["http://127.0.0.1:7777/cb"]},
        )
        assert created.status_code == 201, created.text
        client_id = created.json()["client_id"]
        assert client_id.startswith(clients_svc.PUBLIC_CLIENT_PREFIX)
        assert "client_secret" not in created.json()

        listed = (await c.get("/oauth/clients", headers=person)).json()
        assert [x["client_id"] for x in listed] == [client_id]

        # Another workspace does not see it.
        stranger = await _signup(c)
        assert (await c.get("/oauth/clients", headers=stranger)).json() == []
        assert (await c.delete(f"/oauth/clients/{client_id}", headers=stranger)).status_code == 404

        _, challenge = _pkce()
        rid = _consent_request_id(
            await _authorize(c, client_id, challenge, redirect_uri="http://127.0.0.1:9/cb")
        )
        shown = (await c.get("/oauth/consent", headers=person, params={"request": rid})).json()
        assert shown["registration"] == "manual"
        assert shown["redirect_target"] == "http://127.0.0.1:9"

        gone = await c.delete(f"/oauth/clients/{client_id}", headers=person)
        assert gone.status_code == 204
        assert (await c.get("/oauth/clients", headers=person)).json() == []


async def test_hand_registration_validates_redirects() -> None:
    async with _client() as c:
        person = await _signup(c)
        r = await c.post(
            "/oauth/clients",
            headers=person,
            json={"client_name": "X", "redirect_uris": ["http://evil.example/cb"]},
        )
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "oauth.redirect_uri_invalid"


async def test_hand_registration_is_refused_to_credentials() -> None:
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        verifier, challenge = _pkce()
        code = await _code_for(c, person, client_id, challenge)
        issued = (
            await _token(
                c, code=code, client_id=client_id, redirect_uri=CALLBACK, code_verifier=verifier
            )
        ).json()
        agent = {
            "Authorization": f"Bearer {issued['access_token']}",
            "X-Workspace-Id": person["X-Workspace-Id"],
        }
        assert (await c.get("/oauth/clients", headers=agent)).status_code == 403
        assert (
            await c.post(
                "/oauth/clients",
                headers=agent,
                json={"client_name": "X", "redirect_uris": [CALLBACK]},
            )
        ).status_code == 403


# ----- legacy ----------------------------------------------------------------


async def test_the_legacy_assistant_secret_flow_is_unchanged() -> None:
    async with _client() as c:
        person = await _signup(c)
        created = await c.post(
            "/ai-assistants",
            headers=person,
            json={"label": "Legacy", "scope": list(DEFAULT_SCOPES)},
        )
        assert created.status_code == 200, created.text
        assistant_id = created.json()["assistant"]["id"]
        secret = created.json()["raw_secret"]
        verifier, challenge = _pkce()

        auth = await _authorize(c, assistant_id, challenge)
        # Immediate redirect with a code: the person decided in the settings.
        assert auth.status_code == 302
        target = urlsplit(auth.headers["location"])
        assert f"{target.scheme}://{target.netloc}{target.path}" == CALLBACK
        code = parse_qs(target.query)["code"][0]

        tok = await _token(
            c,
            code=code,
            client_id=assistant_id,
            client_secret=secret,
            redirect_uri=CALLBACK,
            code_verifier=verifier,
        )
        assert tok.status_code == 200, tok.text
        assert tok.json()["access_token"] == secret

        # And a legacy code is refused without the secret.
        auth2 = await _authorize(c, assistant_id, challenge)
        code2 = parse_qs(urlsplit(auth2.headers["location"]).query)["code"][0]
        no_secret = await _token(
            c, code=code2, client_id=assistant_id, redirect_uri=CALLBACK, code_verifier=verifier
        )
        assert no_secret.status_code == 400


async def test_expired_requests_and_codes_are_refused() -> None:
    """A request past its ten minutes cannot be described or answered, and
    a code past its ten minutes cannot be exchanged. Ages the rows directly:
    waiting out the clock is not a test."""
    past = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(seconds=1)
    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        verifier, challenge = _pkce()

        rid = _consent_request_id(await _authorize(c, client_id, challenge))
        async with admin_session() as s:
            await s.execute(
                update(OAuthAuthorizationRequest)
                .where(OAuthAuthorizationRequest.client_id == client_id)
                .values(expires_at=past)
            )
        assert (
            await c.get("/oauth/consent", headers=person, params={"request": rid})
        ).status_code == 404
        late = await c.post(
            "/oauth/consent/approve",
            headers=person,
            json={"request": rid, "label": "x", "scope": list(DEFAULT_SCOPES)},
        )
        assert late.status_code == 404, late.text

        code = await _code_for(c, person, client_id, challenge)
        async with admin_session() as s:
            await s.execute(
                update(OAuthAuthorizationRequest)
                .where(OAuthAuthorizationRequest.client_id == client_id)
                .values(code_expires_at=past)
            )
        r = await _token(
            c, code=code, client_id=client_id, redirect_uri=CALLBACK, code_verifier=verifier
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"


# ----- isolation and races (mycelium-cc review, 2026-11-13) ------------------
#
# Each of these is asserted against the database, not the routes alone: the
# route answering 404 to a stranger proves the service filtered, the RLS
# assertions prove the TABLE refuses even a query that forgets to. They were
# mutation-checked: with each policy replaced by ``USING (true)`` the
# cross-workspace reads, writes and inserts below succeed and these fail.


async def _whole_flow(c: AsyncClient) -> tuple[dict[str, str], str]:
    """A person, a dynamic client, an approval and a redeemed code: leaves an
    assistant and its grant row in the person's workspace."""
    person = await _signup(c)
    client_id = await _client_id(c)
    verifier, challenge = _pkce()
    code = await _code_for(c, person, client_id, challenge)
    r = await _token(
        c, code=code, client_id=client_id, redirect_uri=CALLBACK, code_verifier=verifier
    )
    assert r.status_code == 200, r.text
    return person, client_id


async def test_rls_keeps_hand_registered_clients_inside_their_workspace() -> None:
    from sqlalchemy.exc import DBAPIError

    from mycelium_core.db import tenant_session
    from mycelium_core.models.oauth_client import OAuthClient

    async with _client() as c:
        owner = await _signup(c)
        created = await c.post(
            "/oauth/clients",
            headers=owner,
            json={"client_name": "Mine", "redirect_uris": ["http://127.0.0.1:7777/cb"]},
        )
        assert created.status_code == 201, created.text
        client_id = created.json()["client_id"]
        stranger = await _signup(c)
    org_a, org_b = owner["X-Workspace-Id"], stranger["X-Workspace-Id"]
    user = str(uuid.uuid4())

    async with tenant_session(org_a, user) as s:
        mine = (
            await s.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
        ).scalar_one_or_none()
        assert mine is not None, "the owning workspace must see its own client"

    async with tenant_session(org_b, user) as s:
        # No WHERE on org_id: the policy alone must hide it.
        seen = (
            await s.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
        ).scalar_one_or_none()
        assert seen is None
        renamed = await s.execute(
            update(OAuthClient)
            .where(OAuthClient.client_id == client_id)
            .values(client_name="hijacked")
            .returning(OAuthClient.id)
        )
        assert renamed.first() is None

    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(org_b, user) as s:
            s.add(
                OAuthClient(
                    client_id=f"{clients_svc.PUBLIC_CLIENT_PREFIX}{secrets.token_hex(8)}",
                    client_name="planted",
                    redirect_uris=["http://127.0.0.1:1/cb"],
                    registration="manual",
                    org_id=uuid.UUID(org_a),
                    created_by_id=None,
                )
            )
            await s.flush()

    async with tenant_session(org_a, user) as s:
        name = (
            await s.execute(
                select(OAuthClient.client_name).where(OAuthClient.client_id == client_id)
            )
        ).scalar_one()
        assert name == "Mine"


async def test_rls_keeps_oauth_grants_inside_their_workspace() -> None:
    from sqlalchemy.exc import DBAPIError

    from mycelium_core.db import tenant_session
    from mycelium_core.models.ai_assistant_oauth_grant import AiAssistantOAuthGrant

    async with _client() as c:
        owner, client_id = await _whole_flow(c)
        stranger = await _signup(c)
    org_a, org_b = owner["X-Workspace-Id"], stranger["X-Workspace-Id"]
    user = str(uuid.uuid4())

    async with tenant_session(org_a, user) as s:
        grant = (
            await s.execute(
                select(AiAssistantOAuthGrant).where(AiAssistantOAuthGrant.client_id == client_id)
            )
        ).scalar_one()
        assistant_id, approved_at = grant.assistant_id, grant.approved_at

    async with tenant_session(org_b, user) as s:
        seen = (
            await s.execute(
                select(AiAssistantOAuthGrant).where(
                    AiAssistantOAuthGrant.assistant_id == assistant_id
                )
            )
        ).scalar_one_or_none()
        assert seen is None
        moved = await s.execute(
            update(AiAssistantOAuthGrant)
            .where(AiAssistantOAuthGrant.assistant_id == assistant_id)
            .values(redirect_uri="https://evil.example/cb")
            .returning(AiAssistantOAuthGrant.assistant_id)
        )
        assert moved.first() is None

    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(org_b, user) as s:
            # A grant naming workspace A, written from workspace B.
            s.add(
                AiAssistantOAuthGrant(
                    assistant_id=assistant_id,
                    org_id=uuid.UUID(org_a),
                    client_id="planted",
                    client_registration="dynamic",
                    client_name="planted",
                    redirect_uri="https://evil.example/cb",
                    approved_by_id=None,
                    approved_at=approved_at,
                )
            )
            await s.flush()

    async with tenant_session(org_a, user) as s:
        uri = (
            await s.execute(
                select(AiAssistantOAuthGrant.redirect_uri).where(
                    AiAssistantOAuthGrant.assistant_id == assistant_id
                )
            )
        ).scalar_one()
        assert uri == CALLBACK


async def test_concurrent_redeems_of_one_code_yield_exactly_one_token() -> None:
    import asyncio

    async with _client() as c:
        person = await _signup(c)
        client_id = await _client_id(c)
        verifier, challenge = _pkce()
        code = await _code_for(c, person, client_id, challenge)
        form = {
            "code": code,
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
        }
        results = await asyncio.gather(*(_token(c, **form) for _ in range(8)))
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200] + [400] * 7, [r.text for r in results]
    assert {r.json()["error"] for r in results if r.status_code == 400} == {"invalid_grant"}


async def test_a_person_cannot_approve_into_a_workspace_they_are_not_in() -> None:
    async with _client() as c:
        person = await _signup(c)
        stranger = await _signup(c)
        client_id = await _client_id(c)
        _, challenge = _pkce()
        rid = _consent_request_id(await _authorize(c, client_id, challenge))
        foreign = {**person, "X-Workspace-Id": stranger["X-Workspace-Id"]}
        r = await c.post(
            "/oauth/consent/approve",
            headers=foreign,
            json={"request": rid, "label": "x", "scope": list(DEFAULT_SCOPES)},
        )
        assert r.status_code in (403, 404), r.text
        # The request was not spent: its rightful answer still works, in the
        # person's own workspace, and the code lands there.
        params = await _approve(c, person, rid)
        assert params["code"]
