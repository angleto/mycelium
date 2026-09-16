"""A working session, as a row the server minted rather than a name the
caller made up.

One authorization on a machine yields one credential, and every session
launched there reuses it. The MCP transport is stateless on purpose
(``mcp/server_http.py``: in stateful mode the principal froze into the
session task and ``Mcp-Session-Id`` became ambient authority that could
be replayed), so no session identifier reaches the server either.
Nothing in a request tells fifteen concurrent sessions apart from one.

That leaves exactly three places an agent's identity could come from,
and two of them fail:

- **the credential.** One per session would mean one authorization per
  session, which is the ceremony the whole connector flow exists to
  avoid, and the account-level connector could not carry fifteen anyway.
- **a string the client declares.** Automatic and free, and two sessions
  collide by accident with nothing to notice: the server would believe
  one worker held both leases, and a renewal from either would extend
  the other's possession.
- **an id the server mints on request.** This. The session asks once,
  gets an opaque id back, and passes it thereafter. No human, nothing
  created in advance, no second authorization, and it cannot collide
  because the caller does not choose it.

**Not the session state ADR-0049 forbids**, and the distinction is
precise rather than convenient. That prohibition is on the server
holding a caller's working state -- salience, pinning, scratch with a
TTL -- and on authority frozen to a connection. This row freezes no
authority: the bearer is re-authenticated on every request, this id is
never an authorization input, and what a caller may do is identical
whether it holds one or not. It is the "first-class durable entity with
provenance and RLS" that same ADR names as the legitimate home for
multi-agent coordination.

``label`` is decoration for a human reading a board and is never matched
on. Two sessions may both call themselves "verifier"; the id is what
makes them two.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class AgentWorker(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "agent_workers"

    # Provenance. Recorded so a human can ask which credential a worker
    # came in on; never read back as authority.
    opened_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    opened_by_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    # No FK: tokens rotate and the record of who was working outlives the
    # rotation.
    opened_by_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )

    label: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Retry key. A session whose reply was lost re-sends the same one and
    # gets the same worker back, instead of ending up with two and no way
    # to tell which holds its leases.
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    # Stamped by the operations a worker performs, so a human can see
    # which sessions are still doing anything. Not a heartbeat: nothing
    # expires on it, and a worker that stops being seen keeps whatever it
    # holds until the LEASE expires. Possession has the deadline; this is
    # only a reading.
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None
