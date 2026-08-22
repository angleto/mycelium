"""A gated note's BODY is not readable by another door (task a186c989).

ADR-0043 D2 withholds an un-approved proposal from every surface and a
soft-deleted note is in the bin, but the text of a note does not live on
the note: it lives in ``note_part``. That table knew nothing about
``notes``, so anyone holding a note id (the review inbox hands them out)
could read the body part by part, extract it into KG facts, or promote it
into a task description -- three doors around a perimeter the retrieval
side had already closed.

The perimeter now sits on the two chokepoints of ``note_parts``
(``_get_part`` and ``_get_note_in_org``) plus ``list_parts`` /
``list_trashed``, on ``note_links._get_note`` and on ``kg.extract_facts``.
Two opt-ins stay, and one of them is load-bearing: the delete revision is
snapshotted AFTER ``deleted_at`` is set, so it must still see the parts or
restoring it would empty the note.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from decimal import Decimal
from typing import ClassVar

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.ai_providers import LLMResult
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import ConflictError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import Note, NoteKind, NoteTurn, TurnRole
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.tag import TagKind
from mycelium_core.services import (
    annotations,
    billing,
    entity_revisions,
    garden_review,
    kg,
    memory,
    note_links,
    taxonomy,
)
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import note_parts as parts_svc
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="GATED")
    return r.org_id, r.user_id


async def _note(s: object, org: uuid.UUID, user: uuid.UUID, text: str) -> Note:
    return await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title="gated-subject",
        text=text,
    )


async def _propose(s: object, note_id: uuid.UUID) -> None:
    note = (await s.execute(select(Note).where(Note.id == note_id))).scalar_one()  # type: ignore[attr-defined]
    note.review_state = "proposed"
    await s.flush()  # type: ignore[attr-defined]


async def _version(s: object, note_id: uuid.UUID) -> int:
    return int(
        (await s.execute(select(Note.version).where(Note.id == note_id))).scalar_one()  # type: ignore[attr-defined]
    )


class _FakeExtractLLM:
    """Records what it was asked to summarise, so a leak is observable."""

    model_id = "fake-llm"
    seen: ClassVar[list[str]] = []

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        type(self).seen.append(messages[-1][1])
        payload = json.dumps({"entities": [{"name": "Bob", "type": "person"}], "relations": []})
        return LLMResult(text=payload, tokens_in=5, tokens_out=5, model_id=self.model_id)


async def _seed_billing(s: object, org: uuid.UUID, user: uuid.UUID) -> None:
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))  # type: ignore[arg-type]
    await billing.upsert_rate_card(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        model_id="fake-llm",
        provider="local",
        values={"credits_per_input": Decimal("0.001"), "credits_per_output": Decimal("0.001")},
    )


async def test_parts_of_a_gated_note_are_not_readable() -> None:
    """The part-addressed and note-addressed reads both refuse, for both
    legs of the predicate, and come back on restore/approval."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        trashed = await _note(s, org, user, "binned body FOXTROT")
        gated = await _note(s, org, user, "unreviewed body GOLF")
        # A second part on each note, thrown into the parts bin: the trash
        # side table keeps the whole body, so it is a read surface too and
        # the assertion below must not be an empty-vs-empty comparison.
        for n in (trashed, gated):
            extra = await parts_svc.create_part(
                s, org_id=org, actor_id=user, note_id=n.id, body=f"second body {n.id.hex[:4]}"
            )
            await parts_svc.trash_part(
                s,
                org_id=org,
                actor_id=user,
                part_id=extra.id,
                expected_version=extra.version,
            )
        trashed_part = (await parts_svc.list_parts(s, org_id=org, note_id=trashed.id))[0]
        gated_part = (await parts_svc.list_parts(s, org_id=org, note_id=gated.id))[0]
        assert trashed_part.body and gated_part.body
        assert len(await parts_svc.list_trashed(s, org_id=org, note_id=trashed.id)) == 1
        assert len(await parts_svc.list_trashed(s, org_id=org, note_id=gated.id)) == 1
        trashed_bin_part = (await parts_svc.list_trashed(s, org_id=org, note_id=trashed.id))[0]

        await _propose(s, gated.id)
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=trashed.id,
            expected_version=await _version(s, trashed.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        for nid in (trashed.id, gated.id):
            assert await parts_svc.list_parts(s, org_id=org, note_id=nid) == []
            assert await parts_svc.list_trashed(s, org_id=org, note_id=nid) == []
        for pid in (trashed_part.id, gated_part.id):
            with pytest.raises(NotFoundError):
                await parts_svc.get_part(s, org_id=org, part_id=pid)
            # ... and the write side of the same chokepoint.
            with pytest.raises(NotFoundError):
                await parts_svc.update_part(
                    s,
                    org_id=org,
                    actor_id=user,
                    part_id=pid,
                    body="overwritten",
                    expected_version=1,
                )
        # Creating a part on a gated note is refused BEFORE anything is
        # written: it used to fail later, from inside the revision logger,
        # with the row already inserted and the ords already shifted.
        for nid in (trashed.id, gated.id):
            with pytest.raises(NotFoundError):
                await parts_svc.create_part(
                    s, org_id=org, actor_id=user, note_id=nid, body="new part"
                )
        # Purging is gated like reading: the most destructive door must not
        # be the one left open.
        with pytest.raises(NotFoundError):
            await parts_svc.delete_part(s, org_id=org, actor_id=user, part_id=trashed_part.id)
        with pytest.raises(NotFoundError):
            await parts_svc.delete_part(s, org_id=org, actor_id=user, part_id=trashed_bin_part.id)

    async with tenant_session(str(org), str(user)) as s:
        await nt.restore_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=trashed.id,
            expected_version=await _version(s, trashed.id),
        )
        note = (await s.execute(select(Note).where(Note.id == gated.id))).scalar_one()
        note.review_state = "approved"
        await s.flush()

    async with tenant_session(str(org), str(user)) as s:
        for nid in (trashed.id, gated.id):
            # Nothing was written, purged or shifted while the gate held:
            # the note comes back exactly as it was.
            parts = await parts_svc.list_parts(s, org_id=org, note_id=nid)
            assert len(parts) == 1
            assert "overwritten" not in (parts[0].body or "")
            assert "new part" not in (parts[0].body or "")
            assert len(await parts_svc.list_trashed(s, org_id=org, note_id=nid)) == 1
        # The batch twin agrees with the single-note reader, before and after.
        batch = await parts_svc.parts_by_note(s, org_id=org, note_ids=[trashed.id, gated.id])
        assert {k: [p.id for p in v] for k, v in batch.items()} == {
            nid: [p.id for p in await parts_svc.list_parts(s, org_id=org, note_id=nid)]
            for nid in (trashed.id, gated.id)
        }


