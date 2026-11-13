"""The authorization path, one for every client strategy (ADR-0069).

The endpoints hand this module what the client sent and get back what to
answer. In between, the client is resolved through the registry into a
``ClientDescriptor``, and from there the path depends only on two of its
properties, never on which strategy produced it:

``requires_consent``  True: /authorize stores the request and sends the
                      browser to the consent page; /token redeems the code a
                      person approved. False: the client is pre-authorized
                      (its credential is the person's earlier decision), so
                      /authorize issues a code at once.
``token_auth``        how the client proves itself at /token: PKCE alone
                      (``none``), or PKCE plus a secret the descriptor's
                      authenticator checks (``client_secret``).

Every success ends at the credential issuer (``oauth.issuance``), which
builds the token response.

**Errors before and after the redirect URI is trusted.** Until the redirect
URI has been matched against what the client is allowed to use, an error is
answered here (400), because redirecting it would be an open redirect. After,
errors go back to the client through its redirect URI (RFC 6749 §4.1.2.1) --
for consent clients. Pre-authorized clients keep answering 400 at every step,
which is what the assistant-secret connector flow always did.

**No network at /token.** A consent client is not resolved again at the
exchange: the stored request is the record of what was resolved at
/authorize, and the strategy is found by its pure ``claims`` only to be told
the grant happened.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core import public_origin
from mycelium_core.config import Settings
from mycelium_core.db import admin_session
from mycelium_core.errors import DomainError
from mycelium_core.oauth.issuance import CredentialIssuer, TokenResponse
from mycelium_core.oauth.redirects import policy_from_settings
from mycelium_core.oauth.resolution import (
    AuthRefusal,
    ClientRegistry,
    ClientResolutionError,
    ResolveContext,
    TokenAuth,
)
from mycelium_core.services import oauth_codes as codes_svc
from mycelium_core.services import oauth_consent as consent_svc

_log = logging.getLogger("mycelium.oauth.flow")


@dataclass(frozen=True)
class Redirect:
    """Send the browser here (302)."""

    location: str


@dataclass(frozen=True)
class OAuthFailure:
    """Answer with OAuth's error body. ``status`` is 400 or 401."""

    status: int
    error: str
    description: str


def accepted_resources(issuer: str) -> frozenset[str]:
    """RFC 8707 resource indicators this server issues tokens for: the MCP
    endpoint, with or without the trailing slash clients variously send."""
    mcp = public_origin.mcp_resource(issuer)
    return frozenset({mcp, f"{mcp}/"})


def _redirect_error(redirect_uri: str, error: str, state: str, description: str) -> Redirect:
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    _log.info("oauth authorize refused: error=%s description=%s", error, description)
    return Redirect(consent_svc.with_query(redirect_uri, params))


async def authorize(
    params: Mapping[str, str],
    *,
    issuer: str,
    origin_ip: str | None,
    settings: Settings,
    registry: ClientRegistry,
) -> Redirect | OAuthFailure:
    """``origin_ip`` is an origin bucket (``oauth.origin.origin_bucket``)."""
    response_type = params.get("response_type", "")
    client_id = params.get("client_id", "").strip()
    redirect_uri = params.get("redirect_uri", "").strip()
    code_challenge = params.get("code_challenge", "").strip()
    code_challenge_method = params.get("code_challenge_method", "S256").strip()
    state = params.get("state", "")

    if response_type != "code":
        return OAuthFailure(
            400, "unsupported_response_type", "only response_type=code is supported"
        )
    owner = registry.claimant(client_id, settings)
    if owner is None:
        return OAuthFailure(400, "invalid_request", "unknown client_id")

    # The order is the defence. 1: a consent client's origin is checked
    # against its budget of unanswered requests BEFORE anything is resolved,
    # so a caller over budget gets no metadata document fetched on its
    # behalf. 2: the client is resolved with NO session open -- a metadata
    # document is network I/O, and holding a pooled connection across it let
    # a dozen slow ids exhaust the pool (security review, 2026-11-13).
    # 3: only then a session, for the writes.
    if owner.requires_consent:
        async with admin_session() as session:
            await consent_svc.check_open_rate(session, origin_ip=origin_ip)
    try:
        client = await registry.resolve(client_id, settings, ResolveContext(origin=origin_ip))
    except ClientResolutionError as exc:
        return OAuthFailure(400, "invalid_request", exc.description)
    if client is None:
        return OAuthFailure(400, "invalid_request", "unknown client_id")
    if not redirect_uri or not policy_from_settings(settings).accepts(
        client.redirect_uris, redirect_uri
    ):
        return OAuthFailure(
            400, "invalid_request", "redirect_uri is not acceptable for this client"
        )

    if not client.requires_consent:
        async with admin_session() as session:
            return await _authorize_preauthorized(
                session,
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                state=state,
            )

    if len(state) > consent_svc.MAX_STATE_LENGTH:
        return OAuthFailure(400, "invalid_request", "state too long")
    if code_challenge_method != "S256" or not consent_svc.valid_s256_challenge(code_challenge):
        return _redirect_error(
            redirect_uri, "invalid_request", state, "PKCE with an S256 code_challenge is required"
        )
    resource = params.get("resource")
    if resource is not None and resource not in accepted_resources(issuer):
        return _redirect_error(
            redirect_uri, "invalid_target", state, "resource is not this server's MCP endpoint"
        )
    scope = params.get("scope")
    if scope is not None and len(scope) > consent_svc.MAX_SCOPE_PARAM_LENGTH:
        return _redirect_error(redirect_uri, "invalid_scope", state, "scope too long")

    async with admin_session() as session:
        await consent_svc.make_room(session)
        request_id = await consent_svc.open_request(
            session,
            client_id=client.client_id,
            client_name=client.display_name,
            client_registration=client.registration,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state or None,
            resource=resource,
            requested_scope=scope,
            origin_ip=origin_ip,
        )
    # The SPA lives on the issuer's host. The id is a handle on a pending
    # request, not a credential: the page it opens requires a login, and
    # answering it requires that person's session.
    return Redirect(f"{issuer}/oauth/consent?{urlencode({'request': request_id})}")


