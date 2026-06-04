"""Curated Scaleway model roster + live ``/v1/models`` validation.

Scaleway's serverless model ids carry version/quantization suffixes that
move over time, so we never trust a hardcoded list: the curated allowlist
below is *intersected* with the org's live ``GET /v1/models`` at request
time, and only ids Scaleway actually serves surface in the picker. The
allowlist just bounds the roster to models we have vetted (task d2c60a83).
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.config import get_settings
from flow_core.crypto import decrypt_secret
from flow_core.services.llm_resolver import get_org_llm_provider

# Curated Scaleway serverless chat models (canonical ``provider/model:quant``
# ids). High-confidence picks across tiers; the exact strings are reconciled
# against live ``/v1/models`` so a stale entry here simply never surfaces.
CURATED_SCALEWAY_MODELS: tuple[str, ...] = (
    "mistral/mistral-small-3.2-24b-instruct-2506:fp8",  # fast/cheap default, EU
    "openai/gpt-oss-120b:fp4",  # frontier-ish general, cheap reasoning
    "qwen/qwen3-235b-a22b-instruct-2507",  # large general + long context
    "google/gemma-3-27b-it:bf16",  # vision (text+image)
    "meta/llama-3.3-70b-instruct:fp8",  # general chat
)


# Curated Scaleway embedding models. Must be able to emit the fleet hosted
# dim (4000): qwen3-embedding-8b is Matryoshka (native 4096) so it can;
# bge-multilingual-gemma2 (3584 fixed) cannot and is intentionally excluded.
CURATED_SCALEWAY_EMBED_MODELS: tuple[str, ...] = ("qwen/qwen3-embedding-8b",)


async def list_live_models(*, api_key: str, base_url: str) -> list[str]:
    """Raw ``GET {base_url}/models`` -> the model id strings Scaleway serves."""
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=15.0) as cx:
        r = await cx.get(f"{base_url.rstrip('/')}/models", headers=headers)
        r.raise_for_status()
        data = r.json()
    rows = data.get("data") or []
    return [str(m["id"]) for m in rows if isinstance(m, dict) and m.get("id")]


async def available_models(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    """Curated roster for the picker, validated against the org's live
    Scaleway endpoint when a key is resolvable.

    Key/endpoint come from the org's stored config (BYOK key + per-row
    base_url) falling back to our settings. With no key resolvable (e.g.
    BYOK-first setup before the key is saved) we return the curated list
    unverified, so the admin can still pick a model and then save+probe.
    """
    cfg = await get_org_llm_provider(session, org_id)
    settings = get_settings()
    own_key = (
        decrypt_secret(cfg.api_key_ciphertext) if cfg and cfg.api_key_ciphertext else None
    )
    api_key = own_key or settings.scaleway_api_key
    base_url = (cfg.base_url if cfg else None) or settings.scaleway_base_url
    if not api_key:
        return list(CURATED_SCALEWAY_MODELS)
    live = set(await list_live_models(api_key=api_key, base_url=base_url))
    return [m for m in CURATED_SCALEWAY_MODELS if m in live]


async def available_embedding_models(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    """Curated embedding roster, validated against the org's live Scaleway
    endpoint (key/base_url from ``org_embedder_provider``, else settings)."""
    from flow_core.services.embedder_resolver import get_org_embedder_provider

    cfg = await get_org_embedder_provider(session, org_id)
    settings = get_settings()
    own_key = decrypt_secret(cfg.api_key_ciphertext) if cfg and cfg.api_key_ciphertext else None
    api_key = own_key or settings.scaleway_api_key
    base_url = (cfg.base_url if cfg else None) or settings.scaleway_base_url
    if not api_key:
        return list(CURATED_SCALEWAY_EMBED_MODELS)
    live = set(await list_live_models(api_key=api_key, base_url=base_url))
    return [m for m in CURATED_SCALEWAY_EMBED_MODELS if m in live]
