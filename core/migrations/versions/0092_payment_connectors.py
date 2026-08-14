"""Inbound payment-provider connectors (Stripe -> FatturaPA), ADR-0051.

Four tables plus a SECURITY DEFINER resolver.

``payment_connectors`` is the per-issuer-profile configuration and the only one
that is ENABLE-but-NOT-FORCE row level security. That asymmetry is deliberate
and mirrors ``issuer_api_keys`` (0077): an inbound provider webhook arrives with
no session, no bearer and therefore no ``app.current_org``, so the tenant has to
be resolved BEFORE any tenant context exists. Under FORCE RLS even the owning
role sees zero rows, so the lookup goes through the owner-run
``resolve_payment_connector`` function, which blanks the caller's GUCs, probes by
the connector id carried in the URL, restores them, and returns just enough to
verify the request: the signing secret envelope (plus its grace copy while the
window is open) and the optional API-key hashes. The application then re-enters
through ``tenant_session`` so the actual INSERT is RLS-scoped like everything
else. Verification itself is a MAC over the raw body: the URL id is a routing
selector, never a credential.

``payment_connector_events`` is both the durable ingress ledger and the work
queue. Splitting receive from process is what keeps the endpoint honest: the
provider gets its 2xx as soon as the event is on disk, and the fiscal work (which
can take an SdI dispatch's 120 s) happens in a worker that claims rows with
``FOR UPDATE SKIP LOCKED`` under the ``status='processing'`` lease. Nothing is
held in process memory, so N replicas are interchangeable and a pod dying
mid-emission loses no event. ``UNIQUE (connector_id, provider_event_id)`` is the
at-least-once contract: a provider redelivery, a client retry and two replicas
racing the same POST all collapse onto one row.

``payment_object_links`` is the emission-idempotency ledger. It maps every
provider object that names the same money (invoice, payment intent, charge,
checkout session) onto the one document we emitted, so a second event for a
payment we already invoiced resolves instead of re-emitting, and a refund can
find its parent. The claim is written and committed BEFORE the SdI dispatch, so
a crash inside the ADR-0046 two-phase transmit resumes that document rather than
minting a second fiscal number for the same charge. The FK is RESTRICT, not SET
NULL: a dangling claim would silently re-open the double-emission door.

``payment_customer_links`` maps a provider customer onto a client tag.
``taxonomy.resolve_or_create_client`` dedupes on fiscal identity with a
SELECT-then-INSERT and there is no unique constraint behind it, so two concurrent
redeliveries for one customer would both miss and both insert, yielding two
client tags with two sezionali. Keying the connector's own identity map on the
provider customer id closes that race with a UNIQUE constraint instead of a lock.

Also widens the two actor-kind CHECK constraints with ``payment_connector`` so
the audit trail names the connector as the author of the documents it emits,
rather than laundering them through ``system``.

Revision ID: 0092
Revises: 0091
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0092"
down_revision: str | None = "0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"

# Post-0077 allowed actor kinds. Kept literal so the downgrade restores exactly.
_ACTOR_KINDS_OLD = (
    "'human_direct','human_api','human_telegram','agent_run','mcp_token','system','issuer_api_key'"
)
_ACTOR_KINDS_NEW = _ACTOR_KINDS_OLD + ",'payment_connector'"

# activity_log's CHECK has the clean name; entity_revision's carries the
# historically doubled one (0006 passed an explicit name= that the
# ck_%(table_name)s_%(constraint_name)s convention prefixed again).
_AL_ACTOR_KIND_CK = "ck_activity_log_actor_kind"
_ER_ACTOR_KIND_CK = "ck_entity_revision_ck_entity_revision_actor_kind"

_PROVIDERS = "'stripe','mycelium'"
_MODES = "'transmit','draft','off'"
_EMISSION_EVENTS = "'invoice.paid','payment_intent.succeeded','checkout.session.completed'"
_EVENT_STATUSES = (
    "'pending','processing','done','ignored','no_billing_data','needs_attention','dead'"
)
_OBJECT_KINDS = "'invoice','payment_intent','checkout_session','charge','credit_note','refund'"
_DELIVERY_OUTCOMES = (
    "'accepted','duplicate','signature_invalid','disabled','payload_invalid','too_large'"
)


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY p_{table} ON {table} USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO mycelium_app")


# The tenant resolver for an unauthenticated inbound webhook. Owner-run, so
# ENABLE-RLS does not apply to it; it blanks and restores the caller's GUCs the
# way ``authenticate_issuer_api_key`` does. It returns NOTHING for a revoked
# connector (the caller answers 404, never distinguishing "revoked" from
# "never existed"), and expires the grace copies here rather than in Python so
# a clock skew cannot widen a rotation window.
_RESOLVE_FN = """
CREATE FUNCTION public.resolve_payment_connector(
    p_connector_id uuid,
    OUT out_org_id uuid,
    OUT out_issuer_profile_id uuid,
    OUT out_provider text,
    OUT out_enabled boolean,
    OUT out_signing_secret_ciphertext text,
    OUT out_previous_signing_secret_ciphertext text,
    OUT out_api_key_hash bytea,
    OUT out_previous_api_key_hash bytea
) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_revoked timestamptz;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT c.org_id, c.issuer_profile_id, c.provider, c.enabled,
             c.signing_secret_ciphertext,
             CASE WHEN c.previous_signing_secret_expires_at IS NOT NULL
                   AND c.previous_signing_secret_expires_at > now()
                  THEN c.previous_signing_secret_ciphertext END,
             c.api_key_hash,
             CASE WHEN c.previous_api_key_expires_at IS NOT NULL
                   AND c.previous_api_key_expires_at > now()
                  THEN c.previous_api_key_hash END,
             c.revoked_at
        INTO out_org_id, out_issuer_profile_id, out_provider, out_enabled,
             out_signing_secret_ciphertext, out_previous_signing_secret_ciphertext,
             out_api_key_hash, out_previous_api_key_hash, v_revoked
        FROM payment_connectors c
        WHERE c.id = p_connector_id;

      PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
      PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);

      IF out_org_id IS NULL OR v_revoked IS NOT NULL THEN
        RETURN;
      END IF;
      RETURN NEXT;
    END;
    $$
