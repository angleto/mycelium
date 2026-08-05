"""Restoring a revision of a MULTI-PART note.

The note snapshot used to capture the body only as ``transcript``: the
flat ``"\\n\\n"``-join of every part. The restore then wrote that whole
string into ``note_part(ord=0)`` and left parts 1..N in place -- so
restoring a 3-part note produced part0 = the entire body, followed by
parts 1 and 2 again. Every restore of a multi-part note duplicated its
own content, and structure was unrecoverable either way. The only
coverage was single-part, where the bug is invisible.

The fix has two halves, both pinned here:

- snapshots now also carry ``parts`` (id, ord, title, lang, version,
  body), and a restore replays them, so structure survives;
- a snapshot written BEFORE that key existed still only has the flat
  join, which genuinely contains no structure -- but it does contain
  every part's text, so restoring it must make that string the note's
  WHOLE body (part 0, others removed) instead of appending it to the
  parts it already contains.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.entity_revision import EntityRevision
from mycelium_core.models.note import NoteKind
from mycelium_core.services import entity_revisions as revs
from mycelium_core.services import note_parts as parts_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.auth import signup


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="NREVMP",
        )
    return r.org_id, r.user_id


async def _three_part_note(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="one"
        )
        for body in ("two", "three"):
            await parts_svc.create_part(s, org_id=org, actor_id=user, note_id=note.id, body=body)
        return note.id


async def _parts(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> list[tuple[int, str]]:
    async with tenant_session(str(org), str(user)) as s:
        return [(p.ord, p.body) for p in await parts_svc.list_parts(s, org_id=org, note_id=note_id)]


async def _latest_revision(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_NOTE, entity_id=note_id, limit=10
        )
        return rows[0].id


async def test_restore_replays_part_structure(_embedder: None) -> None:
    """The whole point: a 3-part note restored is a 3-part note, with the
    same ids, and its body is NOT duplicated."""
    org, user = await _org()
    note_id = await _three_part_note(org, user)
    async with tenant_session(str(org), str(user)) as s:
        before_ids = [p.id for p in await parts_svc.list_parts(s, org_id=org, note_id=note_id)]
        before_body = await notes_svc.get_body(s, note_id=note_id)
        # Snapshot the current, healthy state.
        note = await notes_svc.get_note(s, org_id=org, note_id=note_id)
        await notes_svc._log_note_revision(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            version_from=note.version,
            version_to=note.version,
            changed_fields=["parts[0].body"],
            channel="api",
            edit_session_id=None,
        )
    rev_id = await _latest_revision(org, user, note_id)

    # Diverge: edit one part, drop another, add a fourth.
    async with tenant_session(str(org), str(user)) as s:
        parts = await parts_svc.list_parts(s, org_id=org, note_id=note_id)
        await parts_svc.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=parts[1].id,
            expected_version=parts[1].version,
            body="TWO CHANGED",
        )
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=parts[2].id)
        await parts_svc.create_part(s, org_id=org, actor_id=user, note_id=note_id, body="four")

    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.get_note(s, org_id=org, note_id=note_id)
        await notes_svc.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            revision_id=rev_id,
            expected_version=note.version,
        )

    assert await _parts(org, user, note_id) == [(0, "one"), (1, "two"), (2, "three")]
    async with tenant_session(str(org), str(user)) as s:
        restored = await parts_svc.list_parts(s, org_id=org, note_id=note_id)
        assert [p.id for p in restored] == before_ids, (
            "a restore returns the SAME parts, not copies"
        )
        assert await notes_svc.get_body(s, note_id=note_id) == before_body
        # A restore is a write: versions go UP, so an editor holding the
        # pre-restore version loses its next save rather than clobbering
        # the restore.
        rewritten = next(p for p in restored if p.id == before_ids[1])
        assert rewritten.version > 1


async def test_restore_of_a_legacy_transcript_only_snapshot_does_not_duplicate(
    _embedder: None,
) -> None:
    """The regression itself, reproduced on a snapshot shaped the way
    every pre-fix revision is: ``transcript`` present, ``parts`` absent.

    The flat join already contains parts 1..N, so writing it into part 0
    and leaving them behind is exactly the duplication bug. The body
    after the restore must equal the snapshot's transcript, no more.
    """
    org, user = await _org()
    note_id = await _three_part_note(org, user)
    # Write the revision in its PRE-FIX shape: the snapshot a sealed row
    # from before migration 0089 carries, i.e. ``transcript`` and no
    # ``parts``. Sealed rows are immutable (DB trigger), so this is
    # appended as-is rather than edited after the fact.
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.get_note(s, org_id=org, note_id=note_id)
        full = await revs.snapshot_note(s, note)
        legacy = {k: v for k, v in full.items() if k != "parts"}
        transcript = legacy["transcript"]
        await revs.append(
            s,
            org_id=org,
            entity_kind=revs.ENTITY_KIND_NOTE,
            entity_id=note_id,
            actor_id=user,
            snapshot=legacy,
            changed_fields=["transcript"],
            channel="api",
            version_from=note.version,
            version_to=note.version,
        )
    rev_id = await _latest_revision(org, user, note_id)
    async with tenant_session(str(org), str(user)) as s:
        stored = (
            await s.execute(select(EntityRevision).where(EntityRevision.id == rev_id))
        ).scalar_one()
        assert "parts" not in (stored.snapshot or {})
    assert transcript == "one\n\ntwo\n\nthree"

    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.get_note(s, org_id=org, note_id=note_id)
        await notes_svc.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            revision_id=rev_id,
            expected_version=note.version,
        )

    async with tenant_session(str(org), str(user)) as s:
        body = await notes_svc.get_body(s, note_id=note_id)
    # Content preserved exactly once. Structure is collapsed, which is
    # all a transcript-only snapshot can express -- but "one" no longer
    # appears twice.
    assert body == transcript
    assert await _parts(org, user, note_id) == [(0, "one\n\ntwo\n\nthree")]


async def test_restore_reclaims_a_part_that_was_trashed_after_the_snapshot(
    _embedder: None,
) -> None:
    """A part live at snapshot time and trashed afterwards comes back as
    a live part, and its trash entry goes -- otherwise a later
    ``restore_part`` would collide with it on the primary key."""
    org, user = await _org()
    note_id = await _three_part_note(org, user)
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.get_note(s, org_id=org, note_id=note_id)
        await notes_svc._log_note_revision(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            version_from=note.version,
            version_to=note.version,
            changed_fields=["parts[0].body"],
            channel="api",
            edit_session_id=None,
        )
        parts = await parts_svc.list_parts(s, org_id=org, note_id=note_id)
        victim = parts[2].id
    rev_id = await _latest_revision(org, user, note_id)

    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=victim)
    async with tenant_session(str(org), str(user)) as s:
        assert [e.id for e in await parts_svc.list_trashed(s, org_id=org, note_id=note_id)] == [
            victim
        ]

    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.get_note(s, org_id=org, note_id=note_id)
        await notes_svc.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            revision_id=rev_id,
            expected_version=note.version,
        )

    async with tenant_session(str(org), str(user)) as s:
        live = [p.id for p in await parts_svc.list_parts(s, org_id=org, note_id=note_id)]
        assert victim in live
        assert await parts_svc.list_trashed(s, org_id=org, note_id=note_id) == []


async def _fields(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> list[list[str]]:
    async with tenant_session(str(org), str(user)) as s:
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_NOTE, entity_id=note_id, limit=50
        )
        return [list(r.changed_fields or []) for r in rows]


async def test_every_structural_part_mutation_writes_a_revision(_embedder: None) -> None:
    """The body mutators always wrote one; create / reorder / trash /
    restore / purge did not, so the timeline claimed nothing happened and
    the preceding snapshot -- the thing a restore actually replays -- was
    never taken. Each verb now leaves its own tagged row."""
    org, user = await _org()
    note_id = await _three_part_note(org, user)
    async with tenant_session(str(org), str(user)) as s:
        parts = await parts_svc.list_parts(s, org_id=org, note_id=note_id)
        victim, keep = parts[2].id, [p.id for p in parts]
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.reorder_parts(
            s, org_id=org, actor_id=user, note_id=note_id, part_ids=list(reversed(keep))
        )
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.trash_part(s, org_id=org, actor_id=user, part_id=victim)
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.restore_part(s, org_id=org, actor_id=user, part_id=victim)
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.delete_part(s, org_id=org, actor_id=user, part_id=victim)

    flat = {tok for row in await _fields(org, user, note_id) for tok in row}
    # create_part fired twice while building the 3-part note.
    assert any(t.endswith("._create") and t.startswith("parts[") for t in flat)
    assert "parts._reorder" in flat
    assert any(t.endswith("._trash") for t in flat)
    assert any(t.endswith("._restore") for t in flat)
    assert any(t.endswith("._purge") for t in flat)


async def test_merge_writes_a_revision_on_both_notes(_embedder: None) -> None:
    """A merge is the biggest edit either note ever takes: the target
    gains every part, the source loses them and is trashed. Both
    timelines have to show it, not just an audit line naming two ids."""
    org, user = await _org()
    source = await _three_part_note(org, user)
    async with tenant_session(str(org), str(user)) as s:
        target_note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="target"
        )
        target = target_note.id
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.merge_notes(
            s, org_id=org, actor_id=user, source_note_id=source, target_note_id=target
        )

    target_fields = {tok for row in await _fields(org, user, target) for tok in row}
    source_fields = {tok for row in await _fields(org, user, source) for tok in row}
    assert "parts._merge_in" in target_fields
    assert "parts._merge_out" in source_fields


async def test_creating_a_part_is_undone_by_restoring_the_previous_revision(
    _embedder: None,
) -> None:
    """What the create revision buys: the snapshot taken before it is a
    real recovery point, so an accidental add is reversible instead of
    only auditable."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text="only"
        )
        note_id = note.id
    before = await _latest_revision(org, user, note_id)
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.create_part(s, org_id=org, actor_id=user, note_id=note_id, body="oops")
    assert await _parts(org, user, note_id) == [(0, "only"), (1, "oops")]

    async with tenant_session(str(org), str(user)) as s:
        n = await notes_svc.get_note(s, org_id=org, note_id=note_id)
        await notes_svc.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            note_id=note_id,
            revision_id=before,
            expected_version=n.version,
        )
    assert await _parts(org, user, note_id) == [(0, "only")]


