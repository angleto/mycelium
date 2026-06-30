"""Minimal Anthropic Messages client implementing ``LLMProvider``.

httpx-only (no ``anthropic`` SDK dependency), same shape as
:mod:`mycelium_core.llm_ollama`. Anthropic differs from the OpenAI shape in
two ways the adapter absorbs: ``system`` is a top-level field (not a
message role) and ``max_tokens`` is required. Token counts come from the
``usage`` block (``input_tokens`` / ``output_tokens``) so the metering
seam charges real tokens.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from mycelium_core.ai_providers import LLMResult


class AnthropicLLM:
    """Concrete ``LLMProvider`` against the Anthropic Messages API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        version: str = "2023-06-01",
        max_tokens: int = 1024,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._version = version
        self._max_tokens = max_tokens
        self._timeout = timeout

    @property
    def model_id(self) -> str:
        return self._model

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        payload: dict[str, object] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": role, "content": content} for role, content in messages],
        }
        if system:
            payload["system"] = system
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._version,
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.post(f"{self._base_url}/v1/messages", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        # ``content`` is a list of typed blocks; concatenate the text ones.
        blocks = data.get("content") or []
        text = "".join(
            b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        return LLMResult(
            text=text.strip(),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_id=self._model,
        )
