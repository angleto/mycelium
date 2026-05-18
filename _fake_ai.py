"""Deterministic in-memory LLM/STT/TTS for tests (ADR-0012/0020 seam).
Repo-root module so both core/tests and api/tests can import it (the
root conftest puts this dir on sys.path)."""

from __future__ import annotations

from collections.abc import Sequence

from flow_core.ai_providers import LLMResult, TranscriptResult, TtsResult


class FakeLLM:
    model_id = "fake-llm"

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        last = messages[-1][1] if messages else ""
        return LLMResult(
            text=f"echo: {last}",
            tokens_in=sum(len(c.split()) for _, c in messages),
            tokens_out=max(1, len(last.split())),
            model_id=self.model_id,
        )


class FakeSTT:
    model_id = "fake-stt"

    async def transcribe(self, *, audio_ref: str, audio_seconds: int) -> TranscriptResult:
        return TranscriptResult(
            text=f"transcript of {audio_ref}",
            model_id=self.model_id,
            audio_seconds=audio_seconds,
        )


class FakeTTS:
    model_id = "fake-tts"

    async def synthesize(self, *, text: str) -> TtsResult:
        return TtsResult(
            audio_ref="s3://tts/out.wav",
            model_id=self.model_id,
            chars=len(text),
        )
