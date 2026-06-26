"""Minimal Ollama HTTP client implementing the ``LLMProvider`` Protocol.

Used by the worker's revision-summary sweep when ``MYCELIUM_OLLAMA_URL``
is configured (in-cluster service ``flow-ollama:11434``); the same
provider can be reused by other call sites via
``ai_providers.set_llm_override``. CI keeps the stub
``ai_providers.LocalLLM`` (no network); production wires this class
via ``worker/main.py`` startup.

We hit ``POST /api/chat`` (the modern endpoint) and read the
``message.content`` field. ``stream=False`` keeps the call atomic:
the worker sweep prefers a single response over a streamed one
because it is run in a fixed-size batch loop, not a UI surface.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from mycelium_core.ai_providers import LLMResult

_log = logging.getLogger("flow.llm.ollama")


class OllamaLLM:
    """Concrete ``LLMProvider`` against an Ollama HTTP server."""

    def __init__(self, base_url: str, model: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
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
        payload = {
            "model": self._model,
            "messages": payload_messages,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.post(f"{self._base_url}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        text = (data.get("message") or {}).get("content") or ""
        # Ollama exposes ``prompt_eval_count`` (input tokens) and
        # ``eval_count`` (output tokens) on the response. Fall back to
        # rough character heuristics so callers can still meter.
        tokens_in = int(data.get("prompt_eval_count") or 0)
        tokens_out = int(data.get("eval_count") or 0)
        return LLMResult(
            text=text.strip(),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_id=self._model,
        )
