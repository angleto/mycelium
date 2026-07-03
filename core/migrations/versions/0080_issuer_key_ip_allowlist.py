"""Issuer-key ops hardening: per-key IP allowlist (task d3dd69c3).

Adds ``issuer_api_keys.ip_allowlist`` (text[] of CIDR blocks, NULL = no
restriction) and recreates ``authenticate_issuer_api_key`` with two extra OUT
columns:

- ``out_ip_allowlist``  -- the key's allowlist, so the app-side gate can
  enforce it right after credential resolution (RLS is ENABLED on
  issuer_api_keys, so only the SECURITY DEFINER function can read the row
  before a tenant session exists);
- ``out_last_used_at``  -- the matched secret's last-use timestamp BEFORE the
  throttled bump, so the caller can flag a dormant key waking up (a security
  telemetry event) without a second query.

The matching itself stays in the application (``ipaddress`` CIDR semantics,
unit-testable); the function remains a pure credential resolver. OUT columns
change the return type, so this is a DROP + CREATE (with the explicit grant
harden_function_acls expects), not CREATE OR REPLACE.

Revision ID: 0080
Revises: 0079
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fn_body(*, with_allowlist: bool) -> str:
    """The verifier body; the 0077 shape plus (optionally) the two new OUT
    columns. Kept as one template so upgrade/downgrade cannot drift."""
    extra_out = (
        ",\n    OUT out_ip_allowlist text[],\n    OUT out_last_used_at timestamptz"
        if with_allowlist
        else ""
    )
    extra_select = ", k.ip_allowlist" if with_allowlist else ""
    extra_decl = "      v_allowlist text[];\n" if with_allowlist else ""
    extra_into = ", v_allowlist" if with_allowlist else ""
    extra_assign = (
        (
            "      out_ip_allowlist := v_allowlist;\n"
            "      out_last_used_at := CASE WHEN v_matched_prev "
            "THEN v_prev_last_used ELSE v_last_used END;\n"
        )
        if with_allowlist
        else ""
    )
    return f"""
CREATE FUNCTION public.authenticate_issuer_api_key(
    p_hash bytea,
    OUT out_key_id uuid,
    OUT out_org_id uuid,
    OUT out_issuer_profile_id uuid,
    OUT out_permissions text[],
    OUT out_matched_previous boolean{extra_out}
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
{extra_decl}      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      -- Probe 1: the current secret (unique index -> at most one row).
      SELECT k.id, k.org_id, k.issuer_profile_id, k.permissions,
             k.expires_at, k.revoked_at, k.last_used_at{extra_select}
        INTO v_id, v_org, v_issuer, v_perms, v_expires, v_revoked, v_last_used{extra_into}
        FROM issuer_api_keys k
        WHERE k.secret_hash = p_hash;

      -- Probe 2 (only on a current miss): the grace secret, with the grace
      -- window checked HERE, not in the shared gate below.
      IF v_id IS NULL THEN
        SELECT k.id, k.org_id, k.issuer_profile_id, k.permissions,
               k.expires_at, k.revoked_at, k.previous_secret_last_used_at{extra_select}
          INTO v_id, v_org, v_issuer, v_perms, v_expires, v_revoked, v_prev_last_used{extra_into}
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
      -- for the write, mirroring authenticate_agent_token. NOTE: the bump
      -- happens even if the app-side IP gate then denies -- last_used_at is
      -- telemetry ("the credential was presented and valid"), not authz.
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
{extra_assign}      RETURN NEXT;
    END
    $$
"""


def upgrade() -> None:
    op.add_column(
        "issuer_api_keys",
        sa.Column("ip_allowlist", sa.ARRAY(sa.Text()), nullable=True),
    )
    op.execute("DROP FUNCTION IF EXISTS public.authenticate_issuer_api_key(bytea)")
    op.execute(_fn_body(with_allowlist=True))
    op.execute("REVOKE ALL ON FUNCTION public.authenticate_issuer_api_key(bytea) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.authenticate_issuer_api_key(bytea) TO mycelium_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.authenticate_issuer_api_key(bytea)")
    op.execute(_fn_body(with_allowlist=False))
    op.execute("REVOKE ALL ON FUNCTION public.authenticate_issuer_api_key(bytea) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.authenticate_issuer_api_key(bytea) TO mycelium_app"
    )
    op.drop_column("issuer_api_keys", "ip_allowlist")
