"""F6b notes / conversation / canonical intent (DB-backed), ADR-0020/
0021 + FR-16. Deterministic fake STT/TTS/LLM + embedder seams.

Covers: canonical command deterministic + offline + unmetered, project
slot resolution (exact / unambiguous fuzzy / ambiguous -> clarify, no
mis-scope), metered transcription feeding memory with provenance,
erasure cascade, metered conversation turn, metered TTS, isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from _fake_ai import FakeLLM, FakeSTT, FakeTTS
from _fake_embedder import FakeEmbedder

from flow_core.ai_providers import (
    set_llm_override,
    set_stt_override,
    set_tts_override,
)
from flow_core.db import admin_session, tenant_session
from flow_core.embedder import set_embedder_override
from flow_core.errors import DomainError, NotFoundError
from flow_core.models.note import NoteKind
from flow_core.services import billing, taxonomy
from flow_core.services import notes as nt
from flow_core.services.auth import signup


@pytest.fixture
def _providers() -> Iterator[None]:
    set_llm_override(FakeLLM)
    set_stt_override(FakeSTT)
    set_tts_override(FakeTTS)
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_llm_override(None)
        set_stt_override(None)
        set_tts_override(None)
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org(name: str = "NOTE") -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name=name)
    return r.org_id, r.user_id


async def _seed_billing(s, org: uuid.UUID, user: uuid.UUID) -> None:
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))
    for model in ("fake-stt", "fake-llm", "fake-tts", FakeEmbedder.model_id):
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id=model,
            provider="local",
            values={
                "credits_per_input": Decimal("0.001"),
                "credits_per_output": Decimal("0.001"),
            },
        )


async def test_canonical_command_is_deterministic_and_unmetered() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        # No billing seeded at all: capture/command must still work.
        n = await nt.run_command(s, org_id=org, actor_id=user, text="crea una nuova nota")
        assert n.kind is NoteKind.text
        with pytest.raises(DomainError):
            await nt.run_command(s, org_id=org, actor_id=user, text="totally unrelated")


async def test_project_slot_resolution(_providers: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await taxonomy.create_project(s, org_id=org, actor_id=user, name="bitvision")
        n = await nt.run_command(
            s,
            org_id=org,
            actor_id=user,
            text="facciamo una conversazione su una nuova sessione nel progetto bitvision",
        )
        assert n.kind is NoteKind.conversation and n.project_id is not None
        # Unknown project -> clarify, never mis-scope.
        with pytest.raises(NotFoundError):
            await nt.run_command(
                s, org_id=org, actor_id=user, text="crea una nota nel progetto ghost"
            )
        # Ambiguous fuzzy -> clarify.
        await taxonomy.create_project(s, org_id=org, actor_id=user, name="alpha one")
        await taxonomy.create_project(s, org_id=org, actor_id=user, name="alpha two")
        with pytest.raises(DomainError):
            await nt.run_command(
                s, org_id=org, actor_id=user, text="crea una nota nel progetto alpha"
            )


async def test_transcription_metered_feeds_memory_and_erases(
    _providers: None,
) -> None:
    from flow_core.services import memory as memory_svc

    org, user = await _org()
    proj = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        note = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.voice,
            project_id=proj,
            audio_ref="s3://audio/a.webm",
            audio_seconds=120,
        )
        before = await billing.balance(s, org_id=org)
        done = await nt.transcribe(
            s, org_id=org, actor_id=user, note_id=note.id, operation_id="tr1"
        )
        after = await billing.balance(s, org_id=org)
        assert done.transcript and done.status.value == "ready"
        assert after < before  # STT (+ embedding) debited
        # Transcript is retrievable from memory within the note's project.
        hits = await memory_svc.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query=done.transcript,
            operation_id="tr1-q",
        )
        assert len(hits) >= 1
        # Erase by note: cascades to the provenance-linked memory blob.
        erased = await nt.gdpr_erase_note(s, org_id=org, actor_id=user, note_id=note.id)
        assert erased.memory_blobs_deleted >= 1
        with pytest.raises(NotFoundError):
            await nt.get_note(s, org_id=org, note_id=note.id)


async def test_conversation_turn_is_metered(_providers: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        conv = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.conversation)
        before = await billing.balance(s, org_id=org)
        reply = await nt.append_message(
            s,
            org_id=org,
            actor_id=user,
            note_id=conv.id,
            content="what can I do now?",
            operation_id="m1",
        )
        after = await billing.balance(s, org_id=org)
        turns = await nt.list_turns(s, org_id=org, note_id=conv.id)
    assert reply.role.value == "assistant" and reply.content.startswith("echo:")
    assert [t.role.value for t in turns] == ["user", "assistant"]
    assert after < before


async def test_tts_is_metered(_providers: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        before = await billing.balance(s, org_id=org)
        res = await nt.synthesize(
            s, org_id=org, actor_id=user, text="hello world", operation_id="s1"
        )
        after = await billing.balance(s, org_id=org)
    assert res["audio_ref"] == "s3://tts/out.wav"
    assert after < before


async def test_notes_org_isolation(_providers: None) -> None:
    a_org, a_user = await _org("NOTE-A")
    b_org, b_user = await _org("NOTE-B")
    async with tenant_session(str(a_org), str(a_user)) as s:
        n = await nt.create_note(
            s, org_id=a_org, actor_id=a_user, kind=NoteKind.text, text="secret"
        )
    async with tenant_session(str(b_org), str(b_user)) as s:
        with pytest.raises(NotFoundError):
            await nt.get_note(s, org_id=b_org, note_id=n.id)
