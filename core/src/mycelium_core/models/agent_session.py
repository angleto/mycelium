"""A session a person steers: one process of an agent host, with what it is
working on and whether it is waiting for them (docs/adr/0078).

A worker (``agent_worker``) answers "which of the sessions on this
credential holds this task". It cannot answer the question a person running
ten agents at once asks, "which of them needs me", for two reasons measured
on 2026-11-26: a worker is opened only by an agent about to take a task, so
a session that is reading or waiting for a permission has none (12 sessions
alive, 3 workers open); and one Claude Code session speaks through two
credentials, the CLI's in its hooks and the connector's in the agent, which
:func:`agent_workers.belongs_to` correctly refuses to merge. The session is
the row above both.

It is opened by the host's hooks, through the CLI, and it is a CLAIM with a
date: ``attention`` is what the host last said, ``attention_since`` when,
and the server derives nothing from it. Workers join it by presenting the
``join_key`` the opening returned, which only that session was given; see
``services/agent_sessions.join``.

What it does not hold, on purpose: a filesystem path (it would carry the
user's home directory into a store every member reads), a terminal address
(only the machine it is on can use one), or anything a decision reads.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


def hash_join_key(key: str) -> str:
    """What is stored of a join key, and what a presented key is compared as."""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


class SessionAgent(enum.StrEnum):
    """Which host runs the session. ``unknown`` is never stored: it is how a
    worker opened outside any session is shown, so the page stays complete."""

    CLAUDE_CODE = "claude_code"


class Attention(enum.StrEnum):
    """What the session last said about whether it needs its person.

    ``blocked``: a prompt is open in it (a permission, a question) and it
    cannot go on without an answer. ``your_turn``: it finished what it was
    asked and waits for the next instruction. ``working``: neither."""

    WORKING = "working"
    BLOCKED = "blocked"
    YOUR_TURN = "your_turn"


class BlockedOn(enum.StrEnum):
    PERMISSION = "permission"
    QUESTION = "question"


class SessionCloseReason(enum.StrEnum):
    #: The host said it ended (Claude Code's ``SessionEnd``).
    ENDED = "ended"
    #: The host on its machine no longer lists it among the live sessions:
    #: the process is gone without saying so (a killed terminal, a crash).
    GONE = "gone"
    #: Nothing was heard from it for the idle bound and no worker of it is
    #: open, and its machine ran nothing that could say it is gone.
    IDLE = "idle"


class AgentSession(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        CheckConstraint("agent IN ('claude_code')", name="ck_agent_sessions_agent"),
        CheckConstraint(
            "attention IN ('working', 'blocked', 'your_turn')",
            name="ck_agent_sessions_attention",
        ),
        CheckConstraint(
            "(attention = 'blocked') = (blocked_on IS NOT NULL)",
            name="ck_agent_sessions_blocked_on",
        ),
        CheckConstraint(
            "blocked_on IS NULL OR blocked_on IN ('permission', 'question')",
            name="ck_agent_sessions_blocked_on_value",
        ),
        CheckConstraint(
            "(closed_at IS NULL) = (closed_reason IS NULL)",
            name="ck_agent_sessions_closed_reason",
        ),
        CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ('ended', 'gone', 'idle')",
            name="ck_agent_sessions_closed_reason_value",
        ),
    )

    # Provenance and the binding, the same three columns as a worker and
    # compared the same way: only the credential that opened a session may
    # report on it or close it (``agent_sessions.belongs_to``).
    opened_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    opened_by_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    opened_by_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )

    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    # The host's own id for the session, the retry key per opening
    # credential: a resume, or a reply lost to a timeout, finds the same row.
    host_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    project: Mapped[str | None] = mapped_column(String(128), nullable=True)
    machine: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)

    attention: Mapped[str] = mapped_column(String(16), nullable=False)
    attention_since: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    blocked_on: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # SHA-256 of the join key. The key itself is returned once, by the
    # opening, and never stored: a reader of this table cannot join a worker
    # to a session it was not given.
    join_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Said by the host when it opens the session: a relay on its machine
    # delivers the messages a person sends to it (docs/adr/0079). The page
    # offers a composer only where this is true.
    accepts_messages: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None
