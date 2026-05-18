"""Global user (not org-scoped).

Org membership is in ``Membership``. This table is not subject to
tenant RLS: login must be able to resolve the email before having an
org context.
"""

from __future__ import annotations

from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, TimestampMixin, UUIDPKMixin


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
