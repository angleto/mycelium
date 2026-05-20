"""AI assistant: per-user identity an external AI tool (Claude Desktop,
Cursor, a custom MCP client) authenticates with against Flow.

Pattern mirrored from bitvision_phoenix's ``ai_assistants`` table: each
assistant is owned by a workspace user, carries a free-form label,
optional provider / model_id / notes (informational), a JSONB ``scope``
list (the MCP tool-permissions surface), and an ``is_active`` flag.
The actual bearer credential lives in ``agent_tokens`` rows linked
back via ``assistant_id`` — rotating the secret mints a new token row
and revokes the old one, the assistant row stays put so historical
attribution survives.

See migration 0059 for the SECURITY DEFINER function that joins
``agent_tokens`` to ``ai_assistants`` on authenticate.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class AiAssistant(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "ai_assistants"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSONB list of scope strings (e.g. ['tasks:read', 'time:write']);
    # the MCP gate filters @mcp.tool calls against this set. Empty list
    # = the assistant can call ZERO tools (deny-all default).
    scope: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    def scope_list(self) -> list[str]:
        """Defensive coercion: JSONB returns whatever was stored — list
        of str in the normal case, but a misbehaving SQL UPDATE could
        leave a dict / scalar. Anything that isn't ``list[str]`` is
        treated as no scopes (deny-all), matching the gate's safe
        default."""
        v: Any = self.scope
        if not isinstance(v, list):
            return []
        return [str(x) for x in v]


__all__ = ["AiAssistant"]
