"""Note garden ecosystem (docs/adr/0029 P1) tests.

Covers:

- ``set_maturity`` manual override, refused on a transplanted note
- ``link_notes`` / ``unlink_notes`` typed M:N, idempotent
- ``derive_task_from_note``: note stays alive, ``derived_from`` link
- ``promote_note_to_task``: ``promoted_at`` set, ``promoted_from``
  link, subsequent ``set_maturity`` refused
- ``start_task_on_note`` / ``record_task_artifact`` typed links
- ``tick_maturity_transitions`` seasonal rules (seed -> growing on
  touch, growing -> dormant on age, dormant -> growing on touch)
- backfill migration 0088 lifted existing ``notes.task_id`` rows
  into ``note_task_link(kind='artifact')`` (sampled via a fresh
  note created with the legacy column path)
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError, NotFoundError
from flow_core.models.note import Note, NoteKind, NoteMaturity
from flow_core.models.note_link import NoteNoteLink, NoteTaskLink
from flow_core.services import note_links
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _make_workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="GRD")
    # Give the user a handle so the identity sync trigger populates an
    # ``identities`` row (set_maturity / link_notes look it up to
    # attribute ``created_by``).
    handle = f"h{uuid.uuid4().hex[:8]}"
    async with admin_session() as s:
        await s.execute(
            text("UPDATE users SET handle = :h WHERE id = :u"),
            {"h": handle, "u": str(a.user_id)},
        )
        await s.execute(
            text("SELECT set_config('app.current_org', :o, true)"),
            {"o": str(a.org_id)},
        )
        await s.execute(
            text(
                "INSERT INTO identities (org_id, kind, handle, user_id) "
                "VALUES (:o, 'user', :h, :u) "
                "ON CONFLICT (org_id, handle) DO NOTHING"
            ),
            {"o": str(a.org_id), "h": handle, "u": str(a.user_id)},
        )
    return a.org_id, a.user_id


async def _make_note(s: object, org: uuid.UUID, user: uuid.UUID, title: str = "n") -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body of {title}",
    )


# ---------------------------------------------------------------------------
# Maturity
# ---------------------------------------------------------------------------


async def test_default_maturity_is_seed() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user)
    assert n.maturity == NoteMaturity.seed.value


async def test_set_maturity_audited_and_persisted() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user)
        updated = await note_links.set_maturity(
            s, org_id=org, actor_id=user, note_id=n.id, maturity="growing"
        )
        assert updated.maturity == "growing"
        # Re-read fresh
        reloaded = (await s.execute(select(Note).where(Note.id == n.id))).scalar_one()
        assert reloaded.maturity == "growing"


async def test_set_maturity_rejects_invalid_value() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user)
        with pytest.raises((DomainError, NotFoundError)):
            await note_links.set_maturity(
                s, org_id=org, actor_id=user, note_id=n.id, maturity="rotten"
            )


# ---------------------------------------------------------------------------
# Note <-> Note links
# ---------------------------------------------------------------------------


async def test_link_and_unlink_notes_typed() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        parent = await _make_note(s, org, user, "index")
        child = await _make_note(s, org, user, "atom")
        link = await note_links.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=parent.id,
            child_note_id=child.id,
            kind="atom_of",
        )
        assert link.kind == "atom_of"
        # Idempotent
        link2 = await note_links.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=parent.id,
            child_note_id=child.id,
            kind="atom_of",
        )
        assert link2.id == link.id
        # Outgoing visible from parent, incoming from child
        out, inc = await note_links.list_note_links(s, org_id=org, note_id=parent.id)
        assert len(out) == 1
        assert len(inc) == 0
        out2, inc2 = await note_links.list_note_links(s, org_id=org, note_id=child.id)
        assert len(out2) == 0
        assert len(inc2) == 1
        # Unlink
        removed = await note_links.unlink_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=parent.id,
            child_note_id=child.id,
            kind="atom_of",
        )
        assert removed is True
        # Second unlink is a no-op
        removed2 = await note_links.unlink_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=parent.id,
            child_note_id=child.id,
            kind="atom_of",
        )
        assert removed2 is False


async def test_link_notes_rejects_self_link_and_unknown_kind() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user)
        with pytest.raises((DomainError, NotFoundError)):
            await note_links.link_notes(
                s,
                org_id=org,
                actor_id=user,
                parent_note_id=n.id,
                child_note_id=n.id,
                kind="references",
            )
        other = await _make_note(s, org, user, "other")
        with pytest.raises((DomainError, NotFoundError)):
            await note_links.link_notes(
                s,
                org_id=org,
                actor_id=user,
                parent_note_id=n.id,
                child_note_id=other.id,
                kind="unknown_kind",
            )


# ---------------------------------------------------------------------------
# Named lifecycle operations
# ---------------------------------------------------------------------------


async def test_derive_task_from_note_keeps_note_alive() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _make_note(s, org, user, "idea")
        task, link = await note_links.derive_task_from_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            title="action from idea",
        )
        assert link.kind == "derived_from"
        # Note still alive (no promoted_at)
        reloaded = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
        assert reloaded.promoted_at is None
        # And task points back via link
        links = await note_links.list_note_task_links(s, org_id=org, note_id=note.id)
        assert any(lk.task_id == task.id and lk.kind == "derived_from" for lk in links)


async def test_promote_note_to_task_marks_promoted_and_blocks_maturity() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _make_note(s, org, user, "to-promote")
        task, link = await note_links.promote_note_to_task(
            s, org_id=org, actor_id=user, note_id=note.id
        )
        assert link.kind == "promoted_from"
        reloaded = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
        assert reloaded.promoted_at is not None
        # Cannot re-promote
        with pytest.raises((DomainError, NotFoundError)):
            await note_links.promote_note_to_task(s, org_id=org, actor_id=user, note_id=note.id)
        # And maturity is now read-only
        with pytest.raises((DomainError, NotFoundError)):
            await note_links.set_maturity(
                s, org_id=org, actor_id=user, note_id=note.id, maturity="mature"
            )
        # Task has been created and link is queryable
        links = await note_links.list_note_task_links(s, org_id=org, task_id=task.id)
        assert any(lk.kind == "promoted_from" for lk in links)


async def test_start_task_on_note_and_record_artifact() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _make_note(s, org, user, "subject-note")
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="work on note",
            estimate_effort_h=Decimal(1),
        )
        sub_link = await note_links.start_task_on_note(
            s, org_id=org, actor_id=user, task_id=task.id, note_id=note.id
        )
        assert sub_link.kind == "subject"
        # Same task closes with an artifact note (re-use note for the
        # smoke test: a real workflow would create a new note).
        art_link = await note_links.record_task_artifact(
            s, org_id=org, actor_id=user, task_id=task.id, note_id=note.id
        )
        assert art_link.kind == "artifact"
        links = await note_links.list_note_task_links(s, org_id=org, task_id=task.id)
        kinds = {lk.kind for lk in links}
        assert {"subject", "artifact"}.issubset(kinds)


# ---------------------------------------------------------------------------
# Maturity worker tick (seasonal rules)
# ---------------------------------------------------------------------------


async def test_tick_promotes_seed_to_growing_on_recent_touch() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user, "fresh")
        # The note's ``updated_at`` is now -- the tick should pick it up
        # for the seed->growing transition (touched within window).
        counters = await note_links.tick_maturity_transitions(s, org_id=org, actor_id=user)
        assert counters["seed_to_growing"] >= 1
        reloaded = (await s.execute(select(Note).where(Note.id == n.id))).scalar_one()
        assert reloaded.maturity == NoteMaturity.growing.value


async def test_tick_marks_old_growing_as_dormant() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user, "old")
        # Force the note into growing and age its updated_at past the
        # dormant threshold.
        n.maturity = NoteMaturity.growing.value
        await s.flush()
        far_past = dt.datetime.now(dt.UTC) - dt.timedelta(days=120)
        await s.execute(
            text("UPDATE notes SET updated_at = :t WHERE id = :id"),
            {"t": far_past, "id": str(n.id)},
        )
        counters = await note_links.tick_maturity_transitions(s, org_id=org, actor_id=user)
        assert counters["to_dormant"] >= 1
        reloaded = (await s.execute(select(Note).where(Note.id == n.id))).scalar_one()
        assert reloaded.maturity == NoteMaturity.dormant.value


async def test_tick_resurrects_dormant_on_recent_touch() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await _make_note(s, org, user, "revived")
        n.maturity = NoteMaturity.dormant.value
        await s.flush()
        counters = await note_links.tick_maturity_transitions(s, org_id=org, actor_id=user)
        assert counters["dormant_to_growing"] >= 1
        reloaded = (await s.execute(select(Note).where(Note.id == n.id))).scalar_one()
        assert reloaded.maturity == NoteMaturity.growing.value


# ---------------------------------------------------------------------------
# Migration 0088 backfill (Proposal A -> note_task_link)
# ---------------------------------------------------------------------------


async def test_create_note_for_task_writes_artifact_link() -> None:
    """docs/adr/0029 P3: ``create_note_for_task`` now writes a typed
    ``artifact`` link instead of setting the legacy ``note.task_id``
    column (dropped in migration 0089). The artifact link is the
    canonical Proposal A surface."""
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="legacy task",
            estimate_effort_h=Decimal(1),
        )
        note = await notes_svc.create_note_for_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=task.id,
            text="work note",
        )
        # Explicit typed link of a *different* kind must coexist with
        # the artifact link auto-written by create_note_for_task.
        sub = await note_links.start_task_on_note(
            s, org_id=org, actor_id=user, task_id=task.id, note_id=note.id
        )
        assert sub.kind == "subject"
        links = await note_links.list_note_task_links(s, org_id=org, note_id=note.id)
        kinds = {lk.kind for lk in links}
        assert {"artifact", "subject"}.issubset(kinds)
        # And primary_task_id_for_note resolves to this task via the
        # priority order.
        pid = await note_links.primary_task_id_for_note(s, org_id=org, note_id=note.id)
        assert pid == task.id


async def test_derived_task_ids_for_notes_batches_and_filters() -> None:
    """`derived_task_ids_for_notes` returns the fruit/transplant tasks
    grouped per note. Subject/artifact links are excluded; the SPA's
    "N tasks" chip counts only what the note has generated."""
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n_with = await _make_note(s, org, user, "with-fruits")
        n_empty = await _make_note(s, org, user, "no-fruits")
        # Two derived tasks (fruit) on the first note...
        t1, _ = await note_links.derive_task_from_note(
            s, org_id=org, actor_id=user, note_id=n_with.id, title="fruit 1"
        )
        t2, _ = await note_links.derive_task_from_note(
            s, org_id=org, actor_id=user, note_id=n_with.id, title="fruit 2"
        )
        # ...and a subject link too, which must NOT appear in the result.
        subject_task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="working on it"
        )
        await note_links.start_task_on_note(
            s, org_id=org, actor_id=user, task_id=subject_task.id, note_id=n_with.id
        )

        out = await note_links.derived_task_ids_for_notes(
            s, org_id=org, note_ids=[n_with.id, n_empty.id]
        )

    assert set(out.get(n_with.id, [])) == {t1.id, t2.id}
    assert n_empty.id not in out


# ---------------------------------------------------------------------------
# Schema integrity sampler (a sanity tail)
# ---------------------------------------------------------------------------


async def test_note_note_link_unique_constraint_dedupes() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "a")
        b = await _make_note(s, org, user, "b")
        l1 = await note_links.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=a.id,
            child_note_id=b.id,
            kind="references",
        )
        l2 = await note_links.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=a.id,
            child_note_id=b.id,
            kind="references",
        )
        assert l1.id == l2.id
        # Different kind on the same pair is a different row.
        l3 = await note_links.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=a.id,
            child_note_id=b.id,
            kind="replies_to",
        )
        assert l3.id != l1.id
        rows = (
            (
                await s.execute(
                    select(NoteNoteLink).where(
                        NoteNoteLink.parent_note_id == a.id,
                        NoteNoteLink.child_note_id == b.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2


async def test_note_task_link_unique_constraint_dedupes() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _make_note(s, org, user)
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="t",
            estimate_effort_h=Decimal(1),
        )
        a = await note_links.start_task_on_note(
            s, org_id=org, actor_id=user, task_id=task.id, note_id=note.id
        )
        b = await note_links.start_task_on_note(
            s, org_id=org, actor_id=user, task_id=task.id, note_id=note.id
        )
        assert a.id == b.id
        rows = (
            (
                await s.execute(
                    select(NoteTaskLink).where(
                        NoteTaskLink.note_id == note.id, NoteTaskLink.task_id == task.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
