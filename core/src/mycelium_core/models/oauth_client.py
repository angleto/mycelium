"""A public OAuth client: a name and the places a code may be sent.

Two ways a row comes to exist, one table for both, because what they ARE is
the same thing and only who wrote them differs:

``dynamic``  written by anybody, unauthenticated, through Dynamic Client
             Registration (RFC 7591). This is how Claude, ChatGPT, Cursor,
             VS Code and every other MCP host that implements the
             specification connect without a person copying values around.
``manual``   written by a person in the settings, for a host that wants a
             client id pasted into it. Carries the workspace and the person
             who registered it, so it can be listed and removed there.

**A row grants nothing.** It is a redirect-URI allowlist with a label on it.
Nobody holds a secret for it (``token_endpoint_auth_method`` is ``none``,
PKCE is what binds a code to the party that asked for it), and a credential
only comes into existence when a logged-in person approves a request on the
consent page and the client then redeems the code. That is why registration
can be open to the internet: the consent page, which shows the redirect host,
is the control.

**Every field of a dynamic row is unauthenticated input.** ``client_name`` is
whatever the registrant chose to call itself, which is why the consent page
shows the redirect host beside it and never the name alone: the name is a
claim, the host is where the credential will actually go.

Row-level security splits the two kinds. A person's session sees the manual
clients of its workspace and nothing else; the pre-tenant OAuth endpoints run
as the system actor with no workspace, the only reader that sees every row,
because a client id arrives at ``/authorize`` before anybody has logged in.
"""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, TimestampMixin, UUIDPKMixin


class ClientRegistration(enum.StrEnum):
    """How the client came to exist. A closed set: the migration's CHECK
    constraint refuses anything else, and so does the type."""

    dynamic = "dynamic"
    manual = "manual"


class OAuthClient(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "oauth_clients"

    # The public identifier the client presents. Minted by the server, never
    # chosen by the registrant, and shaped so it cannot be mistaken for an AI
    # assistant UUID: the legacy flow uses that UUID as its client id, and the
    # two must be told apart by looking at the value rather than by asking
    # the database which table happens to hold it.
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    client_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Exact strings, compared exactly (except a loopback port, RFC 8252
    # §7.3). JSONB like ``ai_assistants.scope``: a short list read whole.
    redirect_uris: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    registration: Mapped[str] = mapped_column(String(16), nullable=False)

    # Set on ``manual`` rows and only on them (a CHECK constraint pairs the
    # three). A dynamic registration has no workspace and no author: it
    # arrives before anybody has logged in.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )

    # The address the server saw at registration, for the per-origin bound
    # on registrations nobody completes. Never one the client declared.
    origin_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # When a code from this client was first exchanged for a credential.
    # NULL means "registered and never used", which is what the sweep
    # deletes once it is old enough: an abandoned registration is noise, and
    # one that completed a consent is a client somebody relies on.
    first_grant_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["ClientRegistration", "OAuthClient"]
