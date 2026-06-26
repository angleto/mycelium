"""End-to-end chunking pipeline: write a long note, retrieve by a
keyword that lives in exactly one paragraph, assert the top hit points
to the correct chunk (task 6405269c).

The unit tests in ``test_chunker.py`` cover the splitter in isolation
and ``test_dedupe_stage.py`` covers the dedupe stage on synthetic
candidates. This file exercises the full chain via the real
``memory.write_blob`` -> ``memory.retrieve`` services so a regression
in any link (chunker selection, write fan-out, BlobSource creation,
dedupe collapse, candidate -> Hit projection) is caught.

The fake embedder (ADR-0012 seam) returns content-deterministic
vectors so the lexical RRF branch carries the ranking; that is the
realistic path for a rare-keyword search.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.memory_blob import BlobSource
from mycelium_core.services import billing
from mycelium_core.services import memory as mem
from mycelium_core.services.auth import signup

_FAKE = FakeEmbedder()


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="CHUNK-E2E")
    return r.org_id, r.user_id


async def _seed_billing(s, org: uuid.UUID, user: uuid.UUID) -> None:
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))
    await billing.upsert_rate_card(
        s,
        org_id=org,
        actor_id=user,
        model_id=FakeEmbedder.model_id,
        provider="local",
        values={"credits_per_input": Decimal("0.001")},
    )


async def test_long_note_chunked_top_hit_carries_correct_chunk_index() -> None:
    """A 1200+ word note with a unique marker in the *middle* paragraph
    fans out into multiple chunks (ParagraphChunker, max_words=400);
    a search for the marker returns the chunk that contains it, not
    chunk 0.

    Asserts on two levels (matching the task's verification chain):
    1. write_blob created N>1 BlobSource rows for the same source_id
       with monotonically increasing chunk_index (chunker fan-out).
    2. retrieve returns at least one Hit and the top hit's
       chunk_index points into the marker paragraph (i.e. >= 1).
    """
    org, user = await _org()
    proj = uuid.uuid4()
    source_id = uuid.uuid4().hex

    # Three paragraphs, ~400 words each, separated by blank lines so
    # the paragraph splitter sees them. The marker lives only in
    # paragraph 1 (0-indexed) so the winning chunk must be 1 or 2
    # (the middle paragraph oversizes max_words=400 and window-splits
    # into 2 sibling chunks with overlap, both carrying the marker).
    para0 = "alpha " * 400
    para1_unit = "the quarterly zorblax-marker review covers projections "  # 7 words
    para1 = para1_unit * 60  # 420 words, all containing the marker
    para2 = "epsilon " * 400
    long_text = f"{para0}\n\n{para1}\n\n{para2}"

    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body=long_text,
            operation_id="write-long",
            namespace="note",
            sources=[("note", source_id)],
            embedder=_FAKE,
        )

        # 1) write fan-out: every chunk recorded its own BlobSource
        # row against the same parent source_id, with distinct
        # chunk_index values starting at 0.
        rows = (
            await s.execute(
                select(BlobSource.chunk_index)
                .where(BlobSource.source_kind == "note", BlobSource.source_id == source_id)
                .order_by(BlobSource.chunk_index)
            )
        ).all()
        chunk_indexes = [row.chunk_index for row in rows]
        assert len(chunk_indexes) >= 3, chunk_indexes
        assert chunk_indexes == list(range(len(chunk_indexes))), chunk_indexes

        # 2) retrieve returns the right chunk. The marker is rare and
        # unique to paragraph 1, so the lexical branch ranks the
        # marker-bearing chunks at the top; dedupe collapses them to
        # one Hit per source with the winning chunk_index preserved.
        hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="zorblax-marker",
            operation_id="q-marker",
            embedder=_FAKE,
        )
        assert hits, "marker-bearing chunks must surface for the query"
        top = hits[0]
        assert top.chunk_index >= 1, (
            f"top chunk_index must point to the marker paragraph (>=1), got {top.chunk_index}"
        )


async def test_short_note_stays_whole_doc_chunk_index_zero() -> None:
    """Sanity counter-test: a note under the chunking threshold stays
    single-vector (WholeChunker). The lone BlobSource row carries
    chunk_index=0 and retrieve surfaces the blob with chunk_index=0.
    Regression guard for the heuristic in ``pick_chunker``.
    """
    org, user = await _org()
    proj = uuid.uuid4()
    source_id = uuid.uuid4().hex

    short_text = "Short note about the unique-fact-xtb keyword."
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        await mem.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            text_body=short_text,
            operation_id="write-short",
            namespace="note",
            sources=[("note", source_id)],
            embedder=_FAKE,
        )

        rows = (
            await s.execute(
                select(BlobSource.chunk_index).where(
                    BlobSource.source_kind == "note",
                    BlobSource.source_id == source_id,
                )
            )
        ).all()
        assert [r.chunk_index for r in rows] == [0]

        hits = await mem.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=proj,
            query="unique-fact-xtb",
            operation_id="q-short",
            embedder=_FAKE,
        )
        assert hits
        assert hits[0].chunk_index == 0
