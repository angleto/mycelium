"""``index_scope``: the opt-out from automatic search indexing (A1).

DB-driven, same shape as ``test_note_search``: both indexes flush at
``tenant_session`` teardown, so each test mutates in one transaction and
asserts in a fresh one. Uses the deterministic FakeEmbedder seam.

Two of these are the gate of the item and fail against an implementation
that is otherwise plausible. ``test_task_flip_to_none_drops_the_blob``
fails if the scope guard sits AFTER the content_hash short-circuit: a
scope flip leaves the rendered text identical, so the hash matches and
the guard is unreachable on precisely the rows that need the remedy.
``test_note_flip_to_none_drops_every_part`` fails if the note branch was
written as "delete the blob the pointer names", because a note has one
part per blob and one pointer per part.

What is NOT asserted here, because it is not what the column does: that
a scoped-out row stops being readable, or stops being matched by the
free-text ``q=`` of ``list_tasks`` / ``list_notes``. Both still hold at
``none`` by design -- see ``models/index_scope.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import UnprocessableError
from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.memory_blob import BlobSource, MemoryBlob
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.task import Task
from mycelium_core.models.task_index_pointer import TaskIndexPointer
from mycelium_core.services import note_links as nl
from mycelium_core.services import note_parts as np
from mycelium_core.services import note_search, task_search
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as tk
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
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="IS")
    return r.org_id, r.user_id


async def _task_pointer(s, task_id: uuid.UUID) -> TaskIndexPointer | None:
    return (
        await s.execute(select(TaskIndexPointer).where(TaskIndexPointer.task_id == task_id))
    ).scalar_one_or_none()


async def _part_pointer(s, part_id: uuid.UUID) -> NotePartIndexPointer | None:
    return (
        await s.execute(select(NotePartIndexPointer).where(NotePartIndexPointer.part_id == part_id))
    ).scalar_one_or_none()


async def _blob_exists(s, blob_id: uuid.UUID) -> bool:
    count = (
        await s.execute(
            select(func.count()).select_from(MemoryBlob).where(MemoryBlob.id == blob_id)
        )
    ).scalar_one()
    return int(count) > 0


async def _sources_for_part(s, part_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await s.execute(
        select(BlobSource.blob_id).where(
            BlobSource.source_kind == "note_part",
            BlobSource.source_id == str(part_id),
        )
    )
    return [r[0] for r in rows]


async def _note_with_parts(org: uuid.UUID, user: uuid.UUID, bodies: list[str]) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text=bodies[0]
        )
        nid = note.id
    async with tenant_session(str(org), str(user)) as s:
        for body in bodies[1:]:
            await np.create_part(s, org_id=org, actor_id=user, note_id=nid, body=body)
    return nid


async def _part_ids(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> list[uuid.UUID]:
    async with tenant_session(str(org), str(user)) as s:
        return [p.id for p in await np.list_parts(s, org_id=org, note_id=note_id)]


async def _set_note_scope(
    org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID, scope: IndexScope
) -> None:
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.get_note(s, org_id=org, note_id=note_id)
        await nt.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            expected_version=note.version,
            index_scope=scope,
        )


async def test_task_flip_to_none_drops_the_blob(_embedder: None) -> None:
    """The gate of the task side: the flip changes no text, so the
    content_hash is unchanged and a guard placed after the short-circuit
    never runs. Capture the blob id BEFORE the flip -- deleting the blob
    is what removes the pointer, so afterwards there is no id left."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tk.create_task(s, org_id=org, actor_id=user, title="indexed alpha title")
        tid = task.id
    async with tenant_session(str(org), str(user)) as s:
        ptr = await _task_pointer(s, tid)
        assert ptr is not None, "a fresh task is indexed"
        blob_id = ptr.blob_id
        assert await _blob_exists(s, blob_id)
        version = (await tk.get_task(s, org_id=org, task_id=tid)).version
    async with tenant_session(str(org), str(user)) as s:
        await tk.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=version,
            values={"index_scope": IndexScope.none},
        )
    async with tenant_session(str(org), str(user)) as s:
        assert await _task_pointer(s, tid) is None
        assert not await _blob_exists(s, blob_id)
        # The row itself is untouched: this is an indexing opt-out, not a
        # delete and not a read boundary.
        fresh = await tk.get_task(s, org_id=org, task_id=tid)
        assert fresh.title == "indexed alpha title"
        assert fresh.index_scope == IndexScope.none


