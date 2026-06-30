"""Scaleway as a first-class hosted LLM provider (task d2c60a83).

Two changes to ``org_llm_provider`` (migration 0026):

- widen the ``provider`` CHECK constraint to admit ``'scaleway'``
  (Scaleway Generative APIs are OpenAI-compatible, so the resolver reuses
  ``OpenAILLM`` with a Scaleway ``base_url``);
- add a per-row ``base_url`` column so an org can target a specific
  OpenAI-compatible endpoint (e.g. a Scaleway project-scoped URL) without
  changing the process-wide ``settings.*_base_url`` default.

No data migration: existing rows keep ``base_url = NULL`` (the resolver
then falls back to the provider's global default). RLS is unchanged
(the column inherits the table policy from 0026).

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CK = "ck_org_llm_provider_kind"


def upgrade() -> None:
    op.drop_constraint(_CK, "org_llm_provider", type_="check")
    op.create_check_constraint(
        _CK,
        "org_llm_provider",
        "provider IN ('local', 'openai', 'anthropic', 'scaleway')",
    )
    op.add_column(
        "org_llm_provider",
        sa.Column("base_url", sa.String(length=400), nullable=True),
    )


def downgrade() -> None:
    # Demote any scaleway rows to local before narrowing the CHECK, so the
    # downgrade never fails on an out-of-range value.
    op.execute("UPDATE org_llm_provider SET provider = 'local' WHERE provider = 'scaleway'")
    op.drop_column("org_llm_provider", "base_url")
    op.drop_constraint(_CK, "org_llm_provider", type_="check")
    op.create_check_constraint(
        _CK,
        "org_llm_provider",
        "provider IN ('local', 'openai', 'anthropic')",
    )
