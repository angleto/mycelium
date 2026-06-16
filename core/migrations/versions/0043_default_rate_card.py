"""Fleet-wide default rate cards (task 62676443, mechanism B).

A per-org ``rate_cards`` row is an *override*; ``default_rate_card`` is
the *default salvo override*. When an org has no active card for a
``model_id``, the metering core falls back here, so ``our_key`` calls to
hosted providers are billed fleet-wide without a per-tenant seed (they
were FREE before this: ``meter_if_billable`` returned None on a missing
card). The table carries NO ``org_id`` and NO RLS -- it is shared config,
not tenant data -- so it is visible inside any tenant session; writes are
migration / platform-admin only.

Seed values are REASONABLE ESTIMATES (per-token, in credits, ~1 credit
= 1 USD) with markup = 1 (pass-through, no margin) per the decision on
this task. Confirm against the live provider pricing pages and adjust
either here (a follow-up migration) or per-org via the rate_cards admin
API. The seeded ids match the model ids the resolver actually sends
(llm_resolver._DEFAULT_*_MODEL and the curated Scaleway set), which is
what ``meter`` keys on (LLMResult.model_id == the configured model).

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (model_id, provider, cost_per_input, cost_per_output). Per-token, in
# credits; markup applied is 1 (pass-through). Estimates -- verify live.
_SEED: list[tuple[str, str, str, str]] = [
    # Scaleway curated set (Generative APIs, OpenAI-compatible).
    ("mistral-small-3.2-24b-instruct-2506", "scaleway", "0.00000020", "0.00000060"),
    ("gpt-oss-120b", "scaleway", "0.00000015", "0.00000060"),
    ("qwen3-235b-a22b-instruct-2507", "scaleway", "0.00000090", "0.00000090"),
    ("gemma-3-27b-it", "scaleway", "0.00000025", "0.00000050"),
    ("llama-3.3-70b-instruct", "scaleway", "0.00000090", "0.00000090"),
    # OpenAI / Anthropic safety-net defaults the resolver falls back to.
    ("gpt-4o-mini", "openai", "0.00000015", "0.00000060"),
    ("claude-3-5-haiku-latest", "anthropic", "0.00000080", "0.00000400"),
]


def upgrade() -> None:
    table = op.create_table(
        "default_rate_card",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("model_id", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column(
            "unit",
            postgresql.ENUM(name="rate_unit", create_type=False),
            nullable=False,
            server_default="token",
        ),
        sa.Column("credits_per_input", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("credits_per_output", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("provider_cost_per_input", sa.Numeric(18, 8), nullable=True),
        sa.Column("provider_cost_per_output", sa.Numeric(18, 8), nullable=True),
        sa.Column("markup", sa.Numeric(8, 4), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("tier", sa.String(40), nullable=True),
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
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.UniqueConstraint("model_id", name="uq_default_rate_card_model_id"),
    )

    # Shared config: no RLS (fleet-wide, read by every tenant session).
    # flow_app needs the grants the other tables get; writes are gated at
    # the service/platform-admin layer, never exposed to a tenant path.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE default_rate_card TO flow_app")

    op.bulk_insert(
        table,
        [
            {
                "model_id": model_id,
                "provider": provider,
                "provider_cost_per_input": Decimal(cin),
                "provider_cost_per_output": Decimal(cout),
                "markup": Decimal("1"),
            }
            for (model_id, provider, cin, cout) in _SEED
        ],
    )


def downgrade() -> None:
    op.drop_table("default_rate_card")
