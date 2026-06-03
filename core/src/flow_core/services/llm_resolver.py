"""Per-org LLM provider resolution (task 8afda4e7).

The single place that turns an org's configured provider into a concrete
``LLMProvider`` plus the ``CostBasis`` the metering seam charges on:

- no row / inactive / ``local`` -> ``ai_providers.get_llm()`` (the env /
  Ollama / stub fallback), basis ``local``;
- hosted (``openai`` / ``anthropic``) with the org's OWN Fernet-encrypted
  key -> that provider, basis ``byok``;
- hosted with NO own key -> that provider on OUR key
  (``settings.*_api_key``), basis ``our_key``;
- hosted but no key resolvable at all -> degrade to the local seam
  (never break the caller; narration/sweeps stay functional).

The metering wrapper (MeteredLLM, task a66ba043) consumes the
``(provider, basis)`` pair; see ``resolve_llm``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.ai_providers import LLMProvider, get_llm
from flow_core.config import get_settings
from flow_core.crypto import decrypt_secret, encrypt_secret
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.llm_anthropic import AnthropicLLM
from flow_core.llm_openai import OpenAILLM
from flow_core.models.billing import CostBasis
from flow_core.models.membership import Role
from flow_core.models.org_llm_provider import LLMProviderKind, OrgLLMProvider
from flow_core.services import audit
from flow_core.services.rbac import require_role

# Safety-net model ids if a hosted provider is configured without a model.
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-latest"


async def get_org_llm_provider(session: AsyncSession, org_id: uuid.UUID) -> OrgLLMProvider | None:
    return (
        await session.execute(select(OrgLLMProvider).where(OrgLLMProvider.org_id == org_id))
    ).scalar_one_or_none()


async def resolve_provider(
    session: AsyncSession, org_id: uuid.UUID
) -> tuple[LLMProvider, CostBasis]:
    """Resolve the org's provider and the basis to meter it on."""
    cfg = await get_org_llm_provider(session, org_id)
    if cfg is None or not cfg.is_active or cfg.provider == LLMProviderKind.local:
        return get_llm(), CostBasis.local

    settings = get_settings()
    own_key = decrypt_secret(cfg.api_key_ciphertext) if cfg.api_key_ciphertext else None

    if cfg.provider == LLMProviderKind.openai:
        key = own_key or settings.openai_api_key
        if not key:
            return get_llm(), CostBasis.local
        provider: LLMProvider = OpenAILLM(
            api_key=key,
            model=cfg.model or _DEFAULT_OPENAI_MODEL,
            base_url=settings.openai_base_url,
        )
    elif cfg.provider == LLMProviderKind.anthropic:
        key = own_key or settings.anthropic_api_key
        if not key:
            return get_llm(), CostBasis.local
        provider = AnthropicLLM(
            api_key=key,
            model=cfg.model or _DEFAULT_ANTHROPIC_MODEL,
            base_url=settings.anthropic_base_url,
            version=settings.anthropic_version,
        )
    else:  # unknown provider string slipped past the CHECK -> degrade.
        return get_llm(), CostBasis.local

    return provider, (CostBasis.byok if own_key else CostBasis.our_key)


async def set_org_llm_provider(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    provider: str,
    model: str | None = None,
    api_key: str | None = None,
) -> OrgLLMProvider:
    """Admin: select the org's provider. ``api_key`` semantics on update:
    ``None`` leaves the stored key untouched, ``""`` clears it (back to
    our-key/local), a value (re)encrypts and stores it (BYOK)."""
    await require_role(session, org_id, actor_id, Role.admin)
    try:
        kind = LLMProviderKind(provider)
    except ValueError as exc:
        raise DomainError(MessageCode.DOMAIN_ERROR) from exc

    existing = await get_org_llm_provider(session, org_id)
    if existing is None:
        row = OrgLLMProvider(
            org_id=org_id,
            provider=kind.value,
            model=model,
            api_key_ciphertext=encrypt_secret(api_key) if api_key else None,
        )
        session.add(row)
        await session.flush()
    else:
        existing.provider = kind.value
        existing.model = model
        if api_key is not None:
            existing.api_key_ciphertext = encrypt_secret(api_key) if api_key else None
        existing.version += 1
        await session.flush()
        row = existing
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="org_llm_provider",
        entity_id=None,
        action="set",
        diff={"provider": kind.value, "model": model or ""},
    )
    return row
