"""The client strategies this deployment knows, and their order.

Adding a strategy: a module here with a class satisfying
``resolution.ClientResolver``, and one entry in ``default_registry``. Their
ids are disjoint by construction (a ``mycelium_pc_`` prefix, an https URL, a
UUID), so today the order matters only for a strategy added later whose ids
overlap one of these -- which is the case the order is there to decide.
"""

from __future__ import annotations

from functools import lru_cache

from mycelium_core.oauth.resolution import ClientRegistry
from mycelium_core.oauth.strategies.assistant_secret import AssistantSecretClients
from mycelium_core.oauth.strategies.metadata_document import MetadataDocumentClients
from mycelium_core.oauth.strategies.registered import RegisteredClients


@lru_cache(maxsize=1)
def default_registry() -> ClientRegistry:
    return ClientRegistry(
        [RegisteredClients(), MetadataDocumentClients(), AssistantSecretClients()]
    )


__all__ = ["default_registry"]
