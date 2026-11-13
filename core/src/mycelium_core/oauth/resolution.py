"""Client resolution: one contract, strategies in order (ADR-0069).

An OAuth ``client_id`` can mean several things, and which ones a deployment
accepts will change with the providers and the specifications: today an AI
assistant's UUID, a registered public client, or an https URL naming a Client
ID Metadata Document; tomorrow something else. Each meaning is a STRATEGY, an
object satisfying ``ClientResolver``, and the registry asks them in order.
Nothing outside this package knows which strategies exist.

**How to add one.** Write a module under ``strategies/`` with a class that
satisfies ``ClientResolver`` and add an instance to ``strategies.default_registry``. Its
``claims`` must be cheap and pure, and must not overlap another strategy's
ids (the registry takes the FIRST claimant, so an overlap is decided by
order, silently). It must NOT touch the endpoints, the authorization flow, the
credential issuer or another strategy: if it seems to need to, the contract
is missing something and the contract is what changes.

What a strategy decides about its clients, through the descriptor:

- ``redirect_uris``: the registered URIs, or ``None`` for "any URI the
  redirect policy accepts" (only sound where something else binds the code
  to the client, as the assistant secret does);
- ``requires_consent``: whether a person approves each authorization on the
  consent page, or the client is pre-authorized (its credential IS the
  person's earlier decision);
- ``token_auth`` and ``authenticator``: how the client proves itself at
  /token -- PKCE only (``none``) or a secret, checked by the authenticator.

What a strategy contributes to discovery: ``auth_methods`` (the union across
active strategies becomes ``token_endpoint_auth_methods_supported``) and
``metadata_fields`` (merged into the authorization-server metadata, e.g.
``registration_endpoint``). A strategy switched off in ``Settings`` neither
claims ids nor contributes anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.mcp_scopes import VALID_SCOPE_KEYS

if TYPE_CHECKING:
    from mycelium_core.config import Settings
    from mycelium_core.oauth.issuance import PreauthorizedCredential


class TokenAuth(StrEnum):
    """How a client authenticates at the token endpoint. A closed set: the
    flow has one path per member, and an unknown value has no path."""

    none = "none"
    client_secret = "client_secret"  # noqa: S105 (an authentication method's name)


class ClientResolutionError(Exception):
    """The id is one a strategy recognises, but the client it names cannot be
    used (a metadata document that is unreachable or describes another
    client). ``description`` goes to the client's developer, never to a
    person, and says nothing about internal addresses."""

    def __init__(self, description: str) -> None:
        self.description = description
        super().__init__(description)


@dataclass(frozen=True)
class AuthRefusal:
    """A secret the authenticator did not accept. ``description`` is for the
    client's developer (RFC 6749 §5.2 ``error_description``)."""

    description: str


class ClientAuthenticator(Protocol):
    """How a secret-authenticated client proves itself at /token."""

    async def authenticate(self, client_secret: str) -> PreauthorizedCredential | AuthRefusal: ...


@dataclass(frozen=True)
class ClientDescriptor:
    """Everything the authorization flow needs to know about a client, and
    nothing about how it was found."""

    kind: str  # the strategy's name
    client_id: str
    display_name: str
    # What the consent page says about where the client came from:
    # "dynamic", "manual", "metadata_document", "assistant".
    registration: str
    redirect_uris: tuple[str, ...] | None
    token_auth: TokenAuth
    requires_consent: bool
    authenticator: ClientAuthenticator | None = None


