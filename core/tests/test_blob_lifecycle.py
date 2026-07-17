"""Blob lifecycle coherence across the substrates (task c5da112c).

The 2026-07-17 memory audit (note bdc62d7a §2.1) found three legs of the
same hole: soft-deleted notes/tasks kept surfacing through their index
blobs on every blob surface (no retrieval stage read the source row's
``deleted_at``), a trashed humus atom stayed in the humus branch forever,
and the bulk hard-delete paths (``empty_trash``, the retention sweep)
bypassed the ORM listeners that clean the blobs -- orphaned, searchable
rows. These tests pin the fix:

- soft-delete hides the blobs from retrieval (derived predicate, no
  state); restore brings them back with no re-index;
- a trashed humus atom disappears from the walk;
- ``empty_trash`` and ``hard_delete_soft_deleted`` erase the purged
  rows' blobs by provenance (no orphans), while the retention sweep's
  spared originals keep their (hidden) blobs.

Deterministic FakeEmbedder seam; notes/tasks index at ``tenant_session``
teardown (same convention as test_note_search / test_humus_read_path).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import func, select, text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.memory_blob import BlobSource, MemoryBlob
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.task import Task
from mycelium_core.models.task_index_pointer import TaskIndexPointer
from mycelium_core.services import entity_revisions as revs
from mycelium_core.services import memory, trash
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


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
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="BLOBLC")
    return r.org_id, r.user_id


async def _retrieve_texts(org: uuid.UUID, user: uuid.UUID, query: str) -> list[str]:
    async with tenant_session(str(org), str(user)) as s:
        hits = await memory.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=query,
            operation_id=f"op-{uuid.uuid4().hex}",
            limit=10,
        )
        return [h.blob.text or "" for h in hits]


async def _note_version(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> int:
    async with tenant_session(str(org), str(user)) as s:
        return (await s.execute(select(Note.version).where(Note.id == note_id))).scalar_one()


async def _note_blob_ids(s, note_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await s.execute(
                select(NotePartIndexPointer.blob_id).where(NotePartIndexPointer.note_id == note_id)
            )
        )
        .scalars()
        .all()
    )


async def test_soft_deleted_note_blobs_hidden_then_restored(_embedder: None) -> None:
    """Trashing a note removes its part blobs from retrieval (every
    branch, via the effective-source exclusion); restoring it brings
    them back -- no re-index, the perimeter is derived at query time."""
    org, user = await _org()
    token = "xyzygram"
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            text=f"{token} confidential draft body",
        )
        note_id = note.id
    assert any(token in t for t in await _retrieve_texts(org, user, token))

    async with tenant_session(str(org), str(user)) as s:
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            expected_version=await _note_version(org, user, note_id),
        )
    assert not any(token in t for t in await _retrieve_texts(org, user, token))
    # The blob itself is NOT destroyed by a soft-delete (restore must be
    # cheap): only hidden.
    async with tenant_session(str(org), str(user)) as s:
        assert await _note_blob_ids(s, note_id)

    async with tenant_session(str(org), str(user)) as s:
        await nt.restore_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            expected_version=await _note_version(org, user, note_id),
        )
    assert any(token in t for t in await _retrieve_texts(org, user, token))


async def test_trashed_humus_atom_leaves_the_walk(_embedder: None) -> None:
    """A humus-flagged note that lands in the trash stops surfacing --
    both through the boosted humus branch and through the base branches
    (ADR-0041 retention will never hard-delete it, so without this the
    atom would be served forever)."""
    org, user = await _org()
    token = "zorkleatom"
    async with tenant_session(str(org), str(user)) as s:
        atom = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            text=f"{token} distilled lesson",
        )
        atom_id = atom.id
    async with tenant_session(str(org), str(user)) as s:
        note = (await s.execute(select(Note).where(Note.id == atom_id))).scalar_one()
        note.humus_flag = True
    assert any(token in t for t in await _retrieve_texts(org, user, token))

    async with tenant_session(str(org), str(user)) as s:
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=atom_id,
            expected_version=await _note_version(org, user, atom_id),
        )
    assert not any(token in t for t in await _retrieve_texts(org, user, token))


async def test_soft_deleted_task_blob_hidden_then_restored(_embedder: None) -> None:
    """The task-search loader keeps the blob on soft-delete by design
    ("visibility filter applied at search time"): this is that filter on
    the blob surfaces."""
    org, user = await _org()
    token = "qwovexreport"
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title=f"{token} prep")
        task_id, v1 = task.id, task.version
    assert any(token in t for t in await _retrieve_texts(org, user, token))

    async with tenant_session(str(org), str(user)) as s:
        v2 = await tasks_svc.soft_delete_task(
            s, org_id=org, actor_id=user, task_id=task_id, expected_version=v1
        )
    assert not any(token in t for t in await _retrieve_texts(org, user, token))

    async with tenant_session(str(org), str(user)) as s:
        await tasks_svc.restore_task(
            s, org_id=org, actor_id=user, task_id=task_id, expected_version=v2
        )
    assert any(token in t for t in await _retrieve_texts(org, user, token))


async def _blob_and_source_counts(
    org: uuid.UUID, user: uuid.UUID, blob_ids: list[uuid.UUID]
) -> tuple[int, int]:
    async with tenant_session(str(org), str(user)) as s:
        blobs = (
            await s.execute(
                select(func.count()).select_from(MemoryBlob).where(MemoryBlob.id.in_(blob_ids))
            )
        ).scalar_one()
        sources = (
            await s.execute(
                select(func.count()).select_from(BlobSource).where(BlobSource.blob_id.in_(blob_ids))
            )
        ).scalar_one()
    return int(blobs), int(sources)


async def test_empty_trash_erases_blobs_by_provenance(_embedder: None) -> None:
    """The bulk Core DELETE in ``empty_trash`` bypasses the ORM cleanup
    listeners; the explicit provenance erase must leave no orphaned,
    searchable blob behind (note parts AND task)."""
    org, owner = await _org()
    async with tenant_session(str(org), str(owner)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=owner, kind=NoteKind.text, text="plovertrash body"
        )
        task = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="plovertrash job")
        note_id, task_id, task_v = note.id, task.id, task.version
    async with tenant_session(str(org), str(owner)) as s:
        doomed_blobs = await _note_blob_ids(s, note_id)
        doomed_blobs += list(
            (
                await s.execute(
                    select(TaskIndexPointer.blob_id).where(TaskIndexPointer.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(doomed_blobs) >= 2
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=owner,
            note_id=note_id,
            expected_version=(
                await s.execute(select(Note.version).where(Note.id == note_id))
            ).scalar_one(),
        )
        await tasks_svc.soft_delete_task(
            s, org_id=org, actor_id=owner, task_id=task_id, expected_version=task_v
        )
    async with tenant_session(str(org), str(owner)) as s:
        counts = await trash.empty_trash(s, org_id=org, actor_id=owner)
    assert counts == {"tasks": 1, "notes": 1}
    blobs_left, sources_left = await _blob_and_source_counts(org, owner, doomed_blobs)
    assert blobs_left == 0
    assert sources_left == 0
    assert not any("plovertrash" in t for t in await _retrieve_texts(org, owner, "plovertrash"))


async def test_retention_hard_delete_erases_blobs(_embedder: None) -> None:
    """The autonomous retention sweep's raw-SQL DELETE must erase the
    purged rows' blobs by provenance -- and keep sparing the ADR-0041
    originals, whose (hidden) blobs stay intact."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        doomed = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="grimble doomed body"
        )
        spared = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="grimble spared humus"
        )
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="grimble task")
        doomed_id, spared_id, task_id, task_v = doomed.id, spared.id, task.id, task.version
    async with tenant_session(str(org), str(user)) as s:
        doomed_blobs = await _note_blob_ids(s, doomed_id)
        spared_blobs = await _note_blob_ids(s, spared_id)
        task_blobs = list(
            (
                await s.execute(
                    select(TaskIndexPointer.blob_id).where(TaskIndexPointer.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
        assert doomed_blobs and spared_blobs and task_blobs
        # The spared note is humus (an ADR-0041 original).
        spared_note = (await s.execute(select(Note).where(Note.id == spared_id))).scalar_one()
        spared_note.humus_flag = True
        for nid in (doomed_id, spared_id):
            await nt.soft_delete_note(
                s,
                org_id=org,
                actor_id=user,
                note_id=nid,
                expected_version=(
                    await s.execute(select(Note.version).where(Note.id == nid))
                ).scalar_one(),
            )
        await tasks_svc.soft_delete_task(
            s, org_id=org, actor_id=user, task_id=task_id, expected_version=task_v
        )
        await s.execute(
            text("UPDATE notes SET deleted_at = now() - interval '120 days' WHERE id = ANY(:ids)"),
            {"ids": [str(doomed_id), str(spared_id)]},
        )
        await s.execute(
            text("UPDATE tasks SET deleted_at = now() - interval '120 days' WHERE id = :tid"),
            {"tid": str(task_id)},
        )
    async with tenant_session(str(org), str(user)) as s:
        tasks_d, notes_d = await revs.hard_delete_soft_deleted(s, after_days=90)
    assert tasks_d == 1
    assert notes_d == 1  # the humus original is spared
    doomed_left, doomed_sources = await _blob_and_source_counts(org, user, doomed_blobs)
    assert doomed_left == 0
    assert doomed_sources == 0
    # The spared original keeps its blob (hidden by the soft-delete
    # exclusion, recoverable on restore) -- retention never destroys it.
    spared_left, _spared_sources = await _blob_and_source_counts(org, user, spared_blobs)
    assert spared_left == len(spared_blobs)
    # The purged task's INDEX blob (pointer-owned) is erased too.
    task_left, _task_sources = await _blob_and_source_counts(org, user, task_blobs)
    assert task_left == 0
    async with tenant_session(str(org), str(user)) as s:
        assert (
            await s.execute(select(func.count()).select_from(Task).where(Task.id == task_id))
        ).scalar_one() == 0


async def test_citation_memory_survives_retention_dies_on_empty_trash(_embedder: None) -> None:
    """The kind namespace of ``blob_sources`` is shared between index
    provenance and whole-entity citation (an agent memory recording the
    task it came from). The AUTONOMOUS retention sweep must never
    destroy a citation memory (§12); the SOVEREIGN empty-trash mirrors
    gdpr_erase and does."""
    org, owner = await _org()
    async with tenant_session(str(org), str(owner)) as s:
        kept_task = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="kept src")
        binned_task = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="bin src")
        kept_id, kept_v = kept_task.id, kept_task.version
        binned_id, binned_v = binned_task.id, binned_task.version
    async with tenant_session(str(org), str(owner)) as s:
        survivor = await memory.write_blob(
            s,
            org_id=org,
            actor_id=owner,
            project_id=None,
            text_body="vermilion insight from the kept task",
            operation_id=f"op-{uuid.uuid4().hex}",
            namespace="agent",
            sources=[("task", str(kept_id))],
        )
        doomed = await memory.write_blob(
            s,
            org_id=org,
            actor_id=owner,
            project_id=None,
            text_body="vermilion insight from the binned task",
            operation_id=f"op-{uuid.uuid4().hex}",
            namespace="agent",
            sources=[("task", str(binned_id))],
        )
        multi = await memory.write_blob(
            s,
            org_id=org,
            actor_id=owner,
            project_id=None,
            text_body="vermilion insight with a second provenance",
            operation_id=f"op-{uuid.uuid4().hex}",
            namespace="agent",
            sources=[("task", str(binned_id)), ("manual", "note-to-self")],
        )
        survivor_id, doomed_id, multi_id = survivor.id, doomed.id, multi.id
        await tasks_svc.soft_delete_task(
            s, org_id=org, actor_id=owner, task_id=kept_id, expected_version=kept_v
        )
        await s.execute(
            text("UPDATE tasks SET deleted_at = now() - interval '120 days' WHERE id = :tid"),
            {"tid": str(kept_id)},
        )
    # Autonomous sweep: purges the task row, SPARES the citation memory.
    async with tenant_session(str(org), str(owner)) as s:
        tasks_d, _notes_d = await revs.hard_delete_soft_deleted(s, after_days=90)
    assert tasks_d == 1
    blobs_left, sources_left = await _blob_and_source_counts(org, owner, [survivor_id])
    assert blobs_left == 1
    assert sources_left == 1  # the dangling ('task', kept_id) citation stays
    # Sovereign empty-trash: cascades like gdpr_erase -- the sole-source
    # citation dies, the multi-source memory survives minus one source.
    async with tenant_session(str(org), str(owner)) as s:
        await tasks_svc.soft_delete_task(
            s, org_id=org, actor_id=owner, task_id=binned_id, expected_version=binned_v
        )
    async with tenant_session(str(org), str(owner)) as s:
        await trash.empty_trash(s, org_id=org, actor_id=owner)
    doomed_left, _ = await _blob_and_source_counts(org, owner, [doomed_id])
    assert doomed_left == 0
    multi_left, multi_sources = await _blob_and_source_counts(org, owner, [multi_id])
    assert multi_left == 1
    assert multi_sources == 1  # only the 'manual' provenance remains
