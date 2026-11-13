"""Strategy: public clients registered in ``oauth_clients`` (ADR-0069).

Two ways a row comes to exist, one strategy for both: Dynamic Client
Registration (RFC 7591, unauthenticated, ``register_dynamic_client`` below)
and a person registering one by hand in the settings. Ids carry the
``mycelium_pc_`` prefix, which is what ``claims`` reads. Public client, consent
and PKCE.

The registration endpoint lives here rather than in the router because it is
this strategy's: switching the strategy off, or only dynamic registration,
removes it from the discovery document and from service together.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import Settings
from mycelium_core.db import admin_session
from mycelium_core.oauth.production import open_endpoints_allowed
from mycelium_core.oauth.redirects import RedirectUriError, policy_from_settings, redirect_target
from mycelium_core.oauth.resolution import (
    ClientDescriptor,
    RegistrationError,
    ResolveContext,
    TokenAuth,
)
from mycelium_core.services import oauth_clients as clients_svc

# What a public client may ask to be registered for. ``refresh_token`` is
# accepted because most hosts ask for it by default, and dropped from what is
# registered because none is issued: RFC 7591 §3.2.1 has the server return
# what it actually registered, and the client reads that.
_REGISTRABLE_GRANTS = frozenset({"authorization_code", "refresh_token"})
_REGISTRABLE_RESPONSES = frozenset({"code"})


class RegisteredClients:
    name = "registered"
    token_auth = TokenAuth.none
    requires_consent = True
    auth_methods: tuple[str, ...] = ("none",)

    def enabled(self, settings: Settings) -> bool:
        return settings.oauth_registered_clients_enabled

    def claims(self, client_id: str) -> bool:
        return clients_svc.is_public_client_id(client_id)

    async def resolve(self, client_id: str, context: ResolveContext) -> ClientDescriptor | None:
        # A short session of its own, for one indexed lookup, closed before
        # the descriptor is returned.
        async with admin_session() as session:
            row = await clients_svc.get_by_client_id(session, client_id=client_id)
        if row is None:
            return None
        return ClientDescriptor(
            kind=self.name,
            client_id=row.client_id,
            display_name=row.client_name,
            registration=row.registration,
            redirect_uris=tuple(row.redirect_uris),
            token_auth=self.token_auth,
            requires_consent=self.requires_consent,
        )

    def metadata_fields(self, issuer: str, settings: Settings) -> Mapping[str, object]:
        if not dynamic_registration_open(settings):
            return {}
        return {"registration_endpoint": f"{issuer}/api/oauth/register"}

    async def record_grant(self, session: AsyncSession, client_id: str) -> None:
        # Takes the client out of the sweep's reach for good.
        await clients_svc.mark_granted(session, client_id=client_id)

    def registration_open(self, settings: Settings) -> bool:
        return dynamic_registration_open(settings)

    async def register_client(
        self, *, body: object, origin: str | None, settings: Settings
    ) -> dict[str, object] | RegistrationError:
        async with admin_session() as session:
            return await register_dynamic_client(
                session, body=body, origin_ip=origin, settings=settings
            )


def dynamic_registration_open(settings: Settings) -> bool:
    """The open RFC 7591 endpoint: off with the strategy, off on its own
    flag, and off in production without the settings that make its
    per-origin bound and its issuer trustworthy (``oauth.production``).
    Hand-registered clients are unaffected: a person wrote those."""
    return (
        settings.oauth_registered_clients_enabled
        and settings.oauth_dynamic_registration_enabled
        and open_endpoints_allowed(settings)
    )


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(value)


async def register_dynamic_client(
    session: AsyncSession, *, body: object, origin_ip: str | None, settings: Settings
) -> dict[str, object] | RegistrationError:
    """Validate an RFC 7591 request and write the client. Returns the
    registration response body, or the error to answer with.

    Every field is the registrant's claim. ``client_name`` is shown on the
    consent page next to the redirect target and never instead of it."""
    if not isinstance(body, dict):
        return RegistrationError("invalid_client_metadata", "JSON object required")
    try:
        redirect_uris = policy_from_settings(settings).validate_list(body.get("redirect_uris"))
    except RedirectUriError:
        return RegistrationError(
            "invalid_redirect_uri",
            "1 to 10 redirect_uris, each https://, http:// on a loopback host, or an "
            "allowlisted private-use scheme; no fragment or userinfo",
        )
    if "grant_types" in body:
        asked = _string_list(body["grant_types"])
        if (
            asked is None
            or "authorization_code" not in asked
            or not set(asked) <= _REGISTRABLE_GRANTS
        ):
            return RegistrationError(
                "invalid_client_metadata", "grant_types must be authorization_code"
            )
    if "response_types" in body:
        asked = _string_list(body["response_types"])
        if asked is None or not asked or not set(asked) <= _REGISTRABLE_RESPONSES:
            return RegistrationError("invalid_client_metadata", "response_types must be code")
    raw_name = body.get("client_name")
    if raw_name is not None and (
        not isinstance(raw_name, str) or len(raw_name) > clients_svc.MAX_CLIENT_NAME_LENGTH
    ):
        return RegistrationError(
            "invalid_client_metadata",
            f"client_name must be a string of at most {clients_svc.MAX_CLIENT_NAME_LENGTH} "
            "characters",
        )
    # A nameless client is called by where its codes go: the one thing about
    # it that is not a claim.
    name = clients_svc.clean_client_name(raw_name, fallback=redirect_target(redirect_uris[0]))
    await clients_svc.check_registration_rate(session, origin_ip=origin_ip)
    row = await clients_svc.register_dynamic(
        session, client_name=name, redirect_uris=redirect_uris, origin_ip=origin_ip
    )
    issued_at = int((row.created_at or datetime.datetime.now(tz=datetime.UTC)).timestamp())
    return {
        "client_id": row.client_id,
        "client_id_issued_at": issued_at,
        "client_name": name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        # Whatever was asked for: a public client has no secret to
        # authenticate with, and this is what it must use.
        "token_endpoint_auth_method": "none",
    }
