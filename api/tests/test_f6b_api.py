"""F6b API end-to-end (DB-backed): note capture, canonical command,
metered transcription + conversation, cross-org isolation. Provider
seams overridden with deterministic fakes."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_ai import FakeLLM, FakeSTT
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.ai_providers import set_llm_override, set_stt_override
from flow_core.embedder import set_embedder_override


@pytest.fixture
def _providers() -> Iterator[None]:
    set_llm_override(FakeLLM)
    set_stt_override(FakeSTT)
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_llm_override(None)
        set_stt_override(None)
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f6b_api_flow(_providers: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "org_name": "A"},
            )
        ).json()
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "org_name": "B"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Org-Id": a["org_id"]}

        # Canonical command works before any billing (unmetered/offline).
        cmd = await c.post("/notes/command", headers=h, json={"text": "crea una nuova nota"})
        assert cmd.status_code == 200 and cmd.json()["kind"] == "text"

        await c.post("/billing/grant", headers=h, json={"amount": "100"})
        for m in ("fake-stt", "fake-llm", FakeEmbedder.model_id):
            await c.post(
                "/billing/rate-cards",
                headers=h,
                json={
                    "model_id": m,
                    "provider": "local",
                    "credits_per_input": "0.001",
                    "credits_per_output": "0.001",
                },
            )

        voice = (
            await c.post(
                "/notes",
                headers=h,
                json={
                    "kind": "voice",
                    "audio_ref": "s3://audio/a.webm",
                    "audio_seconds": 90,
                },
            )
        ).json()
        tr = await c.post(
            f"/notes/{voice['id']}/transcribe",
            headers=h,
            json={"operation_id": "tr1"},
        )
        assert tr.status_code == 200 and tr.json()["status"] == "ready"
        assert tr.json()["transcript"]

        conv = (await c.post("/notes/conversations", headers=h, json={})).json()
        msg = await c.post(
            f"/notes/{conv['id']}/messages",
            headers=h,
            json={"content": "hello", "operation_id": "m1"},
        )
        assert msg.status_code == 200 and msg.json()["role"] == "assistant"

        cross = await c.post(
            "/notes/command",
            headers={"Authorization": f"Bearer {a['token']}", "X-Org-Id": b["org_id"]},
            json={"text": "crea una nuova nota"},
        )
        assert cross.status_code == 403
