"""Issuer API keys: long-lived bearer credentials scoped to ONE issuer profile.

A per-issuer-profile API key authenticates the public Invoice REST API
(``/api/v1``). Unlike :class:`AgentToken` it is bound to an ``issuer_profile``
(the cedente), not to a user, so it survives any operator's offboarding and its
authorization is a pure function of ``permissions`` + a pinned ``member`` role.

Storage discipline (see migration 0077 + the ``authenticate_issuer_api_key``
SECURITY DEFINER verifier): the raw value is returned exactly once at create /
rotate time; only its keyed hash ``HMAC-SHA256(ISSUER_KEY_PEPPER, raw)`` lives in
the DB, plus a non-secret ``key_public_id`` for the UI. Rotation keeps a
``previous_secret_hash`` valid until ``previous_secret_expires_at`` (default grace
= 0). Revocation or expiry kills both secrets at once.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class IssuerApiKey(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "issuer_api_keys"
    __table_args__ = (
        UniqueConstraint("secret_hash", name="uq_issuer_api_keys_secret_hash"),
        UniqueConstraint("key_public_id", name="uq_issuer_api_keys_key_public_id"),
        CheckConstraint(
            "length(name) >= 1 AND length(name) <= 120",
            name="ck_issuer_api_keys_name_len",
        ),
        Index("ix_issuer_api_keys_issuer_profile_id", "issuer_profile_id"),
        Index(
            "uq_issuer_api_keys_previous_secret_hash",
            "previous_secret_hash",
            unique=True,
            postgresql_where=text("previous_secret_hash IS NOT NULL"),
        ),
    )

    # The cedente this key acts for. ON DELETE CASCADE: the key belongs to the
    # profile, so deleting the profile removes its keys.
    issuer_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Who minted it. Audit only, NOT ownership -> ON DELETE SET NULL, so an
    # operator leaving never breaks a live key.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Human-readable label ("Gestionale X", "Nightly billing", ...).
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Non-secret public handle from an INDEPENDENT random draw; the shown prefix
    # is ``mycelium_ik_`` + key_public_id. Never a slice of the raw secret.
    key_public_id: Mapped[str] = mapped_column(String(24), nullable=False)
    # ``HMAC-SHA256(ISSUER_KEY_PEPPER, raw)``. A DB-only dump is inert without
    # the pepper. UNIQUE in the migration.
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Whitelisted subset of {invoice:read, invoice:compose, invoice:send,
    # invoice:credit_note, invoice:download}; validated at the service layer.
    permissions: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    # Optional CIDR allowlist (task d3dd69c3): NULL/empty = no restriction.
    # Canonical ``ipaddress.ip_network`` strings, validated at the service
    # layer; enforced app-side in ``authenticate`` (defence in depth for a
    # fiscal send credential -- the credential alone stops sufficing off-net).
    ip_allowlist: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    # Rotation-with-grace: the previous secret authenticates until its expiry
    # (default grace 0 -> hard rotation). Partial-unique in the migration.
    previous_secret_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_secret_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rotated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_secret_last_used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Mandatory (no never-expiring key); the service caps the requested lifetime
    # at ``issuer_key_max_lifetime_seconds`` (default 365d).
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["IssuerApiKey"]
