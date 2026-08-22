"""Regression coverage for the assignee picker bugs reported on v2.0:

- Self-assigning a task (PATCH /tasks/{id} with the caller's own handle)
  used to raise ``DomainError(DOMAIN_ERROR)`` because the membership
  insert trigger short-circuited on the empty users.handle sentinel
  at signup time, so the user appeared in /actors (sourced from the
  source tables) but had no row in identities (queried by
  update_task → lookup_by_handle).
- AI assistants created via ``create_assistant`` had handle='' and no
  identity row, so the picker never listed them at all.

The fixes mint the handle + materialise the identity at row-creation
time in auth.signup and ai_assistants.create_assistant. This test
exercises both flows end-to-end at the service layer (which is where
the bug lived; the HTTP layer is a thin pass-through).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.identity import Identity
from mycelium_core.models.user import User
from mycelium_core.services import actors as actors_svc
from mycelium_core.services import ai_assistants as ai_svc
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import memberships as mem_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_signup_creates_user_handle_and_identity_row() -> None:
    """After signup the user has a non-empty handle AND a matching
    identity row in their org. Both are required for the assignee
    picker (sourced from users.handle) and PATCH /tasks
    (resolved via identities.handle) to agree."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        actors = await actors_svc.list_actors(s, org_id=org)
        user_actors = [a for a in actors if a.kind == "user" and a.ref_id == user]
        assert len(user_actors) == 1
        assert user_actors[0].handle != ""
        ident = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == org,
                    Identity.user_id == user,
                )
            )
        ).scalar_one_or_none()
        assert ident is not None, "signup must materialise identity row"
        assert ident.handle == user_actors[0].handle


async def test_self_assign_does_not_raise_domain_error() -> None:
    """A user assigning a task to themselves must succeed. This is the
    user-reported "Domain error" symptom — the picker accepts the click
    but the PATCH used to fail at update_task → lookup_by_handle."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="t")
        task_id, base_v = task.id, task.version
        actors = await actors_svc.list_actors(s, org_id=org)
        self_handle = next(a.handle for a in actors if a.kind == "user" and a.ref_id == user)
        new_v = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=base_v,
            values={"assignee_handle": self_handle},
        )
        assert new_v == base_v + 1
    async with tenant_session(str(org), str(user)) as s:
        reloaded = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        assert reloaded.assignee_id is not None


async def test_create_assistant_appears_in_picker_and_is_assignable() -> None:
    """An AI assistant (the Claude MCP path) must be both visible in the
    picker AND resolvable as a task assignee. Pre-fix both were broken:
    create_assistant left handle='' so list_actors filtered it out, and
    no identity row existed so PATCH /tasks would have raised."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        made = await ai_svc.create_assistant(s, org_id=org, actor_id=user, label="Claude")
        assert made.assistant.handle != ""
        actors = await actors_svc.list_actors(s, org_id=org)
        matches = [a for a in actors if a.kind == "ai_assistant" and a.ref_id == made.assistant.id]
        assert len(matches) == 1
        assistant_handle = matches[0].handle
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="ask claude")
        task_id, base_v = task.id, task.version
        new_v = await tasks_svc.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            expected_version=base_v,
            values={"assignee_handle": assistant_handle},
        )
        assert new_v == base_v + 1
    async with tenant_session(str(org), str(user)) as s:
        reloaded = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        ident = (
            await s.execute(
                select(Identity).where(
                    Identity.org_id == org,
                    Identity.ai_assistant_id == made.assistant.id,
                )
            )
        ).scalar_one_or_none()
        assert ident is not None
        assert reloaded.assignee_id == ident.id


