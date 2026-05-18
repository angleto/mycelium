"""F6b MCP co-equality (DB-backed): note/conversation/command tools
reuse the same service layer as REST (docs/adr/0001), provider seams
overridden with deterministic fakes."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_ai import FakeLLM

from flow_core.ai_providers import set_llm_override
from flow_core.db import admin_session
from flow_core.services.auth import signup
from flow_mcp.server import (
    append_message,
    create_note,
    grant_credits,
    run_command,
    start_conversation_session,
    upsert_rate_card,
)


@pytest.fixture
def _llm() -> Iterator[None]:
    set_llm_override(FakeLLM)
    try:
        yield
    finally:
        set_llm_override(None)


async def test_mcp_notes(_llm: None) -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP6B",
        )
    token, org = r.token, str(r.org_id)

    # Canonical command is deterministic + unmetered (works before billing).
    cmd = await run_command(token=token, org_id=org, text="crea una nuova nota")
    assert cmd["kind"] == "text"

    n = await create_note(token=token, org_id=org, kind="text", text="hi")
    assert n["status"] == "ready"

    await grant_credits(token=token, org_id=org, amount=100.0)
    await upsert_rate_card(
        token=token,
        org_id=org,
        model_id="fake-llm",
        provider="local",
        credits_per_input=0.001,
        credits_per_output=0.001,
    )
    conv = await start_conversation_session(token=token, org_id=org)
    reply = await append_message(
        token=token,
        org_id=org,
        note_id=conv["id"],
        content="hello",
        operation_id="m1",
    )
    assert reply["role"] == "assistant" and reply["content"].startswith("echo:")
