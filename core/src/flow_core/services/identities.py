"""Identity service: lookups + lifecycle (docs/adr/0028).

Identity rows are populated lazily by the layers that create the
underlying subject:

- ``ensure_for_user(org, user)`` is called from signup /
  add-member flows (Stage 1 wires it into auth.signup; future
  invitation flows will call it too).
- ``ensure_for_ai_assistant(org, assistant)`` is called from
  ``services.ai_assistants.create``.

Idempotent by ``(org_id, handle)``: a second call with the same
identifier returns the existing row instead of conflicting.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.ai_assistant import AiAssistant
from flow_core.models.identity import Identity, IdentityKind
from flow_core.models.membership import Membership
from flow_core.models.user import User


async def ensure_for_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Identity:
    """Create or fetch the identity row for ``(org x user)``.

    Requires the user to have a non-empty handle (Flow signup
    enforces it). Idempotent: subsequent calls with the same
    ``(org, user)`` return the existing row.
    """
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise NotFoundError(MessageCode.USER_NOT_FOUND)
    if not user.handle:
        raise DomainError(MessageCode.IDENTITY_HANDLE_REQUIRED, kind="user")

    existing = (
        await session.execute(
            select(Identity).where(
                Identity.org_id == org_id,
                Identity.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    identity = Identity(
        org_id=org_id,
        kind=IdentityKind.user,
        handle=user.handle,
        user_id=user_id,
    )
    session.add(identity)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        # Lost the race against a concurrent insert (same org, same
        # handle): fetch and return the winner.
        return (
            await session.execute(
                select(Identity).where(
                    Identity.org_id == org_id,
                    Identity.user_id == user_id,
                )
            )
        ).scalar_one()
    return identity


async def ensure_for_ai_assistant(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    assistant_id: uuid.UUID,
) -> Identity:
    """Create or fetch the identity row for an ai_assistant.

    Idempotent. Requires a non-empty handle on the assistant row.
    """
    assistant = (
        await session.execute(
            select(AiAssistant).where(
                AiAssistant.id == assistant_id,
                AiAssistant.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if assistant is None:
        raise NotFoundError(MessageCode.AI_ASSISTANT_NOT_FOUND)
    if not assistant.handle:
        raise DomainError(MessageCode.IDENTITY_HANDLE_REQUIRED, kind="ai_assistant")

    existing = (
        await session.execute(
            select(Identity).where(
                Identity.org_id == org_id,
                Identity.ai_assistant_id == assistant_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    identity = Identity(
        org_id=org_id,
        kind=IdentityKind.ai_assistant,
        handle=assistant.handle,
        ai_assistant_id=assistant_id,
    )
    session.add(identity)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return (
            await session.execute(
                select(Identity).where(
                    Identity.org_id == org_id,
                    Identity.ai_assistant_id == assistant_id,
                )
            )
        ).scalar_one()
    return identity


async def lookup_by_handle(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    handle: str,
) -> Identity | None:
    """Resolve a handle to an Identity row in the current org, or None.

    Accepts three forms (DX: task 901f0f9f -- assigning by what the
    user actually knows, not an opaque id space):

      * a bare handle (``angelo``) -- the canonical stored form;
      * a handle with a leading ``@`` (``@angelo``) -- tolerated, the
        ``@`` is stripped before matching;
      * a login email (``angelo@leto.blue``) -- resolved to the org
        member whose ``users.email`` matches (case-insensitively), then
        to their identity. ``users.email`` is globally unique, but we
        still scope the join to org membership so a non-member's email
        never materialises an identity in this org.

    Empty handle returns None defensively (the caller should not pass an
    empty string).

    Self-heals legacy/drifted state: ``list_actors`` sources from the
    user/ai_assistant tables directly, but ``identities`` is the
    resolver. Pre-Stage-A signups (and assistants created before the
    handle-mint fix) can land in a state where the source table has a
    handle but the matching identity row is missing or carries a
    different handle (e.g. backfilled by a migration to a UUID
    sentinel and then renamed). When the lookup misses, we fall back
    to the source tables and ``ensure_*`` the identity so the next
    call hits the fast path.
    """
    handle = handle.strip().lstrip("@")
    if not handle:
        return None
    # Email form: resolve via the org member's login email. An ``@``
    # after the leading-strip means the caller passed ``local@domain``.
    if "@" in handle:
        user_id = (
            await session.execute(
                select(User.id)
                .join(Membership, Membership.user_id == User.id)
                .where(
                    Membership.org_id == org_id,
                    func.lower(User.email) == handle.lower(),
                )
            )
        ).scalar_one_or_none()
        if user_id is not None:
            return await ensure_for_user(session, org_id=org_id, user_id=user_id)
        return None
    row = (
        await session.execute(
            select(Identity).where(
                Identity.org_id == org_id,
                Identity.handle == handle,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    # Fallback: the picker's source-of-truth (users / ai_assistants)
    # diverged from identities. Re-materialise.
    user_id = (
        await session.execute(
            select(User.id)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == org_id, User.handle == handle)
        )
    ).scalar_one_or_none()
    if user_id is not None:
        return await ensure_for_user(session, org_id=org_id, user_id=user_id)
    assistant_id = (
        await session.execute(
            select(AiAssistant.id).where(
                AiAssistant.org_id == org_id,
                AiAssistant.handle == handle,
            )
        )
    ).scalar_one_or_none()
    if assistant_id is not None:
        return await ensure_for_ai_assistant(session, org_id=org_id, assistant_id=assistant_id)
    return None


async def get_identity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    identity_id: uuid.UUID,
) -> Identity:
    row = (
        await session.execute(
            select(Identity).where(
                Identity.id == identity_id,
                Identity.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.IDENTITY_NOT_FOUND)
    return row
