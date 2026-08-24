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
    #: FatturaPA 1.1.1.2 IdTrasmittente/IdCodice: the accredited channel's own
    #: fiscal code. Here rather than in an env var because it is a value with a
    #: reason to change and an operator has to be able to see and correct it
    #: without a redeploy. Empty means "not configured here", and the resolver
    #: then falls back to ``MYCELIUM_SDI_INTERMEDIARY_ID_CODICE`` -- which is
    #: what makes the move expand-only. 28 is CodiceType's maximum.
    sdi_intermediary_id_codice: Mapped[str] = mapped_column(
        String(28), nullable=False, server_default=""
    )
