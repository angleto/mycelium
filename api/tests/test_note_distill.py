"""Distill endpoint (ADR-0034, task 4a718dc4): the fungal-decomposition
trigger surface. The service is unit-tested in core; here we assert the
HTTP adapter wires org/actor, returns the contract, and stays idempotent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_ai import FakeLLM
from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.ai_providers import set_llm_override


@pytest.fixture
def _llm() -> Iterator[None]:
    set_llm_override(FakeLLM)
    try:
        yield
    finally:
        set_llm_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_distill_note_creates_humus_and_is_idempotent(_llm: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
        }
        n = (
            await c.post(
                "/notes",
                headers=h,
                json={
                    "kind": "text",
                    "text": "A finished line of thinking worth distilling.",
                },
            )
        ).json()
        nid = n["id"]

        r = await c.post(f"/notes/{nid}/distill", headers=h)
        assert r.status_code == 200
        first = r.json()
        assert first["created"] is True
        assert first["source_note_id"] == nid
        assert first["distilled_note_id"]
        assert first["model_id"] == "fake-llm"

        # Idempotent: a second call returns the existing distillation
        # untouched, not a second note.
        r2 = await c.post(f"/notes/{nid}/distill", headers=h)
        assert r2.status_code == 200
        second = r2.json()
        assert second["created"] is False
        assert second["distilled_note_id"] == first["distilled_note_id"]

        # The distillation is a real, retrievable note in the workspace.
        d = await c.get(f"/notes/{first['distilled_note_id']}", headers=h)
        assert d.status_code == 200
        assert d.json()["title"].startswith("Distillation")
