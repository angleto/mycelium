"""Per-org LLM provider selection (task 8afda4e7).

One row per org picks which provider backs the ``LLMProvider`` seam for
that tenant: the bundled local/Ollama model, or a hosted provider
(OpenAI/Anthropic/Scaleway) either on our key or on the org's own
Fernet-encrypted key (BYOK). ``services.llm_resolver.resolve_provider``
reads this row and derives the ``CostBasis`` the metering seam charges
on. No row => the local seam (``ai_providers.get_llm``), basis ``local``.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, CheckConstraint, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, TimestampMixin, VersionMixin


class LLMProviderKind(enum.StrEnum):
    local = "local"
    openai = "openai"
    anthropic = "anthropic"
    scaleway = "scaleway"


class OrgLLMProvider(TimestampMixin, VersionMixin, Base):
    __tablename__ = "org_llm_provider"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('local', 'openai', 'anthropic', 'scaleway')",
            name="ck_org_llm_provider_kind",
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, server_default="local")
    # Hosted model id (e.g. ``gpt-4o-mini``). NULL for ``local`` (the
    # model then comes from ``settings.open_model``).
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Fernet ciphertext of the org's OWN provider key (BYOK). NULL means
    # "use our key" for a hosted provider (basis our_key), or N/A for local.
    api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-row base URL for the hosted (OpenAI-compatible) endpoint. NULL
    # falls back to the provider's global default (``settings.*_base_url``).
    # Lets two orgs target different endpoints (e.g. a Scaleway
    # project-scoped URL) without changing process-wide env (task d2c60a83).
    base_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
