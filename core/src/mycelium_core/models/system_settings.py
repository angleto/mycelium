"""Global, single-row system settings (not org-scoped, no RLS).

Holds deployment-wide toggles an operator flips at runtime. Today: the SdI
environment (``test`` | ``production``) that selects which configured endpoint
URL the live RiceviFile send targets, so the test<->production switch is a DB
flip from Settings, not an env-var redeploy. Singleton: the boolean PK is
pinned TRUE so exactly one row can exist.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, TimestampMixin


class SystemSettings(TimestampMixin, Base):
    __tablename__ = "system_settings"
    __table_args__ = (
        CheckConstraint("id IS TRUE", name="system_settings_singleton"),
        CheckConstraint(
            "sdi_environment IN ('test', 'production')", name="system_settings_sdi_env"
        ),
    )

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))
    # 'test' (default, safe) or 'production'. Selects config.sdi_endpoint_url_test
    # vs sdi_endpoint_url_prod for the live SdICoop RiceviFile send.
    sdi_environment: Mapped[str] = mapped_column(String(16), nullable=False, server_default="test")