async def _authorize_preauthorized(
    session: AsyncSession,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str,
) -> Redirect | OAuthFailure:
    if not code_challenge:
        return OAuthFailure(400, "invalid_request", "code_challenge required (PKCE)")
    if code_challenge_method.upper() != "S256":
        return OAuthFailure(400, "invalid_request", "code_challenge_method must be S256")
    code = await codes_svc.mint(
        session,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    target_qs = {"code": code}
    if state:
        target_qs["state"] = state
    return Redirect(consent_svc.with_query(redirect_uri, target_qs))


async def exchange(
    form: Mapping[str, object],
    *,
    client_id: str,
    client_secret: str,
    issuer: str,
    settings: Settings,
    registry: ClientRegistry,
    credential_issuer: CredentialIssuer,
) -> TokenResponse | OAuthFailure:
    """The token endpoint. ``client_id`` / ``client_secret`` come from the
    form or from HTTP Basic, already merged by the adapter."""
    grant_type = str(form.get("grant_type", ""))
    code = str(form.get("code", "")).strip()
    redirect_uri = str(form.get("redirect_uri", "")).strip()
    code_verifier = str(form.get("code_verifier", "")).strip()
    resource = form.get("resource")

    if grant_type != "authorization_code":
        return OAuthFailure(400, "unsupported_grant_type", "only authorization_code is supported")
    owner = registry.claimant(client_id, settings)
    if owner is None:
        return OAuthFailure(401, "invalid_client", "unknown client_id")

    if owner.token_auth is TokenAuth.none:
        return await _exchange_consented(
            client_id=client_id,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            resource=resource,
            issuer=issuer,
            registry=registry,
            settings=settings,
            credential_issuer=credential_issuer,
        )
    return await _exchange_preauthorized(
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        settings=settings,
        registry=registry,
        credential_issuer=credential_issuer,
    )


async def _exchange_consented(
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    resource: object,
    issuer: str,
    registry: ClientRegistry,
    settings: Settings,
    credential_issuer: CredentialIssuer,
) -> TokenResponse | OAuthFailure:
    """A consent client: PKCE binds the code, the stored approval says whose
    credential it becomes. ``redirect_uri`` is required: RFC 6749 §4.1.3
    requires it whenever the authorization request carried one, and here it
    always did."""
    if not code or not code_verifier or not redirect_uri:
        return OAuthFailure(
            400,
            "invalid_request",
            "code, code_verifier, client_id and redirect_uri are required",
        )
    if resource is not None and (
        not isinstance(resource, str) or len(resource) > consent_svc.MAX_RESOURCE_LENGTH
    ):
        return OAuthFailure(400, "invalid_target", "bad resource")
    owner = registry.claimant(client_id, settings)
    try:
        async with admin_session() as session:
            outcome = await consent_svc.redeem(
                session,
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
                resource=resource,
                accepted_resources=accepted_resources(issuer),
            )
            if isinstance(outcome, consent_svc.GrantRefusal):
                return OAuthFailure(400, outcome.error, outcome.description)
            token = await credential_issuer.issue(session, outcome)
            if owner is not None:
                await owner.record_grant(session, client_id)
    except DomainError as exc:
        # The approval was valid when given but cannot be honoured now (the
        # approver lost the role the scope needs, or left the workspace).
        # The transaction rolled back, so nothing was created.
        _log.info("oauth token: approval no longer honoured: %s", exc.code)
        return OAuthFailure(
            400, "invalid_grant", "the approval behind this code can no longer be honoured"
        )
    return token


async def _exchange_preauthorized(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    settings: Settings,
    registry: ClientRegistry,
    credential_issuer: CredentialIssuer,
) -> TokenResponse | OAuthFailure:
    """A pre-authorized client: the code proves the redirect round trip, the
    secret proves the client, the issuer turns the proof into a token."""
    if not code or not code_verifier or not client_id or not client_secret:
        return OAuthFailure(
            400,
            "invalid_request",
            "code, code_verifier, client_id and client_secret are required",
        )
    async with admin_session() as session:
        entry = await codes_svc.consume(session, code=code)
    if entry is None:
        return OAuthFailure(400, "invalid_grant", "code unknown or expired")
    if not secrets.compare_digest(entry.client_id.encode(), client_id.encode()):
        return OAuthFailure(400, "invalid_grant", "client_id mismatch")
    if redirect_uri and not secrets.compare_digest(
        entry.redirect_uri.encode(), redirect_uri.encode()
    ):
        return OAuthFailure(400, "invalid_grant", "redirect_uri mismatch")
    if entry.code_challenge_method.upper() != "S256" or not consent_svc.verify_pkce(
        code_verifier, entry.code_challenge
    ):
        return OAuthFailure(400, "invalid_grant", "PKCE verification failed")

    client = await registry.resolve(client_id, settings, ResolveContext())
    if client is None or client.authenticator is None:
        return OAuthFailure(401, "invalid_client", "client cannot authenticate")
    proof = await client.authenticator.authenticate(client_secret)
    if isinstance(proof, AuthRefusal):
        return OAuthFailure(401, "invalid_client", proof.description)
    async with admin_session() as session:
        return await credential_issuer.issue(session, proof)


__all__ = ["OAuthFailure", "Redirect", "accepted_resources", "authorize", "exchange"]
