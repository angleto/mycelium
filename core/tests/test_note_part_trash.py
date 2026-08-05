"""Restorable delete for note parts (migration 0089).

``delete_note_part`` was the only destructive note operation with no
inverse: it dropped the row and the search blob and left the body
recoverable from nowhere. Being irreversible, it belonged on the
``delete:notes`` danger key -- which meant removing one block of a note,
a routine editing act, was unreachable for every ordinary assistant
(``DEFAULT_SCOPES`` is reads-only, danger keys are opt-in). These tests
pin the pair that fixes it:

- trash removes the part from the note and from search, restore puts it
  back byte-for-byte WITH ITS ORIGINAL ID, at the ord it held;
- a restore whose slot was taken lands in position, shifting the rest;
- the purge (``delete_part``) is still irreversible and now reaches a
  part in either state;
- emptying the workspace bin empties the part trash too.

Deterministic FakeEmbedder seam; parts index at ``tenant_session``
teardown (same convention as test_note_search / test_blob_lifecycle).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_part import NotePart, NotePartTrash
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.services import note_parts as parts_svc
from mycelium_core.services import notes as nt
from mycelium_core.services import trash as trash_svc
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
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="NPTRASH")
    return r.org_id, r.user_id


async def _note_with_parts(
    org: uuid.UUID, user: uuid.UUID, bodies: list[str]
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """A note whose body is ``bodies``: the first lands in part(ord=0)
    via ``create_note``, the rest are appended as parts 1..N."""
    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text=bodies[0]
        )
        note_id = note.id
        for body in bodies[1:]:
            await parts_svc.create_part(s, org_id=org, actor_id=user, note_id=note_id, body=body)
    async with tenant_session(str(org), str(user)) as s:
        parts = await parts_svc.list_parts(s, org_id=org, note_id=note_id)
        return note_id, [p.id for p in parts]


async def _bodies(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> list[str]:
    async with tenant_session(str(org), str(user)) as s:
        return [p.body for p in await parts_svc.list_parts(s, org_id=org, note_id=note_id)]


async def test_trash_then_restore_round_trips_the_part(_embedder: None) -> None:
    """The inverse pair. The restored part is the SAME part -- same id,
    same body/title/lang, same ord, same version -- not a copy, so ids
    captured before the trash still resolve and a stale
    ``expected_version`` still loses."""
    org, user = await _org()
    note_id, part_ids = await _note_with_parts(org, user, ["alpha", "beta", "gamma"])
    target = part_ids[1]

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=target,
            expected_version=1,
            body="beta",
            title="Beta",
            lang="en",
        )
    async with tenant_session(str(org), str(user)) as s:
        before = await parts_svc.get_part(s, org_id=org, part_id=target)
        snap = (before.body, before.title, before.lang, before.ord, before.version)

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=target)

    assert await _bodies(org, user, note_id) == ["alpha", "gamma"]
    async with tenant_session(str(org), str(user)) as s:
        # Gone from the live table, present in the trash, and the note
        # body no longer contains it.
        with pytest.raises(NotFoundError):
            await parts_svc.get_part(s, org_id=org, part_id=target)
        listing = await parts_svc.list_trashed(s, org_id=org, note_id=note_id)
        assert [e.id for e in listing] == [target]
        assert "beta" not in await nt.get_body(s, note_id=note_id)

    async with tenant_session(str(org), str(user)) as s:
        restored = await parts_svc.restore_part(s, org_id=org, actor_id=user, part_id=target)
        assert restored.id == target

    async with tenant_session(str(org), str(user)) as s:
        after = await parts_svc.get_part(s, org_id=org, part_id=target)
        assert (after.body, after.title, after.lang, after.ord, after.version) == snap
        assert await parts_svc.list_trashed(s, org_id=org, note_id=note_id) == []
    assert await _bodies(org, user, note_id) == ["alpha", "beta", "gamma"]


async def test_restore_shifts_survivors_when_the_slot_was_taken(_embedder: None) -> None:
    """A part restored into an occupied ord lands in position, pushing
    the parts at or after it forward -- it does not get appended at the
    end, and the deferred unique constraint tolerates the shift."""
    org, user = await _org()
    note_id, part_ids = await _note_with_parts(org, user, ["alpha", "beta", "gamma"])
    target = part_ids[1]  # ord=1

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=target)
        # Someone fills the hole while the part sits in the trash.
        await parts_svc.create_part(
            s, org_id=org, actor_id=user, note_id=note_id, body="delta", ord=1
        )
    assert await _bodies(org, user, note_id) == ["alpha", "delta", "gamma"]

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.restore_part(s, org_id=org, actor_id=user, part_id=target)

    assert await _bodies(org, user, note_id) == ["alpha", "beta", "delta", "gamma"]
    async with tenant_session(str(org), str(user)) as s:
        ords = [p.ord for p in await parts_svc.list_parts(s, org_id=org, note_id=note_id)]
        assert len(set(ords)) == len(ords), "ords must stay unique after the shift"


async def test_trash_drops_the_search_blob_and_restore_reindexes(_embedder: None) -> None:
    """A trashed part must not keep surfacing in search, so its blob goes
    with it; restoring mints a fresh one from the restored row."""
    org, user = await _org()
    _note_id, part_ids = await _note_with_parts(org, user, ["alpha", "zzyzxian beta"])
    target = part_ids[1]

    async with tenant_session(str(org), str(user)) as s:
        pointers = (
            (
                await s.execute(
                    select(NotePartIndexPointer.part_id).where(
                        NotePartIndexPointer.part_id == target
                    )
                )
            )
            .scalars()
            .all()
        )
        assert list(pointers) == [target], "the part indexes on write"

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=target)
    async with tenant_session(str(org), str(user)) as s:
        gone = (
            (
                await s.execute(
                    select(NotePartIndexPointer.part_id).where(
                        NotePartIndexPointer.part_id == target
                    )
                )
            )
            .scalars()
            .all()
        )
        assert list(gone) == []

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.restore_part(s, org_id=org, actor_id=user, part_id=target)
    async with tenant_session(str(org), str(user)) as s:
        back = (
            (
                await s.execute(
                    select(NotePartIndexPointer.part_id).where(
                        NotePartIndexPointer.part_id == target
                    )
                )
            )
            .scalars()
            .all()
        )
        assert list(back) == [target], "restore re-indexes the part"


async def test_trash_honours_an_explicit_expected_version(_embedder: None) -> None:
    """The optional concurrency guard: a caller that read the part before
    someone else edited it must not silently trash the newer body."""
    org, user = await _org()
    note_id, part_ids = await _note_with_parts(org, user, ["alpha", "beta"])
    target = part_ids[1]

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.update_part(
            s, org_id=org, actor_id=user, part_id=target, expected_version=1, body="beta edited"
        )
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(ConflictError):
            await parts_svc.trash_part(
                s, org_id=org, actor_id=user, part_id=target, expected_version=1
            )
    # Nothing was trashed.
    assert await _bodies(org, user, note_id) == ["alpha", "beta edited"]

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=target, expected_version=2)
    assert await _bodies(org, user, note_id) == ["alpha"]


async def test_purge_reaches_a_part_in_either_state(_embedder: None) -> None:
    """``delete_part`` is the irreversible one. It destroys a live part
    outright, and destroys an already-trashed one's entry without a
    restore-then-delete dance -- after which nothing is restorable."""
    org, user = await _org()
    note_id, part_ids = await _note_with_parts(org, user, ["alpha", "beta", "gamma"])
    live, trashed = part_ids[1], part_ids[2]

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=trashed)
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.delete_part(s, org_id=org, actor_id=user, part_id=live)
        await parts_svc.delete_part(s, org_id=org, actor_id=user, part_id=trashed)

    async with tenant_session(str(org), str(user)) as s:
        assert await parts_svc.list_trashed(s, org_id=org, note_id=note_id) == []
        with pytest.raises(NotFoundError) as err:
            await parts_svc.restore_part(s, org_id=org, actor_id=user, part_id=trashed)
        assert err.value.code is MessageCode.NOTE_PART_NOT_TRASHED
    assert await _bodies(org, user, note_id) == ["alpha"]


async def test_restore_rejects_an_unknown_or_live_part(_embedder: None) -> None:
    """``note.part.not_trashed`` is a distinct signal from "no such
    part": it tells the caller the part is fine where it is."""
    org, user = await _org()
    _note_id, part_ids = await _note_with_parts(org, user, ["alpha", "beta"])
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError) as live_err:
            await parts_svc.restore_part(s, org_id=org, actor_id=user, part_id=part_ids[0])
        assert live_err.value.code is MessageCode.NOTE_PART_NOT_TRASHED
        with pytest.raises(NotFoundError):
            await parts_svc.restore_part(s, org_id=org, actor_id=user, part_id=uuid.uuid4())