async def test_gdpr_erase_takes_the_revision_history_with_it(_embedder: None) -> None:
    """A revision snapshot is a FULL COPY of the body -- ``transcript``
    all along, and ``parts`` since migration 0089 -- and no foreign key
    reaches it: ``entity_revision`` is polymorphic on (entity_kind,
    entity_id). What clears it is the AFTER DELETE trigger
    ``trg_note_revision_cascade`` (migration 0006).

    Pinned here because the guarantee lives in a trigger rather than in
    the service code that appears to be responsible for it: nothing in
    ``gdpr_erase_note`` mentions revisions, so a future refactor that
    stops issuing a row DELETE (a soft flag, a rewrite to a bulk path
    that skips triggers) would silently leave every snapshot behind.
    """
    org, user = await _org()
    secret = "zzyzx-erasable-passage"
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, text=secret
        )
        note_id = note.id
    async with tenant_session(str(org), str(user)) as s:
        await parts_svc.create_part(
            s, org_id=org, actor_id=user, note_id=note_id, body=f"{secret} again"
        )

    async with tenant_session(str(org), str(user)) as s:
        rows = await revs.list_revisions(
            s, entity_kind=revs.ENTITY_KIND_NOTE, entity_id=note_id, limit=50
        )
        assert rows, "the note must have a history to begin with"
        assert any(secret in str(r.snapshot) for r in rows), (
            "precondition: the body really is copied into the snapshots"
        )

    async with tenant_session(str(org), str(user)) as s:
        await notes_svc.gdpr_erase_note(s, org_id=org, actor_id=user, note_id=note_id)

    async with tenant_session(str(org), str(user)) as s:
        left = (
            (
                await s.execute(
                    select(EntityRevision).where(
                        EntityRevision.entity_kind == revs.ENTITY_KIND_NOTE,
                        EntityRevision.entity_id == note_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert list(left) == []
