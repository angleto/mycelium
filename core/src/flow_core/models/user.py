"""Utente globale (non org-scoped).

L'appartenenza a una org e in ``Membership``. La tabella non e soggetta
a RLS tenant: il login deve poter risolvere l'email prima di avere un
contesto org.
"""

from __future__ import annotations

from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, TimestampMixin, UUIDPKMixin


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true")
    )