async def test_trash_refuses_a_promoted_note(_embedder: None) -> None:
    """A note transplanted into a task is read-only (docs/adr/0029 D2);
    the restorable delete inherits that guard like every other part
    mutation."""
    org, user = await _org()
    note_id, part_ids = await _note_with_parts(org, user, ["alpha", "beta"])
    async with tenant_session(str(org), str(user)) as s:
        note = (await s.execute(select(Note).where(Note.id == note_id))).scalar_one()
        note.promoted_at = note.created_at
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError) as err:
            await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=part_ids[1])
        assert err.value.code is MessageCode.NOTE_PROMOTED_READONLY


async def test_empty_trash_purges_trashed_parts_of_live_notes(_embedder: None) -> None:
    """Emptying the workspace bin empties the part trash too -- including
    parts belonging to notes that are very much alive, which no note
    purge would ever reach."""
    org, user = await _org()
    note_id, part_ids = await _note_with_parts(org, user, ["alpha", "beta"])
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=part_ids[1])

    async with tenant_session(str(org), str(user)) as s:
        counts = await trash_svc.empty_trash(s, org_id=org, actor_id=user)
        assert counts["note_parts"] == 1

    async with tenant_session(str(org), str(user)) as s:
        assert await parts_svc.list_trashed(s, org_id=org, note_id=note_id) == []
        rows = (
            (await s.execute(select(NotePartTrash.id).where(NotePartTrash.org_id == org)))
            .scalars()
            .all()
        )
        assert list(rows) == []
        # The note and its surviving part are untouched.
        live = (
            (await s.execute(select(NotePart.id).where(NotePart.note_id == note_id)))
            .scalars()
            .all()
        )
        assert list(live) == [part_ids[0]]


async def test_purging_the_note_takes_its_trashed_parts_with_it(_embedder: None) -> None:
    """No bodies survive a note purge in the side table: the FK cascade
    on ``note_id`` clears the trash entries with the note row."""
    org, user = await _org()
    note_id, part_ids = await _note_with_parts(org, user, ["alpha", "beta"])
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=part_ids[1])
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            expected_version=(
                await s.execute(select(Note.version).where(Note.id == note_id))
            ).scalar_one(),
        )
    async with tenant_session(str(org), str(user)) as s:
        await trash_svc.empty_trash(s, org_id=org, actor_id=user)
    async with tenant_session(str(org), str(user)) as s:
        rows = (
            (await s.execute(select(NotePartTrash.id).where(NotePartTrash.org_id == org)))
            .scalars()
            .all()
        )
        assert list(rows) == []
