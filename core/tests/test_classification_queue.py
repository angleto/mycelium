"""On-create classification queue (ADR-0042 D5, task b8c60940 / WS-D2).

create_note / create_task enqueue a ``classification_jobs`` row in the
create's own transaction (gated, off by default); the garden worker drains
pending rows -- classify_node + cache in precomputed_suggestions -- with
per-job SAVEPOINT isolation. The queue NEVER gates the node (the note/task is
live immediately); it only precomputes read-only suggestions.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.models.classification_job import ClassificationJob
from flow_core.models.note import Note, NoteKind
from flow_core.models.note_tag import NoteTag
from flow_core.models.tag import TagKind
from flow_core.services import garden_classify as gc
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services import taxonomy
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="CJQ")
    return r.org_id, r.user_id


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body of {title}",
    )


async def _jobs_for(s: object, node_id: uuid.UUID) -> list[ClassificationJob]:
    # tenant_session runs with autoflush off; the create_* enqueue is added but
    # not flushed until the caller commits, so flush before reading it back.
    await s.flush()  # type: ignore[attr-defined]
    return list(
        (
            await s.execute(  # type: ignore[attr-defined]
                select(ClassificationJob).where(ClassificationJob.node_id == node_id)
            )
        ).scalars()
    )


async def test_create_note_enqueues_pending_job_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the on-creation flag on, creating a note enqueues exactly one
    pending classification job in the same transaction."""
    monkeypatch.setattr(get_settings(), "garden_autoclassify_on_creation_enabled", True)
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "n")
        jobs = await _jobs_for(s, note.id)
    assert len(jobs) == 1
    assert jobs[0].status == "pending"
    assert jobs[0].node_kind == "note"


async def test_create_note_no_job_when_flag_off() -> None:
    """Default (flag off): no job is enqueued -- behaviour is unchanged."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "n")
        jobs = await _jobs_for(s, note.id)
    assert jobs == []


async def test_drain_classifies_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drain processes pending jobs: it classifies each node and caches the
    suggestions (D4). A note whose tag co-occurs on two related notes gets that
    tag cached; the job is marked done."""
    monkeypatch.setattr(get_settings(), "garden_autoclassify_on_creation_enabled", True)
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _note(s, org, user, "a")  # {x}
        b = await _note(s, org, user, "b")  # {x, y}
        c = await _note(s, org, user, "c")  # {x, y}
        x = (
            await taxonomy.create_tag(s, org_id=org, actor_id=user, kind=TagKind.generic, name="x")
        ).id
        y = (
            await taxonomy.create_tag(s, org_id=org, actor_id=user, kind=TagKind.generic, name="y")
        ).id
        for nid in (a.id, b.id, c.id):
            s.add(NoteTag(org_id=org, note_id=nid, tag_id=x))
        s.add(NoteTag(org_id=org, note_id=b.id, tag_id=y))
        s.add(NoteTag(org_id=org, note_id=c.id, tag_id=y))
        for i in range(gc.COLD_START_NODES):  # clear cold-start damping
            s.add(Note(org_id=org, kind=NoteKind.text, title=f"filler-{i}"))
        await s.flush()

        res = await gc.process_classification_jobs(s, org_id=org)
        assert res["errors"] == 0
        assert res["processed"] >= 3  # a, b, c were each enqueued at create

        job_a = (
            await s.execute(select(ClassificationJob).where(ClassificationJob.node_id == a.id))
        ).scalar_one()
        assert job_a.status == "done"
        assert job_a.processed_at is not None

        cached = await gc.read_classification(s, org_id=org, node_id=a.id)
        assert cached is not None
        assert any(
            r.suggestion_type == "tag" and r.suggestion_value.get("tag_id") == str(y)
            for r in cached
        )


async def test_drain_isolates_a_poison_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A job for a non-existent node fails in its own SAVEPOINT and is marked
    'error'; the good job in the same batch still completes."""
    monkeypatch.setattr(get_settings(), "garden_autoclassify_on_creation_enabled", True)
    org, user = await _org()
    poison = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        good = await _note(s, org, user, "good")  # enqueues a job at create
        s.add(ClassificationJob(org_id=org, node_kind="note", node_id=poison))
        await s.flush()
        res = await gc.process_classification_jobs(s, org_id=org)
        assert res["processed"] >= 1
        assert res["errors"] >= 1
        rows = list(
            (
                await s.execute(select(ClassificationJob).where(ClassificationJob.org_id == org))
            ).scalars()
        )
    by_node = {j.node_id: j for j in rows}
    assert by_node[good.id].status == "done"
    assert by_node[poison].status == "error"
    assert by_node[poison].error  # the message was recorded


async def test_create_task_enqueues_only_when_unified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task is enqueued on create only when BOTH the on-creation flag and the
    unified-task-graph flag are on (classify_node accepts a task only then)."""
    monkeypatch.setattr(get_settings(), "garden_autoclassify_on_creation_enabled", True)
    org, user = await _org()
    # unified OFF (default) -> no task job.
    async with tenant_session(str(org), str(user)) as s:
        t1 = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="t1")
        assert await _jobs_for(s, t1.id) == []
    # unified ON -> a task job is enqueued.
    monkeypatch.setattr(get_settings(), "garden_unified_task_graph_enabled", True)
    async with tenant_session(str(org), str(user)) as s:
        t2 = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="t2")
        jobs = await _jobs_for(s, t2.id)
    assert len(jobs) == 1
    assert jobs[0].node_kind == "task"