"""


def upgrade() -> None:
    op.create_table(
        "payment_connectors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issuer_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(20), nullable=False, server_default="stripe"),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("signing_secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("previous_signing_secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("previous_signing_secret_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("api_key_hash", sa.LargeBinary(), nullable=True),
        sa.Column("previous_api_key_hash", sa.LargeBinary(), nullable=True),
        sa.Column("previous_api_key_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("invoice_mode", sa.String(16), nullable=False, server_default="transmit"),
        sa.Column("credit_note_mode", sa.String(16), nullable=False, server_default="transmit"),
        sa.Column("emission_event", sa.String(40), nullable=False, server_default="invoice.paid"),
        sa.Column(
            "payment_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("series", sa.String(20), nullable=True),
        sa.Column("default_purpose", sa.String(200), nullable=True),
        sa.Column("default_vat_rate", sa.Numeric(5, 2), nullable=True),
        sa.Column("default_vat_nature", sa.String(4), nullable=True),
        sa.Column("default_line_description", sa.String(200), nullable=True),
        sa.Column(
            "amounts_include_vat", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("default_payment_conditions_code", sa.String(4), nullable=True),
        sa.Column("default_payment_method_code", sa.String(4), nullable=True),
        sa.Column("default_country_code", sa.String(2), nullable=True),
        # Ordered candidate key lists, not single names: a real account carries
        # several spellings per field (a migration away from another
        # e-invoicing provider leaves its keys behind; a manual entry path adds
        # a capitalised variant). First key present wins.
        sa.Column(
            "metadata_vat_keys",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{vatId,vat_number,partita_iva}'::text[]"),
        ),
        sa.Column(
            "metadata_tax_code_keys",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{fiscal_code,tax_code,codice_fiscale}'::text[]"),
        ),
        sa.Column(
            "metadata_sdi_keys",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{codice_destinatario,sdi_code,sdi}'::text[]"),
        ),
        sa.Column(
            "metadata_pec_keys",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{pec}'::text[]"),
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint(f"provider IN ({_PROVIDERS})", name="ck_payment_connectors_provider"),
        sa.CheckConstraint(
            f"invoice_mode IN ({_MODES})", name="ck_payment_connectors_invoice_mode"
        ),
        sa.CheckConstraint(
            f"credit_note_mode IN ({_MODES})", name="ck_payment_connectors_credit_note_mode"
        ),
        sa.CheckConstraint(
            f"emission_event IN ({_EMISSION_EVENTS})", name="ck_payment_connectors_emission_event"
        ),
        sa.CheckConstraint(
            "length(label) >= 1 AND length(label) <= 120", name="ck_payment_connectors_label_len"
        ),
        sa.UniqueConstraint("issuer_profile_id", "label", name="uq_payment_connectors_label"),
    )
    op.create_index("ix_payment_connectors_org_id", "payment_connectors", ["org_id"])
    op.create_index(
        "ix_payment_connectors_issuer_profile_id", "payment_connectors", ["issuer_profile_id"]
    )

    op.create_table(
        "payment_connector_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("provider_customer_id", sa.String(255), nullable=True),
        sa.Column("last_error", sa.String(160), nullable=True),
        sa.Column("error_detail", sa.String(512), nullable=True),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
            f"status IN ({_EVENT_STATUSES})", name="ck_payment_connector_events_status"
        ),
        sa.UniqueConstraint(
            "connector_id", "provider_event_id", name="uq_payment_connector_events_dedupe"
        ),
    )
    op.create_index("ix_payment_connector_events_org_id", "payment_connector_events", ["org_id"])
    op.create_index(
        "ix_payment_connector_events_due",
        "payment_connector_events",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_payment_connector_events_processing",
        "payment_connector_events",
        ["last_attempt_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_payment_connector_events_attention",
        "payment_connector_events",
        ["connector_id", "created_at"],
        postgresql_where=sa.text("status IN ('needs_attention','dead')"),
    )
    op.create_index(
        "ix_payment_connector_events_awaiting",
        "payment_connector_events",
        ["connector_id", "provider_customer_id"],
        postgresql_where=sa.text("status = 'no_billing_data'"),
    )
    op.create_index(
        "ix_payment_connector_events_connector",
        "payment_connector_events",
        ["connector_id", "created_at"],
    )

    op.create_table(
        "payment_object_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("object_kind", sa.String(24), nullable=False),
        sa.Column("object_id", sa.String(255), nullable=False),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
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
            f"object_kind IN ({_OBJECT_KINDS})", name="ck_payment_object_links_kind"
        ),
        sa.UniqueConstraint(
            "connector_id", "object_kind", "object_id", name="uq_payment_object_links_object"
        ),
    )
    op.create_index("ix_payment_object_links_org_id", "payment_object_links", ["org_id"])
    op.create_index("ix_payment_object_links_invoice", "payment_object_links", ["invoice_id"])

    op.create_table(
        "payment_webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_connector_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider_event_id", sa.String(255), nullable=True),
        sa.Column("body_bytes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("body_sha256", sa.LargeBinary(), nullable=True),
        sa.Column(
            "signature_present", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("api_key_present", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
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
            f"outcome IN ({_DELIVERY_OUTCOMES})", name="ck_payment_webhook_deliveries_outcome"
        ),
    )
    op.create_index(
        "ix_payment_webhook_deliveries_org_id", "payment_webhook_deliveries", ["org_id"]
    )
    op.create_index(
        "ix_payment_webhook_deliveries_connector",
        "payment_webhook_deliveries",
        ["connector_id", "received_at"],
    )
    op.create_index(
        "ix_payment_webhook_deliveries_refused",
        "payment_webhook_deliveries",
        ["connector_id", "received_at"],
        postgresql_where=sa.text("outcome NOT IN ('accepted','duplicate')"),
    )

    op.create_table(
        "payment_customer_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_customer_id", sa.String(255), nullable=False),
        sa.Column("client_tag_id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.UniqueConstraint(
            "connector_id", "provider_customer_id", name="uq_payment_customer_links_customer"
        ),
    )
    op.create_index("ix_payment_customer_links_org_id", "payment_customer_links", ["org_id"])
    op.create_index(
        "ix_payment_customer_links_client_tag", "payment_customer_links", ["client_tag_id"]
    )

    # payment_connectors: ENABLE only -- the SECURITY DEFINER resolver must read
    # it with no tenant GUC. Its siblings are all FORCE.
    op.execute("ALTER TABLE payment_connectors ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_payment_connectors ON payment_connectors "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE payment_connectors TO mycelium_app")

    _rls("payment_connector_events")
    _rls("payment_webhook_deliveries")
    _rls("payment_object_links")
    _rls("payment_customer_links")

    op.execute(_RESOLVE_FN)
    op.execute("REVOKE ALL ON FUNCTION public.resolve_payment_connector(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.resolve_payment_connector(uuid) TO mycelium_app")

    op.execute(f"ALTER TABLE activity_log DROP CONSTRAINT {_AL_ACTOR_KIND_CK}")
    op.execute(
        f"ALTER TABLE activity_log ADD CONSTRAINT {_AL_ACTOR_KIND_CK} "
        f"CHECK (actor_kind IN ({_ACTOR_KINDS_NEW}))"
    )
    op.execute(f"ALTER TABLE entity_revision DROP CONSTRAINT {_ER_ACTOR_KIND_CK}")
    op.execute(
        f"ALTER TABLE entity_revision ADD CONSTRAINT {_ER_ACTOR_KIND_CK} "
        f"CHECK (actor_kind IN ({_ACTOR_KINDS_NEW}))"
    )


def downgrade() -> None:
    # Re-narrowing the actor-kind CHECK is NOT VALID on purpose.
    #
    # Both tables are append-only (a trigger refuses DELETE), so the rows this
    # subsystem already wrote cannot be removed to make a validating constraint
    # pass -- and they should not be: an audit trail that a downgrade can erase
    # is not an audit trail. NOT VALID constrains every FUTURE write exactly as
    # before while leaving the history readable, which is the only honest
    # reading of "undo the schema change" here. A deployment that really wants
    # the constraint validated must first age out the historical rows through
    # the normal retention path and then VALIDATE CONSTRAINT by hand.
    op.execute(f"ALTER TABLE entity_revision DROP CONSTRAINT {_ER_ACTOR_KIND_CK}")
    op.execute(
        f"ALTER TABLE entity_revision ADD CONSTRAINT {_ER_ACTOR_KIND_CK} "
        f"CHECK (actor_kind IN ({_ACTOR_KINDS_OLD})) NOT VALID"
    )
    op.execute(f"ALTER TABLE activity_log DROP CONSTRAINT {_AL_ACTOR_KIND_CK}")
    op.execute(
        f"ALTER TABLE activity_log ADD CONSTRAINT {_AL_ACTOR_KIND_CK} "
        f"CHECK (actor_kind IN ({_ACTOR_KINDS_OLD})) NOT VALID"
    )

    op.execute("DROP FUNCTION IF EXISTS public.resolve_payment_connector(uuid)")

    op.execute("DROP POLICY IF EXISTS p_payment_customer_links ON payment_customer_links")
    op.drop_table("payment_customer_links")
    op.execute("DROP POLICY IF EXISTS p_payment_webhook_deliveries ON payment_webhook_deliveries")
    op.drop_table("payment_webhook_deliveries")
    op.execute("DROP POLICY IF EXISTS p_payment_object_links ON payment_object_links")
    op.drop_table("payment_object_links")
    op.execute("DROP POLICY IF EXISTS p_payment_connector_events ON payment_connector_events")
    op.drop_table("payment_connector_events")
    op.execute("DROP POLICY IF EXISTS p_payment_connectors ON payment_connectors")
    op.drop_table("payment_connectors")
