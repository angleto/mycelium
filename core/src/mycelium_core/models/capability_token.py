"""Capability tokens: ephemeral, single-use, resource-scoped bearer creds.

Unlike an :class:`~mycelium_core.models.agent_token.AgentToken` (long-lived,
broad ``mcp`` scope), a capability token authorizes exactly ONE action on
ONE resource and is consumed on first successful use. It exists so an
agent that lacks a local Mycelium CLI can be handed a narrowly-scoped
credential to stream a note part's body straight to the API, instead of
being given the operator's long-lived PAT. Minted by an
already-authenticated principal (the MCP session).

See migration 0038 and the SECURITY DEFINER ``authenticate_capability_token``
helper, which validates expiry / consumption without a tenant GUC.

Raw token format
----------------
``mycelium_cap_<43 url-safe chars>``: a fixed ``mycelium_cap_`` discriminator the
verifier branches on (so a capability token never reaches the JWT or
agent-token paths), followed by ``secrets.token_urlsafe(32)`` (256 bits
of entropy). Only its SHA-256 digest lives in the DB.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, OrgScopedMixin, TimestampMixin, UUIDPKMixin


class CapabilityToken(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "capability_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``sha256(raw_value.encode("utf-8"))``. UNIQUE in the migration; the
    # raw value is returned exactly once at mint time and never stored.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # First chars of the raw value (``mycelium_cap_xxxxxxxx``); not a secret,
    # carried for audit / debug disambiguation.
    prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    # What the token authorizes: ``action`` on ``resource_kind`` /
    # ``resource_id``. Kept generic (free text) so a new capability kind
    # needs no schema change. Two kinds today:
    #   - action="note_part_body:write", resource_kind="note_part",
    #     resource_id=<part id>  (single-use, consumed on first write);
    #   - action="attachment:read", resource_kind="note"|"task",
    #     resource_id=<parent id>  (multi-use within the TTL, never
    #     consumed: reads any attachment of that note/task).
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # Short TTL (minted at ``now() + ttl``). NOT NULL: a capability token
    # with no expiry would defeat its purpose.
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # One-time marker, stamped on first successful use. A later use sees a
    # non-null value and is rejected by ``authenticate_capability_token``.
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Revocation, which the table had no way to express (task 1428a184):
    # a minted capability could only be waited out. That is tolerable at
    # five minutes and not at the ceiling, and it is no answer at all for
    # one that leaked -- the raw value is handed back once, and whoever
    # minted it could not take it away again.
    #
    # A column rather than a DELETE, for the reason ``consumed_at`` is one:
    # the row is the audit trail of a grant that existed, and a grant
    # somebody revoked is exactly the one worth being able to see later.
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The CREDENTIAL that minted this grant, or NULL when a person's own
    # bearer did. Without it the grant was a snapshot of an authority
    # nobody could re-check: the scope gate returns early for a capability
    # because "it carries its own action/resource authorization", which is
    # true of the action and false of the AUTHORITY behind it.
    #
    # With it the capability is a DELEGATION of a live authority: the gate
    # resolves this token and applies ITS scope to the route, so revoking
    # the agent token revokes every capability it minted and narrowing an
    # assistant's scope narrows them too. That is what SEC-09 asks for --
    # authorization re-evaluated per request, never frozen into a grant.
    minted_by_agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )


__all__ = ["CapabilityToken"]
