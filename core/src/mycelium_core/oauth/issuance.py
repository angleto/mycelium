"""Credential issuance: what a successful exchange produces (ADR-0069).

The authorization flow ends with a GRANT -- a person's approval of a consent
request, or a pre-authorized client that proved its secret -- and hands it to
a ``CredentialIssuer``, which turns it into the token response. The flow does
not know what a token is, and the issuer does not know how the grant was
obtained or which client strategy produced it.

Today's issuer, ``AssistantBearerIssuer``, returns an AI assistant's
``mycelium_at_`` bearer: for an approval it creates the assistant and its
first token as the approver, in the workspace they approved in; for a
pre-authorized assistant it returns the secret the client already holds.
Short-lived tokens with refresh would be another issuer installed with
``set_credential_issuer``, and nothing upstream of it changes.

What this does NOT cover: a refresh grant at /token. The flow accepts only
``authorization_code``; an issuer that hands out refresh tokens will also
need the flow to accept ``refresh_token``, which is a change to the flow,
deliberately not anticipated here.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import as_tenant
from mycelium_core.models.ai_assistant_oauth_grant import AiAssistantOAuthGrant
from mycelium_core.models.oauth_authorization_request import OAuthAuthorizationRequest
from mycelium_core.services import ai_assistants

# What an assistant created through OAuth carries in ``provider``: a machine
# value for filtering, not prose. The person's label is the name.
OAUTH_PROVIDER = "oauth"

# The legacy bearer is long-lived and has no expiry the client could act on;
# a nominal year is what the connector flow always answered, kept so the
# pre-authorized path is byte-for-byte what it was.
_PREAUTHORIZED_NOMINAL_EXPIRES_IN = 31536000


@dataclass(frozen=True)
class TokenResponse:
    """RFC 6749 §5.1. Built by the issuer, serialised by the endpoint."""

    access_token: str
    expires_in: int | None
    scope: tuple[str, ...]
    token_type: str = "Bearer"  # noqa: S105 (the token type's name)
    refresh_token: str | None = None

    def body(self) -> dict[str, object]:
        out: dict[str, object] = {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "scope": " ".join(sorted(self.scope)),
        }
        if self.expires_in is not None:
            out["expires_in"] = self.expires_in
        if self.refresh_token is not None:
            out["refresh_token"] = self.refresh_token
        return out


@dataclass(frozen=True)
class ApprovedConsent:
    """A person approved a consent request, and its code was redeemed."""

    request_id: uuid.UUID
    client_id: str
    org_id: uuid.UUID
    approver_id: uuid.UUID
    label: str
    scope: tuple[str, ...]
    # Provenance, recorded with the credential (``AiAssistantOAuthGrant``).
    client_name: str
    client_registration: str
    redirect_uri: str
    approved_at: datetime.datetime


@dataclass(frozen=True)
class PreauthorizedCredential:
    """A pre-authorized client proved its secret; the credential exists."""

    raw_secret: str
    scope: tuple[str, ...]


class CredentialIssuer(Protocol):
    async def issue(
        self, session: AsyncSession, grant: ApprovedConsent | PreauthorizedCredential
    ) -> TokenResponse: ...


class AssistantBearerIssuer:
    async def issue(
        self, session: AsyncSession, grant: ApprovedConsent | PreauthorizedCredential
    ) -> TokenResponse:
        if isinstance(grant, PreauthorizedCredential):
            return TokenResponse(
                access_token=grant.raw_secret,
                expires_in=_PREAUTHORIZED_NOMINAL_EXPIRES_IN,
                scope=grant.scope,
            )
        # The caller holds a code and nothing else, so the session carries no
        # tenant. The window is opened as the approver, read from the stored
        # request, never from the call; ``create_assistant`` checks their
        # role again now, so a role taken away since the approval stops it.
        async with as_tenant(session, org_id=grant.org_id, user_id=grant.approver_id):
            created = await ai_assistants.create_assistant(
                session,
                org_id=grant.org_id,
                actor_id=grant.approver_id,
                label=grant.label,
                scope=list(grant.scope),
                provider=OAUTH_PROVIDER,
            )
            # Who holds this bearer, recorded where the assistant lives and
            # for as long as it does (the request row is swept after a week).
            session.add(
                AiAssistantOAuthGrant(
                    assistant_id=created.assistant.id,
                    org_id=grant.org_id,
                    client_id=grant.client_id,
                    client_registration=grant.client_registration,
                    client_name=grant.client_name,
                    redirect_uri=grant.redirect_uri,
                    approved_by_id=grant.approver_id,
                    approved_at=grant.approved_at,
                )
            )
            await session.flush()
        await session.execute(
            update(OAuthAuthorizationRequest)
            .where(OAuthAuthorizationRequest.id == grant.request_id)
            .values(assistant_id=created.assistant.id)
        )
        expires_in = None
        if created.token_expires_at is not None:
            remaining = created.token_expires_at - datetime.datetime.now(tz=datetime.UTC)
            expires_in = max(0, int(remaining.total_seconds()))
        return TokenResponse(
            access_token=created.raw_secret,
            expires_in=expires_in,
            scope=tuple(created.assistant.scope_list()),
        )


_issuer: CredentialIssuer = AssistantBearerIssuer()


def credential_issuer() -> CredentialIssuer:
    return _issuer


def set_credential_issuer(issuer: CredentialIssuer) -> CredentialIssuer:
    """Install an issuer; returns the previous one so the caller can put it
    back. The seam a different token model plugs into."""
    global _issuer
    previous = _issuer
    _issuer = issuer
    return previous


__all__ = [
    "OAUTH_PROVIDER",
    "ApprovedConsent",
    "AssistantBearerIssuer",
    "CredentialIssuer",
    "PreauthorizedCredential",
    "TokenResponse",
    "credential_issuer",
    "set_credential_issuer",
]
