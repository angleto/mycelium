"""Actors: human-readable addressable identities (users + AI assistants)
that a task can be assigned to. Stage A of #21 (kill Executor); the
schema for actors / agent tokens / AI assistants lives in the squashed
``0001_baseline.sql``.

This module is the **single resolver** the UI / MCP picker walks to
turn an ``@handle`` into a concrete principal. Today the only sources
are workspace users and AI assistants; future stages add embedded
LLMs (``llm_embedded``) and may introduce a workspace_handles view to
enforce cross-source uniqueness inside a tenant.

Stage A scope:
- ``list_actors`` returns the candidates the picker draws from,
  optionally narrowed by a substring of ``handle`` or ``display_name``
  / ``label``.
- ``mint_user_handle`` / ``mint_assistant_handle`` are idempotent
  slug-and-dedupe helpers used by signup / create-assistant on the
  next write of any row that still carries the empty-string seed.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.membership import Membership
from mycelium_core.models.user import User

ActorKind = Literal["user", "ai_assistant"]


@dataclass(frozen=True, slots=True)
class Actor:
    handle: str
    kind: ActorKind
    display_name: str
    ref_id: uuid.UUID


_SLUG = re.compile(r"[^a-z0-9]+")


def _slugify(seed: str) -> str:
    """Normalise ``seed`` to the actor-handle charset
    ``[a-z][a-z0-9-]{1,38}[a-z0-9]``. Empty/all-symbol seeds collapse
    to an empty string (the caller adds a dedupe suffix or rejects)."""
    base = _SLUG.sub("-", seed.strip().lower()).strip("-")
    return base[:38]


async def list_actors(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    q: str | None = None,
    limit: int = 50,
    include_inactive: bool = False,
) -> list[Actor]:
    """Return the assignable actors visible to the caller's workspace.
    The list folds workspace members (User joined via Membership) and
    AI assistants owned by anyone in the workspace, sorted by handle.
    A ``q`` substring filters across handle / display_name / label.

    Deactivated principals are excluded by default. The word
    ASSIGNABLE in the first line is the whole argument: an assistant
    with ``is_active=false`` cannot authenticate at all (the SECURITY
    DEFINER ``authenticate_agent_token`` returns no row for it), and a
    user with ``is_active=false`` cannot log in or hold an API session
    (``auth.login``, ``deps`` on every request). Offering either in a
    picker proposes work to someone who cannot pick it up.

    The predicates sit in the two SELECTs rather than in the fold
    below, because ``q`` is filtered in Python and ``limit`` slices
    last: a limit applied over a set that still holds dead rows
    silently returns fewer than ``limit`` live actors.

    ``include_inactive`` is for the readers that resolve a HISTORICAL
    reference rather than offer a choice -- the owner chip turning a
    ``task.owner_id`` back into a name, the assignee chip of a task
    already assigned. Deactivating hides a principal from the pickers;
    it never rewrites what they already hold.

    Note this filter is the picker half only. The resolver half lives
    in ``services.identities``, which refuses to bind a deactivated
    principal on the write path -- a picker that stops offering what
    the resolver would still accept is a UI trick, not a fix.
    """
    m_stmt = (
        select(User.id, User.email, User.display_name, User.handle)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id, User.handle != "")
    )
    if not include_inactive:
        m_stmt = m_stmt.where(User.is_active.is_(True))
    members = list((await session.execute(m_stmt)).all())
    # Explicit org predicate on top of RLS, matching the members half
    # (which is org-scoped through Membership) and ``taxonomy.list_tags``.
    # ``ai_assistants`` is ENABLE but not FORCE row-level security, so
    # outside a tenant session this half would silently return nothing
    # while the members half returned the right rows.
    a_stmt = select(
        AiAssistant.id,
        AiAssistant.label,
        AiAssistant.handle,
    ).where(AiAssistant.org_id == org_id, AiAssistant.handle != "")
    if not include_inactive:
        a_stmt = a_stmt.where(AiAssistant.is_active.is_(True))
    assistants = list((await session.execute(a_stmt)).all())
    out: list[Actor] = []
    for uid, email, display, handle in members:
        out.append(
            Actor(
                handle=handle,
                kind="user",
                display_name=display or email,
                ref_id=uid,
            )
        )
    for aid, label, handle in assistants:
        out.append(Actor(handle=handle, kind="ai_assistant", display_name=label, ref_id=aid))
    needle = (q or "").strip().lower()
    if needle:
        out = [a for a in out if needle in a.handle.lower() or needle in a.display_name.lower()]
    out.sort(key=lambda a: a.handle)
    return out[:limit]


def _dedupe(base: str, taken: Iterable[str]) -> str:
    """Pick the first ``base``, ``base-2``, ``base-3``... not yet
    present in ``taken``. ``taken`` may itself be ``base`` (e.g. when
    the caller has just inserted the seed row) — that's why we start
    at ``-2`` rather than ``-1``."""
    if not base:
        # Caller passed an all-symbol seed; fall back to a short random
        # suffix so we never mint a NULL/empty handle.
        return f"_a_{uuid.uuid4().hex[:8]}"
    taken_set = set(taken)
    if base not in taken_set:
        return base
    n = 2
    while True:
        candidate = f"{base[: 38 - len(str(n)) - 1]}-{n}"
        if candidate not in taken_set:
            return candidate
        n += 1


async def mint_user_handle(session: AsyncSession, *, user_id: uuid.UUID, seed: str) -> str:
    """Idempotent: returns the user's current handle when already set,
    otherwise slugifies ``seed``, dedupes against the existing
    ``users.handle`` set and writes it. The empty-string seed sentinel
    used by migration 0060 is the only state in which this rewrites."""
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
    if user.handle:
        return user.handle
    base = _slugify(seed) or f"_u_{uuid.uuid4().hex[:8]}"
    taken = list(
        (await session.execute(select(User.handle).where(User.handle != ""))).scalars().all()
    )
    handle = _dedupe(base, taken)
    user.handle = handle
    await session.flush()
    return handle


async def mint_assistant_handle(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    assistant_id: uuid.UUID,
    seed: str,
) -> str:
    """Idempotent per-workspace handle for an AI assistant. Uniqueness
    is scoped to ``org_id`` (the partial unique index in 0060)."""
    row = (
        await session.execute(select(AiAssistant).where(AiAssistant.id == assistant_id))
    ).scalar_one()
    if row.handle:
        return row.handle
    base = _slugify(seed) or f"_a_{uuid.uuid4().hex[:8]}"
    taken = list(
        (
            await session.execute(
                select(AiAssistant.handle).where(
                    AiAssistant.org_id == org_id, AiAssistant.handle != ""
                )
            )
        )
        .scalars()
        .all()
    )
    handle = _dedupe(base, taken)
    row.handle = handle
    await session.flush()
    return handle


__all__ = [
    "Actor",
    "ActorKind",
    "list_actors",
    "mint_assistant_handle",
    "mint_user_handle",
]
