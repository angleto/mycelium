"""The note indexer chunks a long part, and the whole index lifecycle
holds at 1:N (task 11154e32).

The obvious test, "a long part produces more than one vector", is not the
gate: it stays green on every broken way of writing N blobs, because the
damage of the broken ways is in what happens to the OTHER chunks
afterwards. These six are the gate. Each one is a lifecycle path that
used to be written against exactly one blob per part:

1. the tail is retrievable AND routable (a chunk with no pointer row is
   fetched by the retrieve and then dropped in silence by the resolver);
2. deleting the part leaves no blob and no provenance behind;
3. an edit that moves the chunk boundaries leaves no pre-edit text;
4. the project perimeter and a merge reach every chunk, not the head;
5. the commit pays a bounded number of embed calls and a bounded wait;
6. the backfill that re-chunks the back-catalogue is idempotent.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import delete, func, select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embed_dims import EMBED_DIM
from mycelium_core.embedder import EmbedResult, EmbedSide, set_embedder_override
from mycelium_core.models.memory_blob import BlobSource, MemoryBlob, MemoryBlobTag
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.tag import TagKind
from mycelium_core.services import note_parts as np
from mycelium_core.services import note_search, task_search, taxonomy
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup
from mycelium_core.services.chunker import get_chunk_threshold_tokens

HEAD_TOKEN = "zulqarnain"
TAIL_TOKEN = "xyzzyplugh"


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="NCHUNK")
    return r.org_id, r.user_id


def _long_body(*, head: str = HEAD_TOKEN, tail: str = TAIL_TOKEN, paragraphs: int = 30) -> str:
    """A part well above the chunker threshold, with one token that occurs
    ONLY in the first paragraph and one that occurs ONLY in the last.

    Paragraph-shaped on purpose: the chunker packs on blank-line
    boundaries, so a single block of words would exercise the sliding
    window instead of the path a written note takes."""
    blocks = [f"{head} apertura del documento indicizzato."]
    for i in range(paragraphs):
        blocks.append(" ".join(f"riempitivo{(i * 7 + j) % 53}" for j in range(60)))
    blocks.append(f"{tail} chiusura del documento indicizzato.")
    return "\n\n".join(blocks)


async def _pointers(s, part_id: uuid.UUID) -> list[NotePartIndexPointer]:
    return list(
        (
            await s.execute(
                select(NotePartIndexPointer)
                .where(NotePartIndexPointer.part_id == part_id)
                .order_by(NotePartIndexPointer.chunk_index)
            )
        )
        .scalars()
        .all()
    )


async def _texts(s, part_id: uuid.UUID) -> list[str]:
    rows = (
        await s.execute(
            select(MemoryBlob.text)
            .join(NotePartIndexPointer, NotePartIndexPointer.blob_id == MemoryBlob.id)
            .where(NotePartIndexPointer.part_id == part_id)
            .order_by(NotePartIndexPointer.chunk_index)
        )
    ).all()
    return [t or "" for (t,) in rows]


async def _long_part(org: uuid.UUID, user: uuid.UUID, body: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="intestazione"
        )
        part = await np.create_part(s, org_id=org, actor_id=user, note_id=note.id, body=body)
        return note.id, part.id


# ------------------------------------------------------------------ 1


async def test_the_tail_of_a_long_part_is_retrievable_and_routable(_embedder: None) -> None:
    """A query matching only the LAST paragraph returns the part, with a
    note_id and a part_id a caller can follow.

    Routable is the half that the vector count cannot see: the unified
    search drops every note-channel blob that does not resolve to a live
    pointer row, so an implementation that writes N blobs and one pointer
    retrieves the tail and then discards it without a word."""
    org, user = await _org()
    note_id, part_id = await _long_part(org, user, _long_body())

    async with tenant_session(str(org), str(user)) as s:
        pointers = await _pointers(s, part_id)
        assert len(pointers) > 1, "a part above the threshold must be several chunks"
        texts = await _texts(s, part_id)
        assert TAIL_TOKEN not in texts[0], "the tail must not already be in the head chunk"
        assert any(TAIL_TOKEN in t for t in texts[1:])

        hits = await task_search.search_unified(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=TAIL_TOKEN,
            kinds=["note"],
            tag_ids=[],
            channel_keys=[],
            limit=5,
            include_archived=False,
            include_deleted=False,
            operation_id=f"op-{uuid.uuid4().hex}",
        )
    matched = [h for h in hits if h.note_id == note_id]
    assert matched, f"the tail did not come back from search: {hits}"
    assert matched[0].part_id == part_id


# ------------------------------------------------------------------ 2


async def test_deleting_a_long_part_leaves_no_blob_and_no_provenance(_embedder: None) -> None:
    """Zero ``memory_blobs`` and zero ``blob_sources`` for the part.

    The pre-existing deletion test used a body of two words, which never
    chunks, and asserted only that the ONE blob the pointer named was
    gone: it was green against an indexer that left chunks 1..N-1 behind
    forever."""
    org, user = await _org()
    _note_id, part_id = await _long_part(org, user, _long_body())

    async with tenant_session(str(org), str(user)) as s:
        assert len(await _pointers(s, part_id)) > 1
        await np.delete_part(s, org_id=org, actor_id=user, part_id=part_id)

    async with tenant_session(str(org), str(user)) as s:
        assert await _pointers(s, part_id) == []
        sources = (
            await s.execute(
                select(func.count())
                .select_from(BlobSource)
                .where(BlobSource.source_kind == "note_part", BlobSource.source_id == str(part_id))
            )
        ).scalar_one()
        assert sources == 0
        orphans = (
            await s.execute(
                select(func.count())
                .select_from(MemoryBlob)
                .where(MemoryBlob.namespace == "note", MemoryBlob.text.like(f"%{TAIL_TOKEN}%"))
            )
        ).scalar_one()
        assert orphans == 0


# ------------------------------------------------------------------ 3


async def test_an_edit_that_moves_the_boundaries_leaves_no_pre_edit_text(
    _embedder: None,
) -> None:
    """After an edit that shortens the part past a chunk boundary, no blob
    of the part carries text from before it.

    The single-blob update path wrote one blob and stopped, and the
    content-hash short circuit meant the others were never visited again:
    the tail of the previous version stayed indexed and retrievable for
    the life of the part."""
    org, user = await _org()
    _note_id, part_id = await _long_part(org, user, _long_body(paragraphs=30))

    async with tenant_session(str(org), str(user)) as s:
        before = len(await _pointers(s, part_id))
        assert before > 2
        parts = await np.list_parts(s, org_id=org, note_id=_note_id)
        version = next(p.version for p in parts if p.id == part_id)

    async with tenant_session(str(org), str(user)) as s:
        await np.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=part_id,
            expected_version=version,
            body=_long_body(head="riscrittura", tail="terminazione", paragraphs=8),
        )

    async with tenant_session(str(org), str(user)) as s:
        texts = await _texts(s, part_id)
        assert texts, "the part must stay indexed"
        assert len(texts) < before, "the shorter text must own fewer chunks"
        joined = "\n".join(texts)
        assert HEAD_TOKEN not in joined
        assert TAIL_TOKEN not in joined
        assert "terminazione" in joined
        # And nothing survived outside the pointer set either: a blob whose
        # pointer row was deleted without its blob would be unreachable
        # from _texts and still retrievable by a search.
        stale = (
            await s.execute(
                select(func.count())
                .select_from(MemoryBlob)
                .where(MemoryBlob.text.like(f"%{TAIL_TOKEN}%"))
            )
        ).scalar_one()
        assert stale == 0


# ------------------------------------------------------------------ 4


async def test_the_perimeter_reaches_every_chunk(_embedder: None) -> None:
    """Tagging the note with a project, and then merging it into another
    note, must move EVERY chunk. Both paths used to resolve one blob per
    part through the pointer, so on a long part they moved the head and
    left the rest scoped to the old perimeter, where a project-scoped
    search of the other project would still find them."""
    org, user = await _org()
    note_id, part_id = await _long_part(org, user, _long_body())

    async with tenant_session(str(org), str(user)) as s:
        project = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name=f"proj-{uuid.uuid4().hex[:8]}"
        )
        await nt.attach_tag(s, org_id=org, actor_id=user, note_id=note_id, tag_id=project.id)
        project_id = project.id

    async with tenant_session(str(org), str(user)) as s:
        rows = (
            await s.execute(
                select(MemoryBlob.project_id)
                .join(NotePartIndexPointer, NotePartIndexPointer.blob_id == MemoryBlob.id)
                .where(NotePartIndexPointer.part_id == part_id)
            )
        ).all()
        assert len(rows) > 1
        assert all(pid == project_id for (pid,) in rows), rows

    async with tenant_session(str(org), str(user)) as s:
        target = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="destinazione del merge"
        )
        target_id = target.id
    async with tenant_session(str(org), str(user)) as s:
        await np.merge_notes(
            s, org_id=org, actor_id=user, source_note_id=note_id, target_note_id=target_id
        )

    async with tenant_session(str(org), str(user)) as s:
        pointers = await _pointers(s, part_id)
        assert len(pointers) > 1
        assert {p.note_id for p in pointers} == {target_id}


# ------------------------------------------------------------------ 5


class _CountingEmbedder:
    """Counts calls and sleeps on each one: the two things criterion 5 has
    to bound. A counter alone is green under a deadline-only budget, and a
    delay alone is green under a call cap, so both are needed to tell the
    two halves apart."""

    model_id = "counting-embed"
    calls = 0
    # Large enough that a per-chunk budget and a per-part budget produce
    # different numbers of vectors: at 0.3 s only a handful of chunks fit
    # inside a 2 s budget for the whole part, while a budget granted per
    # chunk would pay for every one of them up to the call cap.
    delay = 0.3

    async def embed(self, text: str, *, side: EmbedSide) -> EmbedResult:
        del side
        type(self).calls += 1
        await asyncio.sleep(type(self).delay)
        vec = [0.0] * EMBED_DIM
        vec[len(text) % EMBED_DIM] = 1.0
        return EmbedResult(vector=vec, model_id=self.model_id, tokens=1)


async def test_the_commit_pays_a_bounded_embed_budget() -> None:
    """A part far above the threshold commits within the part's budget.

    ``flush_note_search_dirty`` runs in the pre-commit hook of every
    tenant transaction, so an embed cost that scales with the document is
    not a slow test, it is an incident: one imported note would hold a
    write transaction open for as long as the encoder needs for its whole
    text."""
    set_embedder_override(_CountingEmbedder)
    _CountingEmbedder.calls = 0
    try:
        org, user = await _org()
        loop = asyncio.get_running_loop()
        started = loop.time()
        _note_id, part_id = await _long_part(org, user, _long_body(paragraphs=120))
        elapsed = loop.time() - started
    finally:
        set_embedder_override(None)

    async with tenant_session(str(org), str(user)) as s:
        pointers = await _pointers(s, part_id)
    assert len(pointers) > note_search._EMBED_MAX_CHUNKS, (
        "the fixture must produce more chunks than the budget can embed, "
        "or the bound is not under test"
    )
    # THE CALL COUNT. The note's other part ("intestazione") is embedded
    # too, hence the +1: the budget is granted per part.
    assert _CountingEmbedder.calls <= note_search._EMBED_MAX_CHUNKS + 1

    async with tenant_session(str(org), str(user)) as s:
        embedded, keyword_only = (
            await s.execute(
                select(
                    func.count().filter(MemoryBlob.embedding.is_not(None)),
                    func.count().filter(MemoryBlob.embedding.is_(None)),
                )
                .select_from(MemoryBlob)
                .join(NotePartIndexPointer, NotePartIndexPointer.blob_id == MemoryBlob.id)
                .where(NotePartIndexPointer.part_id == part_id)
            )
        ).one()

    # THE WAIT. Asserted on what the wait BOUGHT rather than on a wall
    # clock, which on a shared machine measures the machine: a budget
    # granted per chunk would have paid for every chunk up to the call cap,
    # so a vector count below the cap is the shared deadline binding.
    assert embedded < note_search._EMBED_MAX_CHUNKS, (
        f"{embedded} chunks got a vector under a {note_search._EMBED_TIMEOUT_S}s "
        "budget for the whole part: the budget is being granted per chunk"
    )
    assert elapsed < 20.0
    # The chunks the budget could not pay for are written keyword-only and
    # left for the embedding_migration worker, which is the degradation the
    # module contracts for, not a silent loss.
    assert keyword_only > 0


# ------------------------------------------------------------------ 6


async def test_the_rechunk_backfill_is_idempotent(_embedder: None) -> None:
    """A long part indexed as ONE whole-doc blob ends with N chunks, N
    pointer rows and no whole-doc blob left; the second pass writes
    nothing."""
    org, user = await _org()
    body = _long_body()
    _note_id, part_id = await _long_part(org, user, body)

    # Collapse the part back to the legacy shape: one blob carrying the
    # whole text, one pointer row at chunk 0.
    async with tenant_session(str(org), str(user)) as s:
        pointers = await _pointers(s, part_id)
        head = pointers[0]
        await s.execute(
            delete(MemoryBlob).where(MemoryBlob.id.in_([p.blob_id for p in pointers[1:]]))
        )
        await s.execute(
            MemoryBlob.__table__.update().where(MemoryBlob.id == head.blob_id).values(text=body)
        )
    async with tenant_session(str(org), str(user)) as s:
        assert len(await _pointers(s, part_id)) == 1

    async with tenant_session(str(org), str(user)) as s:
        first = await note_search.run_rechunk_backfill(s, batch_size=50)
    assert first == 1

    async with tenant_session(str(org), str(user)) as s:
        texts = await _texts(s, part_id)
        assert len(texts) > 1
        assert not any(t == body for t in texts), "the whole-doc blob must be gone"
        assert HEAD_TOKEN in texts[0]
        assert any(TAIL_TOKEN in t for t in texts[1:])
        # The provenance rows now carry the chunk positions, which is what
        # keeps the part out of the rechunk_legacy_sources candidate set.
        indices = sorted(
            (
                await s.execute(
                    select(BlobSource.chunk_index).where(
                        BlobSource.source_kind == "note_part",
                        BlobSource.source_id == str(part_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert indices == list(range(len(texts)))

    async with tenant_session(str(org), str(user)) as s:
        second = await note_search.run_rechunk_backfill(s, batch_size=50)
    assert second == 0

    async with tenant_session(str(org), str(user)) as s:
        assert await _texts(s, part_id) == texts


async def test_a_short_part_is_not_a_rechunk_candidate(_embedder: None) -> None:
    """The threshold is read on the RENDERED text, so a part under it is
    left alone however many times the sweep runs. Without this the pass
    would re-embed every short note in the workspace on every tick."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="parte breve e basta"
        )
        note_id = note.id
    async with tenant_session(str(org), str(user)) as s:
        parts = await np.list_parts(s, org_id=org, note_id=note_id)
        assert len(await _pointers(s, parts[0].id)) == 1
        assert await note_search.run_rechunk_backfill(s, batch_size=50) == 0
    assert get_chunk_threshold_tokens() == 800


