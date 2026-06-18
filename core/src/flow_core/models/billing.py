"""Credit metering models (docs/adr/0019, FR-15).

``Wallet`` balance is mutated only through an atomic check-and-debit in
the service layer. ``CreditLedger`` and ``UsageRecord`` are append-only
(DB trigger, like activity_log) and idempotent per ``operation_id``.
"""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class LedgerEntryKind(enum.StrEnum):
    grant = "grant"
    debit = "debit"


class RateUnit(enum.StrEnum):
    token = "token"  # noqa: S105 (metering unit, not a secret)
    audio_min = "audio_min"
    tts_char = "tts_char"
    gb_month = "gb_month"


class CostBasis(enum.StrEnum):
    local = "local"
    our_key = "our_key"
    byok = "byok"


class StorageKind(enum.StrEnum):
    db = "db"
    s3 = "s3"


class Wallet(TimestampMixin, VersionMixin, Base):
    __tablename__ = "wallet"
    __table_args__ = (CheckConstraint("balance >= 0", name="ck_wallet_balance"),)

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")


class CreditLedger(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "credit_ledger"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_credit_ledger_amount"),
        UniqueConstraint("org_id", "operation_id", name="uq_credit_ledger_org_id"),
    )

    kind: Mapped[LedgerEntryKind] = mapped_column(
        SAEnum(LedgerEntryKind, name="ledger_entry_kind", native_enum=True, create_type=False),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RateCard(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "rate_cards"
    __table_args__ = (UniqueConstraint("org_id", "model_id", name="uq_rate_cards_org_id"),)

    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    unit: Mapped[RateUnit] = mapped_column(
        SAEnum(RateUnit, name="rate_unit", native_enum=True, create_type=False),
        nullable=False,
        server_default="token",
    )
    credits_per_input: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default="0"
    )
    credits_per_output: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default="0"
    )
    provider_cost_per_input: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    provider_cost_per_output: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    markup: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    tier: Mapped[str | None] = mapped_column(String(40), nullable=True)


class DefaultRateCard(UUIDPKMixin, TimestampMixin, VersionMixin, Base):
    """Fleet-wide default rate card (task 62676443, mechanism B).

    The per-org :class:`RateCard` is an *override*; this table is the
    *default salvo override*: when an org has no active card for a
    ``model_id``, the metering core (:func:`services.billing._active_rate_card`)
    falls back here, so ``our_key`` calls to hosted providers are billed
    fleet-wide without a per-tenant seed. It carries NO ``org_id`` and NO
    RLS (it is shared config, not tenant data); writes are migration- /
    platform-admin-only, reads are open to every tenant session. The
    cost columns mirror :class:`RateCard` so the same ``_compute_credits``
    consumes either (see the ``RateLike`` alias in the service)."""

    __tablename__ = "default_rate_card"
    __table_args__ = (UniqueConstraint("model_id", name="uq_default_rate_card_model_id"),)

    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    unit: Mapped[RateUnit] = mapped_column(
        SAEnum(RateUnit, name="rate_unit", native_enum=True, create_type=False),
        nullable=False,
        server_default="token",
    )
    credits_per_input: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default="0"
    )
    credits_per_output: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default="0"
    )
    provider_cost_per_input: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    provider_cost_per_output: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    markup: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    tier: Mapped[str | None] = mapped_column(String(40), nullable=True)


class StorageRate(TimestampMixin, VersionMixin, Base):
    __tablename__ = "storage_rates"
    __table_args__ = (PrimaryKeyConstraint("org_id", "kind", name="pk_storage_rates"),)

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kind: Mapped[StorageKind] = mapped_column(
        SAEnum(StorageKind, name="storage_kind", native_enum=True, create_type=False),
        nullable=False,
    )
    credits_per_gb_month: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default="0"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class BillingConfig(TimestampMixin, VersionMixin, Base):
    __tablename__ = "billing_config"

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    byok_fee_factor: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default="0.0001"
    )


class UsageRecord(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "usage_record"
    __table_args__ = (UniqueConstraint("org_id", "operation_id", name="uq_usage_record_org_id"),)

    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    op: Mapped[str] = mapped_column(String(80), nullable=False)
    basis: Mapped[CostBasis] = mapped_column(
        SAEnum(CostBasis, name="cost_basis", native_enum=True, create_type=False),
        nullable=False,
    )
    units_in: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    units_out: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    credits: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # Actor kind that incurred the spend (migration 0046, WS-F5): 'system'
    # for the autonomous metabolism, 'human_*' for user actions, captured
    # from the session GUC in meter(). NULL on rows predating the column.
    actor_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