async def test_the_delete_revision_still_carries_the_body() -> None:
    """The load-bearing opt-in: ``snapshot_note`` photographs the note
    AFTER the soft-delete, so gating its parts without the opt-in would
    write an empty snapshot -- and restoring that revision would empty the
    note instead of bringing it back."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "body HOTEL")
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=await _version(s, note.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        rows = await entity_revisions.list_revisions(
            s,
            entity_kind=entity_revisions.ENTITY_KIND_NOTE,
            entity_id=note.id,
            limit=10,
        )
        latest = rows[0]
        assert latest.snapshot["parts"], "the delete revision must keep the parts"
        assert latest.snapshot["parts"][0]["body"] == "body HOTEL"


async def test_a_snapshot_photographs_a_gated_note_whole() -> None:
    """The other half of the same trap, on the review axis: a snapshot
    records whatever state the note is in. If the reader's gate applied
    here, an un-approved proposal would snapshot with no parts and the
    first restore from that revision would empty it."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "body PAPA")
        await _propose(s, note.id)
        payload = await entity_revisions.snapshot_note(s, note)
        assert payload["parts"], "a photographer does not apply the reader's gate"
        assert payload["parts"][0]["body"] == "body PAPA"
        assert payload["transcript"] == "body PAPA"


async def test_kg_extract_refuses_a_gated_note() -> None:
    """The body must not reach the metered prompt -- and must not come back
    out as effective KG facts."""
    org, user = await _org()
    _FakeExtractLLM.seen = []
    async with tenant_session(str(org), str(user)) as s:
        await _seed_billing(s, org, user)
        trashed = await _note(s, org, user, "binned INDIA")
        gated = await _note(s, org, user, "unreviewed JULIET")
        await _propose(s, gated.id)
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=trashed.id,
            expected_version=await _version(s, trashed.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        for nid in (trashed.id, gated.id):
            with pytest.raises(NotFoundError):
                await kg.extract_facts(
                    s, org_id=org, actor_id=user, note_id=nid, llm=_FakeExtractLLM()
                )
    assert _FakeExtractLLM.seen == []  # the LLM never saw either body


async def test_promote_note_to_task_refuses_an_unreviewed_proposal() -> None:
    """It copies the body into ``Task.description``, which an agent then
    reads: the one mutation that still worked on a proposal."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "unreviewed KILO")
        await _propose(s, note.id)

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError) as err:
            await note_links.promote_note_to_task(s, org_id=org, actor_id=user, note_id=note.id)
        # A note, not a memory blob.
        assert err.value.code is MessageCode.NOTE_NOT_FOUND
        with pytest.raises(NotFoundError):
            await note_links.set_maturity(
                s, org_id=org, actor_id=user, note_id=note.id, maturity="growing"
            )


async def test_an_unreviewed_proposal_does_not_age() -> None:
    """The maturity clock starts when a human accepts the note: otherwise
    a proposal ripens into ``growing`` in the review inbox and becomes a
    candidate for the auto-promotion sweep."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "unreviewed LIMA")
        await _propose(s, note.id)
        counters = await note_links.tick_maturity_transitions(s, org_id=org, actor_id=user)
        assert counters["seed_to_growing"] == 0
        fresh = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
        assert fresh.maturity == "seed"


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def test_conversation_turns_of_a_gated_note_are_not_readable() -> None:
    """``note_turns`` is the other child table that holds a note's text:
    the transcript of a conversation note. Gating the parts and leaving
    the turns open would have moved the door one table over."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        convo = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.conversation, title="chat"
        )
        s.add(
            NoteTurn(
                org_id=org, note_id=convo.id, role=TurnRole.user, content="secret NOVEMBER", ord=0
            )
        )
        await s.flush()
        assert [t.content for t in await nt.list_turns(s, org_id=org, note_id=convo.id)] == [
            "secret NOVEMBER"
        ]
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=convo.id,
            expected_version=await _version(s, convo.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        assert await nt.list_turns(s, org_id=org, note_id=convo.id) == []
        await nt.restore_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=convo.id,
            expected_version=await _version(s, convo.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        assert len(await nt.list_turns(s, org_id=org, note_id=convo.id)) == 1


async def test_a_blob_id_is_not_a_way_around_the_perimeter(_embedder: None) -> None:
    """``retrieve`` hides the indexed text of a gated note, but a blob id
    captured from an earlier search used to read it straight back. The
    by-id read now applies the same effective-source perimeter -- while
    DELETING such a blob stays possible."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "indexed body OSCAR")

    async with tenant_session(str(org), str(user)) as s:
        blob_ids = list(
            (
                await s.execute(
                    select(NotePartIndexPointer.blob_id).where(
                        NotePartIndexPointer.note_id == note.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert blob_ids, "the part must be indexed for this test to mean anything"
        blob = await memory.get_blob(s, org_id=org, blob_id=blob_ids[0])
        assert "OSCAR" in (blob.text or "")
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=await _version(s, note.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError):
            await memory.get_blob(s, org_id=org, blob_id=blob_ids[0])
        # Hidden from reading is not the same as undeletable.
        await memory.delete_blob(s, org_id=org, actor_id=user, blob_id=blob_ids[0])


async def test_comment_threads_do_not_outlive_the_note_they_quote() -> None:
    """An annotation QUOTES the note: ``anchor_quote`` and a suggestion's
    ``original_text`` are verbatim extracts. So the comment surfaces have
    to answer to the note's perimeter, or the body walks out two hops
    away -- with the part id no listing hands out any more."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "quoted body QUEBEC")
        part = (await parts_svc.list_parts(s, org_id=org, note_id=note.id))[0]
        comment = await annotations.create_comment(
            s,
            org_id=org,
            actor_id=user,
            doc_kind="note_part",
            doc_id=part.id,
            body="my rationale",
            anchor_quote="quoted body QUEBEC",
        )
        assert (
            len(await annotations.list_for_doc(s, org_id=org, doc_kind="note_part", doc_id=part.id))
            == 1
        )
        identity = (await identities_svc.ensure_for_user(s, org_id=org, user_id=user)).id
        await annotations.assign(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=comment.id,
            expected_version=comment.version,
            assignee_identity_id=identity,
        )
        assert comment.id in {
            a.id
            for a in await annotations.list_assigned(s, org_id=org, assignee_identity_id=identity)
        }
        await _propose(s, note.id)

    async with tenant_session(str(org), str(user)) as s:
        assert (
            await annotations.list_for_doc(s, org_id=org, doc_kind="note_part", doc_id=part.id)
            == []
        )
        assert await annotations.count_for_doc(
            s, org_id=org, doc_kind="note_part", doc_id=part.id
        ) == (0, 0)
        with pytest.raises(NotFoundError):
            await annotations.get_annotation(s, org_id=org, annotation_id=comment.id)
        # The "assigned to me" inbox has no document handle to start from, so
        # it carries the perimeter of its own: it was the surface handing the
        # part id of a gated note back to whoever held the assignment.
        assert await annotations.list_assigned(s, org_id=org, assignee_identity_id=identity) == []
        # The write door is not wider than the read door.
        with pytest.raises(NotFoundError):
            await annotations.create_comment(
                s,
                org_id=org,
                actor_id=user,
                doc_kind="note_part",
                doc_id=part.id,
                body="another",
            )

    async with tenant_session(str(org), str(user)) as s:
        row = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
        row.review_state = "approved"
        await s.flush()

    async with tenant_session(str(org), str(user)) as s:
        assert (
            len(await annotations.list_for_doc(s, org_id=org, doc_kind="note_part", doc_id=part.id))
            == 1
        )


async def test_task_comments_and_archived_notes_keep_their_threads() -> None:
    """The mechanical trap of the same change: an INNER join would wipe
    out every task-description comment (the work diary of every task),
    and the archive is not part of the predicate."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="T")
        await annotations.create_comment(
            s,
            org_id=org,
            actor_id=user,
            doc_kind="task_description",
            doc_id=task.id,
            body="a task comment",
        )
        note = await _note(s, org, user, "archived body ROMEO")
        part = (await parts_svc.list_parts(s, org_id=org, note_id=note.id))[0]
        await annotations.create_comment(
            s,
            org_id=org,
            actor_id=user,
            doc_kind="note_part",
            doc_id=part.id,
            body="a note comment",
        )
        await nt.archive_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            archived=True,
            expected_version=await _version(s, note.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        assert (
            len(
                await annotations.list_for_doc(
                    s, org_id=org, doc_kind="task_description", doc_id=task.id
                )
            )
            == 1
        )
        assert (
            len(await annotations.list_for_doc(s, org_id=org, doc_kind="note_part", doc_id=part.id))
            == 1
        )


async def test_the_note_family_stops_editing_what_is_in_the_bin() -> None:
    """``update_note`` used to rewrite the title and the body of a trashed
    note while ``update_part``, on the very same part, answered 404. The
    lifecycle actions that must reach into the bin say so explicitly."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "body SIERRA")
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=await _version(s, note.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        v = await _version(s, note.id)
        for call in (
            nt.update_note(
                s, org_id=org, actor_id=user, note_id=note.id, expected_version=v, title="renamed"
            ),
            nt.archive_note(
                s, org_id=org, actor_id=user, note_id=note.id, archived=True, expected_version=v
            ),
            nt.protect_note(
                s, org_id=org, actor_id=user, note_id=note.id, protected=True, expected_version=v
            ),
        ):
            with pytest.raises(NotFoundError):
                await call
        # Restore is the one action that must see the bin, and it works.
        await nt.restore_note(s, org_id=org, actor_id=user, note_id=note.id, expected_version=v)
        fresh = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
        assert fresh.deleted_at is None and fresh.title == "gated-subject"


async def test_deleting_what_is_already_in_the_bin_changes_nothing() -> None:
    """``deleted_at`` is the retention clock, not just a flag: re-stamping
    it on a retried delete pushed the note's purge date forward one retry
    at a time, and wrote a second ``_delete`` revision saying nothing had
    changed. The second call is now a no-op that returns the version."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "body VICTOR")
        v1 = await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=await _version(s, note.id),
        )
        row = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
        stamped_at = row.deleted_at
        revisions_after_delete = len(
            await entity_revisions.list_revisions(
                s,
                entity_kind=entity_revisions.ENTITY_KIND_NOTE,
                entity_id=note.id,
                limit=50,
            )
        )

        # The retry: same call, same intent, nothing to do.
        v2 = await nt.soft_delete_note(
            s, org_id=org, actor_id=user, note_id=note.id, expected_version=v1
        )
        assert v2 == v1
        again = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
        assert again.deleted_at == stamped_at  # the retention clock did not move
        assert (
            len(
                await entity_revisions.list_revisions(
                    s,
                    entity_kind=entity_revisions.ENTITY_KIND_NOTE,
                    entity_id=note.id,
                    limit=50,
                )
            )
            == revisions_after_delete
        )

        # Idempotent, not unguarded: a caller working from a stale read
        # still learns it is stale (mirror of garden_review.reject_node).
        with pytest.raises(ConflictError):
            await nt.soft_delete_note(
                s, org_id=org, actor_id=user, note_id=note.id, expected_version=v1 - 1
            )


async def test_a_live_proposal_is_not_restorable() -> None:
    """The un-reject opens exactly one state. A proposal still waiting in
    the review inbox has nothing to restore, and letting the restore path
    reach it would hand a version bump and a revision, on a note no read
    surface opens, to anyone with notes:write."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "pending body UNIFORM")
        await _propose(s, note.id)

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError):
            await nt.restore_note(
                s,
                org_id=org,
                actor_id=user,
                note_id=note.id,
                expected_version=await _version(s, note.id),
            )
        fresh = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
        assert fresh.version == 1  # untouched: no version bump, no revision


async def test_a_rejected_proposal_can_be_un_rejected() -> None:
    """``reject_node`` rejects by soft-deleting and promises the note is
    "reversible via the normal restore path". It was not: the restore
    refused a note still marked ``proposed``. Restoring one puts it back
    in the review inbox, which is what un-rejecting means."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "proposed body TANGO")
        await _propose(s, note.id)
        await garden_review.reject_node(s, org_id=org, actor_id=user, note_id=note.id)

    async with tenant_session(str(org), str(user)) as s:
        await nt.restore_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=await _version(s, note.id),
        )
        fresh = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
        assert fresh.deleted_at is None
        assert fresh.review_state == "proposed"  # back in the inbox, not silently approved
        pending = await garden_review.list_pending(s, org_id=org)
        assert note.id in {p.note_id for p in pending}


async def test_the_link_family_is_symmetric_on_the_perimeter() -> None:
    """Creating an edge was gated and destroying it was not, which is the
    wrong way round: nothing in the codebase unlinks as cleanup (the hard
    deletes ride the FK cascade, merge writes its own row, review leaves
    the edges standing so a restore brings them back). And the listings
    that feed the mindmap and the links panel now agree, so no edge is
    shown with an unlink button that would refuse."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        live = await _note(s, org, user, "live body WHISKEY")
        gated = await _note(s, org, user, "gated body XRAY")
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="T")
        await note_links.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=live.id,
            child_note_id=gated.id,
            kind="related",
        )
        await note_links.ensure_artifact_link(
            s, org_id=org, actor_id=user, note_id=gated.id, task_id=task.id
        )
        outgoing, incoming = await note_links.list_note_links(s, org_id=org, note_id=live.id)
        assert len(outgoing) + len(incoming) == 1
        assert len(await note_links.list_workspace_note_links(s, org_id=org)) == 1
        assert len(await note_links.list_note_task_links(s, org_id=org, task_id=task.id)) == 1

        await _propose(s, gated.id)

    async with tenant_session(str(org), str(user)) as s:
        # The edge is no longer listed anywhere...
        outgoing, incoming = await note_links.list_note_links(s, org_id=org, note_id=live.id)
        assert outgoing == [] and incoming == []
        assert await note_links.list_workspace_note_links(s, org_id=org) == []
        assert await note_links.list_note_task_links(s, org_id=org, task_id=task.id) == []
        # ... and cannot be removed either, on either side of the family.
        with pytest.raises(NotFoundError):
            await note_links.unlink_notes(
                s,
                org_id=org,
                actor_id=user,
                parent_note_id=live.id,
                child_note_id=gated.id,
                kind="related",
            )
        with pytest.raises(NotFoundError):
            await note_links.unlink_note_task(
                s, org_id=org, actor_id=user, note_id=gated.id, task_id=task.id, kind="artifact"
            )
        # The gate is on BOTH ends: gated as parent refuses too, not just
        # gated as child.
        with pytest.raises(NotFoundError):
            await note_links.unlink_notes(
                s,
                org_id=org,
                actor_id=user,
                parent_note_id=gated.id,
                child_note_id=live.id,
                kind="related",
            )
        # Two live notes are unaffected, and unlinking stays idempotent:
        # False for an edge that is not there, not an error.
        other = await _note(s, org, user, "another live body ZULU")
        assert (
            await note_links.unlink_notes(
                s,
                org_id=org,
                actor_id=user,
                parent_note_id=live.id,
                child_note_id=other.id,
                kind="related",
            )
            is False
        )


async def test_a_tag_does_not_come_off_a_gated_note() -> None:
    """``attach_tag`` was gated and ``detach_tag`` was not. Dropping the
    project of a note in the bin re-scopes its indexed blobs to the personal
    perimeter: a retrieval-visible change through a door no reader opens."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _note(s, org, user, "tagged body YANKEE")
        tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name=f"t-{uuid.uuid4().hex[:6]}"
        )
        await nt.attach_tag(s, org_id=org, actor_id=user, note_id=note.id, tag_id=tag.id)
        await nt.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=note.id,
            expected_version=await _version(s, note.id),
        )

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(NotFoundError):
            await nt.detach_tag(s, org_id=org, actor_id=user, note_id=note.id, tag_id=tag.id)
        with pytest.raises(NotFoundError):
            await nt.attach_tag(s, org_id=org, actor_id=user, note_id=note.id, tag_id=tag.id)
