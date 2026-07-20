"""Cross-package test helpers (importable from any tests/ tree).

Pytest's rootdir is the workspace root (where ``pyproject.toml`` lives)
and gets added to ``sys.path`` automatically, so tests in
``api/tests/``, ``core/tests/``, ``mcp/tests/`` can ``from
tests_helpers import ...`` without any conftest gymnastics.

Kept intentionally minimal --- only the helpers that two or more test
packages would otherwise duplicate.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.mcp_scopes import DEFAULT_SCOPES
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.identity import Identity


async def seed_ai_assistant_identity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    label: str = "agent",
    scope: Sequence[str] | None = None,
) -> Identity:
    """Insert an ``AiAssistant`` and return its mirrored ``Identity``.

    The identity row is materialised by the migration-0085 after-insert
    trigger, conditional on the assistant carrying a non-empty handle
    --- so we mint a unique handle inline rather than going through
    ``ai_assistants_svc.create_assistant`` (which is owner-gated and
    also mints an agent_token we don't need here).

    ``scope`` mirrors ``create_assistant``: ``None`` seeds ``DEFAULT_SCOPES``
    (the every-non-danger default a real assistant gets), so a token bound
    to this identity behaves like a normal assistant under the per-tool /
    per-route scope gate (task c19f2f63, enabler B). An empty list is a
    deliberate deny-all assistant; the model treats ``[]`` as "no scopes",
    matching the gate's fail-closed default. Pass an explicit subset to seed
    a narrowly-scoped assistant.

    Tests that exercise the scheduler / dispatch agent paths use the
    returned identity as ``assignee_id`` so the task is unambiguously
    routed to the llm_agent pool under the "default assignee = creator"
    rule (the human-only fallback would otherwise auto-assign the task
    to the calling user)."""
    handle = f"{label}-{uuid.uuid4().hex[:8]}"
    eff_scope = list(DEFAULT_SCOPES) if scope is None else list(scope)
    assistant = AiAssistant(
        org_id=org_id,
        user_id=user_id,
        label=label,
        handle=handle,
        scope=eff_scope,
        is_active=True,
    )
    session.add(assistant)
    await session.flush()
    identity = (
        await session.execute(
            select(Identity).where(
                Identity.org_id == org_id,
                Identity.ai_assistant_id == assistant.id,
            )
        )
    ).scalar_one()
    return identity
