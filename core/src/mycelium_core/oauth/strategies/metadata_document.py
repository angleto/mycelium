"""Strategy: Client ID Metadata Documents (ADR-0069).

The client id is an https URL; the JSON document at it is the client's
registration. Public client, consent and PKCE, exactly like a registered
client: the only difference is where the name and redirect URIs come from.
The fetch and its defences are ``oauth.cimd``; this module only maps its
answer onto the contract.

Resolution happens at /authorize, the only step that fetches. The flow
snapshots the name and the redirect URI on the stored request, so the
consent page and /token never come back here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import Settings, get_settings
from mycelium_core.oauth import cimd
from mycelium_core.oauth.production import open_endpoints_allowed
from mycelium_core.oauth.redirects import policy_from_settings
from mycelium_core.oauth.resolution import (
    ClientDescriptor,
    ClientResolutionError,
    ResolveContext,
    TokenAuth,
)

_log = logging.getLogger("mycelium.oauth.cimd")


class MetadataDocumentClients:
    name = "metadata_document"
    token_auth = TokenAuth.none
    requires_consent = True
    auth_methods: tuple[str, ...] = ("none",)

    def __init__(self, fetcher: cimd.MetadataFetcher | None = None) -> None:
        # None: the process-wide fetcher, read at call time so a test can
        # install one with a fake network.
        self._fetcher = fetcher

    def enabled(self, settings: Settings) -> bool:
        # Off in production without the settings that make its per-origin
        # meter and its issuer trustworthy (``oauth.production``).
        return settings.oauth_client_metadata_documents_enabled and open_endpoints_allowed(settings)

    def claims(self, client_id: str) -> bool:
        return cimd.is_client_id_url(client_id)

    async def resolve(self, client_id: str, context: ResolveContext) -> ClientDescriptor | None:
        # No session is open here, by contract: this is network I/O for an
        # unauthenticated caller, metered per origin inside the fetcher.
        fetcher = self._fetcher or cimd.shared_fetcher()
        try:
            meta = await fetcher.get(
                client_id, policy_from_settings(get_settings()), origin=context.origin
            )
        except cimd.CimdError as exc:
            _log.info("client metadata document refused: %s", exc.reason)
            raise ClientResolutionError(exc.reason) from None
        return ClientDescriptor(
            kind=self.name,
            client_id=meta.client_id,
            display_name=meta.client_name,
            registration="metadata_document",
            redirect_uris=meta.redirect_uris,
            token_auth=self.token_auth,
            requires_consent=self.requires_consent,
        )

    def metadata_fields(self, issuer: str, settings: Settings) -> Mapping[str, object]:
        return {"client_id_metadata_document_supported": True}

    async def record_grant(self, session: AsyncSession, client_id: str) -> None:
        return None