async def test_lookup_by_handle_self_heals_when_identity_is_missing() -> None:
    """Pre-Stage-A users (and any post-migration drift) can land in a
    state where users.handle is set but the matching identity row was
    never materialised. lookup_by_handle now falls back to the source
    tables and ensures the identity on the spot. Simulates the bug by
    deleting the auto-created identity, then triggers an assign that
    must succeed (re-creating the identity transparently)."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        actors = await actors_svc.list_actors(s, org_id=org)
        self_handle = next(a.handle for a in actors if a.kind == "user" and a.ref_id == user)
        await s.execute(
            delete(Identity).where(
                Identity.org_id == org,
                Identity.user_id == user,
            )
        )
        await s.flush()
        ident = await identities_svc.lookup_by_handle(s, org_id=org, handle=self_handle)
        assert ident is not None
        assert ident.user_id == user
        assert ident.handle == self_handle


async def test_lookup_by_handle_self_heals_when_assistant_identity_is_missing() -> None:
    """Same self-heal path, for ai_assistant identities."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        made = await ai_svc.create_assistant(s, org_id=org, actor_id=user, label="Claude")
        await s.execute(
            delete(Identity).where(
                Identity.org_id == org,
                Identity.ai_assistant_id == made.assistant.id,
            )
        )
        await s.flush()
        ident = await identities_svc.lookup_by_handle(s, org_id=org, handle=made.assistant.handle)
        assert ident is not None
        assert ident.ai_assistant_id == made.assistant.id
        assert ident.handle == made.assistant.handle


# --- Deactivated principals: the directory and the resolver must agree ---
#
# The reported bug was the directory half only: list_actors folded users
# and assistants with no predicate on is_active, so a picker offered
# principals that cannot act (a deactivated user cannot log in; a
# deactivated assistant's token is refused by authenticate_agent_token).
# Filtering the picker alone would have been a UI trick: the write path
# resolves a handle through identities, which accepted anything. Both
# halves are covered here, and the refusal is asserted BY CODE -- an
# IDENTITY_NOT_FOUND for a handle sitting right there in the workspace
# would be a lie the caller cannot act on.


async def _deactivate_user(user_id: uuid.UUID) -> None:
    async with admin_session() as s:
        await s.execute(update(User).where(User.id == user_id).values(is_active=False))


async def test_a_deactivated_user_leaves_the_picker_but_reads_still_resolve() -> None:
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        actors = await actors_svc.list_actors(s, org_id=org)
        handle = next(a.handle for a in actors if a.kind == "user" and a.ref_id == user)

    await _deactivate_user(user)

    async with tenant_session(str(org), str(user)) as s:
        assert handle not in {a.handle for a in await actors_svc.list_actors(s, org_id=org)}
        # Searching for them by name does not bring them back either.
        assert handle not in {
            a.handle for a in await actors_svc.list_actors(s, org_id=org, q=handle)
        }
        # The opt-in is what the owner chip and the assigned-work inbox use.
        opted = await actors_svc.list_actors(s, org_id=org, include_inactive=True)
        assert handle in {a.handle for a in opted}
        ident = await identities_svc.lookup_by_handle(
            s, org_id=org, handle=handle, include_inactive=True
        )
        assert ident is not None


