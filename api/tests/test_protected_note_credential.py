"""A protected note refuses a bound assistant's credential, on BOTH doors.

MCP scopes are per-TOOL, never per-object: any credential holding
``notes:write`` holds it on every note in the workspace, including the
ones carrying the procedures an agent is supposed to follow. So the
boundary cannot be a scope, and ``Note.protected`` -- which existed, and
which only the distiller had ever read -- becomes the thing a write path
consults.

The key is the CREDENTIAL, not ``actor_kind``. That bucket cannot answer
the question: the REST adapter opens EVERY bearer request as
``human_api``, and an agent token resolves to JWT-shaped claims that take
the same branch, so a guard written on it would fence MCP and leave this
surface open. It also defaults to ``human`` on an empty GUC, which fails
open.

Which is why the tests that matter here are the HTTP ones. A
service-level test would pass against a guard that leaves the whole REST
surface unguarded, and would have been the comfortable thing to write.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from tests_helpers import seed_ai_assistant_identity

from mycelium_api.main import app
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.services import agent_tokens as at_svc
from mycelium_core.services import note_parts as parts_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.auth import signup

_CODE = "note.protected.agent_write"


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _workspace() -> tuple[uuid.UUID, uuid.UUID, str]:
    """An org, its owner, and the owner's own human bearer."""
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="PROT")
    assert r.token is not None
    return r.org_id, r.user_id, r.token


async def _agent_bearer(org: uuid.UUID, user: uuid.UUID) -> str:
    """A token bound to an assistant: the population this gate blocks."""
    async with tenant_session(str(org), str(user)) as s:
        ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user, label="claude")
        minted = await at_svc.mint(
            s, org_id=org, actor_id=user, name="bound", assistant_id=ident.ai_assistant_id
        )
    return minted.raw


async def _protected_note(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=notes_svc.NoteKind.text,
            title="Procedura",
            text="il corpo che non va riscritto da un agente\n",
        )
        await notes_svc.protect_note(
            s, org_id=org, actor_id=user, note_id=note.id, expected_version=note.version
        )
        return note.id


def _h(token: str, org: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Workspace-Id": str(org)}


async def test_the_rest_door_refuses_an_assistant_credential() -> None:
    """The test that decides whether this gate is real. ``PATCH
    /notes/{id}/parts/{pid}`` rides ``notes:write`` and reaches the very
    services MCP reaches, over a bearer the adapter labels ``human_api``.
    A guard on the actor kind would let this through."""
    org, user, human = await _workspace()
    note_id = await _protected_note(org, user)
    agent = await _agent_bearer(org, user)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        parts = (await c.get(f"/notes/{note_id}/parts", headers=_h(human, org))).json()
        pid, ver = parts[0]["id"], parts[0]["version"]

        r = await c.patch(
            f"/notes/{note_id}/parts/{pid}",
            headers=_h(agent, org),
            json={"expected_version": ver, "body": "riscritto da un agente"},
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == _CODE

        # And nothing moved.
        after = (await c.get(f"/notes/{note_id}/parts", headers=_h(human, org))).json()
        assert after[0]["body"] == "il corpo che non va riscritto da un agente\n"


async def test_the_same_write_succeeds_for_a_person() -> None:
    """The half that keeps the gate from being an outage. The SPA and the
    CLI authenticate with a human's own bearer, which publishes no
    agent-token subject, so they are outside the blocked population."""
    org, user, human = await _workspace()
    note_id = await _protected_note(org, user)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        parts = (await c.get(f"/notes/{note_id}/parts", headers=_h(human, org))).json()
        r = await c.patch(
            f"/notes/{note_id}/parts/{parts[0]['id']}",
            headers=_h(human, org),
            json={"expected_version": parts[0]["version"], "body": "riscritto da una persona"},
        )
        assert r.status_code == 200, r.text


async def test_an_unprotected_note_is_untouched_by_the_gate() -> None:
    """The gate is a flag, not a prohibition on assistants: without it the
    same credential writes freely."""
    org, user, human = await _workspace()
    agent = await _agent_bearer(org, user)
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=notes_svc.NoteKind.text, text="ordinaria\n"
        )
        note_id = note.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        parts = (await c.get(f"/notes/{note_id}/parts", headers=_h(human, org))).json()
        r = await c.patch(
            f"/notes/{note_id}/parts/{parts[0]['id']}",
            headers=_h(agent, org),
            json={"expected_version": parts[0]["version"], "body": "scritto da un agente"},
        )
        assert r.status_code == 200, r.text


async def test_the_flag_guards_its_own_switch() -> None:
    """The asymmetry, and the reason the condition is "protected NOW".

    Clearing the flag costs the same ``notes:write`` as the writes it
    protects against, so a gate that let an assistant unprotect a note
    would be a gate with its own key taped to it. Setting it stays
    allowed: an assistant may protect what it judges worth protecting,
    and may not unprotect afterwards."""
    org, user, human = await _workspace()
    agent = await _agent_bearer(org, user)
    async with tenant_session(str(org), str(user)) as s:
        note = await notes_svc.create_note(
            s, org_id=org, actor_id=user, kind=notes_svc.NoteKind.text, text="x\n"
        )
        note_id = note.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        cur = (await c.get(f"/notes/{note_id}", headers=_h(human, org))).json()
        protect = await c.post(
            f"/notes/{note_id}/protect",
            headers=_h(agent, org),
            json={"expected_version": cur["version"]},
        )
        assert protect.status_code == 200, protect.text

        release = await c.post(
            f"/notes/{note_id}/unprotect",
            headers=_h(agent, org),
            json={"expected_version": protect.json()["version"]},
        )
        assert release.status_code == 400, release.text
        assert release.json()["code"] == _CODE

        # A person can still release it: the note is not locked forever.
        assert (
            await c.post(
                f"/notes/{note_id}/unprotect",
                headers=_h(human, org),
                json={"expected_version": protect.json()["version"]},
            )
        ).status_code in (
            200,
            204,
        )


async def test_every_part_mutator_is_covered_not_just_update() -> None:
    """``append_to_part``, ``prepend_to_part`` and ``replace_in_part`` call
    ``optimistic_update`` directly and never delegate to ``update_part``:
    a guard placed on the mutators by hand would have missed three of the
    eleven. This asserts the shared choke point instead, by opening a
    session the way an agent's request opens one -- the credential's id
    published as the actor subject."""
    org, user, _human = await _workspace()
    note_id = await _protected_note(org, user)
    raw = await _agent_bearer(org, user)
    async with admin_session() as s:
        auth = await at_svc.authenticate(raw, session=s)
    assert auth is not None
    token_id = str(auth.token_id)

    async with tenant_session(str(org), str(user)) as s:
        part = (await parts_svc.list_parts(s, org_id=org, note_id=note_id))[0]
        part_id, version = part.id, part.version

    async with tenant_session(
        str(org), str(user), actor_kind="human_api", actor_subject_id=token_id
    ) as s:
        with pytest.raises(DomainError) as exc:
            await parts_svc.append_to_part(
                s,
                org_id=org,
                actor_id=user,
                part_id=part_id,
                chunk="coda",
                expected_version=version,
            )
        assert exc.value.code is MessageCode.NOTE_PROTECTED_AGENT_WRITE

        with pytest.raises(DomainError) as exc:
            await parts_svc.prepend_to_part(
                s,
                org_id=org,
                actor_id=user,
                part_id=part_id,
                text="testa",
                expected_version=version,
            )
        assert exc.value.code is MessageCode.NOTE_PROTECTED_AGENT_WRITE


async def test_a_session_without_an_agent_subject_writes_freely() -> None:
    """The control for the test above: the SAME service calls, on the same
    protected note, from a session that publishes no agent-token subject.
    Without this the previous test could be passing because the note is
    protected, not because the credential is an assistant's."""
    org, user, _human = await _workspace()
    note_id = await _protected_note(org, user)

    async with tenant_session(str(org), str(user)) as s:
        part = (await parts_svc.list_parts(s, org_id=org, note_id=note_id))[0]
        await parts_svc.append_to_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=part.id,
            chunk="coda umana",
            expected_version=part.version,
        )


