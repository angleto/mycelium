"""Per-issuer-profile API keys: credential + verify fn + idempotency + fiscal durability.

Phase 1 of the "per-issuer-profile API keys + Invoice REST API + MCP invoice
tools" design (task 19b7e874). This migration lays only the schema; the service,
REST surface and MCP tools follow in later phases.

Adds:

- ``issuer_api_keys`` -- a long-lived credential scoped to ONE ``issuer_profile``
  (the cedente), NOT to a user. The secret is system-generated and stored only as
  a keyed hash (``HMAC-SHA256(pepper, raw)``, computed in the app); a non-secret
  ``key_public_id`` gives the UI handle. Rotation-with-grace keeps a
  ``previous_secret_hash`` valid until ``previous_secret_expires_at``.
- ``authenticate_issuer_api_key(bytea)`` -- a SECURITY DEFINER verifier shaped
  like ``authenticate_agent_token`` / ``authenticate_capability_token``: it looks
  a row up by hash with no tenant GUC (two-probe, current-hash-wins; the grace
  window is checked ONLY on the previous-hash branch, never in the shared gate,
  so the current secret is never NULL-failed). A single ``revoked_at`` (or an
  expired ``expires_at``) kills BOTH secrets. It bumps ``last_used_at`` /
  ``previous_secret_last_used_at`` at most once per 60s (hot-row / revoke-race
  hygiene on a public surface). RLS is ENABLE (not FORCE), same rationale as
  ``agent_tokens`` / ``capability_tokens``: the owner-run function must read a row
  with no ``app.current_org`` set.
- ``api_idempotency`` -- an atomic idempotency claim for the state-changing REST
  endpoints, unique on ``(issuer_profile_id, endpoint, idempotency_key)`` (scoped
  to the ISSUER so a key rotation mid-retry keeps dedupe). FORCE RLS like
  ``event_outbox``; carries a ``request_hash`` for body-mismatch detection and a
  ``response_snapshot`` filled after the mutation.
- ``invoices.progressivo_invio`` / ``invoices.nome_file`` -- persisted before
  dispatch and reused verbatim on retry so a resend collides with SdI's own
  filename dedupe rather than double-filing.
- widens ``ck_activity_log_actor_kind`` and ``ck_entity_revision_actor_kind`` to
  admit ``issuer_api_key`` (else every audited write from the key path would 500).
- ``ix_invoices_org_client`` -- the (org, client) composite for the
  client-scoped "last invoice" query at thousands scale.

Revision ID: 0077
Revises: 0076
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"

# The pre-0077 allowed actor kinds (baseline 0001 for activity_log, 0006 for
# entity_revision). Kept as a literal so the downgrade restores them exactly.
_ACTOR_KINDS_OLD = "'human_direct','human_api','human_telegram','agent_run','mcp_token','system'"
_ACTOR_KINDS_NEW = _ACTOR_KINDS_OLD + ",'issuer_api_key'"

# activity_log's CHECK was created raw in the baseline (clean name); entity_revision's
# carries a historically DOUBLED name -- 0006 defined it via sa.CheckConstraint with an
# explicit ``name=`` AND the ck_%(table)s_%(constraint_name)s naming convention prefixed
# it again. Use each table's real constraint name so DROP finds it.
_AL_ACTOR_KIND_CK = "ck_activity_log_actor_kind"
_ER_ACTOR_KIND_CK = "ck_entity_revision_ck_entity_revision_actor_kind"

# SECURITY DEFINER verifier. Shaped like ``authenticate_agent_token`` (baseline):
# blank the caller's GUCs, look the row up by hash, validate, restore the GUCs,
# and return the principal + permissions. Two-probe (current wins); the grace
# window is scoped to the previous-hash branch; owner-run so ENABLE-RLS does not
# apply to it. The throttled last-used bump is the only write.
_AUTH_FN = """
CREATE FUNCTION public.authenticate_issuer_api_key(
    p_hash bytea,
    OUT out_key_id uuid,
    OUT out_org_id uuid,
    OUT out_issuer_profile_id uuid,
    OUT out_permissions text[],
    OUT out_matched_previous boolean
) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_id uuid;
      v_org uuid;
      v_issuer uuid;
      v_perms text[];
      v_expires timestamptz;
      v_revoked timestamptz;
      v_last_used timestamptz;
      v_prev_last_used timestamptz;
      v_matched_prev boolean := false;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      -- Probe 1: the current secret (unique index -> at most one row).
      SELECT k.id, k.org_id, k.issuer_profile_id, k.permissions,
             k.expires_at, k.revoked_at, k.last_used_at
        INTO v_id, v_org, v_issuer, v_perms, v_expires, v_revoked, v_last_used
        FROM issuer_api_keys k
        WHERE k.secret_hash = p_hash;

      -- Probe 2 (only on a current miss): the grace secret, with the grace
      -- window checked HERE, not in the shared gate below.
      IF v_id IS NULL THEN
        SELECT k.id, k.org_id, k.issuer_profile_id, k.permissions,
               k.expires_at, k.revoked_at, k.previous_secret_last_used_at
          INTO v_id, v_org, v_issuer, v_perms, v_expires, v_revoked, v_prev_last_used
          FROM issuer_api_keys k
          WHERE k.previous_secret_hash = p_hash
            AND k.previous_secret_expires_at IS NOT NULL
            AND k.previous_secret_expires_at > now();
        IF v_id IS NOT NULL THEN
          v_matched_prev := true;
        END IF;
      END IF;

      -- Shared gate: revocation / expiry kill BOTH secrets (row-level).
      IF v_id IS NULL
         OR v_revoked IS NOT NULL
         OR v_expires <= now() THEN
        PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
        PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
        RETURN;
      END IF;

      -- Throttled last-used telemetry (>= 60s): avoids hot-row churn and a
      -- revoke TOCTOU on the public verify path. Set the GUC to the row's org
      -- for the write, mirroring authenticate_agent_token.
      PERFORM set_config('app.current_org', v_org::text, true);
      IF v_matched_prev THEN
        IF v_prev_last_used IS NULL OR v_prev_last_used < now() - interval '60 seconds' THEN
          UPDATE issuer_api_keys SET previous_secret_last_used_at = now(), updated_at = now()
            WHERE id = v_id;
        END IF;
      ELSE
        IF v_last_used IS NULL OR v_last_used < now() - interval '60 seconds' THEN
          UPDATE issuer_api_keys SET last_used_at = now(), updated_at = now()
            WHERE id = v_id;
        END IF;
      END IF;
      PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
      PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);

      out_key_id := v_id;
      out_org_id := v_org;
      out_issuer_profile_id := v_issuer;
      out_permissions := v_perms;
      out_matched_previous := v_matched_prev;
      RETURN NEXT;
    END
    $$;
