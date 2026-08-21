"""The text of a non-effective note must not reach an agent (task
854f1c28).

ADR-0043 D2 withholds an un-approved proposal from every surface, and a
soft-deleted note is in the bin. Two predicates already enforce that: the
blob-side one for retrieval (task c5da112c) and the note-side one for the
note/graph and listing surfaces (task f8402e7f). The agent prompt was the
third way in, covered by neither: ``agent_runtime._build_context`` quotes
the title + body of every note linked to the task and of every pending
handoff's artifact, and both id sources filtered `deleted_at` at best.

The perimeter now lives in ``note_links.notes_for_task`` (so a caller is
right by default) and in the queries that resolve those ids to rows.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.task_handoff import HandoffStatus, TaskHandoff
from mycelium_core.services import agent_runtime as runtime
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="AGCTX")
    return r.org_id, r.user_id


async def _propose(s: object, note_id: uuid.UUID) -> None:
    note = (await s.execute(select(Note).where(Note.id == note_id))).scalar_one()  # type: ignore[attr-defined]
    note.review_state = "proposed"
    await s.flush()  # type: ignore[attr-defined]


async def _approve(s: object, note_id: uuid.UUID) -> None:
    note = (await s.execute(select(Note).where(Note.id == note_id))).scalar_one()  # type: ignore[attr-defined]
    note.review_state = "approved"
    await s.flush()  # type: ignore[attr-defined]


async def _version(s: object, note_id: uuid.UUID) -> int:
    return int(
        (await s.execute(select(Note.version).where(Note.id == note_id))).scalar_one()  # type: ignore[attr-defined]
    )


async def test_linked_note_text_is_gated_in_the_agent_prompt() -> None:
    """A linked note that is un-approved or in the bin contributes neither
    title nor body to the prompt, and comes back on approval/restore with
    nothing re-indexed."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="T")
        await notes_svc.create_note_for_task(
            s, org_id=org, actor_id=user, task_id=task.id, title="LiveNote", text="live body ALPHA"
        )
        gated = await notes_svc.create_note_for_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=task.id,
            title="ProposedNote",
            text="unreviewed body BRAVO",
        )
        trashed = await notes_svc.create_note_for_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=task.id,
            title="TrashedNote",
            text="binned body CHARLIE",
        )
        blob = (await runtime._build_context(s, org_id=org, task=task))[0][1]
        assert "ALPHA" in blob and "BRAVO" in blob and "CHARLIE" in blob

        await _propose(s, gated.id)
        await notes_svc.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=trashed.id,
            expected_version=await _version(s, trashed.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.get_task(s, org_id=org, task_id=task.id)
        blob = (await runtime._build_context(s, org_id=org, task=task))[0][1]
        assert "ALPHA" in blob  # the live note is untouched
        assert "BRAVO" not in blob and "ProposedNote" not in blob
        assert "CHARLIE" not in blob and "TrashedNote" not in blob

        # The read_task_notes tool counts what the agent may see, not the
        # junction rows.
        obs = await runtime._run_tool(
            s,
            org_id=org,
            actor_id=user,
            run=None,  # type: ignore[arg-type]
            task=task,
            action=runtime._Action(tool="read_task_notes", args={}),
        )
        assert obs == "notes:1"

        await _approve(s, gated.id)
        await notes_svc.restore_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=trashed.id,
            expected_version=await _version(s, trashed.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.get_task(s, org_id=org, task_id=task.id)
        blob = (await runtime._build_context(s, org_id=org, task=task))[0][1]
        assert "BRAVO" in blob and "CHARLIE" in blob


async def test_handoff_artifact_text_is_gated_but_the_message_survives() -> None:
    """The artifact quotation drops when the artifact is not effective; the
    handoff message itself still reaches the recipient (it is the
    coordination record, not the note)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        pred = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="Upstream")
        succ = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="Downstream")
        art = await notes_svc.create_note_for_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=pred.id,
            title="Artifact",
            text="the conclusion is DELTA",
        )
        s.add(
            TaskHandoff(
                org_id=org,
                predecessor_task_id=pred.id,
                successor_task_id=succ.id,
                message="handing over ECHO",
                artifact_note_id=art.id,
                status=HandoffStatus.pending,
            )
        )
        await s.flush()
        blob = (await runtime._build_context(s, org_id=org, task=succ))[0][1]
        assert "ECHO" in blob and "DELTA" in blob

        await _propose(s, art.id)

    async with tenant_session(str(org), str(user)) as s:
        succ = await tasks_svc.get_task(s, org_id=org, task_id=succ.id)
        blob = (await runtime._build_context(s, org_id=org, task=succ))[0][1]
        assert "ECHO" in blob  # the message is the handoff, and it stays
        assert "DELTA" not in blob and "Artifact" not in blob


async def test_notes_for_task_is_effective_by_default_with_a_trash_opt_in() -> None:
    """The id source carries the perimeter: the default answer is the
    effective set, ``include_deleted`` re-admits the bin only, and an
    un-approved proposal is never returned (same asymmetry as the shared
    clause)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="T")
        live = await notes_svc.create_note_for_task(
            s, org_id=org, actor_id=user, task_id=task.id, title="live", text="x"
        )
        gated = await notes_svc.create_note_for_task(
            s, org_id=org, actor_id=user, task_id=task.id, title="gated", text="x"
        )
        trashed = await notes_svc.create_note_for_task(
            s, org_id=org, actor_id=user, task_id=task.id, title="trashed", text="x"
        )
        await _propose(s, gated.id)
        await notes_svc.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=trashed.id,
            expected_version=await _version(s, trashed.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        assert set(await note_links_svc.notes_for_task(s, org_id=org, task_id=task.id)) == {live.id}
        assert set(
            await note_links_svc.notes_for_task(
                s, org_id=org, task_id=task.id, include_deleted=True
            )
        ) == {live.id, trashed.id}
        # The link rows are all still there: the perimeter is derived, not
        # written.
        assert len(await note_links_svc.list_note_task_links(s, org_id=org, task_id=task.id)) == 3


async def test_work_note_reuse_skips_a_gated_artifact() -> None:
    """``get_or_create_work_note`` reuses an existing artifact note only
    while it is effective: an un-approved one is not a note the caller may
    be handed back."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="T")
        first = await notes_svc.get_or_create_work_note(
            s, org_id=org, actor_id=user, task_id=task.id
        )
        again = await notes_svc.get_or_create_work_note(
            s, org_id=org, actor_id=user, task_id=task.id
        )
        assert again.id == first.id  # idempotent while effective

        await _propose(s, first.id)
        fresh = await notes_svc.get_or_create_work_note(
            s, org_id=org, actor_id=user, task_id=task.id
        )
        assert fresh.id != first.id
        assert fresh.kind is NoteKind.text
