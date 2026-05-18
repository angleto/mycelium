"""LLM / STT / TTS provider abstractions (docs/adr/0012, 0020).

One pattern, three seams: a Protocol + neutral DTO + injectable
factory each, like the embedder and the email connector. Production
plugs local models (lazily imported optional extras); CI injects
deterministic fakes. Every processing call is metered (ADR-0019);
capture is not.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    model_id: str


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    model_id: str
    audio_seconds: int


@dataclass(frozen=True)
class TtsResult:
    audio_ref: str
    model_id: str
    chars: int


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult: ...


@runtime_checkable
class TranscriptionProvider(Protocol):
    async def transcribe(self, *, audio_ref: str, audio_seconds: int) -> TranscriptResult: ...


@runtime_checkable
class TtsProvider(Protocol):
    async def synthesize(self, *, text: str) -> TtsResult: ...


# --- Reference local implementations (lazy, optional, not CI) ---


class LocalLLM:
    """Reference local LLM. Lazily imported; never loaded in CI."""

    model_id = "local-llm"

    async def complete(  # pragma: no cover - model/network
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        raise RuntimeError("LocalLLM requires the local-inference extra")


class LocalSTT:
    model_id = "local-whisper"

    async def transcribe(  # pragma: no cover - model/network
        self, *, audio_ref: str, audio_seconds: int
    ) -> TranscriptResult:
        raise RuntimeError("LocalSTT requires the 'faster-whisper' extra")


class LocalTTS:
    model_id = "local-tts"

    async def synthesize(self, *, text: str) -> TtsResult:  # pragma: no cover
        raise RuntimeError("LocalTTS requires the local-tts extra")


_llm_override: Callable[[], LLMProvider] | None = None
_stt_override: Callable[[], TranscriptionProvider] | None = None
_tts_override: Callable[[], TtsProvider] | None = None


def set_llm_override(fn: Callable[[], LLMProvider] | None) -> None:
    global _llm_override
    _llm_override = fn


def set_stt_override(fn: Callable[[], TranscriptionProvider] | None) -> None:
    global _stt_override
    _stt_override = fn


def set_tts_override(fn: Callable[[], TtsProvider] | None) -> None:
    global _tts_override
    _tts_override = fn


def get_llm() -> LLMProvider:
    return _llm_override() if _llm_override is not None else LocalLLM()


def get_stt() -> TranscriptionProvider:
    return _stt_override() if _stt_override is not None else LocalSTT()


def get_tts() -> TtsProvider:
    return _tts_override() if _tts_override is not None else LocalTTS()