async def test_a_deactivated_user_cannot_be_assigned() -> None:
    """End to end through the write path, then at the chokepoint itself.

    ``update_task`` is the door the SPA and MCP both go through; the
    resolver underneath is what closes it for every other door
    (create_task, set_task_assignee, add_participant, assign_annotation),
    which is why both id-shaped forms are asserted too."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        actors = await actors_svc.list_actors(s, org_id=org)
        handle = next(a.handle for a in actors if a.kind == "user" and a.ref_id == user)
        ident_id = (await identities_svc.lookup_by_handle(s, org_id=org, handle=handle)).id  # type: ignore[union-attr]
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="t")
        task_id, base_v = task.id, task.version

    await _deactivate_user(user)

    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError) as ete:
            await tasks_svc.update_task(
                s,
                org_id=org,
                actor_id=user,
                task_id=task_id,
                expected_version=base_v,
                values={"assignee_handle": handle},
            )
        assert ete.value.code is MessageCode.IDENTITY_INACTIVE

    async with tenant_session(str(org), str(user)) as s:
        reloaded = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        # create_task self-assigns by default, so "unchanged" is the
        # assertion, not "unassigned": what matters is that the refused
        # write left the row and its version exactly as they were.
        assert reloaded.assignee_id == ident_id
        assert reloaded.version == base_v
        with pytest.raises(DomainError) as exc:
            await identities_svc.lookup_by_handle(s, org_id=org, handle=handle)
        assert exc.value.code is MessageCode.IDENTITY_INACTIVE
        assert exc.value.code is not MessageCode.IDENTITY_NOT_FOUND, (
            "the handle is right there in the workspace; a bare miss would be a lie"
        )
        assert exc.value.params["handle"] == handle
        assert exc.value.params["kind"] == "user"

        # The id-shaped door refuses too, otherwise it is a way around.
        with pytest.raises(DomainError) as exc2:
            await identities_svc.resolve_assignee(s, org_id=org, assignee_id=ident_id)
        assert exc2.value.code is MessageCode.IDENTITY_INACTIVE
        # And so does passing the raw USER id, the other accepted form.
        with pytest.raises(DomainError) as exc3:
            await identities_svc.resolve_assignee(s, org_id=org, assignee_id=user)
        assert exc3.value.code is MessageCode.IDENTITY_INACTIVE


async def test_a_deactivated_assistant_leaves_the_picker_and_refuses_assignment() -> None:
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        made = await ai_svc.create_assistant(s, org_id=org, actor_id=user, label="Claude")
        handle = made.assistant.handle
        assistant_id = made.assistant.id
        assistant_v = made.assistant.version
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="ask claude")
        task_id, base_v = task.id, task.version
    async with tenant_session(str(org), str(user)) as s:
        await ai_svc.update_assistant(
            s,
            org_id=org,
            actor_id=user,
            assistant_id=assistant_id,
            expected_version=assistant_v,
            is_active=False,
        )
    async with tenant_session(str(org), str(user)) as s:
        assert handle not in {a.handle for a in await actors_svc.list_actors(s, org_id=org)}
        assert handle in {
            a.handle for a in await actors_svc.list_actors(s, org_id=org, include_inactive=True)
        }
        with pytest.raises(DomainError) as exc:
            await tasks_svc.update_task(
                s,
                org_id=org,
                actor_id=user,
                task_id=task_id,
                expected_version=base_v,
                values={"assignee_handle": handle},
            )
        assert exc.value.code is MessageCode.IDENTITY_INACTIVE
        assert exc.value.params["kind"] == "ai_assistant"


async def test_the_limit_slices_live_actors_not_a_set_padded_with_dead_ones() -> None:
    """The predicate belongs in the SELECT, not in the fold.

    ``q`` is filtered in Python and ``out[:limit]`` slices last, so a
    filter applied after the slice returns fewer than ``limit`` live
    actors. The deactivated assistant is deliberately the one that sorts
    FIRST, so it is inside the slice: deactivating an arbitrary row
    would leave this green on the unfixed code.
    """
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        made = [
            await ai_svc.create_assistant(s, org_id=org, actor_id=user, label=f"aaa-bot-{i}")
            for i in range(4)
        ]
    ordered = sorted(made, key=lambda m: m.assistant.handle)
    dead = ordered[0].assistant
    dead_handle, dead_id, dead_v = dead.handle, dead.id, dead.version
    async with tenant_session(str(org), str(user)) as s:
        await ai_svc.update_assistant(
            s,
            org_id=org,
            actor_id=user,
            assistant_id=dead_id,
            expected_version=dead_v,
            is_active=False,
        )
    async with tenant_session(str(org), str(user)) as s:
        rows = await actors_svc.list_actors(s, org_id=org, q="aaa-bot", limit=3)
        assert len(rows) == 3, "the slice must be filled with live actors"
        assert dead_handle not in {a.handle for a in rows}


async def test_a_read_opt_in_never_materialises_an_identity_for_a_dead_principal() -> None:
    """The self-heal is for drift, not for resurrection.

    ``lookup_by_handle`` re-materialises a missing identity row from the
    source tables. Under ``include_inactive=True`` (the reader's flag)
    that must NOT happen: a read endpoint has no business writing an
    identity row for someone who was deactivated.
    """
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        actors = await actors_svc.list_actors(s, org_id=org)
        handle = next(a.handle for a in actors if a.kind == "user" and a.ref_id == user)
        await s.execute(delete(Identity).where(Identity.org_id == org, Identity.user_id == user))

    await _deactivate_user(user)

    async with tenant_session(str(org), str(user)) as s:
        assert (
            await identities_svc.lookup_by_handle(
                s, org_id=org, handle=handle, include_inactive=True
            )
            is None
        )
        # And the default path refuses by name rather than self-healing.
        with pytest.raises(DomainError) as exc:
            await identities_svc.lookup_by_handle(s, org_id=org, handle=handle)
        assert exc.value.code is MessageCode.IDENTITY_INACTIVE
    async with tenant_session(str(org), str(user)) as s:
        rows = (
            (
                await s.execute(
                    select(Identity).where(Identity.org_id == org, Identity.user_id == user)
                )
            )
            .scalars()
            .all()
        )
        assert list(rows) == [], "no identity row may be created for a deactivated user"


async def test_valid_handles_hint_does_not_advertise_a_handle_the_resolver_refuses() -> None:
    """``list_handles`` fills the ``valid_handles`` param of
    IDENTITY_NOT_FOUND, which reaches MCP clients verbatim. Handing an
    agent a handle that the resolver would then refuse is how a retry
    loop is built."""
    async with admin_session() as s:
        res = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
    org, user = res.org_id, res.user_id
    async with tenant_session(str(org), str(user)) as s:
        made = await ai_svc.create_assistant(s, org_id=org, actor_id=user, label="Ghost")
        handle, assistant_id = made.assistant.handle, made.assistant.id
        assistant_v = made.assistant.version
        assert handle in await identities_svc.list_handles(s, org_id=org)
    async with tenant_session(str(org), str(user)) as s:
        await ai_svc.update_assistant(
            s,
            org_id=org,
            actor_id=user,
            assistant_id=assistant_id,
            expected_version=assistant_v,
            is_active=False,
        )
    async with tenant_session(str(org), str(user)) as s:
        assert handle not in await identities_svc.list_handles(s, org_id=org)


# --- The other three doors: ex-members, task owner, and the routing that
# --- kept handing work to principals who cannot take it.


async def test_an_ex_member_keeps_their_history_and_stops_being_assignable() -> None:
    """Removal deletes the membership and NOTHING else, deliberately.

    The identity row is the person's historical address: five FKs into
    ``identities`` are ON DELETE SET NULL and ``task_participants`` is
    CASCADE, so dropping it would un-assign every task they held (a NULL
    assignee is indistinguishable from one nobody ever picked up) and
    erase creation attribution. So the row stays and the RESOLVER
    refuses -- and it says ``identity.not_member``, which is a different
    problem from ``identity.inactive`` and sends the caller to a
    different fix.
    """
    owner_email, leaver_email = _email(), _email()
    async with admin_session() as s:
        a = await signup(s, email=owner_email, password="pw-strong-123", org_name="W")
        b = await signup(s, email=leaver_email, password="pw-strong-123", org_name="O")
    org, owner = a.org_id, a.user_id
    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=leaver_email, role="member")
        await actors_svc.mint_user_handle(s, user_id=b.user_id, seed=leaver_email)
        ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=b.user_id)
        ident_id, handle = ident.id, ident.handle
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=owner, title="theirs", assignee_id=ident_id
        )
        task_id = task.id
        assert handle in {a_.handle for a_ in await actors_svc.list_actors(s, org_id=org)}

    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.remove_member(s, org_id=org, actor_id=owner, target_user_id=b.user_id)

    async with tenant_session(str(org), str(owner)) as s:
        # History intact: the row, and what it holds.
        still = (
            await s.execute(select(Identity).where(Identity.id == ident_id))
        ).scalar_one_or_none()
        assert still is not None, "the historical address must survive removal"
        held = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        assert held.assignee_id == ident_id, "removal must not un-assign their work"

        # Not offered, and not bindable, by any of the three doors.
        assert handle not in {a_.handle for a_ in await actors_svc.list_actors(s, org_id=org)}
        assert handle not in await identities_svc.list_handles(s, org_id=org)
        with pytest.raises(DomainError) as by_handle:
            await identities_svc.lookup_by_handle(s, org_id=org, handle=handle)
        assert by_handle.value.code is MessageCode.IDENTITY_NOT_MEMBER
        assert by_handle.value.params["handle"] == handle
        with pytest.raises(DomainError) as by_id:
            await identities_svc.resolve_assignee(s, org_id=org, assignee_id=ident_id)
        assert by_id.value.code is MessageCode.IDENTITY_NOT_MEMBER


async def test_a_task_owner_must_be_an_active_member_of_this_workspace() -> None:
    """``owner_id`` was the one structural reference with NO validation:
    a bare FK to ``users``, so any uuid from ANY workspace was accepted
    and ``_promote_due`` then read that stranger's timezone on every
    later patch."""
    async with admin_session() as s:
        mine = await signup(s, email=_email(), password="pw-strong-123", org_name="W")
        theirs = await signup(s, email=_email(), password="pw-strong-123", org_name="Other")
    org, owner = mine.org_id, mine.user_id
    stranger = theirs.user_id

    async with tenant_session(str(org), str(owner)) as s:
        with pytest.raises(DomainError) as at_create:
            await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="t", owner_id=stranger)
        assert at_create.value.code is MessageCode.TASK_OWNER_NOT_MEMBER

    async with tenant_session(str(org), str(owner)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="t")
        task_id, base_v = task.id, task.version
    async with tenant_session(str(org), str(owner)) as s:
        with pytest.raises(DomainError) as at_update:
            await tasks_svc.update_task(
                s,
                org_id=org,
                actor_id=owner,
                task_id=task_id,
                expected_version=base_v,
                values={"owner_id": stranger},
            )
        assert at_update.value.code is MessageCode.TASK_OWNER_NOT_MEMBER
    async with tenant_session(str(org), str(owner)) as s:
        reloaded = await tasks_svc.get_task(s, org_id=org, task_id=task_id)
        assert reloaded.owner_id == owner
        assert reloaded.version == base_v


async def test_a_deactivated_member_cannot_be_made_task_owner() -> None:
    owner_email, other_email = _email(), _email()
    async with admin_session() as s:
        a = await signup(s, email=owner_email, password="pw-strong-123", org_name="W")
        b = await signup(s, email=other_email, password="pw-strong-123", org_name="O")
    org, owner = a.org_id, a.user_id
    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=other_email, role="member")
        task = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="t")
        task_id, base_v = task.id, task.version

    await _deactivate_user(b.user_id)

    async with tenant_session(str(org), str(owner)) as s:
        with pytest.raises(DomainError) as exc:
            await tasks_svc.update_task(
                s,
                org_id=org,
                actor_id=owner,
                task_id=task_id,
                expected_version=base_v,
                values={"owner_id": b.user_id},
            )
        # A member, so not TASK_OWNER_NOT_MEMBER: the honest reason is
        # that they are deactivated, which is a different fix.
        assert exc.value.code is MessageCode.IDENTITY_INACTIVE
