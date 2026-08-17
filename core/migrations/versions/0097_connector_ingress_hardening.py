"""Two ingress corrections: a key nobody can send, and an unbounded write.

**The ingress key Stripe cannot present.** The optional second factor travels in
a custom header, and a Stripe webhook endpoint sends what Stripe decides to
send: there is no field for one. Arming it on a Stripe connector therefore does
not harden the endpoint, it silences it -- the key is configured, no delivery
carries it, every event is refused, and because the two factors are collapsed
on purpose the refusal does not say which one failed. The symptom is "nothing
arrives", with no cause attached. Offering the option per provider is the fix;
clearing the keys already armed is this migration, because such a connector is
not merely misconfigured, it is broken, and leaving the hash in place would keep
it broken after the code stops offering the choice.

The signature is unaffected and always was the authority here: a caller without
the signing secret cannot get one event ingested.

**The unbounded write.** Every refused delivery appends to
``payment_webhook_deliveries``, which is what makes a refusal auditable and also
what lets anyone who learns a connector URL grow that table without bound. The
URL is not guessable (v4 UUID) but is not a secret either: it is pasted into a
provider dashboard and read off screens. ``payment_connector_refusals`` is a
fixed-window counter of REFUSALS per connector -- refusals, not requests, so a
correctly signed burst (by definition from whoever holds the signing secret) is
never throttled. Past the budget the refusal is UNCHANGED -- the caller gets
the same 401 it always got, learning nothing about having hit a limit -- and
only the ledger append is dropped. The ledger keeps the first N refusals of the
window, which is what an operator would read anyway; the thousandth adds
storage and no information.

Revision ID: 0097
Revises: 0096
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0097"
down_revision: str | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = current_setting('app.current_org', true)::uuid"


def upgrade() -> None:
    # A key on a connector whose provider cannot send it can only refuse
    # traffic. Clearing it is a repair, not a policy change: nothing that ever
    # verified stops verifying, because nothing ever verified.
    op.execute(
        "UPDATE payment_connectors SET api_key_hash = NULL, previous_api_key_hash = NULL, "
        "previous_api_key_expires_at = NULL "
        "WHERE provider <> 'mycelium' "
        "AND (api_key_hash IS NOT NULL OR previous_api_key_hash IS NOT NULL)"
    )

    op.create_table(
        "payment_connector_refusals",
        sa.Column(
            "connector_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("payment_connectors.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "org_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
    )
    # ENABLE, not FORCE, and for the same reason ``payment_connectors`` is:
    # the counter is maintained on the unauthenticated ingress path, which runs
    # with no tenant GUC set. The org column is still written and still
    # constrains every tenant-scoped read.
    op.execute("ALTER TABLE payment_connector_refusals ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_payment_connector_refusals ON payment_connector_refusals "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE payment_connector_refusals TO mycelium_app"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_payment_connector_refusals ON payment_connector_refusals")
    op.drop_table("payment_connector_refusals")
    # The cleared keys are not restored: they are one-way hashes, and the
    # connectors they belonged to could not verify a delivery while they were
    # set.
