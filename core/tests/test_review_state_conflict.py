"""A review verb asked of a proposal in the wrong state says so.

Every review verb used to answer a state it does not act on in one of two
ways, and both were false. Checked after the version, a note approved in the
meantime came back as ``concurrency.stale_version`` (approving bumps the
version), which sends the caller to re-read and retry; on the retry the
version matched and the answer became ``note.not_found``, for a note the
caller was reading. Reported from the field on 2026-11-25: an agent revising a
distillation the person had just approved went round that loop.

The contract pinned here:

- a proposal that exists and is visible on the review surface, in a state the
  verb does not act on, is ``review.state_conflict`` (409), carrying the
  state it is in, the states the verb needs and its current version, and it
  wins over a stale version, since no re-read can make the verb apply;
- a stale version on a state the verb does act on is
  ``concurrency.stale_version`` with the ``current_version`` to re-read at,
  the same answer every other versioned write gives;
- 404 is kept for what is genuinely absent: an ordinary note, a missing id, a
  rejected (invalidated) fact.

Observed against the services of 396d63e4 (2026-11-26): four of the five
tests failed, the state-conflict ones on ``concurrency.stale_version`` and the
stale-version one on params ``{}`` with no ``current_version``. The fifth,
the ordinary note still 404, is the guard the fix must keep and passed on
both.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.services import garden_review as review
from mycelium_core.services import kg
from mycelium_core.services import notes as nt
from mycelium_core.services.auth import signup


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="STATE",
        )
    return r.org_id, r.user_id


async def _proposed(s: Any, org: uuid.UUID, user: uuid.UUID) -> Note:
    note = await nt.create_note(
        s, org_id=org, actor_id=user, kind=NoteKind.text, title="p", text="a proposed body"
    )
    note.origin_model_id = "m"
    note.review_state = "proposed"
    await s.flush()
    return note


async def _fact(s: Any, org: uuid.UUID) -> uuid.UUID:
    x = await kg.ensure_entity(s, org_id=org, name="Ada", entity_type="person")
    y = await kg.ensure_entity(s, org_id=org, name="Analytical", entity_type="project")
    edge = await kg.add_fact(
        s,
        org_id=org,
        subject_id=x.id,
        predicate="works_on",
        object_id=y.id,
        review_state="proposed",
        origin_model_id="fake-llm",
    )
    return edge.id


async def _state_conflict(
    call: Awaitable[Any], *, state: str, allowed: list[str], version: int
) -> None:
    with pytest.raises(ConflictError) as exc:
        await call
    assert exc.value.code is MessageCode.REVIEW_STATE_CONFLICT, exc.value.code
    assert exc.value.params == {
        "state": state,
        "allowed": allowed,
        "current_version": version,
    }


async def test_revising_a_note_approved_since_it_was_read_is_a_state_conflict() -> None:
    """The reported case, exactly: read at v, approved by someone else, then
    revised with v. The answer names the state, not a concurrent write."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _proposed(s, org, user)
        read_at = note.version
        approved = await review.approve_node(s, org_id=org, actor_id=user, note_id=note.id)
        assert approved.version != read_at
        for expected in (read_at, approved.version):
            await _state_conflict(
                review.revise_node(
                    s,
                    org_id=org,
                    actor_id=user,
                    note_id=note.id,
                    expected_version=expected,
                    text="better",
                    origin_model_id="m",
                ),
                state="approved",
                allowed=["declined", "proposed"],
                version=approved.version,
            )


async def test_every_note_review_verb_names_the_state_it_refuses() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        pending = await _proposed(s, org, user)
        await _state_conflict(
            review.remove_declined(
                s, org_id=org, actor_id=user, note_id=pending.id, expected_version=0
            ),
            state="proposed",
            allowed=["declined"],
            version=pending.version,
        )
        await _state_conflict(
            review.withdraw_approval(s, org_id=org, actor_id=user, note_id=pending.id),
            state="proposed",
            allowed=["approved"],
            version=pending.version,
        )
        approved = await review.approve_node(s, org_id=org, actor_id=user, note_id=pending.id)
        await _state_conflict(
            review.decline_node(
                s, org_id=org, actor_id=user, note_id=pending.id, expected_version=0
            ),
            state="approved",
            allowed=["declined", "proposed"],
            version=approved.version,
        )
        await _state_conflict(
            review.reopen_node(
                s, org_id=org, actor_id=user, note_id=pending.id, expected_version=0
            ),
            state="approved",
            allowed=["declined", "proposed"],
            version=approved.version,
        )


async def test_a_stale_version_on_a_state_the_verb_acts_on_carries_the_current_one() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        note = await _proposed(s, org, user)
        calls: list[Callable[[], Awaitable[Any]]] = [
            lambda: review.approve_node(
                s, org_id=org, actor_id=user, note_id=note.id, expected_version=note.version + 7
            ),
            lambda: review.decline_node(
                s, org_id=org, actor_id=user, note_id=note.id, expected_version=note.version + 7
            ),
            lambda: review.revise_node(
                s,
                org_id=org,
                actor_id=user,
                note_id=note.id,
                expected_version=note.version + 7,
                text="better",
                origin_model_id="m",
            ),
        ]
        for call in calls:
            with pytest.raises(ConflictError) as exc:
                await call()
            assert exc.value.code is MessageCode.CONFLICT_STALE_VERSION
            assert exc.value.params == {"current_version": note.version}


async def test_an_ordinary_note_is_still_absent_from_the_review_surface() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        plain = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="plain", text="ordinary"
        )
        with pytest.raises(NotFoundError):
            await review.revise_node(
                s,
                org_id=org,
                actor_id=user,
                note_id=plain.id,
                expected_version=plain.version,
                text="x",
                origin_model_id="m",
            )


async def test_a_fact_in_the_wrong_state_says_so_and_a_rejected_one_is_absent() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        eid = await _fact(s, org)
        edge = await kg.approve_fact(s, org_id=org, actor_id=user, edge_id=eid)
        await _state_conflict(
            kg.reject_fact(s, org_id=org, actor_id=user, edge_id=eid, expected_version=0),
            state="approved",
            allowed=["proposed", "rejected"],
            version=edge.version,
        )
        back = await kg.withdraw_fact_approval(s, org_id=org, actor_id=user, edge_id=eid)
        await _state_conflict(
            kg.withdraw_fact_approval(s, org_id=org, actor_id=user, edge_id=eid),
            state="proposed",
            allowed=["approved"],
            version=back.version,
        )
        with pytest.raises(ConflictError) as exc:
            await kg.approve_fact(
                s, org_id=org, actor_id=user, edge_id=eid, expected_version=back.version + 3
            )
        assert exc.value.params == {"current_version": back.version}

        await kg.reject_fact(s, org_id=org, actor_id=user, edge_id=eid)
        for verb in (kg.approve_fact, kg.withdraw_fact_approval):
            with pytest.raises(DomainError) as absent:
                await verb(s, org_id=org, actor_id=user, edge_id=eid)
            assert absent.value.code is MessageCode.KG_FACT_NOT_FOUND
