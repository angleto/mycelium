"""Note edit-conflict detection + adjudication open (docs/adr/0029 P4).

Covers:

- No conflict when the note is not mature.
- No conflict when the same actor edits twice.
- No conflict between two human_direct edits (multi-tab is routine).
- Conflict (adjudication opened) when an agent_run edit follows a
  human_direct edit within the window on a mature note.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, text

from mycelium_core.db import admin_session, tenant_session, with_actor
from mycelium_core.models.adjudication import Adjudication
from mycelium_core.models.note import NoteKind
from mycelium_core.services import note_conflict
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _make_workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="NC")
    # Ensure the identity sync trigger has a non-empty handle to copy
    # (some downstream calls expect the actor_identity_id to resolve).
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


async def test_no_conflict_when_note_not_mature() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="seed-note",
            text="body",
        )
        adj_id = await note_conflict.detect_and_open_conflict(
            s,
            org_id=org,
            actor_id=user,
            note_id=n.id,
            proposed_body="changed",
            current_actor_kind="agent_run",
            current_actor_subject_id=uuid.uuid4(),
        )
    assert adj_id is None


async def test_no_conflict_for_human_to_human_multitab() -> None:
    """Two human_direct edits within the window are NOT a conflict:
    the SPA's autosave or another tab is normal usage, not adversarial
    agent collision."""
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="mature-note",
            text="body",
        )
        from mycelium_core.services import note_links

        await note_links.set_maturity(s, org_id=org, actor_id=user, note_id=n.id, maturity="mature")
        # Simulate a prior human_direct edit via update_note's audit:
        # set_maturity already audited above with actor_kind=human_direct
        # (the default for tenant_session). The current call is also
        # human_direct => no conflict.
        adj_id = await note_conflict.detect_and_open_conflict(
            s,
            org_id=org,
            actor_id=user,
            note_id=n.id,
            proposed_body="second edit",
            current_actor_kind="human_direct",
            current_actor_subject_id=None,
        )
    assert adj_id is None


async def test_conflict_opens_adjudication_on_agent_after_human_edit() -> None:
    """A mature note edited recently by a human, then an agent_run
    proposes a new direction within the window: conflict opens
    an adjudication with strategy='human_in_loop'."""
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="mature-note",
            text="body",
        )
        from mycelium_core.services import note_links

        await note_links.set_maturity(s, org_id=org, actor_id=user, note_id=n.id, maturity="mature")
        # The set_maturity above is the "previous human edit" the
        # detector sees in activity_log. Switch context to agent_run
        # and call detect from there.
        agent_run_id = uuid.uuid4()
        async with with_actor(s, actor_kind="agent_run", actor_subject_id=str(agent_run_id)):
            adj_id = await note_conflict.detect_and_open_conflict(
                s,
                org_id=org,
                actor_id=user,
                note_id=n.id,
                proposed_body="agent wants to overwrite",
                current_actor_kind="agent_run",
                current_actor_subject_id=agent_run_id,
            )
    assert adj_id is not None
    async with tenant_session(str(org), str(user)) as s2:
        adj = (await s2.execute(select(Adjudication).where(Adjudication.id == adj_id))).scalar_one()
    assert adj.strategy_id == "human_in_loop"
    assert adj.context_json["note_id"] == str(n.id)
    assert adj.context_json["current_actor_kind"] == "agent_run"
    assert adj.context_json["previous_actor_kind"] == "human_direct"


async def test_no_conflict_when_window_expired() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        n = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="mature-note",
            text="body",
        )
        from mycelium_core.services import note_links

        await note_links.set_maturity(s, org_id=org, actor_id=user, note_id=n.id, maturity="mature")
        # Drive the detector with a future ``now`` so the prior edit
        # falls outside the window; we cannot ``UPDATE activity_log``
        # (append-only enforced by trigger) so the test injects time
        # instead of moving the row backwards.
        agent_run_id = uuid.uuid4()
        future = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)
        async with with_actor(s, actor_kind="agent_run", actor_subject_id=str(agent_run_id)):
            adj_id = await note_conflict.detect_and_open_conflict(
                s,
                org_id=org,
                actor_id=user,
                note_id=n.id,
                proposed_body="far-later edit",
                current_actor_kind="agent_run",
                current_actor_subject_id=agent_run_id,
                conflict_window_seconds=60,
                now=future,
            )
    assert adj_id is None