async def test_note_flip_to_none_drops_every_part(_embedder: None) -> None:
    """The gate of the note side. Asserted per PART and on provenance,
    not on "the blob the pointer names": a note has N parts, so N blobs
    and N pointers, and a single-blob delete would leave N-1 indexed."""
    org, user = await _org()
    nid = await _note_with_parts(org, user, ["beta body one", "beta body two", "beta body three"])
    pids = await _part_ids(org, user, nid)
    assert len(pids) == 3
    async with tenant_session(str(org), str(user)) as s:
        for pid in pids:
            assert await _part_pointer(s, pid) is not None, "every part is indexed on write"
            assert await _sources_for_part(s, pid) != []
    await _set_note_scope(org, user, nid, IndexScope.none)
    async with tenant_session(str(org), str(user)) as s:
        for pid in pids:
            assert await _sources_for_part(s, pid) == [], f"part {pid} still has provenance"
            assert await _part_pointer(s, pid) is None, f"part {pid} still has a pointer"


async def test_note_flip_back_to_org_reindexes_without_touching_bodies(_embedder: None) -> None:
    """A note put back to ``org`` has to be re-indexed by the flip
    itself: no mapper listener is registered on ``Note``, so nothing
    would mark its parts dirty and the note would stay unsearchable
    until each part was edited by hand."""
    org, user = await _org()
    nid = await _note_with_parts(org, user, ["gamma body one", "gamma body two"])
    pids = await _part_ids(org, user, nid)
    await _set_note_scope(org, user, nid, IndexScope.none)
    async with tenant_session(str(org), str(user)) as s:
        parts_before = await np.list_parts(s, org_id=org, note_id=nid)
        before = [(p.id, p.body, p.version) for p in parts_before]
    await _set_note_scope(org, user, nid, IndexScope.org)
    async with tenant_session(str(org), str(user)) as s:
        for pid in pids:
            ptr = await _part_pointer(s, pid)
            assert ptr is not None, f"part {pid} was not re-indexed"
            assert await _blob_exists(s, ptr.blob_id)
        parts_after = await np.list_parts(s, org_id=org, note_id=nid)
        assert [(p.id, p.body, p.version) for p in parts_after] == before, (
            "re-indexing must not rewrite the bodies it re-indexes"
        )


async def test_part_created_while_none_is_not_indexed(_embedder: None) -> None:
    """Why the column is on ``notes`` and not on ``note_part``: a part
    added to a scoped-out note is born scoped out. On ``note_part`` the
    new row would take the ``'org'`` server default and silently
    re-index the note."""
    org, user = await _org()
    nid = await _note_with_parts(org, user, ["delta body one"])
    await _set_note_scope(org, user, nid, IndexScope.none)
    async with tenant_session(str(org), str(user)) as s:
        part = await np.create_part(
            s, org_id=org, actor_id=user, note_id=nid, body="delta body added later"
        )
        new_pid = part.id
    async with tenant_session(str(org), str(user)) as s:
        assert await _part_pointer(s, new_pid) is None
        assert await _sources_for_part(s, new_pid) == []


