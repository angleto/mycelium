"""API-process local LLM override wiring (task T5).

The lifespan installs the bundled Ollama provider as the rank-0 env
fallback when configured and clears it on shutdown, mirroring the worker
but adding the teardown the worker omits. Hosted per-org selection is NOT
here (that is resolve_llm, task 8afda4e7).
"""

from __future__ import annotations

from types import SimpleNamespace

from mycelium_api.app import _wire_local_llm_override
from mycelium_core.ai_providers import LocalLLM, get_llm, set_llm_override
from mycelium_core.llm_ollama import OllamaLLM


def test_wire_local_llm_override_installs_ollama_when_configured() -> None:
    settings = SimpleNamespace(ollama_url="http://mycelium-ollama:11434", open_model="llama3.2:3b")
    try:
        assert _wire_local_llm_override(settings) is True
        provider = get_llm()
        assert isinstance(provider, OllamaLLM)
        assert provider.model_id == "llama3.2:3b"
    finally:
        set_llm_override(None)


def test_wire_local_llm_override_noop_when_unset_and_later_override_wins() -> None:
    set_llm_override(None)
    settings = SimpleNamespace(ollama_url="", open_model="")
    assert _wire_local_llm_override(settings) is False
    # Unset -> no override, the default LocalLLM stub stays.
    assert isinstance(get_llm(), LocalLLM)

    # A CI/scripted override installed AFTER the lifespan wiring still wins
    # (set_llm_override is last-writer; the lifespan never clobbers it).
    class _Scripted:
        model_id = "scripted"

    set_llm_override(_Scripted)
    try:
        assert get_llm().model_id == "scripted"
    finally:
        set_llm_override(None)
