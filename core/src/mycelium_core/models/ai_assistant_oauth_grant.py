"""Where an AI assistant created by an OAuth consent came from (ADR-0069).

One row per such assistant, written in the transaction that creates it: the
client that holds its bearer, the redirect that client's codes go to, the
person who approved it and when. The authorization request carries the same
facts but is swept a week after it expires; this row lives as long as the
assistant, so the settings can always say who holds a token and the person
can decide whether to revoke it. Workspace data, under row-level security.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, TimestampMixin


class AiAssistantOAuthGrant(TimestampMixin, Base):
    __tablename__ = "ai_assistant_oauth_grants"

    assistant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_assistants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_registration: Mapped[str] = mapped_column(String(32), nullable=False)
    client_name: Mapped[str] = mapped_column(String(100), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["AiAssistantOAuthGrant"]