async def test_backfill_sweeps_skip_scoped_out_rows(_embedder: None) -> None:
    """A row at ``none`` has no pointer by definition, so both sweeps
    would keep selecting it on every tick and fill their (unordered)
    batch with rows the resync throws away, starving the real backlog.
    Asserted on the RETURN COUNT, not on pointer absence: the count is
    what distinguishes "excluded by the SELECT" from "selected, then
    discarded by the guard inside the resync"."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tk.create_task(s, org_id=org, actor_id=user, title="epsilon title")
        tid = task.id
        version = task.version
    nid = await _note_with_parts(org, user, ["epsilon body one", "epsilon body two"])
    # Drain whatever this workspace's signup left unindexed, so the
    # counts below can only be about the two rows under test.
    async with tenant_session(str(org), str(user)) as s:
        while await task_search.run_pointer_backfill(s, batch_size=200):
            pass
        while await note_search.run_pointer_backfill(s, batch_size=200):
            pass
    async with tenant_session(str(org), str(user)) as s:
        await tk.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=version,
            values={"index_scope": IndexScope.none},
        )
    await _set_note_scope(org, user, nid, IndexScope.none)
    async with tenant_session(str(org), str(user)) as s:
        assert await _task_pointer(s, tid) is None, "the flip left the task without a pointer"
        assert await task_search.run_pointer_backfill(s, batch_size=200) == 0
        assert await note_search.run_pointer_backfill(s, batch_size=200) == 0


async def test_scope_out_holds_under_a_project_scoped_session(_embedder: None) -> None:
    """The opt-out must not depend on which perimeter the request armed.

    ``p_memory_blobs`` carries a project term that ``p_blob_sources`` and
    the source tables do not, so a request with ``app.current_project``
    set can write the row while the blob derived from it is invisible.
    Before the flush was made project-blind this was two different silent
    failures: the task blob DELETE matched nothing and the opt-out looked
    applied, and on the note side the provenance went while the blob
    stayed -- text with no provenance, which no erase-by-provenance path
    can reach again.
    """
    org, user = await _org()
    project = str(uuid.uuid4())
    async with tenant_session(str(org), str(user)) as s:
        task = await tk.create_task(s, org_id=org, actor_id=user, title="theta title")
        tid = task.id
        task_version = task.version
    nid = await _note_with_parts(org, user, ["theta body one", "theta body two"])
    pids = await _part_ids(org, user, nid)
    async with tenant_session(str(org), str(user)) as s:
        task_blob = (await _task_pointer(s, tid)).blob_id  # type: ignore[union-attr]
        note_version = (await nt.get_note(s, org_id=org, note_id=nid)).version
    # Both flips issued from a session scoped to a project the blobs do
    # not belong to: a task blob is project_id NULL by construction, and
    # this note has no project tag.
    async with tenant_session(str(org), str(user), project) as s:
        await tk.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=tid,
            expected_version=task_version,
            values={"index_scope": IndexScope.none},
        )
    async with tenant_session(str(org), str(user), project) as s:
        await nt.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=nid,
            expected_version=note_version,
            index_scope=IndexScope.none,
        )
    async with tenant_session(str(org), str(user)) as s:
        assert await _task_pointer(s, tid) is None
        assert not await _blob_exists(s, task_blob)
        for pid in pids:
            assert await _part_pointer(s, pid) is None
            assert await _sources_for_part(s, pid) == []


async def test_default_scope_keeps_a_row_indexed(_embedder: None) -> None:
    """No backfill: the server default is what preserves the behaviour
    of every row that predates the migration. ``SET index_scope =
    DEFAULT`` is the same value such a row was given."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tk.create_task(s, org_id=org, actor_id=user, title="zeta title")
        tid = task.id
    nid = await _note_with_parts(org, user, ["zeta body"])
    pids = await _part_ids(org, user, nid)
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(
            text("UPDATE tasks SET index_scope = DEFAULT WHERE id = :i"), {"i": str(tid)}
        )
        await s.execute(
            text("UPDATE notes SET index_scope = DEFAULT WHERE id = :i"), {"i": str(nid)}
        )
    async with tenant_session(str(org), str(user)) as s:
        assert (await tk.get_task(s, org_id=org, task_id=tid)).index_scope == IndexScope.org
        assert (await nt.get_note(s, org_id=org, note_id=nid)).index_scope == IndexScope.org
        ptr = await _task_pointer(s, tid)
        assert ptr is not None and await _blob_exists(s, ptr.blob_id)
        for pid in pids:
            part_ptr = await _part_pointer(s, pid)
            assert part_ptr is not None and await _blob_exists(s, part_ptr.blob_id)


