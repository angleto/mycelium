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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.ai_providers import (
    set_llm_override,
    set_stt_override,
    set_tts_override,
)
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.services import billing, taxonomy
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


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


async def _structural(
    s: AsyncSession, note_id: uuid.UUID
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """(client tag ids, project tag ids) carried by the note. Returned as
    LISTS, not sets: what docs/adr/0003 fixes is the CARDINALITY, which a
    set would silently collapse back to "looks fine"."""
    rows = (
        await s.execute(
            select(Tag.id, Tag.kind)
            .join(NoteTag, NoteTag.tag_id == Tag.id)
            .where(NoteTag.note_id == note_id)
        )
    ).all()
    return (
        [tag_id for tag_id, kind in rows if kind is TagKind.client],
        [tag_id for tag_id, kind in rows if kind is TagKind.project],
    )


async def _client_and_project(
    s: AsyncSession, *, org: uuid.UUID, user: uuid.UUID, label: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """One client -> project chain named after ``label`` (tag names are
    unique per org+kind, so every chain in a test needs its own)."""
    client = await taxonomy.create_client(
        s,
        org_id=org,
        actor_id=user,
        name=label,
        profile=ClientInput(legal_name=f"{label} SRL"),
    )
    project = await taxonomy.create_project(
        s, org_id=org, actor_id=user, name=f"{label}-proj", client_tag_id=client.id
    )
    return client.id, project.id


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
        # Migration 0016: project lives in the junction, not on the
        # Note row. Assert the project tag is attached.
        n_project = await nt.project_tag_for_note(s, note_id=n.id)
        assert n.kind is NoteKind.conversation and n_project is not None
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
    from mycelium_core.services import memory as memory_svc

    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        # Migration 0016: project_id now references a real Tag row
        # (was a logical FK on the dropped notes.project_id column).
        proj_tag = await taxonomy.create_project(s, org_id=org, actor_id=user, name="memproj")
        proj = proj_tag.id
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
        # Phase 6 final: the body is in note_part(ord=0); read via the
        # canonical helper.
        body = await nt.get_body(s, note_id=done.id)
        assert body and done.status.value == "ready"
        assert after < before  # STT debited
        note_id = note.id
    # The transcript is indexed PER PART by services.note_search at commit
    # (deferred, like task search -- no inline note-level memory write),
    # so retrieval + erase run in a fresh transaction once the indexing
    # flush of the previous one has landed.
    async with tenant_session(str(org), str(user)) as s:
        # Transcript is retrievable from memory within the note's project.
        hits = await memory_svc.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query=body,
            operation_id="tr1-q",
        )
        assert len(hits) >= 1
        # Erase by note: cascades to the per-part search blobs.
        erased = await nt.gdpr_erase_note(s, org_id=org, actor_id=user, note_id=note_id)
        assert erased.memory_blobs_deleted >= 1
        with pytest.raises(NotFoundError):
            await nt.get_note(s, org_id=org, note_id=note_id)


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


async def test_share_via_project_tag_rescopes_memory_without_content_edit(
    _providers: None,
) -> None:
    """Task 1d152747: attaching a project tag to a personal note re-scopes
    its indexed blobs to the project perimeter immediately -- a peer finds it
    without waiting for the next content edit -- and detaching reverts it."""
    from mycelium_core.services import memory as memory_svc

    org, user = await _org("NOTE-SHARE")
    query = "quokka perimeter marker phrase"
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        proj = (await taxonomy.create_project(s, org_id=org, actor_id=user, name="shareproj")).id
        # A personal note (no project); indexed at commit with project_id NULL.
        note = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text=query)
    # Fresh transaction so the deferred indexing flush has landed.
    async with tenant_session(str(org), str(user)) as s:
        in_project = await memory_svc.retrieve(
            s, org_id=org, actor_id=user, project_id=proj, query=query, operation_id="q0"
        )
        assert not in_project  # invisible in the project perimeter while personal
        # Share it: a bare project re-tag, no content edit.
        await nt.attach_tag(s, org_id=org, actor_id=user, note_id=note.id, tag_id=proj)
        shared = await memory_svc.retrieve(
            s, org_id=org, actor_id=user, project_id=proj, query=query, operation_id="q1"
        )
        assert shared  # now retrievable in the project WITHOUT a content edit
        personal = await memory_svc.retrieve(
            s, org_id=org, actor_id=user, project_id=None, query=query, operation_id="q2"
        )
        assert not personal  # and no longer in the personal (NULL) perimeter
        # Un-share: detaching the project tag sends it back to personal.
        await nt.detach_tag(s, org_id=org, actor_id=user, note_id=note.id, tag_id=proj)
        assert not await memory_svc.retrieve(
            s, org_id=org, actor_id=user, project_id=proj, query=query, operation_id="q3"
        )
        assert await memory_svc.retrieve(
            s, org_id=org, actor_id=user, project_id=None, query=query, operation_id="q4"
        )


