"""Hosted LLM providers + per-org resolver (task 8afda4e7).

The OpenAI/Anthropic adapters are httpx-only (no SDK); respx fakes the
HTTP so we assert the request shape and that real ``usage`` token counts
flow into ``LLMResult`` (what the metering seam later charges on). The
resolver maps an org's stored config to a concrete provider + the
``CostBasis`` to bill it on, degrading to the local seam when no key is
resolvable.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import respx
from httpx import Response

from flow_core.ai_providers import LocalLLM
from flow_core.config import get_settings
from flow_core.crypto import decrypt_secret
from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.llm_anthropic import AnthropicLLM
from flow_core.llm_openai import OpenAILLM
from flow_core.models.billing import CostBasis
from flow_core.services import llm_resolver, scaleway
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _fake_settings(**over: object) -> SimpleNamespace:
    s = get_settings()
    base: dict[str, object] = {
        "openai_api_key": "",
        "openai_base_url": s.openai_base_url,
        "anthropic_api_key": "",
        "anthropic_base_url": s.anthropic_base_url,
        "anthropic_version": s.anthropic_version,
        "scaleway_api_key": "",
        "scaleway_base_url": s.scaleway_base_url,
    }
    base.update(over)
    return SimpleNamespace(**base)


@respx.mock
async def test_openai_llm_parses_text_and_usage() -> None:
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": " hi there "}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            },
        )
    )
    res = await OpenAILLM(api_key="sk-test", model="gpt-4o-mini").complete(
        system="be brief", messages=[("user", "hello")]
    )
    assert res.text == "hi there"
    assert res.tokens_in == 11
    assert res.tokens_out == 5
    assert res.model_id == "gpt-4o-mini"
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer sk-test"


@respx.mock
async def test_anthropic_llm_parses_text_and_usage() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "part one "},
                    {"type": "tool_use", "id": "x"},
                    {"type": "text", "text": "part two"},
                ],
                "usage": {"input_tokens": 20, "output_tokens": 8},
            },
        )
    )
    res = await AnthropicLLM(api_key="ak-test", model="claude-3-5-haiku-latest").complete(
        system="be brief", messages=[("user", "hello")]
    )
    assert res.text == "part one part two"
    assert res.tokens_in == 20
    assert res.tokens_out == 8
    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == "ak-test"
    assert sent.headers["anthropic-version"] == get_settings().anthropic_version


async def test_resolve_provider_local_default() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        provider, basis = await llm_resolver.resolve_provider(s, org)
    assert basis is CostBasis.local
    assert isinstance(provider, LocalLLM)


async def test_resolve_provider_openai_byok_uses_own_key() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        row = await llm_resolver.set_org_llm_provider(
            s,
            org_id=org,
            actor_id=user,
            provider="openai",
            model="gpt-4o",
            api_key="sk-org-secret",
            validate_key=False,
        )
        # Stored key is encrypted at rest, recoverable via the envelope.
        assert row.api_key_ciphertext and row.api_key_ciphertext != "sk-org-secret"
        assert decrypt_secret(row.api_key_ciphertext) == "sk-org-secret"
        provider, basis = await llm_resolver.resolve_provider(s, org)
    assert isinstance(provider, OpenAILLM)
    assert provider.model_id == "gpt-4o"
    assert basis is CostBasis.byok


async def test_resolve_provider_our_key_when_no_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm_resolver, "get_settings", lambda: _fake_settings(openai_api_key="sk-ours")
    )
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await llm_resolver.set_org_llm_provider(
            s, org_id=org, actor_id=user, provider="openai", model="gpt-4o-mini"
        )
        provider, basis = await llm_resolver.resolve_provider(s, org)
    assert isinstance(provider, OpenAILLM)
    assert basis is CostBasis.our_key


async def test_resolve_provider_hosted_no_key_degrades_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_resolver, "get_settings", lambda: _fake_settings())
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await llm_resolver.set_org_llm_provider(
            s, org_id=org, actor_id=user, provider="anthropic", model="claude-3-5-haiku-latest"
        )
        provider, basis = await llm_resolver.resolve_provider(s, org)
    assert isinstance(provider, LocalLLM)
    assert basis is CostBasis.local


async def test_set_org_llm_provider_update_preserves_key_when_omitted() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await llm_resolver.set_org_llm_provider(
            s,
            org_id=org,
            actor_id=user,
            provider="openai",
            model="gpt-4o",
            api_key="sk-keep",
            validate_key=False,
        )
        # Update model only (api_key omitted) -> stored key untouched.
        row = await llm_resolver.set_org_llm_provider(
            s, org_id=org, actor_id=user, provider="openai", model="gpt-4o-mini"
        )
        assert row.api_key_ciphertext and decrypt_secret(row.api_key_ciphertext) == "sk-keep"
        assert row.model == "gpt-4o-mini"


async def test_resolve_provider_scaleway_byok_reuses_openai_client() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await llm_resolver.set_org_llm_provider(
            s,
            org_id=org,
            actor_id=user,
            provider="scaleway",
            model="mistral/mistral-small-3.2-24b-instruct-2506:fp8",
            api_key="scw-org-secret",
            validate_key=False,
        )
        provider, basis = await llm_resolver.resolve_provider(s, org)
    # Scaleway is OpenAI-compatible -> reuse OpenAILLM with the Scaleway URL.
    assert isinstance(provider, OpenAILLM)
    assert provider.model_id == "mistral/mistral-small-3.2-24b-instruct-2506:fp8"
    assert provider._base_url == get_settings().scaleway_base_url.rstrip("/")
    assert basis is CostBasis.byok


async def test_resolve_provider_scaleway_our_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm_resolver, "get_settings", lambda: _fake_settings(scaleway_api_key="scw-ours")
    )
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await llm_resolver.set_org_llm_provider(
            s, org_id=org, actor_id=user, provider="scaleway", validate_key=False
        )
        provider, basis = await llm_resolver.resolve_provider(s, org)
    assert isinstance(provider, OpenAILLM)
    assert provider.model_id == llm_resolver._DEFAULT_SCALEWAY_MODEL
    assert basis is CostBasis.our_key


async def test_resolve_provider_per_row_base_url_override() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    custom = "https://api.scaleway.ai/proj-1234/v1"
    async with tenant_session(str(org), str(user)) as s:
        await llm_resolver.set_org_llm_provider(
            s,
            org_id=org,
            actor_id=user,
            provider="scaleway",
            base_url=custom,
            api_key="scw-org-secret",
            validate_key=False,
        )
        provider, _ = await llm_resolver.resolve_provider(s, org)
    assert isinstance(provider, OpenAILLM)
    assert provider._base_url == custom


@respx.mock
async def test_set_org_llm_provider_probe_rejects_bad_key() -> None:
    respx.post("https://api.scaleway.ai/v1/chat/completions").mock(
        return_value=Response(401, json={"error": "invalid api key"})
    )
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError) as exc:
            await llm_resolver.set_org_llm_provider(
                s,
                org_id=org,
                actor_id=user,
                provider="scaleway",
                model="mistral/mistral-small-3.2-24b-instruct-2506:fp8",
                api_key="scw-bad",
            )
        assert exc.value.code is MessageCode.PROVIDER_KEY_INVALID
        # Nothing persisted active for the org.
        assert await llm_resolver.get_org_llm_provider(s, org) is None


@respx.mock
async def test_set_org_llm_provider_probe_accepts_good_key() -> None:
    route = respx.post("https://api.scaleway.ai/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        row = await llm_resolver.set_org_llm_provider(
            s,
            org_id=org,
            actor_id=user,
            provider="scaleway",
            model="mistral/mistral-small-3.2-24b-instruct-2506:fp8",
            api_key="scw-good",
        )
    assert row.api_key_ciphertext and decrypt_secret(row.api_key_ciphertext) == "scw-good"
    # The probe sent a minimal (max_tokens=1) call on the candidate key.
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer scw-good"


@respx.mock
async def test_available_models_intersects_curated_roster() -> None:
    # Live /v1/models returns a couple curated ids plus an unlisted extra.
    live_ids = [
        "openai/gpt-oss-120b:fp4",
        "meta/llama-3.3-70b-instruct:fp8",
        "some/uncurated-model:fp8",
    ]
    respx.get("https://api.scaleway.ai/v1/models").mock(
        return_value=Response(200, json={"data": [{"id": i} for i in live_ids]})
    )
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LLM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await llm_resolver.set_org_llm_provider(
            s, org_id=org, actor_id=user, provider="scaleway", api_key="scw-k", validate_key=False
        )
        models = await scaleway.available_models(s, org)
    # Only curated ∩ live, in curated order; the uncurated extra is dropped.
    assert models == ["openai/gpt-oss-120b:fp4", "meta/llama-3.3-70b-instruct:fp8"]