async def test_scope_only_patch_leaves_the_title_alone(_embedder: None) -> None:
    """``update_note`` re-derives an omitted title from the body, so a
    patch that states neither used to write ``None`` over the title it
    had. Flipping the scope is exactly such a patch."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="Iota", text="iota body"
        )
        nid = note.id
    await _set_note_scope(org, user, nid, IndexScope.none)
    async with tenant_session(str(org), str(user)) as s:
        assert (await nt.get_note(s, org_id=org, note_id=nid)).title == "Iota"


async def test_a_row_created_at_none_is_never_indexed(_embedder: None) -> None:
    """The create doors carry the class too, so a row that must not be
    indexed is never indexed and then unindexed a moment later."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tk.create_task(
            s, org_id=org, actor_id=user, title="kappa title", index_scope=IndexScope.none
        )
        tid = task.id
        note = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            text="kappa body",
            index_scope=IndexScope.none,
        )
        nid = note.id
    async with tenant_session(str(org), str(user)) as s:
        assert await _task_pointer(s, tid) is None
        for pid in [p.id for p in await np.list_parts(s, org_id=org, note_id=nid)]:
            assert await _part_pointer(s, pid) is None
            assert await _sources_for_part(s, pid) == []


async def test_a_part_merged_into_a_scoped_out_note_loses_its_blob(_embedder: None) -> None:
    """The reparent path is why the guard sits before the hash check and
    reads the scope of the part's CURRENT note: ``merge_notes`` moves a
    part without touching its body, so the hash matches and the
    short-circuit would otherwise return before anything looked at the
    destination's scope."""
    org, user = await _org()
    source = await _note_with_parts(org, user, ["lambda body to be merged"])
    target = await _note_with_parts(org, user, ["lambda target body"])
    await _set_note_scope(org, user, target, IndexScope.none)
    moved = (await _part_ids(org, user, source))[0]
    async with tenant_session(str(org), str(user)) as s:
        assert await _part_pointer(s, moved) is not None, "the source note is indexed"
    async with tenant_session(str(org), str(user)) as s:
        await np.merge_notes(
            s, org_id=org, actor_id=user, source_note_id=source, target_note_id=target
        )
    async with tenant_session(str(org), str(user)) as s:
        assert await _part_pointer(s, moved) is None
        assert await _sources_for_part(s, moved) == []


async def test_promote_carries_the_scope_into_the_task_it_becomes(_embedder: None) -> None:
    """The transplant copies the note's own body into the task's
    description, so a task born at the default would put a scoped-out
    note's text straight back into the org-wide index."""
    org, user = await _org()
    nid = await _note_with_parts(org, user, ["mu body that must not be indexed"])
    await _set_note_scope(org, user, nid, IndexScope.none)
    async with tenant_session(str(org), str(user)) as s:
        task, _link = await nl.promote_note_to_task(s, org_id=org, actor_id=user, note_id=nid)
        tid = task.id
    async with tenant_session(str(org), str(user)) as s:
        fresh = await tk.get_task(s, org_id=org, task_id=tid)
        assert fresh.index_scope == IndexScope.none
        assert await _task_pointer(s, tid) is None


