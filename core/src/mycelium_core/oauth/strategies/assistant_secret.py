"""Strategy: an AI assistant's UUID as client id, its bearer as secret.

The original connector flow, kept byte-for-byte: a person creates an AI
assistant in the settings, then pastes its UUID as ``client_id`` and its
``mycelium_at_`` token as ``client_secret`` into an MCP host. That person has
already decided everything a consent page would ask, so the client is
PRE-AUTHORIZED: /authorize issues a code at once, and /token returns the
secret itself as the access token once the authenticator has checked that
the secret is a live agent token bound to exactly that assistant.

``resolve`` does no I/O and does not check that the assistant exists:
``ai_assistants`` is row-level-secured by workspace and /authorize runs
before anybody has picked one. The secret at /token is the gate; a code for
a non-existent assistant is useless without it.

Redirect URIs: none registered (``None``), so any URI the redirect policy
accepts. Sound here only because the code cannot be redeemed without the
secret.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import Settings
from mycelium_core.db import admin_session
from mycelium_core.oauth.issuance import PreauthorizedCredential
from mycelium_core.oauth.resolution import (
    AuthRefusal,
    ClientDescriptor,
    ResolveContext,
    TokenAuth,
)
from mycelium_core.services import agent_tokens as agent_tokens_svc

_log = logging.getLogger("mycelium.oauth.assistant_secret")


class _SecretAuthenticator:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id

    async def authenticate(self, client_secret: str) -> PreauthorizedCredential | AuthRefusal:
        if not agent_tokens_svc.is_agent_token(client_secret):
            return AuthRefusal("client_secret is not a Mycelium agent token")
        principal = await agent_tokens_svc.authenticate(client_secret)
        if principal is None:
            _log.warning(
                "assistant-secret client: secret rejected for client_id=%s -- %s",
                self.client_id,
                await _diagnose(client_secret),
            )
            return AuthRefusal("client_secret rejected")
        # The secret must be bound to the assistant named as client_id: one
        # assistant's secret presented with another's id would otherwise
        # borrow that id's name for a credential that is not it.
        if principal.assistant_id is None or str(principal.assistant_id) != self.client_id:
            _log.warning(
                "assistant-secret client: secret bound to assistant %s, presented as %s",
                principal.assistant_id,
                self.client_id,
            )
            return AuthRefusal("client_secret is not bound to that client_id")
        return PreauthorizedCredential(
            raw_secret=client_secret, scope=tuple(principal.assistant_scope or [])
        )


async def _diagnose(client_secret: str) -> str:
    """Why a secret was rejected, for the operator's log only. The SECURITY
    DEFINER ``oauth_token_diag`` tells "no row" from "revoked" from "expired"
    from "assistant inactive", which ``authenticate_agent_token`` lumps
    together. Best effort: a failure here is reported, never raised."""
    try:
        digest = hashlib.sha256(client_secret.encode("utf-8")).digest()
        async with admin_session() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT out_exists, out_revoked_at, out_expires_at, "
                        "out_assistant_id, out_assistant_active FROM oauth_token_diag(:h)"
                    ),
                    {"h": digest},
                )
            ).first()
    except Exception as exc:  # diagnostic best-effort: the refusal stands either way
        return f"diagnostic query failed: {type(exc).__name__}"
    if row is None or not row[0]:
        return "no agent_tokens row matches the hash"
    return (
        f"row exists: revoked_at={row[1]} expires_at={row[2]} "
        f"assistant_id={row[3]} assistant_active={row[4]}"
    )


class AssistantSecretClients:
    name = "assistant_secret"
    token_auth = TokenAuth.client_secret
    requires_consent = False
    auth_methods: tuple[str, ...] = ("client_secret_post", "client_secret_basic")

    def enabled(self, settings: Settings) -> bool:
        return settings.oauth_assistant_secret_clients_enabled

    def claims(self, client_id: str) -> bool:
        try:
            uuid.UUID(client_id)
        except (ValueError, AttributeError):
            return False
        return True

    async def resolve(self, client_id: str, context: ResolveContext) -> ClientDescriptor | None:
        return ClientDescriptor(
            kind=self.name,
            client_id=str(uuid.UUID(client_id)),
            display_name=client_id,
            registration="assistant",
            redirect_uris=None,
            token_auth=self.token_auth,
            requires_consent=self.requires_consent,
            authenticator=_SecretAuthenticator(client_id),
        )

    def metadata_fields(self, issuer: str, settings: Settings) -> Mapping[str, object]:
        return {}

    async def record_grant(self, session: AsyncSession, client_id: str) -> None:
        return None
