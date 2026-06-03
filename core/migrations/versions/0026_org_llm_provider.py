"""Per-org LLM provider selection (task 8afda4e7).

One row per org selects the provider backing the ``LLMProvider`` seam:
the bundled local/Ollama model, or a hosted provider (OpenAI/Anthropic)
on our key or on the org's OWN Fernet-encrypted key (BYOK). The resolver
(``services.llm_resolver``) reads this to pick the provider and derive
the ``CostBasis`` the metering seam charges on. No row => local.

RLS pattern mirrors 0025 (garden_health_daily): ENABLE + FORCE row level
security, an org-predicate policy for USING and WITH CHECK, and the
``flow_app`` grant. ``org_id`` is the primary key (one row per org).

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "org_llm_provider",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="local"),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "provider IN ('local', 'openai', 'anthropic')",
            name="ck_org_llm_provider_kind",
        ),
    )

    op.execute("ALTER TABLE org_llm_provider ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE org_llm_provider FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_org_llm_provider ON org_llm_provider "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE org_llm_provider TO flow_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_org_llm_provider ON org_llm_provider")
    op.drop_table("org_llm_provider")