async def test_every_chunk_of_a_rewritten_part_carries_the_same_facets(
    _embedder: None,
) -> None:
    """A part that grows past a boundary must not end up with the note's
    current tags on its new chunks and older tags on the ones that
    survived the rewrite: one document would then answer two different
    faceted queries depending on which section matched."""
    org, user = await _org()
    note_id, part_id = await _long_part(org, user, _long_body(paragraphs=10))

    async with tenant_session(str(org), str(user)) as s:
        facet = await taxonomy.create_tag(
            s,
            org_id=org,
            actor_id=user,
            kind=TagKind.generic,
            name=f"facet-{uuid.uuid4().hex[:8]}",
        )
        await nt.attach_tag(s, org_id=org, actor_id=user, note_id=note_id, tag_id=facet.id)
        facet_id = facet.id
        parts = await np.list_parts(s, org_id=org, note_id=note_id)
        version = next(p.version for p in parts if p.id == part_id)

    async with tenant_session(str(org), str(user)) as s:
        await np.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=part_id,
            expected_version=version,
            body=_long_body(paragraphs=40),
        )

    async with tenant_session(str(org), str(user)) as s:
        pointers = await _pointers(s, part_id)
        assert len(pointers) > 1
        tagged = (
            (
                await s.execute(
                    select(MemoryBlobTag.blob_id).where(
                        MemoryBlobTag.blob_id.in_([p.blob_id for p in pointers]),
                        MemoryBlobTag.tag_id == facet_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(tagged) == {p.blob_id for p in pointers}
