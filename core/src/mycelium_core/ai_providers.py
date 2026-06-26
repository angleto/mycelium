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
    async def transcribe(
        self,
        *,
        audio_ref: str,
        audio_seconds: int,
        audio_bytes: bytes | None = None,
        mime_type: str | None = None,
    ) -> TranscriptResult: ...


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
    """Reference STT backed by ``faster-whisper`` when available.

    The extra is optional: if the package is not importable, the
    provider raises a clear error so the caller (notes.transcribe)
    can degrade gracefully (the audio remains playable; the note's
    ``last_error`` records the missing extra). Model/quantisation
    come from the ``MYCELIUM_STT_MODEL`` / ``MYCELIUM_STT_DEVICE`` env vars
    so a self-host can pick `small` (CPU, ~250 MB) or `large-v3`
    (GPU, ~3 GB) without code changes.
    """

    model_id = "local-whisper"
    _model: object | None = None  # cached across calls

    @classmethod
    def _load_model(cls) -> object:
        if cls._model is not None:
            return cls._model
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "LocalSTT requires the 'faster-whisper' extra (pip install faster-whisper)"
            ) from exc
        import os

        size = os.environ.get("MYCELIUM_STT_MODEL", "small")
        device = os.environ.get("MYCELIUM_STT_DEVICE", "cpu")
        compute_type = os.environ.get("MYCELIUM_STT_COMPUTE_TYPE", "int8")
        cls._model = WhisperModel(size, device=device, compute_type=compute_type)
        return cls._model

    async def transcribe(  # pragma: no cover - model/network
        self,
        *,
        audio_ref: str,
        audio_seconds: int,
        audio_bytes: bytes | None = None,
        mime_type: str | None = None,
    ) -> TranscriptResult:
        if audio_bytes is None:
            raise RuntimeError(
                "LocalSTT needs the raw audio bytes; "
                "the caller must resolve audio_ref before invoking."
            )
        import asyncio
        import tempfile
        from pathlib import Path

        def _ext(mt: str | None) -> str:
            if not mt:
                return ".ogg"
            if "mp4" in mt or "m4a" in mt:
                return ".m4a"
            if "webm" in mt:
                return ".webm"
            if "wav" in mt:
                return ".wav"
            return ".ogg"

        suffix = _ext(mime_type)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:

            def _run() -> str:
                model = self._load_model()
                # faster-whisper returns a (segments, info) tuple; we
                # join the segment texts (whitespace-separated) for the
                # full transcript. Italian + auto-detect default; an
                # MYCELIUM_STT_LANG env var can pin the language.
                import os

                lang = os.environ.get("MYCELIUM_STT_LANG") or None
                segments, _ = model.transcribe(  # type: ignore[attr-defined]
                    tmp_path,
                    language=lang,
                    vad_filter=True,
                )
                return " ".join(seg.text.strip() for seg in segments).strip()

            text = await asyncio.to_thread(_run)
        finally:
            # Offload the blocking unlink too (same thread-pool pattern as
            # the transcription above) so the event loop is never blocked.
            await asyncio.to_thread(Path(tmp_path).unlink, missing_ok=True)
        return TranscriptResult(
            text=text,
            model_id=self.model_id,
            audio_seconds=audio_seconds,
        )


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