"""


def upgrade() -> None:
    # --- issuer_api_keys ---------------------------------------------------
    op.create_table(
        "issuer_api_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The cedente this key acts for. The key belongs to the profile, not a
        # user, so it survives any operator's offboarding.
        sa.Column(
            "issuer_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Audit only (who minted it); NOT ownership -> set-null on user delete.
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        # Non-secret public handle from an INDEPENDENT random draw; the shown
        # prefix is ``mycelium_ik_`` + key_public_id. Never a slice of the raw.
        sa.Column("key_public_id", sa.String(length=24), nullable=False),
        # ``HMAC-SHA256(ISSUER_KEY_PEPPER, raw)`` -- computed in the app; a
        # DB-only dump is inert without the pepper.
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        # Whitelisted subset of {invoice:read, invoice:compose, invoice:send,
        # invoice:credit_note, invoice:download}; enforced at the service layer.
        sa.Column(
            "permissions",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        # Rotation-with-grace: the previous secret authenticates until its expiry.
        sa.Column("previous_secret_hash", sa.LargeBinary(), nullable=True),
        sa.Column("previous_secret_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("previous_secret_last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Mandatory (never a never-expiring key); the service caps it at 365d.
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
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
        sa.UniqueConstraint("secret_hash", name="uq_issuer_api_keys_secret_hash"),
        sa.UniqueConstraint("key_public_id", name="uq_issuer_api_keys_key_public_id"),
        sa.CheckConstraint(
            "length(name) >= 1 AND length(name) <= 120",
            name="ck_issuer_api_keys_name_len",
        ),
    )
    op.create_index("ix_issuer_api_keys_org_id", "issuer_api_keys", ["org_id"])
    op.create_index(
        "ix_issuer_api_keys_issuer_profile_id", "issuer_api_keys", ["issuer_profile_id"]
    )
    # Partial-unique guard on the grace hash (so probe 2 is deterministic).
    op.create_index(
        "uq_issuer_api_keys_previous_secret_hash",
        "issuer_api_keys",
        ["previous_secret_hash"],
        unique=True,
        postgresql_where=sa.text("previous_secret_hash IS NOT NULL"),
    )

    # ENABLE (not FORCE): the SECURITY DEFINER verify function reads a row as the
    # owner with no tenant GUC. mycelium_app is not the owner -> stays confined.
    op.execute("ALTER TABLE issuer_api_keys ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_issuer_api_keys ON issuer_api_keys "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE issuer_api_keys TO mycelium_app")

    op.execute(_AUTH_FN)
    op.execute("REVOKE ALL ON FUNCTION public.authenticate_issuer_api_key(bytea) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.authenticate_issuer_api_key(bytea) TO mycelium_app"
    )

    # --- api_idempotency ---------------------------------------------------
    op.create_table(
        "api_idempotency",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
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
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        # sha256 of the canonical request; a second call with the same key but a
        # different body is a 409/422 rather than silently returning the first.
        sa.Column("request_hash", sa.LargeBinary(), nullable=False),
        # Filled after the mutation commits (NULL between claim and completion).
        sa.Column("response_snapshot", postgresql.JSONB(), nullable=True),
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
        sa.UniqueConstraint(
            "issuer_profile_id",
            "endpoint",
            "idempotency_key",
            name="uq_api_idempotency_claim",
        ),
    )
    op.create_index("ix_api_idempotency_org_id", "api_idempotency", ["org_id"])
    # Supports the TTL purge (delete WHERE created_at < now() - ttl).
    op.create_index("ix_api_idempotency_created_at", "api_idempotency", ["created_at"])

    op.execute("ALTER TABLE api_idempotency ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_idempotency FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_api_idempotency ON api_idempotency "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE api_idempotency TO mycelium_app")

    # --- invoices: fiscal-filename durability + client-scoped index --------
    op.add_column("invoices", sa.Column("progressivo_invio", sa.String(length=10), nullable=True))
    op.add_column("invoices", sa.Column("nome_file", sa.String(length=80), nullable=True))
    op.create_index("ix_invoices_org_client", "invoices", ["org_id", "client_tag_id"])

    # --- widen the actor-kind CHECK constraints ----------------------------
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
    op.execute(f"ALTER TABLE entity_revision DROP CONSTRAINT {_ER_ACTOR_KIND_CK}")
    op.execute(
        f"ALTER TABLE entity_revision ADD CONSTRAINT {_ER_ACTOR_KIND_CK} "
        f"CHECK (actor_kind IN ({_ACTOR_KINDS_OLD}))"
    )
    op.execute(f"ALTER TABLE activity_log DROP CONSTRAINT {_AL_ACTOR_KIND_CK}")
    op.execute(
        f"ALTER TABLE activity_log ADD CONSTRAINT {_AL_ACTOR_KIND_CK} "
        f"CHECK (actor_kind IN ({_ACTOR_KINDS_OLD}))"
    )

    op.drop_index("ix_invoices_org_client", table_name="invoices")
    op.drop_column("invoices", "nome_file")
    op.drop_column("invoices", "progressivo_invio")

    op.execute("DROP POLICY IF EXISTS p_api_idempotency ON api_idempotency")
    op.drop_table("api_idempotency")

    op.execute("DROP FUNCTION IF EXISTS public.authenticate_issuer_api_key(bytea)")
    op.execute("DROP POLICY IF EXISTS p_issuer_api_keys ON issuer_api_keys")
    op.drop_table("issuer_api_keys")
