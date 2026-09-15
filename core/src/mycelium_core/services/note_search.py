"""Note search: keep a note PART's memory blobs in sync with its body.

The note-part analogue of ``services.task_search``. Decision 2026-06-09
(task 9fc94327): notes become first-class hits in the existing memory
pipeline (``memory_blobs``: FTS generated + pgvector + RRF) without a
parallel index, indexed PER PART so each part re-embeds independently.
The binding is held by ``note_part_index_pointer``; the indexed text is
the part's ``title || body``.

Why per-part (not per-note): a long note is several parts; editing one
part should not re-embed the whole note, and the chunked-append path
(``note_parts.append_to_part``) targets a single part. Indexing per part
keeps the re-embed unit aligned with the edit unit.

Why a SET of blobs per part (task 11154e32, migration 0016): the
embedder's window is 2048 tokens, so one vector for a whole part
represents its head and nothing else once the part is longer than about
7600 characters -- while the part reads as fully indexed, because nothing
declared the loss. A part above the chunker's threshold is split by
``chunker.pick_chunker(namespace="note")`` and each piece is its own
blob, its own pointer row and its own vector. Below the threshold the
part is one chunk and every path here degenerates to what it did before.

The cardinality had to move in the schema and not only in this file: the
pointer's primary key was ``(part_id)``, so chunks 1..N-1 had nowhere to
be recorded and would have existed as blobs no maintenance path reaches.

Mutation tracking
-----------------
Sync SQLAlchemy event listeners on ``NotePart`` cover the ORM paths
(``create_part`` and ``_upsert_part_zero`` used by create_note /
update_note / transcribe -- all ``session.add`` / attribute-set + flush).
The Core-update paths (``append_to_part`` / ``prepend_to_part`` /
``replace_in_part`` / ``update_part`` go through ``optimistic_update``,
and ``delete_part`` through a Core ``delete``) bypass mapper events, so
those choke points call :func:`mark_note_part_dirty` /
:func:`mark_note_part_deleted` explicitly. The async
:func:`flush_note_search_dirty` drains the accumulated ids and upserts
the blobs; it is called from ``db.tenant_session`` just before commit
(same chokepoint as the task index).

content_hash, embedder timeout, keyword-only fallback: same contract as
``task_search``, with the cap granted to the PART rather than to each of
its chunks (a 2 s budget and a ceiling on calls; whatever it does not
pay for is written keyword-only and the generic ``embedding_migration``
worker re-embeds NULL-vector blobs later, off the write path).

Project scoping: a part blob inherits the note's project (via
``notes.project_tag_for_note``), matching the existing note-blob scoping
(voice transcripts already landed project-scoped on the "note" channel).
This preserves per-project search isolation -- it is NOT widened to the
org-wide surface the task index uses.

Index scope: a note at ``index_scope='none'`` is not indexed at all. The
scope is read per part in the resync, before the content_hash
short-circuit, and fanned out over the note's parts by
``services.notes.update_note`` -- no mapper listener is registered on
``Note``, so the flip itself has to say so.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, object_session

from mycelium_core.embed_dims import EMBED_DIM
from mycelium_core.embedder import Embedder, EmbedResult, EmbedSide, get_embedder
from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.memory_blob import BlobSource, MemoryBlob
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.services.chunker import (
    Chunk,
    approx_tokens,
    get_chunk_threshold_tokens,
    pick_chunker,
)
from mycelium_core.services.fts_language import detect_fts_language
from mycelium_core.services.memory import erase_blobs_for_sources

logger = logging.getLogger(__name__)

_DIRTY_KEY = "note_search_dirty"
_DELETED_KEY = "note_search_deleted"
# Budget for the WHOLE part, shared across its chunks: see _embed_chunks.
_EMBED_TIMEOUT_S = 2.0
# Ceiling on embed CALLS per part per commit. The deadline alone does not
# bound them: with a fast embedder it lets through as many as the document
# has chunks, and that count follows the text rather than the budget.
# Sixteen chunks is ~6400 words, past any note written by hand; what is
# longer is an import, and an import's tail belongs to the sweep.
_EMBED_MAX_CHUNKS = 16
_NO_EMBED_MODEL = "none"


# ---------------------------------------------------------------- listeners


def _record_dirty(session: Session | None, part_id: uuid.UUID | None) -> None:
    if session is None or part_id is None:
        return
    session.info.setdefault(_DIRTY_KEY, set()).add(part_id)


def _record_deleted(session: Session | None, part_id: uuid.UUID | None) -> None:
    if session is None or part_id is None:
        return
    session.info.setdefault(_DELETED_KEY, set()).add(part_id)


def mark_note_part_dirty(session: AsyncSession, part_id: uuid.UUID) -> None:
    """Mark a note part for re-index at commit time.

    The mapper listeners below cover the ORM paths (``create_part``,
    ``_upsert_part_zero``), but ``optimistic_update`` / Core
    ``update`` bypass mapper-level events (the SQLAlchemy docs note this:
    mapper events fire only for the unit-of-work flush). The part-mutation
    choke points in ``services.note_parts`` call this so the resync still
    fires on commit. Cheap and idempotent (dedup via a ``set``; nothing
    hits the DB here)."""
    _record_dirty(session.sync_session, part_id)


def mark_note_part_deleted(session: AsyncSession, part_id: uuid.UUID) -> None:
    """Mark a note part as deleted so its blob is dropped on commit.

    ``delete_part`` issues a Core ``DELETE`` (no after_delete event), so
    it calls this to schedule the blob cleanup."""
    _record_deleted(session.sync_session, part_id)


# The decorator side-effects are what matters; the function names are
# deliberately unused references (pyright can't see the registration).


@event.listens_for(NotePart, "after_insert")
def _part_after_insert(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: NotePart
) -> None:
    _record_dirty(object_session(target), target.id)


@event.listens_for(NotePart, "after_update")
def _part_after_update(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: NotePart
) -> None:
    # Enqueue unconditionally; the cheap content_hash compare in the
    # flush short-circuits ord/metadata-only updates that don't change
    # the searchable text.
    _record_dirty(object_session(target), target.id)


@event.listens_for(NotePart, "after_delete")
def _part_after_delete(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: NotePart
) -> None:
    _record_deleted(object_session(target), target.id)


# ---------------------------------------------------------------- rendering


def render_part_for_search(part: NotePart) -> str:
    """Title (if any) then body. The title is the user-facing label;
    including it lets a search match a part by its heading even when the
    body is terse."""
    title = (part.title or "").strip()
    body = part.body or ""
    if title:
        return f"{title}\n\n{body}".strip()
    return body.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- embedder helper


@dataclass
class _TimeoutEmbedder:
    """Wrap an Embedder so a slow encode degrades to keyword-only rather
    than blocking commit (same contract as task_search)."""

    inner: Embedder
    timeout: float

    async def embed(self, text: str, *, side: EmbedSide) -> EmbedResult:
        return await asyncio.wait_for(self.inner.embed(text, side=side), timeout=self.timeout)


async def _safe_embed(
    embedder: _TimeoutEmbedder, text_body: str
) -> tuple[list[float] | None, str, int]:
    """Best-effort embed. On timeout or any embedder failure return a
    keyword-only result; the FTS branch still covers the blob and the
    ``embedding_migration`` worker re-embeds it later."""
    try:
        result = await embedder.embed(text_body, side=EmbedSide.document)
    except TimeoutError:
        return None, _NO_EMBED_MODEL, 0
    except Exception as exc:
        logger.debug("note-search embed failed: %s", exc)
        return None, _NO_EMBED_MODEL, 0
    if not result.vector or len(result.vector) != EMBED_DIM:
        return None, _NO_EMBED_MODEL, 0
    return list(result.vector), result.model_id, result.tokens


# ---------------------------------------------------------------- flush


async def flush_note_search_dirty(session: AsyncSession) -> None:
    """Process the dirty/deleted sets accumulated by the listeners.

    Called by ``db.tenant_session`` right before commit, so the resync is
    in the same transaction as the part mutation (FTS visible atomically;
    semantic vector best-effort within the 2 s timeout, recovered async
    otherwise). Idempotent and re-entry safe (pops the sets). Robust to a
    partially-rolled-back outer transaction (skip; the embedding backfill
    reconciles later)."""
    from sqlalchemy.exc import InvalidRequestError

    info = session.sync_session.info
    deleted_ids: set[uuid.UUID] = info.pop(_DELETED_KEY, set())
    dirty_ids: set[uuid.UUID] = info.pop(_DIRTY_KEY, set())
    dirty_ids -= deleted_ids
    if not deleted_ids and not dirty_ids:
        return
    from mycelium_core.db import index_maintenance_scope

    try:
        # Org-scoped maintenance, not the caller's project perimeter: see
        # ``db.index_maintenance_scope``. Inside the ``try`` and after the
        # early return, so an aborted transaction with nothing pending still
        # emits no SQL.
        async with index_maintenance_scope(session):
            for part_id in deleted_ids:
                await _drop_part_blobs(session, part_id)
            for part_id in dirty_ids:
                await _resync_part_blob(session, part_id)
    except InvalidRequestError as exc:
        logger.warning(
            "note-search resync skipped (session unusable, likely partial savepoint rollback): %s",
            exc,
        )


# ---------------------------------------------------------------- delete


async def delete_part_index_now(session: AsyncSession, part_id: uuid.UUID) -> None:
    """Drop a part's search blobs immediately (inline, not deferred).

    Call this BEFORE a hard ``DELETE`` of the part row: the pointer's
    ``part_id`` FK is ``ON DELETE CASCADE``, so once the part row goes the
    pointer set is gone and a deferred :func:`flush_note_search_dirty` can
    no longer resolve the blobs to delete (it would orphan them)."""
    await _drop_part_blobs(session, part_id)


async def _drop_part_blobs(session: AsyncSession, part_id: uuid.UUID) -> None:
    """Erase every blob this part is the source of, by provenance.

    Not "resolve the pointer and delete the blob it names": a part owns N
    blobs as soon as the indexer chunks a long one, and provenance is the
    relation that is already 1:N. It is also the primitive the other two
    hard-delete paths use for ``note_part`` (``trash.empty_trash``,
    ``entity_revisions.hard_delete_soft_deleted``), so the three converge
    on one erase instead of diverging, which is what the rule written in
    the humus predicate asks for ("every hard-delete path must erase the
    index blobs itself").

    The pointer rows are not deleted here and do not need to be: deleting
    the blob cascades them through the composite FK. That is sufficient
    rather than incidental, because a pointer's blob has exactly one
    provenance row -- this module is the only writer of these blobs, and
    consolidation copies a member's provenance onto the MERGED concept,
    which has no pointer row of its own.

    What it does not cover, and deliberately: a blob claimed by a second
    source survives, since ``erase_blobs_for_sources`` drops a blob only
    once nothing else points at it. The merged concept above IS reached
    here -- this part's provenance row on it goes -- but survives on the
    other members' rows, still carrying the merged text. ``index_scope``
    is an opt-out from indexing a row, not a retraction of everything ever
    derived from it.
    """
    await erase_blobs_for_sources(session, sources=[("note_part", str(part_id))])


# ---------------------------------------------------------------- resync


async def _load_part(
    session: AsyncSession, part_id: uuid.UUID
) -> tuple[NotePart, IndexScope] | None:
    """The part, plus the index scope of the note that owns it.

    The scope lives on ``notes`` while the indexed unit is the part, so
    the resync has to read across the two. It rides along in the load
    the resync already pays rather than costing a SELECT of its own:
    this path runs on every commit that touches a part, including the
    cheap hash-unchanged one.

    LEFT rather than INNER as a belt on a brace, not because the case is
    expected: ``note_part.note_id`` is NOT NULL and cascades, so a part
    does not outlive its note (the same fact the backfill sweep below
    relies on to use an inner join). If one ever did, LEFT keeps it in
    the resync -- where the ``pointer is None`` and part-gone paths can
    still clean up -- instead of dropping it silently. A missing note
    reads as ``org``, the default: a part with no note to answer for it
    is not evidence that someone asked for the opt-out.
    """
    row = (
        await session.execute(
            select(NotePart, Note.index_scope)
            .outerjoin(Note, Note.id == NotePart.note_id)
            .where(NotePart.id == part_id)
        )
    ).one_or_none()
    if row is None:
        return None
    part, scope = row
    return part, IndexScope(scope) if scope is not None else IndexScope.org


async def _load_pointers(session: AsyncSession, part_id: uuid.UUID) -> list[NotePartIndexPointer]:
    """The part's pointer set, in chunk order. Empty when never indexed."""
    return list(
        (
            await session.execute(
                select(NotePartIndexPointer)
                .where(NotePartIndexPointer.part_id == part_id)
                .order_by(NotePartIndexPointer.chunk_index)
            )
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True)
class _EmbeddedChunk:
    """One chunk of the part with whatever vector the budget could pay
    for. ``vector is None`` means keyword-only: the FTS branch still
    covers the blob and the ``embedding_migration`` worker fills the
    vector in later, off the write path."""

    index: int
    text: str
    vector: list[float] | None
    model_id: str


async def _embed_chunks(chunks: Sequence[Chunk]) -> list[_EmbeddedChunk]:
    """Embed the part's chunks under ONE budget for the part.

    Two bounds, and both are needed. The 2 s deadline is shared across
    the whole set rather than granted per chunk, because it exists to cap
    the latency a commit pays and a per-chunk cap scales with N (the
    longest part in the reference workspace is ~25k words, of the order
    of 70 chunks). The call cap is the other half: with a fast embedder
    the deadline alone would let N calls through, and N follows the
    document, not the budget.

    Chunks past either bound are written keyword-only, in document order,
    so what gets the vectors is the head of the part. That is the
    contract the module docstring already states for a slow embedder; a
    long part now reaches it by length as well as by latency.

    Not the embedder's batch API: a batch is one call but it is
    all-or-nothing, so a slow tail would cost the whole set its vectors,
    and an unbounded batch inside the pre-commit hook is the shape that
    OOM-killed the worker on 2026-07-24.
    """
    embedder = get_embedder()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _EMBED_TIMEOUT_S
    out: list[_EmbeddedChunk] = []
    for chunk in chunks:
        remaining = deadline - loop.time()
        if chunk.index >= _EMBED_MAX_CHUNKS or remaining <= 0.0:
            out.append(_EmbeddedChunk(chunk.index, chunk.text, None, _NO_EMBED_MODEL))
            continue
        vector, model_id, _tokens = await _safe_embed(
            _TimeoutEmbedder(embedder, remaining), chunk.text
        )
        out.append(_EmbeddedChunk(chunk.index, chunk.text, vector, model_id))
    return out


async def _resync_part_blob(
    session: AsyncSession, part_id: uuid.UUID, *, rechunk: bool = False
) -> None:
    """Bring the part's SET of index blobs in line with its text.

    Four paths, gated on (a) whether the part has a pointer set and (b)
    the content hash, which is a property of the part and is carried on
    every row of the set (so the cheap path reads it off chunk 0, a
    lookup on a primary-key prefix):
      - part gone, or ``index_scope='none'``: erase the set by provenance
      - no pointers: chunk, embed, INSERT the set
      - pointers + same hash: skip (cheap path, no embed)
      - pointers + new hash: reconcile the set against the new chunking

    ``rechunk=True`` skips the hash short circuit. The hash answers "has
    the text changed", and the backfill pass below exists for the case
    where it has not but the CHUNKING has: a part indexed as one whole
    blob before this indexer chunked anything.
    """
    loaded = await _load_part(session, part_id)
    if loaded is None:
        # Part is gone (hard delete that didn't go through after_delete,
        # e.g. a note-cascade DELETE). Clean the blobs/pointers too.
        await _drop_part_blobs(session, part_id)
        return
    part, index_scope = loaded
    if index_scope == IndexScope.none:
        # Before the content_hash short-circuit below, deliberately. A scope
        # flip leaves the rendered text identical, so the hash is unchanged
        # and a guard placed after the short-circuit would never run on a
        # part that is already indexed -- which is the whole remedy case.
        # Delete rather than skip, or the stale blobs stay retrievable; by
        # provenance rather than through the pointer, or a long part would
        # lose its head and keep chunks 1..N-1 indexed.
        await _drop_part_blobs(session, part_id)
        return
    text_body = render_part_for_search(part)
    new_hash = content_hash(text_body)

    pointers = await _load_pointers(session, part_id)

    if pointers and pointers[0].content_hash == new_hash and not rechunk:
        # Text unchanged. ``merge_notes`` reparents a part (new note_id)
        # without touching its body; refresh the set's ownership so the
        # hits still resolve to -- and are project-scoped to -- the new
        # note, without a needless re-embed.
        if pointers[0].note_id != part.note_id:
            await _refresh_pointers_note(session=session, pointers=pointers, part=part)
        return

    chunks = pick_chunker(namespace="note", text=text_body).chunks(text_body)
    embedded = await _embed_chunks(chunks)
    project_id = await _project_of_note(session, note_id=part.note_id)
    tag_ids = await _index_tag_ids(session, org_id=part.org_id, note_id=part.note_id)

    if pointers:
        await _reconcile_blob_set(
            session=session,
            part=part,
            pointers=pointers,
            embedded=embedded,
            project_id=project_id,
            tag_ids=tag_ids,
            content_hash_value=new_hash,
        )
    else:
        await _create_blob_set(
            session=session,
            part=part,
            embedded=embedded,
            project_id=project_id,
            tag_ids=tag_ids,
            content_hash_value=new_hash,
        )


async def _project_of_note(session: AsyncSession, *, note_id: uuid.UUID) -> uuid.UUID | None:
    from mycelium_core.services.notes import project_tag_for_note

    return await project_tag_for_note(session, note_id=note_id)


def _new_chunk_blob(
    session: AsyncSession,
    *,
    part: NotePart,
    project_id: uuid.UUID | None,
    chunk: _EmbeddedChunk,
) -> uuid.UUID:
    """Stage one blob for one chunk and return its id.

    The id is chosen here rather than read back from a flush, so a whole
    set is written by one flush instead of one round trip per chunk. This
    runs inside the pre-commit hook and a long part is dozens of chunks.

    Only the blob: its provenance and its facets are staged by
    :func:`_stage_chunk_satellites` after the blobs are flushed. That
    order is explicit and not left to the unit of work, which sorts
    mappers by relationship and there is no relationship configured
    between these tables -- it inserted ``blob_sources`` first and the
    composite foreign key rejected the batch.
    """
    session.add(
        MemoryBlob(
            id=(blob_id := uuid.uuid4()),
            org_id=part.org_id,
            project_id=project_id,
            namespace="note",
            tier="hot",
            text=chunk.text,
            fts_language=detect_fts_language(chunk.text),
            embedding=chunk.vector,
            model_id=chunk.model_id,
            dim=EMBED_DIM,
            access_count=1,
            last_accessed_at=dt.datetime.now(tz=dt.UTC),
        )
    )
    return blob_id


def _stage_chunk_satellites(
    session: AsyncSession,
    *,
    part: NotePart,
    blob_id: uuid.UUID,
    chunk_index: int,
    tag_ids: Sequence[uuid.UUID],
) -> None:
    """The provenance row and the facets of one chunk blob.

    ``blob_sources.chunk_index`` carries the chunk position. Writing it
    (the single-blob indexer left it to the server default, so every note
    part read as chunk 0) is what keeps a chunked part out of the
    ``rechunk_legacy_sources`` candidate predicate, which is
    ``bool_and(chunk_index = 0)``: that admin path deletes the blob and
    rewrites it through ``write_blob``, which does not recreate the
    pointer row, and the note-search sweep would then index the part a
    second time on top of the orphans. The trap closes in the data rather
    than in a recommendation not to press the button.
    """
    from mycelium_core.models.memory_blob import MemoryBlobTag

    session.add(
        BlobSource(
            blob_id=blob_id,
            org_id=part.org_id,
            source_kind="note_part",
            source_id=str(part.id),
            chunk_index=chunk_index,
        )
    )
    for tid in tag_ids:
        session.add(MemoryBlobTag(blob_id=blob_id, org_id=part.org_id, tag_id=tid))


async def _write_chunk_blobs(
    session: AsyncSession,
    *,
    part: NotePart,
    project_id: uuid.UUID | None,
    chunks: Sequence[_EmbeddedChunk],
    tag_ids: Sequence[uuid.UUID],
) -> dict[int, uuid.UUID]:
    """Blobs first, then everything that points at them. Two flushes for
    the whole set, whatever its size."""
    if not chunks:
        return {}
    ids = {
        chunk.index: _new_chunk_blob(session, part=part, project_id=project_id, chunk=chunk)
        for chunk in chunks
    }
    await session.flush()
    for chunk in chunks:
        _stage_chunk_satellites(
            session,
            part=part,
            blob_id=ids[chunk.index],
            chunk_index=chunk.index,
            tag_ids=tag_ids,
        )
    await session.flush()
    return ids


async def _insert_pointer_set(
    session: AsyncSession,
    *,
    part: NotePart,
    blob_ids: dict[int, uuid.UUID],
    content_hash_value: str,
) -> None:
    """The pointer rows of a freshly written set, in ONE savepoint.

    Per row it would be worse than no guard at all: a conflict on chunk k
    would leave 0..k-1 pointed at and k..N-1 as blobs no maintenance path
    can reach, which is the state the 1:N pointer exists to make
    impossible. On conflict a concurrent resync has written the set and
    its blobs are canonical, so ours go.
    """
    if not blob_ids:
        return
    try:
        async with session.begin_nested():
            for chunk_index, blob_id in sorted(blob_ids.items()):
                session.add(
                    NotePartIndexPointer(
                        part_id=part.id,
                        note_id=part.note_id,
                        org_id=part.org_id,
                        blob_id=blob_id,
                        chunk_index=chunk_index,
                        content_hash=content_hash_value,
                    )
                )
            await session.flush()
    except IntegrityError:
        await session.execute(
            delete(MemoryBlob).where(
                MemoryBlob.id.in_(list(blob_ids.values())),
                MemoryBlob.org_id == part.org_id,
            )
        )


async def _create_blob_set(
    *,
    session: AsyncSession,
    part: NotePart,
    embedded: Sequence[_EmbeddedChunk],
    project_id: uuid.UUID | None,
    tag_ids: Sequence[uuid.UUID],
    content_hash_value: str,
) -> None:
    """Insert path: N blobs and the N pointer rows that own them."""
    blob_ids = await _write_chunk_blobs(
        session, part=part, project_id=project_id, chunks=embedded, tag_ids=tag_ids
    )
    await _insert_pointer_set(
        session, part=part, blob_ids=blob_ids, content_hash_value=content_hash_value
    )


async def _reconcile_blob_set(
    *,
    session: AsyncSession,
    part: NotePart,
    pointers: Sequence[NotePartIndexPointer],
    embedded: Sequence[_EmbeddedChunk],
    project_id: uuid.UUID | None,
    tag_ids: Sequence[uuid.UUID],
    content_hash_value: str,
) -> None:
    """Match an existing set to the new chunking, slot by slot.

    Not erase-then-rewrite. Below the chunk threshold a part is a single
    chunk and this degenerates to exactly the in-place UPDATE the
    single-blob indexer did, which is what keeps an ordinary note edit
    from resetting the blob's access counters and cluster membership --
    the property the update path was written to protect, and the reason
    ``write_blob`` is the wrong seam here (it re-inserts, so it resets
    them on every write).

    Above the threshold, chunk boundaries move with the text, so a slot
    is a position and not an identity: slots present on both sides are
    rewritten, slots the new text no longer has are deleted, slots it
    gained are inserted. What that guarantees, and what the previous
    shape could not, is that no blob of the part carries pre-edit text
    once this returns.
    """
    by_index = {p.chunk_index: p for p in pointers}
    kept = [c for c in embedded if c.index in by_index]
    gained = [c for c in embedded if c.index not in by_index]

    created = await _write_chunk_blobs(
        session, part=part, project_id=project_id, chunks=gained, tag_ids=tag_ids
    )

    for chunk in kept:
        existing = by_index[chunk.index]
        await session.execute(
            update(MemoryBlob)
            .where(MemoryBlob.id == existing.blob_id, MemoryBlob.org_id == existing.org_id)
            .values(
                text=chunk.text,
                fts_language=detect_fts_language(chunk.text),
                embedding=chunk.vector,
                model_id=chunk.model_id,
                dim=EMBED_DIM,
                project_id=project_id,
            )
        )
        existing.note_id = part.note_id
        existing.content_hash = content_hash_value

    await _add_missing_blob_tags(
        session,
        org_id=part.org_id,
        blob_ids=[by_index[c.index].blob_id for c in kept],
        tag_ids=tag_ids,
    )

    # Slots the new text does not have. Deleting the blob cascades both
    # its pointer row and its provenance row, so the set shrinks whole.
    live = {c.index for c in embedded}
    stale = [p.blob_id for p in pointers if p.chunk_index not in live]
    if stale:
        await session.execute(
            delete(MemoryBlob).where(MemoryBlob.id.in_(stale), MemoryBlob.org_id == part.org_id)
        )

    await _insert_pointer_set(
        session, part=part, blob_ids=created, content_hash_value=content_hash_value
    )


async def _refresh_pointers_note(
    *,
    session: AsyncSession,
    pointers: Sequence[NotePartIndexPointer],
    part: NotePart,
) -> None:
    """A part moved to another note (merge) but its body is unchanged:
    re-point the whole set + re-scope every chunk's project, no re-embed."""
    project_id = await _project_of_note(session, note_id=part.note_id)
    await session.execute(
        update(MemoryBlob)
        .where(
            MemoryBlob.id.in_([p.blob_id for p in pointers]),
            MemoryBlob.org_id == part.org_id,
        )
        .values(project_id=project_id)
    )
    for pointer in pointers:
        pointer.note_id = part.note_id


async def _part_ids_of_note(session: AsyncSession, note_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (await session.execute(select(NotePart.id).where(NotePart.note_id == note_id)))
        .scalars()
        .all()
    )


async def mark_note_parts_dirty(session: AsyncSession, *, note_id: uuid.UUID) -> None:
    """Queue every part of a note for re-index at commit.

    The note-level half of the ``index_scope`` guard, for the flip
    itself. No mapper listener is registered on ``Note`` and the scope
    lives there, so the UPDATE that sets it marks nothing dirty: without
    this call the change would reach the index only when some part's
    body next happened to change.

    One call covers BOTH directions, which is why it does not have to
    read the previous value: the per-part guard in ``_resync_part_blob``
    reads the scope that is now on the row and either erases that part's
    blobs or re-indexes them. Deferring to the flush rather than erasing
    here also keeps every index write inside the one window that runs
    without the caller's project perimeter (``db._index_maintenance_scope``);
    an erase issued inline from a project-scoped request deletes the
    provenance and leaves the blob.
    """
    for pid in await _part_ids_of_note(session, note_id):
        mark_note_part_dirty(session, pid)


async def rescope_note_blobs(
    session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID
) -> None:
    """Re-scope every already-indexed blob of a note to the note's CURRENT
    project perimeter (its project tag, or NULL). The perimeter lives on the
    blob's ``project_id``, set at index time; a note's project comes from a
    project-kind tag, so adding/removing that tag changes the perimeter
    without touching content. Project is metadata, not text -- one UPDATE,
    no re-embed -- so a peer's search sees the new perimeter immediately
    instead of only after the note's next content edit re-indexes it (task
    1d152747). A note not yet flushed to the index has no pointers here and
    is scoped correctly by the deferred index at commit.

    Set-shaped already: the subquery selects the note's pointer rows, which
    is every chunk of every part, so a long part is rescoped whole."""
    project_id = await _project_of_note(session, note_id=note_id)
    await session.execute(
        update(MemoryBlob)
        .where(
            MemoryBlob.org_id == org_id,
            MemoryBlob.id.in_(
                select(NotePartIndexPointer.blob_id).where(
                    NotePartIndexPointer.note_id == note_id,
                    NotePartIndexPointer.org_id == org_id,
                )
            ),
        )
        .values(project_id=project_id)
    )


# ---------------------------------------------------------------- tag wiring


async def _index_tag_ids(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Every tag a part blob carries: the note's own tags (faceted-search
    parity with task blobs, which inherit the task's) plus the
    ``memory_channel`` tag with system_key='note', without which the blob
    is off the channel the note search queries.

    Resolved ONCE per part rather than once per blob: a long part is
    dozens of chunks and the facets are the same on all of them. The union
    is deduplicated, which is also what makes the channel tag safe to
    append unconditionally -- a note tagged with the channel tag itself
    would otherwise collide on the blob's tag primary key.
    """
    from mycelium_core.models.note_tag import NoteTag
    from mycelium_core.models.tag import Tag, TagKind
    from mycelium_core.services import taxonomy

    out: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    rows = (
        (await session.execute(select(NoteTag.tag_id).where(NoteTag.note_id == note_id)))
        .scalars()
        .all()
    )
    for tid in rows:
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    # Ensure-seed here to keep the resync self-contained.
    await taxonomy.ensure_default_memory_channels(session, org_id=org_id)
    channel_id = (
        await session.execute(
            select(Tag.id).where(Tag.kind == TagKind.memory_channel, Tag.system_key == "note")
        )
    ).scalar_one_or_none()
    if channel_id is not None and channel_id not in seen:
        out.append(channel_id)
    return out


async def _add_missing_blob_tags(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    blob_ids: Sequence[uuid.UUID],
    tag_ids: Sequence[uuid.UUID],
) -> None:
    """Give blobs that already exist the facets a fresh chunk would get.

    Needed because a rewrite keeps the slots that survive: without this, a
    part that grew past a boundary would carry the note's current tags on
    its new chunks and the tags of its last full re-index on the old ones,
    which is one document answering two different facet queries.

    ADDITIVE, and that is the decision rather than an omission. A blob's
    tags are not only inherited: ``memory.attach_blob_tag`` is a curation
    door open to a person and to an assistant, and a resync that made the
    set match the note exactly would delete by hand what was added by
    hand, on the next edit of the part.
    """
    if not blob_ids or not tag_ids:
        return
    from mycelium_core.models.memory_blob import MemoryBlobTag

    have = {
        (bid, tid)
        for bid, tid in (
            await session.execute(
                select(MemoryBlobTag.blob_id, MemoryBlobTag.tag_id).where(
                    MemoryBlobTag.blob_id.in_(list(blob_ids)),
                    MemoryBlobTag.org_id == org_id,
                )
            )
        ).all()
    }
    missing = [
        MemoryBlobTag(blob_id=bid, org_id=org_id, tag_id=tid)
        for bid in blob_ids
        for tid in tag_ids
        if (bid, tid) not in have
    ]
    if missing:
        session.add_all(missing)
        await session.flush()


# ---------------------------------------------------------------- backfill


async def run_pointer_backfill(session: AsyncSession, *, batch_size: int = 50) -> int:
    """Index note parts that don't have a ``note_part_index_pointer`` yet.

    The listener path catches every new mutation, but parts that pre-date
    this deploy never went through it. This sweep picks the first
    ``batch_size`` unindexed parts, skipping the ones whose note is at
    ``index_scope='none'`` (those have no pointer by design), and runs
    the same ``_resync_part_blob`` the listener would have. Returns the
    count indexed in this batch."""
    rows = (
        await session.execute(
            select(NotePart.id)
            .outerjoin(NotePartIndexPointer, NotePartIndexPointer.part_id == NotePart.id)
            .join(Note, Note.id == NotePart.note_id)
            .where(
                NotePartIndexPointer.part_id.is_(None),
                # A scoped-out part has no pointer by definition, so without
                # this term it stays a candidate on every tick, fills the
                # batch (which has no ORDER BY) and starves the real backlog.
                # The guard in the resync makes it harmless; this makes it
                # free. Inner join: ``note_part.note_id`` is NOT NULL and
                # cascades, so a part without a note does not outlive it.
                Note.index_scope != IndexScope.none,
            )
            .limit(batch_size)
        )
    ).all()
    if not rows:
        return 0
    indexed = 0
    for (part_id,) in rows:
        try:
            await _resync_part_blob(session, part_id)
            indexed += 1
        except Exception:
            logger.exception("note-search pointer backfill failed for part_id=%s", part_id)
    return indexed


async def run_rechunk_backfill(session: AsyncSession, *, batch_size: int = 50) -> int:
    """Re-index parts that are long enough to chunk but hold one blob.

    The back-catalogue of the single-blob indexer: every part written
    before this module chunked anything has exactly one pointer row, and
    for a part above the threshold that one blob is the head of the text
    presented as the whole of it.

    Note-native on purpose. ``memory.rechunk_legacy_sources`` looks like
    the tool for this and is not: it deletes the legacy blob, which
    cascades the pointer row away, and rewrites through ``write_blob``,
    which does not recreate it -- so the chunks land orphaned and
    ``run_pointer_backfill`` above, seeing a part with no pointer, writes
    an (N+1)-th whole-doc blob on top of them within a tick. This pass
    goes through the same resync as every other write instead, which is
    what keeps the pointer set and the blob set one thing.

    Idempotent by construction: a rechunked part has N>1 pointer rows and
    stops matching the candidate predicate, so a second pass writes
    nothing. ``ParagraphChunker`` always yields at least two chunks above
    the threshold (the cap is 400 words against a floor of 800), so the
    predicate cannot re-admit what it just processed.
    """
    # The threshold is counted in words on the RENDERED text, which needs
    # the part; SQL pre-filters on a bound that cannot exclude a
    # candidate (800 words occupy at least 1600 characters, one per word
    # plus one separator) and the exact count is confirmed below.
    min_chars = get_chunk_threshold_tokens() * 2
    rows = (
        await session.execute(
            select(NotePart.id)
            .join(Note, Note.id == NotePart.note_id)
            .join(NotePartIndexPointer, NotePartIndexPointer.part_id == NotePart.id)
            .where(
                Note.index_scope != IndexScope.none,
                func.char_length(func.coalesce(NotePart.body, ""))
                + func.char_length(func.coalesce(NotePart.title, ""))
                >= min_chars,
            )
            .group_by(NotePart.id)
            .having(func.count() == 1)
            .limit(batch_size)
        )
    ).all()
    if not rows:
        return 0
    rechunked = 0
    for (part_id,) in rows:
        try:
            loaded = await _load_part(session, part_id)
            if loaded is None:
                continue
            part, _scope = loaded
            if approx_tokens(render_part_for_search(part)) < get_chunk_threshold_tokens():
                continue
            await _resync_part_blob(session, part_id, rechunk=True)
            rechunked += 1
        except Exception:
            logger.exception("note-search rechunk backfill failed for part_id=%s", part_id)
    return rechunked
