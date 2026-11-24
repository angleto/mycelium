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

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.classification_feedback import ClassificationFeedback
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.tag import TagKind
from mycelium_core.services import garden_classify as gc
from mycelium_core.services import note_links, taxonomy
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.auth import signup


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
    # Growing the way the sweep makes a touched seed growing, not by a
    # person's set_maturity: a person's explicit choice is one the
    # auto-promotion must leave alone (see the override tests below).
    await s.execute(  # type: ignore[attr-defined]
        update(Note).where(Note.id == hub.id).values(maturity="growing")
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


# ---------------------------------------------------------------------------
# A note a person brought back from mature is not re-asserted (ADR-0032)
# ---------------------------------------------------------------------------

# A person's own session over REST: ``human_api`` with no subject is what
# ``services.caller`` calls a person. ``tenant_session``'s default kind is the
# server acting, so it would hold nothing.
_PERSON = {"actor_kind": "human_api"}


async def _set(
    org: uuid.UUID,
    user: uuid.UUID,
    note_id: uuid.UUID,
    maturity: str,
    *,
    as_agent: bool = False,
) -> None:
    """A move, in its own transaction as a request would be: a person's, or
    an agent's (an MCP token)."""
    who = (
        {"actor_kind": "mcp_token", "actor_subject_id": str(uuid.uuid4())} if as_agent else _PERSON
    )
    async with tenant_session(str(org), str(user), **who) as s:
        await note_links.set_maturity(
            s, org_id=org, actor_id=user, note_id=note_id, maturity=maturity
        )


async def _sweep_promote(org: uuid.UUID, user: uuid.UUID) -> int:
    """The worker's auto-promotion alone, as the worker runs it."""
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        return await gc.auto_promote_mature(s, org_id=org, actor_id=user)


async def _worker_cycle(
    org: uuid.UUID, user: uuid.UUID, now: dt.datetime | None = None
) -> tuple[dict[str, int], int]:
    """What the garden worker runs for a workspace: the sweep and then the
    auto-promotion, in ONE system session (worker/garden.py)."""
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        moved = await note_links.tick_maturity_transitions(s, org_id=org, actor_id=user, now=now)
        promoted = await gc.auto_promote_mature(s, org_id=org, actor_id=user)
    return moved, promoted


async def _maturity(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> str:
    async with tenant_session(str(org), str(user)) as s:
        return str((await s.execute(select(Note.maturity).where(Note.id == note_id))).scalar_one())


async def _held(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> bool:
    async with tenant_session(str(org), str(user)) as s:
        return note_id in await note_links.person_held_maturity(s, org_id=org, note_ids=[note_id])


async def _hub_at(org: uuid.UUID, user: uuid.UUID, maturity: str) -> uuid.UUID:
    """A central, curated hub (five inbound links) sitting at ``maturity``,
    put there with no maturity event of its own."""
    async with tenant_session(str(org), str(user)) as s:
        hub_id = await _growing_hub(s, org, user, in_links=5)
        await s.execute(update(Note).where(Note.id == hub_id).values(maturity=maturity))
    return hub_id


async def test_auto_promote_does_not_undo_a_persons_demotion() -> None:
    """The board's mature -> growing move: a central, curated hub the person
    took back to growing stays growing, however often the worker runs."""
    org, user = await _make_workspace()
    hub_id = await _hub_at(org, user, "mature")
    await _set(org, user, hub_id, "growing")
    assert await _held(org, user, hub_id)
    assert (await _worker_cycle(org, user))[1] == 0
    assert (await _worker_cycle(org, user))[1] == 0
    assert await _maturity(org, user, hub_id) == "growing"


@pytest.mark.parametrize("to", ["seed", "dormant"])
async def test_the_worker_cycle_does_not_re_assert_a_note_taken_out_of_mature(to: str) -> None:
    """A person moves a hub out of mature into seed or dormant. Their move is
    a touch, so the very next sweep revives it to growing (writing
    ``auto_maturity``), and the auto-promotion runs right after in the same
    session: the hub must stay growing, not come back as mature."""
    org, user = await _make_workspace()
    hub_id = await _hub_at(org, user, "mature")
    await _set(org, user, hub_id, to)
    _, promoted = await _worker_cycle(org, user)
    assert await _maturity(org, user, hub_id) == "growing"
    assert promoted == 0
    assert await _held(org, user, hub_id)
    _, promoted = await _worker_cycle(org, user)
    assert promoted == 0
    assert await _maturity(org, user, hub_id) == "growing"


async def test_a_move_from_mature_to_seed_holds_through_the_sweep() -> None:
    """The sweep's own moves never end the hold, ageing included: 60
    untouched days take the hub to dormant, a touch revives it, and the
    auto-promotion still leaves it growing."""
    org, user = await _make_workspace()
    hub_id = await _hub_at(org, user, "mature")
    await _set(org, user, hub_id, "seed")
    await _worker_cycle(org, user)
    assert await _maturity(org, user, hub_id) == "growing"
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=61)
    moved, _ = await _worker_cycle(org, user, now=later)
    assert moved["to_dormant"] >= 1
    assert await _maturity(org, user, hub_id) == "dormant"
    # The person's move is still within the 7-day window: the sweep revives.
    _, promoted = await _worker_cycle(org, user)
    assert await _maturity(org, user, hub_id) == "growing"
    assert promoted == 0
    assert await _held(org, user, hub_id)


async def test_a_later_decay_ends_the_hold() -> None:
    """A supersedes link pointing at the held hub decays it to dormant
    (``auto_dormant``): a new judgement about the note, which ends the hold.
    Once the sweep revives it, the auto-promotion applies again."""
    org, user = await _make_workspace()
    hub_id = await _hub_at(org, user, "mature")
    await _set(org, user, hub_id, "growing")
    assert await _held(org, user, hub_id)
    async with tenant_session(str(org), str(user), **_PERSON) as s:
        newer = await _make_note(s, org, user, "the newer idea")
        await note_links.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=newer.id,
            child_note_id=hub_id,
            kind="supersedes",
        )
    assert await _maturity(org, user, hub_id) == "dormant"
    assert not await _held(org, user, hub_id)
    _, promoted = await _worker_cycle(org, user)
    assert await _maturity(org, user, hub_id) == "mature"
    assert promoted >= 1


async def test_a_persons_later_move_ends_the_hold() -> None:
    """The person's own later move decides: mature -> growing holds, then
    the person's growing -> seed (not out of mature) holds nothing."""
    org, user = await _make_workspace()
    hub_id = await _hub_at(org, user, "mature")
    await _set(org, user, hub_id, "growing")
    assert await _held(org, user, hub_id)
    await _set(org, user, hub_id, "seed")
    assert not await _held(org, user, hub_id)
    _, promoted = await _worker_cycle(org, user)
    assert promoted >= 1
    assert await _maturity(org, user, hub_id) == "mature"


@pytest.mark.parametrize("start", ["seed", "dormant"])
async def test_a_persons_move_into_growing_from_elsewhere_does_not_hold(start: str) -> None:
    """Only a move out of mature holds a note (ADR-0032): a person's seed ->
    growing or dormant -> growing says nothing against mature, so the
    auto-promotion promotes the hub as it would one the sweep made growing."""
    org, user = await _make_workspace()
    hub_id = await _hub_at(org, user, start)
    await _set(org, user, hub_id, "growing")
    assert not await _held(org, user, hub_id)
    assert await _sweep_promote(org, user) == 1
    assert await _maturity(org, user, hub_id) == "mature"


async def test_an_agents_move_out_of_mature_does_not_hold() -> None:
    """Agents propose, a person decides: an MCP token's mature -> growing is
    not a person's move, so the auto-promotion may promote the hub again."""
    org, user = await _make_workspace()
    hub_id = await _hub_at(org, user, "mature")
    await _set(org, user, hub_id, "growing", as_agent=True)
    assert not await _held(org, user, hub_id)
    assert await _sweep_promote(org, user) == 1
    assert await _maturity(org, user, hub_id) == "mature"


async def test_an_agents_later_move_does_not_end_a_persons_hold() -> None:
    org, user = await _make_workspace()
    hub_id = await _hub_at(org, user, "mature")
    await _set(org, user, hub_id, "growing")
    await _set(org, user, hub_id, "seed", as_agent=True)
    await _set(org, user, hub_id, "growing", as_agent=True)
    assert await _held(org, user, hub_id)
    assert await _sweep_promote(org, user) == 0


async def test_the_auto_promotions_own_write_is_not_a_hold() -> None:
    org, user = await _make_workspace()
    hub_id = await _hub_at(org, user, "growing")
    assert await _sweep_promote(org, user) == 1
    assert not await _held(org, user, hub_id)


async def _seasons(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> list[tuple[str, dict]]:
    from mycelium_core.models.activity_log import ActivityLog

    async with tenant_session(str(org), str(user)) as s:
        rows = (
            await s.execute(
                select(ActivityLog.actor_kind, ActivityLog.diff)
                .where(
                    ActivityLog.entity == "note",
                    ActivityLog.entity_id == note_id,
                    ActivityLog.action == "auto_maturity",
                )
                .order_by(ActivityLog.ts)
            )
        ).all()
    return [(k, d) for k, d in rows]


async def test_every_sweep_move_is_on_the_notes_history() -> None:
    """Each of the sweep's three moves leaves an ``auto_maturity`` row on the
    note it moved, written as ``system``: seed -> growing, growing -> dormant
    after 60 untouched days, dormant -> growing on a touch."""
    org, user = await _make_workspace()
    async with tenant_session(str(org), str(user)) as s:
        nid = (await _make_note(s, org, user, "ages")).id
    await _worker_cycle(org, user)
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=61)
    await _worker_cycle(org, user, now=later)
    await _worker_cycle(org, user)
    assert await _seasons(org, user, nid) == [
        ("system", {"from": "seed", "to": "growing"}),
        ("system", {"from": "growing", "to": "dormant"}),
        ("system", {"from": "dormant", "to": "growing"}),
    ]
    # Not a touch: the liveness clock reads no system row, so a behavioural
    # check of this (the clock unmoved by the sweep) passes whatever
    # NOTE_TOUCH_ACTIONS lists; the membership is the claim that can fail.
    from mycelium_core.services import note_touch

    assert "auto_maturity" not in note_touch.NOTE_TOUCH_ACTIONS


async def test_the_hold_states_the_workspace_itself() -> None:
    """``person_held_maturity`` names the workspace in its activity-log query,
    not only through row-level security: on an owner connection, which the
    policies do not narrow, another workspace's held note is not held here."""
    import os

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    org, user = await _make_workspace()
    other, _ = await _make_workspace()
    hub_id = await _hub_at(org, user, "mature")
    await _set(org, user, hub_id, "growing")
    owner_url = os.environ["MYCELIUM_DATABASE_URL_SYNC"].replace(
        "postgresql+psycopg", "postgresql+asyncpg"
    )
    engine = create_async_engine(owner_url)
    try:
        async with AsyncSession(engine) as s:
            assert await note_links.person_held_maturity(s, org_id=org, note_ids=[hub_id]) == {
                hub_id
            }
            assert (
                await note_links.person_held_maturity(s, org_id=other, note_ids=[hub_id]) == set()
            )
    finally:
        await engine.dispose()