async def test_a_promoted_note_can_still_be_scoped_out(_embedder: None) -> None:
    """A transplanted note is read-only for CONTENT (ADR-0029 D2). The
    indexing class is not content, and fencing it too would leave that
    whole class of rows with no way out of the index."""
    org, user = await _org()
    nid = await _note_with_parts(org, user, ["nu body"])
    pids = await _part_ids(org, user, nid)
    async with tenant_session(str(org), str(user)) as s:
        await nl.promote_note_to_task(s, org_id=org, actor_id=user, note_id=nid)
    await _set_note_scope(org, user, nid, IndexScope.none)
    async with tenant_session(str(org), str(user)) as s:
        assert (await nt.get_note(s, org_id=org, note_id=nid)).index_scope == IndexScope.none
        for pid in pids:
            assert await _part_pointer(s, pid) is None


async def test_a_stated_null_is_refused_with_the_field_named(_embedder: None) -> None:
    """``{"index_scope": null}`` is well-formed for a schema that types
    the field as optional, and it can only ever end as a NOT NULL
    violation. Refused where the write funnels, on both entities."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tk.create_task(s, org_id=org, actor_id=user, title="xi title")
        tid, tver = task.id, task.version
    nid = await _note_with_parts(org, user, ["xi body"])
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(UnprocessableError) as excinfo:
            await tk.update_task(
                s,
                org_id=org,
                actor_id=user,
                task_id=tid,
                expected_version=tver,
                values={"index_scope": None},
            )
        assert excinfo.value.params["field"] == "index_scope"
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.get_note(s, org_id=org, note_id=nid)
        with pytest.raises(UnprocessableError):
            await nt.update_note(
                s,
                org_id=org,
                actor_id=user,
                note_id=nid,
                expected_version=note.version,
                index_scope=None,
            )


async def test_scoping_out_a_promoted_note_takes_its_task_with_it(_embedder: None) -> None:
    """The transplant is one thing in two rows. The task holds a verbatim
    copy of the note's body and its blob is written ``project_id=NULL``,
    so a note flipped to ``none`` AFTER the promotion would leave the
    same text in a wider perimeter than the part blobs just dropped."""
    org, user = await _org()
    nid = await _note_with_parts(org, user, ["omicron body that must not stay indexed"])
    async with tenant_session(str(org), str(user)) as s:
        task, _link = await nl.promote_note_to_task(s, org_id=org, actor_id=user, note_id=nid)
        tid = task.id
    async with tenant_session(str(org), str(user)) as s:
        assert await _task_pointer(s, tid) is not None, "the promoted task starts indexed"
    await _set_note_scope(org, user, nid, IndexScope.none)
    async with tenant_session(str(org), str(user)) as s:
        assert (await tk.get_task(s, org_id=org, task_id=tid)).index_scope == IndexScope.none
        assert await _task_pointer(s, tid) is None


async def test_an_in_window_failure_still_surfaces_on_a_project_scoped_request(
    _embedder: None,
) -> None:
    """The index flush runs with the project perimeter neutralised, and
    restoring it on the failure path would issue SQL on a transaction the
    body already poisoned: the resulting ``PendingRollbackError`` is the
    class both flush functions downgrade to a warning, so the caller
    would be told a rolled-back transaction succeeded."""
    org, user = await _org()
    title = f"pi title {uuid.uuid4().hex[:8]}"
    with pytest.raises(IntegrityError):
        async with tenant_session(str(org), str(user), str(uuid.uuid4())) as s:
            await tk.create_task(s, org_id=org, actor_id=user, title=title)
            # Pending ORM state with a dangling FK. Nothing flushes it here;
            # the first flush inside the maintenance window does.
            s.add(
                TaskIndexPointer(
                    task_id=uuid.uuid4(),
                    org_id=org,
                    blob_id=uuid.uuid4(),
                    content_hash="x",
                )
            )
    async with tenant_session(str(org), str(user)) as s:
        rows = (
            await s.execute(select(func.count()).select_from(Task).where(Task.title == title))
        ).scalar_one()
        assert int(rows) == 0, "the transaction rolled back, as the raised error says"
