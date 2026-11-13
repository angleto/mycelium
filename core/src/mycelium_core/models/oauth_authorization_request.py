"""An authorization request from a public OAuth client, waiting for a person.

The shape is the device authorization grant's (``device_authorization.py``),
and so is the reasoning, because the problem is the same one: a party that
holds nothing asks, a logged-in person answers, and the asking party then
collects. Three moments, three columns, and the credential exists only after
the last one.

``/authorize``   opens the row and sends the browser to the consent page
                 with an opaque request id. Nothing else exists yet.
approve / deny   a person answers on that page. Approval records WHO, in
                 WHICH workspace, under WHAT label and scope, and mints the
                 authorization code -- a code, not a credential.
``/token``       the client presents the code with its PKCE verifier. The
                 AI assistant and its bearer are created HERE, in the
                 transaction that marks the row redeemed.

**Why the credential is not created at approval.** A client that never
comes back for its code (the tab was closed, the host crashed, the redirect
was blocked) would leave behind an assistant nobody holds a secret for, on
the person's settings page, looking like a connection. Creating it at the
exchange makes that state unrepresentable rather than rare, which is the
same argument the device flow makes and the same answer.

**Why not ``oauth_codes``.** That table holds a code and the PKCE challenge
bound to it, and nothing else: it was built for the legacy flow, where the
person had already decided everything in the settings before the client
asked. A public client's code is only meaningful together with the approval
that produced it (who, where, what scope), and the request is meaningful
before there is any code at all. Putting the code's digest on the request
row makes "this code belongs to an approved request for this client and
redirect" true by construction instead of by a join that can be forgotten.

**What is stored as a digest.** The request id and the code are bearer
values -- whoever holds the first can have it approved by a logged-in
person, whoever holds the second plus the verifier can collect -- so only
their SHA-256 is here, as for ``device_authorizations`` and
``agent_tokens``. Reading this table gives nobody either one.

Pre-tenant, without row-level security, for the same reason as
``device_authorizations``: when a request is opened there is no user and no
workspace to scope it to. Every read is by digest of an unguessable value.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, TimestampMixin, UUIDPKMixin


class OAuthAuthorizationRequest(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "oauth_authorization_requests"

    # SHA-256 hex of the opaque id the consent page is opened with.
    request_id_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # The client that asked, as presented. Not a foreign key: a client is
    # whatever a resolution strategy recognises, and a Client ID Metadata
    # Document client is an https URL that no table holds. Removing a
    # registered client deletes its requests explicitly instead.
    client_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Snapshotted from the resolution at /authorize, so the consent page and
    # the exchange never resolve the client again -- for a metadata-document
    # client that would be a network fetch, and the exchange does none.
    client_name: Mapped[str] = mapped_column(String(100), nullable=False)
    client_registration: Mapped[str] = mapped_column(String(32), nullable=False)
    # The redirect URI the client asked with, already checked against the
    # registration. The code goes HERE and nowhere else, and ``/token`` must
    # be presented with the same string.
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    # Opaque to us, echoed back to the client. Bounded at the edge.
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    # RFC 8707. Recorded when sent, so the exchange can be held to it.
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The ``scope`` parameter as sent. Advisory: it preselects the picker
    # when every key is in the catalogue, and the person decides.
    requested_scope: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    approved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    denied_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    redeemed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Who answered, and the workspace an approval was given in. Read by the
    # exchange to decide whose authority the credential carries: never the
    # caller's, which at ``/token`` is a client holding a code.
    answered_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    approved_org_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    # What the person decided the credential is called and may do.
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    granted_scope: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    # Minted at approval. Digest only; the code itself leaves once, in the
    # redirect the consent page follows.
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    code_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # What the exchange produced. SET NULL rather than CASCADE: deleting the
    # assistant must not erase the record of the approval that created it.
    assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_assistants.id", ondelete="SET NULL"),
        nullable=True,
    )

    origin_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)


__all__ = ["OAuthAuthorizationRequest"]
