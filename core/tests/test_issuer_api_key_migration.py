"""Migration 0077 structure gate (per-issuer API keys).

Phase 1 of task 19b7e874. Verifies the DB-level guarantees the later phases rely
on: the two credential/idempotency tables and their RLS mode, the
``authenticate_issuer_api_key`` verifier, the fiscal-durability columns + the
client-scoped index, and -- the T18 check -- that ``actor_kind = 'issuer_api_key'``
is admitted by BOTH audit CHECK constraints (else every audited write from the
key path would raise a check_violation / 500).

Runs on the sync (owner) engine, like ``test_migrations.py``; assumes the test DB
is at ``alembic upgrade head``.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa


def _engine() -> sa.Engine:
    sync_url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not sync_url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC not set")
    return sa.create_engine(sync_url, future=True)


def test_actor_kind_checks_admit_issuer_api_key() -> None:
    """T18: both audit CHECK constraints widened to include issuer_api_key,
    without dropping the pre-existing kinds."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            # entity_revision's CHECK carries a historically doubled name (see
            # migration 0077); activity_log's is clean (raw baseline).
            for conname in (
                "ck_activity_log_actor_kind",
                "ck_entity_revision_ck_entity_revision_actor_kind",
            ):
                cdef = conn.execute(
                    sa.text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"
                    ),
                    {"n": conname},
                ).scalar_one()
                assert "issuer_api_key" in cdef, f"{conname} not widened: {cdef}"
                # A widening, not a replacement: a prior kind must survive.
                assert "mcp_token" in cdef, f"{conname} lost prior kinds: {cdef}"
    finally:
        engine.dispose()


def test_issuer_api_keys_table_and_rls() -> None:
    """issuer_api_keys exists with ENABLE (not FORCE) RLS -- the owner-run verify
    function must read a row with no tenant GUC."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = 'issuer_api_keys'"
                )
            ).one()
            assert row.relrowsecurity is True
            assert row.relforcerowsecurity is False
            # The grace-hash probe must be deterministic: a partial-unique index.
            idxdef = conn.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_issuer_api_keys_previous_secret_hash'"
                )
            ).scalar_one()
            assert "UNIQUE" in idxdef
            assert "previous_secret_hash IS NOT NULL" in idxdef
    finally:
        engine.dispose()


def test_api_idempotency_table_and_force_rls() -> None:
    """api_idempotency exists with FORCE RLS (no cross-tenant reader) and the
    issuer-scoped uniqueness claim."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = 'api_idempotency'"
                )
            ).one()
            assert row.relrowsecurity is True
            assert row.relforcerowsecurity is True
            cdef = conn.execute(
                sa.text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'uq_api_idempotency_claim'"
                )
            ).scalar_one()
            # Scoped to the ISSUER (not the key), so a rotation mid-retry keeps dedupe.
            assert "issuer_profile_id" in cdef
            assert "endpoint" in cdef
            assert "idempotency_key" in cdef
    finally:
        engine.dispose()


def test_authenticate_function_exists() -> None:
    """The SECURITY DEFINER verifier exists and returns the principal + perms."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            # OUT params live in the argument list (the result type is just
            # "SETOF record" for an OUT-param function).
            args = conn.execute(
                sa.text(
                    "SELECT pg_get_function_arguments(oid) FROM pg_proc "
                    "WHERE proname = 'authenticate_issuer_api_key'"
                )
            ).scalar_one()
            assert args.startswith("p_hash bytea"), args
            for col in ("out_key_id", "out_org_id", "out_issuer_profile_id", "out_permissions"):
                assert col in args, f"missing {col} in {args}"
    finally:
        engine.dispose()


def test_invoice_fiscal_durability_columns() -> None:
    """invoices gains progressivo_invio + nome_file and the (org, client) index."""
    engine = _engine()
    try:
        with engine.connect() as conn:
            cols = set(
                conn.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'invoices' "
                        "AND column_name IN ('progressivo_invio','nome_file')"
                    )
                ).scalars()
            )
            assert cols == {"progressivo_invio", "nome_file"}
            idx = conn.execute(
                sa.text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_invoices_org_client'")
            ).scalar_one_or_none()
            assert idx == 1
    finally:
        engine.dispose()
