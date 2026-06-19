"""ADR-0036 event bus (tasks cb6d6baf / c19b5489).

The coordinated write substrate: emit writes an outbox row in the
caller's transaction; idempotency dedupes a retry; an agent's write
events are capped (anti-runaway 429); the garden_classify mapping lands a
propose -> commit/reject chain; and an autonomous agent may not commit
over a live note (the is_inert gate).
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import func, select, update

from flow_core.db import admin_session, tenant_session
from flow_core.errors import ConflictError, QuotaExceededError
from flow_core.i18n import MessageCode
from flow_core.models.event_outbox import EventOutbox
from flow_core.models.executor import Executor, ExecutorKind
from flow_core.models.note import Note, NoteKind
from flow_core.models.tag import TagKind
from flow_core.services import event_bus, taxonomy
from flow_core.services import garden_classify as gc
from flow_core.services import notes as notes_svc
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EB")
    return a.org_id, a.user_id


# --- emit + idempotency ---------------------------------------------------


async def test_emit_writes_row() -> None:
    org, user = await _workspace()
    nid = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        ev = await event_bus.emit_event(
            s,
            org_id=org,
            actor_id=user,
            actor_kind="human",
            kind="snapshot",
            payload={"k": 1},
            node_kind="note",
            node_id=nid,
        )
    assert ev.kind == "snapshot"
    assert ev.actor_kind == "human"
    assert ev.node_id == nid
    assert ev.payload == {"k": 1}
    assert ev.payload_schema_version == 1


async def test_emit_idempotent_within_window() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await event_bus.emit_event(
            s,
            org_id=org,
            actor_id=user,
            actor_kind="human",
            kind="commit",
            payload={"x": 1},
            idempotency_key="k1",
        )
        b = await event_bus.emit_event(
            s,
            org_id=org,
            actor_id=user,
            actor_kind="human",
            kind="commit",
            payload={"x": 2},
            idempotency_key="k1",
        )
        assert a.id == b.id  # retry returns the existing row
        n = (
            await s.execute(
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.org_id == org, EventOutbox.idempotency_key == "k1")
            )
        ).scalar_one()
    assert n == 1  # not duplicated


# --- anti-runaway quota ---------------------------------------------------


async def test_quota_caps_agent_writes() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        # A per-executor cap of 2/min, bound to this actor.
        s.add(
            Executor(
                org_id=org,
                kind=ExecutorKind.llm_agent,
                name="bot",
                user_id=user,
                event_quota_per_min=2,
            )
        )
        await s.flush()
        for _ in range(2):
            await event_bus.emit_event(
                s, org_id=org, actor_id=user, actor_kind="agent", kind="commit", payload={}
            )
        with pytest.raises(QuotaExceededError) as err:
            await event_bus.emit_event(
                s, org_id=org, actor_id=user, actor_kind="agent", kind="commit", payload={}
            )
    assert err.value.code == MessageCode.EVENT_QUOTA_EXCEEDED


async def test_quota_does_not_cap_human_or_system() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        # Even with a tight executor cap, only agent writes are throttled.
        s.add(
            Executor(
                org_id=org,
                kind=ExecutorKind.human,
                name="me",
                user_id=user,
                event_quota_per_min=1,
            )
        )
        await s.flush()
        for kind in ("human", "system"):
            for _ in range(3):
                await event_bus.emit_event(
                    s, org_id=org, actor_id=user, actor_kind=kind, kind="commit", payload={}
                )  # no raise


# --- classify -> bus mapping ---------------------------------------------


async def test_record_decision_commit_chain() -> None:
    org, user = await _workspace()
    nid = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        propose, decision = await event_bus.record_classification_decision(
            s,
            actor_kind="human",
            org_id=org,
            actor_id=user,
            node_id=nid,
            suggestion_type="tag",
            suggestion_value={"tag_id": str(uuid.uuid4())},
            action="accept",
            model_version="garden-classify-v1",
            signals_snapshot={},
        )
    assert propose.kind == "propose"
    assert decision.kind == "commit"
    assert decision.parent_event_id == propose.id
    assert decision.applied_state == "committed"
    assert decision.payload["action"] == "accept"


async def test_record_decision_reject_marks_soft_ignore() -> None:
    org, user = await _workspace()
    nid = uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        _, ignored = await event_bus.record_classification_decision(
            s,
            actor_kind="human",
            org_id=org,
            actor_id=user,
            node_id=nid,
            suggestion_type="tag",
            suggestion_value={},
            action="ignore",
            model_version="v",
            signals_snapshot={},
        )
        _, rejected = await event_bus.record_classification_decision(
            s,
            actor_kind="human",
            org_id=org,
            actor_id=user,
            node_id=nid,
            suggestion_type="tag",
            suggestion_value={},
            action="reject",
            model_version="v",
            signals_snapshot={},
        )
    assert ignored.kind == "reject" and ignored.payload["soft"] is True
    assert rejected.kind == "reject" and rejected.payload["soft"] is False


# --- read stream ----------------------------------------------------------


async def test_recent_events_newest_first_and_since() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        for i in range(3):
            await event_bus.emit_event(
                s,
                org_id=org,
                actor_id=user,
                actor_kind="human",
                kind="snapshot",
                payload={"i": i},
            )
        evs = await event_bus.recent_events(s, org_id=org, limit=10)
        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        empty = await event_bus.recent_events(s, org_id=org, since=future)
    assert len(evs) >= 3
    ts = [e.ts for e in evs]
    assert ts == sorted(ts, reverse=True)  # newest first
    assert empty == []  # nothing after the cursor


# --- garden_classify integration: the is_inert gate ----------------------


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body {title}",
    )


async def test_agent_commit_on_live_note_is_refused() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "live")
        tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="t"
        )
        note_id, tag_id = note.id, tag.id
    # Re-enter as an AGENT: committing over a live note must be refused.
    async with tenant_session(str(org), str(user), actor_kind="agent_run") as s:
        with pytest.raises(ConflictError) as err:
            await gc.apply_suggestion(
                s,
                org_id=org,
                actor_id=user,
                node_id=note_id,
                suggestion_type="tag",
                suggestion_value={"tag_id": str(tag_id)},
                action="accept",
            )
    assert err.value.code == MessageCode.EVENT_NODE_NOT_INERT


async def test_agent_commit_on_inert_note_allowed_and_on_bus() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "old")
        tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="t"
        )
        note_id, tag_id = note.id, tag.id
        # Make it inert: archived + last edited well before the quiet window.
        await s.execute(
            update(Note)
            .where(Note.id == note_id)
            .values(
                is_archived=True,
                updated_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30),
            )
        )
    async with tenant_session(str(org), str(user), actor_kind="agent_run") as s:
        fb = await gc.apply_suggestion(
            s,
            org_id=org,
            actor_id=user,
            node_id=note_id,
            suggestion_type="tag",
            suggestion_value={"tag_id": str(tag_id)},
            action="accept",
        )
        assert fb.action == "accept"
        evs = await event_bus.recent_events(s, org_id=org, limit=50)
    commits = [
        e for e in evs if e.kind == "commit" and e.node_id == note_id and e.actor_kind == "agent"
    ]
    assert len(commits) == 1
    assert commits[0].parent_event_id is not None  # chained to its propose


async def test_human_apply_lands_on_the_bus() -> None:
    org, user = await _workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "n")
        tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="topic"
        )
        await gc.apply_suggestion(
            s,
            org_id=org,
            actor_id=user,
            node_id=note.id,
            suggestion_type="tag",
            suggestion_value={"tag_id": str(tag.id)},
            action="accept",
        )
        evs = await event_bus.recent_events(s, org_id=org, limit=50)
    kinds = {e.kind for e in evs if e.node_id == note.id}
    assert {"propose", "commit"} <= kinds
    assert all(e.actor_kind == "human" for e in evs if e.node_id == note.id)
