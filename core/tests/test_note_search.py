"""Note search index (services.note_search): one memory blob per note PART.

DB-driven. The per-part index flushes at ``tenant_session`` teardown
(deferred, like task search), so each test mutates in one transaction and
asserts in a fresh one once the indexing flush of the previous one has
landed. Uses the deterministic FakeEmbedder seam.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import delete, select

from flow_core.db import admin_session, tenant_session
from flow_core.embedder import set_embedder_override
from flow_core.models.memory_blob import BlobSource, MemoryBlob
from flow_core.models.note import NoteKind
from flow_core.models.note_part_index_pointer import NotePartIndexPointer
from flow_core.services import note_parts as np
from flow_core.services import note_search
from flow_core.services import notes as nt
from flow_core.services.auth import signup


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
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="NS")
    return r.org_id, r.user_id


async def _pointer(s, part_id: uuid.UUID) -> NotePartIndexPointer | None:
    return (
        await s.execute(select(NotePartIndexPointer).where(NotePartIndexPointer.part_id == part_id))
    ).scalar_one_or_none()


async def _blob(s, blob_id: uuid.UUID) -> MemoryBlob | None:
    res = await s.execute(select(MemoryBlob).where(MemoryBlob.id == blob_id))
    return res.scalar_one_or_none()


async def test_create_note_indexes_part_zero(_embedder: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="searchable alpha body"
        )
        nid = note.id
    async with tenant_session(str(org), str(user)) as s:
        parts = await np.list_parts(s, org_id=org, note_id=nid)
        assert len(parts) == 1
        ptr = await _pointer(s, parts[0].id)
        assert ptr is not None
        assert ptr.note_id == nid
        blob = await _blob(s, ptr.blob_id)
        assert blob is not None
        assert blob.namespace == "note"
        assert "searchable alpha body" in (blob.text or "")
        # The blob is sourced from the PART (not the note): per-part index.
        srcs = (
            (await s.execute(select(BlobSource).where(BlobSource.blob_id == ptr.blob_id)))
            .scalars()
            .all()
        )
        assert any(x.source_kind == "note_part" and x.source_id == str(parts[0].id) for x in srcs)


async def test_update_part_reembeds_on_text_change(_embedder: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="first version"
        )
        nid = note.id
    async with tenant_session(str(org), str(user)) as s:
        parts = await np.list_parts(s, org_id=org, note_id=nid)
        pid, ver = parts[0].id, parts[0].version
        old_hash = (await _pointer(s, pid)).content_hash  # type: ignore[union-attr]
    async with tenant_session(str(org), str(user)) as s:
        await np.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=pid,
            expected_version=ver,
            body="second totally different",
        )
    async with tenant_session(str(org), str(user)) as s:
        ptr = await _pointer(s, pid)
        assert ptr is not None
        assert ptr.content_hash != old_hash
        blob = await _blob(s, ptr.blob_id)
        assert blob is not None
        assert "second totally different" in (blob.text or "")
        assert "first version" not in (blob.text or "")


async def test_append_reindexes_at_is_last(_embedder: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text="head")
        nid = note.id
    async with tenant_session(str(org), str(user)) as s:
        parts = await np.list_parts(s, org_id=org, note_id=nid)
        pid, ver = parts[0].id, parts[0].version
    async with tenant_session(str(org), str(user)) as s:
        await np.append_to_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=pid,
            chunk=" tailtoken",
            expected_version=ver,
            is_last=True,
        )
    async with tenant_session(str(org), str(user)) as s:
        ptr = await _pointer(s, pid)
        assert ptr is not None
        blob = await _blob(s, ptr.blob_id)
        assert blob is not None
        assert "tailtoken" in (blob.text or "")


async def test_delete_part_drops_blob(_embedder: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text="base")
        extra = await np.create_part(
            s, org_id=org, actor_id=user, note_id=note.id, body="disposable part"
        )
        epid = extra.id
    async with tenant_session(str(org), str(user)) as s:
        ptr = await _pointer(s, epid)
        assert ptr is not None
        blob_id = ptr.blob_id
        await np.delete_part(s, org_id=org, actor_id=user, part_id=epid)
    async with tenant_session(str(org), str(user)) as s:
        assert await _pointer(s, epid) is None
        assert await _blob(s, blob_id) is None


async def test_pointer_backfill_recreates_missing(_embedder: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="backfill me please"
        )
        nid = note.id
    # Simulate a pre-index part: drop its blob (the pointer cascades).
    async with tenant_session(str(org), str(user)) as s:
        parts = await np.list_parts(s, org_id=org, note_id=nid)
        pid = parts[0].id
        ptr = await _pointer(s, pid)
        assert ptr is not None
        await s.execute(delete(MemoryBlob).where(MemoryBlob.id == ptr.blob_id))
    async with tenant_session(str(org), str(user)) as s:
        assert await _pointer(s, pid) is None
        indexed = await note_search.run_pointer_backfill(s, batch_size=50)
        assert indexed >= 1
    async with tenant_session(str(org), str(user)) as s:
        ptr = await _pointer(s, pid)
        assert ptr is not None
        blob = await _blob(s, ptr.blob_id)
        assert blob is not None
        assert "backfill me please" in (blob.text or "")
