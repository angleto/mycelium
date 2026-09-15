"""A capability delegates a LIVE authority, not a frozen one.

The app-level scope gate used to return early for a capability token, on
the strength of a comment: "it carries its own action/resource
authorization". True of the action and the resource, checked where the
token is redeemed. False of the AUTHORITY behind them, which nobody
re-checked.

What that left, measurably: ``require_agent_scope`` begins
``if scope is None: return``, and ``current_claims_optional`` returns
``{}`` for a capability, so on that branch EVERY per-route scope check
passes. A scoped assistant that minted one had converted its restricted
identity into a credential on which the restriction does not run. The
grant was decided once, at mint, and frozen -- the ambient authority
SEC-09 forbids.

The fix records the minting credential on the grant and applies ITS scope
at redemption, so the two properties below hold together: what the minter
may not do it cannot delegate, and revoking the minter revokes what it
delegated. Neither is expressible while the authority is a snapshot.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from tests_helpers import seed_ai_assistant_identity

from mycelium_api.main import app
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import agent_tokens as at_svc
from mycelium_core.services import capability_tokens as cap_svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _workspace() -> tuple[uuid.UUID, uuid.UUID, str]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="CAPDEL")
    assert r.token is not None
    return r.org_id, r.user_id, r.token


async def _scoped_agent(org: uuid.UUID, user: uuid.UUID, scope: list[str]) -> tuple[str, uuid.UUID]:
    """An agent token bound to an assistant with exactly ``scope``."""
    async with tenant_session(str(org), str(user)) as s:
        ident = await seed_ai_assistant_identity(
            s, org_id=org, user_id=user, label="claude", scope=scope
        )
        minted = await at_svc.mint(
            s, org_id=org, actor_id=user, name="scoped", assistant_id=ident.ai_assistant_id
        )
    return minted.raw, minted.token.id


async def _part(c: AsyncClient, h: dict[str, str]) -> tuple[str, str]:
    note = (await c.post("/notes", headers=h, json={"kind": "text", "text": "corpo"})).json()
    full = (await c.get(f"/notes/{note['id']}", headers=h)).json()
    return str(note["id"]), str(full["parts"][0]["id"])


async def _mint_as_agent(
    org: uuid.UUID, user: uuid.UUID, token_id: uuid.UUID, part_id: uuid.UUID
) -> str:
    """Mint the way an agent's request mints: inside a session that
    publishes the agent-token id as the actor subject."""
    async with tenant_session(
        str(org), str(user), actor_kind="human_api", actor_subject_id=str(token_id)
    ) as s:
        res = await cap_svc.mint(
            s,
            org_id=org,
            actor_id=user,
            action=cap_svc.ACTION_NOTE_PART_BODY_WRITE,
            resource_kind=cap_svc.RESOURCE_NOTE_PART,
            resource_id=part_id,
        )
    return res.raw


async def test_the_grant_remembers_which_credential_made_it() -> None:
    """The column the rest rests on. Without it the redemption path has
    nothing to re-evaluate and can only trust a snapshot."""
    org, user, human = await _workspace()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = {"Authorization": f"Bearer {human}", "X-Workspace-Id": str(org)}
        _nid, pid = await _part(c, h)

    _raw, token_id = await _scoped_agent(org, user, ["notes:read", "notes:write"])
    await _mint_as_agent(org, user, token_id, uuid.UUID(pid))

    async with tenant_session(str(org), str(user)) as s:
        from sqlalchemy import text

        got = (
            await s.execute(
                text(
                    "SELECT minted_by_agent_token_id FROM capability_tokens "
                    "WHERE resource_id = :r ORDER BY created_at DESC LIMIT 1"
                ),
                {"r": pid},
            )
        ).scalar()
    assert got == token_id


async def test_a_human_minted_grant_records_no_credential() -> None:
    """The control. A person's own bearer publishes no agent-token
    subject, so the column stays NULL and the redemption inherits
    nothing: the change must not fence the human path it never touched."""
    org, user, human = await _workspace()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = {"Authorization": f"Bearer {human}", "X-Workspace-Id": str(org)}
        _nid, pid = await _part(c, h)

    async with tenant_session(str(org), str(user)) as s:
        await cap_svc.mint(
            s,
            org_id=org,
            actor_id=user,
            action=cap_svc.ACTION_NOTE_PART_BODY_WRITE,
            resource_kind=cap_svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(pid),
        )
        from sqlalchemy import text

        got = (
            await s.execute(
                text(
                    "SELECT minted_by_agent_token_id FROM capability_tokens "
                    "WHERE resource_id = :r ORDER BY created_at DESC LIMIT 1"
                ),
                {"r": pid},
            )
        ).scalar()
    assert got is None


async def test_what_the_minter_may_not_do_it_cannot_delegate() -> None:
    """The laundering, closed.

    The assistant here holds ``notes:read`` and nothing else, so the
    write route is denied to its own token. Before this change the
    capability it minted authenticated on a branch where the scope check
    does not run, and the same write succeeded -- the restriction laundered
    away by handing the work to a credential that carries no restriction."""
    org, user, human = await _workspace()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = {"Authorization": f"Bearer {human}", "X-Workspace-Id": str(org)}
        nid, pid = await _part(c, h)

        _raw, token_id = await _scoped_agent(org, user, ["notes:read"])
        cap = await _mint_as_agent(org, user, token_id, uuid.UUID(pid))

        r = await c.put(
            f"/notes/{nid}/parts/{pid}/body/stream?expected_version=1",
            headers={"Authorization": f"Bearer {cap}", "Content-Type": "text/markdown"},
            content=b"scritto con un'autorita' che il mintante non ha",
        )
        assert r.status_code == 403, r.text


async def test_a_minter_that_may_do_it_still_can() -> None:
    """The half that keeps the gate from being an outage: the same flow,
    with the write scope actually granted, still works end to end."""
    org, user, human = await _workspace()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = {"Authorization": f"Bearer {human}", "X-Workspace-Id": str(org)}
        nid, pid = await _part(c, h)

        _raw, token_id = await _scoped_agent(org, user, ["notes:read", "notes:write"])
        cap = await _mint_as_agent(org, user, token_id, uuid.UUID(pid))

        r = await c.put(
            f"/notes/{nid}/parts/{pid}/body/stream?expected_version=1",
            headers={"Authorization": f"Bearer {cap}", "Content-Type": "text/markdown"},
            content=b"scritto da una delega legittima",
        )
        assert r.status_code == 200, r.text


async def test_revoking_the_minter_revokes_what_it_delegated() -> None:
    """The property that only a LIVE authority can have.

    A frozen grant survives its minter: the capability was minted while
    the credential was good, and nothing later looked again. Now revoking
    the agent token takes down every capability it handed out, without
    touching them one by one -- which is what somebody reacting to a
    leaked agent token actually needs."""
    org, user, human = await _workspace()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = {"Authorization": f"Bearer {human}", "X-Workspace-Id": str(org)}
        nid, pid = await _part(c, h)

        _raw, token_id = await _scoped_agent(org, user, ["notes:read", "notes:write"])
        cap = await _mint_as_agent(org, user, token_id, uuid.UUID(pid))

        async with tenant_session(str(org), str(user)) as s:
            await at_svc.revoke(s, org_id=org, actor_id=user, token_id=token_id)

        r = await c.put(
            f"/notes/{nid}/parts/{pid}/body/stream?expected_version=1",
            headers={"Authorization": f"Bearer {cap}", "Content-Type": "text/markdown"},
            content=b"la delega di una credenziale ritirata",
        )
        assert r.status_code == 403, r.text
