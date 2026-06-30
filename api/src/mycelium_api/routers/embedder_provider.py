"""Hosted embedder selection router (task 5276207e).

The embedder mirror of ``routers/llm_provider.py``: surfaces the org's
hosted-embedder choice (local / scaleway), its per-org base_url and BYOK
key, and the curated Scaleway embedding roster validated against live
``GET /v1/models``. Workspace-admin enforced in the service
(``set_org_embedder_provider`` -> ``require_role``); RLS-scoped via
``tenant_ctx``. The stored API key is NEVER returned (only ``has_key``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import EmbedderProviderOut, EmbedderProviderSetIn, ScalewayModelsOut
from mycelium_core.models.org_embedder_provider import EmbedderProviderKind, OrgEmbedderProvider
from mycelium_core.services import embedder_resolver, scaleway

router = APIRouter(prefix="/embedder-provider", tags=["billing"])


def _out(row: OrgEmbedderProvider | None) -> EmbedderProviderOut:
    if row is None:
        return EmbedderProviderOut(
            provider=EmbedderProviderKind.local.value,
            model=None,
            base_url=None,
            has_key=False,
            is_active=True,
            version=0,
        )
    return EmbedderProviderOut(
        provider=row.provider,
        model=row.model,
        base_url=row.base_url,
        has_key=bool(row.api_key_ciphertext),
        is_active=row.is_active,
        version=row.version,
    )


@router.get("", response_model=EmbedderProviderOut)
async def get_provider(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> EmbedderProviderOut:
    row = await embedder_resolver.get_org_embedder_provider(ctx.session, ctx.org_id)
    return _out(row)


@router.put("", response_model=EmbedderProviderOut)
async def set_provider(
    body: EmbedderProviderSetIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> EmbedderProviderOut:
    row = await embedder_resolver.set_org_embedder_provider(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        provider=body.provider,
        model=body.model,
        base_url=body.base_url,
        api_key=body.api_key,
    )
    return _out(row)


@router.get("/scaleway/models", response_model=ScalewayModelsOut)
async def scaleway_embedding_models(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ScalewayModelsOut:
    """Curated Scaleway embedding roster intersected with live ``/v1/models``."""
    models = await scaleway.available_embedding_models(ctx.session, ctx.org_id)
    return ScalewayModelsOut(models=models)
