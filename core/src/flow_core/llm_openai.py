"""Minimal OpenAI Chat Completions client implementing ``LLMProvider``.

httpx-only (no ``openai`` SDK dependency), same shape as
:mod:`flow_core.llm_ollama`. Selected per-org by
``services.llm_resolver.resolve_provider`` when an org configures a
hosted ``openai`` provider; the key is either the org's own (BYOK) or
ours (``settings.openai_api_key``, the "on our key" mode). Token counts
come from the API ``usage`` block so the metering seam (MeteredLLM)
charges real input/output tokens.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from flow_core.ai_providers import LLMResult


class OpenAILLM:
    """Concrete ``LLMProvider`` against the OpenAI Chat Completions API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        max_tokens: int | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        # Optional output cap. Used by the fail-closed key probe to keep the
        # validation call minimal; ``None`` leaves it unset (provider default).
        self._max_tokens = max_tokens
        self._timeout = timeout

    @property
    def model_id(self) -> str:
        return self._model

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        for role, content in messages:
            payload_messages.append({"role": role, "content": content})
        payload: dict[str, object] = {
            "model": self._model,
            "messages": payload_messages,
            "stream": False,
        }
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.post(f"{self._base_url}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        choices = data.get("choices") or []
        text = ((choices[0] if choices else {}).get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        return LLMResult(
            text=text.strip(),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_id=self._model,
        )
