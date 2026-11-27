"""A message a person sends to one of their agent sessions (docs/adr/0079).

It is written by a person on the sessions page and delivered by a relay that
runs on the session's machine, which claims it and reports what happened.
Nothing here names a host: how the text reaches the session is the relay's
business, and a host that has no way in opens its sessions without
``accepts_messages``.

Delivery is at most once. An instruction carried out twice ("rebase and
push") cannot be undone, and one not carried out is visible on the page, so
nothing is ever sent again on its own. That is why ``not_delivered`` is
stored rather than read off a closed session: a closed session can be
reopened (a resume), and its new relay must not claim, days later, what the
page had shown as not delivered.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, OrgScopedMixin, TimestampMixin, UUIDPKMixin

#: The longest message a person may send. Claude Code accepts about a
#: million characters; an instruction typed on a page is a paragraph, and the
#: bound keeps a pasted log from becoming a prompt.
MAX_BODY_CHARS = 8000


class MessageState(enum.StrEnum):
    #: Written, not yet taken by a relay.
    QUEUED = "queued"
    #: Taken by the session's relay, which has not said what happened yet.
    CLAIMED = "claimed"
    #: Written into the session without an error. Not proof that the session
    #: read it: the host may answer nothing either way.
    DELIVERED = "delivered"
    #: The relay could not write it, and said why.
    FAILED = "failed"
    #: Still queued when its session closed. Final: a reopened session never
    #: receives it.
    NOT_DELIVERED = "not_delivered"


class AgentSessionMessage(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "agent_session_messages"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'claimed', 'delivered', 'failed', 'not_delivered')",
            name="ck_agent_session_messages_state",
        ),
        CheckConstraint(
            f"char_length(body) BETWEEN 1 AND {MAX_BODY_CHARS}",
            name="ck_agent_session_messages_body",
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    state_since: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: What the relay said when it could not deliver. Never the message text.
    failure: Mapped[str | None] = mapped_column(String(300), nullable=True)
