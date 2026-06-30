"""F6b MCP co-equality (DB-backed): note/conversation/command tools
reuse the same service layer as REST (docs/adr/0001), provider seams
overridden with deterministic fakes."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_ai import FakeLLM

from mycelium_core.ai_providers import set_llm_override
from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    add_note_tag,
    append_message,
    create_note,
    create_tag,
    get_note,
    grant_credits,
    list_notes,
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


async def test_note_serializers_carry_tags() -> None:
    """Regression: get_note / list_notes surface a note's tags so an MCP
    caller sees them without a separate list_tags round-trip."""
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP6B-tags",
        )
    token, org = r.token, str(r.org_id)
    note = await create_note(token=token, org_id=org, kind="text", text="tagme")
    tag = await create_tag(token=token, org_id=org, kind="generic", name="note-tag")
    await add_note_tag(token=token, org_id=org, note_id=note["id"], tag_id=tag["id"])

    full = await get_note(token=token, org_id=org, note_id=note["id"])
    assert "note-tag" in {g["name"] for g in full["tags"]}
    chip = next(g for g in full["tags"] if g["name"] == "note-tag")
    assert {"id", "kind", "name", "color"} <= chip.keys()

    listed = next(
        n for n in (await list_notes(token=token, org_id=org))["items"] if n["id"] == note["id"]
    )
    assert "note-tag" in {g["name"] for g in listed["tags"]}
