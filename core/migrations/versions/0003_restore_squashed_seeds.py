"""Restore the two reference-data seeds the 2026-08-22 squash dropped.

A squash keeps the schema and drops the data: the new baseline is a
``pg_dump --schema-only``, so every row an old revision INSERTed is gone
from any database built from it. Sixteen revisions carrying data
TRANSFORMATIONS were preserved under ``core/migrations/archive/``, but
two revisions carrying data SEEDS were classified as schema-only and
were neither archived nor replaced:

- ``0074_system_settings_sdi_env``:
  ``INSERT INTO system_settings (id, sdi_environment) VALUES (true, 'test')``
- ``0043_default_rate_card``: an ``op.bulk_insert`` of the seven fleet
  fallback rate cards.

The difference matters: a transformation has nothing to do on a fresh
database, a seed is the fresh database's starting state.

Symptoms this repairs, both of which only ever appear on a database
built from the squashed baseline:

- ``system_settings``: the singleton is missing, so
  ``services.system_settings._get_or_create`` tries to create it on
  first read. Under concurrency several callers race and all but one get
  ``UniqueViolationError`` on the ``id IS TRUE`` primary key. That is
  what turned CI red on tag v2.2.19
  (``test_numbering_is_concurrency_safe_and_allocated_at_transmit``
  transmits five invoices with ``asyncio.gather``).
- ``default_rate_card``: the table is empty, so
  ``billing.resolve_rate`` returns None for any model without a per-org
  card and ``_compute_credits`` raises ``rate_card.not_found``. Every
  non-BYOK LLM call on a new deployment fails. Nothing tested it -- the
  suite seeds its own cards -- so it would have surfaced in production.

Production is NOT affected and this migration is a no-op there: it was
stamped to ``0001`` rather than replaying it, so both seeds survive
(verified 2026-08-23: system_settings 1 row, default_rate_card 7 rows).
The statements are written idempotent for exactly that reason.

Neither table carries RLS, so no ``owner_sees_all_tenants`` bracket is
needed beyond the one ``env.py`` wraps every run in.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Verbatim from 0043's ``_SEED``: (model_id, provider, cost_in, cost_out).
# Per-token, in credits, markup 1 (pass-through). Reproduced rather than
# re-derived so a database repaired here is identical to one built by the
# old chain -- the point of the migration is parity, not a new opinion on
# pricing.
_RATE_CARDS = [
    ("mistral-small-3.2-24b-instruct-2506", "scaleway", "0.00000020", "0.00000060"),
    ("gpt-oss-120b", "scaleway", "0.00000015", "0.00000060"),
    ("qwen3-235b-a22b-instruct-2507", "scaleway", "0.00000090", "0.00000090"),
    ("gemma-3-27b-it", "scaleway", "0.00000025", "0.00000050"),
    ("llama-3.3-70b-instruct", "scaleway", "0.00000090", "0.00000090"),
    ("gpt-4o-mini", "openai", "0.00000015", "0.00000060"),
    ("claude-3-5-haiku-latest", "anthropic", "0.00000080", "0.00000400"),
]


def upgrade() -> None:
    # ON CONFLICT DO NOTHING, not a SELECT-then-INSERT: the whole failure
    # being repaired is a race on this row, and a check-then-act here
    # would reproduce it during a concurrent deploy.
    op.execute(
        "INSERT INTO system_settings (id, sdi_environment) "
        "VALUES (true, 'test') ON CONFLICT (id) DO NOTHING"
    )

    # ``uq_default_rate_card_model_id`` is UNIQUE on model_id alone, so
    # the conflict target is model_id and NOT (model_id, provider): a row
    # already present under a different provider must be left exactly as
    # an operator set it.
    values = ", ".join(f"('{m}', '{p}', {cin}, {cout})" for (m, p, cin, cout) in _RATE_CARDS)
    op.execute(
        "INSERT INTO default_rate_card "
        "(model_id, provider, provider_cost_per_input, provider_cost_per_output, markup) "
        f"SELECT v.model_id, v.provider, v.cin, v.cout, 1 FROM (VALUES {values}) "
        "AS v(model_id, provider, cin, cout) "
        "ON CONFLICT (model_id) DO NOTHING"
    )


def downgrade() -> None:
    # Deliberately a no-op.
    #
    # This migration restores rows that a fresh database should have had
    # since ``0001``; it does not introduce a new concept that can be
    # withdrawn. Nothing distinguishes a row it inserted from one that
    # was always there (production's predate it by months), so a
    # DELETE here would strip the fleet rate cards and the SdI
    # environment switch from a database that never needed repairing --
    # billing would start raising ``rate_card.not_found`` on the way
    # DOWN, which is the failure this exists to prevent.
    pass
