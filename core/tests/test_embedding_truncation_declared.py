"""The retrieval meta says when a hit's vector covers only its head.

Task ef3f477b (C8). A row longer than the local embedder's sequence
window is truncated by the encoder, so its vector represents the
beginning of the document while the row reads as fully indexed. Task
11154e32 removed that for notes by chunking them, and only for notes:
``pick_chunker`` returns ``WholeChunker`` for every other namespace and
``task_search`` never calls it, so a long task description or a long
agent memory is still one vector at any length.

Two properties are under test, and the second is the one that keeps the
first honest: the signal must fire on a real over-window hit, and it must
stay DISJOINT from ``keyword_only_hits``. A row with no vector has
nothing to truncate; if the two signals overlapped, one workspace's
keyword-only degradation would read as a truncation problem and nobody
would look for the missing embedder.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import EmbedResult, EmbedSide, set_embedder_override
from mycelium_core.services import memory, task_search
from mycelium_core.services.auth import signup

MARKER = "qwlfjx"


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


class _DeadEmbedder:
    """Never produces a vector: the row lands keyword-only, which is the
    state ``keyword_only_hits`` exists to name."""

    model_id = "dead-embed"

    async def embed(self, text: str, *, side: EmbedSide) -> EmbedResult:
        del text, side
        raise RuntimeError("no model on this deployment")


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="TRUNC")
    return r.org_id, r.user_id


def _over_window_text() -> str:
    """Comfortably past the window in the shared chars/4 estimate, so the
    boundary error of that heuristic cannot decide the test."""
    window = get_settings().embedder_max_seq_tokens
    words = [MARKER] + [f"parola{i % 97}" for i in range(window * 2)]
    return " ".join(words)


def _under_window_text() -> str:
    return f"{MARKER} una riga corta che sta comodamente dentro la finestra"


async def _write(org: uuid.UUID, user: uuid.UUID, text: str) -> None:
    async with tenant_session(str(org), str(user)) as s:
        await memory.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body=text,
            operation_id=f"op-{uuid.uuid4().hex}",
            namespace="agent",
        )


async def _meta(org: uuid.UUID, user: uuid.UUID) -> memory.RetrievalMeta:
    async with tenant_session(str(org), str(user)) as s:
        _hits, meta = await memory.retrieve_with_meta(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=MARKER,
            operation_id=f"op-{uuid.uuid4().hex}",
            limit=10,
        )
        return meta


async def test_a_hit_longer_than_the_window_is_declared(_embedder: None) -> None:
    org, user = await _org()
    await _write(org, user, _over_window_text())
    meta = await _meta(org, user)
    assert meta.embedding_truncated_hits == 1
    # And it is NOT reported as keyword-only: the row has a vector, it is
    # just a vector of the beginning.
    assert meta.keyword_only_hits == 0


async def test_a_hit_inside_the_window_is_not_declared(_embedder: None) -> None:
    """The half that a always-true implementation would pass: without it,
    "declare truncation" and "declare every hit" are the same code."""
    org, user = await _org()
    await _write(org, user, _under_window_text())
    meta = await _meta(org, user)
    assert meta.embedding_truncated_hits == 0
    assert meta.keyword_only_hits == 0


async def test_a_keyword_only_hit_is_not_counted_as_truncated() -> None:
    """Long text, no vector. It belongs to ``keyword_only_hits`` and to
    nothing else: a row with no embedding has no head for the embedding to
    cover, and counting it here would let a missing embedder read as a
    document-length problem."""
    org, user = await _org()
    set_embedder_override(_DeadEmbedder)
    try:
        await _write(org, user, _over_window_text())
    finally:
        set_embedder_override(None)
    set_embedder_override(FakeEmbedder)
    try:
        meta = await _meta(org, user)
    finally:
        set_embedder_override(None)
    assert meta.keyword_only_hits == 1
    assert meta.embedding_truncated_hits == 0


async def test_the_unified_surface_declares_it_per_hit_and_in_the_meta(
    _embedder: None,
) -> None:
    """The unified meta recomputes over the FINAL list, after the per-note
    collapse and the cross-kind dedup, so the fact has to travel on the hit
    and not only in the branch meta it came from."""
    org, user = await _org()
    await _write(org, user, _over_window_text())
    async with tenant_session(str(org), str(user)) as s:
        hits, meta = await task_search.search_unified_with_meta(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=MARKER,
            kinds=["blob"],
            tag_ids=[],
            channel_keys=[],
            limit=5,
            include_archived=False,
            include_deleted=False,
            operation_id=f"op-{uuid.uuid4().hex}",
        )
    assert hits, "the fixture must be retrievable, or nothing is under test"
    assert all(h.embedding_truncated for h in hits)
    assert meta.embedding_truncated_hits == len(hits)
