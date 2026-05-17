"""Budget envelope (docs/adr/0014, FR-14): an org-scoped allocatable
amount for a period and category. Tasks attach via ``budget_id``;
consumption vs residual is computed in the service layer. No parallel
"personal" domain: a personal workspace is just an org."""

from __future__ import annotations

import datetime
import enum
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class BudgetPeriod(enum.StrEnum):
    month = "month"
    quarter = "quarter"
    year = "year"
    custom = "custom"


class Budget(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "budgets"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="ck_budgets_period"),
        CheckConstraint("amount >= 0", name="ck_budgets_amount"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    period_kind: Mapped[BudgetPeriod] = mapped_column(
        SAEnum(BudgetPeriod, name="budget_period", native_enum=True, create_type=False),
        nullable=False,
    )
    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="EUR")
