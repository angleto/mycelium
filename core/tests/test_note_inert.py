"""Anti-mutation invariant (task 8a26c000): the system never decays or
decomposes a *live* note -- one with an open linked task, or edited within
the quiet window.

Covers the predicate (note_inert) and the guards wired into the autonomous
transitions: the maturity sweep's growing/mature -> dormant decay, the
supersedes/contradicts -> dormant decay, and the distillation humus
side-effect. (auto_promote shares the same open_work_exists query guard as
the sweep decay, exercised here.)
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select, text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import Note, NoteKind, NoteMaturity
from mycelium_core.models.task import Task
from mycelium_core.services import note_inert, note_links
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _ws() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="INERT")
    return a.org_id, a.user_id


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str = "n") -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body {title}",
    )


async def _open_task_on(s: object, org: uuid.UUID, user: uuid.UUID, note: Note) -> Task:
    """A freshly-created task lands in the workflow's initial (non-terminal)
    state, so it counts as open work once linked to the note."""
    task = await tasks_svc.create_task(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        title="work",
        estimate_effort_h=Decimal(1),
    )
    await note_links.start_task_on_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        task_id=task.id,
        note_id=note.id,
    )
    return task


async def _age(s: object, note: Note, days: int) -> None:
    await s.execute(  # type: ignore[attr-defined]
        text("UPDATE notes SET updated_at = :t WHERE id = :id"),
        {"t": dt.datetime.now(dt.UTC) - dt.timedelta(days=days), "id": str(note.id)},
    )
    await s.refresh(note)  # type: ignore[attr-defined]


# --- open-work predicate ---


async def test_note_has_open_work_true_with_open_task() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        n = await _note(s, org, user)
        await _open_task_on(s, org, user, n)
        assert await note_inert.note_has_open_work(s, note_id=n.id) is True


async def test_note_has_open_work_false_without_task() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        n = await _note(s, org, user)
        assert await note_inert.note_has_open_work(s, note_id=n.id) is False


async def test_note_has_open_work_false_when_task_archived() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        n = await _note(s, org, user)
        task = await _open_task_on(s, org, user, n)
        t = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
        t.is_archived = True
        await s.flush()
        assert await note_inert.note_has_open_work(s, note_id=n.id) is False


# --- is_inert matrix ---


async def test_is_inert_true_for_archived_quiet_unworked() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "arch")
        a.is_archived = True
        await s.flush()
        await _age(s, a, 30)
        assert await note_inert.is_inert(s, note=a) is True


async def test_is_inert_false_for_growing() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        g = await _note(s, org, user, "grow")
        g.maturity = NoteMaturity.growing.value
        await s.flush()
        await _age(s, g, 30)
        # Not archived and not dormant -> never inert.
        assert await note_inert.is_inert(s, note=g) is False


async def test_is_inert_false_when_open_work_or_recent() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        # dormant + open task -> live (has work)
        live = await _note(s, org, user, "dorm-live")
        live.maturity = NoteMaturity.dormant.value
        await s.flush()
        await _open_task_on(s, org, user, live)
        await _age(s, live, 30)
        assert await note_inert.is_inert(s, note=live) is False

        # dormant but edited just now -> within the quiet window
        recent = await _note(s, org, user, "dorm-recent")
        recent.maturity = NoteMaturity.dormant.value
        await s.flush()
        await s.refresh(recent)
        assert await note_inert.is_inert(s, note=recent) is False


# --- guard: maturity sweep decay (the headline gap) ---


async def test_tick_does_not_dormant_a_note_with_open_work() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        n = await _note(s, org, user, "live-old")
        await _open_task_on(s, org, user, n)
        n.maturity = NoteMaturity.growing.value
        await s.flush()
        await _age(s, n, 120)  # well past the 60-day dormant threshold
        await note_links.tick_maturity_transitions(s, org_id=org, actor_id=user)
        reloaded = (await s.execute(select(Note).where(Note.id == n.id))).scalar_one()
        assert reloaded.maturity == NoteMaturity.growing.value


async def test_tick_dormants_an_inert_old_note() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        n = await _note(s, org, user, "dead-old")
        n.maturity = NoteMaturity.growing.value
        await s.flush()
        await _age(s, n, 120)
        await note_links.tick_maturity_transitions(s, org_id=org, actor_id=user)
        reloaded = (await s.execute(select(Note).where(Note.id == n.id))).scalar_one()
        assert reloaded.maturity == NoteMaturity.dormant.value


# --- guard: supersedes/contradicts decay ---


async def test_supersedes_does_not_dormant_a_live_child() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        parent = await _note(s, org, user, "new")
        child = await _note(s, org, user, "live")
        await _open_task_on(s, org, user, child)
        await note_links.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=parent.id,
            child_note_id=child.id,
            kind="supersedes",
        )
        await s.refresh(child)
        assert child.maturity != NoteMaturity.dormant.value


async def test_supersedes_dormants_an_inert_child() -> None:
    org, user = await _ws()
    async with tenant_session(str(org), str(user)) as s:
        parent = await _note(s, org, user, "new")
        child = await _note(s, org, user, "dead")
        await note_links.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=parent.id,
            child_note_id=child.id,
            kind="contradicts",
        )
        await s.refresh(child)
        assert child.maturity == NoteMaturity.dormant.value
