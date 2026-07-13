"""What actually gates an MCP editing a comment (regression + documentation).

Reproduces, at the service layer, exactly the arguments the MCP tools pass:
``edit(actor_id=<token-owner user>, actor_identity_id=<ai_assistant identity>)``.
Agent tokens are owner-minted (``agent_tokens`` requires ``Role.owner`` to
mint), so an MCP's ``actor_id`` is an owner and ``_require_author_or_admin``'s
admin fallback passes: the authz gate does NOT block a normally-configured MCP
(``test_R``). It only blocks a non-owner principal (``test_M``). The failure
mode a co-editing human+agent actually hits is the optimistic-version guard
(``test_V``) -- a stale ``expected_version`` after the other party saved --
which is what surfaces to the user as "the MCP can't save".
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, ForbiddenError
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.identity import Identity
from mycelium_core.services import annotations as anno
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup
from mycelium_core.services.memberships import add_member


async def _signup(org_name: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name=org_name,
        )
    return r.org_id, r.user_id


async def _make_assistant_identity(org: uuid.UUID, owner: uuid.UUID) -> uuid.UUID:
    handle = f"asst-{uuid.uuid4().hex[:6]}"
    async with tenant_session(str(org), str(owner)) as s:
        a = AiAssistant(
            org_id=org, user_id=owner, label="mcp", handle=handle, scope=[], is_active=True
        )
        s.add(a)
        await s.flush()
        aid = a.id
    async with tenant_session(str(org), str(owner)) as s:
        ident = (
            await s.execute(
                select(Identity).where(Identity.org_id == org, Identity.ai_assistant_id == aid)
            )
        ).scalar_one()
        return ident.id


async def _task_with_owner_comment(
    org: uuid.UUID, owner: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, int]:
    async with tenant_session(str(org), str(owner)) as s:
        t = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="diag")
        c = await anno.create_comment(
            s,
            org_id=org,
            actor_id=owner,
            doc_kind="task_description",
            doc_id=t.id,
            body="owner text",
        )
        return t.id, c.id, c.version


async def test_R_mcp_as_owner_CAN_edit_owner_comment() -> None:
    """Reported scenario: the MCP (assistant identity) edits a comment the
    human OWNER authored. Token is owner-minted -> actor_id is the owner."""
    org, owner = await _signup("diag-R")
    asst_ident = await _make_assistant_identity(org, owner)
    _, cid, ver = await _task_with_owner_comment(org, owner)

    async with tenant_session(str(org), str(owner)) as s:
        new_ver = await anno.edit(
            s,
            org_id=org,
            actor_id=owner,  # token-owner user (owner)
            actor_identity_id=asst_ident,  # the ai_assistant identity (MCP)
            annotation_id=cid,
            body="edited by mcp",
            expected_version=ver,
        )
    assert new_ver == ver + 1  # authz did NOT block the MCP


async def test_M_mcp_as_member_CANNOT_edit_owner_comment() -> None:
    """The ONLY way authz blocks: the acting user is a non-owner (member).
    Agent tokens can't be minted by a member, so this needs a downgraded /
    non-owner principal."""
    org, owner = await _signup("diag-M")
    # A second user, plain member.
    member_email = f"{uuid.uuid4().hex[:10]}@example.test"
    async with admin_session() as s:
        await signup(s, email=member_email, password="pw-strong-123", org_name="throwaway")
    async with tenant_session(str(org), str(owner)) as s:
        member_id = await add_member(
            s, org_id=org, actor_id=owner, email=member_email, role="member"
        )

    asst_ident = await _make_assistant_identity(org, owner)
    _, cid, ver = await _task_with_owner_comment(org, owner)

    with pytest.raises(ForbiddenError):
        async with tenant_session(str(org), str(member_id)) as s:
            await anno.edit(
                s,
                org_id=org,
                actor_id=member_id,
                actor_identity_id=asst_ident,
                annotation_id=cid,
                body="blocked",
                expected_version=ver,
            )


async def test_V_stale_version_conflicts_after_a_prior_save() -> None:
    """The temporal symptom ('saved as owner, THEN the MCP couldn't'): a
    prior save bumps the version; a retry with the stale version conflicts."""
    org, owner = await _signup("diag-V")
    asst_ident = await _make_assistant_identity(org, owner)
    _, cid, ver = await _task_with_owner_comment(org, owner)

    # The owner's SPA save bumps the version.
    async with tenant_session(str(org), str(owner)) as s:
        bumped = await anno.edit(
            s,
            org_id=org,
            actor_id=owner,
            actor_identity_id=None,
            annotation_id=cid,
            body="owner saved again",
            expected_version=ver,
        )
    assert bumped == ver + 1

    # The MCP retries with the STALE version it still holds: it conflicts, and
    # the error carries the CURRENT version so the caller can re-read + retry
    # (the recoverable-conflict half of the fix), not fail blind.
    with pytest.raises(ConflictError) as ei:
        async with tenant_session(str(org), str(owner)) as s:
            await anno.edit(
                s,
                org_id=org,
                actor_id=owner,
                actor_identity_id=asst_ident,
                annotation_id=cid,
                body="mcp retry",
                expected_version=ver,
            )
    assert ei.value.params.get("current_version") == ver + 1