@runtime_checkable
class ClientResolver(Protocol):
    name: str
    token_auth: TokenAuth
    requires_consent: bool
    auth_methods: tuple[str, ...]

    def enabled(self, settings: Settings) -> bool: ...

    def claims(self, client_id: str) -> bool:
        """Cheap and pure: no I/O. Whether this id is this strategy's."""
        ...

    async def resolve(self, client_id: str, context: ResolveContext) -> ClientDescriptor | None:
        """The client behind a claimed id, or None if there is none. May
        raise ``ClientResolutionError`` for a recognised but unusable one.

        Called with NO database session open, and must not hold one across
        I/O of its own: a strategy that needs a lookup opens a short session
        for it, and one that goes to the network does so holding nothing.
        A slow resolution therefore costs a request, never a pooled
        connection (security review, 2026-11-13: twelve slow metadata
        documents held twelve of the fifteen)."""
        ...

    def metadata_fields(self, issuer: str, settings: Settings) -> Mapping[str, object]: ...

    async def record_grant(self, session: AsyncSession, client_id: str) -> None:
        """Told after a successful exchange. A no-op for most strategies."""
        ...


@dataclass(frozen=True)
class ResolveContext:
    """Who is asking, for strategies whose resolution costs something per
    caller (the metadata-document fetch is metered per origin)."""

    origin: str | None = None


@dataclass(frozen=True)
class RegistrationError:
    """RFC 7591 §3.2.2."""

    error: str
    description: str


@runtime_checkable
class SupportsDynamicRegistration(Protocol):
    """A strategy that also offers RFC 7591 registration. Optional: the
    registry serves ``/register`` from the first active strategy that both
    implements this and says registration is open."""

    def registration_open(self, settings: Settings) -> bool: ...

    async def register_client(
        self, *, body: object, origin: str | None, settings: Settings
    ) -> dict[str, object] | RegistrationError: ...


class ClientRegistry:
    """The strategies, in order. The first enabled one that claims an id owns
    it."""

    def __init__(self, resolvers: Sequence[ClientResolver]) -> None:
        names = [r.name for r in resolvers]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate strategy names: {names}")
        self.resolvers: tuple[ClientResolver, ...] = tuple(resolvers)

    def active(self, settings: Settings) -> list[ClientResolver]:
        return [r for r in self.resolvers if r.enabled(settings)]

    def claimant(self, client_id: str, settings: Settings) -> ClientResolver | None:
        for r in self.active(settings):
            if r.claims(client_id):
                return r
        return None

    async def resolve(
        self, client_id: str, settings: Settings, context: ResolveContext | None = None
    ) -> ClientDescriptor | None:
        owner = self.claimant(client_id, settings)
        if owner is None:
            return None
        return await owner.resolve(client_id, context or ResolveContext())

    def registration(self, settings: Settings) -> SupportsDynamicRegistration | None:
        """The strategy that answers ``/register``, if any is open. The
        endpoint asks this rather than naming a strategy, so which kind of
        client registration creates is the registry's to decide."""
        for r in self.active(settings):
            if isinstance(r, SupportsDynamicRegistration) and r.registration_open(settings):
                return r
        return None

    def metadata(self, issuer: str, settings: Settings) -> dict[str, object]:
        """RFC 8414 authorization-server metadata, generated from the
        strategies that are switched on. The endpoints and the PKCE method
        are the flow's and fixed; what clients can be and how they
        authenticate is the strategies'."""
        methods: list[str] = []
        extra: dict[str, object] = {}
        for r in self.active(settings):
            for m in r.auth_methods:
                if m not in methods:
                    methods.append(m)
            extra.update(r.metadata_fields(issuer, settings))
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/api/oauth/authorize",
            "token_endpoint": f"{issuer}/api/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": methods,
            # The scope catalogue. A client may ask for any subset with the
            # ``scope`` parameter; the consent page preselects it and the
            # person decides.
            "scopes_supported": sorted(VALID_SCOPE_KEYS),
            "service_documentation": issuer,
            "client_uri": issuer,
            **extra,
        }


__all__ = [
    "AuthRefusal",
    "ClientAuthenticator",
    "ClientDescriptor",
    "ClientRegistry",
    "ClientResolutionError",
    "ClientResolver",
    "RegistrationError",
    "ResolveContext",
    "SupportsDynamicRegistration",
    "TokenAuth",
]
