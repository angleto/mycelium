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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.ai_assistant import AiAssistant
from flow_core.models.identity import Identity


async def seed_ai_assistant_identity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    label: str = "agent",
) -> Identity:
    """Insert an ``AiAssistant`` and return its mirrored ``Identity``.

    The identity row is materialised by the migration-0085 after-insert
    trigger, conditional on the assistant carrying a non-empty handle
    --- so we mint a unique handle inline rather than going through
    ``ai_assistants_svc.create_assistant`` (which is owner-gated and
    also mints an agent_token we don't need here).

    Tests that exercise the scheduler / dispatch agent paths use the
    returned identity as ``assignee_id`` so the task is unambiguously
    routed to the llm_agent pool under the "default assignee = creator"
    rule (the human-only fallback would otherwise auto-assign the task
    to the calling user)."""
    handle = f"{label}-{uuid.uuid4().hex[:8]}"
    assistant = AiAssistant(
        org_id=org_id,
        user_id=user_id,
        label=label,
        handle=handle,
        scope=[],
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
