"""LLM provider selection router (task d2c60a83).

Surfaces the org's hosted-provider choice (local / openai / anthropic /
scaleway), the per-org base_url and BYOK key, and the curated Scaleway
roster validated against live ``GET /v1/models``. Workspace-admin is
enforced in the service (``set_org_llm_provider`` -> ``require_role``, the
RBAC choke point); the router is a thin RLS-scoped adapter via
``tenant_ctx``. The stored API key is NEVER returned (only ``has_key``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import LLMProviderOut, LLMProviderSetIn, ScalewayModelsOut
from flow_core.models.org_llm_provider import LLMProviderKind, OrgLLMProvider
from flow_core.services import llm_resolver, scaleway

router = APIRouter(prefix="/llm-provider", tags=["billing"])


def _out(row: OrgLLMProvider | None) -> LLMProviderOut:
    if row is None:
        return LLMProviderOut(
            provider=LLMProviderKind.local.value,
            model=None,
            base_url=None,
            has_key=False,
            is_active=True,
            version=0,
        )
    return LLMProviderOut(
        provider=row.provider,
        model=row.model,
        base_url=row.base_url,
        has_key=bool(row.api_key_ciphertext),
        is_active=row.is_active,
        version=row.version,
    )


@router.get("", response_model=LLMProviderOut)
async def get_provider(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> LLMProviderOut:
    row = await llm_resolver.get_org_llm_provider(ctx.session, ctx.org_id)
    return _out(row)


@router.put("", response_model=LLMProviderOut)
async def set_provider(
    body: LLMProviderSetIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> LLMProviderOut:
    row = await llm_resolver.set_org_llm_provider(
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
async def scaleway_models(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ScalewayModelsOut:
    """Curated Scaleway roster intersected with the org's live ``/v1/models``."""
    models = await scaleway.available_models(ctx.session, ctx.org_id)
    return ScalewayModelsOut(models=models)
