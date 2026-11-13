"""The client-resolution registry: one contract, strategies in order (ADR-0069).

What is pinned is the extension property the design exists for: a new way of
identifying a client is a new object satisfying ``ClientResolver`` and one
entry in the registry, and the registry -- not the endpoints -- decides who
claims an id, what the discovery document advertises, and which strategies
are switched off.

Observed failing before the registry existed (2026-11-13): ``mycelium_core.
oauth.resolution`` did not import.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mycelium_core.config import Settings
from mycelium_core.oauth.resolution import (
    ClientDescriptor,
    ClientRegistry,
    ResolveContext,
    TokenAuth,
)
from mycelium_core.oauth.strategies import default_registry

ISSUER = "https://mcp.example"


class FakeStrategy:
    """A strategy nobody has written yet: claims ids starting with ``fake:``."""

    name: str = "fake"
    token_auth: TokenAuth = TokenAuth.none
    requires_consent: bool = True
    auth_methods: tuple[str, ...] = ("private_key_jwt",)

    def __init__(self, *, on: bool = True) -> None:
        self.on = on
        self.resolved: list[str] = []

    def enabled(self, settings: Settings) -> bool:
        return self.on

    def claims(self, client_id: str) -> bool:
        return client_id.startswith("fake:")

    async def resolve(self, client_id: str, context: ResolveContext) -> ClientDescriptor | None:
        self.resolved.append(client_id)
        return ClientDescriptor(
            kind=self.name,
            client_id=client_id,
            display_name="Fake",
            registration="fake",
            redirect_uris=("https://fake.example/cb",),
            token_auth=self.token_auth,
            requires_consent=True,
        )

    def metadata_fields(self, issuer: str, settings: Settings) -> Mapping[str, object]:
        return {"fake_supported": True}

    async def record_grant(self, session: Any, client_id: str) -> None:
        return None


class GreedyStrategy(FakeStrategy):
    name: str = "greedy"
    auth_methods: tuple[str, ...] = ("none",)

    def claims(self, client_id: str) -> bool:
        return True

    def metadata_fields(self, issuer: str, settings: Settings) -> Mapping[str, object]:
        return {}


def _methods(meta: Mapping[str, object]) -> set[str]:
    value = meta["token_endpoint_auth_methods_supported"]
    assert isinstance(value, list)
    return {str(v) for v in value}


def _settings(**kw: object) -> Settings:
    return Settings(**kw)  # type: ignore[arg-type]


async def test_a_new_strategy_plugs_in_without_touching_the_others() -> None:
    fake = FakeStrategy()
    registry = ClientRegistry([*default_registry().resolvers, fake])
    settings = _settings()
    assert registry.claimant("fake:abc", settings) is fake
    desc = await registry.resolve("fake:abc", settings)
    assert desc is not None and desc.kind == "fake"
    meta = registry.metadata(ISSUER, settings)
    assert meta["fake_supported"] is True
    assert "private_key_jwt" in meta["token_endpoint_auth_methods_supported"]
    # The built-in strategies still claim what they claimed.
    assert registry.claimant("mycelium_pc_x", settings).name == "registered"  # type: ignore[union-attr]
    assert (
        registry.claimant("6f1e1d5a-4a51-4c35-9c4b-8f3fbbd0a3a2", settings).name  # type: ignore[union-attr]
        == "assistant_secret"
    )


def test_the_first_claimant_in_order_wins() -> None:
    fake, greedy = FakeStrategy(), GreedyStrategy()
    settings = _settings()
    assert ClientRegistry([fake, greedy]).claimant("fake:1", settings) is fake
    assert ClientRegistry([greedy, fake]).claimant("fake:1", settings) is greedy


async def test_a_disabled_strategy_neither_claims_nor_advertises() -> None:
    fake = FakeStrategy(on=False)
    registry = ClientRegistry([fake])
    settings = _settings()
    assert registry.claimant("fake:1", settings) is None
    assert await registry.resolve("fake:1", settings) is None
    assert fake.resolved == []
    meta = registry.metadata(ISSUER, settings)
    assert "fake_supported" not in meta
    assert meta["token_endpoint_auth_methods_supported"] == []


def test_registration_is_served_by_the_registry_not_by_name() -> None:
    registry = default_registry()
    open_ = registry.registration(_settings())
    assert open_ is not None and open_.__class__.__name__ == "RegisteredClients"
    assert registry.registration(_settings(oauth_dynamic_registration_enabled=False)) is None
    assert registry.registration(_settings(oauth_registered_clients_enabled=False)) is None
    # A registry without a registering strategy simply has no endpoint.
    assert ClientRegistry([FakeStrategy()]).registration(_settings()) is None


def test_the_built_in_strategies_follow_their_settings() -> None:
    registry = default_registry()
    everything = registry.metadata(ISSUER, _settings())
    assert everything["registration_endpoint"] == f"{ISSUER}/api/oauth/register"
    assert everything["client_id_metadata_document_supported"] is True
    assert _methods(everything) == {
        "none",
        "client_secret_post",
        "client_secret_basic",
    }

    no_dcr = registry.metadata(ISSUER, _settings(oauth_dynamic_registration_enabled=False))
    assert "registration_endpoint" not in no_dcr
    assert "none" in no_dcr["token_endpoint_auth_methods_supported"]  # manual + CIMD remain

    no_cimd = registry.metadata(ISSUER, _settings(oauth_client_metadata_documents_enabled=False))
    assert "client_id_metadata_document_supported" not in no_cimd
    assert registry.claimant("https://app.example/c.json", _settings()) is not None
    assert (
        registry.claimant(
            "https://app.example/c.json",
            _settings(oauth_client_metadata_documents_enabled=False),
        )
        is None
    )

    no_legacy = _settings(oauth_assistant_secret_clients_enabled=False)
    assert _methods(registry.metadata(ISSUER, no_legacy)) == {"none"}
    assert registry.claimant("6f1e1d5a-4a51-4c35-9c4b-8f3fbbd0a3a2", no_legacy) is None

    only_legacy = _settings(
        oauth_registered_clients_enabled=False, oauth_client_metadata_documents_enabled=False
    )
    meta = registry.metadata(ISSUER, only_legacy)
    assert "none" not in meta["token_endpoint_auth_methods_supported"]
    assert "registration_endpoint" not in meta
