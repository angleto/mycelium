"""``garden_apply`` + auto-maturity worker step (ADR-0032 / ADR-0037).

Covers the mutating, reversible counterpart to ``classify_node``:

- accept/override perform the mutation (tag / link / maturity) and write a
  ``classification_feedback`` event;
- reject/ignore mutate nothing but still write the event;
- invalid suggestion_type / action raise a specific DomainError;
- ``auto_promote_mature`` promotes a central+curated growing hub to mature,
  records an ``action='auto'`` event with the signals snapshot, leaves
  under-curated notes alone, and is idempotent (a matured note is no longer
  ``growing`` so a second sweep is a no-op).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.classification_feedback import ClassificationFeedback
from flow_core.models.note import Note, NoteKind
from flow_core.models.note_link import NoteNoteLink
from flow_core.models.note_tag import NoteTag
from flow_core.models.tag import TagKind
from flow_core.services import garden_classify as gc
from flow_core.services import note_links, taxonomy
from flow_core.services import notes as notes_svc
from flow_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _make_workspace() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="GA")
    return a.org_id, a.user_id


async def _make_note(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body of {title}",
    )


async def _feedback_for(s: object, node_id: uuid.UUID) -> list[ClassificationFeedback]:
    rows = await s.execute(  # type: ignore[attr-defined]
        select(ClassificationFeedback).where(ClassificationFeedback.node_id == node_id)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# apply: accept mutates + records
# ---------------------------------------------------------------------------


async def test_apply_accept_tag_attaches_and_records_feedback() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _make_note(s, org, user, "n")
        tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="topic"
        )
        fb = await gc.apply_suggestion(
            s,
            org_id=org,
            actor_id=user,
            node_id=note.id,
            suggestion_type="tag",
            suggestion_value={"tag_id": str(tag.id)},
            action="accept",
        )
        assert fb.action == "accept"
        attached = (
            await s.execute(
                select(NoteTag).where(NoteTag.note_id == note.id, NoteTag.tag_id == tag.id)
            )
        ).scalar_one_or_none()
        assert attached is not None
        rows = await _feedback_for(s, note.id)
    assert any(r.action == "accept" and r.suggestion_type == "tag" for r in rows)


async def test_apply_accept_link_creates_typed_link() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "a")
        b = await _make_note(s, org, user, "b")
        await gc.apply_suggestion(
            s,
            org_id=org,
            actor_id=user,
            node_id=a.id,
            suggestion_type="link",
            suggestion_value={"target_id": str(b.id), "link_kind": "related"},
            action="accept",
        )
        # ``related`` is undirected (the service canonicalises parent <
        # child), so match the pair order-agnostically.
        link = (
            await s.execute(
                select(NoteNoteLink).where(
                    NoteNoteLink.parent_note_id.in_([a.id, b.id]),
                    NoteNoteLink.child_note_id.in_([a.id, b.id]),
                )
            )
        ).scalar_one_or_none()
    assert link is not None
    assert link.kind == "related"


async def test_apply_accept_maturity_sets_value() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _make_note(s, org, user, "n")
        await gc.apply_suggestion(
            s,
            org_id=org,
            actor_id=user,
            node_id=note.id,
            suggestion_type="maturity",
            suggestion_value={"value": "mature"},
            action="accept",
        )
        reloaded = (await s.execute(select(Note).where(Note.id == note.id))).scalar_one()
    assert reloaded.maturity == "mature"


async def test_apply_reject_records_event_without_mutation() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _make_note(s, org, user, "n")
        tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="nope"
        )
        await gc.apply_suggestion(
            s,
            org_id=org,
            actor_id=user,
            node_id=note.id,
            suggestion_type="tag",
            suggestion_value={"tag_id": str(tag.id)},
            action="reject",
        )
        attached = (
            await s.execute(
                select(NoteTag).where(NoteTag.note_id == note.id, NoteTag.tag_id == tag.id)
            )
        ).scalar_one_or_none()
        assert attached is None  # rejected -> not applied
        rows = await _feedback_for(s, note.id)
    assert len(rows) == 1
    assert rows[0].action == "reject"


async def test_apply_rejects_invalid_type_and_action() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        note = await _make_note(s, org, user, "n")
        with pytest.raises(DomainError) as type_err:
            await gc.apply_suggestion(
                s,
                org_id=org,
                actor_id=user,
                node_id=note.id,
                suggestion_type="bogus",
                suggestion_value={},
                action="accept",
            )
        assert type_err.value.code == MessageCode.GARDEN_SUGGESTION_TYPE_INVALID
        with pytest.raises(DomainError) as action_err:
            await gc.apply_suggestion(
                s,
                org_id=org,
                actor_id=user,
                node_id=note.id,
                suggestion_type="tag",
                suggestion_value={"tag_id": str(uuid.uuid4())},
                action="bogus",
            )
        assert action_err.value.code == MessageCode.GARDEN_ACTION_INVALID


# ---------------------------------------------------------------------------
# auto_promote_mature (the worker step behind automatic idea evolution)
# ---------------------------------------------------------------------------


async def _growing_hub(s: object, org: uuid.UUID, user: uuid.UUID, in_links: int) -> uuid.UUID:
    hub = await _make_note(s, org, user, "hub")
    for i in range(in_links):
        leaf = await _make_note(s, org, user, f"leaf{i}")
        await note_links.link_notes(
            s,  # type: ignore[arg-type]
            org_id=org,
            actor_id=user,
            parent_note_id=leaf.id,
            child_note_id=hub.id,
            kind="related",
        )
    await note_links.set_maturity(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        note_id=hub.id,
        maturity="growing",
    )
    return hub.id


async def test_auto_promote_matures_central_curated_hub_with_auto_event() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        hub_id = await _growing_hub(s, org, user, in_links=5)
        promoted = await gc.auto_promote_mature(s, org_id=org, actor_id=user)
        assert promoted >= 1
        reloaded = (await s.execute(select(Note).where(Note.id == hub_id))).scalar_one()
        assert reloaded.maturity == "mature"
        rows = await _feedback_for(s, hub_id)
    auto_rows = [r for r in rows if r.action == "auto" and r.suggestion_type == "maturity"]
    assert len(auto_rows) == 1
    assert "pr_pct" in auto_rows[0].signals_snapshot


async def test_auto_promote_skips_undercurated_growing_note() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        # One inbound link -> deg_term = 1/3 -> conf < MATURE_AUTO.
        hub_id = await _growing_hub(s, org, user, in_links=1)
        promoted = await gc.auto_promote_mature(s, org_id=org, actor_id=user)
        reloaded = (await s.execute(select(Note).where(Note.id == hub_id))).scalar_one()
    assert promoted == 0
    assert reloaded.maturity == "growing"


async def test_auto_promote_is_idempotent_after_maturing() -> None:
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        await _growing_hub(s, org, user, in_links=5)
        first = await gc.auto_promote_mature(s, org_id=org, actor_id=user)
        second = await gc.auto_promote_mature(s, org_id=org, actor_id=user)
    assert first >= 1
    assert second == 0  # already mature -> no longer growing -> nothing to do