async def test_the_capability_path_refuses_too() -> None:
    """The shape a narrower predicate lets through, and the reason it must
    not.

    A redeemed capability publishes the CAPABILITY token's id as the
    subject, which resolves to no agent token at all, so a guard that
    asked only "is this a bound assistant's token" would answer no and
    let the write pass. And the mint is reachable from an assistant: both
    mint tools ride ordinary ``notes:write`` over MCP. Failing open there
    would leave a door open precisely for the population being fenced.

    Refusing costs something and it is stated: a person holding a
    capability is refused too. Their way through is the bearer."""
    org, user, _human = await _workspace()
    note_id = await _protected_note(org, user)

    async with tenant_session(str(org), str(user)) as s:
        part = (await parts_svc.list_parts(s, org_id=org, note_id=note_id))[0]
        part_id, version = part.id, part.version

    async with tenant_session(
        str(org), str(user), actor_kind="mcp_token", actor_subject_id=str(uuid.uuid4())
    ) as s:
        with pytest.raises(DomainError) as exc:
            await parts_svc.update_part(
                s,
                org_id=org,
                actor_id=user,
                part_id=part_id,
                body="scritto con una capability",
                expected_version=version,
            )
        assert exc.value.code is MessageCode.NOTE_PROTECTED_AGENT_WRITE


async def test_the_dispatch_runtime_refuses_too() -> None:
    """The other open failure: the agent runtime shifts the actor to
    ``agent_run`` with an ``agent_runs`` id as subject, which is not an
    agent token either. It is the most autonomous writer in the system --
    the one C4 put into production -- so it is the last place a guard
    should fail open."""
    org, user, _human = await _workspace()
    note_id = await _protected_note(org, user)

    async with tenant_session(str(org), str(user)) as s:
        part = (await parts_svc.list_parts(s, org_id=org, note_id=note_id))[0]
        part_id, version = part.id, part.version

    async with tenant_session(
        str(org), str(user), actor_kind="agent_run", actor_subject_id=str(uuid.uuid4())
    ) as s:
        with pytest.raises(DomainError) as exc:
            await parts_svc.update_part(
                s,
                org_id=org,
                actor_id=user,
                part_id=part_id,
                body="scritto da un run",
                expected_version=version,
            )
        assert exc.value.code is MessageCode.NOTE_PROTECTED_AGENT_WRITE


async def test_the_system_session_is_open_by_construction() -> None:
    """The indexer, the migrations, the backfills and the distiller run on
    ``admin_session``, which publishes kind ``system`` and no subject.
    They must pass: a gate that fenced them would stop the machine from
    maintaining its own store, and that is a decision, not an oversight,
    so it gets a test."""
    org, user, _human = await _workspace()
    note_id = await _protected_note(org, user)

    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        part = (await parts_svc.list_parts(s, org_id=org, note_id=note_id))[0]
        await parts_svc.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=part.id,
            body="riscritto dal sistema",
            expected_version=part.version,
        )