async def test_attaching_a_project_of_another_client_moves_the_note() -> None:
    """A project tag is a MOVE for notes exactly as it is for tasks: the
    note follows the new project's client atomically (docs/adr/0003,
    services/tag_assignment.move_to_project) instead of accumulating a
    second client, which is what the reported bug produced."""
    org, user = await _org("NOTE-MOVE")
    async with tenant_session(str(org), str(user)) as s:
        c1, p1 = await _client_and_project(s, org=org, user=user, label="Uno")
        c2, p2 = await _client_and_project(s, org=org, user=user, label="Due")
        note = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, project_id=p1)
        assert await _structural(s, note.id) == ([c1], [p1])
        await nt.attach_tag(s, org_id=org, actor_id=user, note_id=note.id, tag_id=p2)
        assert await _structural(s, note.id) == ([c2], [p2])


async def test_detaching_a_notes_client_is_rejected() -> None:
    """A note carries exactly one client whether or not it has a project
    (docs/adr/0021), so dropping it would leave it with no perimeter at
    all: the client is changed by attaching another one, never detached."""
    org, user = await _org("NOTE-CLI")
    async with tenant_session(str(org), str(user)) as s:
        client, project = await _client_and_project(s, org=org, user=user, label="Tre")
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, project_id=project
        )
        note_id = note.id
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await nt.detach_tag(s, org_id=org, actor_id=user, note_id=note_id, tag_id=client)
    assert ei.value.code is MessageCode.TAG_STRUCTURAL_REQUIRED


async def test_unsharing_a_note_keeps_its_client_and_rescopes(_providers: None) -> None:
    """Detaching a note's PROJECT is legal (it is the un-share path, the
    asymmetry a task does not have): the blobs go back to the personal
    NULL perimeter at once, and the note keeps the client the project had
    given it -- projectless is not clientless."""
    from mycelium_core.services import memory as memory_svc

    org, user = await _org("NOTE-UNSHARE")
    query = "pangolin perimeter marker phrase"
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        client, project = await _client_and_project(s, org=org, user=user, label="Qua")
        note = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            project_id=project,
            text=query,
        )
        note_id = note.id
    # Fresh transaction so the deferred indexing flush has landed.
    async with tenant_session(str(org), str(user)) as s:
        assert await memory_svc.retrieve(
            s, org_id=org, actor_id=user, project_id=project, query=query, operation_id="u0"
        )
        await nt.detach_tag(s, org_id=org, actor_id=user, note_id=note_id, tag_id=project)
        assert await _structural(s, note_id) == ([client], [])
        assert not await memory_svc.retrieve(
            s, org_id=org, actor_id=user, project_id=project, query=query, operation_id="u1"
        )
        assert await memory_svc.retrieve(
            s, org_id=org, actor_id=user, project_id=None, query=query, operation_id="u2"
        )


async def test_copying_task_tags_replaces_the_notes_client() -> None:
    """Regression for the reported bug: ``_copy_task_tags_to_note`` used
    to ADD the task's client to a note that already carried one, so a
    work note whose task lived under another client ended up with TWO
    client tags. The pair is now set, not appended.

    The private helper is called directly because every public door
    (get_or_create_work_note / create_note_for_task) creates the note
    under the task's own project, where the two clients cannot disagree
    -- and the disagreement is the case that used to break."""
    org, user = await _org("NOTE-COPY")
    async with tenant_session(str(org), str(user)) as s:
        c1, p1 = await _client_and_project(s, org=org, user=user, label="Cin")
        c2, p2 = await _client_and_project(s, org=org, user=user, label="Sei")
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="owner task", tag_ids=[p2]
        )
        note = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, project_id=p1)
        assert await _structural(s, note.id) == ([c1], [p1])
        await nt._copy_task_tags_to_note(
            s, org_id=org, actor_id=user, note_id=note.id, task_id=task.id
        )
        assert await _structural(s, note.id) == ([c2], [p2])
